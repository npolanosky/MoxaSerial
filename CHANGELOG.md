# Changelog

All notable changes to this project are documented here. This project uses
[semantic versioning](https://semver.org/).

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
