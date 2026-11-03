"""本地 Web 服务：上传 PDF → 选章节 → 跑出讲义。

    python -m app.serve            # 打开 http://127.0.0.1:8765

只监听 127.0.0.1，不对外。上传的文件放 uploads/，成品照旧落 out/。
一次生成要几十秒、十几次模型调用，所以两条任务都丢到后台线程跑，前端靠
/api/jobs/{id}/events 这个 SSE 流看进度 —— 同步跑的话浏览器早就超时了。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import logging.handlers
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

from . import __version__
from . import settings as conf
from .errors import MissingConfig, describe
from .llm import LLM
from .pdf import content_id, extract_ir, page_block_map
from .pipeline import build_lesson
from .store import Store, make_id
from .schema import build_sections, is_reference_title

load_dotenv()

log = logging.getLogger("study")

#: 服务起来的时刻，/api/health 用它算 uptime。
STARTED = time.time()

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
_KEY_OF = {"LLM_BASE_URL": "base_url", "LLM_API_KEY": "api_key", "LLM_MODEL": "model"}


def _missing() -> list[str]:
    """还缺哪几项接入配置。按环境变量名报，界面上写的就是这几个名字。"""
    eff = conf.effective()
    return [name for name in REQUIRED if not eff[_KEY_OF[name]]]

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
    """一条后台任务的进度快照。

    状态是工作线程在写、SSE 协程每 0.4 秒在读，所以 advance() 和 snapshot()
    都拿同一把锁。以前只有 JOBS 字典的写入有锁，字段本身全靠 GIL 运气 ——
    尤其 result 是个大 dict，读到一半被换掉不是不可能。
    """

    def __init__(self, kind: str, label: str = ""):
        self._lock = threading.RLock()
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
        with self._lock:
            return {"id": self.id, "kind": self.kind, "label": self.label,
                    "state": self.state, "stage": self.stage, "percent": self.percent,
                    "message": self.message, "error": self.error,
                    "error_info": self.error_info,
                    "cancellable": self.state in ("queued", "running"),
                    "elapsed": round((self.t1 or time.time()) - self.t0, 1),
                    "calls": self.calls, "prompt_tokens": self.prompt_tokens,
                    "completion_tokens": self.completion_tokens,
                    "result": self.result}

    def locked(self):
        """给调用方一把锁：一次要连读/连写好几个字段时，用它包起来。"""
        return self._lock

    def advance(self, percent: int, stage: str, message: str = "") -> None:
        with self._lock:
            self.percent = max(self.percent, min(percent, 99))
            self.stage = stage
            if message:
                self.message = message

    def finish(self, state: str, error: BaseException | None = None) -> None:
        """收尾也走同一把锁：state / error_info / t1 要么一起变，要么都不变。"""
        with self._lock:
            self.state = state
            self.t1 = time.time()
            if state == "done":
                self.percent = 100
                self.stage = "完成"
            elif error is not None:
                self.error_info = describe(error)
                self.error = str(error) or self.error_info["title"]


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
            job.finish("done")
        except Exception as exc:                    # 兜底：别让异常消失在线程里
            job.finish("failed", exc)

    asyncio.create_task(runner())


# ---------------------------------------------------------------- 后台干活

def _parse_result(ir) -> dict:
    """把 IR 折成前端要的那点东西：章节表 + 统计。

    字数先按页聚合一次，再按节的页区间求和。原来每一节都把全书块扫一遍
    （O(章节 × 块)），换成几百页教材就是几十万次判断，而且 cached 分支每次
    重复 parse 都重算一遍。
    """
    secs = build_sections(ir)
    shown = [(i, s) for i, s in enumerate(secs, 1) if not is_reference_title(s.title)]
    by_page: dict[int, int] = {}
    for b in ir.blocks:
        by_page[b.page] = by_page.get(b.page, 0) + len(b.text)
    return {
        "pages": ir.page_count,
        "blocks": len(ir.blocks),
        "ocr_pages": ir.ocr_pages,
        "hidden": len(secs) - len(shown),
        "sections": [{"n": i, "title": s.title, "path": s.path,
                      "page_start": s.page_start, "page_end": s.page_end,
                      "chars": sum(by_page.get(p, 0)
                                   for p in range(s.page_start, s.page_end + 1))}
                     for i, s in shown],
    }


def _do_parse(job: Job, doc_id: str) -> None:
    doc = DOCS[doc_id]
    job.advance(10, "解析 PDF", "文字层优先，没有文字的页走 OCR（一页约 10 秒）")
    ir = extract_ir(doc["path"], ocr_cache=OUT_DIR / "ocr", should_stop=job.cancel_evt.is_set)
    doc["ir"] = ir
    doc["result"] = _parse_result(ir)      # 解析结果只算一次，cached 分支直接用
    job.advance(80, "切分章节")
    job.result = doc["result"]
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
    missing = _missing()
    if missing:
        raise MissingConfig(
            "缺少接入配置：" + ", ".join(missing),
            hint="点左下角「设置」填上接入地址、API 密钥和模型，或者写进 .env。")

    ir = DOCS[doc_id]["ir"]
    eff = conf.effective()
    llm = LLM(eff["base_url"], eff["api_key"], eff["model"],
              max_calls=_limit("LESSON_MAX_CALLS", DEFAULT_MAX_CALLS),
              max_tokens=_limit("LESSON_MAX_TOKENS", DEFAULT_MAX_TOKENS),
              should_stop=job.cancel_evt.is_set,
              reasoning_effort=eff["reasoning_effort"])

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
        elif stage == "verify":
            # 判题是最闷的一段：进度从 82% 挪到 94%，让人看见它还在动
            total = max(1, info.get("total") or 1)
            done = info.get("index") or 1
            job.advance(82 + int(12 * min(done, total) / total),
                        f"独立验证第 {done} 题", "原顺序解一次、打乱选项再解一次")
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
    rec = _store().put({
        "id": make_id(doc_id, chapter),
        "doc_id": doc_id,
        "chapter": chapter,
        "filename": DOCS[doc_id]["filename"],
        "file": out.name,
        "pages": len(lesson.sections),
        "sections": len(lesson.sections),
        "quiz": len(lesson.quiz),
        "calls": job.calls,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": job.completion_tokens,
        "seconds": round((job.t1 or time.time()) - job.t0, 1),
    })
    job.result = {"file": str(out), "chapter": chapter, "lesson_id": rec["id"],
                  "sections": len(lesson.sections), "quiz": len(lesson.quiz),
                  "lesson": lesson.model_dump()}
    # 思考等级被模型拒了要说出来 —— 界面上显示成"已按默认跑"，别假装生效了
    note = getattr(llm, "reasoning_note", "")
    if note:
        job.result["reasoning_note"] = note


# ---------------------------------------------------------------- 接口

app = FastAPI(title="AI Study", docs_url=None, redoc_url=None, openapi_url=None)


def setup_logging(out_dir: Path = OUT_DIR) -> Path:
    """把日志落到 out/server.log，单文件 2 MB 滚两个备份。

    只挂一次（重复调用不会重复加 handler），返回日志文件路径。
    放在这里而不是 import 时执行 —— 测试 import 这个模块不该在磁盘上留文件。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / "server.log"
    for h in log.handlers:
        if isinstance(h, logging.FileHandler) and Path(h.baseFilename) == target.resolve():
            return target
    h = logging.handlers.RotatingFileHandler(
        target, maxBytes=2 * 1024 * 1024, backupCount=2, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    return target


@app.middleware("http")
async def _log_requests(request, call_next):
    t0 = time.time()
    response = await call_next(request)
    path = request.url.path
    # 静态资源和 SSE 心跳太吵，只在出错时记。
    if not path.startswith("/static/") and response.status_code >= 400:
        log.warning("%s %s -> %s %.2fs", request.method, path,
                    response.status_code, time.time() - t0)
    elif not path.startswith(("/static/", "/api/jobs/")):
        log.info("%s %s -> %s %.2fs", request.method, path,
                 response.status_code, time.time() - t0)
    return response


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
    # id 按内容取（边收边算 sha256，不用再读一遍盘）：同一份 PDF 传几次都是同一个
    # doc_id，历史讲义里的 record.doc_id 才对得上，出处才能点回原文。
    hasher = hashlib.sha256()
    size = 0
    tmp: Optional[Path] = None
    fh = None
    try:
        while chunk := await file.read(1 << 20):
            size += len(chunk)
            if size > MAX_UPLOAD:
                raise HTTPException(413, f"文件超过 {MAX_UPLOAD // 1024 // 1024}MB")
            if size == len(chunk) and not chunk.startswith(b"%PDF-"):
                # 只看扩展名的话，80MB 的 exe 改个名就能躺进 uploads/
                raise HTTPException(400, "这个文件不是 PDF（开头没有 %PDF 标记）")
            if tmp is None:
                tmp = UPLOAD_DIR / ".incoming"
                fh = tmp.open("wb")
            fh.write(chunk)
            hasher.update(chunk)
    finally:
        if fh is not None:
            fh.close()
        elif tmp is not None:
            tmp.unlink(missing_ok=True)   # 没写成就别留空壳
    doc_id = hasher.hexdigest()[:16]
    dest = UPLOAD_DIR / f"{doc_id}.pdf"
    if tmp is not None:
        if dest.exists():
            tmp.unlink()                  # 同一份文件已经有了，不重复占地方
        else:
            tmp.replace(dest)
    if DOCS.get(doc_id) is None:
        DOCS[doc_id] = {"path": dest, "filename": name, "ir": None}
    return {"doc_id": doc_id, "filename": name, "bytes": size}


@app.post("/api/docs/{doc_id}/parse")
async def parse(doc_id: str) -> dict:
    doc = DOCS.get(doc_id)
    if doc is None:
        # 重启之后内存是空的，但 uploads/<doc_id>.pdf 还在。doc_id 是按内容算的，
        # 所以凭 id 就能把文件找回来，历史讲义的出处不用重新上传也能点回原文。
        path = UPLOAD_DIR / f"{doc_id}.pdf"
        if not path.is_file():
            raise HTTPException(404, "没有这个 doc_id，先上传")
        doc = DOCS[doc_id] = {"path": path, "filename": path.name, "ir": None}
    if doc["ir"] is not None:
        return {"job_id": None, "cached": True,
                "result": doc.get("result") or _parse_result(doc["ir"])}
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


def _store() -> Store:
    """每次现读 OUT_DIR —— 测试里会被 monkeypatch 到临时目录。"""
    return Store(OUT_DIR)


@app.get("/api/lessons")
async def list_lessons(q: str = "", limit: int = 50) -> dict:
    """生成过的讲义。重启之后还在，因为落在 out/index.json 里。"""
    return {"lessons": _store().list(q, limit=max(1, min(limit, 200)))}


@app.get("/api/lessons/{lesson_id}")
async def get_lesson(lesson_id: str) -> dict:
    rec = _store().get(lesson_id)
    if rec is None:
        raise HTTPException(404, "没有这份讲义")
    path = _store().file_of(lesson_id)
    if path is None:
        raise HTTPException(410, "讲义文件被删了，重新生成一次")
    return {"record": rec, "lesson": json.loads(path.read_text(encoding="utf-8"))}


@app.delete("/api/lessons/{lesson_id}")
async def delete_lesson(lesson_id: str) -> dict:
    st = _store()
    path = st.file_of(lesson_id)
    if not st.delete(lesson_id):
        raise HTTPException(404, "没有这份讲义")
    if path is not None:
        try:
            path.unlink()
        except OSError:
            pass                                  # 索引已经删了，文件删不掉不影响
    return {"ok": True}


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
    missing = _missing()
    return {"ready": not missing,
            "missing": missing,
            "model": conf.effective()["model"]}


class SettingsBody(BaseModel):
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None
    reasoning_effort: Optional[str] = None


@app.get("/api/settings")
async def read_settings() -> dict:
    """设置界面要的东西：现在接的是谁、密钥有没有（不回明文）、能选哪些模型。"""
    return {"settings": conf.public(), "models": _models_of(conf.effective())}


@app.post("/api/settings")
async def write_settings(body: SettingsBody) -> dict:
    """改设置。只传变化的那几项；密钥传空串 = 清掉（回落到 .env）。"""
    data = body.model_dump(exclude_none=True)
    url = (data.get("base_url") or "").strip()
    if url and not url.startswith(("http://", "https://")):
        raise HTTPException(400, "接入地址要以 http:// 或 https:// 开头")
    eff = (data.get("reasoning_effort") or "").strip()
    if eff not in conf.EFFORT_VALUES:
        raise HTTPException(400, "思考等级只能是空、low、medium、high")
    if "reasoning_effort" in data:
        data["reasoning_effort"] = eff
    conf.save(data)
    log.info("设置已更新：%s", ", ".join(sorted(data)) or "（没有变化）")
    return {"settings": conf.public(), "models": _models_of(conf.effective())}


def _models_of(eff: dict) -> list[str]:
    """问一下接入地址有哪些模型。问不到就回空表 —— 界面允许手填。"""
    base, key = eff["base_url"], eff["api_key"]
    if not base or not key:
        return []
    try:
        import httpx
        r = httpx.get(f"{base.rstrip('/')}/models", timeout=4.0,
                      headers={"Authorization": f"Bearer {key}"})
        if r.status_code != 200:
            return []
        rows = r.json().get("data") or []
    except Exception:          # 网络/解析都不该让设置界面打不开
        return []
    return sorted(str(m.get("id")) for m in rows if isinstance(m, dict) and m.get("id"))


@app.get("/api/health")
async def health() -> dict:
    """给脚本和监控看的：服务活着吗、key 配了没、现在几份任务在跑。

    故意不读 PDF、不碰模型 —— 探活接口必须便宜到可以每秒打一次。
    """
    missing = _missing()
    running = sum(1 for j in JOBS.values() if j.state in ("queued", "running"))
    try:
        lessons = len(_store().list(limit=10_000))
    except OSError:
        lessons = -1          # 索引读不了不该让探活变红
    return {"ok": True,
            "version": __version__,
            "uptime": round(time.time() - STARTED, 1),
            "llm": {"ready": not missing, "missing": missing,
                    "model": conf.effective()["model"]},
            "jobs": {"running": running, "total": len(JOBS)},
            "docs": len(DOCS),
            "lessons": lessons,
            "limits": {"max_calls": _limit("LESSON_MAX_CALLS", DEFAULT_MAX_CALLS),
                       "max_tokens": _limit("LESSON_MAX_TOKENS", DEFAULT_MAX_TOKENS)}}


def main() -> None:
    import uvicorn
    setup_logging()
    log.info("服务起来：http://%s:%s，版本 %s", HOST, PORT, __version__)
    print(f"AI Study 服务：http://{HOST}:{PORT}")
    print(f"日志：{(OUT_DIR / 'server.log').resolve()}")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()

