'use strict';
/* Preview player, clip check / prepare panel, smart shuffle, reset used.
 * Loaded after app.js and uses its helpers (state, api, modal, toast, ...). */

(function () {
  const $ = id => document.getElementById(id);

  // ---------------------------------------------------------- smart shuffle
  // Same rules as the pacing module: pinned clips stay on top, the two biggest
  // clips open, big hits land evenly (at least every 4 clips), no two long clips
  // or near-identical titles back to back, one big hit saved for the end.
  const LONG = 90;
  const words = t => new Set(String(t || '').toLowerCase().match(/[a-z0-9']{4,}/g) || []);
  function similar(a, b) {
    const wa = words(a.title), wb = words(b.title);
    if (!wa.size || !wb.size) return false;
    let n = 0; wa.forEach(w => { if (wb.has(w)) n++; });
    return n / Math.min(wa.size, wb.size) >= 0.6;
  }
  const okAfter = (p, c) => !p || (!(p.duration > LONG && c.duration > LONG) && !similar(p, c));
  function take(pool, prev) {
    const i = pool.findIndex(c => okAfter(prev, c));
    return pool.splice(i >= 0 ? i : 0, 1)[0];
  }
  function shuffle(a) {
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]];
    }
    return a;
  }
  function smartOrder(clips, keep, hitEvery) {
    const out = clips.slice(0, keep);
    const rest = clips.slice(keep).sort((a, b) => (b.view_count || 0) - (a.view_count || 0));
    if (!rest.length) return out;
    const nHits = Math.max(1, Math.round(rest.length * 0.2));
    const hits = rest.slice(0, nHits), others = rest.slice(nHits);
    const closer = hits.length > 1 ? hits.pop() : null;
    if (!out.length) for (let i = 0; i < 2 && hits.length; i++) out.push(take(hits, out[out.length - 1]));
    shuffle(hits); shuffle(others);
    let since = 0;
    while (hits.length || others.length) {
      const prev = out[out.length - 1];
      const gap = hits.length ? Math.max(hitEvery - 1, Math.floor(others.length / hits.length)) : 0;
      if (hits.length && (since >= gap || !others.length)) { out.push(take(hits, prev)); since = 0; }
      else { out.push(take(others, prev)); since++; }
    }
    if (closer) out.push(closer);
    return out;
  }

  $('smartShuffle').addEventListener('click', () => {
    const st = window.state;
    if (st.picked.length < 3) { toast('Pick a few clips first'); return; }
    const keep = Math.max(0, Math.min(parseInt($('keepFirst').value, 10) || 0, st.picked.length));
    const clips = st.picked.map(id => st.map.get(id) || { id, title: id });
    st.picked = smartOrder(clips, keep, 4).map(c => c.id);
    render(); renderTray();
    toast(keep ? `Shuffled, first ${keep} kept in place` : 'Shuffled with pacing');
  });

  // ------------------------------------------------------------ clip states
  async function fetchStates(ids, download) {
    const r = await api('/api/prepare', { method: 'POST',
      body: JSON.stringify({ video_ids: ids, download: !!download }) });
    const map = new Map(); r.clips.forEach(c => map.set(c.id, c));
    return { map, started: r.started };
  }
  const STATE_TEXT = { ready: 'ready', missing: 'not downloaded', queued: 'queued',
                       downloading: 'downloading', failed: 'failed' };

  function labelFor(i) {
    const tpl = ($('tpl').value || 'Story {n}');
    const t = tpl.replace(/\{n\}|\{index\}/g, String(i + 1))
                 .replace(/\{total\}/g, String(window.state.picked.length));
    return ($('lblStyle').value === 'box') ? t.toUpperCase() : t;
  }

  // ------------------------------------------------------------- the panel
  // One window for both jobs: watch the compilation as it will play, and see
  // which clips are ready, which are downloading and which failed (and why),
  // with remove / retry right there. Nothing is encoded to preview: the
  // browser plays the downloaded clips back to back with the label on top.
  const pv = { i: 0, states: new Map(), timer: null, labelTimer: null, mode: 'preview' };

  function openPanel(mode) {
    pv.mode = mode; pv.i = 0;
    const vertical = ['9:16', '4:5'].includes($('aspect').value);
    const w = vertical ? 330 : 560, h = vertical ? Math.round(w * 16 / 9) : Math.round(w * 9 / 16);
    modal(`<div class="mh"><b>${mode === 'preview' ? 'Preview' : 'Check clips'}</b>
        <span id="pvSum" class="note" style="margin:0 12px 0 0"></span>
        <button class="btn sm ghost" id="pvClose">Close</button></div>
      <div class="mb"><div class="pv">
        <div class="pvstage" style="width:${w}px;height:${h}px">
          <video id="pvVideo" playsinline></video>
          <div class="pvlabel" id="pvLabel" style="font-size:${Math.round(h * 0.034)}px;opacity:0"></div>
        </div>
        <div class="pvside">
          <div class="pvctl" style="margin:0 0 8px">
            <button class="btn sm" id="pvPrev">&#9664;&#9664;</button>
            <button class="btn sm primary" id="pvPlay">&#9654; Play</button>
            <button class="btn sm" id="pvNext">&#9654;&#9654;</button>
            <select id="pvRate" class="btn sm"><option value="1">1x</option><option value="1.5">1.5x</option>
              <option value="2">2x</option><option value="4">4x</option></select>
            <label class="chk" style="padding:0"><input type="checkbox" id="pvSkim"> skim (first 6s of each)</label>
          </div>
          <div class="pvlist" id="pvList"></div>
          <div class="pvctl">
            <button class="btn sm" id="pvDownload">Download missing</button>
            <button class="btn sm ghost" id="pvDropFailed">Remove failed</button>
            <span id="pvNote"></span>
          </div>
        </div>
      </div></div>`);
    const mask = $('mask');
    mask.querySelector('.modal').classList.add('wide');
    $('pvClose').onclick = closePanel;
    mask.addEventListener('click', e => { if (e.target === mask) closePanel(); });
    $('pvPlay').onclick = togglePlay;
    $('pvPrev').onclick = () => go(pv.i - 1, true);
    $('pvNext').onclick = () => go(pv.i + 1, true);
    $('pvRate').onchange = () => { $('pvVideo').playbackRate = +$('pvRate').value; };
    $('pvDownload').onclick = downloadMissing;
    $('pvDropFailed').onclick = dropFailed;
    const v = $('pvVideo');
    v.addEventListener('ended', () => go(pv.i + 1, true));
    v.addEventListener('timeupdate', () => {
      if ($('pvSkim').checked && v.currentTime > 6) go(pv.i + 1, true);
    });
    refresh().then(() => go(0, false));
    pv.timer = setInterval(refresh, 2500);
  }

  function closePanel() {
    clearInterval(pv.timer); pv.timer = null;
    clearTimeout(pv.labelTimer);
    const v = $('pvVideo'); if (v) { v.pause(); v.removeAttribute('src'); v.load(); }
    closeModal();
  }

  async function refresh() {
    const ids = window.state.picked;
    if (!ids.length) { closePanel(); return; }
    try { pv.states = (await fetchStates(ids, false)).map; } catch (e) { return; }
    renderList();
  }

  function renderList() {
    const st = window.state, list = $('pvList'); if (!list) return;
    let ready = 0, failed = 0, busy = 0;
    list.innerHTML = st.picked.map((id, i) => {
      const v = st.map.get(id) || { title: id };
      const s = pv.states.get(id) || { state: 'missing' };
      if (s.state === 'ready') ready++; else if (s.state === 'failed') failed++;
      else if (s.state === 'downloading' || s.state === 'queued') busy++;
      return `<div class="pvrow${i === pv.i ? ' on' : ''}" data-i="${i}">
          <span class="n">${i + 1}</span><span class="t" title="${esc(v.title)}">${esc(v.title)}</span>
          <span class="v">${views(v.view_count)}</span>
          <span class="st ${s.state}">${STATE_TEXT[s.state] || s.state}</span>
          <button class="x" data-rm="${esc(id)}" title="Remove from the compilation">&times;</button>
        </div>${s.state === 'failed' && s.error ? `<div class="pverr">${esc(s.error)}</div>` : ''}`;
    }).join('');
    list.querySelectorAll('.pvrow').forEach(r => r.onclick = e => {
      if (e.target.dataset.rm) { removeClip(e.target.dataset.rm); return; }
      go(+r.dataset.i, true);
    });
    const total = st.picked.length;
    $('pvSum').textContent = `${ready}/${total} ready` + (busy ? `, ${busy} downloading` : '') +
      (failed ? `, ${failed} failed` : '');
    $('pvDownload').disabled = ready + busy === total;
    $('pvDropFailed').disabled = !failed;
  }

  function removeClip(id) {
    const st = window.state, i = st.picked.indexOf(id);
    if (i < 0) return;
    st.picked.splice(i, 1);
    if (pv.i > i) pv.i--;
    render(); renderTray(); renderList();
    if (i === pv.i) go(pv.i, false);
  }

  function dropFailed() {
    const st = window.state;
    const bad = st.picked.filter(id => (pv.states.get(id) || {}).state === 'failed');
    bad.forEach(id => st.picked.splice(st.picked.indexOf(id), 1));
    pv.i = Math.min(pv.i, Math.max(0, st.picked.length - 1));
    render(); renderTray(); renderList();
    toast(`Removed ${bad.length} clip(s) that would not download`);
  }

  async function downloadMissing() {
    const r = await fetchStates(window.state.picked, true);
    pv.states = r.map; renderList();
    $('pvNote').textContent = r.started ? `downloading ${r.started}, 3 at a time` : 'nothing to download';
  }

  function showLabel(i) {
    const el = $('pvLabel'); if (!el) return;
    clearTimeout(pv.labelTimer);
    const secs = parseFloat($('dur').value) || 0;
    el.textContent = labelFor(i);
    const top = $('pos').value;
    el.style.top = top.startsWith('top') ? '4%' : (top === 'center' ? '46%' : '');
    el.style.bottom = top === 'bottom' ? '6%' : '';
    el.style.opacity = '1';
    if (secs > 0) {
      const rate = +($('pvRate') || { value: 1 }).value || 1;
      pv.labelTimer = setTimeout(() => { el.style.opacity = '0'; }, secs * 1000 / rate);
    }
  }

  function go(i, autoplay) {
    const st = window.state, v = $('pvVideo');
    if (!v) return;
    if (i >= st.picked.length) { v.pause(); $('pvPlay').innerHTML = '&#9654; Play'; return; }
    pv.i = Math.max(0, i);
    renderList();
    const id = st.picked[pv.i];
    const s = pv.states.get(id) || {};
    const row = $('pvList').querySelector(`.pvrow[data-i="${pv.i}"]`);
    if (row) row.scrollIntoView({ block: 'nearest' });
    if (s.state !== 'ready') {
      v.removeAttribute('src'); v.load();
      v.poster = id.startsWith('local:') ? '' : `https://i.ytimg.com/vi/${id}/hqdefault.jpg`;
      $('pvLabel').textContent = `${labelFor(pv.i)}: not downloaded yet`;
      $('pvLabel').style.opacity = '1';
      if (autoplay) pv.labelTimer = setTimeout(() => go(pv.i + 1, true), 1500);
      return;
    }
    v.poster = '';
    v.src = `/api/videos/${encodeURIComponent(id)}/media`;
    v.playbackRate = +$('pvRate').value;
    showLabel(pv.i);
    if (autoplay) {
      v.play().catch(() => {});
      $('pvPlay').innerHTML = '&#10074;&#10074; Pause';
    }
  }

  function togglePlay() {
    const v = $('pvVideo');
    if (v.paused) {
      if (!v.getAttribute('src')) { go(pv.i, true); return; }
      v.play().catch(() => {}); $('pvPlay').innerHTML = '&#10074;&#10074; Pause';
    } else { v.pause(); $('pvPlay').innerHTML = '&#9654; Play'; }
  }

  $('previewBtn').addEventListener('click', () => openPanel('preview'));
  $('checkBtn').addEventListener('click', () => openPanel('check'));

  // ------------------------------------------------------ reset used counts
  document.addEventListener('click', async e => {
    if (e.target && e.target.id === 'resetUsed') {
      if (!confirm('Set every clip back to "never used"?')) return;
      const r = await api('/api/videos/reset-used', { method: 'POST' });
      toast(`${r.reset} clip(s) reset to never used`);
      if (window.loadVideos) window.loadVideos();
    }
  });
})();
