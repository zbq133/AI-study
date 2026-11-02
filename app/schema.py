from __future__ import annotations

import re
from typing import Literal, Optional

from pydantic import BaseModel

BlockKind = Literal["heading", "para", "list", "formula", "table", "figure", "caption"]

_WS = re.compile(r"[\s\u3000]+")


def normalize(t: str) -> str:
    return _WS.sub("", t or "")


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


def build_sections(ir: DocIR) -> list[Section]:
    order: list[tuple] = []
    box: dict[tuple, Section] = {}
    for b in ir.blocks:
        key = tuple(b.section_path) if b.section_path else ("__root__",)
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


def verify_refs(refs: list[SourceRef], page_text: dict[int, str]) -> list[SourceRef]:
    ok: list[SourceRef] = []
    for r in refs:
        hay = normalize(page_text.get(r.page, ""))
        needle = normalize(r.quote)
        if len(needle) >= 6 and needle in hay:
            ok.append(r)
    return ok