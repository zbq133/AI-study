import pytest
from pydantic import BaseModel

from app.errors import LLMBadJSON
from app.llm import LLM


class Payload(BaseModel):
    a: int


class _Msg:
    def __init__(self, content):
        self.content = content


class _Choice:
    def __init__(self, content):
        self.message = _Msg(content)


class _Usage:
    prompt_tokens = 11
    completion_tokens = 7


class _Resp:
    def __init__(self, content):
        self.choices = [_Choice(content)]
        self.usage = _Usage()


class _Completions:
    def __init__(self, seq):
        self.seq = list(seq)
        self.calls = 0
        self.last_messages = None

    def create(self, **kwargs):
        self.last_messages = kwargs.get("messages")
        i = min(self.calls, len(self.seq) - 1)
        self.calls += 1
        return _Resp(self.seq[i])


class _Chat:
    def __init__(self, seq):
        self.completions = _Completions(seq)


class StubClient:
    def __init__(self, seq):
        self.chat = _Chat(seq)


def test_json_parses_valid_payload():
    llm = LLM(client=StubClient(['{"a": 5}']), model="stub")
    assert llm.json("s", "u", Payload).a == 5
    assert llm.calls == 1
    assert llm.prompt_tokens == 11 and llm.completion_tokens == 7


def test_json_retries_after_bad_json():
    llm = LLM(client=StubClient(["不是 JSON", '{"a": 9}']), model="stub")
    assert llm.json("s", "u", Payload).a == 9
    assert llm.calls == 2


def test_json_raises_after_retries_exhausted():
    llm = LLM(client=StubClient(["坏"]), model="stub")
    with pytest.raises(LLMBadJSON) as ei:
        llm.json("s", "u", Payload, retries=1)
    assert ei.value.kind == "llm_bad_json"


def test_repair_prompt_is_appended():
    stub = StubClient(["坏", '{"a": 1}'])
    LLM(client=stub, model="stub").json("s", "u", Payload)
    msgs = stub.chat.completions.last_messages
    assert msgs[-1]["role"] == "user" and "JSON" in msgs[-1]["content"]


# ---------- 思考等级：认得就带上，认不得就摘掉重试一次 ----------

class _PickyCompletions(_Completions):
    """见到 extra_body 就报"不支持"，去掉之后才正常 —— 模拟不认这个参数的模型。"""

    def __init__(self, seq):
        super().__init__(seq)
        self.seen = []

    def create(self, **kwargs):
        self.seen.append(kwargs.get("extra_body"))
        if kwargs.get("extra_body"):
            raise ValueError("Unsupported parameter: 'reasoning_effort' is not supported")
        return super().create(**kwargs)


def test_reasoning_effort_is_sent_when_asked():
    stub = StubClient(['{"a": 1}'])
    llm = LLM(client=stub, model="stub", reasoning_effort="high")
    assert llm.json("s", "u", Payload).a == 1
    assert llm.reasoning_note == ""


def test_unsupported_reasoning_effort_falls_back_instead_of_failing():
    stub = StubClient(['{"a": 2}'])
    stub.chat.completions = _PickyCompletions(['{"a": 2}'])
    llm = LLM(client=stub, model="stub", reasoning_effort="medium")
    assert llm.json("s", "u", Payload).a == 2
    assert stub.chat.completions.seen == [{"reasoning_effort": "medium"}, None]
    assert llm.reasoning_note            # 被拒过，界面上要照实说
    assert llm.reasoning_effort == ""
    assert llm.calls == 1                # 降级那次没多记一次成功调用


def test_reasoning_effort_survives_an_unrelated_error():
    """别的错不该被当成"不认思考等级"吞掉重试。"""
    class _Boom(_Completions):
        def __init__(self):
            super().__init__([])

        def create(self, **kwargs):
            raise ValueError("bad request: messages[0] too long")

    stub = StubClient([])
    stub.chat.completions = _Boom()
    llm = LLM(client=stub, model="stub", reasoning_effort="high")
    with pytest.raises(Exception) as ei:
        llm.json("s", "u", Payload)
    assert "too long" in str(ei.value)
    assert llm.reasoning_effort == "high"   # 没被误摘
