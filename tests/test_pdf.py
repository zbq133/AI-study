import fitz
import pytest

from app.pdf import NoTextLayer, _toc_key, extract_ir, page_text_map


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
        extract_ir(path)


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