"""真模型的回归 —— 默认不跑，`STUDY_LIVE=1` 且配好 key 才跑。

其余 128 条测试全用假桩，跑得快、不花钱，但它们证明不了「真模型现在还按我们的格式回话」。
模型侧一改（JSON 模式变了、字段名变了、限流策略变了），假桩测试全绿而线上全崩 ——
这个文件就是那道闸。

    $env:STUDY_LIVE = "1"
    .\.venv\Scripts\python.exe -m pytest tests/test_live.py -q -p no:warnings

两次调用，几毛钱，二三十秒。改动 `prompts.py`、`schema.py` 的字段、`llm.py` 的解析时都该跑一次。
"""

from __future__ import annotations

import os

import fitz
import pytest
from dotenv import load_dotenv

from app.errors import StudyError
from app.llm import LLM
from app.pdf import extract_ir, page_block_map, page_text_map
from app.pipeline import build_lesson
from app.prompts import OUTLINE_SYS, OUTLINE_USER
from app.schema import Outline, build_sections, verify_refs

LIVE = os.environ.get("STUDY_LIVE") == "1"
load_dotenv()                     # 和命令行/服务一样从项目根的 .env 取 key
HAVE_KEY = all(os.environ.get(k) for k in ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL"))

pytestmark = pytest.mark.skipif(
    not (LIVE and HAVE_KEY),
    reason="真模型回归要 STUDY_LIVE=1 且配好 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL",
)


TEXT = [
    ("Chapter 1 Photosynthesis", 20),
    ("Photosynthesis converts light energy into chemical energy stored in glucose.", 11),
    ("The light reactions happen in the thylakoid membranes and yield ATP and NADPH.", 11),
    ("The Calvin cycle fixes carbon dioxide with the enzyme RuBisCO.", 11),
    ("Chlorophyll absorbs blue and red light most strongly, which is why leaves look green.", 11),
    ("The overall equation is 6CO2 + 6H2O -> C6H12O6 + 6O2.", 11),
]


def _tiny_pdf(tmp_path):
    path = tmp_path / "live.pdf"
    doc = fitz.open()
    page = doc.new_page()
    y = 90
    for text, size in TEXT:
        page.insert_text((72, y), text, fontsize=size)
        y += size * 2.0
    doc.save(str(path))
    doc.close()
    return path


def _llm() -> LLM:
    return LLM(base_url=os.environ["LLM_BASE_URL"],
               api_key=os.environ["LLM_API_KEY"],
               model=os.environ["LLM_MODEL"], timeout=120.0)


def test_live_model_still_answers_the_outline_schema():
    """一次调用：证明 key 还能用、JSON 模式还认、Outline 的字段没被改坏。"""
    llm = _llm()
    outline = llm.json(OUTLINE_SYS, OUTLINE_USER.format(chapter="光合作用",
                                                       text="光合作用把光能转成化学能。"),
                       Outline, temperature=0.0)
    assert outline.sections, "模型回了个空大纲"
    assert all(s.title.strip() for s in outline.sections)
    assert llm.calls == 1 and llm.tokens > 0


def test_live_end_to_end_keeps_the_promise(tmp_path):
    """一条最短的真链路：解析 → 生成 → 每句出处都得能逐字校验回去。

    这是整个项目的卖点，所以真模型回归优先验它：讲义可以写得不好，
    但不能出现「点回去对不上原文」的引用 —— 那正是加 bbox 之前的老毛病。
    """
    path = _tiny_pdf(tmp_path)
    ir = extract_ir(path, ocr=False)
    secs = build_sections(ir)
    assert secs, "连章节都没切出来"
    chapter = secs[0].title

    llm = _llm()
    lesson = build_lesson(llm, ir, chapter, n_quiz=1)

    assert lesson.sections, "一节都没写出来"
    body = " ".join(s.explanation_md for s in lesson.sections)
    assert len(body) > 200, "正文短得不像解释"

    ptext = page_text_map(ir)
    pblocks = page_block_map(ir)
    refs = [r for s in lesson.sections for r in s.source_refs]
    refs += [q.source_ref for q in lesson.quiz if q.source_ref is not None]
    assert refs, "一条出处都没有 —— 校验全被丢了"
    for r in refs:
        assert verify_refs([type(r)(page=r.page, quote=r.quote)], ptext, pblocks), \
            f"出处校验不过：p{r.page} {r.quote!r}"

    assert llm.calls >= 2 and llm.tokens > 0


def test_live_bad_key_fails_loud_with_a_kind():
    """key 写错时抛的是分类过的错误，不是到处乱飞的原始异常。

    注意：`api_key` 只在 `__init__` 里用来建 client，建完之后改属性是没用的 ——
    所以要拿假 key 就得重新构造一个。第一版写成 `llm.api_key = "假的"` 然后期待它失败，
    结果那次调用拿的还是真 key，测试假绿了一次。
    """
    llm = LLM(base_url=os.environ["LLM_BASE_URL"],
              api_key="sk-this-key-is-not-real", model=os.environ["LLM_MODEL"],
              timeout=30.0)
    with pytest.raises(StudyError) as ei:
        llm.json(OUTLINE_SYS, "随便说点什么", Outline)
    assert ei.value.kind in ("llm_auth", "llm_down", "llm_quota", "unknown")
    assert ei.value.title and ei.value.hint

