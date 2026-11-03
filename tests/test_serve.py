import re
import time
from pathlib import Path

import pymupdf as fitz
import pytest
from fastapi.testclient import TestClient

from app import serve
from app.errors import Cancelled
from app.schema import (LessonSection, Outline, OutlineItem, QuizItem, QuizPack,
                        SourceRef, VerifyResult)

QUOTE = "this is body text line number 0"
FAKE = "这句话在原文里根本不存在"


class _FakeLLM:
    """顶替真模型：按内容作答，答对的那道题才留得下来。"""

    def __init__(self, base_url="", api_key="", model="", **kw):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.kw = kw                       # max_calls / max_tokens / should_stop

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        self.calls += 1
        name = getattr(model_cls, "__name__", "")
        if name == "Outline":
            return Outline(sections=[OutlineItem(title="知识点A", points=["考点1"], pages=[1])])
        if name == "LessonSection":
            return LessonSection(title="知识点A", explanation_md="讲解正文",
                                 key_points=["关键点1"], pitfalls=["易错点1"],
                                 source_refs=[SourceRef(page=1, quote=QUOTE)])
        if name == "QuizPack":
            return QuizPack(items=[
                QuizItem(type="single", stem="好题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="B",
                         source_ref=SourceRef(page=1, quote=QUOTE)),
                QuizItem(type="single", stem="坏题：下列说法正确的是？",
                         options=["甲", "乙", "丙", "丁"], answer="A",
                         source_ref=SourceRef(page=1, quote=FAKE))])
        if name == "VerifyResult":
            if "好题" not in user:
                return VerifyResult(answer="C")
            for line in user.splitlines():
                s = line.strip()
                if len(s) > 3 and s[1] == "." and "乙" in s:
                    return VerifyResult(answer=s[0])
            return VerifyResult(answer="?")
        raise AssertionError("未预期的 model_cls: " + name)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """上传目录、成品目录、设置文件都搬到临时目录，别往仓库里写也别读本机的。"""
    monkeypatch.setattr(serve, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(serve, "OUT_DIR", tmp_path / "out")
    monkeypatch.setattr(serve.conf, "SETTINGS_PATH", tmp_path / "settings.json")
    serve.DOCS.clear()
    serve.JOBS.clear()
    return tmp_path


@pytest.fixture()
def doc_pdf(env):
    path = env / "doc.pdf"
    doc = fitz.open()
    p1 = doc.new_page()
    p1.insert_text((72, 90), "Chapter 1 Introduction", fontsize=20)
    for i in range(8):
        p1.insert_text((72, 130 + i * 18), "this is body text line number %d" % i, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


@pytest.fixture()
def book_pdf(env):
    """两章的小书：同一个 doc_id 下能攒出两条历史，才测得出"删一条会不会把 PDF 带走"。"""
    path = env / "book.pdf"
    doc = fitz.open()
    for title in ("Chapter 1 Introduction", "Chapter 2 Methods"):
        page = doc.new_page()
        page.insert_text((72, 90), title, fontsize=20)
        for i in range(6):
            page.insert_text((72, 130 + i * 18),
                             "body line number %d of this chapter" % i, fontsize=10)
    doc.save(str(path))
    doc.close()
    return path


def _wait(client, job_id, timeout=60.0):
    end = time.time() + timeout
    while time.time() < end:
        snap = client.get(f"/api/jobs/{job_id}").json()
        if snap["state"] in ("done", "failed"):
            return snap
        time.sleep(0.05)
    raise AssertionError("后台任务没在 %.0f 秒内结束" % timeout)


def _upload(client, path, name="doc.pdf"):
    with path.open("rb") as fh:
        return client.post("/api/docs", files={"file": (name, fh, "application/pdf")})


def _parsed(client, path):
    """上传 + 解析，返回 (doc_id, 解析结果)。"""
    up = _upload(client, path).json()
    started = client.post(f"/api/docs/{up['doc_id']}/parse").json()
    if started["cached"]:
        return up["doc_id"], started["result"]
    snap = _wait(client, started["job_id"])
    assert snap["state"] == "done", snap["error"]
    return up["doc_id"], snap["result"]


def test_index_serves_page(env):
    with TestClient(serve.app) as client:
        r = client.get("/")
    assert r.status_code == 200
    assert "AI Study" in r.text
    # 界面拆成静态文件后，首页必须把 css / js 引上，否则页面是裸的
    assert "/static/app.css" in r.text
    assert "/static/app.js" in r.text


def test_static_assets_are_served_with_the_right_type(env):
    """js 的 Content-Type 猜错会被浏览器的严格 MIME 检查直接拒绝执行。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css")
        js = client.get("/static/app.js")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")
    assert "color-scheme" in css.text
    assert js.status_code == 200
    assert js.headers["content-type"].startswith("text/javascript")
    assert "EventSource" in js.text


def test_static_is_not_cached(env):
    """本地开发是边改边刷的，缓存住一个旧 app.js 能让人怀疑人生。"""
    with TestClient(serve.app) as client:
        r = client.get("/static/app.css")
    assert r.headers["cache-control"] == "no-cache"


def test_stylesheet_actually_hides_the_hide_class(env):
    """`.hide` 这条规则缺过一次，后果是习题答案在答题前就显出来了。

    样式表里少了 `.hide { display: none }`，HTML/JS 里所有靠它隐起来的东西
    （进度条、停按钮、习题的答案解析）全都从头露着 —— 而且不报任何错。
    所以这里不测"某个元素"的样式，直接盯住这条规则本身。
    """
    import re
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    rule = re.search(r"\.hide\s*\{([^}]*)\}", css)
    assert rule, "app.css 里没有 .hide 规则"
    assert "display" in rule.group(1) and "none" in rule.group(1), rule.group(1)


def test_colors_all_come_from_the_palette(env):
    """颜色栏要能换皮：主色必须走变量，不能是散落在规则里的字面量。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    assert ":root[data-theme=\"light\"]" in css, "没有浅色主题"
    body = re.search(r"\nbody\s*\{([^}]*)\}", css)
    assert body and "var(--bg)" in body.group(1), body.group(0) if body else "没有 body 规则"
    # 面板/正文/强调色这三样也得是变量，否则改一个还剩一堆写死的
    for var in ("--panel", "--fg", "--accent"):
        assert css.count(f"var({var})") >= 2, var
    # 浅色主题是覆盖变量，不许重复定义规则（那样会越改越乱）
    light = re.search(r":root\[data-theme=\"light\"\]\s*\{([^}]*)\}", css).group(1)
    assert "--bg:#eef1f4" in light.replace(" ", "")


def _luminance(hex_color):
    """相对亮度，用来算对比度（WCAG 的公式，够判断"淡不淡"了）。"""
    h = hex_color.lstrip("#")
    chans = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in chans]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def _contrast(a, b):
    la, lb = _luminance(a), _luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def test_light_theme_text_is_dark_enough_to_read(env):
    """浅色主题下正文和次要文字都要够黑 —— 之前那套灰字在白底上读得眼睛疼。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    light = dict(re.findall(r"--([\w-]+):\s*(#[0-9a-fA-F]{6})", re.search(
        r":root\[data-theme=\"light\"\]\s*\{([^}]*)\}", css).group(1)))
    for panel in ("panel", "bg"):        # 讲义栏和页面底色都要够白，正文才压得住
        for name, need in (("fg", 10.0), ("dim", 7.0), ("dim2", 6.0), ("dim3", 5.0)):
            got = _contrast(light[name], light[panel])
            assert got >= need, f"{name} 在 {panel} 上只有 {got:.1f}:1，要 ≥{need}"


def test_settings_button_is_big_enough_to_hit(env):
    """左下角那颗钉子要够大，不然要拿鼠标找。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    gear = re.search(r"\.gear\s*\{([^}]*)\}", css)
    assert gear, "没有 .gear 规则"
    size = float(re.search(r"font-size:\s*([\d.]+)px", gear.group(1)).group(1))
    pad = re.search(r"padding:\s*([\d.]+)px\s+([\d.]+)px", gear.group(1))
    assert size >= 16, f"⚙ 才 {size}px"
    assert pad and float(pad.group(1)) >= 10 and float(pad.group(2)) >= 18, gear.group(1)
    label = re.search(r"\.gear\s+span\s*\{([^}]*)\}", css)
    assert label and float(re.search(r"font-size:\s*([\d.]+)px", label.group(1)).group(1)) >= 14


def test_model_can_be_picked_from_a_real_dropdown(env):
    """模型得能点着换：datalist 不出下拉，只能手打，等于换不了。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
        css = client.get("/static/app.css").text
    assert '<select id="setModel">' in html, "模型还是个输入框"
    assert "datalist" not in html.lower(), "还留着 datalist"
    assert 'id="setModelFree"' in html, "手动填写没有入口"
    assert "MANUAL" in js and "pickedModel" in js
    assert "sel.value = current" in js or "sel.value" in js, "没把当前模型选上"
    # 新下拉要有样式，否则是浏览器默认的丑陋白块
    assert re.search(r"\.field\s+input,\s*\.field\s+select\s*\{", css), "select 没收进字段样式"


def test_source_column_can_be_collapsed(env):
    """收起原文之后讲义要独占整行；点出处时要先展开再跳。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
        html = client.get("/").text
        js = client.get("/static/app.js").text
    rule = re.search(r"\.cols\.no-src\s*\{([^}]*)\}", css)
    assert rule, "收起来没有换栅格"
    # 必须是"目录 + 讲义"两栏，不能偷偷留成三栏（那样讲义也没变大）
    assert re.search(r"grid-template-columns:\s*var\(--toc-w\)\s+minmax\(0,\s*1fr\)\s*;", rule.group(1)), rule.group(1)
    hidden = re.search(r"\.cols\.no-src\s+#source\s*\{([^}]*)\}", css)
    assert hidden and "none" in hidden.group(1)
    assert 'id="srcToggle"' in html
    assert "function setSourceCollapsed" in js
    # 点出处的时候如果原文栏是收起的，要先展开，否则 scrollIntoView 打在一个 display:none 的块上
    ref = js.index("e.target.closest('.ref')")
    assert js.index("srcCollapsed()", ref) < js.index("scrollIntoView", ref)


def test_history_bar_fits_inside_the_left_column(env):
    """管理模式下那行按钮原来会被左栏边界裁掉一半：栏要够宽，挤不下也要能折行。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    toc = re.search(r"--toc-w:\s*(\d+)px", css)
    assert toc, "左栏宽度得是个变量"
    assert int(toc.group(1)) >= 250, f"左栏只有 {toc.group(1)}px，装不下「全选 / 共 N 条 / 删除选中」"
    # 两套栅格都走变量，别只改一处
    assert css.count("var(--toc-w)") >= 2
    bar = re.search(r"\.histbar\s*\{([^}]*)\}", css)
    assert bar and "flex-wrap: wrap" in bar.group(1), "挤不下就该折行，不能溢出到右栏"
    assert re.search(r"#histDel\s*\{[^}]*margin-left:\s*auto", css), "按钮要靠右"


def test_left_column_rows_are_tall_enough_to_hit(env):
    """左栏每条太扁：6px 上下内边距 + 12.5px 字，滚着看像一坨。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    for sel, minimum in ((".hist-item", 10), (".toc-item", 10)):
        rule = re.search(sel.replace(".", r"\.") + r"\s*\{([^}]*)\}", css)
        assert rule, f"{sel} 规则没了"
        body = rule.group(1)
        pad = re.search(r"padding:\s*(\d+)px\s+\d+px", body)
        assert pad and int(pad.group(1)) >= minimum, f"{sel} 上下内边距只有 {pad and pad.group(1)}px"
        size = re.search(r"font-size:\s*([\d.]+)px", body)
        assert size and float(size.group(1)) >= 14, f"{sel} 字号只有 {size and size.group(1)}px"


def test_history_rows_are_separated_by_a_hairline(env):
    """几条同名条目挨在一起会糊成一坨，中间得有一条细线。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    for sel in (".hist-item", ".toc-item"):
        rule = re.search(re.escape(sel) + r"\s*\+\s*" + re.escape(sel) + r"\s*\{([^}]*)\}", css)
        assert rule, f"{sel} 之间没有分隔线"
        assert re.search(r"border-top:\s*1px\s+solid", rule.group(1)), rule.group(1)


def test_selected_rows_show_a_check_mark_not_a_blue_block(env):
    """勾上是"框里出现蓝色对钩"，不是把整块填成蓝的（一列蓝方块认不出是勾）。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
        html = client.get("/").text
        js = client.get("/static/app.js").text
    picked = re.search(r"\.hist\.managing \.hist-item\.picked \.pick[^{]*\{([^}]*)\}", css)
    assert picked and "border-color: var(--accent)" in picked.group(1), "选中没有描边"
    assert "background: var(--accent)" not in picked.group(1), "又填成实心蓝块了"
    # 对钩用边框画：一段 transform: rotate(45deg) 的直角
    tick = re.search(r"\.pick::after[^{]*\{([^}]*)\}", css)
    assert tick, "没有画对钩"
    assert "rotate(45deg)" in tick.group(1) and "border-width: 0 2px 2px 0" in tick.group(1), tick.group(1)
    # 全选那颗也走同一套（原来是原生 checkbox，和行里的方块长得不一样）
    assert 'id="histAll"' in html and "<input" not in html[html.index('id="histAll"') - 80:html.index('id="histAll"') + 60]
    assert re.search(r"function toggleAll\(\)", js), "全选的点击逻辑改名了"
    assert re.search(r"classList\.toggle\('picked', all\)", js), "全选那颗没跟着变对钩"
    assert "#histAll').checked" not in js, "还在用原生 checkbox 的 checked"


def test_default_theme_is_light(env):
    """没设置过颜色时默认浅色 —— 首屏（head 内联脚本）和设置面板两处都别各说各的。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
    head = html[html.index("study.theme"):html.index("</script>")]
    assert re.search(r"t\.mode\s*\|\|\s*'light'", head), "首屏脚本没把默认值设成浅色"
    assert re.search(r"t\.mode\s*\|\|\s*'light'", js), "设置面板没把默认值设成浅色"
    assert not re.search(r"t\.mode\s*\|\|\s*'dark'", js + head), "还有地方把默认当成深色"
    # applyTheme 是真正写 documentElement 的地方：它自己的兜底也必须是浅色，
    # 否则 readTheme() 返回 {} 时又把页面刷回深色（改过一版就是这么翻车的）
    body = js[js.index("function applyTheme"):js.index("function paintTheme")]
    assert re.search(r"const mode = t\.mode \|\| 'light'", body), "applyTheme 的兜底不是浅色"
    assert "dataset.theme = mode === 'custom' ? (t.base || 'light') : mode" in body.replace("\n", " ") \
        or re.search(r"dataset\.theme\s*=\s*mode === 'custom' \? \(t\.base \|\| 'light'\) : mode", body), body


def test_buttons_spring_back_when_pressed(env):
    """按钮要有按下去的手感：过渡走回弹曲线，按下缩一点，松开弹回来。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    assert re.search(r"--spring:\s*cubic-bezier\(", css), "没有定义回弹曲线"
    assert re.search(r"transition:[^;]*transform[^;]*var\(--spring\)", css), "transform 没走回弹曲线"
    assert re.search(r"button:not\(:disabled\):active\s*\{[^}]*transform:\s*scale\(", css)
    # 系统开了"减少动态效果"就得老实待着
    assert "prefers-reduced-motion: reduce" in css, "没有尊重减少动态效果"
    start = css.index("@media (prefers-reduced-motion: reduce)")
    block = css[start:css.index("\n}", start)]
    assert "transition: none" in block and "transform: none" in block, block


def test_source_column_starts_collapsed_and_text_keeps_three_quarters(env):
    """默认收起原文（先看讲义），收起时正文只占四分之三宽 —— 一行拉满读不下去。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
        js = client.get("/static/app.js").text
    # 存的是「展开过」；没存过 → 收起
    assert "study.srcExpanded" in js
    assert re.search(r"setSourceCollapsed\(_srcExpanded !== '1'\)", js), "默认没落成收起"
    # 收起来的那套栅格下，讲义正文要留出四分之一空白
    rule = re.search(r"\.cols\.no-src\s+#lessonBody\s*\{([^}]*)\}", css)
    assert rule, "收起时没给讲义限宽"
    m = re.search(r"padding-right:\s*(\d+)%", rule.group(1))
    assert m, rule.group(1)
    assert 15 <= int(m.group(1)) <= 35, f"留白比例 {m.group(1)}% 不合理"


def test_theme_reaches_the_page_before_the_first_paint(env):
    """主题要在 <head> 里就套上，不然暗色页面会先闪一下白。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
    head = html[:html.index("</head>")]
    assert "study.theme" in head, "首屏脚本没读主题"
    assert head.index("study.theme") < html.index("/static/app.js")
    assert 'id="themeMode"' in html and 'id="customBox"' in html
    for box in ("cBg", "cPanel", "cFg", "cAccent"):
        assert f'id="{box}"' in html
    assert "function applyTheme" in js and "dataset.theme" in js


def test_settings_are_split_into_five_tabs(env):
    """模型接入 / 语言风格 / 颜色 / 字号 / 版本 各是一个标签，点哪个只显示哪个。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
    assert 'id="setTabs"' in html
    tabs = re.findall(r'class="tab[^"]*"\s+data-tab="(\w+)"[^>]*>([^<]+)<', html)
    assert [t[0] for t in tabs] == ["model", "style", "theme", "font", "about"], tabs
    assert [t[1].strip() for t in tabs] == ["模型接入", "语言风格", "颜色", "字号", "版本"]
    panes = re.findall(r'class="pane([^"]*)"\s+data-pane="(\w+)"', html)
    assert [p[1] for p in panes] == ["model", "style", "theme", "font", "about"], panes
    # 除了模型接入，其余四个一开始都是 hide
    assert panes[0][0].strip() == ""
    assert all("hide" in p[0] for p in panes[1:]), panes
    assert "function showTab" in js
    assert "p.dataset.pane !== name" in js or "dataset.pane !== name" in js


def test_font_size_has_three_steps_and_applies_before_paint(env):
    """字号三档（小/中/大），用 zoom 整体缩放，而且在 <head> 里就先套上。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
    assert 'id="fontMode"' in html
    steps = re.findall(r'data-size="(\w+)"[^>]*>([^<]+)<', html)
    assert [s[0] for s in steps] == ["small", "normal", "large"], steps
    assert [s[1].strip() for s in steps] == ["小", "中", "大"]
    # 三档必须真的不一样大，否则"大"和"中"看不出区别
    scale = re.search(r"FONT_SCALE\s*=\s*\{([^}]*)\}", js)
    assert scale, "没有字号档位表"
    nums = [float(x) for x in re.findall(r":\s*([\d.]+)", scale.group(1))]
    assert len(nums) == 3 and nums[0] < nums[1] < nums[2], nums
    assert "style.zoom" in js, "没有真正缩放"
    assert re.search(r"style\.zoom\s*=\s*String\(scale\)", js), "缩放没按档位来，等于没效果"
    # 首屏就要套上，不然打开时字会先小一下再变大
    head = html[:html.index("</head>")]
    assert "study.font" in head and "zoom" in head
    assert head.index("study.font") < html.index("/static/app.js")


def test_version_tab_shows_the_version_and_has_an_update_button(env):
    """版本标签要显示版本号，旁边有「更新」按钮，点了会去问服务端。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
        health = client.get("/api/health").json()
    assert 'id="verNow"' in html and "v1.0" in html
    assert re.search(r'id="updBtn"[^>]*>更新<', html), "没有「更新」按钮"
    assert health["version"] == serve.__version__ == "1.0"
    assert "/api/health" in js and "location.reload()" in js
    # 打开设置时就要把服务端的版本号写回页面，不能一直显示写死的那个
    body = js[js.index("async function paintVersion"):js.index("$('#updBtn')")]
    assert "$('#verNow').textContent = 'v'" in body, "拿到的版本号没写回页面"


def test_quiz_hides_answers_until_the_student_answers(env):
    """答题前不出对策：答案解析和出处都塞在 .qans 里，初始必须带 hide。"""
    with TestClient(serve.app) as client:
        js = client.get("/static/app.js").text
    assert 'class="qans hide"' in js
    # 判分或点「看答案」之后才把 hide 摘掉
    assert ".qans').classList.remove('hide')" in js


def test_static_type_does_not_depend_on_the_os_guess(env, monkeypatch):
    """有的 Windows 把 .js 登记成 text/plain，浏览器会拒绝执行。

    类型表是写死的，系统怎么猜都不该影响结果 —— 这条用例把猜测改成 text/plain，
    模拟那台机器。注意 Starlette 是 `from mimetypes import guess_type`，要打在
    它自己那个名字上，改 mimetypes.guess_type 不起作用。
    """
    import starlette.responses as sr
    monkeypatch.setattr(sr, "guess_type", lambda *a, **k: ("text/plain", None))
    with TestClient(serve.app) as client:
        js = client.get("/static/app.js")
    assert js.headers["content-type"].startswith("text/javascript")


def test_static_cannot_reach_outside_the_web_dir(env):
    with TestClient(serve.app) as client:
        sneaky = client.get("/static/..%2Fserve.py")
        missing = client.get("/static/nope.css")
    assert sneaky.status_code == 404, sneaky.text
    assert missing.status_code == 404
    # 路由层可能先按 "/" 切掉了，所以再直接捅一次函数，确认拦截真的在函数里
    import asyncio
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        asyncio.run(serve.static_file("../serve.py"))


def test_upload_rejects_non_pdf(env, doc_pdf):
    with TestClient(serve.app) as client:
        r = client.post("/api/docs", files={"file": ("a.txt", b"hello", "text/plain")})
    assert r.status_code == 400


def test_parse_unknown_doc_is_404(env):
    with TestClient(serve.app) as client:
        assert client.post("/api/docs/nope/parse").status_code == 404


def test_lesson_before_parse_is_409(env, doc_pdf):
    with TestClient(serve.app) as client:
        doc_id = _upload(client, doc_pdf).json()["doc_id"]
        r = client.post("/api/lessons", json={"doc_id": doc_id, "chapter": "Chapter 1"})
    assert r.status_code == 409


def test_parse_job_lists_sections(doc_pdf):
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        again = client.post(f"/api/docs/{doc_id}/parse").json()
    assert res["pages"] == 1
    assert res["blocks"] >= 2
    assert any("Introduction" in s["title"] for s in res["sections"])
    # 第二次解析不该重跑（OCR 一次十几秒，重复跑纯属浪费）
    assert again["cached"] is True and again["job_id"] is None


def test_doc_id_comes_from_the_file_content(env, doc_pdf):
    """同一份 PDF 传几次都是同一个 doc_id —— 历史讲义靠它对上 record.doc_id。"""
    with TestClient(serve.app) as client:
        a = _upload(client, doc_pdf).json()
        b = _upload(client, doc_pdf, name="another-name.pdf").json()
    assert a["doc_id"] == b["doc_id"], "同一内容不该发新 id"
    assert a["doc_id"] == serve.content_id(doc_pdf)
    assert len(a["doc_id"]) == 16


def test_upload_rejects_a_pdf_named_impostor(env):
    """改个扩展名不算 PDF：开头得有 %PDF 标记。"""
    with TestClient(serve.app) as client:
        r = client.post("/api/docs", files={"file": ("evil.pdf", b"MZ" + b"x" * 200,
                                                     "application/pdf")})
    assert r.status_code == 400
    assert "%PDF" in r.json()["detail"]


def test_parse_can_be_recovered_from_disk_after_a_restart(env, doc_pdf):
    """重启之后 DOCS 是空的，但 uploads/<doc_id>.pdf 还在，parse 要能直接接回来。"""
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        serve.DOCS.clear()                     # 模拟重启
        assert serve.DOCS == {}
        again = client.post(f"/api/docs/{doc_id}/parse").json()
        assert again.get("job_id") is not None or again.get("cached"), again
        if again.get("job_id"):
            snap = _wait(client, again["job_id"])
            assert snap["state"] == "done", snap["error"]
            result = snap["result"]
        else:
            result = again["result"]
    assert result["sections"] == res["sections"]


def test_parse_recomputes_the_result_only_once(env, doc_pdf, monkeypatch):
    """cached 分支不该每次重算一遍章节统计。"""
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        called = []
        monkeypatch.setattr(serve, "_parse_result", lambda ir: called.append(1) or {})
        again = client.post(f"/api/docs/{doc_id}/parse").json()
    assert again["cached"] is True
    assert again["result"] == res
    assert called == [], "cached 分支又算了一遍"


def test_job_snapshot_and_finish_share_one_lock(env):
    """进度读写必须同一把锁，否则 SSE 读到的是拼起来的半个状态。"""
    job = serve.Job("parse", "x")
    assert job._lock is not None
    with job._lock:
        job.advance(30, "解析中")
        job.finish("done")
    snap = job.snapshot()
    assert snap["state"] == "done" and snap["percent"] == 100 and snap["stage"] == "完成"
    assert snap["cancellable"] is False


def test_job_snapshot_really_takes_the_lock():
    """把锁换成会炸的假锁：真拿了锁的路径会当场炸出来，光"有锁"测不出来。

    （`Lock.__enter__` 返回的是 True 不是锁本身，所以只能断言 `locked()` 的返回值，
    不能拿 `with ... as lk` 去比对。）
    """
    class _Explode:
        def __enter__(self):
            raise AssertionError("这道没拿锁")

        def __exit__(self, *a):
            return False

    job = serve.Job("parse", "x")
    assert job.locked() is job._lock          # 调用方连读一串字段时得有锁可拿

    job._lock = _Explode()
    with pytest.raises(AssertionError):
        job.snapshot()
    with pytest.raises(AssertionError):
        job.advance(50, "解析中")
    with pytest.raises(AssertionError):
        job.finish("done")


def test_parse_result_counts_chars_by_page(env, doc_pdf):
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
    ir = serve.DOCS[doc_id]["ir"]
    # 每节的字数要等于它页区间里所有块的字数（按页聚合和逐块扫必须一致）
    secs = serve.build_sections(ir)
    for s in res["sections"]:
        raw = next(x for x in secs if x.title == s["title"])
        expect = sum(len(b.text) for b in ir.blocks
                     if raw.page_start <= b.page <= raw.page_end)
        assert s["chars"] == expect


def test_lesson_job_fails_without_env(doc_pdf, monkeypatch):
    for key in serve.REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        title = res["sections"][0]["title"]
        r = client.post("/api/lessons", json={"doc_id": doc_id, "chapter": title})
        snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "failed"
    assert "缺少接入配置" in snap["error"]
    # 前端照着 error_info 说人话：kind 认得出、title/hint 都有
    info = snap["error_info"]
    assert info["kind"] == "config"
    assert info["title"] and info["hint"]


def test_lesson_job_writes_a_lesson(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        title = res["sections"][0]["title"]
        r = client.post("/api/lessons", json={"doc_id": doc_id, "chapter": title, "n_quiz": 2})
        snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "done", snap["error"]
    result = snap["result"]
    assert result["quiz"] == 1          # 出处是编的那道被丢掉
    assert result["sections"] == 1
    written = Path(result["file"])
    assert written.exists()
    assert written.parent == serve.OUT_DIR
    text = written.read_text(encoding="utf-8")
    assert "知识点A" in text
    # 落盘的 JSON 里必须带 blocks / bbox —— 前端要凭它们把出处高亮回原文，
    # 少一个字段就只能干瞪眼。
    ref = result["lesson"]["sections"][0]["source_refs"][0]
    assert ref["blocks"], ref
    assert ref["bbox"] is not None, ref

def test_layout_returns_blocks_with_ids(env, doc_pdf):
    """原文按页摊开，块 id 要和讲义里 source_ref.blocks 用同一套编号。"""
    with TestClient(serve.app) as client:
        doc_id, _ = _parsed(client, doc_pdf)
        r = client.get(f"/api/docs/{doc_id}/layout")
    assert r.status_code == 200
    pages = r.json()["pages"]
    assert pages and pages[0]["page"] == 1
    first = pages[0]["blocks"][0]
    assert first["id"] and first["text"]
    assert first["kind"] in {"heading", "para", "list", "formula", "table",
                             "figure", "caption"}


def test_layout_page_filter_keeps_only_the_asked_pages(env, doc_pdf):
    with TestClient(serve.app) as client:
        doc_id, _ = _parsed(client, doc_pdf)
        one = client.get(f"/api/docs/{doc_id}/layout?pages=1").json()["pages"]
        none = client.get(f"/api/docs/{doc_id}/layout?pages=9").json()["pages"]
    assert [p["page"] for p in one] == [1]
    assert none == []


def test_layout_before_parse_is_409(env, doc_pdf):
    """没解析就没有块，别让前端拿到空壳当成功。"""
    with TestClient(serve.app) as client:
        doc_id = _upload(client, doc_pdf).json()["doc_id"]
        r = client.get(f"/api/docs/{doc_id}/layout")
    assert r.status_code == 409


def test_layout_rejects_a_silly_page_spec(env, doc_pdf):
    with TestClient(serve.app) as client:
        doc_id, _ = _parsed(client, doc_pdf)
        bad = client.get(f"/api/docs/{doc_id}/layout?pages=abc")
        huge = client.get(f"/api/docs/{doc_id}/layout?pages=1-9999")
    assert bad.status_code == 400
    assert huge.status_code == 400

def test_vendor_assets_reach_a_subdirectory():
    """vendor/katex 在子目录里，/{name} 那种写法够不到它。"""
    with TestClient(serve.app) as client:
        r = client.get("/static/vendor/katex/katex.min.js")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/javascript")
    assert len(r.content) > 100000


def test_katex_fonts_are_served_as_woff2():
    """字体 MIME 写错浏览器会拒用，公式就变成一排方框。"""
    with TestClient(serve.app) as client:
        r = client.get("/static/vendor/katex/fonts/KaTeX_Main-Regular.woff2")
    assert r.status_code == 200
    assert r.headers["content-type"] == "font/woff2"
    assert r.content[:4] == b"wOF2"


def test_index_loads_katex_and_the_grader_before_app_js():
    """三个脚本的顺序是硬要求：KaTeX 先于 app.js，判分模块也先于 app.js。"""
    html = (serve.WEB_DIR / "index.html").read_text(encoding="utf-8")
    css = html.index("vendor/katex/katex.min.css")
    katex = html.index("vendor/katex/katex.min.js")
    grader = html.index("/static/grade.js")
    appjs = html.index("/static/app.js")
    assert css < katex < appjs
    assert katex < grader < appjs


def test_static_cannot_escape_through_a_subpath():
    with TestClient(serve.app) as client:
        r = client.get("/static/vendor/..%2F..%2Fserve.py")
    assert r.status_code == 404


def test_parse_can_be_stopped_too(doc_pdf, monkeypatch):
    """解析一本扫描件要一两分钟，"停"必须也能打断它，不能只对生成生效。"""
    seen = {}
    real = serve.extract_ir

    def spy(path, **kw):
        seen.update(kw)
        return real(path, ocr=False)

    monkeypatch.setattr(serve, "extract_ir", spy)
    with TestClient(serve.app) as client:
        _parsed(client, doc_pdf)
    assert callable(seen.get("should_stop"))


# ---------------------------------------------------------------- 取消与成本闸

class _StuckLLM(_FakeLLM):
    """一直卡在第一次调用上，直到有人点"停" —— 用来测取消真的能打断。"""

    def json(self, system, user, model_cls, temperature=0.3, retries=2):
        self.calls += 1
        stop = self.kw.get("should_stop")
        deadline = time.time() + 8
        while time.time() < deadline:
            if stop is not None and stop():
                raise Cancelled("停了")
            time.sleep(0.01)
        raise AssertionError("测试里等太久，说明取消没生效")


def _lesson_job(client, doc_pdf):
    doc_id, res = _parsed(client, doc_pdf)
    r = client.post("/api/lessons",
                    json={"doc_id": doc_id, "chapter": res["sections"][0]["title"]})
    return r.json()["job_id"]


def test_cancel_stops_a_running_lesson(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _StuckLLM)
    with TestClient(serve.app) as client:
        job_id = _lesson_job(client, doc_pdf)
        for _ in range(200):                     # 等它真的跑起来
            if client.get(f"/api/jobs/{job_id}").json()["state"] == "running":
                break
            time.sleep(0.01)
        r = client.post(f"/api/jobs/{job_id}/cancel")
        assert r.status_code == 200 and r.json()["ok"] is True
        snap = _wait(client, job_id)
    assert snap["state"] == "failed"
    assert snap["error_info"]["kind"] == "cancelled"
    assert snap["cancellable"] is False


def test_cancel_of_a_finished_job_is_a_no_op(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        job_id = _lesson_job(client, doc_pdf)
        _wait(client, job_id)
        r = client.post(f"/api/jobs/{job_id}/cancel")
    assert r.status_code == 200
    assert r.json()["state"] == "done"           # 已经跑完了，点停也不会改状态


def test_cancel_unknown_job_is_404():
    with TestClient(serve.app) as client:
        assert client.post("/api/jobs/nope/cancel").status_code == 404


def test_cost_limits_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("LESSON_MAX_CALLS", "7")
    monkeypatch.setenv("LESSON_MAX_TOKENS", "1234")
    assert serve._limit("LESSON_MAX_CALLS", serve.DEFAULT_MAX_CALLS) == 7
    assert serve._limit("LESSON_MAX_TOKENS", serve.DEFAULT_MAX_TOKENS) == 1234


def test_a_broken_limit_falls_back_to_the_default(monkeypatch):
    """环境变量写歪了不该让服务起不来。"""
    monkeypatch.setenv("LESSON_MAX_CALLS", "很多")
    assert serve._limit("LESSON_MAX_CALLS", 40) == 40
    monkeypatch.setenv("LESSON_MAX_CALLS", "0")
    assert serve._limit("LESSON_MAX_CALLS", 40) == 1
    monkeypatch.delenv("LESSON_MAX_CALLS")
    assert serve._limit("LESSON_MAX_CALLS", 40) == 40


# ---------------------------------------------------------------- 历史与索引

def test_finished_lesson_shows_up_in_history(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        job_id = _lesson_job(client, doc_pdf)
        snap = _wait(client, job_id)
        assert snap["state"] == "done", snap["error"]
        lid = snap["result"]["lesson_id"]
        rows = client.get("/api/lessons").json()["lessons"]
        one = client.get(f"/api/lessons/{lid}").json()
    assert [r["id"] for r in rows] == [lid]
    assert rows[0]["chapter"] == "Chapter 1 Introduction"
    assert rows[0]["quiz"] == 1
    assert rows[0]["calls"] >= 1
    assert one["lesson"]["sections"][0]["title"]          # 讲义本体能直接读回来

def test_history_search_filters(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        _wait(client, _lesson_job(client, doc_pdf))
        assert len(client.get("/api/lessons?q=Introduction").json()["lessons"]) == 1
        assert client.get("/api/lessons?q=静电场").json()["lessons"] == []


def test_delete_lesson_removes_the_file_too(doc_pdf, monkeypatch):
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        snap = _wait(client, _lesson_job(client, doc_pdf))
        lid = snap["result"]["lesson_id"]
        path = Path(snap["result"]["file"])
        assert path.exists()
        assert client.delete(f"/api/lessons/{lid}").status_code == 200
        assert client.get("/api/lessons").json()["lessons"] == []
        assert client.get(f"/api/lessons/{lid}").status_code == 404
    assert not path.exists()

# ---------------------------------------------------------------- 管理历史

def _two_lessons(client, pdf):
    """同一份 PDF 生成两章，返回 (两条 lesson_id, 那份 PDF 的 doc_id)。"""
    doc_id, res = _parsed(client, pdf)
    ids = []
    for sec in res["sections"][:2]:
        jid = client.post("/api/lessons",
                          json={"doc_id": doc_id, "chapter": sec["title"], "n_quiz": 1}).json()["job_id"]
        snap = _wait(client, jid)
        assert snap["state"] == "done", snap["error"]
        ids.append(snap["result"]["lesson_id"])
    return ids, doc_id


def test_history_list_says_how_much_space_each_one_takes(doc_pdf, monkeypatch):
    """光有"多少条"不够 —— 占地方的到底是讲义 JSON 还是那份 PDF，得看得出来。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        _wait(client, _lesson_job(client, doc_pdf))
        data = client.get("/api/lessons").json()
    row = data["lessons"][0]
    assert row["bytes"] > 0                      # 讲义 JSON 的大小
    assert row["pdf_bytes"] > 0                  # 对应的 PDF 还在
    assert data["total_bytes"] == row["bytes"] + row["pdf_bytes"]


def test_history_total_counts_a_shared_pdf_once(book_pdf, monkeypatch):
    """两章共用一份 PDF：合计里它只该出现一次，不然"腾出多少"会虚高好几倍。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        _two_lessons(client, book_pdf)
        data = client.get("/api/lessons").json()
    rows = data["lessons"]
    assert len(rows) == 2
    assert rows[0]["pdf_bytes"] == rows[1]["pdf_bytes"] > 0     # 同一个 doc_id
    assert data["total_bytes"] == sum(r["bytes"] for r in rows) + rows[0]["pdf_bytes"]


def test_purge_deletes_the_records_the_files_and_the_unused_pdf(book_pdf, monkeypatch):
    """批量删：索引、out 里的 JSON、以及没人再用的上传 PDF 一起清掉。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        ids, doc_id = _two_lessons(client, book_pdf)
        files = [Path(client.get(f"/api/lessons/{i}").json()["record"]["file"]) for i in ids]
        pdf = serve.UPLOAD_DIR / f"{doc_id}.pdf"
        assert pdf.exists()
        serve.DOCS.pop(doc_id, None)             # 当作重启过：内存里没这份了
        out = client.post("/api/lessons/purge", json={"ids": ids, "drop_pdfs": True}).json()
        assert out["deleted"] == 2 and out["pdfs"] == 1 and out["freed"] > 0
        assert client.get("/api/lessons").json()["lessons"] == []
    assert not any(f.exists() for f in files)
    assert not pdf.exists()


def test_purge_keeps_a_pdf_that_another_lesson_still_uses(book_pdf, monkeypatch):
    """同一份 PDF 生成过好几章：删掉其中一条不能把 PDF 带走，别的章还要它。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        ids, doc_id = _two_lessons(client, book_pdf)
        serve.DOCS.pop(doc_id, None)
        out = client.post("/api/lessons/purge", json={"ids": ids[:1], "drop_pdfs": True}).json()
        assert out["deleted"] == 1 and out["pdfs"] == 0
        assert len(client.get("/api/lessons").json()["lessons"]) == 1
    assert (serve.UPLOAD_DIR / f"{doc_id}.pdf").exists()


def test_purge_can_leave_the_pdfs_alone(book_pdf, monkeypatch):
    """只想清记录、留 PDF（下次重新解析不用再传一遍）时也要能选。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        ids, doc_id = _two_lessons(client, book_pdf)
        serve.DOCS.pop(doc_id, None)
        out = client.post("/api/lessons/purge", json={"ids": ids, "drop_pdfs": False}).json()
        assert out["deleted"] == 2 and out["pdfs"] == 0
    assert (serve.UPLOAD_DIR / f"{doc_id}.pdf").exists()


def test_purge_with_nothing_selected_changes_nothing(doc_pdf, monkeypatch):
    """前端勾选刚被清空就点了删除，不该把整张表抹了。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    monkeypatch.setattr(serve, "LLM", _FakeLLM)
    with TestClient(serve.app) as client:
        _wait(client, _lesson_job(client, doc_pdf))
        out = client.post("/api/lessons/purge", json={"ids": []}).json()
        assert out["deleted"] == 0 and out["freed"] == 0
        assert len(client.get("/api/lessons").json()["lessons"]) == 1


def test_unknown_lesson_is_404(env):
    with TestClient(serve.app) as client:
        assert client.get("/api/lessons/nope").status_code == 404
        assert client.delete("/api/lessons/nope").status_code == 404

def test_lesson_whose_file_vanished_says_410(env):
    """索引还在、文件被人手动删了 —— 说清楚要重新生成，别 500。"""
    with TestClient(serve.app) as client:
        serve._store().put({"id": "gone", "chapter": "第一章", "file": "gone.json"})
        r = client.get("/api/lessons/gone")
    assert r.status_code == 410


# ---------------------------------------------------------------- 探活与日志

def test_health_says_whether_the_key_is_configured(env, monkeypatch):
    for k in serve.REQUIRED:
        monkeypatch.delenv(k, raising=False)
    with TestClient(serve.app) as client:
        h = client.get("/api/health").json()
    assert h["ok"] is True
    assert h["llm"]["ready"] is False
    assert set(h["llm"]["missing"]) == set(serve.REQUIRED)
    assert h["version"] == serve.__version__
    assert h["uptime"] >= 0


def test_health_sees_the_key_and_the_limits(env, monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "http://x")
    monkeypatch.setenv("LLM_API_KEY", "k")
    monkeypatch.setenv("LLM_MODEL", "m")
    monkeypatch.setenv("LESSON_MAX_CALLS", "7")
    with TestClient(serve.app) as client:
        h = client.get("/api/health").json()
    assert h["llm"]["ready"] is True
    assert h["llm"]["model"] == "m"
    assert h["limits"]["max_calls"] == 7


def test_health_counts_jobs_and_lessons(env):
    with TestClient(serve.app) as client:
        serve._store().put({"id": "a", "chapter": "第一章", "file": "a.json"})
        serve._store().put({"id": "b", "chapter": "第二章", "file": "b.json"})
        h = client.get("/api/health").json()
    assert h["lessons"] == 2
    assert h["jobs"] == {"running": 0, "total": 0}
    assert h["docs"] == 0


def test_health_survives_a_broken_index(env):
    (env / "out").mkdir(parents=True, exist_ok=True)
    (env / "out" / "index.json").write_text("{ 这不是 json", encoding="utf-8")
    with TestClient(serve.app) as client:
        h = client.get("/api/health").json()
    assert h["ok"] is True and h["lessons"] == 0


def test_logging_goes_to_a_file_in_the_out_dir(tmp_path):
    import logging
    logfile = serve.setup_logging(tmp_path)
    added = [h for h in serve.log.handlers if isinstance(h, logging.FileHandler)]
    try:
        serve.log.info("打个卡 %s", "ok")
        for h in added:
            h.flush()
        assert "打个卡 ok" in logfile.read_text(encoding="utf-8")
        assert serve.setup_logging(tmp_path) == logfile          # 幂等
        assert len([h for h in serve.log.handlers
                    if isinstance(h, logging.FileHandler)]) == len(added)
    finally:
        for h in added:
            serve.log.removeHandler(h)
            h.close()


# ---------- 设置界面后端 ----------

@pytest.fixture()
def clean_env(env, monkeypatch):
    """把接入配置清干净，也不许去问真服务的模型列表。"""
    for key in serve.REQUIRED + ("LLM_REASONING_EFFORT",):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(serve, "_models_of", lambda eff: [])
    return env


def test_settings_endpoint_says_what_is_connected(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "deepseek-chat")
    with TestClient(serve.app) as client:
        body = client.get("/api/settings").json()
    s = body["settings"]
    assert s["model"] == "deepseek-chat"
    assert s["source"]["model"] == "env"
    assert {"value": "high", "label": "高 · 想得多"} in s["efforts"]
    assert body["models"] == []


def test_settings_endpoint_does_not_echo_the_key(clean_env, monkeypatch):
    monkeypatch.setenv("LLM_API_KEY", "sk-secret-value-1234")
    with TestClient(serve.app) as client:
        text = client.get("/api/settings").text
    assert "sk-secret-value-1234" not in text
    assert "sk-…1234" in text


def test_post_settings_saves_and_takes_effect(clean_env):
    with TestClient(serve.app) as client:
        r = client.post("/api/settings", json={"base_url": "https://api.example.com",
                                               "model": "m1", "reasoning_effort": "high"})
        assert r.status_code == 200
        assert r.json()["settings"]["model"] == "m1"
        assert r.json()["settings"]["source"]["model"] == "settings"
        assert client.get("/api/config").json()["ready"] is False   # 还差密钥
    assert serve.conf.load()["reasoning_effort"] == "high"


def test_post_settings_rejects_junk(clean_env):
    with TestClient(serve.app) as client:
        assert client.post("/api/settings", json={"base_url": "ftp://x"}).status_code == 400
        assert client.post("/api/settings",
                           json={"reasoning_effort": "极高"}).status_code == 400


def test_settings_make_the_service_ready(clean_env):
    with TestClient(serve.app) as client:
        client.post("/api/settings", json={"base_url": "https://api.example.com",
                                           "api_key": "sk-x", "model": "m1"})
        assert client.get("/api/config").json()["ready"] is True
        assert client.get("/api/health").json()["llm"]["ready"] is True


def test_settings_need_no_restart(clean_env):
    """改完设置立刻能生成 —— 不用重启服务，这是做这一栏的全部理由。"""
    with TestClient(serve.app) as client:
        assert client.get("/api/config").json()["ready"] is False
        client.post("/api/settings", json={"base_url": "https://api.example.com",
                                           "api_key": "sk-x", "model": "m1"})
        assert client.get("/api/config").json()["model"] == "m1"


def test_chapter_pages_are_not_scraped_off_the_option_text(env):
    """页码要从解析结果里取，别拿正则去抠 DOM 上的显示文案。"""
    with TestClient(serve.app) as client:
        js = client.get("/static/app.js").text
    assert "opt.textContent.match" not in js, "又在拿 DOM 当数据库用"
    assert "sections = res.sections" in js
    assert "function byTitle" in js


def test_graded_questions_keep_a_way_back_to_the_explanation(env):
    """判过之后「看答案」变成「看解析」，不能逼人重答一遍才看得见解析。"""
    with TestClient(serve.app) as client:
        js = client.get("/static/app.js").text
    body = js[js.index("function lockQuestion"):js.index("function revealAnswer")]
    assert "show.classList.remove('hide')" in body
    assert "'看解析'" in body


def test_source_column_is_fetched_only_for_the_pages_in_view(env):
    """原文栏按需取页，别把整本书的块文本全下载下来。"""
    with TestClient(serve.app) as client:
        js = client.get("/static/app.js").text
    body = js[js.index("function renderSource"):js.index("function paintSource")]
    assert "?pages=${spec}" in body
    assert "refPages()" in body


def test_a_refused_reasoning_effort_is_shown_to_the_user(env, doc_pdf, monkeypatch):
    """模型不认思考等级时要说出来，别假装它生效了。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)

    class _PickyLLM(_FakeLLM):
        def __init__(self, *a, **kw):
            super().__init__(*a, **kw)
            self.reasoning_note = "当前模型不接受「思考等级」，这次已经按默认跑过去了。"

    monkeypatch.setattr(serve, "LLM", _PickyLLM)
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        title = res["sections"][0]["title"]
        r = client.post("/api/lessons", json={"doc_id": doc_id, "chapter": title, "n_quiz": 1})
        snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "done"
    assert "思考等级" in snap["result"]["reasoning_note"]


def test_style_tab_offers_three_tones_and_marks_the_default(env):
    """三档风格都要露出来，默认那颗要亮着。"""
    with TestClient(serve.app) as client:
        html = client.get("/").text
        js = client.get("/static/app.js").text
        data = client.get("/api/settings").json()
    assert 'id="setStyle"' in html and 'id="styleHint"' in html
    assert [s["value"] for s in data["settings"]["styles"]] == ["rigorous", "vivid", "humorous"]
    assert data["settings"]["style"] == "rigorous"
    assert "function paintStyles" in js
    assert "value === (current || 'rigorous')" in js


def test_style_is_saved_and_reaches_the_pipeline(env, doc_pdf, monkeypatch):
    """选了风格要落进 settings.json，并且真的进了给模型的 system 提示词。"""
    for key, val in zip(serve.REQUIRED, ("http://x", "sk-test", "fake-model")):
        monkeypatch.setenv(key, val)
    seen_systems = []

    class _RecLLM(_FakeLLM):
        def json(self, system, user, model_cls, temperature=0.3, retries=2):
            seen_systems.append(system)
            return super().json(system, user, model_cls, temperature, retries)

    monkeypatch.setattr(serve, "LLM", _RecLLM)
    with TestClient(serve.app) as client:
        assert client.post("/api/settings", json={"style": "humorous"}).status_code == 200
        assert client.get("/api/settings").json()["settings"]["style"] == "humorous"
        doc_id, res = _parsed(client, doc_pdf)
        r = client.post("/api/lessons",
                        json={"doc_id": doc_id, "chapter": res["sections"][0]["title"],
                              "n_quiz": 1})
        _wait(client, r.json()["job_id"])
    assert any("幽默诙谐" in s for s in seen_systems), "风格没进提示词"
    assert not any("严谨认真" in s for s in seen_systems)


def test_a_junk_style_is_refused(env):
    with TestClient(serve.app) as client:
        r = client.post("/api/settings", json={"style": "说相声"})
    assert r.status_code == 400
    assert "语言风格" in r.json()["detail"]


def test_clicking_a_row_does_not_draw_a_black_frame(env):
    """鼠标点一下不该在整行外面挂一圈黑框（那是默认焦点环），键盘走位才给。"""
    with TestClient(serve.app) as client:
        css = client.get("/static/app.css").text
    assert re.search(r"button:focus,\s*\.hist-item:focus[^{]*\{[^}]*outline:\s*none", css), \
        "没关掉鼠标点击的焦点环"
    assert re.search(r":focus-visible[^{]*\{[^}]*outline:\s*2px solid var\(--accent\)", css), \
        "键盘用户看不见焦点在哪了"
