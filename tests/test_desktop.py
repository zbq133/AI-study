"""客户端外壳：端口选择、复用已在跑的服务、窗口参数。

窗口本身没法在测试里开（要真图形环境），所以这里只测"开窗之前"那部分逻辑，
外加用假 webview 模块检查传给它的参数。
"""
import http.server
import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

from app import desktop, serve


def _serve_health(port, payload=b'{"jobs": {}, "llm": {}}'):
    """起一个只回 /api/health 的假服务，用来测"这个端口上是不是本程序"。"""

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):                     # noqa: N802
            body = payload if self.path == "/api/health" else b"nope"
            self.send_response(200 if self.path == "/api/health" else 404)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a):            # 别把测试日志刷到屏幕上
            pass

    srv = http.server.HTTPServer(("127.0.0.1", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def test_free_port_is_actually_free():
    port = desktop.free_port()
    assert 1024 < port < 65536
    assert not desktop.port_open(port)


def test_port_open_sees_a_listening_socket():
    srv = _serve_health(desktop.free_port())
    try:
        assert desktop.port_open(srv.server_address[1])
    finally:
        srv.shutdown()
        srv.server_close()          # 只 shutdown 不 close，监听套接字还占着端口
    assert not desktop.port_open(srv.server_address[1], timeout=0.2)


def test_health_returns_the_payload_only_for_this_app():
    port = desktop.free_port()
    srv = _serve_health(port)
    try:
        assert desktop.health(port) == {"jobs": {}, "llm": {}}
    finally:
        srv.shutdown()


def test_health_rejects_a_port_that_is_not_us():
    port = desktop.free_port()
    srv = _serve_health(port, payload=b'{"hello": "world"}')
    try:
        assert desktop.health(port) is None           # 少了 jobs/llm 就当不是本程序
    finally:
        srv.shutdown()
    assert desktop.health(port) is None               # 端口空着也不能炸


def test_choose_port_reuses_a_service_that_is_already_running():
    """再双击一次不该起第二份服务 —— 两份会抢同一个 out/index.json。"""
    port = desktop.free_port()
    srv = _serve_health(port)
    try:
        assert desktop.choose_port(port) == (port, False)
    finally:
        srv.shutdown()


def test_choose_port_moves_away_from_a_foreign_service():
    """端口被别的程序占着（回的东西不是我们）→ 换一个端口，并且自己起服务。"""
    port = desktop.free_port()
    srv = _serve_health(port, payload=b'{"other": 1}')
    try:
        picked, needs_own = desktop.choose_port(port)
    finally:
        srv.shutdown()
    assert needs_own is True
    assert picked != port
    assert not desktop.port_open(picked)


def test_service_comes_up_and_goes_down():
    port = desktop.free_port()
    svc = desktop.Service(port)
    svc.start()
    try:
        assert desktop.wait_ready(port, timeout=20), "服务没起来"
        assert desktop.health(port)["jobs"] is not None
    finally:
        svc.stop()
    assert not desktop.port_open(port, timeout=0.5), "关了之后端口还开着"


def test_open_window_passes_the_right_arguments(monkeypatch):
    calls = {}
    fake = types.SimpleNamespace()
    fake.settings = {"ALLOW_DOWNLOADS": False}

    def create_window(title, url, **kw):
        calls["window"] = dict(title=title, url=url, **kw)

    def start(**kw):
        calls["start"] = kw

    fake.create_window = create_window
    fake.start = start
    monkeypatch.setitem(sys.modules, "webview", fake)

    desktop.open_window("http://127.0.0.1:1234/")
    assert calls["window"]["url"] == "http://127.0.0.1:1234/"
    assert calls["window"]["title"] == "AI Study"
    assert calls["window"]["text_select"] is True      # 讲义要能选中复制
    assert calls["start"]["private_mode"] is False     # 关掉隐私模式，主题/字号才留得住
    assert calls["start"]["storage_path"].endswith("webview")
    assert calls["start"]["gui"] == "edgechromium"
    # 默认是 False：客户端里点「导出讲义」会被静默 cancel，看着像按钮坏了
    assert fake.settings["ALLOW_DOWNLOADS"] is True


def test_run_starts_a_service_and_opens_a_window(monkeypatch):
    opened = []
    monkeypatch.setattr(desktop, "choose_port", lambda p: (desktop.free_port(), True))
    monkeypatch.setattr(desktop, "wait_ready", lambda port, timeout=None: True)
    monkeypatch.setattr(desktop, "open_window", lambda url, debug=False: opened.append(url))
    assert desktop.run(0, window=True) == 0
    assert opened and opened[0].startswith("http://127.0.0.1:")


def test_webview2_requirement_only_gates_windows(monkeypatch):
    """WebView2 是 Windows 独有的：别的平台不该被"找不到 Edge 运行时"拦下。"""
    # 先假装这台机器没装 WebView2，否则 Windows 上跑测试会因为"真的有"而永远通过
    monkeypatch.setattr(desktop, "webview2_available", lambda dirs=None: False)

    monkeypatch.setattr(desktop.sys, "platform", "linux")
    assert desktop.ensure_webview2(ask=lambda text: False) is True

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop.ensure_webview2(ask=lambda text: False) is False


def test_run_reports_a_service_that_never_comes_up(monkeypatch):
    monkeypatch.setattr(desktop, "choose_port", lambda p: (desktop.free_port(), True))
    monkeypatch.setattr(desktop, "wait_ready", lambda port, timeout=None: False)
    monkeypatch.setattr(desktop, "open_window", lambda url, debug=False: pytest.fail("不该开窗"))
    assert desktop.run(0) == 1


def test_service_survives_a_windowed_exe_without_console(monkeypatch):
    """窗口版 exe 里 sys.stdout 是 None：uvicorn 的 formatter 会当场炸，必须兜住。"""
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    desktop._ensure_streams()
    assert sys.stdout is not None and sys.stderr is not None    # 至少 isatty() 调得动了
    assert "nul" in getattr(sys.stdout, "name", "") or sys.stdout.name == os.devnull
    port = desktop.free_port()
    svc = desktop.Service(port)          # 这一步原来会 AttributeError
    svc.start()
    try:
        assert desktop.wait_ready(port, timeout=20)
    finally:
        svc.stop()


def test_main_parses_the_flags(monkeypatch, capsys):
    seen = {}
    monkeypatch.setattr(desktop, "run", lambda port=None, **kw: seen.update(port=port, **kw) or 0)
    assert desktop.main(["--port", "8811", "--no-window"]) == 0
    assert seen == {"port": 8811, "window": False, "debug": False}
    assert desktop.main(["--help"]) == 0
    assert "python -m app.desktop" in capsys.readouterr().out
    assert desktop.main(["--port", "abc"]) == 2
    assert "端口要写数字" in capsys.readouterr().err
    assert desktop.main(["--nope"]) == 2
    assert "不认识的参数" in capsys.readouterr().err


# ---------------------------------------------------------------- 别人的电脑上

def test_webview2_is_found_by_looking_for_the_runtime_exe(tmp_path, monkeypatch):
    """目录里得有 msedgewebview2.exe 才算装了运行时，空目录不算。"""
    empty = tmp_path / "empty"
    empty.mkdir()
    assert desktop.webview2_available([str(empty)]) is False

    good = tmp_path / "EdgeWebView" / "Application"
    (good / "120.0.1.0").mkdir(parents=True)
    (good / "120.0.1.0" / "msedgewebview2.exe").write_text("x", encoding="utf-8")
    assert desktop.webview2_available([str(good)]) is True
    assert desktop.webview2_available([str(empty), str(good)]) is True

    # 有目录但没那个 exe（装到一半、或者别的软件留下的空壳）不算装了
    hollow = tmp_path / "hollow" / "Application"
    (hollow / "120.0.0.0").mkdir(parents=True)
    assert desktop.webview2_available([str(hollow)]) is False


def test_missing_webview2_tells_the_user_what_to_do(monkeypatch):
    """缺运行时不能"双击没反应"：要弹话，还要能帮着打开下载页。"""
    monkeypatch.setattr(desktop.sys, "platform", "win32")   # WebView2 只关 Windows 的事
    monkeypatch.setattr(desktop, "webview2_available", lambda dirs=None: False)
    said, opened = [], []
    ok = desktop.ensure_webview2(ask=lambda text: said.append(text) or True,
                                 open_url=opened.append)
    assert ok is False                       # 这次开不了窗
    assert said and "WebView2" in said[0] and "--no-window" in said[0]
    assert opened == [desktop.WEBVIEW2_URL]


def test_webview2_present_means_no_nagging(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "webview2_available", lambda dirs=None: True)
    assert desktop.ensure_webview2(ask=lambda t: pytest.fail("不该弹"), open_url=None) is True


def test_run_refuses_to_open_a_window_without_webview2(monkeypatch):
    monkeypatch.setattr(desktop, "ensure_data_dir", lambda *a, **k: None)
    monkeypatch.setattr(desktop, "ensure_webview2", lambda: False)
    monkeypatch.setattr(desktop, "open_window", lambda *a, **k: pytest.fail("不该开窗"))
    monkeypatch.setattr(desktop, "choose_port", lambda p: (desktop.free_port(), False))
    assert desktop.run(0, window=True) == 3   # 退出码 3 = 缺运行时，且没去碰窗口


def test_no_window_mode_does_not_need_webview2(monkeypatch):
    """--no-window 是服务器模式（调试/当后台服务用），跟 WebView2 无关，别在这里拦。"""
    monkeypatch.setattr(desktop, "ensure_data_dir", lambda *a, **k: None)
    monkeypatch.setattr(desktop, "ensure_webview2",
                        lambda: pytest.fail("服务器模式不该查 WebView2"))
    monkeypatch.setattr(desktop, "choose_port", lambda p: (desktop.free_port(), False))
    monkeypatch.setattr(desktop, "open_window", lambda *a, **k: pytest.fail("--no-window 不该开窗"))

    def ctrl_c(_seconds):                    # 那个等 Ctrl-C 的死循环，直接从 sleep 里退出
        raise KeyboardInterrupt

    monkeypatch.setattr(desktop.time, "sleep", ctrl_c)
    assert desktop.run(0, window=False) == 0


def test_data_dir_falls_back_when_it_cannot_write(tmp_path, monkeypatch):
    """装在 Program Files 里写不进去：数据要挪到用户目录，不能让上传全部 500。"""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    bad = tmp_path / "readonly"
    real = desktop._probe_writable

    def probe(path):
        if Path(path) == bad:
            raise PermissionError("denied")
        real(path)

    monkeypatch.setattr(desktop, "_probe_writable", probe)
    before = os.getcwd()
    try:
        where = desktop.ensure_data_dir(bad)
        assert where == tmp_path / "local" / "AI Study"
        assert os.getcwd() == str(where)      # out/ uploads/ 是相对当前目录的
    finally:
        os.chdir(before)


def test_data_dir_stays_put_when_it_is_writable(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    before = os.getcwd()
    try:
        assert desktop.ensure_data_dir(tmp_path) == tmp_path
        assert os.getcwd() == before          # 能写就别乱切目录
    finally:
        os.chdir(before)


def test_the_exe_carries_a_version_resource():
    """打包出来的 exe 得看得出名字和版本（SmartScreen 和杀软对"三无"包更凶）。"""
    from pathlib import Path

    info = Path("version_info.txt").read_text(encoding="utf-8")
    assert "AI Study" in info
    assert "filevers=(1, 1, 0, 0)" in info and "prodvers=(1, 1, 0, 0)" in info
    assert "'FileVersion', '1.1.0.0'" in info and "'ProductVersion', '1.1.0.0'" in info
    build = Path("build_desktop.ps1").read_text(encoding="utf-8-sig")
    assert "--version-file" in build and "version_info.txt" in build

