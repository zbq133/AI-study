from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel

BlockKind = Literal["heading", "para", "list", "formula", "table", "figure", "caption"]

_WS = re.compile(r"[\s\u3000]+")
_ANS_TOKEN = re.compile(r"(?<![A-Za-z])([A-H]{1,4})(?![A-Za-z])")
_ANS_PUNCT = re.compile(r"[\s.,;:!?'\"，。；：！？、（）()\[\]【】“”‘’]+")
_Y_TOL = 20.0


def normalize(t: str) -> str:
    return _WS.sub("", t or "")


def extract_answer(raw: str, n_options: int = 0) -> str:
    """把独立解题返回的自由文本收成选项字母串。

    'B' / 'B. 乙' / '答案是 B' / 'AC' 都能收成字母；越界的字母一律作废。
    """
    text = (raw or "").strip()
    if not text:
        return ""
    up = text.upper()
    flat = _ANS_PUNCT.sub("", up)
    if re.fullmatch(r"[A-H]{1,4}", flat):
        cand = flat
    else:
        hits = _ANS_TOKEN.findall(up)
        cand = hits[-1] if hits else ""
    letters = "".join(sorted(set(cand)))
    if n_options and any(ord(c) - 65 >= n_options for c in letters):
        return ""
    return letters


class Block(BaseModel):
    id: str
    kind: BlockKind
    text: str
    page: int
    level: int = 0
    section_path: list[str] = []
    bbox: Optional[tuple[float, float, float, float]] = None


class DocIR(BaseModel):
    doc_id: str
    filename: str
    page_count: int
    blocks: list[Block]
    toc: list[list] = []


class Section(BaseModel):
    title: str
    path: list[str]
    page_start: int
    page_end: int
    text: str = ""
    block_ids: list[str] = []


class SourceRef(BaseModel):
    page: int
    quote: str


class LessonSection(BaseModel):
    title: str
    explanation_md: str
    key_points: list[str] = []
    pitfalls: list[str] = []
    source_refs: list[SourceRef] = []


class OutlineItem(BaseModel):
    title: str
    points: list[str] = []
    pages: list[int] = []


class Outline(BaseModel):
    sections: list[OutlineItem] = []


class QuizItem(BaseModel):
    type: Literal["single", "multi", "fill"] = "single"
    stem: str
    options: list[str] = []
    answer: str
    explanation: str = ""
    difficulty: int = 2
    knowledge_point: str = ""
    source_ref: Optional[SourceRef] = None
    verified: bool = False


class QuizPack(BaseModel):
    items: list[QuizItem] = []


class VerifyResult(BaseModel):
    answer: str = ""
    confidence: float = 0.0
    reason: str = ""


class Lesson(BaseModel):
    chapter: str
    learning_objectives: list[str] = []
    sections: list[LessonSection] = []
    summary_one_liner: str = ""
    summary_bullets: list[str] = []
    quiz: list[QuizItem] = []


_ROOT_LABEL = "（正文前）"


def build_sections(ir: DocIR) -> list[Section]:
    order: list[tuple] = []
    box: dict[tuple, Section] = {}
    for b in ir.blocks:
        # 首页的页眉、期刊名、作者信息出现在第一个标题之前，section_path 是空的。
        # 以前这里塞 "__root__" 当键，会一路漏到 CLI 的"可用章节"列表和讲义标题上，
        # 主人看到的就是一个叫 __root__ 的章节。
        key = tuple(b.section_path) if b.section_path else (_ROOT_LABEL,)
        if key not in box:
            box[key] = Section(title=key[-1], path=list(key),
                               page_start=b.page, page_end=b.page)
            order.append(key)
        s = box[key]
        s.page_start = min(s.page_start, b.page)
        s.page_end = max(s.page_end, b.page)
        s.block_ids.append(b.id)
        s.text += f"[p{b.page}] {b.text}\n"
    return [box[k] for k in order]


def select_chapter(sections: list[Section], query: str) -> list[Section]:
    q = normalize(query)
    if not q:
        return []
    start = None
    for i, s in enumerate(sections):
        if q in normalize("".join(s.path)):
            start = i
            break
    if start is None:
        return []
    root = sections[start]
    base_len = len(root.path)
    out = [root]
    for s in sections[start + 1:]:
        if len(s.path) <= base_len:
            break
        out.append(s)
    return out


def _reading_order_ok(needle: str, blocks: list[Block]) -> bool:
    """quote 必须落在阅读顺序连续的若干行里。

    同一页里行的 y 坐标应当一路往下走；出现大幅回跳只可能是换了栏或跳到了
    另一段（双栏排版里左栏末尾接右栏开头就是这个形状），那属于拼接，不算引用。
    """
    joined = ""
    owner: list[int] = []
    for i, b in enumerate(blocks):
        t = normalize(b.text)
        joined += t
        owner.extend([i] * len(t))
    if not joined or not needle:
        return False
    start = 0
    while True:
        pos = joined.find(needle, start)
        if pos < 0:
            return False
        first, last = owner[pos], owner[pos + len(needle) - 1]
        ys = [blocks[i].bbox[1] for i in range(first, last + 1)
              if blocks[i].bbox is not None]
        if all(b2 >= b1 - _Y_TOL for b1, b2 in zip(ys, ys[1:])):
            return True
        start = pos + 1


def verify_refs(refs: list[SourceRef], page_text: dict[int, str],
                blocks: Optional[dict[int, list[Block]]] = None) -> list[SourceRef]:
    ok: list[SourceRef] = []
    for r in refs:
        hay = normalize(page_text.get(r.page, ""))
        needle = normalize(r.quote)
        if len(needle) < 6 or needle not in hay:
            continue
        page_blocks = blocks.get(r.page) if blocks else None
        if page_blocks and not _reading_order_ok(needle, page_blocks):
            continue
        ok.append(r)
    return ok