"""客户端入口：`python run_desktop.py`，也是 PyInstaller 打包时的脚本。

真正的逻辑在 `app/desktop.py`，这里只留一个入口，免得打包指向包内部模块。

打包成窗口版之后控制台没有了，崩起来会是一句"Unhandled exception in script"就没了下文，
所以这里兜一层：把完整 traceback 写到 exe 旁边的 `crash.log`，用户能把它贴给你。
"""

import sys
import traceback
from pathlib import Path


def _log_path() -> Path:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
    return base / "crash.log"


def main() -> int:
    try:
        from app.desktop import main as run

        return run()
    except BaseException:
        text = traceback.format_exc()
        try:
            _log_path().write_text(text, encoding="utf-8")
        except OSError:
            pass
        sys.stderr.write(text)
        raise


if __name__ == "__main__":
    raise SystemExit(main())
