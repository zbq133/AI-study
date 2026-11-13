"""把一份 Lesson 排成 PDF（A4）。

不引新依赖：PyMuPDF 自带 Story（HTML + 一小撮 CSS 的子集 → 排版），够排讲义这种
"标题 + 段落 + 列表 + 公式行"的东西。三件要额外处理的事：

1. **中文**：Story 默认字体没有汉字。把系统里的雅黑（msyh.ttc）塞进 Archive，
   用 `@font-face` 指过去。找不到字体时退回默认（英文能出，中文会是方块）——
   这是"少个装饰"而不是"导出失败"，不该把整个接口拖挂。
2. **公式**：讲义里是 `$\\lambda = u T$` 这种 LaTeX。Story 不认 KaTeX，
   所以这里做一遍**尽力而为的 LaTeX → Unicode**（λ / √ / 上下标），翻不动的原样留着。
   宁可露出 `\\frac{a}{b}`，也不要把公式悄悄吞掉。
3. **Markdown**：只认讲义实际会产出的四种语法（段落 / `#` 标题 / `-` 列表 / `**粗体**`），
   和前端 `md()` 保持同一套约定，别各写一份不一样的。
"""
from __future__ import annotations

import html as _html
import io
import re
from pathlib import Path
from typing import Optional

import pymupdf

from .schema import Lesson

PAGE_W, PAGE_H, MARGIN = 595, 842, 56        # A4（pt）
FONT_CANDIDATES = (
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\simhei.ttf",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
)

# ---------------------------------------------------------------- LaTeX → 文本

_GREEK = {
    "alpha": "α", "beta": "β", "gamma": "γ", "delta": "δ", "epsilon": "ε",
    "varepsilon": "ε", "zeta": "ζ", "eta": "η", "theta": "θ", "kappa": "κ",
    "lambda": "λ", "mu": "μ", "nu": "ν", "xi": "ξ", "pi": "π", "rho": "ρ",
    "sigma": "σ", "tau": "τ", "phi": "φ", "varphi": "φ", "chi": "χ",
    "psi": "ψ", "omega": "ω", "Gamma": "Γ", "Delta": "Δ", "Theta": "Θ",
    "Lambda": "Λ", "Xi": "Ξ", "Pi": "Π", "Sigma": "Σ", "Phi": "Φ",
    "Psi": "Ψ", "Omega": "Ω",
}
_OPS = {
    "times": "×", "cdot": "·", "div": "÷", "pm": "±", "mp": "∓",
    "leq": "≤", "le": "≤", "geq": "≥", "ge": "≥", "neq": "≠", "ne": "≠",
    "approx": "≈", "sim": "∼", "propto": "∝", "infty": "∞", "partial": "∂",
    "nabla": "∇", "sum": "∑", "prod": "∏", "int": "∫", "to": "→",
    "rightarrow": "→", "leftarrow": "←", "leftrightarrow": "↔",
    "Rightarrow": "⇒", "Leftrightarrow": "⇔", "degree": "°", "circ": "∘",
    "ldots": "…", "dots": "…", "angle": "∠", "perp": "⊥", "parallel": "∥",
    "in": "∈", "notin": "∉", "subset": "⊂", "cup": "∪", "cap": "∩",
}
_SUP = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUB = str.maketrans("0123456789+-=()aehijklmnoprstuvx",
                     "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₕᵢⱼₖₗₘₙₒₚᵣₛₜᵤᵥₓ")


def _group(tex: str, i: int) -> tuple[str, int]:
    """从 i 开始取一个参数：`{...}` 或单个字符。返回 (内容, 下一个位置)。"""
    while i < len(tex) and tex[i] == " ":
        i += 1
    if i >= len(tex):
        return "", i
    if tex[i] != "{":
        return tex[i], i + 1
    depth, j = 0, i
    while j < len(tex):
        if tex[j] == "{":
            depth += 1
        elif tex[j] == "}":
            depth -= 1
            if depth == 0:
                return tex[i + 1:j], j + 1
        j += 1
    return tex[i + 1:], len(tex)


def latex_to_text(tex: str) -> str:
    """尽力把一小撮 LaTeX 翻成人能读的 Unicode。翻不动的命令原样留下。"""
    out: list[str] = []
    i = 0
    while i < len(tex):
        c = tex[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        m = re.match(r"\\([A-Za-z]+)", tex[i:])
        if not m:
            ch = tex[i + 1] if i + 1 < len(tex) else ""
            if ch in (",", ";", " "):
                out.append(" ")
            elif ch in ("%", "&", "#"):
                out.append(ch)
            else:
                out.append(tex[i:(i + 2)])
            i += 2 if ch else 1
            continue
        name = m.group(1)
        i += len(m.group(0))
        if name in ("frac", "dfrac", "tfrac"):
            a, i = _group(tex, i)
            b, i = _group(tex, i)
            a, b = latex_to_text(a).strip(), latex_to_text(b).strip()
            out.append(f"({a})/({b})" if len(a) > 1 or len(b) > 1 else f"{a}/{b}")
        elif name == "sqrt":
            a, i = _group(tex, i)
            inner = latex_to_text(a).strip()
            out.append(f"√({inner})" if len(inner) > 1 else f"√{inner}")
        elif name in ("text", "mathrm", "mathit", "mathbf", "operatorname"):
            a, i = _group(tex, i)
            out.append(latex_to_text(a))
        elif name in ("left", "right", "big", "Big", "bigl", "bigr"):
            continue
        elif name in _GREEK:
            out.append(_GREEK[name])
        elif name in _OPS:
            out.append(_OPS[name])
        elif name in ("quad", "qquad"):
            out.append(" ")
        else:
            out.append(name)          # 认不出就留名字，别把内容吞掉
    s = "".join(out)
    s = re.sub(r"\^\s*\{([^{}]*)\}", lambda m: "".join(ch.translate(_SUP) for ch in m.group(1)), s)
    s = re.sub(r"\^\s*(\w)", lambda m: m.group(1).translate(_SUP), s)
    s = re.sub(r"_\s*\{([^{}]*)\}", lambda m: "".join(ch.translate(_SUB) for ch in m.group(1)), s)
    s = re.sub(r"_\s*(\w)", lambda m: m.group(1).translate(_SUB), s)
    s = s.replace("{", "").replace("}", "").replace("$", "")
    return re.sub(r"[ \t]{2,}", " ", s).strip()


_INLINE_MATH = re.compile(r"\$([^$\n]{1,200})\$")
_DISPLAY_MATH = re.compile(r"\$\$([^$]{1,600})\$\$")
_INLINE_PAREN = re.compile(r"\\\(([^\n]{1,200}?)\\\)")
_DISPLAY_BRACKET = re.compile(r"\\\[(.{1,600}?)\\\]", re.DOTALL)


def _math(text: str) -> str:
    """把 `$…$` / `$$…$$` 与 `\\(…\\)` / `\\[…\\]` 换成可读文本。"""
    text = _DISPLAY_MATH.sub(lambda m: "\n" + latex_to_text(m.group(1)) + "\n", text)
    text = _DISPLAY_BRACKET.sub(lambda m: "\n" + latex_to_text(m.group(1)) + "\n", text)
    text = _INLINE_MATH.sub(lambda m: latex_to_text(m.group(1)), text)
    return _INLINE_PAREN.sub(lambda m: latex_to_text(m.group(1)), text)


# ---------------------------------------------------------------- Markdown → HTML

def md_to_html(text: str) -> str:
    """讲义正文用的极简 Markdown：段落 / `#` 标题 / `-` 列表 / `**粗体**` / `` `代码` ``。"""
    text = _math(text or "")
    parts: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                parts.append("</ul>")
                in_list = False
            continue
        m = re.match(r"^(#{1,4})\s+(.*)$", line)
        if m:
            if in_list:
                parts.append("</ul>")
                in_list = False
            level = min(3, len(m.group(1)) + 1)        # 讲义里的 # 是"节内小标题"，别和章节抢 h1
            parts.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            continue
        if re.match(r"^\s*[-*]\s+", line):
            if not in_list:
                parts.append("<ul>")
                in_list = True
            parts.append(f"<li>{_inline(re.sub(r'^\\s*[-*]\\s+', '', line))}</li>")
            continue
        if in_list:
            parts.append("</ul>")
            in_list = False
        parts.append(f"<p>{_inline(line)}</p>")
    if in_list:
        parts.append("</ul>")
    return "\n".join(parts)


def _inline(s: str) -> str:
    s = _html.escape(s, quote=False)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"`([^`]+)`", r"<span class='code'>\1</span>", s)
    return s


# ---------------------------------------------------------------- HTML 组装

_CSS = """
@font-face { font-family: study; src: url(%(font)s); }
body { font-family: study; font-size: 10.5pt; line-height: 1.65; color: #1b2431; }
h1 { font-size: 20pt; margin: 0 0 4pt 0; }
h2 { font-size: 13pt; margin: 14pt 0 4pt 0; border-bottom: 1px solid #d4dde8; padding-bottom: 2pt; }
h3 { font-size: 11.5pt; margin: 10pt 0 3pt 0; }
p { margin: 3pt 0; }
ul { margin: 3pt 0 3pt 12pt; padding: 0; }
li { margin: 1pt 0; }
.meta { color: #5f6b7a; font-size: 8.5pt; }
.tag { color: #2563eb; font-size: 9pt; }
.pit { border-left: 2pt solid #c9a227; padding-left: 6pt; margin: 4pt 0; }
.q { margin: 8pt 0 2pt 0; font-weight: bold; }
.opt { margin: 1pt 0 1pt 12pt; }
.ans { color: #0f7a43; }
.expl { color: #3c4756; }
.code { font-family: study; background: #f3f7fc; }
"""


def _esc(s: str) -> str:
    return _html.escape(s or "", quote=False)


def build_html(lesson: Lesson, *, source_name: str = "", style_label: str = "",
               when: str = "") -> str:
    """把 Lesson 拼成一页（可能多页）HTML，交给 Story 排版。"""
    head = [f"<h1>{_esc(lesson.chapter or '讲义')}</h1>"]
    bits = [b for b in (source_name, f"{len(lesson.sections)} 节",
                        f"{len(lesson.quiz)} 题", style_label, when) if b]
    if bits:
        head.append(f"<p class='meta'>{_esc(' · '.join(bits))}</p>")
    if lesson.summary_one_liner:
        head.append(f"<p class='meta'>{_esc(lesson.summary_one_liner)}</p>")

    body: list[str] = []
    if lesson.learning_objectives:
        body.append("<h2>学完能做到</h2><ul>")
        body += [f"<li>{_esc(o)}</li>" for o in lesson.learning_objectives]
        body.append("</ul>")

    for i, sec in enumerate(lesson.sections, 1):
        body.append(f"<h2>{i}. {_esc(sec.title)}</h2>")
        body.append(md_to_html(sec.explanation_md))
        if sec.key_points:
            body.append("<ul>")
            body += [f"<li>{_esc(k)}</li>" for k in sec.key_points]
            body.append("</ul>")
        for p in sec.pitfalls or []:
            body.append(f"<p class='pit'>易错：{_esc(p)}</p>")
        pages = sorted({r.page for r in sec.source_refs or []})
        if pages:
            body.append("<p class='tag'>出处：" +
                        "、".join(f"原文 p{p}" for p in pages) + "</p>")

    if lesson.quiz:
        body.append("<h2>习题</h2>")
        for i, q in enumerate(lesson.quiz, 1):
            body.append(f"<p class='q'>{i}. {_esc(q.stem)}</p>")
            for k, opt in enumerate(q.options, 0):
                body.append(f"<p class='opt'>{chr(65 + k)}. {_esc(opt)}</p>")
            body.append(f"<p class='ans'>答案 {_esc(q.answer)}</p>")
            if q.explanation:
                body.append(f"<p class='expl'>解析：{_esc(q.explanation)}</p>")
            if q.source_ref is not None:
                body.append(f"<p class='tag'>出处：原文 p{q.source_ref.page}</p>")

    if lesson.summary_bullets:
        body.append("<h2>这一章要记住的</h2><ul>")
        body += [f"<li>{_esc(b)}</li>" for b in lesson.summary_bullets]
        body.append("</ul>")

    return ("<html><head><meta charset='utf-8'></head><body>"
            + "\n".join(head + body) + "</body></html>")


def find_font() -> Optional[str]:
    for p in FONT_CANDIDATES:
        if Path(p).is_file():
            return p
    return None


def lesson_pdf(lesson: Lesson, path: Path, *, source_name: str = "",
               style_label: str = "", when: str = "") -> Path:
    """排成 PDF 写到 path（目录不存在就建）。返回 path。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    font = find_font()
    html = build_html(lesson, source_name=source_name, style_label=style_label, when=when)

    archive = pymupdf.Archive()
    css = _CSS % {"font": "study.ttc"}
    if font:
        archive.add(font, "study.ttc")
    else:
        css = _CSS.replace("@font-face { font-family: study; src: url(study.ttc); }", "")
        css = css.replace("font-family: study", "font-family: sans-serif")

    story = pymupdf.Story(html=html, user_css=css, archive=archive)
    mediabox = pymupdf.Rect(0, 0, PAGE_W, PAGE_H)
    where = pymupdf.Rect(MARGIN, MARGIN, PAGE_W - MARGIN, PAGE_H - MARGIN)

    def rectfn(rect_num, filled):
        return mediabox, where, pymupdf.Point(0, 0)

    # Story 会把整个字体文件塞进去（雅黑 19 MB，一份讲义就 19 MB）。
    # 先写进内存，子集化之后再落盘：同一份讲义 84 KB。
    # （别写临时文件再删：Windows 上 PyMuPDF 关文档后句柄还挂一会儿，删不掉会留 19 MB 垃圾。）
    buf = io.BytesIO()
    writer = pymupdf.DocumentWriter(buf)
    try:
        story.write(writer, rectfn)
    finally:
        writer.close()

    doc = pymupdf.open("pdf", buf.getvalue())
    try:
        doc.subset_fonts()
        doc.save(str(path), garbage=4, deflate=True)
    finally:
        doc.close()
    return path
