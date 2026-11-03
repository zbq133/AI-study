"""讲义的索引：out/index.json。

`out/` 里本来就只有一堆 `*.json`，重启之后谁也不知道哪份是什么 —— 左边目录也列不出来。
这里维护一张小表：一条讲义一行，按"文档 + 章节"去重（同一章重新生成就覆盖旧的，不留一堆
同名文件），写入用临时文件 + `os.replace`，中途崩了也不会留下半截 JSON。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

#: 索引文件名。放在 out/ 里，跟着产物一起走。
INDEX_NAME = "index.json"

#: read-modify-write 是一把整锁，不是只锁写盘那一下：两个并发的 put() 会各自
#: _read() 再各自 _write()，后写的把先写的那条冲掉。os.replace 只保证文件不写
#: 半截，管不了这个。生成有 _generate_lock 串行，但 DELETE 走的是另一条路。
_LOCK = threading.RLock()


def make_id(doc_id: str, chapter: str) -> str:
    """同文档同一章 = 同一个 id，重新生成就是覆盖，不是再存一份。"""
    return hashlib.sha1(f"{doc_id}\x00{chapter}".encode("utf-8")).hexdigest()[:12]


class Store:
    """out/ 里那张表的读写。坏了就当空的用，不让索引把整个服务拖死。"""

    def __init__(self, out_dir):
        self.out_dir = Path(out_dir)
        self.path = self.out_dir / INDEX_NAME

    # ---------------------------------------------------------------- 读
    def _read(self) -> list[dict]:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return []
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return []                     # 半截文件 / 手改坏了：当空的，下次写入会重排
        if not isinstance(data, list):
            return []
        return [r for r in data if isinstance(r, dict) and r.get("id")]

    def _write(self, rows: list[dict]) -> None:
        self.out_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, self.path)        # 原子替换：要么旧表，要么新表

    # ---------------------------------------------------------------- 写
    def put(self, rec: dict) -> dict:
        """插入或覆盖一条。返回存进去的那条（补了 created / updated）。"""
        with _LOCK:
            rows = self._read()
            now = time.strftime("%Y-%m-%dT%H:%M:%S")
            old = next((r for r in rows if r["id"] == rec["id"]), None)
            rec = dict(rec)
            rec["created"] = (old or {}).get("created") or now
            rec["updated"] = now
            rows = [r for r in rows if r["id"] != rec["id"]]
            rows.append(rec)
            rows.sort(key=lambda r: r.get("updated", ""), reverse=True)
            self._write(rows)
            return rec

    def delete(self, rid: str) -> bool:
        with _LOCK:
            rows = self._read()
            keep = [r for r in rows if r["id"] != rid]
            if len(keep) == len(rows):
                return False
            self._write(keep)
            return True

    def delete_many(self, ids) -> list[dict]:
        """一次删一批，返回真被删掉的那些记录（调用方靠它去删文件）。

        整批只读一次、写一次 —— 逐条 delete 会在中间把表写好几遍，
        而且要删 50 条就得加 50 次锁。
        """
        wanted = {str(i) for i in ids}
        if not wanted:
            return []
        with _LOCK:
            rows = self._read()
            gone = [r for r in rows if r["id"] in wanted]
            if not gone:
                return []
            self._write([r for r in rows if r["id"] not in wanted])
            return gone

    def get(self, rid: str) -> dict | None:
        with _LOCK:
            return next((r for r in self._read() if r["id"] == rid), None)

    # ---------------------------------------------------------------- 列表
    def list(self, query: str = "", limit: int = 50) -> list[dict]:
        with _LOCK:
            rows = self._read()
        q = (query or "").strip().lower()
        if q:
            rows = [r for r in rows
                    if q in (r.get("chapter") or "").lower()
                    or q in (r.get("filename") or "").lower()]
        rows.sort(key=lambda r: r.get("updated", ""), reverse=True)
        return rows[:limit]

    def file_of(self, rid: str) -> Path | None:
        """这条记录对应的讲义文件；记录在但文件被人删了就返回 None。"""
        rec = self.get(rid)
        if not rec or not rec.get("file"):
            return None
        p = self.out_dir / Path(rec["file"]).name     # 只认文件名，防目录穿越
        return p if p.is_file() else None
