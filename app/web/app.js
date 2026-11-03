const $ = s => document.querySelector(s);
let docId = null;

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

$('#up').onclick = async () => {
  const f = $('#file').files[0];
  if (!f) return;
  $('#up').disabled = true; $('#pErr').textContent = '';
  $('#pBarWrap').classList.remove('hide');
  const fd = new FormData(); fd.append('file', f);
  try {
    const r = await fetch('/api/docs', { method: 'POST', body: fd });
    if (!r.ok) throw new Error((await r.json()).detail || r.status);
    const up = await r.json();
    docId = up.doc_id;
    $('#upMeta').textContent = `${up.filename} · ${(up.bytes/1048576).toFixed(1)} MB`;
    const p = await fetch(`/api/docs/${docId}/parse`, { method: 'POST' });
    const pj = await p.json();
    if (pj.cached) { renderSections(pj.result); $('#up').disabled = false; return; }
    watch(pj.job_id, {
      snap: s => stageView($('#pBar'), $('#pStage'), $('#pDetail'), s),
      end: s => {
        $('#up').disabled = false;
        if (s.state === 'failed') { $('#pErr').textContent = s.error; return; }
        renderSections(s.result);
      }
    });
  } catch (e) {
    $('#pErr').textContent = String(e.message || e);
    $('#up').disabled = false;
  }
};

function renderSections(res) {
  const sel = $('#sec');
  sel.innerHTML = '';
  for (const s of res.sections) {
    const o = document.createElement('option');
    o.value = s.title;
    o.textContent = `${String(s.n).padStart(2,' ')}. ${s.path.join(' > ')}  ·  p${s.page_start}-${s.page_end}  ·  ${s.chars} 字`;
    sel.appendChild(o);
  }
  $('#pickCard').classList.remove('hide');
  if (res.hidden) $('#goMeta').textContent = `另有 ${res.hidden} 条参考文献段已隐藏`;
}

$('#go').onclick = async () => {
  if (!docId) return;
  $('#go').disabled = true; $('#err').textContent = ''; $('#outCard').classList.add('hide');
  $('#runCard').classList.remove('hide');
  const body = JSON.stringify({ doc_id: docId, chapter: $('#sec').value,
                                n_quiz: Number($('#nq').value) || 3 });
  const r = await fetch('/api/lessons', { method: 'POST',
      headers: { 'content-type': 'application/json' }, body });
  if (!r.ok) {
    $('#err').textContent = (await r.json()).detail || r.status;
    $('#go').disabled = false; return;
  }
  const { job_id } = await r.json();
  watch(job_id, {
    snap: s => {
      stageView($('#bar'), $('#stage'), $('#detail'), s);
      $('#goMeta').textContent = s.elapsed ? `${s.elapsed}s` : '';
    },
    end: s => {
      $('#go').disabled = false;
      if (s.state === 'failed') { $('#err').textContent = s.error; return; }
      showResult(s.result);
    }
  });
};

function showResult(res) {
  $('#outCard').classList.remove('hide');
  const L = res.lesson || {};
  $('#summary').innerHTML =
    `<b>${L.sections ? L.sections.length : 0}</b> 节 · ` +
    `<b>${res.quiz || 0}</b> 道题通过验证 · 写入 <code>${res.file || ''}</code>`;
  const parts = [];
  if (L.learning_objectives?.length) {
    parts.push('学习目标');
    L.learning_objectives.forEach(x => parts.push('  • ' + x));
    parts.push('');
  }
  (L.sections || []).forEach((s, i) => {
    parts.push(`【${i+1}/${L.sections.length}】${s.title}`);
    parts.push(s.explanation_md);
    if (s.pitfalls?.length) parts.push('易错点：' + s.pitfalls.join('；'));
    parts.push('');
  });
  if (L.quiz?.length) {
    parts.push(`习题（${L.quiz.length} 道，均已独立重解验证）`);
    L.quiz.forEach((q, i) => {
      parts.push(`${i+1}. ${q.stem}`);
      (q.options || []).forEach((o, j) =>
        parts.push(`   ${String.fromCharCode(65+j)}. ${o}`));
      parts.push(`   答案 ${q.answer}`);
    });
  }
  const pre = document.createElement('pre');
  pre.textContent = parts.join('\n');
  $('#preview').innerHTML = '';
  $('#preview').appendChild(pre);
}
