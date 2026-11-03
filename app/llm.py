from __future__ import annotations

import json

from openai import OpenAI
from pydantic import ValidationError

from .errors import BudgetExceeded, Cancelled, LLMBadJSON, wrap_llm_error


class LLM:
    """一层薄封装：记调用次数/token，并在每次调用前问一句要不要停。

    max_calls / max_tokens 是"这一次生成"的上限，撞上就抛 BudgetExceeded；
    should_stop 是个无参函数，返回 True 就抛 Cancelled。两者都在**调用之前**检查，
    所以停下时不会再多花一次钱。
    """

    def __init__(self, base_url="", api_key="", model="", timeout=240.0, client=None,
                 max_calls=None, max_tokens=None, should_stop=None):
        self.client = client if client is not None else OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.max_calls = max_calls
        self.max_tokens = max_tokens
        self.should_stop = should_stop

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

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
                resp = self.client.chat.completions.create(
                    model=self.model, messages=msgs, temperature=temperature,
                    response_format={"type": "json_object"})
            except Exception as exc:   # 鉴权/限流/超时/网络，全收敛成人话
                raise wrap_llm_error(exc) from exc
            self.calls += 1
            usage = getattr(resp, "usage", None)
            if usage is not None:
                self.prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                self.completion_tokens += getattr(usage, "completion_tokens", 0) or 0
            txt = (resp.choices[0].message.content or "").strip()
            try:
                return model_cls.model_validate(json.loads(txt))
            except (ValidationError, json.JSONDecodeError, TypeError) as exc:
                last = exc
                msgs.append({"role": "assistant", "content": txt})
                msgs.append({"role": "user",
                             "content": f"上面的 JSON 未通过校验：{exc}\n只输出修正后的完整 JSON，不要任何解释。"})
        raise LLMBadJSON(f"JSON 连续校验失败：{last}")
