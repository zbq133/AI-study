from __future__ import annotations

from .pdf import page_text_map
from .prompts import (OUTLINE_SYS, OUTLINE_USER, QUIZ_SYS, QUIZ_USER,
                      SECTION_SYS, SECTION_USER, VERIFY_SYS, VERIFY_USER)
from .schema import (DocIR, Lesson, LessonSection, Outline, QuizItem, QuizPack,
                     Section, VerifyResult, build_sections, normalize,
                     select_chapter, verify_refs)

MAX_CHARS = 18000


def _render(sections):
    return "\n".join(s.text for s in sections)[:MAX_CHARS]


def plan_outline(llm, chapter, sections):
    return llm.json(OUTLINE_SYS,
                    OUTLINE_USER.format(chapter=chapter, text=_render(sections)),
                    Outline, temperature=0.2)


def write_section(llm, chapter, item, sections, page_text):
    pages = set(item.pages or [])
    picked = [s for s in sections if set(range(s.page_start, s.page_end + 1)) & pages]
    src = picked or sections
    lesson = llm.json(
        SECTION_SYS,
        SECTION_USER.format(chapter=chapter, title=item.title,
                            points="、".join(item.points or []), text=_render(src)),
        LessonSection, temperature=0.4)
    lesson.source_refs = verify_refs(lesson.source_refs, page_text)
    return lesson


def _solve_independently(llm, q):
    opts = "\n".join(f"{chr(65 + i)}. {o}" for i, o in enumerate(q.options))
    try:
        r = llm.json(VERIFY_SYS, VERIFY_USER.format(stem=q.stem, options=opts),
                     VerifyResult, temperature=0.0)
    except Exception:
        return ""
    return (r.answer or "").strip().upper()


def make_quiz(llm, lessons, n, page_text):
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
                        QUIZ_USER.format(n=asked, k=max(3, asked // 2), text=body),
                        QuizPack, temperature=0.5)
        before = len(kept)
        for q in pack.items:
            key = normalize(q.stem)[:120]
            if not key or key in seen:
                continue
            seen.add(key)
            if q.source_ref and not verify_refs([q.source_ref], page_text):
                q.source_ref = None
            q.verified = _solve_independently(llm, q) == (q.answer or "").strip().upper()
            if q.verified:
                kept.append(q)
        if len(kept) == before:
            break
        asked = n - len(kept)
    return kept[:n]


def build_lesson(llm, ir, chapter, n_quiz=5):
    ptext = page_text_map(ir)
    sections = select_chapter(build_sections(ir), chapter)
    if not sections:
        raise ValueError(f"没找到章节：{chapter}（先看 extract_ir 打印的可用章节）")

    outline = plan_outline(llm, chapter, sections)
    lessons = [write_section(llm, chapter, it, sections, ptext)
               for it in outline.sections]
    quiz = make_quiz(llm, lessons, n_quiz, ptext)

    return Lesson(
        chapter=chapter,
        learning_objectives=[p for it in outline.sections for p in it.points][:8],
        sections=lessons,
        summary_one_liner=(lessons[0].key_points[0]
                           if lessons and lessons[0].key_points else ""),
        summary_bullets=[k for l in lessons for k in l.key_points][:10],
        quiz=quiz,
    )