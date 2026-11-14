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


# ---------------- flow.js：前端流程里的纯逻辑 ----------------

FLOW = ROOT / "app" / "web" / "flow.js"


def _flow(expr: str):
    """在 node 里跑一段以 f 为模块的表达式（可以是 Promise），返回它的 JSON。"""
    js = ("const f = require(" + json.dumps(str(FLOW).replace("\\", "/")) + ");"
          "Promise.resolve((async () => (" + expr + "))())"
          ".then(v => process.stdout.write(JSON.stringify(v === undefined ? null : v)));")
    p = subprocess.run([NODE, "-e", js], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout)


DUP = ("[{n:2,title:'小结',path:['第1章','小结']},"
       "{n:4,title:'小结',path:['第2章','小结']}]")


def test_find_section_tells_duplicate_titles_apart_by_n():
    """两章各有一个「小结」：按序号找，第二个也找得到（按标题永远只命中第一个）。"""
    assert _flow(f"f.findSection({DUP}, '4').path")[1:] == ["小结"]
    assert _flow(f"f.findSection({DUP}, '4').path")[0] == "第2章"
    assert _flow(f"f.findSection({DUP}, 2).path")[0] == "第1章"
    assert _flow(f"f.findSection({DUP}, '3')") is None
    assert _flow(f"f.findSection({DUP}, '小结')") is None


def test_lesson_request_carries_section_n():
    body = _flow("f.lessonRequest('d1', {n:4,title:'小结'}, '2')")
    assert body == {"doc_id": "d1", "chapter": "小结", "n_quiz": 2, "section_n": 4}
    # 题数没填（空串）按 3；没有序号的老数据就别带 section_n，后端按标题找
    assert _flow("f.lessonRequest('d1', {title:'绪论'}, '')") == {
        "doc_id": "d1", "chapter": "绪论", "n_quiz": 3}


def test_error_text_survives_non_json_bodies():
    """反代的 502 是 HTML、连接被掐是空串：都得给出一句人话，而不是再抛一次。"""
    assert _flow("f.errorText(400, JSON.stringify({detail:'只接受 .pdf 文件'}))") == "只接受 .pdf 文件"
    assert _flow("f.errorText(502, '<html>Bad Gateway</html>')") == "请求失败（HTTP 502）"
    assert _flow("f.errorText(500, '')") == "请求失败（HTTP 500）"
    assert "loc" in _flow("f.errorText(422, JSON.stringify({detail:[{loc:['body']}]}))")


FAKE_JOB = """
(() => {
  const seq = %s;
  let i = 0;
  const seen = [];
  const getJob = async () => {
    const step = seq[Math.min(i, seq.length - 1)]; i += 1;
    if (step === 'throw') throw new TypeError('Failed to fetch');
    if (step === 404) return { ok: false, status: 404, snap: null };
    return { ok: true, status: 200, snap: { state: step } };
  };
  return f.pollJob(getJob, 'j1', { interval: 0, maxMisses: %d, sleep: async () => {},
                                   onSnap: s => seen.push(s.state) })
    .then(end => ({ end, seen, calls: i }));
})()
"""


def test_poll_job_rides_out_blips_and_returns_the_real_ending():
    """SSE 断了改轮询：中间两次网络抖动不算失败，一直等到真正的结局。"""
    out = _flow(FAKE_JOB % ("['throw','throw','running','done']", 5))
    assert out["end"] == {"state": "done"}
    assert out["seen"] == ["running", "done"]
    assert out["calls"] == 4


def test_poll_job_stops_at_a_failed_ending():
    """任务自己失败了也是结局：当场返回，别接着等（不然失败原因永远出不来）。"""
    out = _flow(FAKE_JOB % ("['failed','done']", 5))
    assert out["end"] == {"state": "failed"}
    assert out["calls"] == 1


def test_poll_job_gives_up_with_an_ending_when_server_is_gone():
    """连不上就判失败 —— 关键是一定有结局，按钮才解得开。"""
    out = _flow(FAKE_JOB % ("['throw']", 3))
    assert out["end"]["state"] == "failed"
    assert out["end"]["error"] == _flow("f.UNREACHABLE")
    assert out["calls"] == 3


def test_poll_job_treats_404_as_gone_not_as_a_blip():
    out = _flow(FAKE_JOB % ("['running', 404]", 50))
    assert out["end"] == {"state": "failed", "error": _flow("f.GONE")}
    assert out["calls"] == 2


def test_latest_only_trusts_the_last_click():
    assert _flow("(() => { const l = f.latest(); const a = l.next(); const b = l.next();"
                 " return [l.isCurrent(a), l.isCurrent(b)]; })()") == [False, True]


def test_highlight_targets_marks_blocks_even_without_bbox():
    """图片挂了时看的是文字块：没有 bbox 的老块也得标出来；矩形按页尺寸换成百分比。"""
    t = _flow("f.highlightTargets({blocks:["
              "{id:'a',bbox:[100,200,300,242]},{id:'b'},{id:'c',bbox:[0,0,1,1]}]},"
              " ['a','b'], '400', '800')")
    assert t["blocks"] == ["a", "b"]
    assert t["boxes"] == [{"left": 25, "top": 25, "width": 50, "height": 5.25}]


def test_match_section_restores_chapter_by_path_index_or_title():
    """打开旧讲义时按 record.chapter 同步目录选中项，别总停在第一章。"""
    assert _flow(f"f.matchSection({DUP}, '第2章 > 小结').n") == 4
    dup_same = ("[{n:2,title:'小结',path:['第1章','小结']},"
                "{n:5,title:'小结',path:['第1章','小结']}]")
    assert _flow(f"f.matchSection({dup_same}, '第1章 > 小结#5').n") == 5
    assert _flow(f"f.matchSection({DUP}, '小结').n") == 2
    assert _flow(f"f.matchSection({DUP}, '不存在')") is None


def test_quiz_stats_counts_answered_and_correct():
    assert _flow("f.quizStats([])") == {"total": 0, "answered": 0, "right": 0}
    assert _flow("f.quizStats([{answered:true,ok:true},{answered:true,ok:false},{answered:false,ok:false}])") == {
        "total": 3, "answered": 2, "right": 1,
    }


def test_split_ratio_clamps_drag_bounds_and_falls_back_on_narrow_screen():
    # 默认讲义略宽于原文栏（约 61% : 39%），主工作区太窄或非法输入回退默认值
    d = _flow("f.splitCss(null)")
    assert d["lessonFr"] == "1.38fr" and d["srcFr"] == "0.88fr"
    assert _flow("f.splitCss('bad')") == d
    assert _flow("f.splitRatio(500, {left:0, width:500}, 264)") == d

    # 拖拽中间位置、直接传入占空比、以及拖出左右边界时的安全钳制（0.38..0.74）
    mid = _flow("f.splitRatio(864, {left:0, width:1264}, 264)")
    assert mid == {"lessonFr": "0.600fr", "srcFr": "0.400fr", "ratio": 0.6}
    assert _flow("f.splitCss(0.1)")["ratio"] == 0.38
    assert _flow("f.splitCss(0.95)")["ratio"] == 0.74
    assert _flow("f.splitRatio(100, {left:0, width:1264}, 264)")["ratio"] == 0.38
    assert _flow("f.splitRatio(1300, {left:0, width:1264}, 264)")["ratio"] == 0.74


