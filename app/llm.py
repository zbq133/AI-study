from __future__ import annotations

import json
import os
import re

from openai import APIConnectionError, DefaultHttpxClient, OpenAI
from pydantic import ValidationError

from .errors import BudgetExceeded, Cancelled, LLMBadJSON, wrap_llm_error

#: 报错里出现这些词，才认为是"这个模型不认 reasoning_effort"，才降级重试。
#: 别放宽：宽了会把真错误吞成两次调用。
_REASONING_WORDS = ("reasoning", "thinking", "effort")

#: httpx 默认读这些环境变量当代理。代理客户端没开、变量还挂着的时候，每个请求都会往
#: 一个没人监听的本地端口撞，报出来是"连不上模型服务"，可浏览器和 PowerShell 明明能上 ——
#: 这两天的那个故障就是这么来的，所以下面给它留了一次直连的机会。
_PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
               "ALL_PROXY", "all_proxy")


def env_proxy() -> str:
    """环境变量里挂着的那张代理，没有就是空串。顺序跟 httpx 一致：https 优先。"""
    for name in _PROXY_VARS:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def make_client(base_url: str, api_key: str, timeout: float, trust_env: bool = True):
    """建 openai 客户端。

    `trust_env=False` = 不看环境变量里的代理，直接连。使用 openai 自带的
    `DefaultHttpxClient`，兼容 1.x/2.x（httpx）与 3.x（httpx2），并把超时一起传入。
    """
    return OpenAI(base_url=base_url, api_key=api_key, timeout=timeout,
                  http_client=DefaultHttpxClient(timeout=timeout, trust_env=trust_env))


def _mentions_reasoning(exc: Exception) -> bool:
    text = f"{exc}".lower()
    return any(w in text for w in _REASONING_WORDS)


_FENCE = re.compile(r"^\s*```(?:json)?\s*\r?\n?(.*?)\r?\n?```\s*$",
                    re.DOTALL | re.IGNORECASE)


def _strip_fence(text: str) -> str:
    """剥掉模型偶尔多套的 ```json ... ``` 围栏，避免白白重试一次。"""
    m = _FENCE.match(text)
    return m.group(1).strip() if m else text


class LLM:
    """一层薄封装：记调用次数/token，并在每次调用前问一句要不要停。

    max_calls / max_tokens 是"这一次生成"的上限，撞上就抛 BudgetExceeded；
    should_stop 是个无参函数，返回 True 就抛 Cancelled。两者都在**调用之前**检查，
    所以停下时不会再多花一次钱。

    reasoning_effort 是界面上的"思考等级"（low/medium/high，空串=不传）。
    不是所有模型都认这个参数，所以第一次被拒时会**去掉它重试一次**，
    并把这个事实记在 `reasoning_note` 里 —— 界面上可以照实说，而不是假装生效了。

    连不上时会看一眼环境变量里有没有代理：有就说明大概率是"代理客户端没开、变量还挂着"，
    于是换成直连再试一次，把这件事记在 `proxy_note` 里。外部传进来的 client 不动它
    （那是调用方自己的事，测试里也用得上）。
    """

    def __init__(self, base_url="", api_key="", model="", timeout=240.0, client=None,
                 max_calls=None, max_tokens=None, should_stop=None,
                 reasoning_effort=""):
        self.client = client if client is not None else make_client(base_url, api_key, timeout)
        self._conn = (base_url, api_key, timeout)   # 只在自建客户端时才用来重建
        self._own_client = client is None
        self._tried_direct = False
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.should_stop = should_stop
        self.reasoning_effort = (reasoning_effort or "").strip()
        self.reasoning_note = ""
        self.proxy_note = ""

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def _kwargs(self, msgs, temperature):
        kw = {"model": self.model, "messages": msgs, "temperature": temperature,
              "response_format": {"type": "json_object"}}
        if self.reasoning_effort:
            kw["extra_body"] = {"reasoning_effort": self.reasoning_effort}
        return kw

    def _create(self, msgs, temperature):
        """发一次请求。两种降级都在这儿：代理连不上就直连，思考等级被拒就摘掉它。"""
        try:
            return self.client.chat.completions.create(**self._kwargs(msgs, temperature))
        except Exception as exc:
            if self._retry_without_proxy(exc):
                try:
                    return self.client.chat.completions.create(**self._kwargs(msgs, temperature))
                except Exception as exc2:
                    exc = exc2
            if not self.reasoning_effort or not _mentions_reasoning(exc):
                raise
            self.reasoning_note = ("当前模型不接受「思考等级」，这次已经按默认跑过去了。")
            self.reasoning_effort = ""
            return self.client.chat.completions.create(**self._kwargs(msgs, temperature))

    def _retry_without_proxy(self, exc: Exception) -> bool:
        """环境变量里挂着代理却连不上时，换成直连（只试一次）。

        判据只有一条：`APIConnectionError` 且环境里确实有代理变量。别放宽 ——
        4xx/5xx 是"连上了但对方不收"，换直连没用，重试只会多花一次钱。
        """
        if not self._own_client or self._tried_direct:
            return False
        if not isinstance(exc, APIConnectionError):
            return False
        proxy = env_proxy()
        if not proxy:
            return False
        self._tried_direct = True
        self.client = make_client(*self._conn, trust_env=False)
        self.proxy_note = (f"环境变量里的代理（{proxy}）连不上，这次改成直连跑过去了。"
                           "要一直直连就删掉 HTTP_PROXY / HTTPS_PROXY，或者把代理客户端开起来。")
        return True

    def _check(self) -> None:
        """花钱之前问一次：被叫停了吗？快到上限了吗？"""
        if self.should_stop is not None and self.should_stop():
            raise Cancelled()
        if self.max_calls is not None and self.calls >= self.max_calls:
            raise BudgetExceeded(
                f"这次已经调用 {self.calls} 次，到上限了",
                hint=f"上限来自 LESSON_MAX_CALLS={self.max_calls}，"
                     f"要跑完就调大它（或者把题数调小）。")
        if self.max_tokens is not None and self.tokens >= self.max_tokens:
            raise BudgetExceeded(
                f"这次已经用掉 {self.tokens} tokens，到上限了",
                hint=f"上限来自 LESSON_MAX_TOKENS={self.max_tokens}，要跑完就调大它。")

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        last = None
        for _ in range(retries + 1):
            self._check()
            try:
                resp = self._create(msgs, temperature)
            except Exception as exc:   # 鉴权/限流/超时/网络，全收敛成人话
                raise wrap_llm_error(exc) from exc
            self.calls += 1
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            choices = getattr(resp, "choices", None) or []
            msg = getattr(choices[0], "message", None) if choices else None
            if msg is None:
                last = ValueError("模型返回为空（没有 choices）")
                msgs.append({"role": "user",
                             "content": "上一次响应为空，请直接输出完整 JSON，不要任何解释。"})
                continue
            txt = (getattr(msg, "content", None) or "").strip()
            try:
                return model_cls.model_validate(json.loads(_strip_fence(txt)))
            except (ValidationError, json.JSONDecodeError, TypeError) as exc:
                last = exc
                msgs.append({"role": "assistant", "content": txt})
                msgs.append({"role": "user",
                             "content": f"上面的 JSON 未通过校验：{exc}\n只输出修正后的完整 JSON，不要任何解释。"})
        raise LLMBadJSON(f"JSON 连续校验失败：{last}")
