from __future__ import annotations

import json

from openai import OpenAI
from pydantic import ValidationError

from .errors import LLMBadJSON, wrap_llm_error


class LLM:
    def __init__(self, base_url="", api_key="", model="", timeout=240.0, client=None):
        self.client = client if client is not None else OpenAI(
            base_url=base_url, api_key=api_key, timeout=timeout)
        self.model = model
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        msgs = [{"role": "system", "content": system},
                {"role": "user", "content": user}]
        last = None
        for _ in range(retries + 1):
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