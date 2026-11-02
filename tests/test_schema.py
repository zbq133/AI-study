from app.schema import (Block, DocIR, SourceRef, build_sections, extract_answer,
                        normalize, select_chapter, verify_refs)


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


def test_blocks_before_first_heading_get_readable_label():
    # 首页页眉/作者信息出现在第一个标题之前：章节名不能再是 "__root__"
    ir = make_ir()
    ir.blocks.insert(0, Block(id="b0", kind="para", text="页眉信息", page=1,
                              section_path=[]))
    secs = build_sections(ir)
    assert secs[0].path == ["（正文前）"]
    assert secs[0].title == "（正文前）"
    assert secs[0].block_ids == ["b0"]
    assert "__root__" not in [s.title for s in secs]


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


def _line(bid, text, y):
    return Block(id=bid, kind="para", text=text, page=1,
                 section_path=["第1章 绪论"], bbox=(60.0, y, 300.0, y + 14.0))


def test_verify_refs_rejects_cross_column_stitch():
    """左栏末尾 + 右栏开头拼起来是两处文字，y 会回跳，必须丢。"""
    blocks = [_line("b1", "左栏最后一句在这里结束", 700.0),
              _line("b2", "右栏开头是另一段内容", 60.0)]
    page_text = {1: "左栏最后一句在这里结束\n右栏开头是另一段内容\n"}
    bad = SourceRef(page=1, quote="左栏最后一句在这里结束右栏开头是另一段内容")
    assert verify_refs([bad], page_text, {1: blocks}) == []


def test_verify_refs_allows_normally_wrapped_lines():
    """同一段被排版拆成多行、y 一路往下走，是正常引用。"""
    blocks = [_line("b1", "这是一句正常换行的话", 100.0),
              _line("b2", "从上一行接着写下来", 114.0)]
    page_text = {1: "这是一句正常换行的话\n从上一行接着写下来\n"}
    good = SourceRef(page=1, quote="正常换行的话从上一行接着写")
    assert [r.quote for r in verify_refs([good], page_text, {1: blocks})] == [good.quote]


def test_verify_refs_without_geometry_still_works():
    """没给 bbox 时不做行序检查（纯文本调用不误杀）。"""
    page_text = {1: "这是一句可以在原文里找到的话"}
    ref = SourceRef(page=1, quote="可以在原文里找到的话")
    assert [r.quote for r in verify_refs([ref], page_text)] == [ref.quote]


def test_extract_answer_handles_free_text():
    assert extract_answer("B") == "B"
    assert extract_answer("b.") == "B"
    assert extract_answer("答案是 B") == "B"
    assert extract_answer("B. 乙") == "B"
    assert extract_answer("AC") == "AC"
    assert extract_answer("因为 C 项不对，所以选 B") == "B"
    assert extract_answer("") == ""
    assert extract_answer("不知道") == ""


def test_extract_answer_rejects_out_of_range_letter():
    assert extract_answer("E", 4) == ""
    assert extract_answer("", 4) == ""
    assert extract_answer("C", 4) == "C"