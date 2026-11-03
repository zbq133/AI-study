import time
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from app import serve
from app.schema import (LessonSection, Outline, OutlineItem, QuizItem, QuizPack,
                        SourceRef, VerifyResult)

QUOTE = "this is body text line number 0"
FAKE = "这句话在原文里根本不存在"


class _FakeLLM:
    """顶替真模型：按内容作答，答对的那道题才留得下来。"""

    def __init__(self, base_url="", api_key="", model=""):
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0

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
