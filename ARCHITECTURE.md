# MoxaSerial — architecture

DNC for Autodesk Fusion: send a post-processed NC program to a CNC control
over RS-232 through a Moxa NPort, using the device's native TCP protocol
instead of a virtual COM port driver, and receive programs back.

Stdlib only — Fusion embeds its own CPython (3.12) and `pip install` into it
is not something a shop should have to do.

## 1. Layering rule

```
Fusion only →  MoxaSerial.py · moxaserial_loader.py · moxaserial/ui/*   may import adsk
Portable    →  moxaserial/bridge.py                                     action router
               moxaserial/dnc/*  config.py  log.py  events.py  paths.py  engines
               moxaserial/transport/*                                   bytes on a wire
```

**Nothing below `moxaserial/ui/` may import `adsk`, at module scope or
otherwise.** That one rule is what makes `tools/dev_server.py` and the test
suite possible: the whole add-in minus the Fusion chrome runs in a plain
CPython interpreter. `moxaserial/ui/*` may import `adsk`, but only *lazily*
inside functions, so even those modules import cleanly off-Fusion.

## 2. Module map

| Module | Role |
|---|---|
| `MoxaSerial.py` | `run()` / `stop()`. Bootstraps the loader, delegates to `moxaserial.ui.app`. |
| `moxaserial_loader.py` | UNC-safe importer: `spec_from_file_location` by explicit path, bypassing `sys.path`, because Windows UNC installs (`\\server\share\…`) break the standard finders. `purge()` drops `moxaserial.*` from `sys.modules` so stop→start picks up edited code. |
| `paths.py` | Per-user data dir. `MOXASERIAL_DATA_DIR` overrides everything (tests, dev server). |
| `events.py` | Thread-safe pub/sub. Dotted topics matched exactly, by `prefix.*`, or `*`. A subscriber exception is caught and routed to an error hook — a bad listener must never kill an engine thread mid-send. |
| `log.py` | `RotatingFileHandler` (1 MB × 3) + a 2000-entry ring buffer the Log page reads + a handler republishing every record as `log.entry`. |
| `config.py` | Versioned JSON settings (§5). Normalisation, validation, migration, atomic saves. |
| `bridge.py` | The action router (§4). Owns the store, the sender, the receiver, and a `Host` abstraction for the few things only the embedding app can do. |
| `transport/base.py` | Abstract `Transport`: `open/close/write/read/purge`, `set_line_params`, `set_flow_control`, `set_dtr`, `set_rts`, `get_modem_status()`. Locking, stats and events live in the base; subclasses implement `_do_*`. |
| `transport/fake.py` | `FakeTransport` — a simulated control: finite buffer with real XON/XOFF water marks, wire-speed emulation, modem-line delays, injectable faults, punch-out thread. Used by the tests, the `Simulator` machine and the dev server. |
| `transport/moxa.py` | Native NPort transport (§6). |
| `transport/aspp.py` | ASPP codec: opcodes, baud/mode tables, encoders, per-opcode response lengths, `split_frames`, NOTIFY/POLLING decoders. Pure functions, no sockets. |
| `dnc/preprocess.py` | Pure functions. Owns the escape conventions and the CIMCO transmit filter order (triggers → omit → remove chars → case → whitespace → line ending). |
| `dnc/sender.py`, `dnc/receiver.py` | The two engines, one daemon thread per job (§3). The receiver's filter chain is pure and tested directly. |
| `ui/app.py` | Lifecycle. `FusionHost` implements file dialogs, last-post lookup, toast, reveal-in-Finder. |
| `ui/palette.py`, `ui/commands.py`, `ui/toast.py`, `ui/lastpost.py` | Palette + HTML bridge, toolbar commands, notification, "most recent posted program". |

`create_transport(machine, bus)` picks by `machine["type"]`: `simulator` →
`FakeTransport`, `moxa` → `MoxaTransport`.

`tools/dev_server.py` serves `resources/palette` over stdlib `http.server`
and wires the *same* `Bridge` to the *same* engines with every machine
forced onto `FakeTransport`, so the whole UI is driveable in a browser with
no Fusion and no hardware. `tools/nport_sim.py` implements the device side
of §6 for bench tests.

## 3. Threading model

Four kinds of thread, and no others.

1. **Fusion's main (UI) thread** — `run()`/`stop()`, command handlers,
   `incomingFromHTML`, `sendInfoToHTML`. Every Fusion API call happens here.
2. **The send thread** (`moxa-sender`), one per job, daemon. Driven from
   outside by two `threading.Event`s: `_stop_evt`, checked at every chunk
   boundary and inside both blocking waits, and `_resume_evt`, cleared by
   `pause()` and set by `resume()` — and *also* set by `stop()`, so a paused
   loop wakes up and then sees the stop flag.
3. **The receive thread** (`moxa-receiver`), one per job, daemon. Same
   shape, plus it can block on `_overwrite_evt`, released by
   `resolve_overwrite(token, decision)` from the UI thread with a 300 s
   backstop so a dismissed prompt can never wedge the thread.
4. **Simulation threads** (FakeTransport only). Never present with real
   hardware.

**Locking.** `EventBus`: one `RLock` around the subscriber list, handlers
called *outside* it so a handler may publish. `Transport`: one `RLock` for
open/closed state and stats; `close()` is idempotent and safe to call while
a `read()` blocks. `Sender`/`Receiver`: one `RLock` around the progress
dataclass, so `snapshot()` from the UI thread is always coherent.
`ConfigStore`: one `RLock`; `data` and `machines()` return deep copies and
saves are atomic (temp file + `os.replace`).

**Back-pressure.** Progress events are throttled to one per 80 ms
(`PROGRESS_INTERVAL`), and the palette queue *collapses* consecutive
`send.progress` / `receive.progress` entries — each is a complete idempotent
snapshot, so only the latest matters.

**The thread hop.** `sendInfoToHTML` is main-thread-only, so engine events
go: engine thread → `bus.publish` → `Bridge.push` → `PaletteBridge._enqueue`
(a `deque(maxlen=400)`) → `app.fireCustomEvent` → **main thread**
`_PushHandler.notify` → `_drain` → `sendInfoToHTML` → `onPush` in `app.js`.
`fireCustomEvent` is only called when no drain is already pending, so a
burst of 500 progress events costs one hop. The other direction is
synchronous and already on the main thread: `adsk.fusionSendData` →
`incomingFromHTML` → `Bridge.handle_json` → `args.returnData`.

In the dev server the same `Bridge` sits behind different pipes: `POST /api`
for request/reply, `GET /events` for an SSE stream. `app.js` picks the pipe
by sniffing `window.adsk`, so the same HTML runs in both.

## 4. Message schema

**Every** reply has one of two shapes. An unknown action, or an exception
inside a handler, is an `ok:false` reply — never a raised exception. The
reply string must be **non-empty**; Fusion treats `""` as failure.

```jsonc
{ "ok": true,  "action": "<name>", "data": { /* action specific */ } }
{ "ok": false, "action": "<name>", "error": "human readable sentence" }
```

### Inbound — UI → Python

| Action | Payload | `data` on success |
|---|---|---|
| `ui.ready`, `state.get` | `{}` | full state (below) |
| `machines.list` | `{}` | `{machines}` |
| `machines.new` | `{name?}` | `{machine}` (not persisted) |
| `machines.save` | `{machine}` | `{machine, machines, warnings}` |
| `machines.delete` | `{id}` | `{removed, machines}` |
| `machines.duplicate` | `{id}` | `{machine, machines}` |
| `machines.setDefault` | `{id}` | `{settings}` |
| `machines.test` | `{id, sync?}` | `{started, machineId}`; the result arrives as a `machines.testResult` push. Runs on a worker thread so a wrong IP cannot freeze Fusion; `sync:true` returns inline (tests, dev server). |
| `settings.get`, `settings.save` | `{}`, `{settings}` | `{settings, enums}`, `{settings}` |
| `theme.set` | `{theme}` | `{settings}` |
| `file.browse` | `{}` | `{file}` or `{path:"", cancelled:true}` |
| `file.lastPost` | `{}` | `{file, candidates?}` or `{file:{}, error, candidates}` |
| `file.preview` | `{path?, machineId?}` | `{path, name, lines, lineCount, byteCount, droppedLines, sourceLines, truncated}` |
| `file.reveal` | `{path}` | `{revealed}` |
| `send.start` | `{machineId?, path?, useLastPost?, source?}` | `{started, machine, path}`. With `confirm_before_send` on and no `confirmed:true`, replies `{started:false, needsConfirm:true, …}` and sends nothing. |
| `send.pause`, `send.resume`, `send.stop` | `{}` | `{state}` |
| `send.resend` | `{}` | `{started}` |
| `receive.start` | `{machineId?, filename?}` | `{started, machine}` |
| `receive.stop` | `{}` | `{state}` |
| `receive.overwriteResponse` | `{token, decision}` | `{accepted}` |
| `log.list`, `log.clear`, `log.openFile` | `{level?, text?, limit?}` | `{entries, counts, path}`, `{cleared}`, `{path, revealed}` |
| `about.get` | `{}` | `{version, host, paths, protocol}` |

Omitting `machineId` means "the active machine" — the default, or the last
used one, per `settings.machine_selection`.

### Outbound — Python → UI

Fire and forget, via `sendInfoToHTML(action, json)` or an SSE frame.

| Action | Payload |
|---|---|
| `send.state`, `send.progress` | `SendProgress` (progress is throttled and collapsible) |
| `send.done`, `send.error` | `SendProgress` + `{elapsed_s}` / `{message}` |
| `send.log` | `{message, level}` — an inline milestone for the status line |
| `receive.state`, `receive.progress` | `ReceiveProgress` |
| `receive.done`, `receive.error` | `ReceiveProgress` + `{path, bytes}` / `{message}` |
| `receive.overwrite_request` | `{token, path, name, suggested}` — if never answered, the capture is still saved under the next free name and the job ends in ERROR naming that file |
| `machines`, `file.info` | `{machines}` / `{file}` — the list or the selection changed |
| `log.entry` | `LogEntry` `{seq, ts, time, level, source, message}` |
| `toast` | `{level, message}` |

### Payload shapes

`SendProgress` and `ReceiveProgress` are the two engine dataclasses,
serialised field for field — `dnc/sender.py` and `dnc/receiver.py` are the
authoritative definitions. Worth knowing without reading them:

* `send.state ∈ IDLE|CONNECTING|WAITING_READY|SENDING|PAUSED|STOPPED|DONE|ERROR`;
  `receive.state ∈ IDLE|CONNECTING|WAITING|RECEIVING|STOPPED|DONE|ERROR`.
* `cps` and `eta_s` are CIMCO's "CPS:" and "Remaining time:"; `errors`
  counts connect retries, handshake timeouts and break/DC4 aborts;
  `inbound_chars` is what "Break after receiving characters" counts.
* `line_index` is the 0-based index of the in-flight line, `-1` for none.
* `window` is why the UI never needs the whole program in memory: a slice of
  `{i, text, current}` around the in-flight line (6 before, 8 after) ships
  with each progress event, so the preview scrolls correctly for a
  200 000-line program at no extra cost. `receive.tail` is the same idea —
  the last 14 lines captured.
* `FileInfo` is `{path, name, dir, size, mtime, mtimeText, source}` with
  `source ∈ nc-program | folder-scan | browse | send | …`; `LogEntry` is
  `{seq, ts, time, level, source, message}`.

## 5. Settings

One versioned JSON document at `<appdata>/settings.json`: globals plus a
list of machines. A machine is identity and connection fields plus three
sections — `serial`, `send`, `receive` — most of whose keys mirror a named
CIMCO Edit DNC option. `config.default_machine()` is the authoritative list.

**Character fields** (`*_chars`, `*_trigger`, `remove_chars`,
`omit_lines_containing`, `line_ending_custom`, `parity_insert`) go through
`preprocess.unescape`, which accepts `\n \r \t \0 \\`, `\xNN` **and**
CIMCO's `\NN` decimal form; one space after a decimal escape is a
separator, so `\13 \10` is exactly CR+LF. `preprocess.char_set` reads the
set-valued fields, where whitespace separates entries and `\32` means a
real space. Receive keeps the **wire** line ending (`line_ending`, possibly
`AUTO`) separate from the one written to **disk** (`save_line_ending`).

**Three concerns, deliberately kept apart.** `normalize_*` is *lenient* —
unknown keys dropped, missing keys defaulted, numbers clamped, choices
matched case-insensitively — so a hand-edited or older file always loads.
`validate_machine` is *advisory*: problems matching `ADVISORY_MARKERS` are
warnings that still save, everything else blocks. `upsert_machine` is
*strict* and checks the **raw** name before normalisation, which would
otherwise silently invent "Unnamed" for a save coming from the UI.
`migrate(raw)` walks `_MIGRATIONS[version]` one step at a time, only ever
adding fields and carrying old values forward; a file from a *newer* schema
is left alone rather than downgraded.

## 6. The wire protocol (ASPP)

Reconstructed from Moxa's GPL `npreal2` driver sources and Moxa's IPSerial
documentation, and exercised end to end against `tools/nport_sim.py`.

**Sockets.** The command socket (`966 + n`) is connected first, then the
data socket (`950 + n` in Real COM mode, `4001 + n` in TCP Server mode).
Both get `TCP_NODELAY` and `SO_KEEPALIVE`. No banner, no auth.

**Framing.** Requests are `[opcode][len][payload]`. Replies are *not*
uniformly length-prefixed: most are `[opcode] 'O' 'K'`; `PORT_INIT` and
`LSTATUS` are `[op][3][dsr][cts][dcd]`; `OQUEUE`/`IQUEUE`/`WAIT_OQUEUE` are
`[op][2][lo][hi]`. `aspp.RESPONSE_LENGTHS` is the table; an unknown opcode
means the stream desynchronised, and the transport reconnects.

**Unsolicited frames** on the command socket:

* `POLLING` (0x27) `[0x27][1][token]` — the host must answer `ALIVE`
  `[0x28][1][token]` or the NPort drops the connection.
* `NOTIFY` (0x26) `[0x26][flags][msr][0]` — `flags & 0x20` means `msr`
  carries absolute CTS (0x10) / DSR (0x20) / RI (0x40) / DCD (0x80) levels;
  the low bits report parity / framing / overrun / break.

**Open sequence.** Connect cmd, connect data, start the reader thread, then
`PORT_INIT` (baud index or 0xFF, mode byte, DTR, RTS, RTS/CTS flow ×2, XON,
XOFF) whose reply gives the initial modem lines; `SETBAUD` if the rate is
not in the 19-entry table; `XONXOFF` chars if software flow; `TX_FIFO`.

**Flow control policy.** Hardware RTS/CTS is always done by the device.
Software XON/XOFF is done by the device when `serial.device_flow_control` is
on (the default) *and* by the sender above the transport, which still
watches the RX stream for XOFF/XON/DC4 — so either path stops the flow.
DTR/DSR flow control has no ASPP command: the NPort's own web setting
applies, and *wait for DSR* still works from modem status.

**Threads.** `_reader_loop` owns command-socket receive. `_command()`
serialises requests with `_cmd_lock`, sets `_pending_op`, writes under
`_cmd_write_lock` (shared with the ALIVE reply path) and waits on
`_reply_cv`. Reader death sets `_reader_error`, wakes waiters, publishes
`transport.error` and flips `command_channel_available` off — while the data
path keeps working, so a transfer in flight can finish.

**Sender integration.** After the epilogue the sender calls
`transport.drain()` (an abortable `WAIT_OQUEUE` loop) so DONE means the
bytes really left the UART, and `_finish_stopped` calls `purge(tx=True)`
(`FLUSH`) so Stop really stops.

## 7. Learned the hard way

**Fusion has no toast API.** `ui.messageBox` is modal and stops everything;
`ui.statusMessage` is unstyled with an indeterminate lifetime;
`ui.progressBar.show(msg, min, max, isModal=False)` is lower-right and
non-modal. `ui/toast.py` shows a short-lived non-modal progress bar *and*
sets `statusMessage` (which survives the bar disappearing), auto-hiding
after 4 s, falling back to `statusMessage` alone and finally to a message
box — but only for errors, and never during a transfer. `progressBar.hide()`
is assumed *not* to be safe from a `threading.Timer` thread, so the hide is
routed back onto the main thread through its own custom event, exactly like
engine progress.

**"Most recent posted program" cannot be read from browser order.**
`cam.ncPrograms` holds the document's post deliverables, and each
`NCProgram.parameters` carries `nc_program_output_folder`,
`nc_program_filename` (base name, no extension), `nc_program_nc_extension`
and `nc_program_default_output_folder`, read as `param.value.value`. The
expected path is `output_folder / (filename + nc_extension)`, falling back
to the default folder when the per-program one is blank. Every NC program is
resolved this way and **the one whose file has the newest mtime wins** —
that, not browser order or program numbering, answers "what did I just
post?". Two fallbacks follow: a scan of every folder found that way plus
`settings.watch_folders`, then Fusion's default NC folder, for the newest
file with an NC-ish extension, excluding `.failed` stubs, `.log`, `.tmp`,
`.bak` and dotfiles. Off-Fusion the first strategy returns empty and the
module degrades to the other two.

**`app.unregisterCustomEvent` must be called defensively before
registering**, because a crashed previous session can leave the id claimed.

**Toolbar commands set `isAutoExecute = True`** and do their work in
`execute`, so a click performs the action instead of opening an empty
dialog. Workspace `CAMEnvironment`; *Send Last Program* is additionally
added to `CAMActionPanel` on the Milling and Turning tabs, next to Post
Process.

**Still unverified, worth confirming on unfamiliar firmware:** command port
966 vs the 996 one manual prints; parity encoding even = 8 / odd = 16;
cmd-then-data connect order in TCP Server mode; whether NOTIFY is on by
default (send `START_NOTIFY` if not); `WAIT_OQUEUE` timeout units and the
hi-byte decode; whether the NPort strips XON/XOFF from RX when device flow
control is on; a DTR/RTS glitch on connect. Also whether
`nc_program_nc_extension` exists on documents from older Fusion versions,
and whether `isAutoExecute` reliably fires `execute` with no visible dialog
in every Fusion build.

## 8. Testing

`python3 -m pytest` — no Fusion, no hardware; `test_moxa.py` drives the
transport and the whole sender over loopback TCP to `tools/nport_sim.py`.
`conftest.py` redirects `MOXASERIAL_DATA_DIR` into a per-test temp directory
and resets the `LogManager` singleton, so no test can touch real settings or
the real log. `wait_until(predicate, timeout)` is the only concurrency
primitive the tests use — no bare `sleep`-and-hope assertions on engine
state. `tests/test_cimco_parity.py` holds one case per CIMCO Edit DNC option
we claim to implement, and guards the UI contract: every key of
`default_machine()` must have a form control in `index.html`, and every
`data-enum` in the form must be published in `bridge.ENUMS`.

Lint `ruff check .`; publication gate `python3 tools/check_public_tree.py`.
