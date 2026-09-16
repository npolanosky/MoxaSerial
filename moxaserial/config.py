"""Settings store: versioned JSON in a per-user application data directory.

Layout (``settings.json``)::

    {
      "schema_version": 1,
      "machines": [ <machine>, ... ],
      "default_machine_id": "...",
      "machine_selection": "default" | "last_used",
      "last_used_machine_id": "...",
      "log_level": "INFO",
      "theme": "dark" | "light",
      "confirm_before_send": false,
      "watch_folders": ["/path/to/nc"]
    }

Every machine is normalised through :func:`normalize_machine`, so reading
a hand-edited or older file never raises - unknown keys are dropped,
missing keys get defaults, and out-of-range values are clamped with a
validation warning the UI can show.

The store is deliberately dependency-free and synchronous. Writes are
atomic (temp file + ``os.replace``) so a crash mid-save cannot leave a
truncated settings file behind.
"""

from __future__ import annotations

import copy
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

from moxaserial import paths
from moxaserial.log import get_logger

log = get_logger("config")

SCHEMA_VERSION = 2

# --------------------------------------------------------------------------
# Enumerations - keep these in sync with resources/palette/app.js
# --------------------------------------------------------------------------
MACHINE_TYPES = ("moxa", "simulator")
PARITIES = ("none", "odd", "even", "mark", "space")
DATA_BITS = (5, 6, 7, 8)
STOP_BITS = ("1", "1.5", "2")
FLOW_CONTROLS = ("none", "xonxoff", "rtscts", "dtrdsr", "both")
#: Transmit CR/LF. ``CUSTOM`` takes its value from ``send.line_ending_custom``
#: and is CIMCO's "Send files with non-standard CR/LF" (``TRAN_DOUBLELF``).
LINE_ENDINGS = ("LF", "CR", "CRLF", "CUSTOM")
#: Receive CR/LF (CIMCO ``RECV_CRLF``): what arrives on the wire. ``AUTO``
#: auto-detects CR+LF / CR-only / LF-only, which is CIMCO's default.
RECEIVE_LINE_ENDINGS = ("AUTO", "LF", "CR", "CRLF", "CUSTOM")
#: Saved-file line ending (CIMCO ``SAVECRLF``): what lands on disk. Kept
#: separate from the wire ending exactly as CIMCO does.
SAVE_LINE_ENDINGS = ("KEEP", "LF", "CR", "CRLF")
#: CIMCO "Remove characters:" on the Receive page (``RECV_REMOVECHAR``).
#: ``custom`` is ours - CIMCO only offers the first three - and takes its
#: character list from ``receive.remove_chars_custom``.
DIALECTS = (
    "iso_mill", "iso_lathe", "fanuc", "haas", "heidenhain", "heidenhain_iso",
    "siemens", "mazak_iso", "mazatrol", "okuma",
)
RECEIVE_REMOVE_CHARS = ("none", "ascii0", "ascii0to31", "custom")
#: CIMCO trigger modes (``<mode>:<text>``, PCFG_RECEIVE_TRIGGERMODE_*).
#: Mode 1 "Save from trigger" and mode 2 "Save after trigger" are the two
#: that make sense for a start trigger.
START_TRIGGER_MODES = ("save_from_trigger", "save_after_trigger")
#: Mode 4 "Save including trigger" and mode 5 "Save until trigger".
END_TRIGGER_MODES = ("save_including_trigger", "save_until_trigger")
WAIT_MODES = ("immediate", "cts", "dsr", "xon")
OVERWRITE_POLICIES = ("allow", "ask", "deny", "rename")
MACHINE_SELECTION = ("default", "last_used")
THEMES = ("dark", "light")
COMMON_BAUDS = (
    300, 600, 1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400,
)

# Moxa NPort defaults. The real port numbers are confirmed in the protocol
# research phase; these are the documented factory defaults.
DEFAULT_DATA_PORT_BASE = 4001  # port 1 = 4001, port 2 = 4002, ...
DEFAULT_CMD_PORT_BASE = 966  # ASPP command port, port 1 = 966, port 2 = 967


# --------------------------------------------------------------------------
# Defaults
# --------------------------------------------------------------------------

def default_serial() -> dict[str, Any]:
    return {
        "baud": 9600,
        "data_bits": 8,
        "parity": "none",
        "stop_bits": "1",
        "flow_control": "xonxoff",
        "xon_char": 17,   # DC1
        "xoff_char": 19,  # DC3
        "assert_dtr": True,
        "assert_rts": True,
        # CIMCO "Check parity" (USEPARITY) + "Insert on parity error"
        # (PARITYINSERT, shipped default 035 = '#').
        "check_parity": False,
        "parity_insert": "\\35",
        # Let the NPort itself enforce XON/XOFF (recommended: it reacts
        # within a byte time; the host sees the same bytes with TCP latency).
        "device_flow_control": True,
        # UART TX FIFO depth the NPort uses. 1 = FIFO off (safest for old
        # controls with no receive buffer), 16 = 16550 default.
        "tx_fifo": 16,
    }


def cimco_serial() -> dict[str, Any]:
    """CIMCO Edit's shipped serial defaults - what a *new* machine gets.

    ``Sys/MACHINE1.MCH``: 9600 baud, 7 data bits, EVEN parity, 2 stop bits,
    SOFTWARE flow control, DTR and RTS high, XOn 017, XOff 019. The
    built-in Simulator deliberately keeps :func:`default_serial` (8-N-1),
    because it is not modelling any particular control.
    """
    s = default_serial()
    s.update(
        baud=9600,
        data_bits=7,
        parity="even",
        stop_bits="2",
        flow_control="xonxoff",  # CIMCO SOFTWARE
        assert_dtr=True,
        assert_rts=True,
    )
    return s


def default_send() -> dict[str, Any]:
    return {
        "line_ending": "CRLF",
        # Used when line_ending == "CUSTOM" - CIMCO's editable CR/LF combo
        # and its "Send files with non-standard CR/LF" checkbox in one field.
        "line_ending_custom": "\\13 \\10",
        "uppercase": True,
        "strip_comments": False,
        "strip_blank_lines": True,
        "strip_spaces": False,
        "leading_chars": "",
        "trailing_chars": "",
        "start_chars": "%\n",
        "end_chars": "%\n",
        "eob_chars": "",
        "line_numbers": False,
        "line_number_start": 1,
        "line_number_increment": 1,
        "line_number_prefix": "N",
        "line_number_digits": 0,
        "wait_for_ready": "immediate",
        "ready_timeout_s": 60,
        "char_delay_ms": 0,
        "line_delay_ms": 0,
        "chunk_size": 256,
        # Max bytes allowed to sit in the device (NPort) TX queue before we
        # hand over the next chunk. Keeps progress honest and Stop quick.
        # 0 = no throttling.
        "device_queue_limit": 512,
        # -- CIMCO "Transmit" page ---------------------------------------
        # TRAN_STARTTRIG / TRAN_ENDTRIG: bound the part of the file sent.
        "start_trigger": "",
        "end_trigger": "",
        # TRAN_OMMITLINES: any line containing one of these characters is
        # not transmitted. TRAN_REMCHAR: characters stripped from the data.
        "omit_lines_containing": "",
        "remove_chars": "",
        # TRAN_STRIP0 ("Remove ASCII 0's" - checked by default in CIMCO)
        # and TRAN_TABTOSPACE ("Replace tabs with spaces").
        "remove_nulls": True,
        "tabs_to_spaces": False,
        # TRAN_BREAKCOUNT: abort once this many characters have come back
        # from the control. 0 = ignore anything the control sends.
        "break_count": 0,
        # TRAN_TIMEOUT: seconds to wait on XOff / CTS-low before giving up.
        # 0 = wait forever, which is CIMCO's default.
        "handshake_timeout_s": 0,
        # CIMCO "Send/Recv" page, send half.
        "default_folder": "",
        "default_extension": "",
        "additional_extensions": "",
    }


def default_receive() -> dict[str, Any]:
    return {
        # Re-insert the spaces a control strips when punching (CIMCO "Insert
        # spaces"), according to the control's dialect.
        "insert_spaces": False,
        "dialect": "iso_mill",
        # Drop the space some controls punch before every EOB.
        "trim_trailing_spaces": True,
        "folder": str(paths.default_receive_dir()),
        "filename_pattern": "{machine}_{date}_{time}.nc",
        "overwrite": "ask",
        "start_trigger": "",
        "end_trigger": "",
        # CIMCO RECV_TIMEOUT, shipped default 5 s. 0 = no idle timeout, in
        # which case an end trigger (or the operator) must end the capture.
        "idle_timeout_s": 5,
        "overall_timeout_s": 1800,
        # Deprecated in favour of ``remove_chars``; still read so an older
        # settings file (or a hand edit) keeps working. See migration 1->2.
        "strip_control_chars": True,
        "append_extension": ".nc",
        # -- CIMCO "Receive" page ----------------------------------------
        # RECV_CRLF (wire) vs SAVECRLF (on disk) - deliberately separate.
        "line_ending": "AUTO",
        "line_ending_custom": "",
        "save_line_ending": "CRLF",
        # PCFG_RECEIVE_TRIGGERMODE_*: where the trigger itself lands.
        "start_trigger_mode": "save_from_trigger",
        "end_trigger_mode": "save_including_trigger",
        # RECV_OMMITLINES (character set) / RECV_OMMITSTRING (substring).
        "omit_lines_containing": "",
        "omit_lines_with_string": "",
        # RECV_REMOVECHAR: none | ascii0 | ascii0to31 | custom.
        "remove_chars": "ascii0to31",
        "remove_chars_custom": "",
        # RECV_NOSAVEEMPTY.
        "omit_empty_lines": False,
        # RECV_SENDXON / RECV_FEEDSTART.
        "send_xon": False,
        "start_chars": "",
        "additional_extensions": "",
    }


def default_machine(name: str = "New Machine", kind: str = "moxa") -> dict[str, Any]:
    kind = kind if kind in MACHINE_TYPES else "moxa"
    return {
        "id": uuid.uuid4().hex[:12],
        "name": name,
        "type": kind,
        "host": "192.168.1.100",
        "port_index": 1,
        "data_port": DEFAULT_DATA_PORT_BASE,
        "cmd_port": DEFAULT_CMD_PORT_BASE,
        "connect_timeout_s": 8,
        # CIMCO [TCPIPDIRECT] AUTORECONNECT - "Attempt auto re-connect".
        "auto_reconnect": True,
        "reconnect_attempts": 3,
        "reconnect_delay_s": 2,
        "notes": "",
        # A real control gets CIMCO's shipped line settings (9600 7E2,
        # software flow control); the Simulator keeps our own 8-N-1.
        "serial": cimco_serial() if kind == "moxa" else default_serial(),
        "send": default_send(),
        "receive": default_receive(),
    }


def simulator_machine() -> dict[str, Any]:
    """The built-in Simulator machine - a FakeTransport target.

    Always present so the add-in is useful (and demonstrable) with no
    hardware attached at all.
    """
    m = default_machine("Simulator", "simulator")
    m["id"] = "simulator"
    m["host"] = "localhost"
    m["notes"] = (
        "Built-in fake CNC. No hardware required - used for dry runs and testing. "
        "Runs at the configured baud rate so progress looks real."
    )
    m["simulator"] = {"realtime": True}
    # Nothing to reconnect to: the simulator is in-process.
    m["auto_reconnect"] = False
    m["send"]["wait_for_ready"] = "immediate"
    m["receive"]["overwrite"] = "rename"
    return m


def default_settings() -> dict[str, Any]:
    sim = simulator_machine()
    return {
        "schema_version": SCHEMA_VERSION,
        "machines": [sim],
        "default_machine_id": sim["id"],
        "machine_selection": "default",
        "last_used_machine_id": sim["id"],
        "log_level": "INFO",
        "theme": "dark",
        "confirm_before_send": False,
        "watch_folders": [],
    }


# --------------------------------------------------------------------------
# Validation / normalisation
# --------------------------------------------------------------------------

class ValidationError(ValueError):
    """Raised by :func:`validate_machine` when a machine cannot be saved."""


def _as_int(value: Any, fallback: int, lo: int | None = None, hi: int | None = None) -> int:
    try:
        out = int(value)
    except (TypeError, ValueError):
        return fallback
    if lo is not None and out < lo:
        return lo
    if hi is not None and out > hi:
        return hi
    return out


def _as_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(value, (int, float)):
        return bool(value)
    return fallback


def _as_str(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    return str(value)


def _choice(value: Any, options: tuple, fallback: Any) -> Any:
    if isinstance(value, str):
        low = value.strip().lower()
        for opt in options:
            if str(opt).lower() == low:
                return opt
    elif value in options:
        return value
    return fallback


def _normalize_section(raw: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    return {**defaults, **{k: v for k, v in src.items() if k in defaults}}


def normalize_machine(raw: dict[str, Any]) -> dict[str, Any]:
    """Coerce an arbitrary dict into a complete, valid machine record."""
    src = raw if isinstance(raw, dict) else {}
    base = default_machine()

    m: dict[str, Any] = {
        "id": _as_str(src.get("id") or base["id"]),
        "name": _as_str(src.get("name") or "Unnamed").strip() or "Unnamed",
        "type": _choice(src.get("type"), MACHINE_TYPES, "moxa"),
        "host": _as_str(src.get("host", base["host"])).strip(),
        # CIMCO's [TCPIPDIRECT] PORTNO is a 1..32 device port index.
        "port_index": _as_int(src.get("port_index"), 1, 1, 32),
        "connect_timeout_s": _as_int(src.get("connect_timeout_s"), 8, 1, 120),
        "auto_reconnect": _as_bool(src.get("auto_reconnect"), True),
        "reconnect_attempts": _as_int(src.get("reconnect_attempts"), 3, 0, 10),
        "reconnect_delay_s": _as_int(src.get("reconnect_delay_s"), 2, 0, 60),
        "notes": _as_str(src.get("notes", "")),
    }
    m["data_port"] = _as_int(
        src.get("data_port"), DEFAULT_DATA_PORT_BASE + m["port_index"] - 1, 1, 65535
    )
    m["cmd_port"] = _as_int(
        src.get("cmd_port"), DEFAULT_CMD_PORT_BASE + m["port_index"] - 1, 1, 65535
    )

    # -- serial ----------------------------------------------------------
    s = _normalize_section(src.get("serial"), default_serial())
    s["baud"] = _as_int(s["baud"], 9600, 50, 3_000_000)
    s["data_bits"] = _choice(_as_int(s["data_bits"], 8), DATA_BITS, 8)
    s["parity"] = _choice(s["parity"], PARITIES, "none")
    s["stop_bits"] = _choice(str(s["stop_bits"]), STOP_BITS, "1")
    s["flow_control"] = _choice(s["flow_control"], FLOW_CONTROLS, "none")
    s["xon_char"] = _as_int(s["xon_char"], 17, 0, 255)
    s["xoff_char"] = _as_int(s["xoff_char"], 19, 0, 255)
    s["assert_dtr"] = _as_bool(s["assert_dtr"], True)
    s["assert_rts"] = _as_bool(s["assert_rts"], True)
    s["check_parity"] = _as_bool(s["check_parity"], False)
    s["parity_insert"] = _as_str(s["parity_insert"], "")
    s["device_flow_control"] = _as_bool(s["device_flow_control"], True)
    s["tx_fifo"] = _as_int(s["tx_fifo"], 16, 1, 128)
    m["serial"] = s
    sim_src = src.get("simulator") if isinstance(src.get("simulator"), dict) else {}
    # The built-in Simulator predates this key in older settings files;
    # give it realtime timing so a demo send does not finish instantly.
    default_rt = m.get("id") == "simulator"
    m["simulator"] = {"realtime": _as_bool(sim_src.get("realtime", default_rt), default_rt)}

    # -- send ------------------------------------------------------------
    d = _normalize_section(src.get("send"), default_send())
    d["line_ending"] = _choice(d["line_ending"], LINE_ENDINGS, "CRLF")
    for key in ("uppercase", "strip_comments", "strip_blank_lines", "strip_spaces",
                "line_numbers", "remove_nulls", "tabs_to_spaces"):
        d[key] = _as_bool(d[key], bool(default_send()[key]))
    for key in ("leading_chars", "trailing_chars", "start_chars", "end_chars", "eob_chars",
                "line_number_prefix", "line_ending_custom", "start_trigger", "end_trigger",
                "omit_lines_containing", "remove_chars", "default_folder",
                "default_extension", "additional_extensions"):
        d[key] = _as_str(d[key], "")
    d["line_number_start"] = _as_int(d["line_number_start"], 1, 0, 999_999)
    d["line_number_increment"] = _as_int(d["line_number_increment"], 1, 1, 1000)
    d["line_number_digits"] = _as_int(d["line_number_digits"], 0, 0, 8)
    d["wait_for_ready"] = _choice(d["wait_for_ready"], WAIT_MODES, "immediate")
    d["ready_timeout_s"] = _as_int(d["ready_timeout_s"], 60, 1, 3600)
    d["char_delay_ms"] = _as_int(d["char_delay_ms"], 0, 0, 1000)
    d["line_delay_ms"] = _as_int(d["line_delay_ms"], 0, 0, 10_000)
    d["chunk_size"] = _as_int(d["chunk_size"], 256, 1, 8192)
    d["device_queue_limit"] = _as_int(d.get("device_queue_limit", 512), 512, 0, 65535)
    d["break_count"] = _as_int(d["break_count"], 0, 0, 1_000_000)
    d["handshake_timeout_s"] = _as_int(d["handshake_timeout_s"], 0, 0, 3600)
    m["send"] = d

    # -- receive ---------------------------------------------------------
    raw_recv = src.get("receive") if isinstance(src.get("receive"), dict) else {}
    r = _normalize_section(raw_recv, default_receive())
    # Legacy key: ``strip_control_chars`` predates CIMCO's three-way
    # "Remove characters:" combo. A file that only has the old key keeps
    # behaving exactly as it did.
    if "remove_chars" not in raw_recv and "strip_control_chars" in raw_recv:
        r["remove_chars"] = (
            "ascii0to31" if _as_bool(raw_recv["strip_control_chars"], True) else "none"
        )
    r["folder"] = _as_str(r["folder"], str(paths.default_receive_dir()))
    r["filename_pattern"] = _as_str(r["filename_pattern"], "{machine}_{date}_{time}.nc").strip() \
        or "{machine}_{date}_{time}.nc"
    r["overwrite"] = _choice(r["overwrite"], OVERWRITE_POLICIES, "ask")
    for key in ("start_trigger", "end_trigger", "line_ending_custom",
                "omit_lines_containing", "omit_lines_with_string",
                "remove_chars_custom", "start_chars", "additional_extensions"):
        r[key] = _as_str(r[key], "")
    # 0 = no idle timeout, matching CIMCO's RECV_TIMEOUT semantics.
    r["idle_timeout_s"] = _as_int(r["idle_timeout_s"], 5, 0, 3600)
    r["overall_timeout_s"] = _as_int(r["overall_timeout_s"], 1800, 5, 86_400)
    r["strip_control_chars"] = _as_bool(r["strip_control_chars"], True)
    r["append_extension"] = _as_str(r["append_extension"], ".nc")
    r["insert_spaces"] = _as_bool(r.get("insert_spaces", False), False)
    r["trim_trailing_spaces"] = _as_bool(r.get("trim_trailing_spaces", True), True)
    r["dialect"] = _choice(r.get("dialect", "iso_mill"), DIALECTS, "iso_mill")
    r["line_ending"] = _choice(r["line_ending"], RECEIVE_LINE_ENDINGS, "AUTO")
    r["save_line_ending"] = _choice(r["save_line_ending"], SAVE_LINE_ENDINGS, "CRLF")
    r["start_trigger_mode"] = _choice(
        r["start_trigger_mode"], START_TRIGGER_MODES, "save_from_trigger"
    )
    r["end_trigger_mode"] = _choice(
        r["end_trigger_mode"], END_TRIGGER_MODES, "save_including_trigger"
    )
    r["remove_chars"] = _choice(r["remove_chars"], RECEIVE_REMOVE_CHARS, "ascii0to31")
    r["omit_empty_lines"] = _as_bool(r["omit_empty_lines"], False)
    r["send_xon"] = _as_bool(r["send_xon"], False)
    m["receive"] = r

    return m


#: Fragments identifying problems that are warnings, not save blockers.
#: ``upsert_machine`` lets these through; the UI still shows them.
ADVISORY_MARKERS = ("no placeholder", "stopped by hand")


def _is_advisory(problem: str) -> bool:
    return any(marker in problem for marker in ADVISORY_MARKERS)


def validate_machine(machine: dict[str, Any]) -> list[str]:
    """Return a list of human-readable problems. Empty list == valid."""
    problems: list[str] = []
    name = _as_str(machine.get("name", "")).strip()
    if not name:
        problems.append("Machine name is required.")
    kind = machine.get("type")
    if kind not in MACHINE_TYPES:
        problems.append(f"Unknown machine type '{kind}'.")
    if kind == "moxa":
        host = _as_str(machine.get("host", "")).strip()
        if not host:
            problems.append("Host / IP address is required for a Moxa machine.")
        port = machine.get("data_port")
        if not isinstance(port, int) or not (1 <= port <= 65535):
            problems.append("Data port must be between 1 and 65535.")
    serial = machine.get("serial", {})
    send = machine.get("send", {})
    if send.get("wait_for_ready") == "xon" and serial.get("flow_control") not in (
        "xonxoff",
        "both",
    ):
        problems.append(
            "'Wait for XON' requires XON/XOFF software flow control to be enabled."
        )
    if send.get("line_ending") == "CUSTOM" and not _as_str(
        send.get("line_ending_custom", "")
    ).strip():
        problems.append(
            "A custom transmit CR/LF was selected but no characters were given "
            "(for example '\\13 \\10')."
        )
    if _as_bool(serial.get("check_parity"), False) and serial.get("parity") in (
        None,
        "none",
    ):
        problems.append(
            "'Check parity' needs a parity setting other than None."
        )
    recv = machine.get("receive", {})
    if not _as_str(recv.get("folder", "")).strip():
        problems.append("A receive folder is required.")
    if (
        _as_int(recv.get("idle_timeout_s"), 5, 0, 3600) == 0
        and not _as_str(recv.get("end_trigger", "")).strip()
    ):
        # CIMCO says the same thing on the Receive page.
        problems.append(
            "No end trigger and no receive timeout - the receive will have to be "
            "stopped by hand."
        )
    if recv.get("line_ending") == "CUSTOM" and not _as_str(
        recv.get("line_ending_custom", "")
    ).strip():
        problems.append(
            "A custom receive CR/LF was selected but no characters were given."
        )
    if recv.get("remove_chars") == "custom" and not _as_str(
        recv.get("remove_chars_custom", "")
    ).strip():
        problems.append(
            "'Remove characters: custom' was selected but no characters were listed."
        )
    if "{" not in _as_str(recv.get("filename_pattern", "")):
        # Not fatal, but every received file would collide.
        problems.append(
            "Receive filename pattern has no placeholder - every receive will reuse one name."
        )
    return problems


def normalize_settings(raw: dict[str, Any]) -> dict[str, Any]:
    src = raw if isinstance(raw, dict) else {}
    out = default_settings()

    machines_raw = src.get("machines")
    machines: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    if isinstance(machines_raw, list):
        for item in machines_raw:
            if not isinstance(item, dict):
                continue
            m = normalize_machine(item)
            while m["id"] in seen_ids:
                m["id"] = uuid.uuid4().hex[:12]
            seen_ids.add(m["id"])
            machines.append(m)
    if not any(m["id"] == "simulator" for m in machines):
        machines.insert(0, simulator_machine())
    out["machines"] = machines

    ids = {m["id"] for m in machines}
    dflt = _as_str(src.get("default_machine_id", ""))
    out["default_machine_id"] = dflt if dflt in ids else machines[0]["id"]
    last = _as_str(src.get("last_used_machine_id", ""))
    out["last_used_machine_id"] = last if last in ids else out["default_machine_id"]
    out["machine_selection"] = _choice(
        src.get("machine_selection"), MACHINE_SELECTION, "default"
    )
    out["log_level"] = _choice(
        src.get("log_level"), ("DEBUG", "INFO", "WARNING", "ERROR"), "INFO"
    )
    out["theme"] = _choice(src.get("theme"), THEMES, "dark")
    out["confirm_before_send"] = _as_bool(src.get("confirm_before_send"), False)
    folders = src.get("watch_folders")
    out["watch_folders"] = [str(f) for f in folders if str(f).strip()] if isinstance(
        folders, list
    ) else []
    out["schema_version"] = SCHEMA_VERSION
    return out


# --------------------------------------------------------------------------
# Migration
# --------------------------------------------------------------------------

def migrate(raw: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Upgrade *raw* to :data:`SCHEMA_VERSION`. Returns (data, notes).

    Each step is a small function keyed by the version it upgrades *from*.
    New schema versions append a step here; nothing else changes.
    """
    notes: list[str] = []
    data = copy.deepcopy(raw) if isinstance(raw, dict) else {}
    version = _as_int(data.get("schema_version"), 0, 0, 10_000)

    if version > SCHEMA_VERSION:
        notes.append(
            f"Settings were written by a newer version (schema {version} > {SCHEMA_VERSION}); "
            "unknown fields are ignored."
        )
        return data, notes

    while version < SCHEMA_VERSION:
        step = _MIGRATIONS.get(version)
        if step is None:
            notes.append(f"No migration from schema {version}; falling back to defaults.")
            data = default_settings()
            break
        data = step(data)
        notes.append(f"Migrated settings schema {version} -> {version + 1}.")
        version += 1
        data["schema_version"] = version

    return data, notes


def _migrate_0_to_1(data: dict[str, Any]) -> dict[str, Any]:
    """Version 0 == 'no schema_version key'; treat it as a pre-release file."""
    out = dict(data)
    out.setdefault("machines", [])
    out.setdefault("machine_selection", "default")
    return out


def _migrate_1_to_2(data: dict[str, Any]) -> dict[str, Any]:
    """Schema 2 adds the CIMCO parity fields.

    Nothing is renamed. Two values are *carried forward* so an existing
    machine keeps behaving identically:

    * ``receive.strip_control_chars`` seeds the new three-way
      ``receive.remove_chars`` combo (True -> ``ascii0to31``, CIMCO's own
      default; False -> ``none``). The old key is left in place.
    * ``receive.save_line_ending`` is set to ``KEEP`` for machines that
      predate the wire/saved line-ending split, because that is what the
      old receiver did - new machines get CIMCO's ``CRLF``.
    """
    out = dict(data)
    machines = out.get("machines")
    if not isinstance(machines, list):
        return out
    upgraded = []
    for m in machines:
        if not isinstance(m, dict):
            continue
        m = dict(m)
        recv = dict(m.get("receive") or {})
        if "remove_chars" not in recv:
            recv["remove_chars"] = (
                "ascii0to31" if _as_bool(recv.get("strip_control_chars"), True) else "none"
            )
        recv.setdefault("save_line_ending", "KEEP")
        m["receive"] = recv
        upgraded.append(m)
    out["machines"] = upgraded
    return out


_MIGRATIONS = {0: _migrate_0_to_1, 1: _migrate_1_to_2}


# --------------------------------------------------------------------------
# Store
# --------------------------------------------------------------------------

class ConfigStore:
    """Load / mutate / save the settings document. Thread-safe."""

    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path) if path else paths.settings_path()
        self._lock = threading.RLock()
        self._data: dict[str, Any] = default_settings()
        self.migration_notes: list[str] = []
        self.load()

    # -- persistence -----------------------------------------------------
    def load(self) -> dict[str, Any]:
        with self._lock:
            raw: dict[str, Any] = {}
            if self.path.exists():
                try:
                    raw = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    log.error("Settings file unreadable (%s); using defaults: %s", self.path, exc)
                    self._backup_corrupt()
                    raw = {}
            migrated, notes = migrate(raw) if raw else (raw, [])
            self.migration_notes = notes
            for n in notes:
                log.info("%s", n)
            self._data = normalize_settings(migrated)
            return self._data

    def save(self) -> None:
        with self._lock:
            data = copy.deepcopy(self._data)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, indent=2, sort_keys=False), encoding="utf-8")
            os.replace(tmp, self.path)
            log.debug("Settings saved to %s", self.path)
        except OSError as exc:
            log.error("Could not write settings to %s: %s", self.path, exc)
            raise

    def _backup_corrupt(self) -> None:
        try:
            bad = self.path.with_suffix(".json.corrupt")
            os.replace(self.path, bad)
            log.warning("Corrupt settings moved to %s", bad)
        except OSError:
            pass

    # -- whole-document access -------------------------------------------
    @property
    def data(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def replace(self, data: dict[str, Any], save: bool = True) -> dict[str, Any]:
        with self._lock:
            self._data = normalize_settings(data)
        if save:
            self.save()
        return self.data

    def get(self, key: str, fallback: Any = None) -> Any:
        with self._lock:
            return copy.deepcopy(self._data.get(key, fallback))

    def set(self, key: str, value: Any, save: bool = True) -> None:
        with self._lock:
            merged = dict(self._data)
            merged[key] = value
            self._data = normalize_settings(merged)
        if save:
            self.save()

    def update_globals(self, values: dict[str, Any], save: bool = True) -> dict[str, Any]:
        allowed = (
            "default_machine_id",
            "machine_selection",
            "last_used_machine_id",
            "log_level",
            "theme",
            "confirm_before_send",
            "watch_folders",
        )
        with self._lock:
            merged = dict(self._data)
            for k, v in values.items():
                if k in allowed:
                    merged[k] = v
            self._data = normalize_settings(merged)
        if save:
            self.save()
        return self.data

    # -- machines --------------------------------------------------------
    def machines(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._data["machines"])

    def machine(self, machine_id: str) -> dict[str, Any] | None:
        with self._lock:
            for m in self._data["machines"]:
                if m["id"] == machine_id:
                    return copy.deepcopy(m)
        return None

    def active_machine(self) -> dict[str, Any]:
        """The machine the Send page should preselect."""
        with self._lock:
            mode = self._data["machine_selection"]
            wanted = (
                self._data["last_used_machine_id"]
                if mode == "last_used"
                else self._data["default_machine_id"]
            )
            for m in self._data["machines"]:
                if m["id"] == wanted:
                    return copy.deepcopy(m)
            return copy.deepcopy(self._data["machines"][0])

    def upsert_machine(self, machine: dict[str, Any], save: bool = True) -> dict[str, Any]:
        """Insert or update. Raises :class:`ValidationError` on bad input.

        The raw name is checked *before* normalisation: ``normalize_machine``
        is deliberately lenient (it renames a nameless machine to "Unnamed"
        so a hand-edited settings file still loads), but a save coming from
        the UI must not silently invent a name.
        """
        if not _as_str((machine or {}).get("name", "")).strip():
            raise ValidationError("Machine name is required.")
        norm = normalize_machine(machine)
        problems = validate_machine(norm)
        fatal = [p for p in problems if not _is_advisory(p)]
        if fatal:
            raise ValidationError("; ".join(fatal))
        with self._lock:
            machines = self._data["machines"]
            for i, m in enumerate(machines):
                if m["id"] == norm["id"]:
                    machines[i] = norm
                    break
            else:
                machines.append(norm)
            self._data = normalize_settings(self._data)
        if save:
            self.save()
        return norm

    def delete_machine(self, machine_id: str, save: bool = True) -> bool:
        if machine_id == "simulator":
            raise ValidationError("The built-in Simulator machine cannot be deleted.")
        with self._lock:
            before = len(self._data["machines"])
            self._data["machines"] = [
                m for m in self._data["machines"] if m["id"] != machine_id
            ]
            removed = len(self._data["machines"]) != before
            self._data = normalize_settings(self._data)
        if removed and save:
            self.save()
        return removed

    def duplicate_machine(self, machine_id: str, save: bool = True) -> dict[str, Any] | None:
        src = self.machine(machine_id)
        if src is None:
            return None
        clone = copy.deepcopy(src)
        clone["id"] = uuid.uuid4().hex[:12]
        clone["type"] = "moxa" if src["type"] == "simulator" else src["type"]
        names = {m["name"] for m in self.machines()}
        base = f"{src['name']} copy"
        name = base
        n = 2
        while name in names:
            name = f"{base} {n}"
            n += 1
        clone["name"] = name
        return self.upsert_machine(clone, save=save)

    def set_default_machine(self, machine_id: str, save: bool = True) -> None:
        self.update_globals({"default_machine_id": machine_id}, save=save)

    def note_machine_used(self, machine_id: str, save: bool = True) -> None:
        self.update_globals({"last_used_machine_id": machine_id}, save=save)
