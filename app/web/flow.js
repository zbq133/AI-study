/* 前端流程里的纯逻辑：不碰 DOM，node 能直接 require 来测（tests/test_web.py）。
 * 浏览器里是 window.Flow，node 里 require 出来是同一份实现 —— 跟 grade.js 一个套路。
 */
(function (root) {
  'use strict';

  /* 章节的键用解析结果里的 n（build_sections 的 1 基序号，跟后端
   * select_chapter(index=) 对得上）。标题不能当键：两章各有一个「小结」时，
   * 按标题找永远只找得到第一个，生成出来的也永远是第一个。 */
  function findSection(sections, key) {
    const n = Number(key);
    if (!Number.isFinite(n)) return null;
    for (const s of sections || []) if (s.n === n) return s;
    return null;
  }

  /** 按历史记录里的 chapter 文本找回对应章节：支持 `路径 > 小节#n`、`路径 > 小节` 与纯标题。 */
  function matchSection(sections, chapter) {
    const ch = String(chapter || '').trim();
    const list = sections || [];
    if (!ch || !list.length) return null;
    const hash = ch.match(/^(.*)#(\d+)$/);
    if (hash) {
      const byIdx = findSection(list, Number(hash[2]));
      if (byIdx && ((byIdx.path || []).join(' > ') === hash[1] || byIdx.title === hash[1])) {
        return byIdx;
      }
    }
    for (const s of list) if ((s.path || []).join(' > ') === ch) return s;
    for (const s of list) if (s.title === ch) return s;
    return null;
  }

  /** POST /api/lessons 的请求体。带上 section_n，同名章节才分得开。 */
  function lessonRequest(docId, section, nQuiz) {
    const body = { doc_id: docId, chapter: section.title, n_quiz: Number(nQuiz) || 3 };
    if (Number.isInteger(section.n)) body.section_n = section.n;
    return body;
  }

  /* 错误响应不一定是 JSON（反代的 502 是一页 HTML，连接被掐是空串）。
   * 直接 r.json() 会再抛一次，把真正的错误盖掉，按钮也跟着卡死。 */
  function errorText(status, bodyText) {
    try {
      const d = JSON.parse(bodyText).detail;
      if (typeof d === 'string' && d) return d;
      if (d) return JSON.stringify(d);
    } catch (e) { /* 不是 JSON，走下面的兜底 */ }
    return '请求失败（HTTP ' + status + '）';
  }

  const GONE = '这个任务已经不在了（服务可能重启过），请重新操作一次。';
  const UNREACHABLE = '连不上本地服务，任务结果未知。确认服务还在跑，再刷新页面。';

  /* SSE 断了不等于任务没了（睡眠唤醒、代理掐长连接都会断）。改成轮询把结局取回来；
   * 只有真连不上、或者任务确实不存在了，才判失败 —— 总之一定会给出一个结局，
   * 调用方靠这个结局把按钮解开。
   *   getJob(id) -> Promise<{ok, status, snap}>，网络错误可以直接 reject。 */
  async function pollJob(getJob, jobId, opts) {
    const o = Object.assign({
      interval: 500,
      maxMisses: 20,
      onSnap: null,
      sleep: function (ms) { return new Promise(function (r) { setTimeout(r, ms); }); },
    }, opts || {});
    let misses = 0;
    for (;;) {
      let res = null;
      try { res = await getJob(jobId); } catch (e) { res = null; }
      if (res && res.status === 404) return { state: 'failed', error: GONE };
      if (res && res.ok && res.snap) {
        misses = 0;
        const s = res.snap;
        if (o.onSnap) o.onSnap(s);
        if (s.state === 'done' || s.state === 'failed') return s;
      } else if (++misses >= o.maxMisses) {
        return { state: 'failed', error: UNREACHABLE };
      }
      await o.sleep(o.interval);
    }
  }

  /* 只认最后一次：连点两条历史，后点的先回来、先点的后回来，
   * 不拦一下就会把先点的那条盖到屏幕上。 */
  function latest() {
    let cur = 0;
    return {
      next: function () { cur += 1; return cur; },
      isCurrent: function (t) { return t === cur; },
    };
  }

  /* 一页里要高亮的东西。bbox 是 pt，按原页尺寸换成百分比贴到页面图上；
   * 文字块（图片拿不到时的兜底视图）不管有没有 bbox 都要标出来，
   * 否则老讲义 / 图片挂了的时候点出处什么也看不到。 */
  function highlightTargets(page, ids, w, h) {
    const want = new Set(ids || []);
    const hits = ((page && page.blocks) || []).filter(function (b) { return want.has(b.id); });
    const W = Number(w) || 595, H = Number(h) || 842;
    return {
      blocks: hits.map(function (b) { return b.id; }),
      boxes: hits.filter(function (b) { return b.bbox && b.bbox.length === 4; }).map(function (b) {
        const x0 = b.bbox[0], y0 = b.bbox[1], x1 = b.bbox[2], y1 = b.bbox[3];
        return {
          left: (x0 / W) * 100, top: (y0 / H) * 100,
          width: ((x1 - x0) / W) * 100, height: ((y1 - y0) / H) * 100,
        };
      }),
    };
  }

  /** 统计当前页所有习题的作答进度与正确数（纯计算，不碰 DOM）。 */
  function quizStats(rows) {
    const list = rows || [];
    let answered = 0;
    let right = 0;
    for (const r of list) {
      if (r && r.answered) {
        answered += 1;
        if (r.ok) right += 1;
      }
    }
    return { total: list.length, answered: answered, right: right };
  }

  const api = {
    findSection: findSection,
    matchSection: matchSection,
    lessonRequest: lessonRequest,
    errorText: errorText,
    pollJob: pollJob,
    latest: latest,
    highlightTargets: highlightTargets,
    quizStats: quizStats,
    GONE: GONE,
    UNREACHABLE: UNREACHABLE,
  };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Flow = api;
})(typeof window !== 'undefined' ? window : globalThis);
