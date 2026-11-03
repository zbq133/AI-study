from app.schema import (Block, DocIR, SourceRef, build_sections, extract_answer,
                        is_reference_title, locate_quote, normalize, select_chapter,
                        verify_refs)


def test_is_reference_title_matches_common_spellings():
    """参考文献常吃掉全文四成篇幅，不该出现在"可用章节"里让人误选。"""
    assert is_reference_title("References")
    assert is_reference_title("REFERENCES")
    assert is_reference_title("参考文献")
    assert is_reference_title(" Bibliography ")
    assert not is_reference_title("12. Summary and recommendations")
    assert not is_reference_title("10. Heat treatment")


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

def test_verify_refs_records_which_blocks_the_quote_covers():
    """校验通过后要把"落在哪几块、占多大一块地方"写回 ref。

    前端点"出处"要高亮原文那一行，只有 page + quote 做不到：同一页几十行里
    光凭一串文字根本找不着位置。
    """
    blocks = [_line("b1", "这是一句正常换行的话", 100.0),
              _line("b2", "从上一行接着写下来", 114.0)]
    page_text = {1: "这是一句正常换行的话\n从上一行接着写下来\n"}
    kept = verify_refs([SourceRef(page=1, quote="正常换行的话从上一行接着写")],
                       page_text, {1: blocks})
    assert kept[0].blocks == ["b1", "b2"]
    assert kept[0].bbox == (60.0, 100.0, 300.0, 128.0)


def test_verify_refs_keeps_only_the_blocks_actually_hit():
    """只盖住一块的 quote，不能把整页都算进去。"""
    blocks = [_line("b1", "第一句完全无关的话", 100.0),
              _line("b2", "第二句才是要引用的内容", 114.0),
              _line("b3", "第三句又无关了", 128.0)]
    page_text = {1: "\n".join(b.text for b in blocks) + "\n"}
    kept = verify_refs([SourceRef(page=1, quote="第二句才是要引用的内容")],
                       page_text, {1: blocks})
    assert kept[0].blocks == ["b2"]
    assert kept[0].bbox == (60.0, 114.0, 300.0, 128.0)


def test_locate_quote_rejects_a_stitch_across_columns():
    """跨栏拼接定位不到，返回空列表 —— 调用方据此丢弃这条出处。"""
    blocks = [_line("b1", "左栏最后一句在这里结束", 700.0),
              _line("b2", "右栏开头是另一段内容", 60.0)]
    assert locate_quote("左栏最后一句在这里结束右栏开头是另一段内容", blocks) == []


def test_locate_quote_returns_empty_when_quote_is_absent():
    blocks = [_line("b1", "这里只有这一句", 100.0)]
    assert locate_quote("原文里根本没有的话", blocks) == []
