const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s)
  .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let docId = null;
let layout = null;      // {pages:[{page, blocks:[{id,kind,text,level}]}]}
let section = null;     // 当前选中的章节（来自解析结果）
let sections = [];      // 解析结果原样存着 —— 页码从这里取，别去抠 DOM 上的文案
let lesson = null;      // 生成出来的讲义

/* ---------------- 极简 Markdown：段落 / 标题 / 列表 / 粗体 / 行内代码 ------------- */
/* 只渲染讲义里实际会出现的语法。宁可少认，也别把 $x^2$ 当 HTML 吃掉。 */

function inline(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code>$1</code>');
}

function md(text) {
  const out = [];
  let para = [];
  let lis = [];
  const flushP = () => { if (para.length) { out.push('<p>' + inline(para.join(' ')) + '</p>'); para = []; } };
  const flushL = () => { if (lis.length) { out.push('<ul>' + lis.map(x => '<li>' + inline(x) + '</li>').join('') + '</ul>'); lis = []; } };
  for (const raw of String(text || '').split('\n')) {
    const line = raw.replace(/\s+$/, '');
    if (!line.trim()) { flushP(); flushL(); continue; }
    const h = line.match(/^#{1,6}\s+(.*)$/);
    const li = line.match(/^\s*[-*]\s+(.*)$/);
    if (h) { flushP(); flushL(); out.push('<h4>' + inline(h[1]) + '</h4>'); }
    else if (li) { flushP(); lis.push(li[1]); }
    else { flushL(); para.push(line.trim()); }
  }
  flushP(); flushL();
  return out.join('');
}

/* ---------------- 公式：交给本地 KaTeX ---------------- */
/* 只处理 $…$ / $$…$$ / \(…\) / \[…\]，其它语法一概不认；出错就原样留着，
   宁可看见一行 LaTeX 源码，也不能因为一个公式把整段讲义的渲染搞崩。 */

function paintMath(el) {
  if (!el || typeof renderMathInElement !== 'function') return;
  try {
    renderMathInElement(el, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '\\[', right: '\\]', display: true },
        { left: '$', right: '$', display: false },
        { left: '\\(', right: '\\)', display: false }
      ],
      throwOnError: false,
      errorColor: '#d08a8a',
      ignoredTags: ['script', 'noscript', 'style', 'textarea', 'pre', 'code']
    });
  } catch (e) { console.warn('公式渲染失败', e); }
}

/* ---------------- 出处 chip ---------------- */

function chips(refs) {
  if (!refs || !refs.length) return '';
  return refs.map(r =>
    `<button class="ref" data-page="${r.page}" data-ids="${esc((r.blocks || []).join(','))}"` +
    ` title="${esc(r.quote)}">原文 p${r.page}</button>`).join('');
}

/* ---------------- 进度 ---------------- */

/** 失败要说人话：title 一句话 + hint 怎么办；细节只在认不出来时露出来。 */
function fail(info, fallback) {
  const box = $('#err');
  box.textContent = '';
  if (!info || !info.title) { box.textContent = fallback || '出错了'; return; }
  const t = document.createElement('b');
  t.textContent = info.title;
  box.appendChild(t);
  if (info.hint) {
    const h = document.createElement('span');
    h.className = 'hintline';
    h.textContent = info.hint;
    box.appendChild(h);
  }
  if (info.detail && info.kind === 'unknown') {
    const d = document.createElement('code');
    d.textContent = info.detail;
    box.appendChild(d);
  }
}

function stageView(bar, stageEl, detailEl, snap) {
  bar.style.width = (snap.percent || 0) + '%';
  stageEl.textContent = snap.stage + (snap.state === 'running' ? '…' : '');
  detailEl.textContent = [snap.message, snap.calls ? `模型调用 ${snap.calls} 次` : '',
                          snap.elapsed ? `${snap.elapsed}s` : '']
                          .filter(Boolean).join(' · ');
}

function watch(jobId, onDone) {
  const stop = $('#stop');
  stop.classList.remove('hide');
  stop.disabled = false;
  stop.textContent = '停';
  stop.onclick = async () => {
    stop.disabled = true;
    stop.textContent = '停…';
    try { await fetch(`/api/jobs/${jobId}/cancel`, { method: 'POST' }); } catch (e) { /* 已经结束了 */ }
  };
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = ev => {
    const snap = JSON.parse(ev.data);
    onDone.snap(snap);
    if (snap.state === 'done' || snap.state === 'failed') {
      es.close(); stop.classList.add('hide'); onDone.end(snap);
    }
  };
  es.onerror = () => { es.close(); stop.classList.add('hide'); };
}

/* ---------------- 上传与解析 ---------------- */

$('#up').onclick = async () => {
  const f = $('#file').files[0];
  if (!f) { $('#err').textContent = '先选一个 PDF。'; return; }
  $('#up').disabled = true; $('#go').disabled = true;
  $('#err').textContent = ''; $('#pBarWrap').classList.remove('hide');
  $('#runLine').classList.remove('hide');
  $('#lessonBody').innerHTML = '<p class="hint">解析中…</p>';
  const fd = new FormData(); fd.append('file', f);
  try {
    const r = await fetch('/api/docs', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const up = await r.json();
    docId = up.doc_id;
    $('#upMeta').textContent = `${up.filename} · ${(up.bytes / 1048576).toFixed(1)} MB`;
    const p = await fetch(`/api/docs/${docId}/parse`, { method: 'POST' });
    const pj = await p.json();
    if (!pj.ok && pj.detail) throw new Error(pj.detail);
    if (pj.cached) { await afterParse(pj.result); return; }
    watch(pj.job_id, {
      snap: s => stageView($('#pBar'), $('#stage'), $('#detail'), s),
      end: s => {
        if (s.state === 'failed') {
          fail(s.error_info, s.error); $('#up').disabled = false;
          $('#lessonBody').innerHTML = '<p class="hint">解析失败。</p>';
          return;
        }
        afterParse(s.result);
      }
    });
  } catch (e) {
    $('#err').textContent = String(e.message || e);
    $('#up').disabled = false;
  }
};

async function afterParse(res) {
  renderSections(res);
  await loadLayout();
  $('#up').disabled = false;
  $('#go').disabled = false;
  $('#lessonBody').innerHTML = '<p class="hint">选好章节和题数，点「生成讲义」。' +
    `这份课件 ${res.pages} 页、${res.blocks} 块${res.ocr_pages.length ? '（其中 ' + res.ocr_pages.length + ' 页走 OCR）' : ''}。` +
    '</p>';
}

async function loadLayout() {
  _srcSpec = null;               // 换了一份 PDF，上一次的页清单作废
  await renderSource();
}

/* ---------------- 章节列表 ---------------- */

function renderSections(res) {
  sections = res.sections || [];      // 页码等元数据留在这里，别从 option 文案里正则抠
  const sel = $('#sec');
  sel.innerHTML = '';
  const toc = $('#tocBody');
  toc.innerHTML = '';
  for (const s of sections) {
    const o = document.createElement('option');
    o.value = s.title;
    o.textContent = `${String(s.n).padStart(2, ' ')}. ${s.path.join(' > ')} · p${s.page_start}-${s.page_end} · ${s.chars} 字`;
    sel.appendChild(o);

    const b = document.createElement('button');
    b.className = 'toc-item';
    b.dataset.title = s.title;
    b.innerHTML = `${esc(s.title)}<br><span class="pg">p${s.page_start}-${s.page_end} · ${s.chars} 字</span>`;
    toc.appendChild(b);
  }
  sel.classList.remove('hide');
  section = sections[0] || null;
  if (res.hidden) $('#goMeta').textContent = `另有 ${res.hidden} 条参考文献段已隐藏`;
  markToc();
  renderSource();
}

function byTitle(title) {
  return sections.find(s => s.title === title) || { title };
}

function markToc() {
  document.querySelectorAll('.toc-item').forEach(b =>
    b.classList.toggle('on', section && b.dataset.title === section.title));
}

$('#tocBody').addEventListener('click', e => {
  const b = e.target.closest('.toc-item');
  if (!b) return;
  $('#sec').value = b.dataset.title;
  section = byTitle(b.dataset.title);
  markToc();
  renderSource();
});

$('#sec').addEventListener('change', () => {
  section = byTitle($('#sec').value);
  markToc();
  renderSource();
});

/* ---------------- 生成 ---------------- */

$('#go').onclick = async () => {
  if (!docId) return;
  $('#go').disabled = true; $('#up').disabled = true;
  $('#err').textContent = ''; $('#runLine').classList.remove('hide');
  $('#pBarWrap').classList.remove('hide');
  const body = JSON.stringify({ doc_id: docId, chapter: $('#sec').value,
                                n_quiz: Number($('#nq').value) || 3 });
  const r = await fetch('/api/lessons', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body });
  if (!r.ok) {
    $('#err').textContent = (await r.json()).detail || r.status;
    $('#go').disabled = false; $('#up').disabled = false; return;
  }
  const { job_id } = await r.json();
  watch(job_id, {
    snap: s => {
      stageView($('#pBar'), $('#stage'), $('#detail'), s);
      $('#goMeta').textContent = s.elapsed ? `${s.elapsed}s` : '';
    },
    end: s => {
      $('#go').disabled = false; $('#up').disabled = false;
      if (s.state === 'failed') { fail(s.error_info, s.error); return; }
      lesson = s.result.lesson || {};
      $('#goMeta').textContent = `${s.result.sections || 0} 节 · ${s.result.quiz || 0} 题 · ${s.elapsed}s`;
      if (s.result.reasoning_note) $('#err').textContent = s.result.reasoning_note;
      renderLesson(lesson);
      renderSource();
      loadHistory();            // 新的排到历史最前面
    }
  });
};

/* ---------------- 历史：重启之后还认得的那些讲义 ---------------- */

let histRows = [];          // 最近一次的列表快照（管理模式下要按 id 找回来）
let histMode = false;       // 管理模式：行变成可勾选，点行是"选"而不是"打开"
const picked = new Set();

function kb(n) {
  if (!n) return '0 KB';
  const m = n / 1024 / 1024;
  return m >= 1 ? m.toFixed(1) + ' MB' : Math.max(1, Math.round(n / 1024)) + ' KB';
}

function paintHistBar() {
  $('#histAll').checked = histRows.length > 0 && picked.size === histRows.length;
  $('#histDel').disabled = picked.size === 0;
  $('#histDel').textContent = picked.size ? `删除选中 ${picked.size} 条` : '删除选中';
  const total = histRows.reduce((s, r) => s + (r.bytes || 0) + (r.pdf_bytes || 0), 0);
  $('#histSize').textContent = `共 ${histRows.length} 条 · ${kb(total)}`;
}

async function loadHistory() {
  let rows = [];
  try {
    const r = await fetch('/api/lessons');
    if (r.ok) rows = (await r.json()).lessons || [];
  } catch (e) { rows = []; }
  histRows = rows;
  for (const id of [...picked]) {          // 已经不存在的就别留着占数
    if (!rows.some(r => r.id === id)) picked.delete(id);
  }
  const box = $('#histBody');
  box.classList.toggle('managing', histMode);
  box.innerHTML = '';
  $('#histEdit').textContent = histMode ? '完成' : '管理';
  $('#histBar').classList.toggle('hide', !histMode);
  if (!rows.length) {
    box.innerHTML = '<div class="hint sm">还没有生成过讲义。</div>';
    if (histMode) histMode = false;
    $('#histBar').classList.add('hide');
    $('#histEdit').textContent = '管理';
    return;
  }
  for (const rec of rows) {
    const b = document.createElement('button');
    b.className = 'hist-item' + (picked.has(rec.id) ? ' picked' : '');
    b.dataset.id = rec.id;
    b.title = `${rec.filename || ''} · ${rec.chapter} · ${rec.updated || ''}`;
    b.innerHTML = '<span class="pick"></span>' +
      `<span class="hname">${esc(rec.chapter)}</span>` +
      `<span class="when">${rec.quiz || 0} 题 · ${kb((rec.bytes || 0) + (rec.pdf_bytes || 0))}</span>`;
    box.appendChild(b);
  }
  paintHistBar();
}

function togglePick(id) {
  if (picked.has(id)) picked.delete(id); else picked.add(id);
  const b = $(`#histBody .hist-item[data-id="${id}"]`);
  if (b) b.classList.toggle('picked', picked.has(id));
  paintHistBar();
}

async function purgePicked() {
  const ids = [...picked];
  if (!ids.length) return;
  const total = histRows.filter(r => picked.has(r.id))
    .reduce((s, r) => s + (r.bytes || 0) + (r.pdf_bytes || 0), 0);
  const ask = `删掉这 ${ids.length} 条讲义？\n\n` +
    `会一起删掉 out/ 里对应的 JSON；原 PDF 如果没被别的讲义用着，也一起删。\n` +
    `能腾出大约 ${kb(total)}。删了就找不回来。`;
  if (!confirm(ask)) return;
  const r = await fetch('/api/lessons/purge', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify({ ids, drop_pdfs: true }),
  });
  if (!r.ok) { fail(null, (await r.json()).detail || '删除失败'); return; }
  const out = await r.json();
  picked.clear();
  await loadHistory();
  $('#err').textContent = '';
  $('#detail').textContent = `删了 ${out.deleted} 条，腾出 ${kb(out.freed)}` +
    (out.pdfs ? `（含 ${out.pdfs} 份 PDF）` : '');
}

/** 打开一份旧讲义。doc_id 是按文件内容算的，所以同一份 PDF 重传一次就接得上。 */
async function openLesson(id) {
  const r = await fetch(`/api/lessons/${id}`);
  if (!r.ok) {
    fail(null, (await r.json()).detail || '打不开这份讲义');
    return;
  }
  const { record, lesson: L } = await r.json();
  lesson = L;
  renderLesson(L);
  $('#err').textContent = '';
  if (record.doc_id && record.doc_id === docId && layout) {
    await renderSource();      // 同一份 PDF 还开着，原文照旧能点
  } else if (record.doc_id && docId !== record.doc_id) {
    // 服务重启过，或者换了份 PDF：磁盘上有 uploads/<doc_id>.pdf 就能直接重解析
    try {
      const p = await fetch(`/api/docs/${record.doc_id}/parse`, { method: 'POST' });
      const pj = await p.json();
      if (pj.job_id) {
        const snap = await waitJob(pj.job_id);
        if (snap.state !== 'done') throw new Error('parse failed');
        await afterParse(snap.result);
      } else {
        await afterParse(pj.result);
      }
      docId = record.doc_id;
      $('#upMeta').textContent = record.filename || '';
      await renderSource();
      return;
    } catch (e) { /* 文件不在了，走下面的提示 */ }
    $('#sourceBody').innerHTML =
      '<p class="hint">这是之前生成的讲义，原 PDF 已经不在磁盘上了。' +
      '重新上传同一份 PDF，就能点出处回原文。</p>';
    $('#srcMeta').textContent = '';
  } else {
    await renderSource();
  }
}

/** 轮询一条任务直到结束（打开旧讲义时自动重解析用）。 */
function waitJob(jobId) {
  return new Promise(resolve => {
    const t = setInterval(async () => {
      const r = await fetch(`/api/jobs/${jobId}`);
      if (!r.ok) { clearInterval(t); resolve({ state: 'failed' }); return; }
      const s = await r.json();
      if (s.state === 'done' || s.state === 'failed') { clearInterval(t); resolve(s); }
    }, 500);
  });
}

$('#histBody').addEventListener('click', e => {
  const b = e.target.closest('.hist-item');
  if (!b) return;
  if (histMode) { togglePick(b.dataset.id); return; }
  openLesson(b.dataset.id);
});

$('#histEdit').onclick = () => {
  histMode = !histMode;
  if (!histMode) picked.clear();
  loadHistory();
};

$('#histAll').onchange = e => {
  if (e.target.checked) histRows.forEach(r => picked.add(r.id));
  else picked.clear();
  loadHistory();
};

$('#histDel').onclick = purgePicked;

/* ---------------- 讲义 ---------------- */

function renderLesson(L) {
  const parts = [];
  if (L.learning_objectives && L.learning_objectives.length) {
    parts.push('<div class="objectives"><h4>学完能做到</h4><ul>' +
      L.learning_objectives.map(x => '<li>' + esc(x) + '</li>').join('') + '</ul></div>');
  }
  (L.sections || []).forEach((s, i) => {
    parts.push(`<div class="sec" data-i="${i}"><h3>${i + 1}. ${esc(s.title)}</h3>`);
    parts.push(md(s.explanation_md));
    if (s.key_points && s.key_points.length) {
      parts.push('<ul>' + s.key_points.map(k => '<li>' + esc(k) + '</li>').join('') + '</ul>');
    }
    if (s.pitfalls && s.pitfalls.length) {
      parts.push(s.pitfalls.map(t => '<div class="pitfall">易错：' + esc(t) + '</div>').join(''));
    }
    parts.push(chips(s.source_refs));
    parts.push('</div>');
  });
  if (L.quiz && L.quiz.length) parts.push(renderQuiz(L.quiz));
  if (!parts.length) parts.push('<p class="hint">这次没有生成出内容。</p>');
  $('#lessonBody').innerHTML = parts.join('');
  paintMath($('#lessonBody'));
  $('#lessonBody').scrollTop = 0;
}

/* ---------------- 习题：先自己做，再判分 ---------------- */

function renderQuiz(items) {
  const out = ['<div class="quiz"><h3>习题</h3>' +
               '<p class="hint">先自己选（可多选会标出来），点「提交」才看得到对错。</p>'];
  items.forEach((it, i) => {
    const type = it.type || 'single';
    out.push(`<div class="q" data-i="${i}" data-type="${esc(type)}" data-picked="">`);
    out.push(`<div class="stem">${i + 1}. ${esc(it.stem)}</div>`);
    if (type === 'fill') {
      out.push('<div class="fillrow"><input class="fillin" type="text" ' +
               'placeholder="把答案写在这里" autocomplete="off"></div>');
    } else {
      out.push('<div class="opts">' + (it.options || []).map((o, j) => {
        const k = String.fromCharCode(65 + j);
        return `<button class="opt" data-k="${k}">` +
               `<b>${k}</b> ${esc(o)}</button>`;
      }).join('') + '</div>');
      if (type === 'multi') out.push('<div class="hint sm">这题是多选</div>');
    }
    out.push('<div class="qbar"><button class="qgo">提交</button>' +
             '<button class="qshow ghost">看答案</button>' +
             '<button class="qreset ghost hide">再做一次</button>' +
             '<span class="qres"></span></div>');    out.push('<div class="qans hide"><div class="cans"></div>' +
             (it.explanation ? `<div class="expl">${esc(it.explanation)}</div>` : '') +
             chips(it.source_ref ? [it.source_ref] : []) + '</div>');
    out.push('</div>');
  });
  out.push('</div>');
  return out.join('');
}

function qItem(q) {
  const i = Number(q.dataset.i);
  return (lesson && lesson.quiz && lesson.quiz[i]) || null;
}

function pickedOf(q) {
  return [...q.querySelectorAll('.opt.on')].map(b => b.dataset.k).join('');
}

function lockQuestion(q, on) {
  q.dataset.locked = on ? '1' : '';
  q.querySelectorAll('.opt').forEach(b => { b.disabled = on; });
  const fill = q.querySelector('.fillin');
  if (fill) fill.disabled = on;
  q.querySelector('.qgo').classList.toggle('hide', on);
  // 「看答案」一直留着：判过之后它变成"看解析"，否则学生想回头再看一眼
  // 只能点「再做一次」重答一遍。答题前点它本来就等于认输，没有额外风险。
  const show = q.querySelector('.qshow');
  show.classList.remove('hide');
  show.textContent = on ? '看解析' : '看答案';
  q.querySelector('.qreset').classList.toggle('hide', !on);
}

function revealAnswer(q, it, res) {
  q.querySelector('.qans').classList.remove('hide');
  const want = String(it.answer == null ? '' : it.answer);
  let line = `答案 ${esc(want)}`;
  if (it.type === 'fill') line = `参考答案：${esc(want)}`;
  const cans = q.querySelector('.cans');
  cans.innerHTML = line;
  if (res && !res.reason) {
    const dom = q.querySelector('.qres');
    dom.textContent = res.ok ? '对了' : '不对';
    dom.className = 'qres ' + (res.ok ? 'ok' : 'no');
  }
}

$('#lesson').addEventListener('click', e => {
  const q = e.target.closest('.q');
  if (!q) return;
  const it = qItem(q);
  if (!it) return;

  const opt = e.target.closest('.opt');
  if (opt && q.dataset.locked !== '1') {
    const multi = (q.dataset.type || 'single') === 'multi';
    if (multi) opt.classList.toggle('on');
    else {
      q.querySelectorAll('.opt').forEach(b => b.classList.toggle('on', b === opt));
    }
    q.dataset.picked = pickedOf(q);
    return;
  }

  if (e.target.closest('.qgo')) {
    const typed = (q.querySelector('.fillin') || {}).value || '';
    const res = Grade.grade(it, pickedOf(q), typed);
    if (res.reason === 'empty') {
      const dom = q.querySelector('.qres');
      dom.textContent = it.type === 'fill' ? '先写点什么' : '先选一个';
      dom.className = 'qres warn';
      return;
    }
    const want = Grade.normLetters(it.answer);
    q.querySelectorAll('.opt').forEach(b => {
      const k = b.dataset.k;
      b.classList.remove('on');
      if (want.indexOf(k) >= 0) b.classList.add('right');
      else if (res.ok === false && q.dataset.picked.indexOf(k) >= 0) b.classList.add('wrong');
    });
    revealAnswer(q, it, res);
    lockQuestion(q, true);
    return;
  }

  if (e.target.closest('.qshow')) { revealAnswer(q, it, null); lockQuestion(q, true); return; }

  if (e.target.closest('.qreset')) {
    q.dataset.picked = '';
    q.querySelectorAll('.opt').forEach(b => { b.classList.remove('on', 'right', 'wrong'); });
    const fill = q.querySelector('.fillin');
    if (fill) fill.value = '';
    q.querySelector('.qans').classList.add('hide');
    q.querySelector('.qres').textContent = '';
    q.querySelector('.qres').className = 'qres';
    lockQuestion(q, false);
  }
});

/* ---------------- 原文 + 高亮 ---------------- */

function refPages() {
  const pages = new Set();
  if (section) {
    const a = section.page_start, b = section.page_end;
    if (a && b) for (let p = a; p <= b; p++) pages.add(p);
  }
  if (lesson) {
    for (const s of lesson.sections || []) for (const r of s.source_refs || []) pages.add(r.page);
    for (const q of lesson.quiz || []) if (q.source_ref) pages.add(q.source_ref.page);
  }
  return pages;
}

function renderSource() {
  const spec = [...refPages()].sort((a, b) => a - b).join(',');
  if (spec === _srcSpec) { paintSource(); return Promise.resolve(); }   // 同一批页直接用
  _srcSpec = spec;
  _srcSeq += 1;
  const seq = _srcSeq;
  return fetch(`/api/docs/${docId}/layout` + (spec ? `?pages=${spec}` : ''))
    .then(r => (r.ok ? r.json() : null))
    .then(data => {
      if (seq !== _srcSeq) return;     // 中途又换了章节，旧的那次丢掉
      layout = data;
      paintSource();
    })
    .catch(() => { /* 原文栏拿不到也不至于让讲义看不了 */ });
}

let _srcSpec = null;
let _srcSeq = 0;

function paintSource() {
  if (!layout || !layout.pages) return;
  const want = refPages();
  const shown = layout.pages.filter(p => want.has(p.page));
  if (!shown.length) {
    $('#sourceBody').innerHTML = '<p class="hint">还没有可显示的原文。</p>';
    $('#srcMeta').textContent = '';
    return;
  }
  $('#srcMeta').textContent = `p${shown[0].page}–${shown[shown.length - 1].page}`;
  $('#sourceBody').innerHTML = shown.map(p =>
    `<div class="pg"><div class="pgno">PAGE ${p.page}</div>` +
    p.blocks.map(b =>
      `<div class="blk k-${b.kind}" data-id="${esc(b.id)}">${esc(b.text)}</div>`).join('') +
    '</div>').join('');
}

$('#lesson').addEventListener('click', e => {
  const btn = e.target.closest('.ref');
  if (!btn) return;
  if (srcCollapsed()) setSourceCollapsed(false);   // 收起来了就先展开，不然跳过去看不见
  const ids = (btn.dataset.ids || '').split(',').filter(Boolean);
  document.querySelectorAll('.blk.hl').forEach(x => x.classList.remove('hl'));
  if (!ids.length) return;
  const first = document.querySelector(`.blk[data-id="${ids[0]}"]`);
  if (!first) return;
  ids.forEach(id =>
    document.querySelectorAll(`.blk[data-id="${id}"]`).forEach(x => x.classList.add('hl')));
  first.scrollIntoView({ block: 'center', behavior: 'smooth' });
});

/* 页面一打开就把历史列出来 —— 不依赖用户先传 PDF。 */
loadHistory();

/* ---------- 设置：左下角那颗钉子 ---------- */

let setState = null;   // 最近一次从服务端拿到的设置（含 efforts 表）

const SOURCE_TEXT = {
  settings: '来自设置界面',
  env: '来自 .env（设置界面留空就用它）',
  '': '还没填',
};

function effortLabel(v) {
  const row = (setState && setState.efforts || []).find(x => x.value === v);
  return row ? row.label : (v || '默认');
}

function paintEfforts(current) {
  const box = $('#setEffort');
  box.innerHTML = '';
  (setState && setState.efforts || []).forEach(({ value, label }) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.dataset.value = value;
    b.textContent = label;
    if (value === (current || '')) b.classList.add('on');
    b.onclick = () => {
      box.querySelectorAll('button').forEach(x => x.classList.remove('on'));
      b.classList.add('on');
    };
    box.appendChild(b);
  });
}

const MANUAL = '__manual__';

function fillModels(models, current) {
  const sel = $('#setModel');
  const list = (models || []).slice();
  if (current && list.indexOf(current) < 0) list.unshift(current);   // 当前这个永远要能选中
  sel.innerHTML = '';
  list.forEach(id => {
    const o = document.createElement('option');
    o.value = id;
    o.textContent = id;
    sel.appendChild(o);
  });
  const manual = document.createElement('option');
  manual.value = MANUAL;
  manual.textContent = '手动填写…';
  sel.appendChild(manual);
  sel.value = current || (list.length ? list[0] : MANUAL);
  const isManual = sel.value === MANUAL;
  $('#setModelFree').classList.toggle('hide', !isManual);
  if (isManual) $('#setModelFree').value = '';
}

$('#setModel').addEventListener('change', () => {
  const manual = $('#setModel').value === MANUAL;
  $('#setModelFree').classList.toggle('hide', !manual);
  if (manual) $('#setModelFree').focus();
});

function pickedModel() {
  const sel = $('#setModel').value;
  return sel === MANUAL ? $('#setModelFree').value.trim() : sel;
}

function paintSettings(payload) {
  const s = setState = payload.settings;
  $('#setUrl').value = s.base_url || '';
  $('#setKey').value = '';
  $('#setKey').placeholder = s.has_key
    ? `已保存 ${s.key_hint}，留空就不改`
    : '粘贴密钥';
  $('#srcUrl').textContent = SOURCE_TEXT[s.source.base_url || ''] || '';
  $('#srcKey').textContent = s.has_key
    ? `已保存 ${s.key_hint} · ${SOURCE_TEXT[s.source.api_key || ''] || ''}`
    : '还没有密钥';
  $('#srcModel').textContent = SOURCE_TEXT[s.source.model || ''] || '';
  fillModels(payload.models, s.model);
  paintEfforts(s.reasoning_effort);
  $('#setMsg').textContent = payload.models && payload.models.length
    ? `接入地址上有 ${payload.models.length} 个模型` : '';
}

async function openSettings() {
  $('#setModal').classList.remove('hide');
  paintTheme();
  paintFont();
  paintVersion();
  $('#setMsg').textContent = '读设置……';
  try {
    paintSettings(await (await fetch('/api/settings')).json());
  } catch (e) {
    $('#setMsg').textContent = '读不到设置：' + e;
  }
}

function closeSettings() {
  $('#setModal').classList.add('hide');
}

$('#gear').onclick = openSettings;
$('#setClose').onclick = closeSettings;
$('#setModal').addEventListener('click', e => {
  if (e.target === $('#setModal')) closeSettings();   // 点空白处关掉
});
document.addEventListener('keydown', e => {
  if (e.key === 'Escape') closeSettings();
});

async function saveSettings(extra) {
  const body = Object.assign({
    base_url: $('#setUrl').value.trim(),
    model: pickedModel(),
  }, extra || {});
  const key = $('#setKey').value.trim();
  if (key) body.api_key = key;            // 留空 = 不动已保存的
  const on = $('#setEffort button.on');
  if (on) body.reasoning_effort = on.dataset.value;
  const r = await fetch('/api/settings', {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!r.ok) throw new Error(data.detail || '保存失败');
  paintSettings(data);
  $('#setMsg').textContent = '保存好了';
}

$('#setSave').onclick = async () => {
  $('#setMsg').textContent = '保存中……';
  try {
    await saveSettings();
    const c = await (await fetch('/api/config')).json();
    $('#go').disabled = !(docId && section && c.ready);
    if (!c.ready) $('#setMsg').textContent = '还差：' + c.missing.join('、');
  } catch (e) {
    $('#setMsg').textContent = String(e.message || e);
  }
};

$('#setTest').onclick = async () => {
  $('#setMsg').textContent = '问一下模型列表……（会先把上面的地址和密钥存下来）';
  try {
    await saveSettings();
    const n = $('#setModel').options.length - 1;   // 减去"手动填写…"
    $('#setMsg').textContent = n > 0
      ? `拿到 ${n} 个模型，在「模型」下拉里挑`
      : '没问到模型列表：地址/密钥可能不对，或者这家不提供 /models';
  } catch (e) {
    $('#setMsg').textContent = String(e.message || e);
  }
};

/* ---------- 设置里的标签页 ---------- */

function showTab(name) {
  document.querySelectorAll('#setTabs .tab')
    .forEach(b => b.classList.toggle('on', b.dataset.tab === name));
  document.querySelectorAll('.pane')
    .forEach(p => p.classList.toggle('hide', p.dataset.pane !== name));
}

$('#setTabs').onclick = e => {
  const b = e.target.closest('.tab');
  if (b) showTab(b.dataset.tab);
};

/* ---------- 字号 ---------- */

const FONT_KEY = 'study.font';
// 用 zoom 整体缩放：这套样式全是 px，只改根字号不会动 90% 的规则。
const FONT_SCALE = { small: 0.9, normal: 1, large: 1.16 };

function readFont() {
  let v = 'normal';
  try { v = localStorage.getItem(FONT_KEY) || 'normal'; } catch (e) { /* 无痕模式就读不到 */ }
  return FONT_SCALE[v] ? v : 'normal';
}

function applyFont(name) {
  const scale = FONT_SCALE[name] || 1;
  if (scale === 1) document.documentElement.style.removeProperty('zoom');
  else document.documentElement.style.zoom = String(scale);
}

function paintFont() {
  const cur = readFont();
  document.querySelectorAll('#fontMode button')
    .forEach(b => b.classList.toggle('on', b.dataset.size === cur));
}

$('#fontMode').onclick = e => {
  const b = e.target.closest('button[data-size]');
  if (!b) return;
  try { localStorage.setItem(FONT_KEY, b.dataset.size); } catch (err) { /* 无所谓 */ }
  applyFont(b.dataset.size);
  paintFont();
};

/* ---------- 版本 ---------- */

let _serverVer = '';

async function paintVersion() {
  try {
    const h = await (await fetch('/api/health', { cache: 'no-store' })).json();
    _serverVer = h.version || '';
    if (_serverVer) $('#verNow').textContent = 'v' + _serverVer;
  } catch (e) { /* 拿不到就留着页面上写死的那个 */ }
}

$('#updBtn').onclick = async () => {
  const btn = $('#updBtn');
  btn.disabled = true;
  $('#verMsg').textContent = '正在核对版本……';
  try {
    const h = await (await fetch('/api/health', { cache: 'no-store' })).json();
    const now = h.version || '';
    if (now && now !== _serverVer) {
      $('#verNow').textContent = 'v' + now;
      $('#verMsg').textContent = `发现 v${now}，正在重新拉界面……`;
      setTimeout(() => location.reload(), 700);
      return;
    }
    $('#verMsg').textContent = `已经是最新版本 v${now || _serverVer || '1.0'}。`;
  } catch (e) {
    $('#verMsg').textContent = '问不到服务端版本：服务可能没起来。';
  } finally {
    btn.disabled = false;
  }
};

/* ---------- 颜色 ---------- */

const THEME_KEY = 'study.theme';
const THEME_VARS = { bg: '--bg', panel: '--panel', fg: '--fg', accent: '--accent' };

function readTheme() {
  try {
    return JSON.parse(localStorage.getItem(THEME_KEY) || 'null') || {};
  } catch (e) {
    return {};
  }
}

function saveTheme(t) {
  try { localStorage.setItem(THEME_KEY, JSON.stringify(t)); } catch (e) { /* 无痕模式就存不上 */ }
}

function cssVar(name) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return /^#[0-9a-f]{6}$/i.test(v) ? v : '#000000';
}

function applyTheme(t) {
  const root = document.documentElement;
  Object.values(THEME_VARS).forEach(v => root.style.removeProperty(v));
  root.dataset.theme = t.mode === 'light' ? 'light' : (t.mode === 'custom' ? (t.base || 'dark') : 'dark');
  if (t.mode === 'custom') {
    Object.entries(t.custom || {}).forEach(([k, v]) => root.style.setProperty(k, v));
  }
}

function paintTheme() {
  const t = readTheme();
  const mode = t.mode || 'dark';
  $('#themeMode').querySelectorAll('button').forEach(b => {
    b.classList.toggle('on', b.dataset.mode === mode);
  });
  $('#customBox').classList.toggle('hide', mode !== 'custom');
  const custom = t.custom || {};
  const pick = (id, name) => { $(id).value = custom[name] || cssVar(THEME_VARS[name]); };
  pick('#cBg', 'bg');
  pick('#cPanel', 'panel');
  pick('#cFg', 'fg');
  pick('#cAccent', 'accent');
}

$('#themeMode').addEventListener('click', e => {
  const b = e.target.closest('button[data-mode]');
  if (!b) return;
  const t = readTheme();
  t.mode = b.dataset.mode;
  if (t.mode === 'custom' && !t.custom) {
    t.base = t.base || 'dark';
    t.custom = {
      '--bg': cssVar('--bg'), '--panel': cssVar('--panel'),
      '--fg': cssVar('--fg'), '--accent': cssVar('--accent'),
    };
  }
  saveTheme(t);
  applyTheme(t);
  paintTheme();
});

[['#cBg', 'bg'], ['#cPanel', 'panel'], ['#cFg', 'fg'], ['#cAccent', 'accent']]
  .forEach(([sel, name]) => {
    $(sel).addEventListener('input', () => {
      const t = readTheme();
      t.mode = 'custom';
      t.base = t.base || (document.documentElement.dataset.theme === 'light' ? 'light' : 'dark');
      t.custom = Object.assign({}, t.custom, { [THEME_VARS[name]]: $(sel).value });
      saveTheme(t);
      applyTheme(t);
      paintTheme();
    });
  });

applyTheme(readTheme());
applyFont(readFont());
paintFont();

/* ---------- 原文栏可折叠 ---------- */

// 存的是「展开过」而不是「收起过」：没存过就是收起，先让人看讲义。
const SRC_KEY = 'study.srcExpanded';

function srcCollapsed() {
  return document.querySelector('.cols').classList.contains('no-src');
}

function setSourceCollapsed(on) {
  document.querySelector('.cols').classList.toggle('no-src', on);
  $('#srcToggle').textContent = on ? '展开原文' : '收起原文';
  try { localStorage.setItem(SRC_KEY, on ? '0' : '1'); } catch (e) { /* 无所谓 */ }
}

$('#srcToggle').onclick = () => setSourceCollapsed(!srcCollapsed());

let _srcExpanded = null;
try { _srcExpanded = localStorage.getItem(SRC_KEY); } catch (e) { /* 无所谓 */ }
setSourceCollapsed(_srcExpanded !== '1');      // 默认收起


