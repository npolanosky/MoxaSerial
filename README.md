# MoxaSerial

DNC for Autodesk Fusion. Sends a post-processed NC program to a CNC control
over RS-232 through a **Moxa NPort** serial device server, speaking the
NPort's native TCP protocol directly — **no virtual COM port driver** on
Windows or macOS.

* **Send the last posted program** with one toolbar click, or browse for any
  file.
* **Receive** a program punched out by the control, straight to a file.
* **Direct serial ports too** — a machine can talk to a local RS-232 port
  (onboard UART, USB adapter, virtual COM) instead of an NPort.
* **CIMCO-style options**, name for name: baud / parity / stop bits, flow
  control, start and end triggers, omit and remove filters, line endings,
  block renumbering, handshake and idle timeouts, overwrite policy.
* Live progress, RS-232 indicator LEDs, a scrolling preview of the in-flight
  line, and a built-in **Simulator** machine for a dry run with no hardware.
* **Finds the NPort for you** — a network search fills in the address, and a
  port probe reports which serial port is free and what mode it is in.
* **Updates itself** from GitHub Releases — checked in the background,
  installed on request, no Fusion restart.
* Stdlib only — nothing to `pip install` into Fusion.

## Install

Pick one, then in Fusion: **Utilities → Scripts and Add-Ins → Add-Ins →
MoxaSerial → Run** (tick *Run on Startup*).

* **macOS** — open `MoxaSerial-<version>.pkg`.
* **Windows** — run `MoxaSerial-<version>-setup.exe`.
* **Any OS** — unzip `MoxaSerial-<version>.zip` into Fusion's AddIns folder,
  or run `python install.py` from the unzipped folder.

## First use

* Set the NPort's serial port to **Real COM** mode in its web interface.
* Set **Max connection = 1** and allow driver control.
* In the palette's **Machines** tab, add a machine with the NPort's address
  and port number, and the control's baud / parity / flow control.
* Hit **Test connection** — it should report CTS and DSR.
* Post a program, then **Send Last Program** from the Manufacture toolbar.

**Verified hardware:** Moxa NPort W2250A driving a Fanuc 0i-TB.

## Development

`tools/dev_server.py` runs the entire palette in a normal browser against a
simulated control — no Fusion, no hardware. `tools/nport_sim.py` is a fake
NPort for bench-testing the wire protocol. `python3 -m pytest` and
`ruff check .` must both stay green.

[ARCHITECTURE.md](ARCHITECTURE.md) — module map, threading rules, message
schema, protocol summary.
[CHANGELOG.md](CHANGELOG.md) — release notes.

## License

MIT — see [LICENSE](LICENSE).
