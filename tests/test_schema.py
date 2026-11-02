from app.schema import (Block, DocIR, SourceRef, build_sections, normalize,
                        select_chapter, verify_refs)


def make_ir():
    blocks = [
        Block(id="b1", kind="heading", text="第1章 绪论", page=1, level=1,
              section_path=["第1章 绪论"]),
        Block(id="b2", kind="para", text="绪论的正文内容在这里。", page=1,
              section_path=["第1章 绪论"]),
        Block(id="b3", kind="heading", text="1.1 基本概念", page=2, level=2,
              section_path=["第1章 绪论", "1.1 基本概念"]),
        Block(id="b4", kind="para", text="基本概念的正文。", page=2,
              section_path=["第1章 绪论", "1.1 基本概念"]),
        Block(id="b5", kind="heading", text="第2章 方法", page=3, level=1,
              section_path=["第2章 方法"]),
        Block(id="b6", kind="para", text="方法的正文。", page=3,
              section_path=["第2章 方法"]),
    ]
    return DocIR(doc_id="x", filename="t.pdf", page_count=3, blocks=blocks)


def test_normalize_strips_all_whitespace():
    assert normalize("  松 弛\t算\n子 ") == "松弛算子"


def test_build_sections_groups_and_marks_pages():
    secs = build_sections(make_ir())
    assert [s.title for s in secs] == ["第1章 绪论", "1.1 基本概念", "第2章 方法"]
    assert secs[0].page_start == 1 and secs[0].page_end == 1
    assert "[p1]" in secs[0].text
    assert secs[1].block_ids == ["b3", "b4"]


def test_select_chapter_returns_subtree_only():
    secs = build_sections(make_ir())
    assert [s.title for s in select_chapter(secs, "第1章")] == ["第1章 绪论", "1.1 基本概念"]
    assert [s.title for s in select_chapter(secs, "1.1 基本概念")] == ["1.1 基本概念"]
    assert select_chapter(secs, "") == []
    assert select_chapter(secs, "不存在的章节") == []


def test_verify_refs_drops_hallucination():
    page_text = {2: "基本概念的正文。这里还有一句可以逐字引用的长句子。"}
    good = SourceRef(page=2, quote="可以逐字引用的长句子")
    kept = verify_refs([good,
                        SourceRef(page=2, quote="原文里根本没有这句话"),
                        SourceRef(page=9, quote="可以逐字引用的长句子"),
                        SourceRef(page=2, quote="基本")], page_text)
    assert [r.quote for r in kept] == [good.quote]


def test_verify_refs_tolerates_line_breaks():
    page_text = {1: "松 弛\n算 子 的 定 义"}
    assert verify_refs([SourceRef(page=1, quote="松弛算子的定义")], page_text)