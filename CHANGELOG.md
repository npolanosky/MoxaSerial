# Changelog

All notable changes to this project are documented here. This project uses
[semantic versioning](https://semver.org/).

## 0.2.0 - 2026-09-16

### Added

* **Automatic NPort discovery.** A "Find NPort…" button on the Machines page
  broadcasts Moxa's UDP search on port 4800 and lists every device that
  answers with its model, IP, MAC, name, firmware, serial number and port
  count; devices appear as they answer rather than all at the end. Wi-Fi
  access points routinely drop broadcast — and the W-series is a Wi-Fi
  device — so a subnet can be swept over TCP instead, looking for an open
  command port (966), TCP Server data port (4001) or web console. Devices
  found either way are named by reading the console's login page.
  (`discovery.py`, actions `machines.discover` / `machines.discoverStop`,
  push `machines.discoverResult`.)
* **Automatic port detection.** Probing a device reports, per serial port:
  whether it is reachable, whether something else already holds it
  (`Max connection = 1`), its operation mode (Real COM vs TCP Server, guessed
  from which data port accepts), and the DSR/CTS/DCD modem lines. Each probe
  is under two seconds and closes both sockets immediately. A per-port
  **Use** button fills in host, port number, data port and command port on
  the machine form. (Action `machines.probePorts`, push
  `machines.probePortsResult`.)
* **Web-console credentials in the OS keychain.** Optional per-machine
  account and password for the NPort's web console, stored in the macOS login
  keychain, Windows Credential Manager, or — with a clearly marked warning —
  a 0600 file. Never written to `settings.json`, never logged, and never sent
  to the UI: the bridge only ever reports a boolean `has_credentials`.
  (`secrets.py`, actions `machines.setCredentials` /
  `machines.clearCredentials` / `machines.credentials`.)
* **Web-console client** (`nport_console.py`) that logs in the way the
  browser does and reads the Serial Port Settings table and each port's
  operation mode. With credentials stored, port probing shows the real
  baud/parity per port — and becomes non-destructive, because the probe
  echoes the device's current line settings back at it instead of imposing
  its own.
* `tools/nport_sim.py` gained `DiscoveryResponder` (a fake NPort answering
  the UDP search protocol) and a `strict_aspp` mode that enforces the two
  connection rules a real NPort enforces: the command socket is **reset**
  unless the first ASPP frame on it is `PORT_INIT`, and that `PORT_INIT` is
  **not answered** until the matching data socket is connected as well.
  Neither behaviour is in any Moxa document; both were recovered from, and
  verified against, a real NPort W2250A (firmware 2.2 Build 18082311), whose
  captured datagrams are pinned byte-for-byte in `tests/test_discovery.py`.

### Fixed

Found reviewing the three features together, before any of this shipped:

* **Port probing can no longer disturb a transfer.** `PORT_INIT` applies line
  settings, so probing is refused while a send or receive is running and
  skips any port this add-in still holds open.
* **A serial port that is not a tty no longer leaks its file descriptor.**
  `termios.error` is not an `OSError` subclass, so the handlers that named
  only `OSError` let it escape with the descriptor open — and because the
  transport marks itself open only after `_do_open` returns, `close()` was
  then a no-op and the descriptor leaked for the life of the Fusion process.
* **Two threads closing a serial port at once no longer double-close the
  self-pipe**, which could close a descriptor number the kernel had already
  handed to another part of Fusion.
* **Windows: reopening a serial port re-applies `SetCommTimeouts`.** The
  cached value was per backend, not per handle, so a reopen could leave
  `ReadFile` unbounded and `close()` landing on a pending read.
* **The updater fails closed.** A folder it cannot classify is treated as a
  development install rather than overwritten, `addin_dir()` no longer
  resolves away the symlink that marks such an install, and a symlink member
  inside a release archive is refused.
* **Backup pruning cannot destroy the new backup.** `rename()` does not
  update a directory's mtime, so "newest wins" could delete the only rollback
  copy; the fresh backup is now protected by path. Pruning also no longer
  reports a completed update as failed.
* **Auto-update from GitHub Releases.** The add-in checks
  `https://api.github.com/repos/<update.repo>/releases/latest` a few seconds
  after start-up (on a worker thread, at most once per
  `update.check_interval_hours`, default 24) and can download, verify and
  install a new release in place, then reload itself without a Fusion
  restart. The download is checked against the release's `SHA256SUMS` and
  must contain `MoxaSerial/MoxaSerial.manifest`; the live folder is renamed
  to `MoxaSerial.bak-<version>` rather than deleted, and any failure during
  the swap restores it. The two newest backups are kept. A development
  install — a git checkout or a symlink to one — is refused with
  "update with git pull". New About-page card: current and latest version,
  last-check time, Check now / Install now, and the `auto_check`,
  `auto_install` (off by default: notify only), `include_prereleases` and
  `repo` settings. Settings schema 3 adds the `update` section.
* **Direct serial ports.** New machine type `serial` sends and receives
  through a local RS-232 port — an onboard UART, a USB-serial adapter, or a
  virtual COM port — with no NPort in between. Same transport contract as the
  Moxa path, so every send and receive option behaves identically: baud/data
  bits/parity/stop bits, XON/XOFF, RTS/CTS and DTR/DSR flow control, DTR/RTS
  assertion, CTS/DSR/DCD/RI readback, output-queue drain and break. Stdlib
  only (`termios`/`fcntl`/`select` on macOS and Linux, `ctypes` + `kernel32`
  on Windows). The machine form gains a port dropdown with a **Refresh
  ports** button, backed by the new `serial.listPorts` action. Verified
  against a pseudo-terminal on macOS; the Windows backend is covered by tests
  but has not yet run on real hardware.

## 0.1.1 - 2026-09-16

### Fixed

- Windows: the palette failed to load (`ERR_INVALID_URL`) because the page was passed to Fusion as a backslash path; it is now a `file:///` URI.

## 0.1.0

First public release.

* Send a post-processed NC program to a CNC control over RS-232 through a
  Moxa NPort, using the device's native ASPP protocol — no virtual COM port
  driver on Windows or macOS.
* **Send Last Program** toolbar command that resolves the newest posted NC
  file from the active document and sends it in one click.
* Receive a program punched out from the control, with trigger, filter,
  filename and overwrite policies per machine.
* CIMCO Edit-compatible option set and escape conventions, so a machine
  already configured there can be copied across field for field.
* Per-machine serial settings, live progress and RS-232 indicators, toast
  notifications and a filterable log.
* Built-in Simulator machine, plus `tools/dev_server.py` (whole UI in a
  browser) and `tools/nport_sim.py` (fake NPort) for development with no
  Fusion and no hardware.
* Hardware verified against a Moxa NPort W2250A driving a Fanuc 0i-TB.
