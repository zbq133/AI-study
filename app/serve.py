"""本地 Web 服务：上传 PDF → 选章节 → 跑出讲义。

    python -m app.serve            # 打开 http://127.0.0.1:8765

只监听 127.0.0.1，不对外。上传的文件放 uploads/，成品照旧落 out/。
一次生成要几十秒、十几次模型调用，所以两条任务都丢到后台线程跑，前端靠
/api/jobs/{id}/events 这个 SSE 流看进度 —— 同步跑的话浏览器早就超时了。
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import BaseModel

from .llm import LLM
from .pdf import NoTextLayer, extract_ir
from .pipeline import build_lesson
from .schema import build_sections, is_reference_title

load_dotenv()

HOST = "127.0.0.1"
PORT = 8765
UPLOAD_DIR = Path("uploads")
OUT_DIR = Path("out")
MAX_UPLOAD = 80 * 1024 * 1024
REQUIRED = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

# 解析和生成都占 CPU/网络，两个线程够用；生成再加一把锁，一次只跑一份，
# 免得不小心把 key 的额度并发打光。
_executor = ThreadPoolExecutor(max_workers=2)
_generate_lock = threading.Lock()
_jobs_lock = threading.Lock()

JOBS: dict[str, "Job"] = {}
DOCS: dict[str, dict[str, Any]] = {}


class Job:
    """一条后台任务的进度快照。"""

    def __init__(self, kind: str, label: str = ""):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind                      # parse | lesson
        self.label = label
        self.state = "queued"                 # queued | running | done | failed
        self.stage = "排队中"
        self.percent = 0
        self.message = ""
        self.result: Any = None
        self.error = ""
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.t0 = time.time()
        self.t1 = 0.0

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "state": self.state, "stage": self.stage, "percent": self.percent,
                "message": self.message, "error": self.error,
                "elapsed": round((self.t1 or time.time()) - self.t0, 1),
                "calls": self.calls, "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "result": self.result}

    def advance(self, percent: int, stage: str, message: str = "") -> None:
        self.percent = max(self.percent, min(percent, 99))
        self.stage = stage
        if message:
            self.message = message


def _new_job(kind: str, label: str = "") -> Job:
    job = Job(kind, label)
    with _jobs_lock:
        JOBS[job.id] = job
    return job


def _spawn(job: Job, fn, *args) -> None:
    """把同步函数丢进线程池，状态变化写回 job。"""
    loop = asyncio.get_running_loop()

    async def runner():
        job.state = "running"
        try:
            await loop.run_in_executor(_executor, fn, job, *args)
            job.state = "done"
            job.percent = 100
        except NoTextLayer as exc:
            job.state = "failed"
            job.error = str(exc)
        except ValueError as exc:
            job.state = "failed"
            job.error = str(exc)
        except Exception as exc:                     # 兜底：别让异常消失在线程里
            job.state = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            job.t1 = time.time()
            if job.state == "done":
                job.stage = "完成"

    asyncio.create_task(runner())


# ---------------------------------------------------------------- 后台干活

def _parse_result(ir) -> dict:
    """把 IR 折成前端要的那点东西：章节表 + 统计。"""
    secs = build_sections(ir)
    shown = [(i, s) for i, s in enumerate(secs, 1) if not is_reference_title(s.title)]
    return {
        "pages": ir.page_count,
        "blocks": len(ir.blocks),
        "ocr_pages": ir.ocr_pages,
        "hidden": len(secs) - len(shown),
        "sections": [{"n": i, "title": s.title, "path": s.path,
                      "page_start": s.page_start, "page_end": s.page_end,
                      "chars": sum(len(b.text) for b in ir.blocks
                                   if s.page_start <= b.page <= s.page_end)}
                     for i, s in shown],
    }


def _do_parse(job: Job, doc_id: str) -> None:
    doc = DOCS[doc_id]
    job.advance(10, "解析 PDF", "文字层优先，没有文字的页走 OCR（一页约 10 秒）")
    ir = extract_ir(doc["path"], ocr_cache=OUT_DIR / "ocr")
    doc["ir"] = ir
    job.advance(80, "切分章节")
    job.result = _parse_result(ir)
    job.advance(99, "解析完成")


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip()[:80]


def _do_lesson(job: Job, doc_id: str, chapter: str, n_quiz: int) -> None:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise ValueError("缺少环境变量：" + ", ".join(missing) + "（在 .env 里填）")

    ir = DOCS[doc_id]["ir"]
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"],
              os.environ["LLM_MODEL"])

    def on_step(info: dict) -> None:
        stage = info.get("stage")
        if stage == "outline":
            job.advance(5, "规划大纲")
        elif stage == "section":
            total = max(1, info.get("total") or 1)
            done = (info.get("index") or 1) - 1
            job.advance(5 + int(75 * done / total), f"写第 {done + 1}/{total} 节",
                        info.get("title", ""))
        elif stage == "quiz":
            job.advance(82, "出题并逐一独立验证", "每道题都要重解一遍，比较慢")
        elif stage == "done":
            job.advance(95, "整理讲义")

    with _generate_lock:
        lesson = build_lesson(llm, ir, chapter, n_quiz, on_step=on_step)

    out = OUT_DIR / f"{Path(DOCS[doc_id]['path']).stem}_{_safe(chapter)}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(lesson.model_dump_json(indent=2), encoding="utf-8")

    job.calls = getattr(llm, "calls", 0)
    job.prompt_tokens = getattr(llm, "prompt_tokens", 0)
    job.completion_tokens = getattr(llm, "completion_tokens", 0)
    job.result = {"file": str(out), "chapter": chapter,
                  "sections": len(lesson.sections), "quiz": len(lesson.quiz),
                  "lesson": lesson.model_dump()}


# ---------------------------------------------------------------- 接口

app = FastAPI(title="AI Study", docs_url=None, redoc_url=None, openapi_url=None)


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    return INDEX_HTML


@app.post("/api/docs")
async def upload(file: UploadFile) -> dict:
    name = file.filename or "upload.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "只接受 .pdf 文件")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    doc_id = uuid.uuid4().hex[:12]
    dest = UPLOAD_DIR / f"{doc_id}.pdf"
    size = 0
    with dest.open("wb") as fh:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                fh.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"文件超过 {MAX_UPLOAD // 1024 // 1024}MB")
            fh.write(chunk)
    DOCS[doc_id] = {"path": dest, "filename": name, "ir": None}
    return {"doc_id": doc_id, "filename": name, "bytes": size}


@app.post("/api/docs/{doc_id}/parse")
async def parse(doc_id: str) -> dict:
    doc = DOCS.get(doc_id)
    if doc is None:
        raise HTTPException(404, "没有这个 doc_id，先上传")
    if doc["ir"] is not None:
        return {"job_id": None, "cached": True, "result": _parse_result(doc["ir"])}
    job = _new_job("parse", doc["filename"])
    _spawn(job, _do_parse, doc_id)
    return {"job_id": job.id, "cached": False}


class LessonReq(BaseModel):
    doc_id: str
    chapter: str
    n_quiz: int = 3


@app.post("/api/lessons")
async def create_lesson(req: LessonReq) -> dict:
    doc = DOCS.get(req.doc_id)
    if doc is None:
        raise HTTPException(404, "没有这个 doc_id，先上传")
    if doc["ir"] is None:
        raise HTTPException(409, "这份 PDF 还没解析，先调 /api/docs/{id}/parse")
    n = max(1, min(req.n_quiz, 10))
    job = _new_job("lesson", req.chapter)
    _spawn(job, _do_lesson, req.doc_id, req.chapter, n)
    return {"job_id": job.id}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "没有这个 job")
    return job.snapshot()


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str) -> StreamingResponse:
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "没有这个 job")

    async def gen():
        last = None
        while True:
            body = json.dumps(job.snapshot(), ensure_ascii=False)
            if body != last:
                yield f"data: {body}\n\n"
                last = body
            if job.state in ("done", "failed"):
                break
            await asyncio.sleep(0.4)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/config")
async def config() -> dict:
    """前端先问一句 key 配好没有。"""
    return {"ready": all(os.environ.get(k) for k in REQUIRED),
            "missing": [k for k in REQUIRED if not os.environ.get(k)],
            "model": os.environ.get("LLM_MODEL", "")}


def main() -> None:
    import uvicorn
    print(f"AI Study 服务：http://{HOST}:{PORT}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


INDEX_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>AI Study</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
         background: #0f1419; color: #d7dde5; }
  .wrap { max-width: 860px; margin: 0 auto; padding: 28px 20px 80px; }
  h1 { font-size: 20px; margin: 0 0 4px; font-weight: 600; }
  .sub { color: #7c8794; font-size: 13px; margin-bottom: 24px; }
  .card { background: #151b23; border: 1px solid #232c38; border-radius: 10px;
          padding: 18px; margin-bottom: 16px; }
  .card h2 { font-size: 13px; color: #7c8794; font-weight: 600; margin: 0 0 12px;
             letter-spacing: .04em; }
  label { display: block; font-size: 13px; color: #9aa6b2; margin: 10px 0 6px; }
  select, input[type=number] { width: 100%; padding: 9px 10px; border-radius: 7px;
        border: 1px solid #2c3541; background: #0f1419; color: #d7dde5; font-size: 14px; }
  input[type=file] { width: 100%; font-size: 13px; color: #9aa6b2; }
  button { padding: 9px 18px; border-radius: 7px; border: 1px solid #2f6fb3;
           background: #1b4d80; color: #e6eef7; font-size: 14px; cursor: pointer; }
  button:disabled { opacity: .45; cursor: default; }
  .row { display: flex; gap: 12px; align-items: flex-end; }
  .row > * { flex: 1; }
  .row > button { flex: 0 0 auto; }
  .bar { height: 6px; background: #232c38; border-radius: 3px; overflow: hidden;
         margin: 14px 0 8px; }
  .bar > i { display: block; height: 100%; width: 0; background: #3f8fd6;
             transition: width .35s ease; }
  .meta { font-size: 12.5px; color: #7c8794; font-variant-numeric: tabular-nums; }
  .err { color: #e06c75; font-size: 13.5px; white-space: pre-wrap; }
  .ok { color: #5fb87a; }
  pre { background: #0b1016; border: 1px solid #1e252f; border-radius: 8px;
        padding: 12px; font-size: 12.5px; max-height: 420px; overflow: auto;
        white-space: pre-wrap; word-break: break-word; }
  .hide { display: none; }
  a { color: #5aa8e6; }
</style></head><body><div class="wrap">
<h1>AI Study</h1>
<div class="sub">上传课件 → 选一章 → 生成不复述原文的讲义，和答案经过独立验证的习题。</div>

<div class="card">
  <h2>1 · 课件</h2>
  <input type="file" id="file" accept=".pdf">
  <div class="row" style="margin-top:12px">
    <button id="up">上传并解析</button>
    <span class="meta" id="upMeta"></span>
  </div>
  <div class="bar hide" id="pBarWrap"><i id="pBar"></i></div>
  <div class="meta" id="pStage"></div>
  <div class="meta" id="pDetail"></div>
  <div class="err" id="pErr"></div>
</div>

<div class="card hide" id="pickCard">
  <h2>2 · 选章节</h2>
  <label for="sec">章节（页码与字数来自解析结果）</label>
  <select id="sec"></select>
  <label for="nq">出几道题</label>
  <input type="number" id="nq" value="3" min="1" max="10">
  <div class="row" style="margin-top:14px">
    <button id="go">生成讲义</button>
    <span class="meta" id="goMeta"></span>
  </div>
</div>

<div class="card hide" id="runCard">
  <h2>3 · 进度</h2>
  <div class="bar"><i id="bar"></i></div>
  <div class="meta" id="stage">—</div>
  <div class="meta" id="detail"></div>
  <div class="err" id="err"></div>
</div>

<div class="card hide" id="outCard">
  <h2>4 · 结果</h2>
  <div class="meta" id="summary"></div>
  <div id="preview"></div>
</div>

<script>
const $ = s => document.querySelector(s);
let docId = null;

function stageView(bar, stageEl, detailEl, snap) {
  bar.style.width = (snap.percent || 0) + '%';
  stageEl.textContent = snap.stage + (snap.state === 'running' ? '…' : '');
  detailEl.textContent = [snap.message, snap.calls ? `模型调用 ${snap.calls} 次` : '',
                          snap.elapsed ? `${snap.elapsed}s` : '']
                          .filter(Boolean).join(' · ');
}

function watch(jobId, onDone) {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = ev => {
    const snap = JSON.parse(ev.data);
    onDone.snap(snap);
    if (snap.state === 'done' || snap.state === 'failed') { es.close(); onDone.end(snap); }
  };
  es.onerror = () => { es.close(); };
}

$('#up').onclick = async () => {
  const f = $('#file').files[0];
  if (!f) return;
  $('#up').disabled = true; $('#pErr').textContent = '';
  $('#pBarWrap').classList.remove('hide');
  const fd = new FormData(); fd.append('file', f);
  try {
    const r = await fetch('/api/docs', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const up = await r.json();
    docId = up.doc_id;
    $('#upMeta').textContent = `${up.filename} · ${(up.bytes/1048576).toFixed(1)} MB`;
    const p = await fetch(`/api/docs/${docId}/parse`, { method: 'POST' });
    const pj = await p.json();
    if (pj.cached) { renderSections(pj.result); $('#up').disabled = false; return; }
    watch(pj.job_id, {
      snap: s => stageView($('#pBar'), $('#pStage'), $('#pDetail'), s),
      end: s => {
        $('#up').disabled = false;
        if (s.state === 'failed') { $('#pErr').textContent = s.error; return; }
        renderSections(s.result);
      }
    });
  } catch (e) {
    $('#pErr').textContent = String(e.message || e);
    $('#up').disabled = false;
  }
};

function renderSections(res) {
  const sel = $('#sec');
  sel.innerHTML = '';
  for (const s of res.sections) {
    const o = document.createElement('option');
    o.value = s.title;
    o.textContent = `${String(s.n).padStart(2,' ')}. ${s.path.join(' > ')}  ·  p${s.page_start}-${s.page_end}  ·  ${s.chars} 字`;
    sel.appendChild(o);
  }
  $('#pickCard').classList.remove('hide');
  if (res.hidden) $('#goMeta').textContent = `另有 ${res.hidden} 条参考文献段已隐藏`;
}

$('#go').onclick = async () => {
  if (!docId) return;
  $('#go').disabled = true; $('#err').textContent = ''; $('#outCard').classList.add('hide');
  $('#runCard').classList.remove('hide');
  const body = JSON.stringify({ doc_id: docId, chapter: $('#sec').value,
                                n_quiz: Number($('#nq').value) || 3 });
  const r = await fetch('/api/lessons', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body });
  if (!r.ok) {
    $('#err').textContent = (await r.json()).detail || r.status;
    $('#go').disabled = false; return;
  }
  const { job_id } = await r.json();
  watch(job_id, {
    snap: s => {
      stageView($('#bar'), $('#stage'), $('#detail'), s);
      $('#goMeta').textContent = s.elapsed ? `${s.elapsed}s` : '';
    },
    end: s => {
      $('#go').disabled = false;
      if (s.state === 'failed') { $('#err').textContent = s.error; return; }
      showResult(s.result);
    }
  });
};

function showResult(res) {
  $('#outCard').classList.remove('hide');
  const L = res.lesson || {};
  $('#summary').innerHTML =
    `调用 ${res.sections ? '' : ''}<b>${L.sections ? L.sections.length : 0}</b> 节 · ` +
    `<b>${res.quiz || 0}</b> 道题通过验证 · 写入 <code>${res.file || ''}</code>`;
  const parts = [];
  if (L.learning_objectives?.length) {
    parts.push('学习目标');
    L.learning_objectives.forEach(x => parts.push('  • ' + x));
    parts.push('');
  }
  (L.sections || []).forEach((s, i) => {
    parts.push(`【${i+1}/${L.sections.length}】${s.title}`);
    parts.push(s.explanation_md);
    if (s.pitfalls?.length) parts.push('易错点：' + s.pitfalls.join('；'));
    parts.push('');
  });
  if (L.quiz?.length) {
    parts.push(`习题（${L.quiz.length} 道，均已独立重解验证）`);
    L.quiz.forEach((q, i) => {
      parts.push(`${i+1}. ${q.stem}`);
      (q.options || []).forEach((o, j) =>
        parts.push(`   ${String.fromCharCode(65+j)}. ${o}`));
      parts.push(`   答案 ${q.answer}`);
    });
  }
  const pre = document.createElement('pre');
  pre.textContent = parts.join('\\n');
  $('#preview').innerHTML = '';
  $('#preview').appendChild(pre);
}
</script></div></body></html>"""


if __name__ == "__main__":
    main()

