"""第 ② 步：给 LLM 加取消与成本闸的测试。全用桩客户端，不碰网络。"""
import pytest
from pydantic import BaseModel

from app.errors import BudgetExceeded, Cancelled
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

    def create(self, **kwargs):
        i = min(self.calls, len(self.seq) - 1)
        self.calls += 1
        return _Resp(self.seq[i])


class _Chat:
    def __init__(self, seq):
        self.completions = _Completions(seq)


class StubClient:
    def __init__(self, seq):
        self.chat = _Chat(seq)


def test_stop_before_the_first_call_costs_nothing():
    stub = StubClient(['{"a": 1}'])
    hits = {"n": 0}

    def should_stop():
        hits["n"] += 1
        return True

    llm = LLM(client=stub, model="stub", should_stop=should_stop)
    with pytest.raises(Cancelled):
        llm.json("s", "u", Payload)
    assert stub.chat.completions.calls == 0      # 一次都没发出去
    assert hits["n"] == 1


def test_cancel_after_a_few_calls_keeps_what_it_already_did():
    stub = StubClient(['{"a": 1}'])
    box = {"stop": False}
    llm = LLM(client=stub, model="stub", should_stop=lambda: box["stop"])
    assert llm.json("s", "u", Payload).a == 1
    assert llm.calls == 1
    box["stop"] = True
    with pytest.raises(Cancelled):
        llm.json("s", "u", Payload)
    assert llm.calls == 1                        # 没有新调用


def test_max_calls_stops_before_going_over():
    stub = StubClient(['{"a": 1}'])
    llm = LLM(client=stub, model="stub", max_calls=2)
    llm.json("s", "u", Payload)
    llm.json("s", "u", Payload)
    with pytest.raises(BudgetExceeded) as ei:
        llm.json("s", "u", Payload)
    assert ei.value.kind == "budget"
    assert "LESSON_MAX_CALLS" in ei.value.hint
    assert stub.chat.completions.calls == 2      # 第三次没发出去


def test_max_tokens_stops_before_the_next_call():
    """预算是"调用前"查的：最多超出一次调用的量，但绝不继续往下烧。"""
    stub = StubClient(['{"a": 1}'])
    llm = LLM(client=stub, model="stub", max_tokens=17)
    llm.json("s", "u", Payload)                  # 0 < 17 放行，用掉 18
    assert llm.tokens == 18
    with pytest.raises(BudgetExceeded) as ei:
        llm.json("s", "u", Payload)
    assert "LESSON_MAX_TOKENS" in ei.value.hint
    assert stub.chat.completions.calls == 1


def test_no_limits_means_no_limits():
    stub = StubClient(['{"a": 1}'])
    llm = LLM(client=stub, model="stub")
    assert llm.max_calls is None and llm.max_tokens is None
    for _ in range(3):
        llm.json("s", "u", Payload)
    assert llm.calls == 3
