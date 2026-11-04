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
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .serve import app

#: 优先用这个端口；被别的程序占了就换一个空闲的。
PREFERRED_PORT = 8765
HOST = "127.0.0.1"
START_TIMEOUT = 30.0

#: WebView2 运行时可能被装在这几个地方（用户级安装在最下面那个）。
WEBVIEW2_DIRS = (
    r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application",
    r"C:\Program Files\Microsoft\EdgeWebView\Application",
    r"%LOCALAPPDATA%\Microsoft\EdgeWebView\Application",
)
#: 微软官方的 Evergreen 引导包（下完是个小 exe，双击装完就行）。
WEBVIEW2_URL = "https://go.microsoft.com/fwlink/p/?LinkId=2124703"



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


# ---------------------------------------------------------------- 前置自检
# 这两件事在开发机上永远遇不到，到了别人的电脑上就是"双击没反应"和"上传全部 500"。


def webview2_available(dirs=None) -> bool:
    """这台机器上有没有 WebView2 运行时（pywebview 的 edgechromium 后端靠它）。

    Win11 和较新的 Win10 自带；精简系统、长期没更新的 Win10、某些虚拟机里没有。
    没有的话 `webview.start()` 会抛异常，而窗口版没有控制台 —— 用户只会看到"点了没反应"。
    """
    for raw in (dirs if dirs is not None else WEBVIEW2_DIRS):
        base = Path(os.path.expandvars(raw))
        try:
            if not base.is_dir():
                continue
            for child in base.iterdir():
                if child.is_dir() and (child / "msedgewebview2.exe").is_file():
                    return True
        except OSError:
            continue
    return False


def _say(text: str, title: str = "AI Study") -> None:
    """弹一个系统对话框（窗口版没有控制台，print 没人看得见）。非 Windows 就退回 stderr。"""
    try:
        import ctypes

        ctypes.windll.user32.MessageBoxW(None, text, title, 0x00000040)  # MB_ICONINFORMATION
    except Exception:
        print(f"[{title}] {text}", file=sys.stderr)


def ensure_webview2(*, ask=None, open_url=None) -> bool:
    """没有运行时就告诉人怎么办，并可以帮着打开下载页。返回能不能继续开窗。"""
    if webview2_available():
        return True
    opener = open_url or webbrowser.open
    text = (
        "这台电脑缺少「Microsoft Edge WebView2 运行时」，窗口开不出来。\n\n"
        "它是微软的一个小运行时（几 MB），装一次就好：\n"
        f"{WEBVIEW2_URL}\n\n"
        "现在就去下载页吗？下完双击安装，再打开本程序即可。\n"
        "（也可以只跑命令行版：AI Study.exe --no-window）"
    )
    if ask is None:
        _say(text)
        try:
            opener(WEBVIEW2_URL)
        except Exception:
            pass
        return False
    if ask(text):
        try:
            opener(WEBVIEW2_URL)
        except Exception:
            pass
    return False


def _probe_writable(path: Path) -> None:
    """真的写一个文件试试：只靠 `os.access` 在 Windows 上会骗人。"""
    path.mkdir(parents=True, exist_ok=True)
    probe = path / ".write-test"
    probe.write_text("ok", encoding="utf-8")
    probe.unlink()


def ensure_data_dir(start: Path | None = None) -> Path:
    """数据（out/ uploads/ settings.json）放哪。

    优先放自己旁边 —— 整个文件夹拷走就是完整状态。但装在 `C:\\Program Files`、
    只读 U 盘、或者被公司策略锁了目录时写不进去，那就退到用户的本地目录，
    总比"上传报 500"强。返回真正用的目录（必要时已经把工作目录切过去了）。
    """
    start = Path(start or Path.cwd())
    try:
        _probe_writable(start)
        return start
    except OSError:
        pass
    fallback = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "AI Study"
    _probe_writable(fallback)
    os.chdir(fallback)          # serve 里的 out/ uploads/ 都是相对当前目录的
    return fallback



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

    # 客户端里「导出讲义」是一次下载。pywebview 默认把下载直接 cancel（ALLOW_DOWNLOADS=False），
    # 表现是"点了按钮什么都不发生" —— 用户只会以为导不出来。开了之后 WebView2 会弹系统的
    # 另存为对话框，默认目录就是"下载"。
    try:
        webview.settings["ALLOW_DOWNLOADS"] = True
    except Exception:      # 老版本没有这个 settings 字典，那就只能靠浏览器自己接管
        pass

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
    ensure_data_dir()                            # 只读目录里就把数据挪到用户目录去
    if window and not ensure_webview2():
        return 3                                 # 缺 WebView2：已经告诉人怎么办了
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
