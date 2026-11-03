"""错误分类的单元测试：认得出、说得清、不吞掉细节。"""
from app.errors import (BadPDF, Cancelled, ChapterNotFound, EmptyDocument, LLMAuth,
                        LLMBusy, LLMQuota, LLMTimeout, LLMUnavailable, MissingConfig,
                        StudyError, describe, wrap_llm_error)


class _Fake(Exception):
    """冒充 openai 的异常：只要类名对得上就该被认出来。"""

    status_code = None


def _named(name, code=None):
    cls = type(name, (_Fake,), {"status_code": code})
    return cls("boom")


def test_wraps_by_class_name():
    assert isinstance(wrap_llm_error(_named("AuthenticationError")), LLMAuth)
    assert isinstance(wrap_llm_error(_named("PermissionDeniedError")), LLMAuth)
    assert isinstance(wrap_llm_error(_named("RateLimitError")), LLMQuota)
    assert isinstance(wrap_llm_error(_named("APITimeoutError")), LLMTimeout)
    assert isinstance(wrap_llm_error(_named("APIConnectionError")), LLMUnavailable)


def test_wraps_by_status_code_when_the_name_is_new():
    """openai 换版本改了类名，靠状态码也要认得出。"""
    assert isinstance(wrap_llm_error(_named("WeirdError", 401)), LLMAuth)
    assert isinstance(wrap_llm_error(_named("WeirdError", 429)), LLMQuota)
    assert isinstance(wrap_llm_error(_named("WeirdError", 504)), LLMTimeout)
    assert isinstance(wrap_llm_error(_named("WeirdError", 503)), LLMBusy)


def test_status_code_wins_over_a_generic_class_name():
    """APIStatusError 是泛型父类名：503 也打这个名字，先按名字查会说成「连不上」。"""
    assert isinstance(wrap_llm_error(_named("APIStatusError", 503)), LLMBusy)
    assert isinstance(wrap_llm_error(_named("InternalServerError", 500)), LLMBusy)
    assert isinstance(wrap_llm_error(_named("APIStatusError", 429)), LLMQuota)
    # 没有状态码时才轮到名字
    assert isinstance(wrap_llm_error(_named("APIConnectionError")), LLMUnavailable)


def test_unknown_error_keeps_its_own_text():
    err = wrap_llm_error(_named("WeirdError", 418))
    assert type(err) is StudyError
    assert "WeirdError" in str(err)
    assert err.kind == "unknown"


def test_already_classified_passes_through():
    err = MissingConfig("缺 key")
    assert wrap_llm_error(err) is err


def test_every_kind_has_a_title_and_a_hint():
    """前端直接显示 title/hint，缺一个就是界面上空一块。"""
    for cls in (EmptyDocument, BadPDF, MissingConfig, ChapterNotFound, Cancelled,
                LLMAuth, LLMBusy, LLMQuota, LLMTimeout, LLMUnavailable):
        assert cls.title and len(cls.title) <= 20, cls
        assert cls.hint, cls
        assert cls.kind != "unknown", cls


def test_describe_is_json_shaped():
    d = describe(ChapterNotFound("没找到章节：第9章"))
    assert set(d) == {"kind", "title", "hint", "detail"}
    assert d["kind"] == "chapter"
    assert "第9章" in d["detail"]
    assert d["hint"] == ChapterNotFound.hint


def test_describe_truncates_a_huge_detail():
    d = describe(StudyError("x" * 5000))
    assert len(d["detail"]) == 800


def test_hint_can_be_overridden_per_instance():
    err = ChapterNotFound("没找到章节：第9章", hint="先解析")
    assert err.hint == "先解析"
    assert ChapterNotFound.hint != "先解析"      # 类属性没被改脏
