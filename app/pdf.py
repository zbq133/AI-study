from __future__ import annotations

import hashlib
import re
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


def _mode_size(weighted):
    """正文基准字号：取**字数最多**的那一档。

    以前取"出现次数最多的字号"，在这本综述上只差 83 行就翻车：正文 8.0 有 713
    行、参考文献小字 6.4 有 630 行。行数不看每行多长，一页塞满短行的参考文献
    就能把基准拽下去；同一份文件按字数算是 43487 : 36109，余量宽松得多。

    取"字数最多"而不是"字号最大的常见档"：标题那一档字数天然少（20pt 的章名
    也就二十来个字），一旦让它进候选，基准会抬到标题字号，于是所有正文行都不
    再满足 `size >= base * 1.15`，整本一个标题都认不出来。

    仍然存在残余风险：参考文献真按字数盖过正文时基准还是会翻。彻底解法是按
    参考文献段把那些页从直方图里剔掉（需要先切章节，顺序上放不到这里）。

    weighted 是 (字号, 字数) 的可迭代对象。
    """
    total = {}
    for size, n in weighted:
        size = round(size, 1)
        total[size] = total.get(size, 0) + n
    if not total:
        return 10.0
    return max(total.items(), key=lambda kv: (kv[1], kv[0]))[0]


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


_WIDE_RATIO = 0.75
_ORDER_TOL = 2.0


def _order(lines, page_w):
    """按阅读顺序排一页的行：双栏就先左后右，单栏就自上而下。

    span 只数"跨过中线、又不占满整行"的窄元素（居中标题、跨栏短句），它们说明
    这不是干净的左右分栏。图注、表题这类**全宽元素**横跨两栏是正常的 —— 以前把
    它们也算进 span，一页有两条图注就把整页判成单栏，于是左右栏交错，后半页每
    一句都接错了上下文（p3 就是这样：14 行左栏和 15 行右栏被按 y 混排）。

    全宽块还要分"页眉带"和"正文里"两种：页眉（`C. Wang et al.` 那种）在正文
    上方，必须排在左栏**之前**。以前一视同仁地和右栏一起按 y 排，于是每页的
    页眉都被塞到左栏末尾之后 —— 14 页全中，14 条页眉最后混进了 10 个章节的正文。
    页底的全宽图注不用特殊处理：它和右栏一起按 y 排，位置本来就是对的。
    """
    if not lines:
        return []
    mid = page_w / 2.0
    wide = [l for l in lines if l[3][2] - l[3][0] >= page_w * _WIDE_RATIO]
    rest = [l for l in lines if l[3][2] - l[3][0] < page_w * _WIDE_RATIO]
    left = [l for l in rest if l[3][0] < mid - 20]
    right = [l for l in rest if l[3][0] >= mid - 20]
    span = [l for l in rest if l[3][0] < mid and l[3][2] > mid + 20]
    key = lambda l: (round(l[3][1], 1), l[3][0])
    if len(left) > 0.3 * len(lines) and len(right) > 0.3 * len(lines) and len(span) <= 2:
        top = min((l[3][1] for l in rest), default=0.0)
        head = [l for l in wide if l[3][1] <= top + _ORDER_TOL]
        body = [l for l in wide if l[3][1] > top + _ORDER_TOL]
        return sorted(head, key=key) + sorted(left, key=key) + sorted(body + right, key=key)
    return sorted(lines, key=key)


def _glue(head: str, tail: str) -> str:
    """两行连成一句：中文直接接，英文补空格，行末连字符断词要还原。"""
    a, b = head.rstrip(), tail.lstrip()
    if not a:
        return b
    if len(a) > 1 and a.endswith("-") and a[-2].isalpha() and b[:1].islower():
        return a[:-1] + b                       # high-tempe / rature
    if b[:1].isascii() and b[:1].isalnum():
        return a + " " + b
    return a + b


def _paragraphs(lines, page_w):
    """把同一段落的连续行并成一个块。

    每行一个 Block 的话，下游拿到的是一串断在句中的碎片：讲义只能照着碎片写，
    出题的 quote 也只能引用碎片。判据就两条 —— 上一行写满了它所在栏的右边界，
    才可能还有下文；下一行没有重新缩进、字号没变、行距正常，才算同一段。
    """
    if not lines:
        return []
    mid = page_w / 2.0

    def col(x0):
        return 0 if x0 < mid - 20 else 1

    right_of = {}
    for _t, _s, _b, bbox in lines:
        if bbox[2] - bbox[0] >= page_w * _WIDE_RATIO:
            continue                    # 全宽图注/页眉不代表栏的右边界
        c = col(bbox[0])
        right_of[c] = max(right_of.get(c, 0.0), bbox[2])

    out = []
    cur = None
    for text, size, bold, bbox in lines:
        clean = text.strip()
        if not clean:
            continue
        wide = bbox[2] - bbox[0] >= page_w * _WIDE_RATIO
        if cur is not None:
            ctext, csize, cbold, cbbox, ctail = cur
            cur_wide = cbbox[2] - cbbox[0] >= page_w * _WIDE_RATIO
            same_col = col(bbox[0]) == col(cbbox[0])
            same_size = abs(size - csize) < 0.5
            # 看的是"上一行的行末"有没有写到栏右边界，不是整段的最大值 ——
            # 段末那行短一截，就说明段落到此为止。
            filled = (right_of[col(cbbox[0])] - ctail) <= 2.0 * csize
            # 段落首行是缩进的（x0 更大），正文行回到栏的左边界；所以只允许
            # "往左回到原处"，往右跳一下就是新段落开了头。
            no_reindent = bbox[0] <= cbbox[0] + 2.0
            tight = -4.0 <= bbox[1] - cbbox[3] <= 1.5 * csize
            if (not wide and not cur_wide) and same_col and same_size \
                    and filled and no_reindent and tight:
                cur = (_glue(ctext, clean), csize, cbold,
                       (cbbox[0], cbbox[1],
                        max(cbbox[2], bbox[2]), max(cbbox[3], bbox[3])),
                       bbox[2])
                continue
            out.append(cur[:4])
        cur = (clean, size, bold, bbox, bbox[2])
    if cur is not None:
        out.append(cur[:4])
    return out


_REPEAT_MIN_PAGES = 3
_REPEAT_MIN_SHARE = 0.2
_REPEAT_MAX_CHARS = 80
_REPEAT_EDGE_TOL = 1.0


def _running_lines(pages):
    """找出跨页重复、又只在页面最上/最下一行出现的短文本 —— 页眉、页脚、期刊名。

    这些行每页都来一遍，留在正文里就是白喂给模型的噪声：那份综述 14 页的
    `C. Wang et al.` 位置修对之后，文本仍然混进了 9 个章节的正文。

    四条门槛一起上，宁可漏掉也不误删正文：
    1. 归一化（压空白、转小写）后逐字相同；
    2. 至少在 3 页、且至少占全文 20% 的页数上出现 —— 同一页重复几次不算；
    3. 归一化后不超过 80 字 —— 再长更可能是正文小标题；
    4. 每次出现都在该页的最上一行或最下一行（容差 1pt），而且这个文本**从没**
       在页面中部出现过。第 4 条是关键：正文里的高频短语（`lithium-ion battery`）
       会落在栏中间，只要出现过一次中部就赦免。
    """
    seen = {}
    mid = set()
    for _pno, lines, _w, _h in pages:
        if not lines:
            continue
        top = min(l[3][1] for l in lines)
        bottom = max(l[3][3] for l in lines)
        for text, _size, _bold, bbox in lines:
            key = " ".join(text.split()).lower()
            if not key or len(key) > _REPEAT_MAX_CHARS:
                continue
            if bbox[1] <= top + _REPEAT_EDGE_TOL or bbox[3] >= bottom - _REPEAT_EDGE_TOL:
                seen.setdefault(key, set()).add(_pno)
            else:
                mid.add(key)
    need = max(_REPEAT_MIN_PAGES, int(len(pages) * _REPEAT_MIN_SHARE))
    return {k for k, ps in seen.items() if k not in mid and len(ps) >= need}


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
    # 页眉页脚先摘掉，再算字号基准、再排阅读顺序 —— 留着它们，基准和段落都会
    # 被这些每页重复的短行带偏。
    drop = _running_lines(raw)
    if drop:
        cleaned = [(pno, [l for l in lines
                          if " ".join(l[0].split()).lower() not in drop], w, h)
                   for pno, lines, w, h in raw]
        if any(lines for _, lines, *_ in cleaned):
            raw = cleaned

    base = _mode_size([(l[1], len(l[0])) for _, lines, *_ in raw for l in lines])

    toc = [[lvl, title, pno] for lvl, title, pno in (doc.get_toc() or [])]
    toc_map = {_toc_key(t): (lvl, t) for lvl, t, _ in toc}

    ocr_set = set(ocr_pages)
    # 原生文字层按段落合并；OCR 出来的 PPT 页不合并 —— 那一行就是一个要点，
    # 拼起来反而把几个要点揉成一句。
    pages = []
    for pno, lines, page_w, page_h in raw:
        ordered = _order(lines, page_w)
        if pno not in ocr_set:
            ordered = _paragraphs(ordered, page_w)
        pages.append((pno, ordered, page_w, page_h))
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