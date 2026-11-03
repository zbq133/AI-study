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

/* ---------------- 出处 chip ---------------- */

function chips(refs) {
  if (!refs || !refs.length) return '';
  return refs.map(r =>
    `<button class="ref" data-page="${r.page}" data-ids="${esc((r.blocks || []).join(','))}"` +
    ` title="${esc(r.quote)}">原文 p${r.page}</button>`).join('');
}

/* ---------------- 进度 ---------------- */

function stageView(bar, stageEl, detailEl, snap) {
  bar.style.width = (snap.percent || 0) + '%';
  stageEl.textContent = snap.stage + (snap.state === 'running' ? '…' : '');
  detailEl.textContent = [snap.message, snap.calls ? `模型调用 ${snap.calls} 次` : '',
                          snap.elapsed ? `${snap.elapsed}s` : '']
                          .filter(Boolean).join(' · ');
}

function watch(jobId, onDone) {
  const es = new EventSource(`/api/jobs/${jobId}/events`);
  es.onmessage = ev => {
    const snap = JSON.parse(ev.data);
    onDone.snap(snap);
    if (snap.state === 'done' || snap.state === 'failed') { es.close(); onDone.end(snap); }
  };
  es.onerror = () => { es.close(); };
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
          $('#err').textContent = s.error; $('#up').disabled = false;
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
      if (s.state === 'failed') { $('#err').textContent = s.error; return; }
      lesson = s.result.lesson || {};
      $('#goMeta').textContent = `${s.result.sections || 0} 节 · ${s.result.quiz || 0} 题 · ${s.elapsed}s`;
      renderLesson(lesson);
      renderSource();
    }
  });
};

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
  if (L.quiz && L.quiz.length) {
    const q = ['<div class="quiz"><h3>习题（答案已独立重解验证）</h3>'];
    L.quiz.forEach((it, i) => {
      q.push(`<div class="q"><div class="stem">${i + 1}. ${esc(it.stem)}</div>`);
      (it.options || []).forEach((o, j) =>
        q.push(`<div class="opt">${String.fromCharCode(65 + j)}. ${esc(o)}</div>`));
      q.push(`<div class="ans">答案 ${esc(it.answer)}</div>`);
      q.push(chips(it.source_ref ? [it.source_ref] : []));
      q.push('</div>');
    });
    q.push('</div>');
    parts.push(q.join(''));
  }
  if (!parts.length) parts.push('<p class="hint">这次没有生成出内容。</p>');
  $('#lessonBody').innerHTML = parts.join('');
  $('#lessonBody').scrollTop = 0;
}

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
  const ids = (btn.dataset.ids || '').split(',').filter(Boolean);
  document.querySelectorAll('.blk.hl').forEach(x => x.classList.remove('hl'));
  if (!ids.length) return;
  const first = document.querySelector(`.blk[data-id="${ids[0]}"]`);
  if (!first) return;
  ids.forEach(id =>
    document.querySelectorAll(`.blk[data-id="${id}"]`).forEach(x => x.classList.add('hl')));
  first.scrollIntoView({ block: 'center', behavior: 'smooth' });
});
