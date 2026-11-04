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
    #: 走了 OCR 的页码。这些页的文字是识别出来的，公式与上下标不可信，
    #: 引用校验、讲义措辞都该知道这件事——别让下游把它当原生文字层用。
    ocr_pages: list[int] = []
    #: 每页的纸面尺寸（pt）。前端要把 bbox 换算成图片上的百分比，得知道原页多大。
    page_sizes: dict[int, tuple[float, float]] = {}


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
    #: quote 落在哪几个块上（记 Block.id）。前端点"出处"要能把原文那一行高亮
    #: 出来，只给页码 + 一串纯文本做不到 —— 得知道是哪几块。
    blocks: list[str] = []
    #: 这几块并起来的矩形（PDF 坐标，单位 pt）。没有原始 PDF 也能画出位置。
    bbox: Optional[tuple[float, float, float, float]] = None


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


_REFERENCE_TITLES = frozenset({"references", "reference", "bibliography",
                               "参考文献", "引用文献", "参考书目"})


def is_reference_title(title: str) -> bool:
    """这一节是不是参考文献列表。

    参考文献常吃掉全文三到四成篇幅（那份 15 页综述里 35392 字，占 40%），
    既不该出现在"可用章节"里让人误选，也不该占用素材预算。
    """
    return normalize(title).lower() in _REFERENCE_TITLES


def _locate(needle: str, blocks: list[Block]) -> Optional[tuple[int, int]]:
    """在若干块里找 needle，返回它覆盖的**块下标区间**（首、尾）。

    同一页里行的 y 坐标应当一路往下走；出现大幅回跳只可能是换了栏或跳到了
    另一段（双栏排版里左栏末尾接右栏开头就是这个形状），那属于拼接，不算引用。
    所以一路往后试，只认第一次 y 单调不回头的位置。
    """
    joined = ""
    owner: list[int] = []
    for i, b in enumerate(blocks):
        t = normalize(b.text)
        joined += t
        owner.extend([i] * len(t))
    if not joined or not needle:
        return None
    start = 0
    while True:
        pos = joined.find(needle, start)
        if pos < 0:
            return None
        first, last = owner[pos], owner[pos + len(needle) - 1]
        ys = [blocks[i].bbox[1] for i in range(first, last + 1)
              if blocks[i].bbox is not None]
        if all(b2 >= b1 - _Y_TOL for b1, b2 in zip(ys, ys[1:])):
            return first, last
        start = pos + 1


def _reading_order_ok(needle: str, blocks: list[Block]) -> bool:
    return _locate(needle, blocks) is not None


def locate_quote(needle: str, blocks: list[Block]) -> list[int]:
    """`quote` 盖住了哪几块（返回 blocks 里的下标）。定位不到就是空列表。

    `quote` 是纯文本，前端想"点一下就高亮原文那一行"必须知道落在哪些块上；
    只有页码的话，同一页几十行里根本找不着。
    """
    hit = _locate(needle, blocks)
    return list(range(hit[0], hit[1] + 1)) if hit else []


def _merge_bbox(blocks: list[Block], idxs: list[int]) -> Optional[tuple[float, float, float, float]]:
    boxes = [blocks[i].bbox for i in idxs if blocks[i].bbox is not None]
    if not boxes:
        return None
    return (min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes))


def verify_refs(refs: list[SourceRef], page_text: dict[int, str],
                blocks: Optional[dict[int, list[Block]]] = None) -> list[SourceRef]:
    ok: list[SourceRef] = []
    for r in refs:
        hay = normalize(page_text.get(r.page, ""))
        needle = normalize(r.quote)
        if len(needle) < 6 or needle not in hay:
            continue
        page_blocks = blocks.get(r.page) if blocks else None
        if not page_blocks:
            ok.append(r)
            continue
        idxs = locate_quote(needle, page_blocks)
        if not idxs:
            continue
        # 定位成功就把"在哪几块、占多大一块地方"一起写进 ref —— 下游（讲义、习题、
        # 前端高亮）全靠这两个字段把出处点回原文。
        ok.append(SourceRef(page=r.page, quote=r.quote,
                            blocks=[page_blocks[i].id for i in idxs],
                            bbox=_merge_bbox(page_blocks, idxs)))
    return ok