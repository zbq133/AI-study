from __future__ import annotations

import re

from .errors import ChapterNotFound, StudyError, is_fatal
from .pdf import page_block_map, page_text_map
from .prompts import (OUTLINE_SYS, OUTLINE_USER, QUIZ_SYS, QUIZ_USER,
                      SECTION_SYS, SECTION_USER, VERIFY_SYS, VERIFY_USER,
                      style_block)
from .schema import (DocIR, Lesson, LessonSection, Outline, QuizItem, QuizPack,
                     Section, VerifyResult, build_sections, extract_answer,
                     normalize, select_chapter, verify_refs)

MAX_CHARS = 18000

# 同一句结论两次写出来，差别常常只在句号、逗号和空格上（"同一句。" / "同一句"），
# 所以去重前把这些全丢掉再看。
_DE_DUP = re.compile(r"[\s\u3000。，、；：！？,.．;:!?\"'“”‘’()（）\[\]【】]+")


def _dedup_key(text: str) -> str:
    return _DE_DUP.sub("", text or "").lower()


# 英文折行处要补空格（"the black" + "mass" 不能粘成 "theblackmass"），中文折行
# 补空格就难看（"机械波的传播需 有传播振动的介质"）—— 只在两侧都不是 ASCII
# 的时候把空格吃掉。
_SPACE_BETWEEN_CJK = re.compile(r"(?<=[^\x00-\x7f])\s+(?=[^\x00-\x7f])")


def _squeeze(text: str) -> str:
    """把逐字抄来的多行 quote 压成一行，中文之间不留空格。"""
    return _SPACE_BETWEEN_CJK.sub("", " ".join((text or "").split()))


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    """按行边界截断，不切在句子中间。

    `text[:18000]` 会切在半句话上，模型看见半句就顺着把后半句补完 —— 补出来的
    还带着原文的语气，最容易被当成材料本身。
    """
    if len(text) <= limit:
        return text
    out, used = [], 0
    for line in text.splitlines():
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    # 一行都放不下（单行超长）时才退回硬截，否则整节没素材比截半句更糟
    return "\n".join(out) if out else text[:limit]


def _render(sections):
    return _clip("\n".join(s.text for s in sections))


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
    return _clip("\n".join(lines), limit)


def plan_outline(llm, chapter, sections):
    return llm.json(OUTLINE_SYS,
                    OUTLINE_USER.format(chapter=chapter, text=_render(sections)),
                    Outline, temperature=0.2)


def write_section(llm, chapter, item, sections, page_text, blocks=None, style=""):
    pages = set(item.pages or [])
    picked = [s for s in sections if set(range(s.page_start, s.page_end + 1)) & pages]
    src = picked or sections
    lesson = llm.json(
        SECTION_SYS + style_block(style),
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
    except StudyError as exc:
        # 答得牛头不对马嘴 = 这题不好，丢掉就行；但 key 失效 / 限流 / 超预算是
        # "这次跑不下去"，再往下逐题吞掉只会安静地交出 0 道题，人还以为是材料不行。
        if is_fatal(exc):
            raise
        return ""
    except Exception:
        return ""
    return extract_answer(r.answer, len(options))


def _solve_text(llm, stem):
    """填空题没有选项可打乱，按文本比对，不能走字母抠取。"""
    try:
        r = llm.json(VERIFY_SYS, VERIFY_USER.format(stem=stem, options=""),
                     VerifyResult, temperature=0.0)
    except StudyError as exc:
        if is_fatal(exc):
            raise
        return ""
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


def make_quiz(llm, lessons, n, page_text, blocks=None, source="", on_step=None,
              style=""):
    """on_step 每判完一道题调一次：{"stage": "verify", "index": i, "total": n}。

    出题阶段每道题要重解两遍（原顺序 + 打乱），是整条流水线里最闷的一段，
    进度条停在那里十几秒不动，人会以为卡死了。
    """
    def step(**info):
        if on_step is not None:
            on_step(info)

    body = "\n\n".join(
        f"### {l.title}\n{l.explanation_md}\n易错点：{'；'.join(l.pitfalls)}"
        for l in lessons)[:MAX_CHARS]

    kept = []
    seen = set()
    asked = n
    judged = 0
    for _ in range(2):
        if asked <= 0:
            break
        pack = llm.json(QUIZ_SYS + style_block(style),
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
            ok = verify_refs([q.source_ref], page_text, blocks)
            if not ok:
                continue
            # 换掉而不是改原对象：verify_refs 顺手把 blocks / bbox 填好了，
            # 前端要高亮原文靠的就是这两个字段。
            q.source_ref = ok[0]
            # 校验过了再把摘录里的换行压平：OCR 的行是断开的，逐字通过的 quote
            # 常常长这样「电磁波的传播可\n不需介质」，存进讲义很难看。
            q.source_ref.quote = _squeeze(q.source_ref.quote)
            q.verified = _solve_independently(llm, q)
            if q.verified:
                kept.append(q)
            judged += 1
            step(stage="verify", index=judged, total=max(1, len(pack.items)))
        if len(kept) == before:
            break
        asked = n - len(kept)
    return kept[:n]


def _one_liner(chapter, outline) -> str:
    """一句话交代这一章分几块、讲的什么。

    原来是拿第一节的第一条要点顶替，结果和 summary_bullets 的第一条一字不差，
    同一句话在讲义开头和结尾各出现一次。改成列结构，读者一眼看到整章骨架。
    """
    titles = [it.title.strip() for it in outline.sections if it.title.strip()]
    if not titles:
        return chapter
    return f"本章分 {len(titles)} 块讲：{'、'.join(titles)}。"


def _bullets(lessons, limit: int = 10) -> list[str]:
    """各节关键论断，去重后取前 limit 条。

    同一句结论常被相邻两节各写一遍（换介质那段在第 5、6 条几乎一字不差），
    所以按 _dedup_key 比，标点和空格不同也算同一句；每节最多留 2 条，免得
    一节把整页占满。
    """
    out, seen = [], set()
    for lesson in lessons:
        kept = 0
        for point in lesson.key_points:
            if kept >= 2 or len(out) >= limit:
                break
            key = _dedup_key(point)
            if not key or key in seen:
                continue
            seen.add(key)
            out.append(point)
            kept += 1
        if len(out) >= limit:
            break
    return out[:limit]


def build_lesson(llm, ir, chapter, n_quiz=5, on_step=None, style=""):
    """跑完整条流水线。

    on_step 每推进一格调一次，收到 {"stage": ..., ...}：outline / section /
    quiz / done。命令行不传；Web 那边拿它推进度条 —— 一次生成几十秒、十几
    次模型调用，中间什么都不显示的话，人只会以为卡死了。
    """

    def step(**info):
        if on_step is not None:
            on_step(info)

    ptext = page_text_map(ir)
    blocks = page_block_map(ir)
    sections = select_chapter(build_sections(ir), chapter)
    if not sections:
        raise ChapterNotFound(
            f"没找到章节：{chapter}",
            hint="重新解析一次看左侧目录，章节名要对得上（也可以直接填序号）。")

    step(stage="outline")
    outline = plan_outline(llm, chapter, sections)
    total = len(outline.sections)
    lessons = []
    for i, item in enumerate(outline.sections, 1):
        step(stage="section", index=i, total=total, title=item.title)
        lessons.append(write_section(llm, chapter, item, sections, ptext, blocks, style))
    step(stage="quiz")
    quiz = make_quiz(llm, lessons, n_quiz, ptext, blocks,
                     source=_render_sources(ir, sections), on_step=on_step, style=style)
    step(stage="done")

    return Lesson(
        chapter=chapter,
        learning_objectives=[p for it in outline.sections for p in it.points][:8],
        sections=lessons,
        summary_one_liner=_one_liner(chapter, outline),
        summary_bullets=_bullets(lessons),
        quiz=quiz,
    )