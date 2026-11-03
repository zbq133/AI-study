"""把"出错"分成几种人看得懂的原因。

之前所有失败都只剩一句 `error`：OCR 没文字层、key 过期、模型被限流、想要的那一章
不存在，界面上长得一模一样，用户只能猜。这里给每种失败配一个 kind（程序认的）+
title/hint（人看的），并且把 openai 抛的那一堆类型收敛成四种。

约定：**只在这儿定义异常类，别在别处 raise 裸 Exception**。
"""
from __future__ import annotations


class StudyError(Exception):
    """所有能解释给自己听的失败。"""

    kind = "unknown"
    title = "出错了"
    hint = ""

    def __init__(self, message: str = "", hint: str = ""):
        super().__init__(message or self.title)
        self.message = message or self.title
        if hint:
            self.hint = hint


#: 这几类不是"这一道题不好"，是"这次跑不下去"（key 失效、限流、连不上、
#: 超预算、被叫停）。出题验证阶段逐题吞异常时，它们必须往上抛 —— 否则一次
#: 生成最后出 0 道题，用户只看到"没有生成出内容"，压根不知道配额用完了。
FATAL_KINDS = frozenset({"llm_auth", "llm_quota", "llm_down", "llm_timeout",
                         "llm_busy", "budget", "cancelled", "config"})


def is_fatal(exc: BaseException) -> bool:
    return getattr(exc, "kind", "unknown") in FATAL_KINDS


class EmptyDocument(StudyError):
    kind = "empty"
    title = "这份 PDF 里没有文字"
    hint = ("它多半是扫描件或 PPT 导出的一堆图片。开 OCR 也能读，但公式基本认不出来；"
            "认不出的部分讲义会缺内容。换一份带文字层的 PDF 更稳。")


class BadPDF(StudyError):
    kind = "bad_pdf"
    title = "这个文件打不开"
    hint = "确认它是完整的 PDF（不是改了扩展名的图片或加密文件），或者重新导出一遍。"


class MissingConfig(StudyError):
    kind = "config"
    title = "还差模型配置"
    hint = "在项目根目录的 .env 里填好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL，再重启服务。"


class ChapterNotFound(StudyError):
    kind = "chapter"
    title = "没找到这一章"
    hint = "重新解析一次看左侧目录，章节名要跟目录里一致（也可以用序号）。"


class Cancelled(StudyError):
    kind = "cancelled"
    title = "已取消"
    hint = "这次调用花掉的钱照算，但不会再有新的调用。"


class BudgetExceeded(StudyError):
    kind = "budget"
    title = "超出这次的成本上限"
    hint = ("到上限就停下，防止一次手滑烧太多。要放宽就调大 .env 里的 "
            "LESSON_MAX_CALLS / LESSON_MAX_TOKENS，或者把题数调小。")


class LLMAuth(StudyError):
    kind = "llm_auth"
    title = "模型那边不认这个 key"
    hint = "检查 .env 里的 LLM_API_KEY：有没有过期、余额、或者 base_url 写错成了别家的地址。"


class LLMQuota(StudyError):
    kind = "llm_quota"
    title = "模型在限流"
    hint = "等半分钟再试；一直这样就是账号额度/并发到了上限。"


class LLMTimeout(StudyError):
    kind = "llm_timeout"
    title = "模型太久没回"
    hint = "网络或对方太慢。重试一次通常就好；反复超时就把题数调小。"


class LLMUnavailable(StudyError):
    kind = "llm_down"
    title = "连不上模型服务"
    hint = "看 base_url 和网络（代理开着吗）。这类失败重试往往没用，先确认服务可用。"


class LLMBusy(StudyError):
    kind = "llm_busy"
    title = "模型服务繁忙"
    hint = "对方服务太忙（5xx），等半分钟再试；一直这样就是服务端过载。"


class LLMBadJSON(StudyError):
    kind = "llm_bad_json"
    title = "模型没按格式回话"
    hint = "自动重试过仍然不合格。可以重跑一次；总是这样就是模型不太听这个 prompt。"


#: openai 的异常类型名 → 我们的异常类。按名字认，不 import openai 的具体类，
#: 这样它换包/换版本也不会把这里带崩。
_BY_NAME = {
    "AuthenticationError": LLMAuth,
    "PermissionDeniedError": LLMAuth,
    "RateLimitError": LLMQuota,
    "APITimeoutError": LLMTimeout,
    "Timeout": LLMTimeout,
    "APIConnectionError": LLMUnavailable,
    "InternalServerError": LLMBusy,
    "APIStatusError": LLMUnavailable,
    "APIError": LLMUnavailable,
}


def wrap_llm_error(exc: BaseException) -> StudyError:
    """把 openai 抛的异常收敛成上面几种；认不出来的原样包一层。

    **先看状态码，再看类名。** 状态码是更准的信号：openai 的 `APIStatusError` 是个泛型
    父类名，503 也打这个类名，先按名字查就会把「对方在忙」说成「连不上」。
    """
    if isinstance(exc, StudyError):
        return exc
    code = getattr(exc, "status_code", None) or getattr(exc, "status", None)
    cls = None
    if code == 401 or code == 403:
        cls = LLMAuth
    elif code == 429:
        cls = LLMQuota
    elif code == 408 or code == 504:
        cls = LLMTimeout
    elif isinstance(code, int) and code >= 500:
        cls = LLMBusy
    if cls is None:
        cls = _BY_NAME.get(type(exc).__name__)
    if cls is None:
        return StudyError(f"{type(exc).__name__}: {exc}")
    return cls(str(exc)[:400])


def describe(exc: BaseException) -> dict:
    """给前端用的一句人话。detail 留着排查，title/hint 是给人看的。"""
    err = wrap_llm_error(exc)
    return {"kind": getattr(err, "kind", "unknown"),
            "title": getattr(err, "title", "出错了"),
            "hint": getattr(err, "hint", ""),
            "detail": str(exc)[:800]}
