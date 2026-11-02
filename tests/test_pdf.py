import fitz
import pytest

from app.pdf import NoTextLayer, _plausible_title, _toc_key, extract_ir, page_text_map


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


def test_ocr_page_gets_its_own_section(tmp_path, monkeypatch):
    # OCR 页的几何信息不可靠（框高由文字高度推出来，字号全是估的），
    # 套教材章节那套启发式会把正文行也判成标题。规则改成：每页自己成章，
    # 页首确实像标题就用它，否则用"第 N 页"兜底。
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
    assert heads == ["横波与纵波", "第 2 页"]
    # 第二页的正文要挂在它自己的"第 2 页"下面，不能串到第一页标题下
    last = [b for b in ir.blocks if b.page == 2 and b.kind == "para"]
    assert last and last[0].section_path == ["第 2 页"]


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