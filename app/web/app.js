const $ = s => document.querySelector(s);
const esc = s => String(s == null ? '' : s)
  .replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));

let docId = null;
let layout = null;      // {pages:[{page, blocks:[{id,kind,text,level}]}]}
let section = null;     // 当前选中的章节（来自解析结果）
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
  const r = await fetch(`/api/docs/${docId}/layout`);
  layout = r.ok ? await r.json() : null;
}

/* ---------------- 章节列表 ---------------- */

function renderSections(res) {
  const sel = $('#sec');
  sel.innerHTML = '';
  const toc = $('#tocBody');
  toc.innerHTML = '';
  for (const s of res.sections) {
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
  section = res.sections[0] || null;
  if (res.hidden) $('#goMeta').textContent = `另有 ${res.hidden} 条参考文献段已隐藏`;
  markToc();
  renderSource();
}

function markToc() {
  document.querySelectorAll('.toc-item').forEach(b =>
    b.classList.toggle('on', section && b.dataset.title === section.title));
}

$('#tocBody').addEventListener('click', e => {
  const b = e.target.closest('.toc-item');
  if (!b) return;
  $('#sec').value = b.dataset.title;
  section = { title: b.dataset.title };
  // 从当前解析结果里补回页范围
  const opt = [...$('#sec').options].find(o => o.value === b.dataset.title);
  if (opt) {
    const m = opt.textContent.match(/p(\d+)-(\d+)/);
    if (m) section = { title: b.dataset.title, page_start: +m[1], page_end: +m[2] };
  }
  markToc();
  renderSource();
});

$('#sec').addEventListener('change', () => {
  const title = $('#sec').value;
  const opt = [...$('#sec').options].find(o => o.value === title);
  const m = opt && opt.textContent.match(/p(\d+)-(\d+)/);
  section = m ? { title, page_start: +m[1], page_end: +m[2] } : { title };
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
      renderLesson(lesson);
      renderSource();
      loadHistory();            // 新的排到历史最前面
    }
  });
};

/* ---------------- 历史：重启之后还认得的那些讲义 ---------------- */

async function loadHistory() {
  let rows = [];
  try {
    const r = await fetch('/api/lessons');
    if (r.ok) rows = (await r.json()).lessons || [];
  } catch (e) { rows = []; }
  const box = $('#histBody');
  box.innerHTML = '';
  if (!rows.length) {
    box.innerHTML = '<div class="hint sm">还没有生成过讲义。</div>';
    return;
  }
  for (const rec of rows) {
    const b = document.createElement('button');
    b.className = 'hist-item';
    b.dataset.id = rec.id;
    b.title = `${rec.filename || ''} · ${rec.chapter} · ${rec.updated || ''}`;
    b.innerHTML = `${esc(rec.chapter)}` +
      `<span class="when">${rec.quiz || 0} 题</span>`;
    box.appendChild(b);
  }
}

/** 打开一份旧讲义。原文栏要能显示才有意义 —— 那份 PDF 得还在内存里。 */
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
  if (record.doc_id && record.doc_id === docId) {
    renderSource();            // 同一份 PDF 还开着，原文照旧能点
  } else {
    $('#sourceBody').innerHTML =
      '<p class="hint">这是之前生成的讲义，原 PDF 已经不在内存里了。' +
      '重新上传同一份 PDF 解析一次，就能点出处回原文。</p>';
    $('#srcMeta').textContent = '';
  }
}

$('#histBody').addEventListener('click', e => {
  const b = e.target.closest('.hist-item');
  if (b) openLesson(b.dataset.id);
});

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
             '<span class="qres"></span></div>');
    out.push('<div class="qans hide"><div class="cans"></div>' +
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
  q.querySelector('.qshow').classList.toggle('hide', on);
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


