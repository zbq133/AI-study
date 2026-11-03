#!/usr/bin/env bash
# 起本地服务（macOS / Linux）。服务只监听 127.0.0.1:8765，不对外网开放。
set -euo pipefail
cd "$(dirname "$0")"

PY="./.venv/bin/python"
if [ ! -x "$PY" ]; then
  echo "没有找到 .venv。先按 README 的「第一次」跑一遍：" >&2
  echo "  python3 -m venv .venv" >&2
  echo "  ./.venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

if [ ! -f .env ]; then
  echo "还没有 .env —— 复制 .env.example 填上 key 再跑。" >&2
  echo "不填也能起服务：能上传、能解析、能看原文，只有「生成讲义」会失败。"
fi

if command -v lsof >/dev/null 2>&1 && lsof -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
  echo "8765 已经被占用。先停掉它，或者改 app/serve.py 里的 PORT。" >&2
  exit 1
fi

echo "AI Study 服务：http://127.0.0.1:8765   （Ctrl+C 结束）"
echo "日志：out/server.log"
exec "$PY" -m app.serve
