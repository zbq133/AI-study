import time
from pathlib import Path

import fitz
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
    """上传目录和成品目录都搬到临时目录，别往仓库里写。"""
    monkeypatch.setattr(serve, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(serve, "OUT_DIR", tmp_path / "out")
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


def test_lesson_job_fails_without_env(doc_pdf, monkeypatch):
    for key in serve.REQUIRED:
        monkeypatch.delenv(key, raising=False)
    with TestClient(serve.app) as client:
        doc_id, res = _parsed(client, doc_pdf)
        title = res["sections"][0]["title"]
        r = client.post("/api/lessons", json={"doc_id": doc_id, "chapter": title})
        snap = _wait(client, r.json()["job_id"])
    assert snap["state"] == "failed"
    assert "缺少环境变量" in snap["error"]
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
