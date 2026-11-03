"""前端判分逻辑的测试。

判分规则放在 `app/web/grade.js` 这个纯函数模块里，就是为了能拿 node 直接跑：
浏览器里它是 `window.Grade`，node 里 `require` 出来是同一份实现。这样"多选顺序
无关""全角字母也认""填空比之前先掏空白"这些规则有真断言，不是靠手点。
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GRADE = ROOT / "app" / "web" / "grade.js"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="本机没有 node，跳过前端判分测试")


def _grade(expr: str):
    """在 node 里跑一段以 g 为模块的表达式，返回它的 JSON。"""
    js = ("const g = require(" + json.dumps(str(GRADE).replace("\\", "/")) + ");"
          "process.stdout.write(JSON.stringify(" + expr + "));")
    p = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


def test_grade_marks_the_right_letter():
    assert _grade("g.grade({type:'single',answer:'B'},'B','')")["ok"] is True
    assert _grade("g.grade({type:'single',answer:'B'},'C','')")["ok"] is False


def test_grade_reads_multi_choice_regardless_of_order():
    """"AC" 和 "CA" 是同一次作答；少选一个不算对。"""
    assert _grade("g.grade({type:'multi',answer:'A,C'},'CA','')")["ok"] is True
    assert _grade("g.grade({type:'multi',answer:'AC'},'A','')")["ok"] is False
    assert _grade("g.grade({type:'multi',answer:'AC'},'ACD','')")["ok"] is False


def test_grade_accepts_full_width_letters():
    """中文输入法下按出来的 Ａ Ｃ 也算 A C。"""
    assert _grade("g.grade({type:'multi',answer:'AC'},'\\uFF21\\uFF23','')")["ok"] is True


def test_grade_treats_no_answer_as_empty_not_wrong():
    res = _grade("g.grade({type:'single',answer:'B'},'','')")
    assert res["ok"] is False and res["reason"] == "empty" and res["want"] == "B"


def test_grade_compares_fill_answers_without_spaces():
    assert _grade("g.grade({type:'fill',answer:'E=mc^2'},'','e = mc^2')")["ok"] is True
    assert _grade("g.grade({type:'fill',answer:'E=mc^2'},'','mc^2')")["ok"] is False


def test_grade_accepts_any_listed_fill_variant():
    """参考答案里用 / 或 | 隔开的写法，命中任意一个都算对。"""
    assert _grade("g.grade({type:'fill',answer:'牛顿|Newton'},'','newton')")["ok"] is True


def test_grade_fill_with_nothing_typed_is_empty():
    res = _grade("g.grade({type:'fill',answer:'42'},'','   ')")
    assert res["reason"] == "empty"
