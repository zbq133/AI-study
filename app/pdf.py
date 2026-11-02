from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

import fitz

from .schema import Block, DocIR

HEAD_NUM = re.compile(
    r"^(第[一二三四五六七八九十百0-9]+[章节讲篇部]|\d+(\.\d+){0,3}[\s、.]|[一二三四五六七八九十]+、)"
)


class NoTextLayer(RuntimeError):
    """整份 PDF 提不出任何文字行（扫描件、图片型课件，或文字被做了轮廓化）。

    以前这种情况会一路返回空 IR：0 块 → 0 章节 → CLI 打印一张空表还退出 0，
    看上去像"解析成功但文件没内容"，排查方向全被带偏。
    """


_TOC_PUNCT = re.compile(r"[\s．.。、,，:：;；\-—_()（）\[\]【】/／]+")

#: 节号前缀。出版方常用 "§10.1 机械波" 表示节，而且后面常常不跟空格。
_SECTION_NO = re.compile(r"^§+\s*\d")

#: 章级标题。PPT 课件的分章痕迹只有这一种，其余（§节号、栏目名、示意图标签）
#: 都不该把一个 PDF 切碎 —— 切碎了每章的素材还不够讲一节。
_CHAPTER_TITLE = re.compile(
    r"^(第\s*[0-9一二三四五六七八九十百千]+\s*[章讲部篇]"
    r"|Chapter\s*\d+|CHAPTER\s*\d+|Lecture\s*\d+)"
)

_TITLE_MAX_CHARS = 42
_SENTENCE_END = ("。", ".", "，", ",", "；", ";", "：", ":")
_MIN_TITLE_AR = 2.5


def _aspect(bbox) -> float:
    if not bbox:
        return 0.0
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    return w / h if h > 0 else 0.0


def _plausible_title(text: str, ar: float) -> bool:
    """一行字像不像标题。

    挡掉真文件里踩到的三类误判：
    - 单字："十""四""五"（OCR 常把列表序号单独切出来，原生层里也有孤立序号行）
    - 竖排或方框："两类波的不同之处"这种侧栏标签、示意图里的"波源""介质"
      —— 它们的框又高又窄，宽高比 0.1~2.0
    - 整句：句末带标点的一定是正文（"电磁波交变电磁场在空间的传播."）
    """
    t = text.strip()
    if len(t) < 2 or len(t) > _TITLE_MAX_CHARS:
        return False
    if ar < _MIN_TITLE_AR:
        return False
    return not t.endswith(_SENTENCE_END)


def _toc_key(t: str) -> str:
    """TOC 条目与正文标题的比对键：忽略空白，以及编号后的点号差异。

    出版方的书签常写 "1 Introduction"、"10.1 Heat treatment in air"，
    正文却是 "1. Introduction"、"10.1. Heat treatment in air"。
    原来只调 normalize（只去空白）→ 整份 TOC 一条也命中不了，
    层级全部退化成字号启发式，长标题还会被长度阈值挡掉、章节边界直接丢。
    """
    return _TOC_PUNCT.sub("", t or "").lower()


def _mode_size(sizes):
    c = Counter(round(s, 1) for s in sizes)
    return c.most_common(1)[0][0] if c else 10.0


def _page_lines(page):
    out = []
    for b in page.get_text("dict")["blocks"]:
        if b.get("type") != 0:
            continue
        for ln in b.get("lines", []):
            spans = ln.get("spans") or []
            text = "".join(s["text"] for s in spans).strip()
            if not text:
                continue
            size = max(s["size"] for s in spans)
            bold = any(("Bold" in (s.get("font") or "")) or ("Heavy" in (s.get("font") or ""))
                       for s in spans)
            out.append((text, size, bold, tuple(ln["bbox"])))
    return out


def _order(lines, page_w):
    if not lines:
        return []
    mid = page_w / 2.0
    left = [l for l in lines if l[3][0] < mid - 20]
    right = [l for l in lines if l[3][0] >= mid - 20]
    span = [l for l in lines if l[3][0] < mid and l[3][2] > mid + 20]
    key = lambda l: (round(l[3][1], 1), l[3][0])
    if len(left) > 0.3 * len(lines) and len(right) > 0.3 * len(lines) and len(span) <= 2:
        return sorted(left, key=key) + sorted(right, key=key)
    return sorted(lines, key=key)


def extract_ir(path, *, ocr: bool = True, ocr_cache="out/ocr") -> DocIR:
    """把 PDF 变成 DocIR。

    ocr=True 时，整页没有文字层的页面（PPT 导出的图片型课件）会走 OCR 补文字层；
    有文字层的页永远走原生提取。一页 OCR 约 10 秒，默认缓存到 out/ocr/<doc_id>/，
    传 ocr_cache=None 关掉缓存。
    """
    path = Path(path)
    doc = fitz.open(str(path))
    doc_id = hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    if ocr_cache:
        ocr_cache = Path(ocr_cache)

    raw = []
    ocr_pages = []
    for i, page in enumerate(doc):
        lines = _page_lines(page)
        if not lines and ocr:
            from .ocr import ocr_page_cached

            lines = ocr_page_cached(page, doc_id, i + 1, cache_dir=ocr_cache)
            if lines:
                ocr_pages.append(i + 1)
        raw.append((i + 1, lines, page.rect.width, page.rect.height))

    if not any(lines for _, lines, *_ in raw):
        if ocr:
            raise NoTextLayer(
                f"{path.name}：{doc.page_count} 页既没有文字层，OCR 也没认出内容。"
                "确认文件不是空的或加密的；扫描件可以试着提高 dpi 再跑。"
            )
        raise NoTextLayer(
            f"{path.name}：{doc.page_count} 页里没有任何文字层（扫描件或图片型课件）。"
            "传 ocr=True 让它走 OCR：extract_ir(path, ocr=True)"
        )
    base = _mode_size([l[1] for _, lines, *_ in raw for l in lines])

    toc = [[lvl, title, pno] for lvl, title, pno in (doc.get_toc() or [])]
    toc_map = {_toc_key(t): (lvl, t) for lvl, t, _ in toc}

    ocr_set = set(ocr_pages)
    pages = [(pno, _order(lines, page_w), page_w, page_h)
             for pno, lines, page_w, page_h in raw]
    first_ocr = min(ocr_set) if ocr_set else None
    # 整本课件连一个"第X章"都没有时，合成一个用文件名当章名，否则所有块
    # 都会落进「（正文前）」那节里。
    ocr_named = any(
        _CHAPTER_TITLE.match(t.strip())
        for pno, ordered, _, _ in pages if pno in ocr_set
        for t, *_ in ordered
    )
    blocks = []
    stack = []
    bid = 0
    for pno, ordered, page_w, page_h in pages:
        if pno in ocr_set and pno == first_ocr and not ocr_named:
            bid += 1
            label = path.stem
            stack.clear()
            stack.append((1, label))
            blocks.append(Block(id=f"b{bid}", kind="heading", text=label, page=pno,
                                level=1, section_path=[label]))

        for text, size, bold, bbox in ordered:
            bid += 1
            clean = text.strip()
            ar = _aspect(bbox)
            level = 0
            hit = toc_map.get(_toc_key(clean))
            if pno in ocr_set:
                # PPT 课件和教材书是两种输入：书一页几百字、一章几十页；PPT 一页
                # 五十个字，整本才够讲一节。上一版按"一页一章"切，于是模型要拿
                # 246 字讲一节课，只能拿自己脑子里的知识填 —— 内容是对的，但不
                # 是从这份材料读出来的，source_refs 也就名存实亡。
                # 所以 OCR 页只认"第X章"这类章级标题，其余全部并进当前章。
                level = 1 if _CHAPTER_TITLE.match(clean) else 0
            elif hit:
                level = hit[0]
            elif _SECTION_NO.match(clean) and _plausible_title(clean, ar):
                level = 2 if bold else 3
            elif size >= base * 1.15 and _plausible_title(clean, ar):
                level = 1
            elif size >= base * 1.05 and bold and _plausible_title(clean, ar):
                level = 2
            elif bold and _plausible_title(clean, ar) and HEAD_NUM.match(clean):
                level = 3

            if level:
                while stack and stack[-1][0] >= level:
                    stack.pop()
                stack.append((level, clean))
                blocks.append(Block(id=f"b{bid}", kind="heading", text=clean, page=pno,
                                    level=level, section_path=[t for _, t in stack],
                                    bbox=bbox))
            else:
                blocks.append(Block(id=f"b{bid}", kind="para", text=clean, page=pno,
                                    level=0, section_path=[t for _, t in stack],
                                    bbox=bbox))

    return DocIR(
        doc_id=doc_id,
        filename=path.name,
        page_count=doc.page_count,
        blocks=blocks,
        toc=toc,
        ocr_pages=ocr_pages,
    )


def page_text_map(ir: DocIR) -> dict[int, str]:
    m = {}
    for b in ir.blocks:
        m[b.page] = m.get(b.page, "") + b.text + "\n"
    return m


def page_block_map(ir: DocIR) -> dict[int, list[Block]]:
    m: dict[int, list[Block]] = {}
    for b in ir.blocks:
        m.setdefault(b.page, []).append(b)
    return m