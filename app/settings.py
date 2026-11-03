"""模型接入的运行时设置：界面上改一次，落到 `settings.json`，优先于 `.env`。

为什么要有这么一层：`.env` 是给命令行和初次部署用的，改它得重启服务；
而"换个模型试试"是每天都会做的事。所以顺序是

    settings.json  >  环境变量（.env）  >  空

空串等于"没设置"，删掉那个键就回到 `.env` 的值 —— 界面上把密钥清空不该把配置弄坏，
它只是让位给下面那层。文件本身不进仓库（`.gitignore` 里有）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

#: 配置放项目根，不放 out/ —— 那是产物目录，会被清空、会被当成缓存。
SETTINGS_PATH = Path("settings.json")

FIELDS = ("base_url", "api_key", "model", "reasoning_effort")
ENV_OF = {"base_url": "LLM_BASE_URL", "api_key": "LLM_API_KEY",
          "model": "LLM_MODEL", "reasoning_effort": "LLM_REASONING_EFFORT"}

#: 思考等级。空串 = 不传这个参数（默认行为，兼容不认它的模型）。
EFFORTS: tuple[tuple[str, str], ...] = (
    ("", "默认"),
    ("low", "低 · 快"),
    ("medium", "中"),
    ("high", "高 · 想得多"),
)
EFFORT_VALUES = tuple(v for v, _ in EFFORTS)


def path() -> Path:
    return SETTINGS_PATH


def load() -> dict[str, str]:
    """读配置。文件不在、坏了、不是对象，一律当空 —— 配置坏不该让服务起不来。

    带 mtime 缓存：设置弹层一次要调 effective() + source_of()×4，每次都读盘就是
    五次 IO。这里按 (mtime_ns, size) 判断变没变，没变就直接回上一份。
    """
    global _CACHE, _CACHE_KEY
    p = path()
    try:
        st = p.stat()
        key = (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        key = (str(p), -1, -1)
    if key == _CACHE_KEY:
        return dict(_CACHE)
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        _CACHE, _CACHE_KEY = {}, key
        return {}
    if not isinstance(raw, dict):
        _CACHE, _CACHE_KEY = {}, key
        return {}
    _CACHE = {k: str(raw.get(k) or "") for k in FIELDS if raw.get(k)}
    _CACHE_KEY = key
    return dict(_CACHE)


_CACHE: dict[str, str] = {}
_CACHE_KEY: tuple = ("", -2, -2)


def save(data: dict[str, Any]) -> dict[str, str]:
    """只认已知字段；空串等于删除该键（回落到环境变量）。原子写。"""
    current = load()
    for k in FIELDS:
        if k not in data:
            continue
        v = str(data.get(k) or "").strip()
        if v:
            current[k] = v
        else:
            current.pop(k, None)
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(current, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, p)
    # 写完自己把缓存作废：mtime 分辨率在有些文件系统上是秒级，靠它判断会漏掉
    # 同一秒内的第二次保存。
    global _CACHE_KEY
    _CACHE_KEY = ("", -2, -2)
    return current


def effective() -> dict[str, str]:
    """真正生效的那份：settings.json 优先，环境变量兜底。"""
    stored = load()
    out: dict[str, str] = {}
    for k in FIELDS:
        v = (stored.get(k) or "").strip()
        if not v:
            v = (os.environ.get(ENV_OF[k]) or "").strip()
        out[k] = v
    return out


def source_of(key: str) -> str:
    """这个值是从哪来的：`settings` / `env` / 空。界面上要标出来。"""
    if (load().get(key) or "").strip():
        return "settings"
    if (os.environ.get(ENV_OF[key]) or "").strip():
        return "env"
    return ""


def _hint(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 10:
        return "…"
    return f"{key[:3]}…{key[-4:]}"


def public() -> dict[str, Any]:
    """给前端看的那份。**密钥不回明文**，只回有没有、尾四位。"""
    eff = effective()
    return {"base_url": eff["base_url"],
            "model": eff["model"],
            "reasoning_effort": eff["reasoning_effort"],
            "has_key": bool(eff["api_key"]),
            "key_hint": _hint(eff["api_key"]),
            "source": {k: source_of(k) for k in FIELDS},
            "efforts": [{"value": v, "label": lab} for v, lab in EFFORTS]}
