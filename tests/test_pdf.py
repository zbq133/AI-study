import fitz
import pytest

from app.pdf import (NoTextLayer, _mode_size, _order, _paragraphs, _plausible_title,
                     _toc_key, extract_ir, page_text_map)
from app.schema import build_sections


@pytest.fixture()
def sample_pdf(tmp_path):
    path = tmp_path / "sample.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 90), "Chapter 1 Introduction", fontsize=20)
    for i in range(8):
        p1.insert_text((72, 130 + i * 18), "this is body text line number %d" % i, fontsize=10)
    p2 = doc.new_page()
    p2.insert_text((72, 90), "1.1 Basic Concepts", fontsize=11, fontname="hebo")
    for i in range(8):
        p2.insert_text((72, 130 + i * 18), "more body text here padding %d" % i, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_extract_ir_finds_headings(sample_pdf):
    ir = extract_ir(sample_pdf)
    assert ir.page_count == 2
    heads = [b for b in ir.blocks if b.kind == "heading"]
    assert heads[0].text == "Chapter 1 Introduction"
    assert heads[0].level >= 1
    assert heads[0].page == 1
    assert ir.blocks[0].section_path == ["Chapter 1 Introduction"]


def test_extract_ir_nests_subsections(sample_pdf):
    ir = extract_ir(sample_pdf)
    sub = [b for b in ir.blocks if len(b.section_path) == 2]
    assert sub
    assert sub[0].section_path == ["Chapter 1 Introduction", "1.1 Basic Concepts"]


def test_page_text_map_separates_pages(sample_pdf):
    ir = extract_ir(sample_pdf)
    m = page_text_map(ir)
    assert set(m) == {1, 2}
    assert "Chapter 1 Introduction" in m[1]
    assert "Basic Concepts" in m[2]


def test_missing_file_raises():
    with pytest.raises(Exception):
        extract_ir("no-such-file.pdf")


def test_order_keeps_columns_apart_when_page_has_full_width_captions():
    """全宽图注横跨两栏是正常的，不能因此把整页判成单栏。

    那份综述的 p3 就是这样：14 行左栏、15 行右栏被按 y 混排成 L1 R1 L2 R2…，
    后半页每一句都接错了上下文。
    """
    W = 600.0

    def line(text, x0, y0, x1):
        return (text, 8.0, False, (x0, y0, x1, y0 + 10))

    lines = [
        line("L1", 40, 100, 280), line("L2", 40, 112, 280), line("L3", 40, 124, 280),
        line("R1", 320, 100, 560), line("R2", 320, 112, 560), line("R3", 320, 124, 560),
        line("CAP1", 40, 140, 560), line("CAP2", 40, 155, 560), line("CAP3", 40, 170, 560),
    ]
    texts = [l[0] for l in _order(lines, W)]
    assert texts.index("L3") < texts.index("R1")


def test_order_puts_a_page_header_above_both_columns():
    """页眉在全宽块里位置最高，必须排在左栏之前。

    那份综述每页的页眉 `C. Wang et al.` 都是全宽的，以前和右栏一起按 y 排，
    于是被塞到左栏末尾之后，14 页全中，14 条页眉混进了 10 个章节的正文。
    """
    W = 600.0

    def line(text, x0, y0, x1):
        return (text, 8.0, False, (x0, y0, x1, y0 + 10))

    lines = [
        line("HEADER", 40, 20, 560),
        line("L1", 40, 60, 280), line("L2", 40, 72, 280), line("L3", 40, 84, 280),
        line("R1", 320, 60, 560), line("R2", 320, 72, 560), line("R3", 320, 84, 560),
    ]
    texts = [l[0] for l in _order(lines, W)]
    assert texts[0] == "HEADER"
    assert texts.index("HEADER") < texts.index("L1")


def test_order_keeps_a_bottom_caption_after_the_columns():
    """页底的全宽图注不能跟着页眉一起被提到最前面 —— 它本来就该在正文之后。"""
    W = 600.0

    def line(text, x0, y0, x1):
        return (text, 8.0, False, (x0, y0, x1, y0 + 10))

    lines = [
        line("L1", 40, 60, 280), line("L2", 40, 72, 280), line("L3", 40, 84, 280),
        line("R1", 320, 60, 560), line("R2", 320, 72, 560), line("R3", 320, 84, 560),
        line("CAP1", 40, 200, 560), line("CAP2", 40, 215, 560),
    ]
    texts = [l[0] for l in _order(lines, W)]
    assert texts.index("CAP1") > texts.index("R3")
    assert texts.index("CAP2") > texts.index("CAP1")


def test_mode_size_follows_characters_not_line_count():
    """小字行数更多、但每行都短的时候，字号基准仍要跟着正文走。

    那本综述的正文 8.0 有 713 行、参考文献 6.4 有 630 行，只差 83 行；行数不看
    每行多长，一页塞满短行就能把基准拽下去。这里 8.0 有 4 行、10.0 只有 2 行，
    但 10.0 承载的字数更多，基准必须是 10.0。
    """
    weighted = [(10.0, 60)] * 2 + [(8.0, 20)] * 4     # 行数 4 > 2，字数 80 < 120
    assert _mode_size(weighted) == 10.0


def test_mode_size_does_not_lift_to_a_heading():
    # 标题字号很大但字数极少，一旦让它当基准，所有正文行都不满足 size >= base*1.15
    weighted = [(10.0, 50)] * 8 + [(18.0, 5)]
    assert _mode_size(weighted) == 10.0
    assert _mode_size([]) == 10.0


def test_toc_key_ignores_numbering_punctuation():
    # 出版方书签写 "1 Introduction"，正文写 "1. Introduction"——必须算同一个标题
    assert _toc_key("1 Introduction") == _toc_key("1. Introduction")
    assert _toc_key("10.1 Heat treatment") == _toc_key("10.1. Heat treatment")
    assert _toc_key("1 Introduction") != _toc_key("2 Introduction")


def test_extract_ir_raises_on_pdf_without_text_layer(tmp_path):
    # 图片型课件（整页一张图、零文字层）不能被当成"解析成功但没内容"
    path = tmp_path / "scan.pdf"
    doc = fitz.open()
    page = doc.new_page()
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 32, 32))
    pix.set_rect(pix.irect, (200, 60, 60))
    page.insert_image(page.rect, pixmap=pix)
    doc.save(str(path))
    doc.close()

    with pytest.raises(NoTextLayer):
        extract_ir(path, ocr=False)


def test_ocr_fills_text_layer_of_image_only_pdf(tmp_path):
    # 真课件的样子：字看得见，但整页就是一张图，一个文字行都提不出来
    src = fitz.open()
    page = src.new_page(width=420, height=200)
    page.insert_text((30, 60), "第十章 波动", fontsize=26, fontname="china-s")
    page.insert_text((30, 110), "机械波在弹性介质中的传播", fontsize=15, fontname="china-s")
    pix = page.get_pixmap(dpi=150)

    path = tmp_path / "slides.pdf"
    scanned = fitz.open()
    target = scanned.new_page(width=420, height=200)
    target.insert_image(target.rect, pixmap=pix)
    scanned.save(str(path))
    scanned.close()
    src.close()

    # 原生提取一个字都拿不到
    with pytest.raises(NoTextLayer):
        extract_ir(path, ocr=False, ocr_cache=None)

    # 走 OCR 才拿得到内容
    ir = extract_ir(path, ocr=True, ocr_cache=tmp_path / "ocr")
    assert ir.ocr_pages == [1]
    text = "".join(b.text for b in ir.blocks)
    assert "波动" in text or "机械波" in text
    # OCR 页自己成章：页首"第十章 波动"当标题，不再按字号切出一堆伪标题
    heads = [b for b in ir.blocks if b.kind == "heading"]
    assert len(heads) == 1 and heads[0].level == 1


def test_ocr_pages_join_into_one_chapter(tmp_path, monkeypatch):
    """教材书按章切，PPT 课件要整本并成一章。

    一页 PPT 只有几十上百字。按页切会让模型拿两百字讲一节课，它只能拿自己
    的知识补 —— 讲义看着漂亮，却跟这份材料无关，source_refs 也就名存实亡。
    """
    import app.ocr

    path = tmp_path / "slides.pdf"
    doc = fitz.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    doc.save(str(path))
    doc.close()

    monkeypatch.setattr("app.pdf._page_lines", lambda page: [])

    def fake_ocr(page, doc_id, pno, cache_dir=None, dpi=200):
        if pno == 1:
            return [("横波与纵波", 20.0, False, (10.0, 10.0, 200.0, 40.0))]
        return [("机械波在弹性介质中的传播.", 10.0, False, (10.0, 10.0, 380.0, 30.0))]

    monkeypatch.setattr(app.ocr, "ocr_page_cached", fake_ocr)

    ir = extract_ir(path, ocr_cache=None)
    assert ir.ocr_pages == [1, 2]
    heads = [b.text for b in ir.blocks if b.kind == "heading"]
    assert heads == ["slides"]              # 整本没有章标题时用文件名兜底
    secs = build_sections(ir)
    assert len(secs) == 1
    assert (secs[0].page_start, secs[0].page_end) == (1, 2)


def test_ocr_chapter_heading_splits_chapters(tmp_path, monkeypatch):
    """真课件第一页写着"第十章 波动"，那就是章界。"""
    import app.ocr

    path = tmp_path / "slides.pdf"
    doc = fitz.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    doc.save(str(path))
    doc.close()

    monkeypatch.setattr("app.pdf._page_lines", lambda page: [])

    def fake_ocr(page, doc_id, pno, cache_dir=None, dpi=200):
        if pno == 1:
            return [("第十章 波动", 26.0, False, (10.0, 10.0, 300.0, 50.0))]
        return [("教学基本要求", 18.0, False, (10.0, 10.0, 200.0, 40.0))]

    monkeypatch.setattr(app.ocr, "ocr_page_cached", fake_ocr)

    ir = extract_ir(path, ocr_cache=None)
    heads = [b.text for b in ir.blocks if b.kind == "heading"]
    # p1 的"第十章 波动"开章；p2 的"教学基本要求"只是正文，不再切一章
    assert heads == ["第十章 波动"]
    assert len(build_sections(ir)) == 1


def test_plausible_title_filters_ocr_noise():
    # 真课件上踩到的三类误判
    assert not _plausible_title("十", 8.0)                      # 单字序号
    assert not _plausible_title("两类波的不同之处", 0.1)          # 竖排侧栏标签
    assert not _plausible_title("电磁波交变电磁场在空间的传播.", 16.0)  # 整句正文
    assert _plausible_title("教学基本要求", 6.1)


def test_long_toc_title_matches_body_heading(tmp_path):
    # 真 PDF 里踩到的坑：标题 53 个字符，超过"短标题"阈值 42，
    # 且正文带点号而书签不带，结果整节被上一节吞掉。TOC 命中必须优先于长度启发式。
    bookmark = "11 Development of novel and easily degradable binder"
    body = "11. Development of novel and easily degradable binder"
    path = tmp_path / "toc.pdf"
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 90), body, fontsize=10, fontname="hebo")
    for i in range(6):
        page.insert_text((72, 120 + i * 16), "body text %d" % i, fontsize=10)
    doc.set_toc([[1, "Review", 1], [2, bookmark, 1]])
    doc.save(str(path))
    doc.close()

    ir = extract_ir(path)
    heads = [b for b in ir.blocks if b.kind == "heading"]
    assert heads and heads[0].text == body
    assert heads[0].level == 2

_ROW = (37.6, 100.0, 291.0, 113.0)


def _row(text, y, x0=37.6, x1=291.0, size=8.0):
    return (text, size, False, (x0, y, x1, y + 13.0))


def test_paragraphs_join_lines_of_one_paragraph():
    lines = [_row("layered structure of the cathode", 100.0),
             _row("material is prone to deformation", 109.8)]
    out = _paragraphs(lines, 595.276)
    assert len(out) == 1
    assert out[0][0] == "layered structure of the cathode material is prone to deformation"


def test_paragraphs_stop_at_a_short_line():
    lines = [_row("short ending line", 100.0, x1=143.9),
             _row("next paragraph text", 109.8)]
    out = _paragraphs(lines, 595.276)
    assert [t for t, *_ in out] == ["short ending line", "next paragraph text"]


def test_paragraphs_break_on_first_line_indent():
    lines = [_row("end of the first paragraph.", 100.0),
             _row("Second paragraph starts here", 109.8, x0=49.5)]
    out = _paragraphs(lines, 595.276)
    assert len(out) == 2


def test_paragraphs_glue_hyphenated_word():
    lines = [_row("the powder is heated to a high-tempe-", 100.0),
             _row("rature inside a tube furnace", 109.8)]
    out = _paragraphs(lines, 595.276)
    assert len(out) == 1
    assert "high-temperature inside" in out[0][0]


def test_paragraphs_keep_full_width_caption_separate():
    caption = ("Fig. 1. Separation process of cathode materials from spent LIBs, "
               "which is a rather long caption spanning both columns of the page.")
    lines = [_row("body line of the left column", 100.0),
             _row(caption, 120.0, x1=559.7)]
    out = _paragraphs(lines, 595.276)
    assert [t for t, *_ in out] == ["body line of the left column", caption]

def _repeat_pdf(tmp_path, pages=4, header="Journal of Testing 2025", mid=None):
    """造一份每页页眉相同、正文各不相同的小 PDF。"""
    path = tmp_path / "repeat.pdf"
    doc = fitz.open()
    for i in range(pages):
        p = doc.new_page()
        p.insert_text((72, 40), header, fontsize=9)
        if mid:
            p.insert_text((72, 250), mid, fontsize=10)
        p.insert_text((72, 400), "unique body line on page %d" % (i + 1), fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def test_running_header_is_dropped_from_the_body_text(tmp_path):
    """每页重复的页眉会被摘掉，正文一行不少。

    位置修对之后（置顶）页眉仍在章节正文里：那份综述 14 页的 `C. Wang et al.`
    混进了 9 个章节，会随 _render 一起喂给模型。
    """
    path = _repeat_pdf(tmp_path)
    ir = extract_ir(path, ocr=False)
    text = "\n".join(b.text for b in ir.blocks)
    assert "Journal of Testing 2025" not in text
    assert "unique body line on page 3" in text


def test_phrase_repeated_mid_page_is_kept(tmp_path):
    """正文中部反复出现的短语不能当页眉删掉 —— 它只是高频词，不是噪声。"""
    path = _repeat_pdf(tmp_path, mid="lithium-ion battery")
    ir = extract_ir(path, ocr=False)
    text = "\n".join(b.text for b in ir.blocks)
    assert "lithium-ion battery" in text
    assert "Journal of Testing 2025" not in text


def test_two_pages_are_not_enough_to_call_a_line_a_header(tmp_path):
    """两页还不算"跨页重复"，门槛是 3 页 —— 短文档宁可留着。"""
    path = _repeat_pdf(tmp_path, pages=2)
    ir = extract_ir(path, ocr=False)
    assert "Journal of Testing 2025" in "\n".join(b.text for b in ir.blocks)

def test_a_line_that_also_appears_mid_page_is_not_treated_as_a_header(tmp_path):
    """页眉那种短句如果在正文里也出现过，说明它是内容，不能删。

    只按"每页最上一行 + 跨页重复"删，会把正文里引用的标题一起带走；所以还要
    求这个文本从没在页面中部出现过。
    """
    path = tmp_path / "mixed.pdf"
    doc = fitz.open()
    for i in range(4):
        p = doc.new_page()
        p.insert_text((72, 40), "Key Findings", fontsize=9)
        if i == 1:
            p.insert_text((72, 250), "Key Findings", fontsize=10)
        p.insert_text((72, 400), "unique body line on page %d" % (i + 1), fontsize=10)
    doc.save(str(path))
    doc.close()
    ir = extract_ir(path, ocr=False)
    assert "Key Findings" in "\n".join(b.text for b in ir.blocks)
