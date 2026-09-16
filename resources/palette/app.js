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
  overwriteToken: '',
  serialPortsLoaded: false,  /* list ports once per panel */
  /* discovery: found devices keyed by IP, probe results keyed by
     "<ip>:<portIndex>", and has_credentials by machine id. Never a password. */
  found: {},
  probes: {},
  credentials: {},
  scanId: ''
};

/* ==================================================================== */
/* Boot                                                                 */
/* ==================================================================== */
document.addEventListener('DOMContentLoaded', () => {
  wireTabs();
  wireSendPage();
  wireReceivePage();
  wireMachinesPage();
  wireNPortFinder();   // discovery
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
    /* --- discovery --- */
    case 'machines.discoverResult':   applyDiscoverResult(payload); break;
    case 'machines.probePortsResult': applyProbeResult(payload); break;
    case 'machines.credentials':      applyCredentials(payload); break;
    /* --- end discovery --- */
    case 'transport.line_error':      setStatus('Device reported: ' + (payload.error || 'line error'), 'warn'); setLed('err', true); break;
    case 'receive.state':
    case 'receive.progress':          applyReceive(payload); break;
    case 'receive.done':              applyReceive(payload); setRcvStatus('Saved ' + (payload.target_name || ''), 'ok'); break;
    case 'receive.error':             applyReceive(payload); setRcvStatus(payload.message || 'Receive failed.', 'err'); break;
    case 'receive.overwrite_request': showOverwrite(payload); break;
    case 'log.entry':                 addLogEntry(payload); break;
    case 'file.info':                 setFile(payload.file); break;
    case 'toast':                     toast(payload.message, payload.level); break;
    /* ===== auto-update : begin ===== */
    case 'update.checked':
    case 'update.available':          applyUpdateResult(payload); break;
    case 'update.progress':           setUpdateStatus(payload.message || payload.stage,
                                        payload.percent >= 0 ? payload.percent : null); break;
    case 'update.done':               setUpdateStatus('Installed ' + payload.to_version + '. Reloading…');
                                      $('#btnUpdateInstall').disabled = true; break;
    case 'update.error':              setUpdateStatus(payload.message || 'Update failed.', null, true);
                                      $('#btnUpdateInstall').disabled = false; break;
    case 'update.restart':            if (!payload.restarted) setUpdateStatus('Restart Fusion to load the update.', null, true); break;
    /* ===== auto-update : end ===== */
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
  S.credentials = state.credentials || {};   // booleans only, never a password
  renderCredentialState();
  call('about.get').then((d) => {
    if (!d) return;
    renderProtocolNotice(d.protocol);
    if (d.update) renderUpdateStatus(d.update);
  });
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

  /* --- direct serial ports --- */
  $('#btnSerialRefresh').addEventListener('click', () => { S.serialPortsLoaded = false; refreshSerialPorts(); });
  $('#serialPortList').addEventListener('change', (e) => {
    const field = $('#machineForm').elements['serial_device'];
    if (field && e.target.value) field.value = e.target.value;
  });
  /* --- end direct serial ports --- */

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
  // discovery: the credential fields are not part of the form, so
  // they have to be refreshed by hand when the selection changes.
  if ($('#nportPass')) {
    $('#nportPass').value = '';
    $('#nportUser').value = '';
    renderCredentialState();
    call('machines.credentials', { id: m.id }).then((d) => { if (d) applyCredentials(d); });
  }
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
  /* --- direct serial ports: three types, not two ------------ */
  const type = form.elements['type'].value;
  const isSim = type === 'simulator';
  const isSerial = type === 'serial';
  ['host', 'port_index', 'data_port', 'cmd_port'].forEach((k) => {
    const node = form.elements[k];
    if (!node) return;
    const label = node.closest('label');
    /* A direct serial port has no host at all, so hide those fields
       rather than dimming them the way the Simulator does. */
    if (label) {
      label.classList.toggle('hidden', isSerial);
      label.style.opacity = isSim ? 0.45 : 1;
    }
    node.disabled = isSim || isSerial;
  });
  const row = $('#serialPortRow');
  if (row) row.classList.toggle('hidden', !isSerial);
  if (isSerial && !S.serialPortsLoaded) refreshSerialPorts();
  /* --- end direct serial ports ---------------------------------------------- */
}

/* --- direct serial ports ------------------------------------ */
async function refreshSerialPorts() {
  const list = $('#serialPortList');
  const field = $('#machineForm').elements['serial_device'];
  if (!list) return;
  S.serialPortsLoaded = true;
  list.replaceChildren(new Option('looking for ports…', ''));
  const data = await call('serial.listPorts', {});
  const ports = (data && data.ports) || [];
  const current = field ? field.value : '';
  /* Built with new Option() rather than innerHTML: a device name comes
     from the operating system, not from us, and never gets to be markup. */
  const options = [new Option(ports.length ? 'Choose a port…' : 'No serial ports found', '')];
  ports.forEach((p) => options.push(new Option(p.label || p.device, p.device)));
  if (current && !ports.some((p) => p.device === current)) {
    options.push(new Option(current + ' (not detected)', current));
  }
  list.replaceChildren(...options);
  list.value = current || '';
}
/* --- end direct serial ports ------------------------------------------------ */

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

/* ==================================================================== */
/* discovery: NPort finder, port probe, web-console credentials    */
/* Self-contained: nothing above this block calls into it except the    */
/* three push cases, the wire call in boot, and renderCredentialState() */
/* from applyState / selectMachineForEdit.                              */
/* ==================================================================== */
function wireNPortFinder() {
  $('#btnFindNport').addEventListener('click', async () => {
    const btn = $('#btnFindNport');
    if (btn.dataset.running === '1') {
      await call('machines.discoverStop', { scanId: S.scanId });
      return;
    }
    S.found = {};
    S.probes = {};
    renderNPortList();
    btn.dataset.running = '1';
    btn.textContent = 'Stop';
    setNPortStatus('Searching…');
    const data = await call('machines.discover', {
      timeout: 2.5,
      subnet: $('#nportSubnet').value.trim()
    });
    if (!data || !data.started) finishScan('');
  });

  $('#btnProbePorts').addEventListener('click', () => {
    const host = (($('#machineForm').elements['host'] || {}).value || '').trim();
    if (!host) { toast('Enter the NPort IP address first.', 'error'); return; }
    // Probe as many ports as the model has. Nothing discovered for this
    // address yet? Sweep the first four - an unreachable port costs well
    // under a second, and no NPort in this family has more.
    const known = S.found[host];
    probeDevice(host, (known && known.ports) || 4);
  });

  $('#btnCredSave').addEventListener('click', async () => {
    if (!S.editingId) return;
    const data = await call('machines.setCredentials', {
      id: S.editingId,
      username: $('#nportUser').value.trim(),
      password: $('#nportPass').value
    });
    $('#nportPass').value = '';          // never keep it in the DOM
    if (data) toast('Web console login stored in the ' + data.store.backend + '.', 'success');
  });

  $('#btnCredClear').addEventListener('click', async () => {
    if (!S.editingId) return;
    const data = await call('machines.clearCredentials', { id: S.editingId });
    $('#nportPass').value = '';
    if (data) toast(data.removed ? 'Login forgotten.' : 'Nothing was stored.', 'info');
  });

  renderNPortList();
}

function setNPortStatus(text) {
  $('#nportStatus').textContent = text || '';
}

function finishScan(message) {
  const btn = $('#btnFindNport');
  btn.dataset.running = '0';
  btn.textContent = 'Find NPort…';
  setNPortStatus(message);
}

function applyDiscoverResult(p) {
  if (p.scanId) S.scanId = p.scanId;
  if (p.message) setNPortStatus(p.message);
  if (p.device) {
    S.found[p.device.ip] = p.device;
    renderNPortList();
  }
  if (p.done) {
    const n = p.count || 0;
    finishScan(p.error
      ? 'Search failed: ' + p.error
      : (n ? n + ' device' + (n === 1 ? '' : 's') + ' found.'
           : 'Nothing answered. Try a subnet sweep — Wi-Fi often blocks broadcast.'));
  }
}

function probeDevice(ip, ports) {
  setNPortStatus('Probing ' + ip + '…');
  Object.keys(S.probes).forEach((k) => { if (k.indexOf(ip + ':') === 0) delete S.probes[k]; });
  // A host typed into the form has never been discovered, so give it a
  // row of its own - otherwise the probe result would have nowhere to go.
  if (!S.found[ip]) {
    S.found[ip] = { ip: ip, model: 'NPort at ' + ip, ports: ports || 2, source: 'manual' };
  } else if (ports > S.found[ip].ports) {
    S.found[ip].ports = ports;
  }
  renderNPortList();
  call('machines.probePorts', {
    host: ip,
    machineId: S.editingId,
    count: Math.max(1, ports || 2)
  });
}

function applyProbeResult(p) {
  if (p.port) {
    S.probes[p.host + ':' + p.port.portIndex] = p.port;
    renderNPortList();
  }
  if (p.done) {
    (p.ports || []).forEach((port) => { S.probes[p.host + ':' + port.portIndex] = port; });
    renderNPortList();
    if (p.error) setNPortStatus('Probe failed: ' + p.error);
    else if (p.consoleError) setNPortStatus('Probed ' + p.host + ' (web console: ' + p.consoleError + ')');
    else setNPortStatus('Probed ' + p.host + (p.consoleUsed ? ' with the web console.' : '.'));
  }
}

function renderNPortList() {
  const list = $('#nportList');
  list.innerHTML = '';
  const ips = Object.keys(S.found);
  if (!ips.length) {
    const li = el('li');
    li.appendChild(el('span', 'd-empty', 'No devices yet. Press "Find NPort…".'));
    list.appendChild(li);
    return;
  }
  ips.sort().forEach((ip) => {
    const d = S.found[ip];
    const li = el('li');

    const head = el('div', 'd-head');
    head.appendChild(el('span', 'd-model', d.model || 'Moxa device'));
    head.appendChild(el('span', 'd-ip', d.ip));
    const probe = el('button', 'btn btn-sm', 'Probe');
    probe.type = 'button';
    probe.addEventListener('click', () => probeDevice(d.ip, d.ports || 2));
    head.appendChild(probe);
    li.appendChild(head);

    const bits = [];
    if (d.name) bits.push(d.name);
    if (d.mac) bits.push(d.mac);
    if (d.firmware) bits.push('fw ' + d.firmware);
    if (d.serialNumber) bits.push('S/N ' + d.serialNumber);
    if (d.ports) bits.push(d.ports + (d.ports === 1 ? ' port' : ' ports'));
    if (d.source === 'tcp') bits.push('found by port scan');
    li.appendChild(el('div', 'd-sub', bits.join(' · ')));

    const row = el('div', 'd-ports');
    for (let i = 1; i <= Math.max(1, d.ports || 1); i += 1) {
      row.appendChild(portChip(d, i, S.probes[d.ip + ':' + i]));
    }
    li.appendChild(row);
    list.appendChild(li);
  });
}

function portChip(device, index, probe) {
  const chip = el('div', 'd-port');
  chip.appendChild(el('span', 'p-n', 'Port ' + index));
  if (probe) {
    if (probe.busy) chip.classList.add('is-busy');
    else if (!probe.reachable) chip.classList.add('is-dead');
    const detail = [];
    if (probe.modeLabel) detail.push(probe.modeLabel);
    if (probe.settings && probe.settings.summary) detail.push(probe.settings.summary);
    if (probe.modem) {
      detail.push('DSR ' + (probe.modem.dsr ? '1' : '0')
        + ' CTS ' + (probe.modem.cts ? '1' : '0')
        + ' DCD ' + (probe.modem.dcd ? '1' : '0'));
    }
    if (probe.busy) detail.push('in use');
    else if (!probe.reachable) detail.push('no answer');
    chip.appendChild(el('span', 'p-lines', detail.join(' · ')));
    if (probe.error) chip.title = probe.error;
  }
  const use = el('button', 'btn btn-sm', 'Use');
  use.type = 'button';
  use.addEventListener('click', () => useDevicePort(device, index, probe));
  chip.appendChild(use);
  return chip;
}

/* Fill the machine form's connection fields from a discovered port. The
   form is left dirty on purpose - the operator still has to press Save. */
function useDevicePort(device, index, probe) {
  const form = $('#machineForm');
  if (form.elements['type'].value === 'simulator') {
    form.elements['type'].value = 'moxa';
    toggleTypeFields(form);
  }
  setField(form, 'host', device.ip);
  setField(form, 'port_index', index);
  setField(form, 'cmd_port', (probe && probe.cmdPort) || (966 + index - 1));
  const realcom = !probe || probe.mode !== 'tcp_server';
  setField(form, 'data_port', (probe && probe.dataPort)
    || ((realcom ? 950 : 4001) + index - 1));
  if (probe && probe.settings) {
    const s = probe.settings;
    if (s.baud) setField(form, 'serial.baud', s.baud);
    if (s.data_bits) setField(form, 'serial.data_bits', s.data_bits);
    if (s.parity) setField(form, 'serial.parity', s.parity);
    if (s.stop_bits) setField(form, 'serial.stop_bits', s.stop_bits);
    if (s.flow_control) setField(form, 'serial.flow_control', s.flow_control);
  }
  toast('Filled in ' + device.ip + ' port ' + index + '. Press Save to keep it.', 'success');
}

function applyCredentials(p) {
  if (!p || !p.machineId) return;
  S.credentials[p.machineId] = !!p.has_credentials;
  if (p.machineId === S.editingId && p.username !== undefined) {
    $('#nportUser').value = p.username || '';
  }
  renderCredentialState(p.store);
}

function renderCredentialState(store) {
  const chip = $('#credState');
  if (!chip) return;
  const has = !!S.credentials[S.editingId];
  chip.textContent = has ? 'login stored' : 'no login stored';
  chip.className = 'chip ' + (has ? 'chip-ok' : 'chip-idle');
  if (store && !store.secure) {
    chip.title = 'Stored in ' + store.backend + ' — ' + (store.warning || 'weaker than a keychain');
  }
}

/* ==================================================================== */
/* end discovery                                                   */
/* ==================================================================== */

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
  wireUpdateCard();
}

/* ===== auto-update : begin ===== */
const U = { latest: '', notesUrl: '', available: false, dev: false };

function saveUpdateSetting(patch) {
  call('settings.save', { settings: { update: patch } });
}

function wireUpdateCard() {
  $('#updateAutoCheck').addEventListener('change', (e) => saveUpdateSetting({ auto_check: e.target.checked }));
  $('#updateAutoInstall').addEventListener('change', (e) => saveUpdateSetting({ auto_install: e.target.checked }));
  $('#updatePrereleases').addEventListener('change', (e) => saveUpdateSetting({ include_prereleases: e.target.checked }));
  $('#updateRepo').addEventListener('change', (e) => saveUpdateSetting({ repo: e.target.value.trim() }));
  $('#btnUpdateCheck').addEventListener('click', () => {
    setUpdateStatus('Checking GitHub…');
    call('update.check', {});
  });
  $('#btnUpdateInstall').addEventListener('click', () => {
    if (U.dev) { setUpdateStatus('Development install — use git pull.', null, true); return; }
    $('#btnUpdateInstall').disabled = true;
    setUpdateStatus('Starting…', 0);
    call('update.install', {});
  });
  $('#updateNotesLink').addEventListener('click', (e) => {
    if (!U.notesUrl) e.preventDefault();
  });
}

/* Renders `about.get -> update` (settings + last known result). */
function renderUpdateStatus(status) {
  const cfg = status.settings || {};
  $('#updateCurrent').textContent = status.current || '—';
  $('#updateAutoCheck').checked = !!cfg.auto_check;
  $('#updateAutoInstall').checked = !!cfg.auto_install;
  $('#updatePrereleases').checked = !!cfg.include_prereleases;
  if (document.activeElement !== $('#updateRepo')) $('#updateRepo').value = cfg.repo || '';
  $('#updateLastCheck').textContent = fmtWhen(cfg.last_check);
  U.dev = !!status.developmentInstall;
  $('#updateDevNote').classList.toggle('hidden', !U.dev);
  $('#btnUpdateInstall').disabled = U.dev;
  if (status.result && status.result.latest) applyUpdateResult(status.result);
  else $('#updateLatest').textContent = cfg.last_seen_version || '—';
  if (cfg.last_error) setUpdateStatus(cfg.last_error, null, true);
}

/* Renders a check result ({available, latest, notes, html_url, error}). */
function applyUpdateResult(r) {
  if (!r) return;
  if (r.current) $('#updateCurrent').textContent = r.current;
  if (r.checked) $('#updateLastCheck').textContent = fmtWhen(r.checked);
  $('#updateLatest').textContent = r.latest || '—';
  U.latest = r.latest || '';
  U.notesUrl = r.html_url || '';
  U.available = !!r.available;

  const banner = $('#updateBanner');
  if (r.available) {
    $('#updateBannerText').textContent = 'Update ' + r.latest + ' available';
    const link = $('#updateNotesLink');
    link.href = r.html_url || '#';
    link.title = (r.notes || '').slice(0, 400);
    banner.classList.remove('hidden');
    $('#btnUpdateInstall').disabled = U.dev;
    setUpdateStatus(U.dev ? 'Development install — use git pull.' : '', null, U.dev);
  } else {
    banner.classList.add('hidden');
    if (r.error) setUpdateStatus(r.error, null, true);
    else if (r.latest) setUpdateStatus('Up to date.');
  }
}

function setUpdateStatus(text, percent, isError) {
  const box = $('#updateStatus');
  box.textContent = (percent === null || percent === undefined)
    ? (text || '')
    : (text || '') + ' ' + Math.round(percent) + '%';
  box.style.color = isError ? 'var(--err)' : '';
}

function fmtWhen(epochSeconds) {
  if (!epochSeconds) return 'never';
  const d = new Date(epochSeconds * 1000);
  if (isNaN(d.getTime())) return 'never';
  return d.toLocaleString();
}
/* ===== auto-update : end ===== */

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
