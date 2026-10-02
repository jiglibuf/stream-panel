'use strict';
const $ = (id) => document.getElementById(id);
let state = {}, ws = null, dragging = null, sigScenes = '', sigSounds = '';

/* ── связь ── */
function connect() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onopen = () => $('dot').classList.add('on');
  ws.onclose = () => {
    $('dot').classList.remove('on');
    $('live').textContent = 'нет связи';
    setTimeout(connect, 1500);
  };
  ws.onerror = () => {};
  ws.onmessage = (e) => {
    const m = JSON.parse(e.data);
    if (m && m.error) { flash(m.error); return; }
    if (m && m.obs !== undefined) { state = m; render(); }
  };
}
function send(cmd, quiet) {
  if (!quiet) buzz();
  if (ws && ws.readyState === 1) ws.send(JSON.stringify(cmd));
  else fetch('/api/cmd', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(cmd) })
    .then(r => r.json()).then(j => { if (j.state) { state = j.state; render(); } });
}
function buzz() { try { navigator.vibrate && navigator.vibrate(12); } catch (e) {} }
function flash(text) {
  const live = $('live');
  const prev = live.textContent;
  live.textContent = text;
  clearTimeout(flash.t);
  flash.t = setTimeout(() => { live.textContent = prev; }, 2200);
}

/* ── отрисовка ── */
function render() {
  const s = state, st = s.stream || {}, aud = s.audio || {};

  // индикатор состояния
  const dot = $('dot');
  dot.className = 'dot' + (st.active ? ' live' : (s.obs ? ' on' : ''));
  $('live').textContent = st.active
    ? `В ЭФИРЕ ${st.time || '00:00:00'}${st.reconnect ? ' · переподключение' : ''}`
    : (s.obs ? 'на связи' : 'OBS офлайн');
  $('obsBadge').className = 'badge obs' + (s.obs ? '' : ' off');
  $('obsBadge').textContent = s.obs ? 'OBS' : 'OBS офлайн';
  const rec = $('recBadge');
  rec.className = 'badge rec' + (s.record && s.record.active ? '' : ' hidden');
  rec.textContent = `● ЗАПИСЬ ${s.record && s.record.active ? s.record.time || '' : ''}`;

  // сцены
  const sig = (s.scenes || []).map(x => x.name).join('|');
  if (sig !== sigScenes) {
    sigScenes = sig;
    const box = $('scenes');
    box.innerHTML = '';
    (s.scenes || []).forEach(sc => {
      const b = document.createElement('button');
      b.className = 'btn scene';
      b.dataset.scene = sc.name;
      b.innerHTML = `${sc.num ? `<span class="num">${sc.num}</span>` : ''}<span class="nm"></span>`;
      b.querySelector('.nm').textContent = sc.name;
      box.appendChild(b);
    });
  }
  document.querySelectorAll('.scene').forEach(b => {
    b.classList.toggle('active', b.dataset.scene === s.scene);
  });

  // транспорт
  const bs = $('btnStream');
  bs.classList.toggle('live', !!st.active);
  bs.textContent = st.active ? 'Остановить эфир' : 'Начать эфир';
  $('btnRecord').classList.toggle('on', !!(s.record && s.record.active));
  $('btnRecord').lastChild.textContent = (s.record && s.record.active) ? ' Стоп записи' : ' Запись';
  $('btnReplay').disabled = !s.replay;

  // звук
  ['mic', 'sys'].forEach(k => {
    const a = aud[k] || {};
    const btn = document.querySelector(`[data-mute="${k}"]`);
    const sl = document.querySelector(`[data-vol="${k}"]`);
    const val = document.querySelector(`[data-val="${k}"]`);
    btn.classList.toggle('muted', a.mute === true);
    btn.disabled = a.mute === null || a.mute === undefined;
    if (a.vol !== null && a.vol !== undefined && dragging !== sl) sl.value = a.vol;
    val.textContent = a.vol === null || a.vol === undefined ? '—' : (dragging === sl ? sl.value : a.vol);
  });

  // звуковая панель
  const sndSig = (s.sounds || []).map(x => x.file + x.playing).join('|');
  const filesSig = (s.sounds || []).map(x => x.file).join('|');
  if (sndSig !== sigSounds) {
    const rebuild = filesSig !== sigSounds.split('||')[0];
    sigSounds = sndSig;
    const box = $('sounds');
    if (rebuild) box.innerHTML = '';
    if (!(s.sounds || []).length) {
      box.innerHTML = '<p class="empty">Пока пусто. Закинь mp3/wav/ogg в ~/obs-panel/sounds/ или нажми «＋ загрузить».</p>';
    } else if (rebuild) {
      (s.sounds || []).forEach(snd => {
        const b = document.createElement('button');
        b.className = 'btn snd';
        b.dataset.file = snd.file;
        b.innerHTML = '<span class="nm"></span>' + (snd.grp ? '<span class="grp"></span>' : '');
        b.querySelector('.nm').textContent = snd.name;
        if (snd.grp) b.querySelector('.grp').textContent = snd.grp;
        box.appendChild(b);
      });
    }
    box.querySelectorAll('.snd').forEach(b => {
      const snd = (s.sounds || []).find(x => x.file === b.dataset.file);
      b.classList.toggle('playing', !!(snd && snd.playing));
    });
  }

  const sv = $('sndvol');
  if (dragging !== sv && s.sndvol !== undefined) sv.value = s.sndvol;
  $('sndvolVal').textContent = dragging === sv ? sv.value : (s.sndvol ?? '—');
}

/* ── нажатия ── */
document.addEventListener('click', (ev) => {
  const t = ev.target.closest('.scene, #btnStream, #btnRecord, #btnReplay, #btnStopSounds, .snd, [data-mute]');
  if (!t) return;
  if (t.classList.contains('scene')) send({ c: 'scene', name: t.dataset.scene });
  else if (t.id === 'btnStream') send({ c: 'stream', a: (state.stream || {}).active ? 'stop' : 'start' });
  else if (t.id === 'btnRecord') send({ c: 'record', a: (state.record || {}).active ? 'stop' : 'start' });
  else if (t.id === 'btnReplay') send({ c: 'replay' });
  else if (t.id === 'btnStopSounds') send({ c: 'stopsounds' });
  else if (t.classList.contains('snd')) send({ c: 'sound', file: t.dataset.file });
  else if (t.dataset.mute) send({ c: 'mute', src: t.dataset.mute });
});

['mic', 'sys'].forEach(k => {
  const sl = document.querySelector(`[data-vol="${k}"]`);
  sl.addEventListener('pointerdown', () => { dragging = sl; });
  sl.addEventListener('input', () => { document.querySelector(`[data-val="${k}"]`).textContent = sl.value; });
  sl.addEventListener('change', () => { dragging = null; send({ c: 'vol', src: k, v: +sl.value }, true); });
  sl.addEventListener('pointerup', () => { dragging = null; });
});

const sv = $('sndvol');
sv.addEventListener('pointerdown', () => { dragging = sv; });
sv.addEventListener('input', () => { $('sndvolVal').textContent = sv.value; });
sv.addEventListener('change', () => { dragging = null; send({ c: 'sndvol', v: +sv.value }, true); });
sv.addEventListener('pointerup', () => { dragging = null; });

$('upload').addEventListener('change', async (e) => {
  const files = [...e.target.files];
  if (!files.length) return;
  for (const f of files) {
    const fd = new FormData();
    fd.append('file', f, f.name);
    flash(`загружаю ${f.name}…`);
    try {
      const r = await fetch('/api/upload', { method: 'POST', body: fd });
      const j = await r.json();
      flash(j.ok ? `загружено: ${j.saved.join(', ')}` : `ошибка: ${j.error}`);
    } catch (err) { flash('ошибка загрузки'); }
  }
  e.target.value = '';
});

/* ── мелочи ── */
(async () => {
  try { if ('wakeLock' in navigator) await navigator.wakeLock.request('screen'); } catch (e) {}
})();
document.addEventListener('visibilitychange', async () => {
  if (!document.hidden) {
    try { if ('wakeLock' in navigator) await navigator.wakeLock.request('screen'); } catch (e) {}
    if (!ws || ws.readyState > 1) connect();
  }
});
connect();
