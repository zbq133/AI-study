import pytest
from pydantic import BaseModel

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
    with pytest.raises(RuntimeError):
        llm.json("s", "u", Payload, retries=1)


def test_repair_prompt_is_appended():
    stub = StubClient(["坏", '{"a": 1}'])
    LLM(client=stub, model="stub").json("s", "u", Payload)
    msgs = stub.chat.completions.last_messages
    assert msgs[-1]["role"] == "user" and "JSON" in msgs[-1]["content"]