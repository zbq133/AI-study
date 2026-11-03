"""桌面客户端外壳：在后台起本地服务，然后开一个原生窗口指向它。

和 `python -m app.serve` 的区别只有一个 —— 那个要你自己开浏览器、自己敲地址；
这个双击就跑起来，窗口关掉进程就退，端口和地址都不用管。

用法：

    python -m app.desktop              # 正常启动
    python -m app.desktop --port 8765  # 指定端口（被占就走随机端口）
    python -m app.desktop --no-window  # 只起服务不开窗（调试用）

同一台机器上再双击一次不会开第二份服务：如果目标端口上的 `/api/health` 认得是本程序，
就只开一个窗口指过去。
"""

from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request

import uvicorn

from . import __version__
from .serve import app

#: 优先用这个端口；被别的程序占了就换一个空闲的。
PREFERRED_PORT = 8765
HOST = "127.0.0.1"
START_TIMEOUT = 30.0


def free_port() -> int:
    """向系统要一个当前空闲的端口（拿到就立刻释放，只是探个号）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind((HOST, 0))
        return int(s.getsockname()[1])


def port_open(port: int, timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((HOST, port)) == 0


def health(port: int, timeout: float = 1.0) -> dict | None:
    """读 `/api/health`；端口上不是本程序（回 404、回别的东西、连不上）就返回 None。"""
    url = f"http://{HOST}:{port}/api/health"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(data, dict) or "jobs" not in data or "llm" not in data:
        return None
    return data


def wait_ready(port: int, timeout: float = START_TIMEOUT) -> bool:
    """等服务真的能应答再开窗，否则窗口会先闪一下「无法访问」。"""
    end = time.time() + timeout
    while time.time() < end:
        if health(port) is not None:
            return True
        time.sleep(0.15)
    return False


def _ensure_streams() -> None:
    """窗口版 exe 没有控制台，`sys.stdout` 是 None。

    uvicorn 的日志格式器会调 `sys.stdout.isatty()`，None 上直接 AttributeError，
    于是程序在"起服务"这一步就死了，屏幕上只留一句 Unhandled exception。
    这里把空流接到 devnull，日志另走 out/server.log。
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            setattr(sys, name, open(os.devnull, "w", encoding="utf-8"))


class Service:
    """后台线程里跑的 uvicorn；`stop()` 让它把连接处理完再退。"""

    def __init__(self, port: int) -> None:
        self.port = port
        _ensure_streams()
        self._server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=HOST,
                port=port,
                log_config=None,      # 日志我们自己收（out/server.log），别让 uvicorn 去配 formatter
                log_level="warning",
            )
        )
        self._thread = threading.Thread(target=self._server.run, name="uvicorn", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._server.should_exit = True
        if self._thread.is_alive():
            self._thread.join(timeout)


def choose_port(preferred: int = PREFERRED_PORT) -> tuple[int, bool]:
    """挑端口；返回 (端口, 需不需要自己起服务)。

    端口上已经有一个本程序在跑 → 复用，不再起第二份（数据目录会打架）。
    """
    if health(preferred) is not None:
        return preferred, False
    if not port_open(preferred):
        return preferred, True
    return free_port(), True


def open_window(url: str, title: str = "AI Study", debug: bool = False) -> None:
    """开原生窗口。关掉窗口这个调用才返回。"""
    import webview  # 只有客户端模式才需要它，命令行/测试不受影响

    webview.create_window(
        title,
        url,
        width=1360,
        height=900,
        min_size=(960, 640),
        text_select=True,          # 讲义里的文字要能选、能复制
    )
    # private_mode=False + 固定 storage_path：主题、字号、折叠状态这些 localStorage 才留得住
    webview.start(
        debug=debug,
        private_mode=False,
        storage_path=str(_storage_dir()),
        gui="edgechromium",
    )


def _storage_dir():
    from pathlib import Path

    path = Path.home() / ".ai-study" / "webview"
    path.mkdir(parents=True, exist_ok=True)
    return path


def run(port: int | None = None, *, window: bool = True, debug: bool = False) -> int:
    """起服务 + 开窗口。返回进程退出码。"""
    picked, needs_own = choose_port(port or PREFERRED_PORT)
    service: Service | None = None
    if needs_own:
        from .serve import setup_logging

        setup_logging()                      # 客户端没有控制台，日志只能落在 out/server.log
        service = Service(picked)
        service.start()
        if not wait_ready(picked):
            service.stop()
            print(f"服务没能在 {START_TIMEOUT:.0f} 秒内起来，端口 {picked}", file=sys.stderr)
            return 1
    url = f"http://{HOST}:{picked}/"
    print(f"AI Study {__version__} → {url}")
    if not window:
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
        finally:
            if service:
                service.stop()
        return 0
    try:
        open_window(url, debug=debug)
    finally:
        if service:
            service.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    port: int | None = None
    window = True
    debug = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-h", "--help"):
            print(__doc__)
            return 0
        if a == "--port" and i + 1 < len(args):
            try:
                port = int(args[i + 1])
            except ValueError:
                print("端口要写数字，比如 --port 8765", file=sys.stderr)
                return 2
            i += 2
            continue
        if a == "--no-window":
            window = False
        elif a == "--debug":
            debug = True
        else:
            print(f"不认识的参数：{a}", file=sys.stderr)
            return 2
        i += 1
    return run(port, window=window, debug=debug)


if __name__ == "__main__":
    raise SystemExit(main())
