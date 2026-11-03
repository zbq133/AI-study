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
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from .errors import MissingConfig, describe
from .llm import LLM
from .pdf import extract_ir, page_block_map
from .pipeline import build_lesson
from .schema import build_sections, is_reference_title

load_dotenv()

HOST = "127.0.0.1"
PORT = 8765
UPLOAD_DIR = Path("uploads")
OUT_DIR = Path("out")
#: 前端三个文件放在 app/web/ 下，跟 Python 代码平级。不引构建链 —— 改一行
#: HTML 不该先跑一次 npm install。
WEB_DIR = Path(__file__).resolve().parent / "web"
#: 前端资源的类型表。**不交给系统的 MIME 猜测**：Windows 上 mimetypes 会去读
#: 注册表，某些机器把 .js 登记成 text/plain，浏览器严格 MIME 检查直接拒绝执行，
#: 表现是"页面出来了但按钮全不响应"。写死一张表，换机器也一样。
WEB_TYPES = {".css": "text/css; charset=utf-8",
             ".js": "text/javascript; charset=utf-8",
             ".html": "text/html; charset=utf-8",
             ".woff2": "font/woff2"}
MAX_UPLOAD = 80 * 1024 * 1024
REQUIRED = ("LLM_BASE_URL", "LLM_API_KEY", "LLM_MODEL")

#: 一次生成最多调几次、最多烧多少 token —— 手滑跑整本时兜底。
DEFAULT_MAX_CALLS = 40
DEFAULT_MAX_TOKENS = 200_000


def _limit(name: str, default: int) -> int:
    """从环境变量读一个正整数上限；写歪了就退回默认值，别让服务起不来。"""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        return default

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
        self.error_info: dict = {}          # kind/title/hint/detail，前端照着说人话
        self.cancel_evt = threading.Event()  # 前端点"停"就 set()
        self.calls = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.t0 = time.time()
        self.t1 = 0.0

    def snapshot(self) -> dict:
        return {"id": self.id, "kind": self.kind, "label": self.label,
                "state": self.state, "stage": self.stage, "percent": self.percent,
                "message": self.message, "error": self.error,
                "error_info": self.error_info,
                "cancellable": self.state in ("queued", "running"),
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
        except Exception as exc:                    # 兜底：别让异常消失在线程里
            job.state = "failed"
            job.error_info = describe(exc)
            job.error = str(exc) or job.error_info["title"]
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
    ir = extract_ir(doc["path"], ocr_cache=OUT_DIR / "ocr", should_stop=job.cancel_evt.is_set)
    doc["ir"] = ir
    job.advance(80, "切分章节")
    job.result = _parse_result(ir)
    job.advance(99, "解析完成")


def _parse_pages(spec: str) -> Optional[set[int]]:
    """把 `11,13-15` 解析成页号集合。空串表示"全都要"。"""
    spec = (spec or "").strip()
    if not spec:
        return None
    want: set[int] = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if not (a.strip().isdigit() and b.strip().isdigit()):
                raise HTTPException(400, "页号要写成 11 或 11-13")
            lo, hi = int(a), int(b)
            if lo > hi or hi - lo > 200:
                raise HTTPException(400, "页号区间不对")
            want.update(range(lo, hi + 1))
        elif part.isdigit():
            want.add(int(part))
        else:
            raise HTTPException(400, "页号要写成 11 或 11-13")
    return want


def _safe(name: str) -> str:
    bad = '<>:"/\\|?*'
    return "".join("_" if c in bad else c for c in name).strip()[:80]


def _do_lesson(job: Job, doc_id: str, chapter: str, n_quiz: int) -> None:
    missing = [k for k in REQUIRED if not os.environ.get(k)]
    if missing:
        raise MissingConfig(
            "缺少环境变量：" + ", ".join(missing),
            hint="在项目根目录的 .env 里填上这三个，然后重启服务。")

    ir = DOCS[doc_id]["ir"]
    llm = LLM(os.environ["LLM_BASE_URL"], os.environ["LLM_API_KEY"],
              os.environ["LLM_MODEL"],
              max_calls=_limit("LESSON_MAX_CALLS", DEFAULT_MAX_CALLS),
              max_tokens=_limit("LESSON_MAX_TOKENS", DEFAULT_MAX_TOKENS),
              should_stop=job.cancel_evt.is_set)

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


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html",
                        media_type=WEB_TYPES[".html"],
                        headers={"cache-control": "no-cache"})


@app.get("/static/{name:path}")
async def static_file(name: str) -> FileResponse:
    """只发 app/web/ 里的文件。

    不用 StaticFiles：那个不方便给每个响应加 no-cache，而本地开发时前端是边改
    边刷的，缓存住一个旧 app.js 能让人怀疑人生。
    """
    path = (WEB_DIR / name).resolve()
    if WEB_DIR not in path.parents or not path.is_file():
        raise HTTPException(404, "没有这个文件")
    return FileResponse(path,
                        media_type=WEB_TYPES.get(path.suffix, "application/octet-stream"),
                        headers={"cache-control": "no-cache"})


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


@app.get("/api/docs/{doc_id}/layout")
async def layout(doc_id: str, pages: str = "") -> dict:
    """原文按页摊开，前端拿它做"点出处高亮那一行"。

    块 id 和讲义里 `source_ref.blocks` 是同一套编号，所以前端只需要按 id 找。
    """
    doc = DOCS.get(doc_id)
    if doc is None:
        raise HTTPException(404, "没有这个 doc_id，先上传")
    ir = doc["ir"]
    if ir is None:
        raise HTTPException(409, "还没解析，先调 /parse")
    want = _parse_pages(pages)
    per = page_block_map(ir)
    out = []
    for pno in sorted(per):
        if want is not None and pno not in want:
            continue
        out.append({"page": pno,
                    "blocks": [{"id": b.id, "kind": b.kind, "text": b.text,
                                "level": b.level} for b in per[pno]]})
    return {"doc_id": doc_id, "pages": out}


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


@app.post("/api/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """叫停一条任务。已经跑完的任务点它不报错，只是什么都不发生。"""
    job = JOBS.get(job_id)
    if job is None:
        raise HTTPException(404, "没有这个 job_id")
    job.cancel_evt.set()
    if job.state in ("queued", "running"):
        job.stage = "正在停"
    return {"ok": True, "job_id": job_id, "state": job.state}


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


if __name__ == "__main__":
    main()

