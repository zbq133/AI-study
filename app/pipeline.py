from __future__ import annotations

from .pdf import page_block_map, page_text_map
from .prompts import (OUTLINE_SYS, OUTLINE_USER, QUIZ_SYS, QUIZ_USER,
                      SECTION_SYS, SECTION_USER, VERIFY_SYS, VERIFY_USER)
from .schema import (DocIR, Lesson, LessonSection, Outline, QuizItem, QuizPack,
                     Section, VerifyResult, build_sections, extract_answer,
                     normalize, select_chapter, verify_refs)

MAX_CHARS = 18000


def _render(sections):
    return "\n".join(s.text for s in sections)[:MAX_CHARS]


def _render_sources(ir, sections, limit=MAX_CHARS):
    """把本章原文带页码渲染出来。

    出题阶段原来只把讲义喂给模型，却要求 source_ref 给"原文逐字 quote" ——
    它根本没看见原文，只能凭记忆改写一句塞进去。真跑一次就露馅了：三题都给了
    出处，三题逐字校验全落空，引用是模型自己润色过的句子，页码也是编的。
    """
    pages = set()
    for s in sections:
        pages.update(range(s.page_start, s.page_end + 1))
    lines = [f"[p{b.page}] {b.text}" for b in ir.blocks if b.page in pages]
    return "\n".join(lines)[:limit]


def plan_outline(llm, chapter, sections):
    return llm.json(OUTLINE_SYS,
                    OUTLINE_USER.format(chapter=chapter, text=_render(sections)),
                    Outline, temperature=0.2)


def write_section(llm, chapter, item, sections, page_text, blocks=None):
    pages = set(item.pages or [])
    picked = [s for s in sections if set(range(s.page_start, s.page_end + 1)) & pages]
    src = picked or sections
    lesson = llm.json(
        SECTION_SYS,
        SECTION_USER.format(chapter=chapter, title=item.title,
                            points="、".join(item.points or []), text=_render(src)),
        LessonSection, temperature=0.4)
    lesson.source_refs = verify_refs(lesson.source_refs, page_text, blocks)
    return lesson


def _solve_once(llm, stem, options):
    opts = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(options))
    try:
        r = llm.json(VERIFY_SYS, VERIFY_USER.format(stem=stem, options=opts),
                     VerifyResult, temperature=0.0)
    except Exception:
        return ""
    return extract_answer(r.answer, len(options))


def _solve_text(llm, stem):
    """填空题没有选项可打乱，按文本比对，不能走字母抠取。"""
    try:
        r = llm.json(VERIFY_SYS, VERIFY_USER.format(stem=stem, options=""),
                     VerifyResult, temperature=0.0)
    except Exception:
        return ""
    return (r.answer or "").strip()


def _rotate(n):
    """选项左旋一位：保证没有任何选项留在原位，答案必须跟着内容走。"""
    if n < 2:
        return list(range(n))
    return list(range(1, n)) + [0]


def _solve_independently(llm, q) -> bool:
    """原顺序解一次、打乱顺序再解一次，两次指向同一个选项才算这题可靠。

    同模型重解只能证明自洽，打乱选项才能查出「按字母位置上蒙」的题。
    """
    target = "".join(sorted(set((q.answer or "").strip().upper())))
    if not q.options:
        got = _solve_text(llm, q.stem)
        return bool(got) and normalize(got) == normalize(q.answer)
    first = _solve_once(llm, q.stem, q.options)
    if not first or first != target:
        return False
    order = _rotate(len(q.options))
    second = _solve_once(llm, q.stem, [q.options[i] for i in order])
    if not second:
        return False
    mapped = "".join(sorted({chr(65 + order[ord(c) - 65]) for c in second}))
    return mapped == target


def make_quiz(llm, lessons, n, page_text, blocks=None, source=""):
    body = "\n\n".join(
        f"### {l.title}\n{l.explanation_md}\n易错点：{'；'.join(l.pitfalls)}"
        for l in lessons)[:MAX_CHARS]

    kept = []
    seen = set()
    asked = n
    for _ in range(2):
        if asked <= 0:
            break
        pack = llm.json(QUIZ_SYS,
                        QUIZ_USER.format(n=asked, k=max(3, asked // 2),
                                         text=body, source=source),
                        QuizPack, temperature=0.5)
        before = len(kept)
        for q in pack.items:
            key = normalize(q.stem)[:120]
            if not key or key in seen:
                continue
            seen.add(key)
            # 铁律是「quote 必须能在原文逐字找到，找不到就丢弃」。原来只拦
            # 「给了 ref 但校验不过」，模型干脆不给 ref 时反而放行 —— 真跑
            # 一次就撞上了：两道题 source_ref 全是 null，照样进了讲义。
            # 没出处的题学生点不回原文，可溯源等于没做，一律丢弃。
            if not q.source_ref:
                continue
            if not verify_refs([q.source_ref], page_text, blocks):
                continue
            # 校验过了再把摘录里的换行压平：OCR 的行是断开的，逐字通过的 quote
            # 常常长这样「电磁波的传播可\n不需介质」，存进讲义很难看。
            q.source_ref.quote = " ".join(q.source_ref.quote.split())
            q.verified = _solve_independently(llm, q)
            if q.verified:
                kept.append(q)
        if len(kept) == before:
            break
        asked = n - len(kept)
    return kept[:n]


def build_lesson(llm, ir, chapter, n_quiz=5):
    ptext = page_text_map(ir)
    blocks = page_block_map(ir)
    sections = select_chapter(build_sections(ir), chapter)
    if not sections:
        raise ValueError(f"没找到章节：{chapter}（先看 extract_ir 打印的可用章节）")

    outline = plan_outline(llm, chapter, sections)
    lessons = [write_section(llm, chapter, it, sections, ptext, blocks)
               for it in outline.sections]
    quiz = make_quiz(llm, lessons, n_quiz, ptext, blocks,
                     source=_render_sources(ir, sections))

    return Lesson(
        chapter=chapter,
        learning_objectives=[p for it in outline.sections for p in it.points][:8],
        sections=lessons,
        summary_one_liner=(lessons[0].key_points[0]
                           if lessons and lessons[0].key_points else ""),
        summary_bullets=[k for l in lessons for k in l.key_points][:10],
        quiz=quiz,
    )