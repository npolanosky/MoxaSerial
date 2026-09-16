/* Moxa DNC palette UI - vanilla JS, no framework, no CDN, works offline.
 *
 * Two hosts, one file:
 *   Fusion     window.adsk.fusionSendData(action, json) -> Promise<string>
 *              window.fusionJavaScriptHandler.handle(action, data) <- push
 *   Browser    POST /api {action, payload}  +  EventSource /events
 * `Host` below picks whichever exists, so the same HTML renders and
 * behaves identically in Fusion's Qt WebEngine and in a normal browser
 * against tools/dev_server.py.
 */
'use strict';

/* ==================================================================== */
/* Host shim                                                            */
/* ==================================================================== */
const Host = (() => {
  const listeners = [];
  /* Fusion injects window.adsk asynchronously (QWebChannel), often after
     this script has run, so the check must be made at call time. A page
     loaded from file:// can only be Fusion; give the bridge a moment to
     appear before giving up. */
  const fromFile = window.location.protocol === 'file:';
  function bridgeReady() {
    return typeof window.adsk !== 'undefined' && typeof window.adsk.fusionSendData === 'function';
  }
  async function waitForBridge(ms) {
    const deadline = Date.now() + ms;
    while (!bridgeReady() && Date.now() < deadline) {
      await new Promise((r) => setTimeout(r, 50));
    }
    return bridgeReady();
  }

  function dispatch(action, payload) {
    listeners.forEach((fn) => {
      try { fn(action, payload); } catch (e) { console.error('handler failed', action, e); }
    });
  }

  function parse(raw) {
    if (raw === undefined || raw === null || raw === '') return {};
    if (typeof raw === 'object') return raw;
    try { return JSON.parse(raw); } catch (e) { return { value: raw }; }
  }

  /* Fusion -> JS push channel. Must return a non-empty string or Fusion
     treats sendInfoToHTML as failed. */
  window.fusionJavaScriptHandler = {
    handle(action, data) {
      try {
        dispatch(action, parse(data));
      } catch (e) {
        console.error('push handler failed', action, e);
        return JSON.stringify({ status: 'ERROR', message: String(e) });
      }
      return JSON.stringify({ status: 'OK' });
    }
  };

  async function send(action, payload) {
    if (bridgeReady() || (fromFile && await waitForBridge(5000))) {
      const raw = await window.adsk.fusionSendData(action, JSON.stringify(payload || {}));
      return parse(raw);
    }
    if (fromFile) throw new Error('Fusion bridge (window.adsk) not available');
    const res = await fetch('api', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action, payload: payload || {} })
    });
    if (!res.ok) throw new Error('HTTP ' + res.status);
    return res.json();
  }

  function connectEvents() {
    if (fromFile || bridgeReady() || typeof EventSource === 'undefined') return;
    const src = new EventSource('events');
    src.onmessage = (ev) => {
      try {
        const msg = JSON.parse(ev.data);
        dispatch(msg.action, msg.payload || {});
      } catch (e) { console.error('bad SSE frame', e); }
    };
    src.onerror = () => { /* EventSource retries on its own */ };
  }

  return {
    get inFusion() { return fromFile || bridgeReady(); },
    send,
    on: (fn) => listeners.push(fn),
    connectEvents
  };
})();

/* ==================================================================== */
/* Tiny helpers                                                         */
/* ==================================================================== */
const $  = (sel, root) => (root || document).querySelector(sel);
const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

function fmtBytes(n) {
  if (!n) return '0 B';
  if (n < 1024) return n + ' B';
  if (n < 1024 * 1024) return (n / 1024).toFixed(1) + ' kB';
  return (n / 1048576).toFixed(2) + ' MB';
}

function fmtClock(sec) {
  if (sec === undefined || sec === null || sec < 0 || !isFinite(sec)) return '--:--';
  const s = Math.floor(sec % 60);
  const m = Math.floor(sec / 60);
  return String(m).padStart(2, '0') + ':' + String(s).padStart(2, '0');
}

function setLed(name, on) {
  const node = $(`.led[data-led="${name}"]`);
  if (node) node.classList.toggle('on', !!on);
}

function applyModem(modem) {
  if (!modem) return;
  ['cts', 'dsr', 'dcd', 'dtr', 'rts'].forEach((k) => {
    if (k in modem) setLed(k, modem[k]);
  });
}

/* ==================================================================== */
/* Application state                                                    */
/* ==================================================================== */
const S = {
  settings: null,
  machines: [],
  enums: {},
  activeMachineId: '',
  editingId: '',
  dirtyMachine: null,
  file: null,
  send: {},
  receive: {},
  logEntries: [],
  logSeq: 0,
  unseenProblems: 0,
  overwriteToken: ''
};

/* ==================================================================== */
/* Boot                                                                 */
/* ==================================================================== */
document.addEventListener('DOMContentLoaded', () => {
  wireTabs();
  wireSendPage();
  wireReceivePage();
  wireMachinesPage();
  wireLogPage();
  wireAboutPage();
  wireModal();

  Host.on(onPush);
  Host.connectEvents();

  call('ui.ready').then((data) => { if (data) applyState(data); });
});

/* Returns the full {ok, data|error} reply and never throws, so a caller
   that wants to render the error itself (the machine form) can. */
async function callRaw(action, payload) {
  try {
    return await Host.send(action, payload);
  } catch (e) {
    console.error(action, e);
    return { ok: false, action, error: String(e && e.message ? e.message : e) };
  }
}

/* The common case: surface any failure as a toast and hand back just the
   data, or null. */
async function call(action, payload) {
  const reply = await callRaw(action, payload);
  if (!reply || reply.ok === false) {
    const message = (reply && reply.error) || ('"' + action + '" failed');
    toast(message, 'error');
    setStatus(message, 'err');
    return null;
  }
  return reply.data;
}

/* ==================================================================== */
/* Inbound push router                                                  */
/* ==================================================================== */
function onPush(action, payload) {
  switch (action) {
    case 'state':                     applyState(payload); break;
    case 'machines':                  S.machines = payload.machines || []; renderMachineSelect(); renderMachineList(); break;
    case 'send.state':
    case 'send.progress':             applySend(payload); break;
    case 'send.done':                 applySend(payload); setStatus('Transfer complete — ' + payload.file_name, 'ok'); break;
    case 'send.error':                applySend(payload); setStatus(payload.message || 'Send failed.', 'err'); break;
    case 'send.log':                  setStatus(payload.message, payload.level === 'WARNING' ? 'warn' : ''); break;
    case 'transport.modem':           applyModem(payload); break;
    case 'machines.testResult': {
      const btn = $('#btnMachineTest');
      btn.disabled = false; btn.textContent = 'Test connection';
      if (payload.ok) {
        const caps = payload.info.capabilities;
        const modem = payload.info.modem || {};
        toast('Connected in ' + payload.info.elapsed_ms + ' ms'
          + (caps && !caps.command_channel ? ' (data channel only)' : '')
          + ' · CTS ' + (modem.cts ? 'high' : 'low') + ', DSR ' + (modem.dsr ? 'high' : 'low'), 'success');
      } else {
        toast('Connection failed: ' + (payload.error || 'unknown error'), 'error');
      }
      break;
    }
    case 'transport.line_error':      setStatus('Device reported: ' + (payload.error || 'line error'), 'warn'); setLed('err', true); break;
    case 'receive.state':
    case 'receive.progress':          applyReceive(payload); break;
    case 'receive.done':              applyReceive(payload); setRcvStatus('Saved ' + (payload.target_name || ''), 'ok'); break;
    case 'receive.error':             applyReceive(payload); setRcvStatus(payload.message || 'Receive failed.', 'err'); break;
    case 'receive.overwrite_request': showOverwrite(payload); break;
    case 'log.entry':                 addLogEntry(payload); break;
    case 'file.info':                 setFile(payload.file); break;
    case 'toast':                     toast(payload.message, payload.level); break;
    default: break;
  }
}

function applyState(state) {
  if (!state) return;
  S.settings = state.settings || {};
  S.machines = state.machines || [];
  S.enums = state.enums || {};
  S.activeMachineId = state.activeMachineId || '';
  setTheme(S.settings.theme || 'dark');

  renderMachineSelect();
  renderMachineList();
  populateEnumSelects();
  selectMachineForEdit(S.activeMachineId);
  renderOptionChips();
  renderReceiveTarget();

  if (state.file && state.file.path) setFile(state.file);
  if (state.send) applySend(state.send);
  if (state.receive) applyReceive(state.receive);
  $('#btnResend').disabled = !state.canResend;

  if (state.log) {
    S.logEntries = state.log.entries || [];
    renderLog();
    $('#aboutLog').textContent = state.log.path || '—';
    $('#globalLogLevel').value = state.log.level || 'INFO';
  }
  $('#aboutVersion').textContent = 'v' + (state.version || '?');
  $('#aboutHost').textContent = (state.host && state.host.host) || 'standalone';
  $('#aboutSettings').textContent = (state.paths && state.paths.settings) || '—';
  $('#globalConfirm').checked = !!S.settings.confirm_before_send;

  $$('input[name="selmode"]').forEach((r) => { r.checked = r.value === S.settings.machine_selection; });
  call('about.get').then((d) => { if (d) renderProtocolNotice(d.protocol); });
}

/* ==================================================================== */
/* Tabs / theme / toasts                                                */
/* ==================================================================== */
function wireTabs() {
  $$('.tab').forEach((tab) => {
    tab.addEventListener('click', () => showPage(tab.dataset.page));
  });
  $('#themeToggle').addEventListener('click', () => {
    const next = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
    setTheme(next);
    call('theme.set', { theme: next });
  });
}

function showPage(name) {
  $$('.tab').forEach((t) => t.classList.toggle('is-active', t.dataset.page === name));
  $$('.page').forEach((p) => p.classList.toggle('is-active', p.dataset.page === name));
  if (name === 'log') {
    S.unseenProblems = 0;
    $('#logBadge').classList.add('hidden');
  }
}

function setTheme(theme) {
  document.documentElement.dataset.theme = theme === 'light' ? 'light' : 'dark';
}

function toast(message, level) {
  if (!message) return;
  const node = el('div', 'toast t-' + (level || 'info'), message);
  $('#toasts').appendChild(node);
  setTimeout(() => node.remove(), 4200);
}

/* ==================================================================== */
/* SEND page                                                            */
/* ==================================================================== */
function wireSendPage() {
  $('#machineSelect').addEventListener('change', (e) => {
    S.activeMachineId = e.target.value;
    renderOptionChips();
    renderReceiveTarget();
    refreshPreview();
  });

  $('#btnLastPost').addEventListener('click', async () => {
    setStatus('Looking for the most recent posted program…');
    const data = await call('file.lastPost');
    if (!data) return;
    if (!data.file || !data.file.path) {
      setStatus(data.error || 'No posted NC file found.', 'warn');
      toast(data.error || 'No posted NC file found.', 'warning');
      return;
    }
    setFile(data.file);
    setStatus('Using the most recent posted program.', 'ok');
  });

  $('#btnBrowse').addEventListener('click', async () => {
    const data = await call('file.browse', { machineId: S.activeMachineId });
    if (!data) return;
    if (data.cancelled) return;
    if (data.file) { setFile(data.file); setStatus('File selected.'); }
  });

  $('#btnEditMachine').addEventListener('click', () => {
    selectMachineForEdit(S.activeMachineId);
    showPage('machines');
  });

  $('#btnStart').addEventListener('click', async () => {
    if (!S.file || !S.file.path) { toast('Choose a program first.', 'warning'); return; }
    setStatus('Starting…');
    const reply = await call('send.start', { machineId: S.activeMachineId, path: S.file.path });
    if (reply && reply.needsConfirm) {
      const ok = window.confirm('Send ' + reply.fileName + ' to ' + reply.machine + '?');
      if (!ok) { setStatus('Send cancelled.'); return; }
      await call('send.start', { machineId: S.activeMachineId, path: S.file.path, confirmed: true });
    }
  });
  $('#btnPause').addEventListener('click', () => {
    const paused = S.send.state === 'PAUSED';
    call(paused ? 'send.resume' : 'send.pause');
  });
  $('#btnStop').addEventListener('click', () => call('send.stop'));
  $('#btnResend').addEventListener('click', () => call('send.resend'));
}

function setFile(file) {
  S.file = file || null;
  const node = $('#fileInfo');
  if (!file || !file.path) {
    node.className = 'file-info empty';
    node.textContent = 'No file selected.';
    $('#fileSource').textContent = '';
    return;
  }
  node.className = 'file-info';
  node.innerHTML = '';
  node.appendChild(el('b', null, file.name));
  node.appendChild(document.createTextNode('  ' + fmtBytes(file.size) + '  ·  ' + (file.mtimeText || '')));
  node.title = file.path;
  $('#fileSource').textContent = file.source === 'nc-program' ? 'from NC program'
    : file.source === 'folder-scan' ? 'newest in output folder'
    : file.source === 'browse' ? 'browsed' : (file.source || '');
  refreshPreview();
}

async function refreshPreview() {
  if (!S.file || !S.file.path) return;
  const data = await call('file.preview', { path: S.file.path, machineId: S.activeMachineId });
  if (!data) return;
  renderPreviewLines(data.lines, -1);
  $('#previewMeta').textContent =
    data.lineCount + ' lines · ' + fmtBytes(data.byteCount) +
    (data.droppedLines ? ' · ' + data.droppedLines + ' dropped' : '');
}

function renderPreviewLines(lines, current) {
  const box = $('#linePreview');
  box.innerHTML = '';
  if (!lines || !lines.length) {
    box.appendChild(el('div', 'code-empty', 'Nothing to show.'));
    return;
  }
  lines.forEach((entry, idx) => {
    const isObj = typeof entry === 'object';
    const i = isObj ? entry.i : idx;
    const text = isObj ? entry.text : entry;
    const cur = isObj ? entry.current : i === current;
    const row = el('div', 'code-line' + (cur ? ' is-current' : (i < current ? ' is-sent' : '')));
    row.appendChild(el('span', 'n', String(i + 1)));
    row.appendChild(el('span', 't', text || ' '));
    box.appendChild(row);
  });
}

function applySend(p) {
  if (!p || !p.state) return;
  S.send = p;
  // A toolbar send may target the default machine while the dropdown shows
  // another one; follow the job so the chips and options match what is sent.
  if (p.machine_id && p.machine_id !== S.activeMachineId && S.machines.some((m) => m.id === p.machine_id)) {
    S.activeMachineId = p.machine_id;
    $('#machineSelect').value = p.machine_id;
    renderOptionChips();
  }

  const pct = Math.max(0, Math.min(100, p.percent || 0));
  $('#progPercent').textContent = pct.toFixed(pct < 10 ? 1 : 0) + '%';
  const bar = $('#progBar');
  bar.style.width = pct + '%';
  bar.className = 'progress-fill'
    + (p.state === 'DONE' ? ' is-done' : '')
    + (p.state === 'ERROR' ? ' is-err' : '')
    + (p.state === 'PAUSED' ? ' is-pause' : '');

  $('#progCounts').textContent = (p.lines_sent || 0) + ' / ' + (p.lines_total || 0) + ' lines';
  $('#progBytes').textContent = fmtBytes(p.bytes_sent || 0) + ' / ' + fmtBytes(p.bytes_total || 0);
  $('#progTime').textContent = fmtClock(p.elapsed_s) + ' elapsed';
  $('#progEta').textContent = 'ETA ' + fmtClock(p.eta_s);
  $('#progCps').textContent = Math.round(p.cps || p.rate_bps || 0) + ' CPS';
  $('#progErrors').textContent = (p.errors || 0) + (p.errors === 1 ? ' error' : ' errors');
  $('#progErrors').classList.toggle('is-bad', (p.errors || 0) > 0);
  setLed('err', (p.errors || 0) > 0);

  const modem = p.modem || {};
  setLed('tx', p.tx && p.state === 'SENDING');
  setLed('rx', p.rx);
  applyModem(modem);
  setLed('xoff', p.xoff);

  if (p.window && p.window.length) renderPreviewLines(p.window, p.line_index);

  setStateChip(p.state);
  const active = ['CONNECTING', 'WAITING_READY', 'SENDING', 'PAUSED'].indexOf(p.state) >= 0;
  $('#btnStart').disabled = active;
  $('#btnPause').disabled = !(p.state === 'SENDING' || p.state === 'PAUSED' || p.state === 'WAITING_READY');
  $('#btnPause').textContent = p.state === 'PAUSED' ? 'Resume' : 'Pause';
  $('#btnStop').disabled = !active;
  $('#btnResend').disabled = active || !(p.file_path || S.file);

  if (p.message) {
    setStatus(p.message, p.state === 'ERROR' ? 'err' : p.state === 'DONE' ? 'ok' : p.state === 'PAUSED' ? 'warn' : '');
  }
}

function setStateChip(state) {
  const chip = $('#stateChip');
  chip.textContent = state || 'IDLE';
  const map = {
    SENDING: 'chip-active', CONNECTING: 'chip-active', WAITING_READY: 'chip-warn',
    RECEIVING: 'chip-active', WAITING: 'chip-warn',
    PAUSED: 'chip-warn', DONE: 'chip-ok', ERROR: 'chip-err', STOPPED: 'chip-warn'
  };
  chip.className = 'chip ' + (map[state] || 'chip-idle');
}

function setStatus(text, kind) {
  const node = $('#sendStatus');
  node.textContent = text || '';
  node.className = 'status-line' + (kind ? ' is-' + kind : '');
}

function activeMachine() {
  return S.machines.find((m) => m.id === S.activeMachineId) || S.machines[0] || null;
}

function renderMachineSelect() {
  const sel = $('#machineSelect');
  const previous = S.activeMachineId;
  sel.innerHTML = '';
  S.machines.forEach((m) => {
    const opt = el('option', null, m.name + (m.type === 'simulator' ? '  (sim)' : ''));
    opt.value = m.id;
    sel.appendChild(opt);
  });
  if (S.machines.some((m) => m.id === previous)) sel.value = previous;
  else if (S.machines.length) { sel.value = S.machines[0].id; S.activeMachineId = S.machines[0].id; }
}

function renderOptionChips() {
  const m = activeMachine();
  const box = $('#optionChips');
  box.innerHTML = '';
  if (!m) return;
  const s = m.serial, d = m.send;
  const parityLetter = { none: 'N', odd: 'O', even: 'E', mark: 'M', space: 'S' }[s.parity] || 'N';
  const chips = [
    s.baud + ' ' + s.data_bits + parityLetter + s.stop_bits,
    { none: 'no flow', xonxoff: 'XON/XOFF', rtscts: 'RTS/CTS', dtrdsr: 'DTR/DSR', both: 'HW+SW flow' }[s.flow_control],
    d.line_ending,
    d.uppercase ? 'UPPER' : null,
    d.strip_comments ? 'no comments' : null,
    d.strip_blank_lines ? 'no blanks' : null,
    d.strip_spaces ? 'no spaces' : null,
    d.line_numbers ? 'renumber' : null,
    { immediate: 'send now', cts: 'wait CTS', dsr: 'wait DSR', xon: 'wait XON' }[d.wait_for_ready],
    m.type === 'simulator' ? 'SIMULATOR' : (m.host + ':' + m.data_port)
  ];
  chips.filter(Boolean).forEach((text) => box.appendChild(el('span', 'chip', text)));
}

/* ==================================================================== */
/* RECEIVE page                                                         */
/* ==================================================================== */
function wireReceivePage() {
  $('#btnRcvStart').addEventListener('click', () => {
    setRcvStatus('Waiting for the control…');
    call('receive.start', { machineId: S.activeMachineId, filename: $('#rcvFilename').value.trim() });
  });
  $('#btnRcvStop').addEventListener('click', () => call('receive.stop'));
  $('#btnRcvReveal').addEventListener('click', () => {
    if (S.receive && S.receive.target_path) call('file.reveal', { path: S.receive.target_path });
  });
  $('#btnEditReceive').addEventListener('click', () => {
    selectMachineForEdit(S.activeMachineId);
    showPage('machines');
  });
}

function renderReceiveTarget() {
  const m = activeMachine();
  if (!m) return;
  const r = m.receive;
  $('#rcvFolder').textContent = r.folder || '—';
  $('#rcvFolder').title = r.folder || '';
  $('#rcvPattern').textContent = r.filename_pattern || '—';
  $('#rcvPolicy').textContent = {
    allow: 'overwrite it', ask: 'ask me', deny: 'refuse', rename: 'save as a new name'
  }[r.overwrite] || r.overwrite;
  $('#rcvTriggers').textContent =
    (r.start_trigger ? 'start ' + JSON.stringify(r.start_trigger) : 'start: any data') +
    ' · ' + (r.end_trigger ? 'end ' + JSON.stringify(r.end_trigger) : 'end: idle');
  $('#rcvIdle').textContent = r.idle_timeout_s ? r.idle_timeout_s + ' s' : 'none';
}

function applyReceive(p) {
  if (!p || !p.state) return;
  S.receive = p;
  $('#rcvState').textContent = p.state;
  $('#rcvCounts').textContent = (p.lines_received || 0) + ' lines · ' + fmtBytes(p.bytes_received || 0) +
    ' · ' + Math.round(p.cps || 0) + ' CPS' + (p.errors ? ' · ' + p.errors + ' errors' : '');
  setLed('rcvrx', p.rx);
  $('#rcvIdleLed').textContent = p.idle_s > 0.4 ? 'idle ' + p.idle_s.toFixed(0) + 's' : '';
  $('#rcvTarget').textContent = p.target_name || '';
  if (p.target_path) $('#rcvTarget').title = p.target_path;

  const box = $('#rcvTail');
  if (p.tail && p.tail.length) {
    box.innerHTML = '';
    p.tail.forEach((text, i) => {
      const row = el('div', 'code-line');
      row.appendChild(el('span', 'n', String(Math.max(1, (p.lines_received || p.tail.length) - p.tail.length + i + 1))));
      row.appendChild(el('span', 't', text || ' '));
      box.appendChild(row);
    });
    box.scrollTop = box.scrollHeight;
  } else if (p.state === 'IDLE') {
    box.innerHTML = '';
    box.appendChild(el('div', 'code-empty', 'Nothing received yet.'));
  }

  const active = ['CONNECTING', 'WAITING', 'RECEIVING'].indexOf(p.state) >= 0;
  $('#btnRcvStart').disabled = active;
  $('#btnRcvStop').disabled = !active;
  $('#btnRcvReveal').disabled = !p.target_path;
  if (active || p.state === 'DONE' || p.state === 'ERROR') setStateChip(p.state);
  if (p.message) setRcvStatus(p.message, p.state === 'ERROR' ? 'err' : p.state === 'DONE' ? 'ok' : '');
}

function setRcvStatus(text, kind) {
  const node = $('#rcvStatus');
  node.textContent = text || '';
  node.className = 'status-line' + (kind ? ' is-' + kind : '');
}

/* ==================================================================== */
/* MACHINES page                                                        */
/* ==================================================================== */
function wireMachinesPage() {
  $('#btnMachineNew').addEventListener('click', async () => {
    const data = await call('machines.new', {});
    if (!data) return;
    S.dirtyMachine = data.machine;
    S.editingId = data.machine.id;
    fillMachineForm(data.machine);
    $('#machineFormTitle').textContent = 'New machine';
  });

  $('#btnMachineDup').addEventListener('click', async () => {
    if (!S.editingId) return;
    const data = await call('machines.duplicate', { id: S.editingId });
    if (!data) return;
    S.machines = data.machines;
    renderMachineSelect(); renderMachineList();
    selectMachineForEdit(data.machine.id);
    toast('Duplicated as "' + data.machine.name + '".', 'success');
  });

  $('#btnMachineDel').addEventListener('click', async () => {
    const m = S.machines.find((x) => x.id === S.editingId);
    if (!m) return;
    if (!window.confirm('Delete machine "' + m.name + '"?')) return;
    const data = await call('machines.delete', { id: S.editingId });
    if (!data) return;
    S.machines = data.machines;
    renderMachineSelect(); renderMachineList();
    selectMachineForEdit(S.machines[0] ? S.machines[0].id : '');
  });

  $('#btnSetDefault').addEventListener('click', async () => {
    if (!S.editingId) return;
    const data = await call('machines.setDefault', { id: S.editingId });
    if (data) { S.settings = data.settings; renderMachineList(); toast('Default machine set.', 'success'); }
  });

  $('#btnMachineRevert').addEventListener('click', () => selectMachineForEdit(S.editingId));

  $('#btnMachineTest').addEventListener('click', async () => {
    const btn = $('#btnMachineTest');
    btn.disabled = true; btn.textContent = 'Testing…';
    const data = await call('machines.test', { id: S.editingId });
    if (!data || !data.started) { btn.disabled = false; btn.textContent = 'Test connection'; }
    // Result arrives as a 'machines.testResult' push (see dispatch).
  });

  $('#machineForm').addEventListener('submit', async (e) => {
    e.preventDefault();
    const machine = readMachineForm();
    const reply = await callRaw('machines.save', { machine });
    if (!reply || reply.ok === false) {
      // Keep the rejection next to the fields that caused it, not only in a toast.
      const message = (reply && reply.error) || 'Could not save this machine.';
      showErrors(message.split('; '));
      toast(message, 'error');
      return;
    }
    const data = reply.data;
    S.machines = data.machines;
    renderMachineSelect(); renderMachineList(); renderOptionChips(); renderReceiveTarget();
    selectMachineForEdit(data.machine.id);
    showErrors(data.warnings);
    toast('Saved "' + data.machine.name + '".', 'success');
  });

  $$('input[name="selmode"]').forEach((radio) => {
    radio.addEventListener('change', async () => {
      const data = await call('settings.save', { settings: { machine_selection: radio.value } });
      if (data) S.settings = data.settings;
    });
  });
}

function populateEnumSelects() {
  $$('select[data-enum]').forEach((sel) => {
    const values = S.enums[sel.dataset.enum] || [];
    const labels = {
      flowControls: { none: 'None', xonxoff: 'XON / XOFF (software)', rtscts: 'RTS / CTS (hardware)', dtrdsr: 'DTR / DSR (hardware)', both: 'Hardware + software' },
      waitModes: { immediate: 'Send immediately', cts: 'Wait for CTS', dsr: 'Wait for DSR', xon: 'Wait for XON / DC2' },
      overwritePolicies: { allow: 'Overwrite', ask: 'Ask me', deny: 'Refuse', rename: 'Save under a new name' },
      parities: { none: 'None', odd: 'Odd', even: 'Even', mark: 'Mark (test only)', space: 'Space (test only)' },
      lineEndings: { LF: 'LF (\\10)', CR: 'CR (\\13)', CRLF: 'CR LF (\\13 \\10)', CUSTOM: 'Custom…' },
      receiveLineEndings: { AUTO: 'Auto', LF: 'LF (\\10)', CR: 'CR (\\13)', CRLF: 'CR LF (\\13 \\10)', CUSTOM: 'Custom…' },
      saveLineEndings: { KEEP: 'Keep what was received', LF: 'Unix: LF (\\10)', CR: 'Mac: CR (\\13)', CRLF: 'DOS/Windows: CR LF (\\13 \\10)' },
      receiveRemoveChars: { none: 'None', ascii0: 'ASCII 0', ascii0to31: 'All below ASCII 32', custom: 'Custom list…' },
      startTriggerModes: { save_from_trigger: 'Save from trigger', save_after_trigger: 'Save after trigger' },
      endTriggerModes: { save_including_trigger: 'Save including trigger', save_until_trigger: 'Save until trigger' }
    }[sel.dataset.enum];
    sel.innerHTML = '';
    values.forEach((v) => {
      const opt = el('option', null, labels && labels[v] ? labels[v] : String(v));
      opt.value = String(v);
      sel.appendChild(opt);
    });
  });
}

function renderMachineList() {
  const list = $('#machineList');
  list.innerHTML = '';
  const settings = S.settings || {};
  S.machines.forEach((m) => {
    const li = el('li');
    li.classList.toggle('is-selected', m.id === S.editingId);
    li.appendChild(el('span', 'm-name', m.name));
    li.appendChild(el('span', 'm-addr', m.type === 'simulator' ? 'simulator' : m.host + ':' + m.data_port));
    if (m.id === settings.default_machine_id) li.appendChild(el('span', 'm-tag', 'DEFAULT'));
    else if (m.id === settings.last_used_machine_id) li.appendChild(el('span', 'm-tag', 'LAST'));
    li.addEventListener('click', () => selectMachineForEdit(m.id));
    list.appendChild(li);
  });
  $('#btnMachineDel').disabled = S.editingId === 'simulator' || !S.editingId;
}

function selectMachineForEdit(id) {
  const m = S.machines.find((x) => x.id === id) || S.machines[0];
  if (!m) return;
  S.editingId = m.id;
  S.dirtyMachine = null;
  fillMachineForm(m);
  $('#machineFormTitle').textContent = m.name;
  renderMachineList();
  showErrors([]);
}

function setField(form, name, value) {
  const node = form.elements[name];
  if (!node) return;
  if (node.type === 'checkbox') node.checked = !!value;
  else node.value = value === undefined || value === null ? '' : String(value);
}

function fillMachineForm(m) {
  const form = $('#machineForm');
  form.dataset.machineId = m.id;
  ['name', 'type', 'host', 'port_index', 'data_port', 'cmd_port', 'connect_timeout_s',
    'auto_reconnect', 'reconnect_attempts', 'reconnect_delay_s', 'notes']
    .forEach((k) => setField(form, k, m[k]));
  ['serial', 'send', 'receive'].forEach((section) => {
    Object.keys(m[section] || {}).forEach((k) => setField(form, section + '.' + k, m[section][k]));
  });
  toggleTypeFields(form);
  form.elements['type'].onchange = () => toggleTypeFields(form);
}

function toggleTypeFields(form) {
  const isSim = form.elements['type'].value === 'simulator';
  ['host', 'port_index', 'data_port', 'cmd_port'].forEach((k) => {
    const node = form.elements[k];
    if (node && node.closest('label')) node.closest('label').style.opacity = isSim ? 0.45 : 1;
    if (node) node.disabled = isSim;
  });
}

function readMachineForm() {
  const form = $('#machineForm');
  const out = { id: form.dataset.machineId, serial: {}, send: {}, receive: {} };
  Array.from(form.elements).forEach((node) => {
    if (!node.name) return;
    let value;
    if (node.type === 'checkbox') value = node.checked;
    else if (node.type === 'number') value = node.value === '' ? 0 : Number(node.value);
    else value = node.value;
    const dot = node.name.indexOf('.');
    if (dot < 0) out[node.name] = value;
    else out[node.name.slice(0, dot)][node.name.slice(dot + 1)] = value;
  });
  return out;
}

function showErrors(problems) {
  const box = $('#machineErrors');
  if (!problems || !problems.length) { box.classList.add('hidden'); box.textContent = ''; return; }
  box.classList.remove('hidden');
  box.textContent = problems.join('  ·  ');
}

/* ==================================================================== */
/* LOG page                                                             */
/* ==================================================================== */
function wireLogPage() {
  $('#logLevel').addEventListener('change', renderLog);
  $('#logFilter').addEventListener('input', renderLog);
  $('#btnLogClear').addEventListener('click', async () => {
    await call('log.clear');
    S.logEntries = [];
    renderLog();
  });
  $('#btnLogOpen').addEventListener('click', async () => {
    const data = await call('log.openFile');
    if (data && !data.revealed) toast('Log file: ' + (data.path || 'unavailable'), 'info');
  });
}

const LEVEL_ORDER = { DEBUG: 0, INFO: 1, WARNING: 2, ERROR: 3, CRITICAL: 4 };

function addLogEntry(entry) {
  if (!entry || !entry.level) return;
  S.logEntries.push(entry);
  if (S.logEntries.length > 2000) S.logEntries.splice(0, S.logEntries.length - 2000);
  if (LEVEL_ORDER[entry.level] >= 2 && !$('.tab[data-page="log"]').classList.contains('is-active')) {
    S.unseenProblems += 1;
    const badge = $('#logBadge');
    badge.textContent = String(S.unseenProblems);
    badge.classList.remove('hidden');
  }
  renderLog();
}

function renderLog() {
  const min = LEVEL_ORDER[$('#logLevel').value] || 0;
  const needle = $('#logFilter').value.trim().toLowerCase();
  const list = $('#logList');
  const atBottom = list.scrollTop + list.clientHeight >= list.scrollHeight - 24;
  list.innerHTML = '';

  const counts = { WARNING: 0, ERROR: 0 };
  const rows = S.logEntries.filter((e) => {
    if (e.level === 'WARNING') counts.WARNING += 1;
    if (e.level === 'ERROR' || e.level === 'CRITICAL') counts.ERROR += 1;
    if ((LEVEL_ORDER[e.level] || 0) < min) return false;
    if (needle && e.message.toLowerCase().indexOf(needle) < 0) return false;
    return true;
  });

  rows.slice(-800).forEach((e) => {
    const row = el('div', 'log-row l-' + e.level);
    row.appendChild(el('span', 'lt', e.time));
    row.appendChild(el('span', 'll', e.level.slice(0, 4)));
    row.appendChild(el('span', 'lm', e.message));
    list.appendChild(row);
  });

  $('#logCounts').textContent = rows.length + ' shown · ' + counts.WARNING + ' warnings · ' + counts.ERROR + ' errors';
  if (atBottom) list.scrollTop = list.scrollHeight;
}

/* ==================================================================== */
/* ABOUT page                                                           */
/* ==================================================================== */
function wireAboutPage() {
  $('#globalLogLevel').addEventListener('change', (e) => {
    call('settings.save', { settings: { log_level: e.target.value } });
  });
  $('#globalConfirm').addEventListener('change', (e) => {
    call('settings.save', { settings: { confirm_before_send: e.target.checked } });
  });
}

function renderProtocolNotice(protocol) {
  const box = $('#aboutProtocol');
  if (!protocol) { box.textContent = ''; return; }
  if (!protocol.implemented) {
    box.textContent = 'Moxa ASPP command channel: not implemented yet. ' + (protocol.notes || '');
  } else if (protocol.verified_on_hardware === false) {
    box.textContent = 'Moxa ASPP command channel: implemented from Moxa driver sources, '
      + 'not yet verified against real NPort hardware. Use "Test" on a machine to check.';
  } else {
    box.textContent = 'Moxa ASPP command channel: active.';
  }
}

/* ==================================================================== */
/* Overwrite modal                                                      */
/* ==================================================================== */
function wireModal() {
  $('#owOverwrite').addEventListener('click', () => answerOverwrite('overwrite'));
  $('#owRename').addEventListener('click', () => answerOverwrite('rename'));
  $('#owCancel').addEventListener('click', () => answerOverwrite('cancel'));
}

function showOverwrite(payload) {
  S.overwriteToken = payload.token;
  $('#overwriteText').textContent =
    payload.name + ' already exists in the target folder. Save it as "' + payload.suggested + '" instead?';
  $('#overwriteModal').classList.remove('hidden');
}

function answerOverwrite(decision) {
  $('#overwriteModal').classList.add('hidden');
  call('receive.overwriteResponse', { token: S.overwriteToken, decision });
  S.overwriteToken = '';
}
