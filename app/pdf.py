from __future__ import annotations

import hashlib
import re
from collections import Counter
from pathlib import Path

import fitz

from .schema import Block, DocIR, normalize

HEAD_NUM = re.compile(
    r"^(第[一二三四五六七八九十百0-9]+[章节讲篇部]|\d+(\.\d+){0,3}[\s、.]|[一二三四五六七八九十]+、)"
)


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


def extract_ir(path) -> DocIR:
    path = Path(path)
    doc = fitz.open(str(path))
    raw = [(i + 1, _page_lines(p), p.rect.width) for i, p in enumerate(doc)]
    base = _mode_size([l[1] for _, lines, _ in raw for l in lines])

    toc = [[lvl, title, pno] for lvl, title, pno in (doc.get_toc() or [])]
    toc_map = {normalize(t): (lvl, t) for lvl, t, _ in toc}

    blocks = []
    stack = []
    bid = 0
    for pno, lines, page_w in raw:
        for text, size, bold, bbox in _order(lines, page_w):
            bid += 1
            clean = text.strip()
            short = len(clean) <= 42
            level = 0
            hit = toc_map.get(normalize(clean))
            if hit:
                level = hit[0]
            elif size >= base * 1.15 and short:
                level = 1
            elif size >= base * 1.05 and bold and short and not clean.endswith(("。", ".", "，", ",")):
                level = 2
            elif bold and short and HEAD_NUM.match(clean):
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
        doc_id=hashlib.sha256(path.read_bytes()).hexdigest()[:16],
        filename=path.name,
        page_count=doc.page_count,
        blocks=blocks,
        toc=toc,
    )


def page_text_map(ir: DocIR) -> dict[int, str]:
    m = {}
    for b in ir.blocks:
        m[b.page] = m.get(b.page, "") + b.text + "\n"
    return m