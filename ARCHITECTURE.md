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
| `update.py` | Auto-update (§8): GitHub Releases check, download + SHA verify, folder swap with rollback, and the `UpdateService` that schedules it all. Fusion-free. |
| `discovery.py` | Finding NPorts and probing their serial ports (§9). UDP search on port 4800, a TCP fallback sweep, and a bounded per-port ASPP probe. |
| `nport_console.py` | Read-only client for the device's GoAhead web console (§9): login handshake, serial port settings, per-port operation mode. |
| `secrets.py` | Per-machine web-console credentials in the OS keychain (§9). Never in `settings.json`, never in the log, never in a bridge payload. |
| `transport/base.py` | Abstract `Transport`: `open/close/write/read/purge`, `set_line_params`, `set_flow_control`, `set_dtr`, `set_rts`, `get_modem_status()`. Locking, stats and events live in the base; subclasses implement `_do_*`. |
| `transport/fake.py` | `FakeTransport` — a simulated control: finite buffer with real XON/XOFF water marks, wire-speed emulation, modem-line delays, injectable faults, punch-out thread. Used by the tests, the `Simulator` machine and the dev server. |
| `transport/moxa.py` | Native NPort transport (§6). |
| `transport/aspp.py` | ASPP codec: opcodes, baud/mode tables, encoders, per-opcode response lengths, `split_frames`, NOTIFY/POLLING decoders. Pure functions, no sockets. |
| `transport/serial_port.py` | Direct RS-232 through a local port (§7): `SerialTransport` over two private backends — `termios`/`fcntl`/`select` on POSIX, `ctypes` + `kernel32` on Windows — plus `list_serial_ports()`. |
| `dnc/preprocess.py` | Pure functions. Owns the escape conventions and the CIMCO transmit filter order (triggers → omit → remove chars → case → whitespace → line ending). |
| `dnc/sender.py`, `dnc/receiver.py` | The two engines, one daemon thread per job (§3). The receiver's filter chain is pure and tested directly. |
| `ui/app.py` | Lifecycle. `FusionHost` implements file dialogs, last-post lookup, toast, reveal-in-Finder. |
| `ui/palette.py`, `ui/commands.py`, `ui/toast.py`, `ui/lastpost.py` | Palette + HTML bridge, toolbar commands, notification, "most recent posted program". |
| `ui/updater.py` | The one Fusion-only half of auto-update: restart the add-in in place (§8). |

`create_transport(machine, bus)` picks by `machine["type"]`: `simulator` →
`FakeTransport`, `moxa` → `MoxaTransport`, `serial` → `SerialTransport`.

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
| `serial.listPorts` | `{}` | `{ports:[{device, label, description}]}` — serial ports on this computer, for the machine form's port dropdown (§7). Never fails: an empty list just means "type the name in". |
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
| `about.get` | `{}` | `{version, host, paths, protocol, update, discovery, credentialStore}` |
| `machines.discover` | `{timeout?, targets?, subnet?, udp?, sync?}` | `{started, scanId}`; devices arrive as `machines.discoverResult` pushes. `sync:true` returns `{scanId, devices, count}` inline (§9). |
| `machines.discoverStop` | `{scanId?}` | `{stopped, count}` — aborts that subnet sweep, or every running one. Each scan owns its own stop flag, so two scans cannot cancel each other. |
| `machines.probePorts` | `{host?, machineId?, count?, useCredentials?, sync?}` | `{started, host, count}`; results arrive as `machines.probePortsResult` pushes. `sync:true` returns `{host, machineId, ports, consoleUsed, consoleError}` inline. |
| `machines.setCredentials` | `{id, username, password}` | `{machineId, has_credentials, username, store}` — **the password is never echoed back** |
| `machines.clearCredentials`, `machines.credentials` | `{id}` | `{machineId, has_credentials, username, store, removed?}` |
| `update.status` | `{}` | `{settings, current, busy, developmentInstall, addinDir, lastCheck, lastError, result}` |
| `update.check` | `{sync?}` | `{started}`; the result arrives as an `update.checked` push (plus `update.available` when there is one). `sync:true` returns inline (tests, dev server). Runs on a worker thread so a stalled GitHub cannot freeze Fusion. |
| `update.install` | `{release?}` | `{started, error?}` — progress arrives as `update.progress`, the outcome as `update.done` / `update.error` (§8). |

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
| `update.checked`, `update.available` | the §8 check result — `checked` on every check, `available` only when a newer release exists |
| `update.progress` | `{stage, percent, message}` — `stage ∈ check\|download\|verify\|extract\|install\|restart` |
| `update.done`, `update.error` | `{from_version, to_version, backup, addin_dir, pruned}` / `{message}` |
| `update.restart` | `{restarted}` — `false` means the operator must restart Fusion |
| `machines.discoverResult` | `{scanId, phase: "udp"\|"tcp"\|"done", message?, device?, done?, devices?, count?, error?}` — one message per device as it is found, then a final `done` |
| `machines.probePortsResult` | `{host, port?}` per port, then `{host, machineId, ports, consoleUsed, consoleError, done:true}` |
| `machines.credentials` | `{machineId, has_credentials, username, store}` — **boolean only, never a password** |

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
`type ∈ moxa | serial | simulator` selects which connection fields matter:
a `serial` machine carries `serial_device` (§7) and ignores `host` /
`data_port` / `cmd_port`, exactly as a `moxa` machine ignores
`serial_device`. Schema 3 adds the global `update` section (§8):
`auto_check`, `auto_install`, `include_prereleases`,
`check_interval_hours`, `repo`, plus the written-back `last_check`,
`last_seen_version` and `last_error`.

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

## 7. Direct serial ports

`transport/serial_port.py`. Machine type `serial`: an onboard UART, a
USB-serial adapter, or a virtual COM port from some other driver. Same
`Transport` contract as the Moxa path, so both engines are unchanged — a
`serial` machine differs from a `moxa` one only by carrying `serial_device`
instead of `host` / `data_port` / `cmd_port`. Stdlib only:
`os`/`termios`/`fcntl`/`select` on POSIX, `ctypes` against `kernel32` on
Windows. No pyserial.

**Two private backends, one public transport.** `SerialTransport` holds the
shared state (device name, DTR/RTS shadow, modem-change events) and never
touches a platform API itself; `_PosixSerialBackend` and
`_WindowsSerialBackend` implement the same nine operations.

| Operation | POSIX | Windows |
|---|---|---|
| open | `os.open(O_RDWR\|O_NOCTTY\|O_NONBLOCK)`, `flock(LOCK_EX\|LOCK_NB)` for exclusive use | `CreateFileW(\\.\COMn)`, share mode 0 |
| line params | `tcgetattr`/`tcsetattr`: raw mode, `CSIZE`, `PARENB`/`PARODD`(/`CMSPAR`), `CSTOPB`, `B<rate>` | DCB `BaudRate`, `ByteSize`, `Parity`, `StopBits`, `fBinary` |
| flow control | `IXON`/`IXOFF` + `VSTART`/`VSTOP`, `CRTSCTS`, (macOS) `CDTR_IFLOW`/`CDSR_OFLOW` | `fOutX`/`fInX` + `XonChar`/`XoffChar`, `fOutxCtsFlow` + `fRtsControl`, `fOutxDsrFlow` + `fDsrSensitivity` |
| DTR / RTS | `TIOCMBIS` / `TIOCMBIC` | `EscapeCommFunction` SETDTR/CLRDTR/SETRTS/CLRRTS |
| modem status | `TIOCMGET` → CTS/DSR/DCD/RI | `GetCommModemStatus` |
| read | `select` on the fd **and a self-pipe**, then `os.read` | `SetCommTimeouts` + blocking `ReadFile`, sliced |
| write | `select` for writability, `os.write`, abortable | `WriteFile` with a write timeout, abortable |
| purge | `tcflush` TCIFLUSH/TCOFLUSH/TCIOFLUSH | `PurgeComm` |
| pending_tx / drain | `TIOCOUTQ`, then `tcdrain` | `ClearCommError` → `COMSTAT.cbOutQue`, then `FlushFileBuffers` |
| break | `TIOCSBRK`/`TIOCCBRK`, else `tcsendbreak` | `SetCommBreak` / `ClearCommBreak` |

`termios` publishes a different subset of the `TIOC*` constants per platform,
so `_ioctl_const` takes the module's value when it is there and falls back to
the macOS/Linux numeric value when it is not — a constant Python does not
export is not a feature the kernel lacks. The `TIOCM_*` bit values are
identical on macOS and Linux, so one table covers both.

**Draining means draining.** `drain()` polls the *output queue* to zero
before calling `tcdrain`/`FlushFileBuffers`, rather than calling them first:
`tcdrain` is uninterruptible, so a control holding CTS low would wedge Stop.
Polling first makes the final call a formality. This is what makes DONE mean
"the control has the program" on a direct port, the same promise
`WAIT_OQUEUE`/`OQUEUE` gives on the NPort.

**Closing unblocks a read.** POSIX `select`s on the port *and* a self-pipe;
`close()` writes one byte to the pipe, waits (up to 1 s) for the reader to
leave, and only then closes the descriptor — so a descriptor is never pulled
out from under a blocked `select` and handed by the kernel to some other part
of Fusion. Windows keeps every `ReadFile` bounded to 50 ms of comm timeout,
re-checks the stop flag, and calls `CancelIoEx` best-effort.

**Port enumeration.** `list_serial_ports()` → `[{device, label,
description}]`, USB-looking adapters first. macOS globs `/dev/cu.*` — `cu`
not `tty`, because opening a `tty.*` device blocks until DCD, which a CNC
control generally never asserts — and names them from one `ioreg -r -c
IOSerialBSDClient -l` call. Linux prefers `/dev/serial/by-id/*` and then the
raw `ttyUSB*`/`ttyACM*`/`ttyS*` nodes. Windows reads
`HKLM\HARDWARE\DEVICEMAP\SERIALCOMM` through `winreg`. Enumeration never
raises — it is a convenience, and the device can always be typed in. The
bridge publishes it as `serial.listPorts`, behind the **Refresh ports**
button next to the machine form's port dropdown.

**UNVERIFIED: everything Windows.** The DCB and COMSTAT layouts, the
flow-control bit mapping and the `\\.\COMn` naming are from the Win32
documentation and match what pyserial does, but no code here has run against
a real COM port. On Linux a baud rate with no `B<rate>` constant is reported
as unsupported rather than set through `BOTHER`/`termios2`; macOS takes the
literal rate. Mark/space parity needs `CMSPAR` (Linux only) and degrades to
no parity with a warning elsewhere; 1.5 stop bits has no POSIX expression and
becomes 2.

## 8. Auto-update

The add-in watches its own GitHub Releases and can replace itself in place,
without the operator downloading a zip or touching Fusion's AddIns folder.
`update.py` is Fusion-free like everything else below `ui/`; `ui/updater.py`
holds the one thing that is not.

**What CI publishes.** One release per version: `MoxaSerial-<v>.zip` (a
top-level `MoxaSerial/` folder), the `.pkg` and `-Setup.exe` installers, and
`SHA256SUMS`. The updater consumes **only** the zip and `SHA256SUMS` — the
installers need elevation, which an add-in cannot ask for from inside Fusion.

**Checking.** `check_for_update(current, repo, timeout, include_prereleases)`
GETs `https://api.github.com/repos/<repo>/releases/latest` with a
`User-Agent` (GitHub rejects requests without one). With
`include_prereleases` it reads `/releases` and picks the highest version
itself, skipping drafts. **It never raises**: offline, DNS failure, 404, 401
and a 403/429 with `X-RateLimit-Remaining: 0` each come back as
`available:false` plus one actionable sentence in `error` — the caller is a
background timer thread with nowhere to propagate to. The comparison is
semver-aware (`compare_versions`): `v` prefixes stripped, `1.2 == 1.2.0`,
`1.0.0-rc.1 < 1.0.0`, numeric pre-release identifiers sort numerically and
below alphanumeric ones, `+build` ignored. **The installed version comes from
`MoxaSerial.manifest`**, not `moxaserial.__version__`, because the manifest is
what Fusion shows in Scripts and Add-Ins.

**Downloading.** `download_update(zip_url, sha_url, dest_dir)` streams to a
temp file in 64 kB chunks, hashing as it goes, then applies three checks in
order, each deleting the download and raising `UpdateError`: SHA256 against
`SHA256SUMS` (a release with no such asset only warns — unverifiable is not
corrupt — but a *mismatch* is always fatal); the file opens as a zip and
passes `testzip()`; the zip contains `MoxaSerial/MoxaSerial.manifest`, which
is what stops a wrong URL installing some other project over the add-in.

**Applying.** `apply_update(zip_path, addin_dir)` refuses a development
install, extracts into `.MoxaSerial.staging-<pid>-<ts>` *next to* the add-in
folder so the swap is a rename on one filesystem (rejecting any member whose
path escapes the staging directory), renames the live folder to
`MoxaSerial.bak-<version>`, then renames the staged folder into the live
name. A failure at the first rename means nothing changed; a failure at the
second restores the backup, and if even that fails the error names the exact
folder to rename by hand. **Nothing inside the add-in folder is preserved** —
settings and logs live in the app-data directory, so the folder holds only
shipped files, and keeping strays would let a module deleted in the new
version linger and shadow its replacement. The two newest
`MoxaSerial.bak-*` folders are kept.

**Development installs are refused**, with "development install — update with
git pull": replacing someone's working tree with a release zip is not a
recoverable mistake. Three signals, any one enough: the add-in folder is a
symlink, it contains a `.git` entry (directory *or* worktree file), or its
real path differs from its absolute path. The About page shows the same
notice and disables **Install now**.

**Reloading — the Fusion part.** Fusion has no "reload add-in" API; what it
has is `adsk.core.Application.scripts` (`ScriptDefinitions`, which lists
add-ins too), `itemByPath(folder)` / `itemsByName(name)`, and
`script.stop()` / `script.run(False)` / `script.isRunning`. Two facts drive
the shape of `ui/updater.py`. **`stop()` + `run()` alone reloads nothing** —
every add-in shares one interpreter (§1), so `run()` re-imports the *cached*
modules; they must be deleted from `sys.modules` **between** stop and run.
`moxaserial_loader.purge()` covers `moxaserial.*` but not the
`__main__<encoded-path>` namespace Fusion uses for `MoxaSerial.py` itself, so
`purge_modules_under(root)` purges by `__file__` path instead, never touching
`adsk.*`, the stdlib or another add-in. **Self-reload cannot happen inline** —
`script.stop()` tears down the code making the call — so `restart_addin()`
starts a 1 s timer that fires the `P3D_MoxaSerial_Restart` custom event and
the handler does stop/purge/run on the main thread after the caller has
returned, the same hop as the palette push (§3). It returns whether the
restart was *scheduled*, not whether it worked; `False` degrades to a toast:
"Update installed. Restart Fusion to load it."

**Scheduling.** `UpdateService.schedule_startup_check()` is called at the end
of `ui/app.start()`. It does nothing unless `auto_check` is on *and*
`check_interval_hours` have passed since `last_check`; otherwise it arms a
6 s timer whose thread does network work only. `last_check`,
`last_seen_version` and `last_error` are written back through
`update_globals`, which *merges* the `update` section rather than replacing
it — a partial save from the About page must not reset keys it did not send.
`auto_install` is **off** by default: an available update notifies only.

**UNVERIFIED (needs a live Fusion):** that `app.scripts` lists a
`runOnStartup` add-in installed outside Fusion's own AddIns folder; that
`script.run(False)` after a folder swap re-reads `MoxaSerial.py` from the
*new* folder; and — on Windows — that renaming the add-in directory succeeds
while its `.py` files are imported (it should, but a locked file would hit
the "nothing was changed" path rather than corrupt anything).

## 9. Discovery, port probing and credentials

Three jobs that all start from "the operator knows there is an NPort
somewhere but not its IP, its port numbers, or its settings".

**UDP discovery** (`discovery.py`). Moxa's NPort Administrator finds devices
with a broadcast on **UDP 4800**. Every datagram starts with the same 8-byte
big-endian header — `struct.pack("!BBHI", opcode, status, total_length,
sequence)` — where a reply echoes the opcode with bit 7 set, `status` 4 means
"opcode not supported", and the length includes the header. A reply then
repeats a 12-byte **device id** at offset 8 (`APID` uint32 LE, model id
uint16 LE, MAC 6 bytes) which every follow-up request has to quote back as
its body. Opcode-specific data starts at offset 20.

| Opcode | Request | Reply payload (offset 20 onwards) |
|---|---|---|
| `0x01` search | header only, 8 bytes | 4-byte IPv4 address |
| `0x10` name | header + device id | 40-byte NUL-padded ASCII device name |
| `0x16` info | header + device id | firmware (uint32 LE at 20, top two bytes major/minor), serial number (uint16 LE at 28), serial-port count in the last byte |

`discover()` sends the search to `255.255.255.255` **and** to every local
interface's directed broadcast (`local_networks()` shells out to `ifconfig` /
`ip` / `ipconfig`, because the standard library cannot report a netmask),
then unicasts the two follow-ups to each responder. Devices are reported
through an `on_device` callback as they answer, so the UI fills in
progressively. The **model name** is not decoded from the model id — no
published tool has a complete id → name table, and the one id we can check
against hardware (`0x2452` = W2250A) does not follow the NPort 5000 series'
"the hex digits are the model number" rule. Instead `enrich_from_console()`
does an unauthenticated `GET /login.asp`, which prints model, name, serial
number, firmware and MAC above the login form.

`tcp_scan(subnet)` is the fallback, and it matters: Wi-Fi access points
routinely drop broadcast, and the W-series *is* a Wi-Fi device. It sweeps a
subnet for an open command port (966), TCP Server data port (4001) or web
console (80) with a thread pool, and refuses anything larger than 4096
addresses. Port 80 on its own is never enough — that is every printer on the
network — so a host is only reported when 966 or 4001 answers.

**Port probing.** Two rules, both verified on a W2250A, decide how a probe
has to be written: `PORT_INIT` must be the **first** frame on the command
socket (send anything else, even a read-only `LSTATUS`, and the NPort resets
the connection), and the NPort **will not answer** that `PORT_INIT` until the
matching data socket is connected too (command socket alone: accepted and
never answered). So `probe_port()` opens the command socket (966+n), then a
data socket, sends `PORT_INIT`, reads the `{dsr, cts, dcd}` reply and closes
both — about 100 ms on a healthy port, always inside its ~2 s budget. Which
data port accepted is the **operation-mode guess**: Real COM listens on 950+n
and refuses 4001+n, TCP Server the other way round. "Busy" is inferred rather
than reported: an NPort with `Max connection = 1` accepts the TCP connection
and then hangs up, so a socket that dies before or instead of the reply means
somebody else has the port. One retry after a short pause absorbs the
device's dislike of rapid reconnects.

**`PORT_INIT` carries the line settings, so a probe unavoidably *applies*
them** — it changes the port's running baud/parity, though never the stored
configuration. When credentials exist the probe reads the current settings
from the web console first and echoes them straight back, which makes it a
no-op; otherwise it uses the machine's own configured line settings.

Because of that, **a probe never touches a port this add-in is already
using.** `machines.probePorts` refuses outright while the sender or receiver
is running, and skips individual ports it still holds open: `transport.open`
and `transport.close` carry the endpoint (`host`, `portIndex`, `device`), the
bridge keeps a counted set of them, and a port in that set is reported busy
without a byte going out. Probing a live port would otherwise change the baud
rate under a running job — and on a `Max connection = 1` device the extra
connection alone can drop the live one.

**The web console** (`nport_console.py`). `GET /login.asp` is
unauthenticated and carries the identity block. Everything else needs the
GoAhead login, performed exactly as the browser does: `GET /login.asp` sets a
`ChallID` cookie and contains `document.all.FakeChallenge.value = "<64 hex>"`
plus a `setToken('<16 chars>')` call, whose value `valid.js` appends to every
form as a hidden `token_text` CSRF field; then `POST /goform/webLogin` with
`UserName_sel`, `Passwd` = `md5(username + password + FakeChallenge)`,
`UserName`, `FakeChallenge`, `Loginin.x`/`.y` and `token_text`. Success
replaces the cookie with a session id and redirects to `/index.asp`; failure
lands back on `/login.asp`, whose `if ("<reason>" == "exceed")` block names
the reason (`exceed` / `expired` / `password`). `MonitorSerialPortSet.asp`
then gives baud / data bits / stop bits / parity / RTS-CTS / XON-XOFF / FIFO
/ interface per port, and `opmode.asp?Port=NN` carries the numeric operation
mode (0 disabled, 256 Real COM, 257 RFC2217, 512 TCP Server, 513 TCP Client,
514 UDP, 768/769 pair connection, 1024 Ethernet modem, 1536 reverse
terminal). Sessions are short-lived and **the client never writes**: every
request is a `GET` except the login `POST`, which is deliberately the one
request that is never retried — a login that timed out while reading the
reply may have succeeded, and repeating it burns one of the device's few
login slots.

**Credentials** (`secrets.py`). Web-console credentials are the operator's,
not the add-in's, so they live outside `settings.json` entirely, keyed
`MoxaSerial:<machine_id>`:

* **macOS** — the login keychain via `/usr/bin/security`
  (`add-generic-password -U` / `find-generic-password -w` /
  `delete-generic-password`).
* **Windows** — Credential Manager via `ctypes` and `advapi32`
  (`CredWriteW` / `CredReadW` / `CredDeleteW`, `CRED_TYPE_GENERIC`).
* **anything else** — a 0600 JSON file under the app data dir, created with
  `os.open(..., 0o600)` so there is never a moment when it is world-readable.
  It is *weaker*, says so in a `_warning` key inside itself, and `describe()`
  reports `secure: false` so the UI can say so too.

Username and password go into one small JSON blob so the account name is
protected as well. `MOXASERIAL_SECRET_BACKEND=file` forces the file backend
(the test suite uses it). **The bridge never sends a password anywhere**:
`machines.setCredentials` takes one and returns only `has_credentials`,
`state()` carries a `{machineId: bool}` map, and `SecretStore` logs the
account name but never the secret. On the UI side the credential fields
deliberately sit **outside** `#machineForm`, because `readMachineForm()`
walks `form.elements` and none of this belongs in a machine record.

Everything above except the credential backends was verified against a real
NPort W2250A (firmware 2.2 Build 18082311, serial 9645, two ports in Real COM
mode): the captured datagrams are pinned byte-for-byte in
`tests/test_discovery.py`, and `tools/nport_sim.py`'s `DiscoveryResponder`
reproduces them. The credential backends are exercised through fakes only —
writing to a real keychain from a test suite is not acceptable.

## 10. Learned the hard way

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

## 11. Testing

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
`tests/test_serial_port.py` drives `SerialTransport` against a `pty` pair —
line parameters read back off the tty, flow-control flags, timeouts, purge,
`pending_tx`/`drain`, close-unblocks-read, and both engines end to end. A pty
has no modem lines, so the DTR/RTS readback case skips itself rather than
pretending. The Windows backend is driven by a ctypes double that records
every `kernel32` call and fills the structures back in.
`tests/test_update.py` mocks `urllib.request.urlopen` with a routing fake —
there is no network in the suite — and uses real zip files in `tmp_path`:
version-compare edge cases, every HTTP failure mode, asset selection,
checksum match and mismatch, a non-zip, a foreign zip, the successful swap,
both rollback paths, the symlink and `.git` refusals, backup pruning and
collision, and `UpdateService` end to end against a fake host.
`tests/test_discovery.py` replays byte-for-byte datagrams captured from a
real W2250A against `DiscoveryResponder`; `tests/test_nport_console.py`
drives the login handshake and both parsers off captured HTML;
`tests/test_secrets.py` runs the file backend for real and the keychain
backends through fakes — `conftest.py` sets `MOXASERIAL_SECRET_BACKEND=file`
so no test can ever reach the real keychain.

Lint `ruff check .`; publication gate `python3 tools/check_public_tree.py`.
