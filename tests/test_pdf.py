import fitz
import pytest

from app.pdf import extract_ir, page_text_map


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