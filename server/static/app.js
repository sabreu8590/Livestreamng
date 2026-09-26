'use strict';

const $ = id => document.getElementById(id);
const state = {
  channels: [], channel: '', videos: [], map: new Map(),
  picked: [], months: [], polling: null, syncing: false, trims: {},
};

// ---------------------------------------------------------------- helpers

async function api(path, opts) {
  const res = await fetch(path, Object.assign({
    headers: { 'Content-Type': 'application/json' },
  }, opts || {}));
  if (res.status === 401) { location.reload(); throw new Error('signed out'); }
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (e) {}
    throw new Error(detail);
  }
  return res.json();
}

function hms(s) {
  s = Math.max(0, Math.round(s || 0));
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
  return h ? `${h}:${String(m).padStart(2, '0')}:${String(x).padStart(2, '0')}`
           : `${m}:${String(x).padStart(2, '0')}`;
}
function views(n) {
  if (n === null || n === undefined) return '';
  if (n >= 1e6) return (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  if (n >= 1e3) return (n / 1e3).toFixed(n >= 1e4 ? 0 : 1) + 'K';
  return String(n);
}
function size(bytes) {
  const b = bytes || 0;
  if (b >= 1073741824) return (b / 1073741824).toFixed(2) + ' GB';
  if (b >= 1048576) return (b / 1048576).toFixed(1) + ' MB';
  return Math.round(b / 1024) + ' KB';
}
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}
function toast(msg, ms) {
  const el = document.createElement('div');
  el.className = 'toast'; el.textContent = msg;
  document.body.appendChild(el);
  setTimeout(() => el.remove(), ms || 3200);
}

// ---------------------------------------------------------------- channels

async function loadChannels() {
  const data = await api('/api/channels');
  state.channels = data.channels;
  const sel = $('chan');
  sel.innerHTML = state.channels.length
    ? state.channels.map(c =>
        `<option value="${esc(c.id)}">${esc(c.title || c.handle)} (${c.video_count})</option>`).join('')
    : '<option value="">No channel yet</option>';
  if (state.channels.length) {
    if (!state.channels.some(c => c.id === state.channel)) state.channel = state.channels[0].id;
    sel.value = state.channel;
  }
  const active = state.channels.find(c => c.id === state.channel);
  if (active) {
    $('stat').textContent = active.sync_state === 'syncing'
      ? `syncing: ${active.sync_message || ''}`
      : (active.sync_state === 'error' ? `sync failed: ${active.sync_message}` : '');
    if (active.sync_state === 'syncing') {
      state.syncing = true;
      setTimeout(() => { loadChannels(); loadVideos(); }, 2000);
    } else if (state.syncing) {
      state.syncing = false;
      loadVideos();
    }
  }
}

// ------------------------------------------------------------------ videos

async function loadVideos() {
  if (!state.channel) { render(); return; }
  const p = new URLSearchParams({
    channel: state.channel, q: $('q').value.trim(), month: $('month').value,
    shorts: $('shorts').value, sort: $('sort').value,
    unused: $('unused').checked ? '1' : '', limit: '2000',
    min_views: $('minViews').value, date_from: $('dateFrom').value,
    date_to: $('dateTo').value,
  });
  const data = await api('/api/videos?' + p);
  state.videos = data.videos;
  state.totals = data;
  data.videos.forEach(v => state.map.set(v.id, v));
  if (data.months.length !== state.months.length) {
    state.months = data.months;
    const cur = $('month').value;
    $('month').innerHTML = '<option value="">All months</option>' +
      data.months.map(m => `<option value="${m}">${m}</option>`).join('');
    $('month').value = cur;
  }
  render();
}

function renderTotals() {
  const d = state.totals;
  if (!d) { $('totals').innerHTML = ''; return; }
  const lib = d.library || {};
  const unknown = d.unknown || 0;
  $('totals').innerHTML =
    `<span><b>${d.total}</b> clips shown &middot; <b>${hms(d.runtime)}</b> of footage</span>` +
    (lib.count ? `<span>library: <b>${lib.count}</b> clips, <b>${hms(lib.runtime)}</b></span>` : '') +
    (unknown ? `<span class="warn">${unknown} still missing length and date</span>
       <button class="btn sm" id="fillDetails">Fill them in</button>` : '');
  const btn = $('fillDetails');
  if (btn) btn.addEventListener('click', async () => {
    btn.disabled = true;
    const r = await api(`/api/channels/${state.channel}/hydrate`, { method: 'POST' });
    toast(`Filling in ${r.pending} clips, this runs in the background`);
    setTimeout(() => { loadChannels(); loadVideos(); }, 3000);
  });
}

function render() {
  renderTotals();
  const grid = $('grid'), empty = $('empty');
  if (!state.videos.length) {
    grid.innerHTML = '';
    empty.style.display = 'block';
    empty.textContent = state.channels.length
      ? 'No clips match these filters. Try clearing the month or type filter, or hit Sync.'
      : 'Add a channel to get started.';
    return;
  }
  empty.style.display = 'none';
  grid.innerHTML = state.videos.map(v => {
    const i = state.picked.indexOf(v.id);
    const thumb = v.thumb || (v.id.startsWith('local:') ? '' : `https://i.ytimg.com/vi/${v.id}/hqdefault.jpg`);
    const wide = v.is_short ? '' : ' wide';
    return `<div class="card${i >= 0 ? ' sel' : ''}" data-id="${esc(v.id)}">
      <div class="thumbwrap">
        ${thumb ? `<img class="thumb${wide}" loading="lazy" src="${esc(thumb)}" alt="">`
                : `<div class="thumb${wide}"></div>`}
        <div class="pill dur">${v.duration > 0 ? hms(v.duration) : '?'}</div>
        ${v.used_count > 0 ? `<div class="pill used">used ${v.used_count}x</div>` : ''}
        ${i >= 0 ? `<div class="ordnum">${i + 1}</div>` : ''}
      </div>
      <div class="meta">
        <div class="t">${esc(v.title)}</div>
        <div class="s"><span>${esc(v.upload_date || 'no date yet')}</span><span>${views(v.view_count)}</span></div>
      </div></div>`;
  }).join('');
}

$('grid').addEventListener('click', e => {
  const card = e.target.closest('.card');
  if (!card) return;
  toggle(card.dataset.id);
});

function toggle(id) {
  const i = state.picked.indexOf(id);
  if (i >= 0) state.picked.splice(i, 1);
  else state.picked.push(id);
  render(); renderTray();
}

// ------------------------------------------------------------------- tray

function renderTray() {
  const box = $('picked');
  const total = state.picked.reduce(
    (a, id) => a + ((state.map.get(id) || {}).duration || 0), 0);
  $('sum').textContent = state.picked.length
    ? `${state.picked.length} clips, ${hms(total)}` : 'nothing picked';
  $('build').disabled = state.picked.length === 0;
  $('previewBtn').disabled = state.picked.length === 0;
  $('checkBtn').disabled = state.picked.length === 0;

  const head = parseFloat($('trimHead').value) || 0;
  const tail = parseFloat($('trimTail').value) || 0;
  box.innerHTML = state.picked.map((id, i) => {
    const v = state.map.get(id) || { title: id };
    const t = state.trims[id];
    const custom = !!t;
    const th = custom ? t.start : head;
    const tt = custom ? t.end : tail;
    return `<div class="row" draggable="true" data-i="${i}">
      <div class="n">${i + 1}</div>
      <div class="nm" title="${esc(v.title)}">${esc(v.title)}</div>
      <button class="tr${custom ? ' on' : ''}" data-trim="${esc(id)}"
        title="Trim this clip">&#9986;</button>
      <button class="rm" data-id="${esc(id)}" title="Remove">&times;</button>
    </div>` + (custom ? `<div class="trimbox">
      start <input type="number" min="0" step="0.5" value="${th}" data-th="${esc(id)}">
      end <input type="number" min="0" step="0.5" value="${tt}" data-tt="${esc(id)}">
      <button class="rm" data-untrim="${esc(id)}" title="Use the global trim">&times;</button>
    </div>` : '');
  }).join('');
  updatePreview();
}

$('picked').addEventListener('click', e => {
  const t = e.target;
  if (t.dataset.trim !== undefined) {
    const id = t.dataset.trim;
    if (state.trims[id]) delete state.trims[id];
    else state.trims[id] = { start: parseFloat($('trimHead').value) || 0,
                             end: parseFloat($('trimTail').value) || 0 };
    renderTray();
    return;
  }
  if (t.dataset.untrim !== undefined) { delete state.trims[t.dataset.untrim]; renderTray(); return; }
  if (t.classList.contains('rm') && t.dataset.id) toggle(t.dataset.id);
});
$('picked').addEventListener('input', e => {
  const t = e.target;
  const id = t.dataset.th || t.dataset.tt;
  if (!id || !state.trims[id]) return;
  const key = t.dataset.th ? 'start' : 'end';
  state.trims[id][key] = parseFloat(t.value) || 0;
});

// drag to reorder
let dragFrom = null;
$('picked').addEventListener('dragstart', e => {
  const row = e.target.closest('.row'); if (!row) return;
  dragFrom = +row.dataset.i; row.classList.add('drag');
  e.dataTransfer.effectAllowed = 'move';
});
$('picked').addEventListener('dragend', e => {
  const row = e.target.closest('.row'); if (row) row.classList.remove('drag');
  document.querySelectorAll('.row.over').forEach(r => r.classList.remove('over'));
});
$('picked').addEventListener('dragover', e => {
  e.preventDefault();
  const row = e.target.closest('.row'); if (!row) return;
  document.querySelectorAll('.row.over').forEach(r => r.classList.remove('over'));
  row.classList.add('over');
});
$('picked').addEventListener('drop', e => {
  e.preventDefault();
  const row = e.target.closest('.row');
  if (!row || dragFrom === null) return;
  const to = +row.dataset.i;
  const [moved] = state.picked.splice(dragFrom, 1);
  state.picked.splice(to, 0, moved);
  dragFrom = null;
  render(); renderTray();
});

function updatePreview() {
  const tpl = $('tpl').value || 'Story {n}';
  const first = state.picked.length ? state.map.get(state.picked[0]) : null;
  $('tplPrev').textContent = tpl
    .replace(/\{n\}/g, '1').replace(/\{index\}/g, '1')
    .replace(/\{total\}/g, String(state.picked.length || 1))
    .replace(/\{title\}/g, first ? first.title : 'Title');
}

// ------------------------------------------------------------------ build

$('build').addEventListener('click', async () => {
  const name = $('bname').value.trim() ||
    `compilation-${new Date().toISOString().slice(0, 10)}`;
  $('build').disabled = true;
  try {
    const res = await api('/api/builds', {
      method: 'POST',
      body: JSON.stringify({
        name, video_ids: state.picked,
        label: { template: $('tpl').value || 'Story {n}',
                 position: $('pos').value,
                 duration: parseFloat($('dur').value) || 0,
                 box: $('lblStyle').value === 'box',
                 all_caps: $('lblStyle').value === 'box',
                 fade_in: parseFloat($('fadeIn').value) || 0,
                 fade_out: parseFloat($('fadeOut').value) || 0 },
        video: { aspect: $('aspect').value, fill: $('fill').value },
        trim: { start: parseFloat($('trimHead').value) || 0,
                end: parseFloat($('trimTail').value) || 0 },
        trims: state.trims,
        test: $('testBuild').checked,
      }),
    });
    openBuild(res.id);
  } catch (err) {
    toast('Could not start: ' + err.message);
    $('build').disabled = false;
  }
});

function modal(html) {
  closeModal();
  const mask = document.createElement('div');
  mask.className = 'mask'; mask.id = 'mask';
  mask.innerHTML = `<div class="modal">${html}</div>`;
  mask.addEventListener('click', e => { if (e.target === mask) closeModal(); });
  document.body.appendChild(mask);
  return mask;
}
function closeModal() {
  const m = $('mask'); if (m) m.remove();
  if (state.polling) { clearInterval(state.polling); state.polling = null; }
}

const STAGES = ['download', 'encode', 'assemble', 'done'];

async function openBuild(id) {
  modal('<div class="mh"><b>Building</b><button class="btn sm ghost" onclick="closeModal()">Close</button></div><div class="mb" id="bbody">Starting...</div>');
  const tick = async () => {
    let b;
    try { b = await api('/api/builds/' + id); }
    catch (e) { return; }
    const pct = b.total_clips ? Math.round(100 * b.done_clips / b.total_clips) : 0;
    const si = STAGES.indexOf(b.stage);
    const stages = STAGES.map((s, i) => {
      const cls = b.status === 'done' ? 'done'
        : (i < si ? 'done' : (i === si ? 'on' : ''));
      return `<div class="stage ${cls}">${s}</div>`;
    }).join('');
    const failed = (b.items || []).filter(it => it.status === 'failed');
    const replaced = (b.items || []).filter(it => it.status !== 'failed' && (it.note || '').startsWith('replaced'));
    let foot = '';
    if (b.status === 'done') {
      foot = `<div class="note">Finished in ${hms((b.finished_at - b.started_at) || 0)}.
        Runtime ${hms(b.duration_s)}, ${size(b.output_bytes)}.</div>
        <div style="display:flex;gap:8px;margin-top:12px">
        <a class="btn primary" href="/api/builds/${id}/download?file=video">Download video</a>
        <a class="btn" href="/api/builds/${id}/download?file=chapters">Chapters</a>
        <a class="btn" href="/api/builds/${id}/download?file=manifest">Manifest</a></div>`;
      state.picked = []; render(); renderTray(); loadVideos();
    } else if (b.status === 'failed') {
      foot = `<div class="warnbox">Build failed: ${esc((b.error || '').slice(0, 400))}</div>`;
    } else if (b.status === 'cancelled') {
      foot = `<div class="warnbox">Cancelled.</div>`;
    } else {
      foot = `<button class="btn sm" onclick="cancelBuild(${id})">Cancel build</button>`;
    }
    if (failed.length) {
      foot += `<div class="warnbox">${failed.length} clip(s) skipped:<br>` +
        failed.slice(0, 5).map(f => esc(f.video_title || f.video_id) + (skipWhy(b, f) ? ' <span style="opacity:.75">— ' + esc(skipWhy(b, f)) + '</span>' : '')).join('<br>') + '</div>';
    }
    if (replaced.length) {
      foot += `<div class="note" style="margin-top:10px">${replaced.length} clip(s) swapped in automatically:<br>` +
        replaced.slice(0, 8).map(r => `Story ${r.position + 1}: ${esc(r.video_title || r.video_id)} <span style="opacity:.7">(${esc(r.note)})</span>`).join('<br>') + '</div>';
    }
    $('bbody').innerHTML = `<b>${esc(b.name)}</b>
      <div class="stages">${stages}</div>
      <div class="bar"><i style="width:${b.status === 'done' ? 100 : pct}%"></i></div>
      <div class="note">${esc(b.message || '')} &middot; ${b.done_clips}/${b.total_clips}</div>
      ${foot}`;
    if (['done', 'failed', 'cancelled'].includes(b.status)) {
      clearInterval(state.polling); state.polling = null;
      $('build').disabled = state.picked.length === 0;
    }
  };
  await tick();
  state.polling = setInterval(tick, 1500);
}

async function cancelBuild(id) {
  await api(`/api/builds/${id}/cancel`, { method: 'POST' });
  toast('Cancelling after the current clip');
}

$('showBuilds').addEventListener('click', async () => {
  const { builds } = await api('/api/builds');
  const rows = builds.length ? builds.map(b => `
    <div class="bitem">
      <div class="dot ${b.status}"></div>
      <div style="flex:1">
        <div>${esc(b.name)}</div>
        <div style="font-size:11px;color:var(--dim2)">
          ${b.total_clips} clips &middot; ${b.status}
          ${b.duration_s ? ' &middot; ' + hms(b.duration_s) : ''}</div>
      </div>
      <button class="btn sm" onclick="closeModal();openBuild(${b.id})">Open</button>
    </div>`).join('') : '<div class="note">No builds yet.</div>';
  modal(`<div class="mh"><b>Builds</b><button class="btn sm ghost" onclick="closeModal()">Close</button></div>
    <div class="mb blist">${rows}</div>`);
});

// --------------------------------------------------------------- channels

$('addChan').addEventListener('click', () => {
  modal(`<div class="mh"><b>Add a channel</b><button class="btn sm ghost" onclick="closeModal()">Close</button></div>
    <div class="mb">
      <div class="field"><label>YouTube channel or handle</label>
        <input id="newUrl" placeholder="@HisYTStory"></div>
      <label class="chk"><input type="checkbox" id="inclLong"> Also list long form videos</label>
      <div class="note">Shorts are always listed. Listing is metadata only, nothing downloads until you build.</div>
      <button class="btn primary" style="width:100%;margin-top:14px" onclick="doAddChannel()">Add and sync</button>
    </div>`);
  setTimeout(() => $('newUrl').focus(), 50);
});

async function doAddChannel() {
  const url = $('newUrl').value.trim();
  if (!url) return;
  const kind = (url.startsWith('/') || url.startsWith('~')) ? 'local' : 'youtube';
  try {
    const res = await api('/api/channels', {
      method: 'POST',
      body: JSON.stringify({ url, kind, include_long: $('inclLong').checked }),
    });
    state.channel = res.id;
    closeModal();
    toast('Syncing the channel, this can take a minute');
    loadChannels();
  } catch (err) { toast('Failed: ' + err.message); }
}

$('sync').addEventListener('click', () => {
  if (!state.channel) return;
  modal(`<div class="mh"><b>Re-sync this channel</b>
      <button class="btn sm ghost" onclick="closeModal()">Close</button></div>
    <div class="mb">
      <label class="chk"><input type="checkbox" id="syncLong" checked> Also list long form videos</label>
      <div class="note">Shorts are always listed. Tick the box to pull in the long
        form uploads too. Clips you already have keep their usage counts.</div>
      <button class="btn primary" style="width:100%;margin-top:14px" id="doSync">Sync now</button>
    </div>`);
  $('doSync').addEventListener('click', async () => {
    await api(`/api/channels/${state.channel}/sync`, {
      method: 'POST',
      body: JSON.stringify({ include_long: $('syncLong').checked }),
    });
    closeModal();
    toast('Syncing, the grid will fill in as it goes');
    loadChannels();
  });
});

// ---------------------------------------------------------------- wiring

$('chan').addEventListener('change', e => {
  state.channel = e.target.value; loadVideos();
});
['month', 'shorts', 'sort', 'minViews', 'dateFrom', 'dateTo'].forEach(id =>
  $(id).addEventListener('change', loadVideos));
$('unused').addEventListener('change', loadVideos);
$('tpl').addEventListener('input', updatePreview);
['trimHead','trimTail'].forEach(id => $(id).addEventListener('input', renderTray));

let qTimer;
$('q').addEventListener('input', () => {
  clearTimeout(qTimer); qTimer = setTimeout(loadVideos, 260);
});

$('selAll').addEventListener('click', () => {
  state.videos.forEach(v => {
    if (!state.picked.includes(v.id)) state.picked.push(v.id);
  });
  render(); renderTray();
});
$('clearSel').addEventListener('click', () => {
  state.picked = []; state.trims = {}; render(); renderTray();
});

window.closeModal = closeModal;
window.loadVideos = loadVideos; window.state = state; window.api = api; window.render = render; window.renderTray = renderTray;
window.modal = modal; window.toast = toast; window.esc = esc; window.hms = hms; window.views = views;
window.openBuild = openBuild;
window.cancelBuild = cancelBuild;
window.doAddChannel = doAddChannel;

(async function init() {
  await loadChannels();
  await loadVideos();
  renderTray();
})();

// ============================================================ views

function showView(name) {
  ['Library', 'Recipes', 'Streams'].forEach(v => {
    $('view' + v).hidden = (v !== name);
  });
  document.querySelectorAll('.navb').forEach(b =>
    b.classList.toggle('on', b.dataset.view === name));
  if (name === 'Recipes') loadRecipes();
  if (name === 'Streams') loadStreams();
}
document.querySelectorAll('.navb').forEach(b =>
  b.addEventListener('click', () => showView(b.dataset.view)));

// ============================================================ recipes

const rState = { list: [], current: null, defaults: {}, previewTimer: null };

function windowLabel(w) {
  if (!w || !w.from) return 'any';
  if (w.from === w.to) return w.from.slice(5);
  const days = (new Date(w.to) - new Date(w.from)) / 86400000;
  // A whole calendar month reads better as the month; anything shorter or
  // ragged should show the actual dates it covers.
  const wholeMonth = w.from.endsWith('-01') && days > 26 && days < 32
    && w.from.slice(0, 7) === w.to.slice(0, 7);
  return wholeMonth ? w.from.slice(0, 7) : `${w.from.slice(5)} to ${w.to.slice(5)}`;
}

function fmtWhen(ts) {
  if (!ts) return 'not scheduled';
  const d = new Date(ts * 1000);
  return d.toLocaleString(undefined,
    { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
}

async function loadRecipes() {
  const data = await api('/api/recipes');
  rState.list = data.recipes;
  rState.defaults = data.defaults;
  const box = $('recipeList');
  box.innerHTML = rState.list.length ? rState.list.map(r => `
    <div class="rcard${rState.current && rState.current.id === r.id ? ' on' : ''}"
         data-id="${r.id}">
      <div class="rn">${esc(r.name)}
        ${r.schedule !== 'off' && r.enabled ? '<span class="pill used" style="position:static">auto</span>' : ''}
      </div>
      <div class="rs">
        <b>${esc(r.channel_title || r.channel_id)}</b><br>
        ${r.schedule === 'off' ? 'manual only' : `${r.schedule}, next ${fmtWhen(r.next_run)}`}
        ${r.last_status ? '<br>' + esc(r.last_status) : ''}
      </div>
    </div>`).join('') : '<div class="hintbox" style="border:0">No recipes yet.</div>';
  box.querySelectorAll('.rcard').forEach(c =>
    c.addEventListener('click', () => editRecipe(+c.dataset.id)));
  if (!rState.current && rState.list.length) editRecipe(rState.list[0].id);
  else if (!rState.list.length) newRecipe();
}

function newRecipe() {
  rState.current = {
    id: null, name: '', channel_id: state.channel, enabled: true,
    rules: Object.assign({}, rState.defaults),
    label: { template: 'Story {n}', position: 'top', duration: 5,
             box: true, all_caps: true, fade_in: 0.5, fade_out: 0.05 },
    output: { path: '', post_command: '' },
    schedule: 'off', schedule_day: 1, schedule_hour: 4,
  };
  renderEditor();
}
function editRecipe(id) {
  const r = rState.list.find(x => x.id === id);
  if (!r) return;
  rState.current = {
    id: r.id, name: r.name, channel_id: r.channel_id, enabled: !!r.enabled,
    rules: Object.assign({}, rState.defaults, r.rules || {}),
    label: Object.assign({ template: 'Story {n}', position: 'top', duration: 5 }, r.label || {}),
    output: Object.assign({ path: '', post_command: '' }, r.output || {}),
    schedule: r.schedule, schedule_day: r.schedule_day, schedule_hour: r.schedule_hour,
  };
  loadRecipes();
  renderEditor();
}

function opts(list, cur) {
  return list.map(([v, l]) =>
    `<option value="${esc(v)}"${String(v) === String(cur) ? ' selected' : ''}>${esc(l)}</option>`).join('');
}

function renderEditor() {
  const c = rState.current;
  if (!c) { $('recipeEditor').innerHTML = ''; return; }
  const R = c.rules;
  const picked = c.channel_ids && c.channel_ids.length ? c.channel_ids : [c.channel_id];
  const chanPick = state.channels.map(ch =>
    `<label><input type="checkbox" class="rch" value="${esc(ch.id)}"
      ${picked.includes(ch.id) ? 'checked' : ''}> ${esc(ch.title || ch.handle)}</label>`).join('')
    || '<span style="color:var(--dim2);font-size:12px">Add a channel first.</span>';

  $('recipeEditor').innerHTML = `
    <div class="rsec">
      <h3>${c.id ? 'Edit recipe' : 'New recipe'}</h3>
      <p class="sub">Set the rule once. It picks the clips and the order every time it runs.</p>
      <div class="rgrid">
        <div class="field"><label>Name</label>
          <input id="rName" value="${esc(c.name)}" placeholder="His Story monthly"></div>
      </div>
      <div class="field" style="margin-top:10px">
        <label>Channels (one compilation per channel)</label>
        <div class="chanpick" id="rChans">${chanPick}</div>
        <div class="chantot" id="rChanTot"></div>
      </div>
    </div>

    <div class="rsec">
      <h3>Which clips qualify</h3>
      <p class="sub">Leave a box empty to ignore it.</p>
      <div class="rgrid">
        <div class="field"><label>Date window</label>
          <select id="rMonth">${opts([['','Any date'],['yesterday','Yesterday'],['today','Today'],['last_7_days','Last 7 days'],['last_14_days','Last 14 days'],['last_30_days','Last 30 days'],['last_month','Last month'],['this_month','This month']], R.month)}</select></div>
        <div class="field"><label>Minimum views</label>
          <input id="rMinViews" type="number" min="0" step="100000" value="${R.min_views || 0}"></div>
        <div class="field"><label>Maximum views</label>
          <input id="rMaxViews" type="number" min="0" step="100000" value="${R.max_views || 0}"></div>
        <div class="field"><label>Type</label>
          <select id="rType">${opts([['only','Shorts only'],['','All videos'],['exclude','Long form only']], R.type)}</select></div>
        <div class="field"><label>Already used</label>
          <select id="rMaxUsed">${opts([['','Any'],['0','Never used'],['1','Once or less'],['2','Twice or less']], R.max_used === null ? '' : String(R.max_used))}</select></div>
        <div class="field"><label>Title contains</label>
          <input id="rSearch" value="${esc(R.search || '')}" placeholder="optional"></div>
      </div>
    </div>

    <div class="rsec">
      <h3>How many to take</h3>
      <div class="rgrid">
        <div class="field"><label>Pick by</label>
          <select id="rSelect">${opts([['top_views','Best performing'],['newest','Newest'],['oldest','Oldest'],['random','Random draw']], R.select)}</select></div>
        <div class="field"><label>Target runtime (hours)</label>
          <input id="rHours" type="number" min="0" step="0.5" value="${R.target_hours || 0}"></div>
        <div class="field"><label>Hard cap on clips</label>
          <input id="rLimit" type="number" min="0" value="${R.limit || 0}"></div>
      </div>
    </div>

    <div class="rsec">
      <h3>What order they go in</h3>
      <p class="sub">Spread puts the strongest clip first, the second strongest last,
      and paces the rest of the top performers evenly through the middle so it never
      sags. The remainder is shuffled between them.</p>
      <div class="rgrid">
        <div class="field"><label>Order</label>
          <select id="rOrder">${opts([['spread','Spread the good ones'],['random','Pure random'],['views_desc','Best first'],['date_desc','Newest first'],['date_asc','Oldest first']], R.order)}</select></div>
        <div class="field"><label>A strong one every N clips</label>
          <input id="rAnchor" type="number" min="2" max="20" value="${R.anchor_every || 5}"></div>
        <div class="field"><label>Shuffle seed (0 = new each run)</label>
          <input id="rSeed" type="number" min="0" value="${R.seed || 0}"></div>
      </div>
    </div>

    <div class="rsec">
      <h3>Label and output</h3>
      <div class="rgrid">
        <div class="field"><label>Label template</label>
          <input id="rTpl" value="${esc(c.label.template || 'Story {n}')}"></div>
        <div class="field"><label>Position</label>
          <select id="rPos">${opts([['top','Top'],['top-left','Top left'],['top-right','Top right'],['center','Center'],['bottom','Bottom']], c.label.position)}</select></div>
        <div class="field"><label>Seconds on screen</label>
          <input id="rDur" type="number" min="0" step="0.5" value="${c.label.duration ?? 5}"></div>
        <div class="field"><label>Label style</label>
          <select id="rStyle">${opts([['box','Plate'],['outline','Outline']], c.label.box === false ? 'outline' : 'box')}</select></div>
        <div class="field"><label>Fade in (s)</label>
          <input id="rFadeIn" type="number" min="0" step="0.05" value="${c.label.fade_in ?? 0.5}"></div>
        <div class="field"><label>Fade out (s)</label>
          <input id="rFadeOut" type="number" min="0" step="0.05" value="${c.label.fade_out ?? 0.05}"></div>
        <div class="field"><label>Shape</label>
          <select id="rAspect">${opts([['9:16','9:16 vertical'],['16:9','16:9 wide'],['1:1','1:1 square'],['4:5','4:5 feed']], (c.video && c.video.aspect) || '9:16')}</select></div>
        <div class="field"><label>Fill the sides</label>
          <select id="rFill">${opts([['blur','Blurred, no bars'],['pad','Black bars'],['crop','Crop to fill']], (c.video && c.video.fill) || 'blur')}</select></div>
        <div class="field"><label>Trim off start (s)</label>
          <input id="rTrimS" type="number" min="0" step="0.5" value="${R.trim_start || 0}"></div>
        <div class="field"><label>Trim off end (s)</label>
          <input id="rTrimE" type="number" min="0" step="0.5" value="${R.trim_end || 0}"></div>
        <div class="field" style="grid-column:1/-1"><label>Output path (blank = default folder)</label>
          <input id="rOut" value="${esc(c.output.path || '')}" placeholder="/srv/streams/his-story/compilation.mp4"></div>
        <div class="field" style="grid-column:1/-1"><label>Run after the build (optional)</label>
          <input id="rPost" value="${esc(c.output.post_command || '')}" placeholder="systemctl restart stream@his-story"></div>
      </div>
    </div>

    <div class="rsec">
      <h3>Run it automatically</h3>
      <div class="rgrid">
        <div class="field"><label>Schedule</label>
          <select id="rSched">${opts([['off','Manual only'],['monthly','Every month'],['weekly','Every week'],['daily','Every day']], c.schedule)}</select></div>
        <div class="field"><label>Day</label>
          <input id="rDay" type="number" min="0" max="28" value="${c.schedule_day}"></div>
        <div class="field"><label>Hour</label>
          <input id="rHour" type="number" min="0" max="23" value="${c.schedule_hour}"></div>
      </div>
      <label class="chk" style="margin-top:8px"><input type="checkbox" id="rEnabled" ${c.enabled ? 'checked' : ''}> Enabled</label>
    </div>

    <div class="dot-sep"></div>

    <div class="rsec">
      <h3>What this would pick right now</h3>
      <p class="sub">Updates as you change the rule. Nothing is built until you say so.</p>
      <div class="preview" id="rPreview"><div class="legend">Loading...</div></div>
      <div style="display:flex;gap:8px;margin-top:14px;flex-wrap:wrap">
        <button class="btn primary" id="rSave">${c.id ? 'Save recipe' : 'Create recipe'}</button>
        <button class="btn" id="rRun"${c.id ? '' : ' disabled'}>Build it now</button>
        <button class="btn" id="rTest"${c.id ? '' : ' disabled'} title="Builds it without counting any clip as used">Test build</button>
        <button class="btn ghost" id="rNewFromHere">New recipe</button>
        ${c.id ? '<div class="spacer"></div><button class="btn ghost" id="rDel">Delete</button>' : ''}
      </div>
    </div>`;

  ['rName','rChan','rMonth','rMinViews','rMaxViews','rType','rMaxUsed','rSearch',
   'rSelect','rHours','rLimit','rOrder','rAnchor','rSeed','rTpl','rPos','rDur',
   'rOut','rPost','rSched','rDay','rHour','rEnabled','rTrimS','rTrimE',
   'rAspect','rFill','rStyle','rFadeIn','rFadeOut'].forEach(id => {
    const el = $(id); if (!el) return;
    el.addEventListener('input', collectAndPreview);
    el.addEventListener('change', collectAndPreview);
  });
  document.querySelectorAll('.rch').forEach(el =>
    el.addEventListener('change', collectAndPreview));
  $('rSave').addEventListener('click', saveRecipe);
  $('rRun').addEventListener('click', () => runRecipe(false));
  $('rTest').addEventListener('click', () => runRecipe(true));
  $('rNewFromHere').addEventListener('click', newRecipe);
  if ($('rDel')) $('rDel').addEventListener('click', deleteRecipe);
  collectAndPreview();
}

function collect() {
  const c = rState.current;
  const num = id => parseFloat($(id).value) || 0;
  const usedRaw = $('rMaxUsed').value;
  c.name = $('rName').value.trim();
  c.channel_ids = Array.from(document.querySelectorAll('.rch:checked')).map(e => e.value);
  c.channel_id = c.channel_ids[0] || c.channel_id;
  c.enabled = $('rEnabled').checked;
  c.schedule = $('rSched').value;
  c.schedule_day = parseInt($('rDay').value, 10) || 1;
  c.schedule_hour = parseInt($('rHour').value, 10) || 0;
  c.rules = Object.assign({}, c.rules, {
    month: $('rMonth').value,
    min_views: num('rMinViews'), max_views: num('rMaxViews'),
    type: $('rType').value,
    max_used: usedRaw === '' ? null : parseInt(usedRaw, 10),
    search: $('rSearch').value.trim(),
    select: $('rSelect').value,
    target_hours: num('rHours'), limit: num('rLimit'),
    order: $('rOrder').value,
    anchor_every: parseInt($('rAnchor').value, 10) || 5,
    seed: parseInt($('rSeed').value, 10) || 0,
    trim_start: num('rTrimS'), trim_end: num('rTrimE'),
  });
  const plate = $('rStyle').value === 'box';
  c.label = { template: $('rTpl').value || 'Story {n}',
              position: $('rPos').value, duration: num('rDur'),
              box: plate, all_caps: plate,
              fade_in: num('rFadeIn'), fade_out: num('rFadeOut') };
  c.video = { aspect: $('rAspect').value, fill: $('rFill').value };
  c.output = { path: $('rOut').value.trim(), post_command: $('rPost').value.trim() };
  return c;
}

function collectAndPreview() {
  collect();
  clearTimeout(rState.previewTimer);
  rState.previewTimer = setTimeout(runPreview, 320);
}

async function runPreview() {
  const c = rState.current;
  if (!c || !(c.channel_ids || []).length) {
    if ($('rPreview')) $('rPreview').innerHTML =
      '<div class="legend">Pick at least one channel.</div>';
    if ($('rChanTot')) $('rChanTot').innerHTML = '';
    return;
  }
  let data;
  try {
    data = await api('/api/recipes/preview', {
      method: 'POST',
      body: JSON.stringify({ channel_ids: c.channel_ids && c.channel_ids.length
                               ? c.channel_ids : [c.channel_id],
                             rules: c.rules }),
    });
  } catch (err) {
    $('rPreview').innerHTML = `<div class="legend">Preview failed: ${esc(err.message)}</div>`;
    return;
  }
  const sample = data.sample || [];
  const vals = sample.map(s => s.views || 0).sort((a, b) => b - a);
  const cut = vals.length ? vals[Math.max(0, Math.floor(vals.length / 4) - 1)] : 0;
  const rows = sample.map((s, i) => `
    <div class="prow${(s.views || 0) >= cut && cut > 0 ? ' anchor' : ''}">
      <div class="pi">${i + 1}</div>
      <div class="pt">${esc(s.title || s.id)}</div>
      <div class="pv">${views(s.views)}</div>
    </div>`).join('');

  if ($('rChanTot')) {
    $('rChanTot').innerHTML = (data.channels || []).map(ch =>
      `<span class="chanchip${ch.selected ? '' : ' zero'}">${esc(ch.channel)}
        <b>${ch.selected}</b></span>`).join('');
  }
  $('rPreview').innerHTML = `
    <div class="pstats">
      <div class="pstat"><div class="v">${data.qualified}</div><div class="k">qualify</div></div>
      <div class="pstat"><div class="v">${data.selected}</div><div class="k">selected</div></div>
      <div class="pstat"><div class="v">${hms(data.runtime)}</div><div class="k">runtime</div></div>
      <div class="pstat"><div class="v">${windowLabel(data.window)}</div><div class="k">window</div></div>
    </div>
    ${sample.length ? `<div class="ptable">${rows}</div>
      <div class="legend"><b>Green</b> marks the top quarter by views. With spread
      ordering they land evenly down the list instead of bunching at the top.
      ${data.sample_channel ? `Order shown is for <b style="color:var(--text)">${esc(data.sample_channel)}</b>.` : ''}
      ${data.selected > sample.length ? `Showing ${sample.length} of ${data.selected}.` : ''}</div>`
    : '<div class="legend">Nothing matches this rule yet. Loosen the filters, or sync the channel.</div>'}`;
}

async function saveRecipe() {
  const c = collect();
  if (!c.name) { toast('Give the recipe a name'); return; }
  try {
    const res = await api('/api/recipes', { method: 'POST', body: JSON.stringify(c) });
    rState.current.id = res.id;
    toast(res.next_run ? 'Saved, next run ' + fmtWhen(res.next_run) : 'Saved');
    await loadRecipes();
    editRecipe(res.id);
  } catch (err) { toast('Could not save: ' + err.message); }
}

async function runRecipe(test) {
  const c = rState.current;
  if (!c || !c.id) return;
  try {
    const res = await api(`/api/recipes/${c.id}/run${test === true ? '?test=1' : ''}`, { method: 'POST' });
    const n = (res.builds || []).length;
    if (n > 1) toast(`Started ${n} compilations, one per channel`);
    showView('Library');
    openBuild(res.build_id);
  } catch (err) { toast(err.message); }
}

async function deleteRecipe() {
  const c = rState.current;
  if (!c || !c.id) return;
  await api(`/api/recipes/${c.id}`, { method: 'DELETE' });
  rState.current = null;
  toast('Recipe deleted');
  loadRecipes();
}

$('newRecipe').addEventListener('click', newRecipe);

// ============================================================ streams

async function loadStreams() {
  let data;
  try { data = await api('/api/streams'); }
  catch (err) {
    $('streamList').innerHTML = `<div class="hintbox">Could not read streams: ${esc(err.message)}</div>`;
    return;
  }
  $('streamPattern').value = data.pattern || '';
  if (data.error) {
    $('streamList').innerHTML = `<div class="hintbox" style="border:0">
      <b style="color:var(--bad)">systemd could not be reached.</b><br>
      ${esc(data.error)}<br><br>
      On a normal VPS this means the dashboard is not running as root, or it is
      inside a container without systemd. The rest of storystack works either way.</div>`;
    return;
  }
  const list = (data.streams || []).filter(s => s.unit);
  if (!list.length) {
    $('streamList').innerHTML = `<div class="hintbox" style="border:0">
      No units match <code>${esc(data.pattern)}</code>.
      Set the pattern to whatever your stream services are called, for example
      <code>stream@*.service</code> or <code>ffmpeg-*.service</code>.</div>`;
    return;
  }
  $('streamList').innerHTML = list.map(s => `
    <div class="scard" data-unit="${esc(s.unit)}">
      <div>
        <div class="sname">${esc(s.channel || s.unit)}</div>
        <div class="smeta">${esc(s.unit)}${s.source ? ' &middot; ' + esc(s.source) : ''}</div>
      </div>
      <div class="spacer"></div>
      <div class="smeta">${s.uptime ? 'up ' + hms(s.uptime) : ''}${s.restarts ? ' &middot; ' + s.restarts + ' restarts' : ''}</div>
      <span class="state ${esc(s.state)}">${esc(s.state)}${s.sub && s.sub !== s.state ? ' / ' + esc(s.sub) : ''}</span>
      <button class="btn sm" data-act="restart">Restart</button>
      <button class="btn sm" data-act="${s.state === 'active' ? 'stop' : 'start'}">${s.state === 'active' ? 'Stop' : 'Start'}</button>
      <button class="btn sm ghost" data-act="log">Log</button>
    </div>`).join('');

  $('streamList').querySelectorAll('.scard').forEach(card => {
    card.querySelectorAll('button[data-act]').forEach(btn => {
      btn.addEventListener('click', async () => {
        const unit = card.dataset.unit, act = btn.dataset.act;
        if (act === 'log') {
          const old = card.parentElement.querySelector('.logbox');
          if (old) old.remove();
          const { log } = await api(`/api/streams/${encodeURIComponent(unit)}/log`);
          const box = document.createElement('div');
          box.className = 'logbox'; box.textContent = log || '(empty)';
          card.appendChild(box);
          return;
        }
        btn.disabled = true;
        try {
          await api(`/api/streams/${encodeURIComponent(unit)}/${act}`, { method: 'POST' });
          toast(`${act} sent to ${unit}`);
          setTimeout(loadStreams, 1200);
        } catch (err) { toast(err.message); btn.disabled = false; }
      });
    });
  });
}

$('refreshStreams').addEventListener('click', loadStreams);
$('savePattern').addEventListener('click', async () => {
  await api('/api/streams/pattern', {
    method: 'POST', body: JSON.stringify({ pattern: $('streamPattern').value.trim() }),
  });
  toast('Pattern saved');
  loadStreams();
});

window.showView = showView;


// ============================================================ settings

$('showSettings').addEventListener('click', openSettings);

async function openSettings() {
  const st = await api('/api/settings');
  const where = st.yt_api_key_source === 'environment'
    ? 'set in the service environment file'
    : (st.yt_api_key_set ? 'saved here' : 'not set');
  const cookieState = st.cookies_set
    ? `<span style="color:var(--good)">Loaded</span>${st.cookie_age_days != null
        ? ` &middot; ${st.cookie_age_days} days old${st.cookie_age_days > 25
            ? ' <span style="color:var(--warn)">(may have expired)</span>' : ''}` : ''}`
    : `<span style="color:var(--warn)">Not set</span>`;

  modal(`<div class="mh"><b>Settings</b>
      <button class="btn sm ghost" onclick="closeModal()">Close</button></div>
    <div class="mb">

      <div class="note" style="color:var(--text);font-size:13px"><b>Downloads</b></div>
      <div class="note">Start here if builds are failing. This tries one real clip
        from your library and tells you exactly what YouTube said.</div>
      <div class="btnrow" style="display:flex;gap:8px;margin:10px 0">
        <button class="btn primary" id="runDiag">Test download</button>
      </div>
      <div id="diagOut"></div>

      <div class="note" style="margin-top:18px">JavaScript runtime:
        ${st.js_runtime
          ? `<span style="color:var(--good)">${esc(st.js_runtime)}</span>`
          : '<span style="color:var(--bad)">missing, downloads will fail</span>'}
      </div>
      <div class="note">yt-dlp needs one for YouTube. Without it formats come back
        incomplete however everything else is set. The setup script below installs it.</div>

      <div class="note" style="margin-top:18px">Token provider:
        ${st.pot_running
          ? '<span style="color:var(--good)">running</span>'
          : (st.pot_plugin
             ? '<span style="color:var(--warn)">plugin installed but not running</span>'
             : '<span style="color:var(--warn)">not installed</span>')}
      </div>
      <div class="note">YouTube requires a proof-of-origin token for its better
        formats on most player clients. A desktop app gets one from the browser
        beside it; a server runs its own. This is the normal setup for
        server-side downloading. To install it, one command on the VPS:
        <span class="mono">sudo /opt/storystack/setup-potoken.sh</span></div>

      <div class="note" style="margin-top:18px">YouTube cookies: ${cookieState}</div>
      <div class="note">YouTube blocks most datacenter IPs, so a VPS usually needs
        these. Install a "Get cookies.txt" extension, open YouTube while signed in,
        export, then pick the file here. They expire every few weeks.</div>
      <div class="btnrow" style="display:flex;gap:8px;margin-top:10px;align-items:center">
        <input type="file" id="cookieFile" accept=".txt" style="font-size:12px">
        ${st.cookies_set ? '<button class="btn sm ghost" id="clearCookies">Remove</button>' : ''}
      </div>

      <div class="field" style="margin-top:18px"><label>Proxy (only if cookies are not enough)</label>
        <input id="proxyUrl" placeholder="http://user:pass@host:port"
          value="${st.proxy_set ? '' : ''}"></div>
      <div class="note">${st.proxy_set ? 'A proxy is currently set. ' : ''}A residential
        proxy is the reliable way past an IP block when cookies alone do not do it.
        Leave blank to go direct.</div>

      <div class="field" style="margin-top:18px"><label>Download quality</label>
        <select id="maxH">
          <option value="1080"${st.max_height == 1080 ? ' selected' : ''}>Up to 1080p</option>
          <option value="720"${st.max_height == 720 ? ' selected' : ''}>Up to 720p</option>
          <option value="1440"${st.max_height == 1440 ? ' selected' : ''}>Up to 1440p</option>
          <option value="2160"${st.max_height == 2160 ? ' selected' : ''}>Up to 2160p</option>
        </select></div>
      <div class="note">Best stream at or under the cap, stepping down to 720p if
        nothing higher exists.</div>

      <div class="field" style="margin-top:18px"><label>YouTube Data API key (optional)</label>
        <input id="ytKey" type="password" placeholder="${st.yt_api_key_set ? 'leave blank to keep the current key' : 'AIza...'}"></div>
      <div class="note">Currently ${where}. Without a key, filling in the length and
        date of every clip is one lookup per video, which is slow on a big channel.
        With a key it is one call per 50. Free, no billing account needed.</div>

      <div class="note" style="margin-top:16px">Output folder
        <span class="mono">${esc(st.output_dir)}</span> &middot; ${st.cpu_count} CPU cores
        &middot; yt-dlp runs from <span class="mono">/opt/storystack/venv</span></div>

      <div class="btnrow" style="display:flex;gap:8px;margin-top:16px">
        <button class="btn primary" id="saveKey">Save settings</button>
        <div class="spacer"></div>
        <button class="btn ghost" id="resetUsed" title="Sets every clip back to never used">Reset used counts</button>
      </div>
    </div>`);

  $('runDiag').addEventListener('click', async () => {
    const btn = $('runDiag');
    btn.disabled = true; btn.textContent = 'Testing, this takes a moment...';
    $('diagOut').innerHTML = '';
    let r;
    try { r = await api('/api/diagnose', { method: 'POST', body: '{}' }); }
    catch (err) { r = { ok: false, stage: 'request', error: err.message }; }
    btn.disabled = false; btn.textContent = 'Test download';
    const ladder = (r.routes || []).length
      ? `<div class="outbox" style="margin-top:10px;font-size:11.5px">` +
        r.routes.map(rt => rt.ok
          ? `<span style="color:var(--good)">works </span> ${esc(rt.route)}  up to ${rt.best_height}p`
          : `<span style="color:var(--dim2)">no    </span> ${esc(rt.route)}  ${esc((rt.error || '').slice(0, 70))}`
        ).join('\n') + `</div>`
      : '';
    const logBlock = r.log
      ? `<details style="margin-top:10px">
           <summary style="cursor:pointer;font-size:12px;color:var(--dim)">
             Full yt-dlp log (paste this if you need help)</summary>
           <textarea id="diagLog" readonly style="width:100%;height:190px;margin-top:8px;
             background:#07090c;color:var(--dim);border:1px solid var(--line);
             border-radius:8px;padding:10px;font:11px/1.5 ui-monospace,monospace">${esc(r.log)}</textarea>
           <button class="btn sm" id="copyLog" style="margin-top:6px">Copy log</button>
         </details>`
      : '';
    const env = `<div class="note" style="font-size:11px;margin-top:8px">
      yt-dlp ${esc(r.ytdlp_version)} &middot;
      cookies ${r.cookies ? 'loaded' : 'not set'} &middot;
      js runtime ${r.js_runtime ? esc(r.js_runtime) : 'MISSING'} &middot;
      token provider ${r.pot_running ? 'running' : (r.pot_plugin ? 'plugin installed, not running' : 'not installed')}
      ${r.proxy ? '&middot; proxy on' : ''}</div>`;

    if (r.ok) {
      const differs = JSON.stringify(r.working_clients || []) !== JSON.stringify(r.configured_clients || []);
      $('diagOut').innerHTML =
        `<div class="outbox" style="color:var(--good)">Downloads are working.
${esc(r.title || '')}
${r.resolution} &middot; ${hms(r.duration)} &middot; ${r.size_mb} MB
via ${esc(r.working_route)}</div>` + ladder + env + logBlock +
        (differs ? `<button class="btn sm primary" id="applyRoute" style="margin-top:10px">
           Use this route from now on</button>` : '');
      const ap = $('applyRoute');
      if (ap) ap.addEventListener('click', async () => {
        await api('/api/settings', { method: 'POST',
          body: JSON.stringify({ player_clients: r.working_clients || [] }) });
        toast('Saved. Builds will use that route.');
        ap.disabled = true;
      });
    } else {
      $('diagOut').innerHTML =
        `<div class="warnbox"><b>Failed at the ${esc(r.stage)} step.</b><br><br>
${esc(r.error || 'unknown')}</div>` + ladder + env + logBlock;
    }
    const cl = $('copyLog');
    if (cl) cl.addEventListener('click', () => {
      const ta = $('diagLog'); ta.select();
      try { document.execCommand('copy'); toast('Log copied'); }
      catch (e) { toast('Select the text and copy it manually'); }
    });
  });

  $('cookieFile').addEventListener('change', async (e) => {
    const f = e.target.files && e.target.files[0];
    if (!f) return;
    const text = await f.text();
    try {
      await api('/api/settings/cookies', {
        method: 'POST', body: JSON.stringify({ text }),
      });
      toast('Cookies loaded');
      closeModal(); openSettings();
    } catch (err) { toast(err.message, 6000); }
  });

  if ($('clearCookies')) $('clearCookies').addEventListener('click', async () => {
    await api('/api/settings/cookies', { method: 'DELETE' });
    toast('Cookies removed');
    closeModal(); openSettings();
  });

  $('saveKey').addEventListener('click', async () => {
    const body = { max_height: parseInt($('maxH').value, 10) || 1080 };
    const v = $('ytKey').value.trim();
    if (v) body.yt_api_key = v;
    const px = $('proxyUrl').value.trim();
    if (px) body.proxy = px;
    await api('/api/settings', { method: 'POST', body: JSON.stringify(body) });
    closeModal(); toast('Settings saved');
  });
}


// StoryStack fix: say WHY a clip was skipped (reason is stored on the build)
function skipWhy(b, f) {
  let why = f.reason || f.error || f.message || '';
  if (!why) {
    try { why = (Object.fromEntries(JSON.parse(b.error || '[]'))[f.video_id]) || ''; } catch (e) { why = ''; }
  }
  why = String(why).replace(/^(download|encode) failed:\s*/i, '').replace(/^ERROR:\s*/i, '');
  return why.length > 160 ? why.slice(0, 157) + '…' : why;
}
