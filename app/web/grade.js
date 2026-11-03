/* 判分：只负责"对/不对"，不碰 DOM —— 这样能拿 node 直接测。
 *
 * 题型（app/schema.py 的 QuizItem.type）：
 *   single 单选，answer 是一个字母
 *   multi  多选，answer 是若干字母（"AC" / "A,C" / "A、C" 都认）
 *   fill   填空，answer 是文本；用 / 或 | 分隔可接受的其他写法
 */
(function (root) {
  'use strict';

  // "a,c" / "C A" / "ＡＣ"（全角）都归一成 "AC"；顺序无关，重复字母去掉
  function normLetters(s) {
    const up = String(s == null ? '' : s)
      .replace(/[\uFF21-\uFF3A]/g, c => String.fromCharCode(c.charCodeAt(0) - 0xFEE0))
      .toUpperCase();
    const seen = new Set();
    for (const ch of up) if (ch >= 'A' && ch <= 'Z') seen.add(ch);
    return [...seen].sort().join('');
  }

  // 文本比对前先掏空空白、统一大小写、把全角括号之类压平
  function normText(s) {
    return String(s == null ? '' : s)
      .replace(/\s+/g, '')
      .replace(/[\uFF08\uFF09]/g, c => (c === '\uFF08' ? '(' : ')'))
      .toLowerCase();
  }

  function accepted(answer) {
    return String(answer == null ? '' : answer)
      .split(/[\/|]/)
      .map(normText)
      .filter(Boolean);
  }

  /* q: 题目对象；picked: 选项字母（single/multi）；typed: 填空题输入
   * 返回 {ok, want, reason}；reason = 'empty' 表示没作答、'type' 表示题型不认识 */
  function grade(q, picked, typed) {
    const type = (q && q.type) || 'single';
    const answer = q ? q.answer : '';
    if (type === 'fill') {
      const want = String(answer == null ? '' : answer).trim();
      const got = normText(typed);
      if (!got) return { ok: false, want: want, reason: 'empty' };
      return { ok: accepted(answer).indexOf(got) >= 0, want: want, reason: '' };
    }
    const want = normLetters(answer);
    const got = normLetters(picked);
    if (!got) return { ok: false, want: want, reason: 'empty' };
    return { ok: got === want, want: want, reason: '' };
  }

  const api = { normLetters: normLetters, normText: normText, grade: grade };
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.Grade = api;
})(typeof window !== 'undefined' ? window : globalThis);
