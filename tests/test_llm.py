import types

import pytest
from openai import APIConnectionError
from pydantic import BaseModel

import httpx2

from app import llm as llm_module
from app.errors import LLMBadJSON, LLMUnavailable
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


# ---------- 环境变量里挂着死代理：连不上就直连一次 ----------
#
# 真实故障现场：用户环境里有 HTTP_PROXY=http://127.0.0.1:7897，可代理客户端没开。
# httpx 默认吃这些变量，于是每个请求都往没人监听的端口撞 → APIConnectionError
# → 界面上一句"连不上模型服务"，而浏览器、PowerShell 明明是通的。


def _conn_error():
    return APIConnectionError(
        request=httpx2.Request("POST", "https://api.deepseek.com/chat/completions"))


class _DeadCompletions:
    def create(self, **kwargs):
        raise _conn_error()


class _DeadClient:
    """连不上的客户端，长得跟 openai 客户端一样。"""

    def __init__(self):
        self.chat = types.SimpleNamespace(completions=_DeadCompletions())


def _seq_make_client(clients):
    """替身 make_client：按顺序发客户端，并把每次的 trust_env 记下来。"""
    built = []
    queue = list(clients)

    def _make(base_url, api_key, timeout, trust_env=True):
        built.append(trust_env)
        return queue.pop(0)

    return _make, built


def _clear_proxy_env(monkeypatch):
    for name in llm_module._PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


def test_dead_proxy_in_env_falls_back_to_direct(monkeypatch):
    """代理没开、变量还挂着：直连重试一次，并且照实告诉人。"""
    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    made, built = _seq_make_client([_DeadClient(), StubClient(['{"a": 7}'])])
    monkeypatch.setattr(llm_module, "make_client", made)

    llm = LLM(base_url="https://api.deepseek.com", api_key="sk-test", model="stub")
    assert llm.json("s", "u", Payload).a == 7
    assert built == [True, False]        # 第二次是不看环境变量的直连
    assert "7897" in llm.proxy_note and "直连" in llm.proxy_note
    assert llm.calls == 1                # 换直连那次不算两次成功调用


def test_no_proxy_in_env_means_no_direct_retry(monkeypatch):
    """环境里本来就没代理：连不上就是连不上，别拿直连当万能重试。"""
    _clear_proxy_env(monkeypatch)
    made, built = _seq_make_client([_DeadClient()])
    monkeypatch.setattr(llm_module, "make_client", made)

    llm = LLM(base_url="https://api.deepseek.com", api_key="sk-test", model="stub")
    with pytest.raises(LLMUnavailable) as ei:
        llm.json("s", "u", Payload)
    assert ei.value.kind == "llm_down"
    assert built == [True]
    assert llm.proxy_note == ""


def test_a_real_http_error_is_not_retried_without_proxy(monkeypatch):
    """400/401 是"连上了但对方不收"，换直连没用。别放宽成两次调用。"""

    class _BoomClient:
        def __init__(self):
            self.chat = types.SimpleNamespace(completions=self)

        def create(self, **kwargs):
            raise RuntimeError("400 Bad Request: model not found")

    _clear_proxy_env(monkeypatch)
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7897")
    made, built = _seq_make_client([_BoomClient()])
    monkeypatch.setattr(llm_module, "make_client", made)

    llm = LLM(base_url="https://api.deepseek.com", api_key="sk-test", model="stub")
    with pytest.raises(Exception) as ei:
        llm.json("s", "u", Payload)
    assert "model not found" in str(ei.value)
    assert built == [True]
    assert llm.proxy_note == ""
