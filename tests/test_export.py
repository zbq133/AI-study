"""导出 PDF：LaTeX 兜底、Markdown 转换、Story 排版。"""
from pathlib import Path

import pymupdf

from app.export import (build_html, find_font, latex_to_text, lesson_pdf,
                        md_to_html)
from app.schema import Lesson

LESSON = {
    "chapter": "第十章 波动",
    "learning_objectives": ["能说出机械波与电磁波的两类划分"],
    "sections": [{
        "title": "波速与介质",
        "explanation_md": "## 三、横波与纵波\n\n横波的波速是 $u=\\sqrt{G/\\rho}$。\n\n"
                          "- 声波在空气中约 $343\\ \\text{m/s}$\n\n**关键**：选错模量就失去意义。",
        "key_points": ["横波只能在固体中传播"],
        "pitfalls": ["误以为波速由波源频率决定"],
        "source_refs": [{"page": 10, "quote": "周期或频率只决定于波源的振动",
                         "blocks": ["b77"], "bbox": None}],
    }],
    "summary_one_liner": "本章分 1 块讲：波速与介质。",
    "summary_bullets": ["波速由介质的弹性和惯性决定"],
    "quiz": [{
        "type": "single", "stem": "关于波速，下列说法正确的是：",
        "options": ["由波源决定", "由介质决定", "与两者都无关", "由振幅决定"],
        "answer": "B", "explanation": "波速由介质性质决定。", "difficulty": 2,
        "knowledge_point": "波速", "verified": True,
        "source_ref": {"page": 10, "quote": "周期或频率只决定于波源的振动",
                       "blocks": ["b77"], "bbox": None},
    }],
}


def test_latex_to_text_covers_what_the_prompts_actually_emit():
    assert latex_to_text(r"\lambda = u T = \frac{u}{\nu}") == "λ = u T = u/ν"
    assert latex_to_text(r"u = \sqrt{E/\rho}") == "u = √(E/ρ)"
    assert latex_to_text(r"6CO_2 + 6H_2O \to C_6H_{12}O_6") == "6CO₂ + 6H₂O → C₆H₁₂O₆"
    assert latex_to_text(r"\alpha^2 \leq \beta") == "α² ≤ β"


def test_latex_to_text_keeps_what_it_cannot_translate():
    """翻不动就留名字 —— 宁可露出 \\foo，也不能把公式悄悄变成空字符串。"""
    assert "frac" not in latex_to_text(r"\frac{a+b}{c}").replace("(a+b)/(c)", "")
    assert latex_to_text(r"\frac{a+b}{c}") == "(a+b)/(c)"
    assert "nosuchcmd" in latex_to_text(r"\nosuchcmd{x}")


def test_md_to_html_matches_the_frontend_conventions():
    html = md_to_html("普通一段\n\n## 小标题\n\n- 第一条\n- 第二条\n\n**粗体**")
    assert "<p>普通一段</p>" in html
    assert "<h3>小标题</h3>" in html          # 讲义里的 ## 是节内小标题，不许抢 h1
    assert html.count("<li>") == 2
    assert "<b>粗体</b>" in html


def test_md_to_html_escapes_html():
    """讲义正文来自模型，里面真出现 <script> 也不能当成标签排进 PDF。"""
    html = md_to_html("看这段 <script>alert(1)</script> 代码")
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_build_html_carries_the_parts_a_student_needs():
    html = build_html(Lesson(**LESSON), source_name="第十章 波动.pdf",
                      style_label="严谨认真", when="2026-11-04 10:20")
    for probe in ("学完能做到", "波速与介质", "易错：", "原文 p10", "答案 B", "解析：",
                  "这一章要记住的", "严谨认真", "第十章 波动.pdf"):
        assert probe in html, probe


def test_lesson_pdf_is_a_real_pdf_with_chinese_in_it(tmp_path):
    out = lesson_pdf(Lesson(**LESSON), tmp_path / "a.pdf", source_name="课件.pdf",
                     style_label="严谨认真", when="2026-11-04 10:20")
    raw = out.read_bytes()
    assert raw.startswith(b"%PDF-") and len(raw) > 20_000
    doc = pymupdf.open(str(out))
    try:
        # Story 排版时空格会变成不换行空格（\xa0），取文本时要归一化再断言
        text = "".join(p.get_text() for p in doc).replace("\xa0", " ")
    finally:
        doc.close()
    assert "波速与介质" in text and "答案 B" in text
    # 有中文字体时别把 19 MB 的雅黑整份塞进去：子集化之后应该是几百 KB 级
    assert len(raw) < 2_000_000, f"字体没子集化？{len(raw)} 字节"


def test_lesson_pdf_survives_an_empty_lesson(tmp_path):
    """没有正文/没有题也要能导出（刚建的空讲义、或某一章一题都没过校验）。"""
    out = lesson_pdf(Lesson(chapter="空章", sections=[], quiz=[]), tmp_path / "e.pdf")
    assert out.read_bytes().startswith(b"%PDF-")


def test_font_lookup_returns_a_file_when_it_finds_one():
    font = find_font()
    if font is None:
        return                    # 没装中文字体的机器上，导出会退回 sans-serif，不是失败
    assert Path(font).is_file()
