"""Receive engine - captures a program punched out by the control to a file.

Mirrors :mod:`moxaserial.dnc.sender`: background daemon thread, event-bus
progress, no ``adsk`` import, driveable against
:class:`moxaserial.transport.fake.FakeTransport`.

States::

    IDLE -> CONNECTING -> WAITING -> RECEIVING -> DONE
                              |          |
                              +--> STOPPED / ERROR

Termination rules, in priority order (CIMCO's):

1. the **end trigger** string appears in the stream (if configured),
2. **idle timeout** - no bytes for ``idle_timeout_s`` after data started;
   0 means no idle timeout, exactly as CIMCO's ``RECV_TIMEOUT`` does,
3. **overall timeout** - a hard cap on the whole capture (ours, not CIMCO's),
4. the operator presses Stop.

Before the capture starts, ``send_xon`` puts an XOn on the wire ("the DNC
will send a XOn character when it is ready to receive data") and
``start_chars`` sends CIMCO's "Send at start of reception" string.

What lands on disk is then filtered exactly as CIMCO's Receive page
describes, in this order:

1. parity check over the raw stream, inserting the "Insert on parity
   error" character and bumping the error counter (``check_parity``),
2. split into lines on the **wire** CR/LF - ``AUTO`` detects CR+LF,
   CR-only or LF-only,
3. per line: remove characters (none / ASCII 0 / all below ASCII 32 /
   a custom list),
4. drop lines containing an omit character, lines containing the omit
   string, and - optionally - empty lines,
5. re-join with the **saved** line ending, which is a separate setting
   (CIMCO ``SAVECRLF``).

The overwrite policy ``ask`` round-trips to the UI: the engine publishes
``receive.overwrite_request`` with a token and blocks until
:meth:`Receiver.resolve_overwrite` is called with that token.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from moxaserial.dnc.preprocess import char_set, program_name, unescape
from moxaserial.events import EventBus
from moxaserial.log import get_logger
from moxaserial.transport.base import Transport, TransportError

log = get_logger("receiver")

#: How long the UI has to answer an "overwrite?" prompt before we give up.
OVERWRITE_PROMPT_TIMEOUT = 300.0

PROGRESS_INTERVAL = 0.1
PREVIEW_TAIL = 14

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

#: Explicit line endings for the receive CR/LF and saved CR/LF settings.
LINE_ENDINGS = {"LF": "\n", "CR": "\r", "CRLF": "\r\n"}

#: The captured stream is decoded latin-1 (byte-preserving, never raises)
#: so filtering decisions are made per byte and nothing is lost before the
#: "Remove characters" setting has had its say.
WIRE_ENCODING = "latin-1"


class ReceiveState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    WAITING = "WAITING"
    RECEIVING = "RECEIVING"
    STOPPED = "STOPPED"
    DONE = "DONE"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (ReceiveState.STOPPED, ReceiveState.DONE, ReceiveState.ERROR)


class OverwriteDenied(Exception):
    """The target file must not be replaced. *fallback* is where the
    capture is saved instead so the data is never thrown away."""

    def __init__(self, message: str, fallback: Path | None = None) -> None:
        super().__init__(message)
        self.fallback = fallback


@dataclass
class ReceiveProgress:
    state: str = ReceiveState.IDLE.value
    machine_id: str = ""
    machine_name: str = ""
    bytes_received: int = 0
    lines_received: int = 0
    elapsed_s: float = 0.0
    idle_s: float = 0.0
    #: CIMCO's "CPS:" on the receive status dialog.
    cps: float = 0.0
    #: CIMCO's "Errors:" - parity errors seen during this capture.
    errors: int = 0
    rx: bool = False
    target_path: str = ""
    target_name: str = ""
    tail: list[str] = field(default_factory=list)
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------
# Filename policy - pure functions so they are unit-testable
# --------------------------------------------------------------------------

def sanitize(name: str) -> str:
    cleaned = _UNSAFE.sub("_", name).strip().strip(".")
    return cleaned or "received"


def render_filename(
    pattern: str,
    machine_name: str,
    program: str = "",
    when: datetime | None = None,
    counter: int = 1,
) -> str:
    """Expand ``{machine} {program} {date} {time} {datetime} {n}``."""
    ts = when or datetime.now()
    values = {
        "machine": sanitize(machine_name or "machine"),
        "program": sanitize(program) if program else "program",
        "date": ts.strftime("%Y-%m-%d"),
        "time": ts.strftime("%H%M%S"),
        "datetime": ts.strftime("%Y-%m-%d_%H%M%S"),
        "n": str(counter),
    }
    out = pattern
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return sanitize(out)


def parity_of(value: int, bits: int = 7) -> int:
    """Number of 1 bits in the low *bits* of *value*, modulo 2."""
    return bin(value & ((1 << bits) - 1)).count("1") & 1


def check_parity(
    data: bytes, parity: str, data_bits: int, marker: str = ""
) -> tuple[bytes, int]:
    """CIMCO "Check parity" + "Insert on parity error", over a raw stream.

    Only meaningful when the control sends **7 data bits plus a parity
    bit** and the device hands the parity bit through as bit 7 - which is
    the configuration NC controls actually use (7-E-2). With 8 data bits
    or no parity there is no parity bit inside the data, so the stream is
    returned untouched and no error can be detected here.

    Returns ``(clean_bytes, error_count)``. *marker* is inserted directly
    in front of each offending character, "in the received file at the
    receiving point"; leave it empty to insert nothing.
    """
    mode = str(parity or "none").lower()
    if mode == "none" or int(data_bits or 8) != 7:
        return bytes(data), 0

    marker_bytes = marker.encode(WIRE_ENCODING, errors="replace")
    out = bytearray()
    errors = 0
    for byte in data:
        high = (byte >> 7) & 1
        if mode == "even":
            expected = parity_of(byte)
        elif mode == "odd":
            expected = parity_of(byte) ^ 1
        elif mode == "mark":
            expected = 1
        elif mode == "space":
            expected = 0
        else:  # pragma: no cover - guarded by the enum
            expected = high
        if high != expected:
            errors += 1
            out.extend(marker_bytes)
        out.append(byte & 0x7F)
    return bytes(out), errors


def detect_line_ending(text: str) -> str:
    """CIMCO's receive CR/LF ``AUTO``: CR+LF, then CR-only, then LF-only."""
    if "\r\n" in text:
        return "\r\n"
    if "\r" in text:
        return "\r"
    return "\n"


def wire_lines(text: str, cfg: dict[str, Any]) -> tuple[list[str], str]:
    """Split *text* on the configured receive CR/LF.

    Returns the lines plus the ending that was in play, which is what
    ``save_line_ending = KEEP`` writes back out.
    """
    mode = str(cfg.get("line_ending", "AUTO")).upper()
    if mode == "CUSTOM":
        sep = unescape(str(cfg.get("line_ending_custom", ""))) or "\n"
    else:
        sep = LINE_ENDINGS.get(mode, "")
    if not sep:  # AUTO, or an unknown value
        detected = detect_line_ending(text)
        # Controls punch all sorts of EOBs: a Fanuc's default is LF CR CR
        # (parameter 0100 NCR=0), others send CR CR LF or LF CR. Any run of
        # CR/LF is one block boundary; pick an explicit mode to keep blank
        # lines that really are in the program.
        lines = re.split(r"[\r\n]+", text)
        if lines and lines[-1] == "":
            lines.pop()
        if lines and lines[0] == "" and text[:1] in "\r\n":
            lines.pop(0)
        return lines, detected
    lines = text.split(sep)
    if lines and lines[-1] == "":
        lines.pop()
    return lines, sep


def strip_received_chars(line: str, cfg: dict[str, Any]) -> str:
    """CIMCO "Remove characters:" on the Receive page (``RECV_REMOVECHAR``)."""
    mode = str(cfg.get("remove_chars", "ascii0to31")).lower()
    if mode == "none":
        return line
    if mode == "ascii0":
        return line.replace("\0", "")
    if mode == "custom":
        drop = char_set(str(cfg.get("remove_chars_custom", "")))
        return "".join(ch for ch in line if ch not in drop) if drop else line
    # ascii0to31 - "All below ASCII 32". Tabs go too; CR and LF have
    # already been consumed by the line split.
    return "".join(ch for ch in line if ord(ch) >= 32)


def filter_received_lines(lines: list[str], cfg: dict[str, Any]) -> list[str]:
    """Apply "Remove characters" / "Omit lines …" / "Omit empty lines"."""
    omit_chars = char_set(str(cfg.get("omit_lines_containing", "")))
    omit_string = str(cfg.get("omit_lines_with_string", ""))
    omit_empty = bool(cfg.get("omit_empty_lines", False))
    out: list[str] = []
    for line in lines:
        if omit_chars and any(ch in omit_chars for ch in line):
            continue
        if omit_string and omit_string in line:
            continue
        cleaned = strip_received_chars(line, cfg)
        if omit_empty and not cleaned.strip():
            continue
        out.append(cleaned)
    return out


def postprocess_received(data: bytes, cfg: dict[str, Any]) -> str:
    """Turn the captured bytes into the text that is written to disk."""
    text = bytes(data).decode(WIRE_ENCODING)
    lines, detected = wire_lines(text, cfg)
    lines = filter_received_lines(lines, cfg)
    if bool(cfg.get("trim_trailing_spaces", True)):
        # Fanuc-style punch output carries a space before every EOB.
        lines = [ln.rstrip(" \t") for ln in lines]
    if bool(cfg.get("insert_spaces", False)):
        dialect = str(cfg.get("dialect", "iso_mill"))
        lines = [insert_spaces(ln, dialect) for ln in lines]
    if not lines:
        return ""
    save_mode = str(cfg.get("save_line_ending", "CRLF")).upper()
    eol = detected if save_mode == "KEEP" else LINE_ENDINGS.get(save_mode, "\r\n")
    # Every saved line is terminated, so the last line always ends with a
    # line feed (CIMCO's default last-LF policy).
    return "".join(line + eol for line in lines)


#: Dialects whose blocks are word-addressed (letter + number) and read
#: better with a space before every address. Conversational formats
#: (Heidenhain plain-language, Mazatrol) are left exactly as punched.
SPACED_DIALECTS = {
    "iso_mill", "iso_lathe", "fanuc", "haas", "heidenhain_iso", "siemens", "mazak_iso", "okuma"
}
_ADDRESS_RE = re.compile(r"(?<=[^\s(])(?=[A-Z(])")


def insert_spaces(line: str, dialect: str = "iso_mill") -> str:
    """CIMCO "Insert spaces": ``G01X10.Y-5.F100`` -> ``G01 X10. Y-5. F100``.

    A space goes before every address letter and before a comment that
    is glued to the previous word. Text inside ``(...)`` comments and
    lines that already contain spaces between words are left alone.
    """
    if dialect not in SPACED_DIALECTS or not line:
        return line
    out: list[str] = []
    depth = 0
    for ch in line:
        if ch == "(":
            if depth == 0 and out and not out[-1].isspace():
                out.append(" ")
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif depth == 0 and ch.isalpha() and ch.isupper() and out:
            prev = out[-1]
            # letter following a digit/sign/dot = new address; letters in a
            # row (e.g. "MOXA") stay together
            if not prev.isspace() and not prev.isalpha():
                out.append(" ")
        out.append(ch)
    return "".join(out)


def next_free_name(path: Path) -> Path:
    """``prog.nc`` -> ``prog_1.nc`` -> ``prog_2.nc`` ..."""
    if not path.exists():
        return path
    stem, suffix = path.stem, path.suffix
    n = 1
    while True:
        candidate = path.with_name(f"{stem}_{n}{suffix}")
        if not candidate.exists():
            return candidate
        n += 1


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class Receiver:
    """One receive job at a time."""

    def __init__(self, bus: EventBus, transport_factory=None) -> None:
        self.bus = bus
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._state = ReceiveState.IDLE
        bus.subscribe("transport.line_error", self._on_line_error)
        self._progress = ReceiveProgress()
        self._last_emit = 0.0
        self._transport: Transport | None = None
        self._overwrite_token = ""
        self._overwrite_answer: str = ""
        self._overwrite_evt = threading.Event()
        #: Populated on success - the path actually written.
        self.last_path: str = ""
        #: Exact bytes of the last capture, before any conversion.
        self.last_raw: bytes = b""
        self._cfg: dict[str, Any] = {}

    # -- introspection ---------------------------------------------------
    def _on_line_error(self, evt: Any) -> None:
        """Device-reported parity/framing/overrun/break while receiving."""
        if not self.is_running:
            return
        with self._lock:
            self._progress.errors += 1
        log.warning("Serial line error reported by the device: %s", (evt.payload or {}).get("error"))

    @property
    def state(self) -> ReceiveState:
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._progress.to_dict()

    def _open_with_retry(
        self, transport: Transport, machine: dict[str, Any], cfg: dict[str, Any]
    ) -> None:
        """Connect, retrying until Stop or the overall receive timeout.

        A device server keeps a dropped slot busy for a while (Max
        connection 1), and the operator may still be walking to the
        control: a receive must not fail on the first refused connect.
        ``auto_reconnect`` off means a single try.
        """
        delay = max(1.0, float(machine.get("reconnect_delay_s", 3) or 3))
        budget = float(cfg.get("overall_timeout_s", 1800) or 1800)
        deadline = time.monotonic() + budget
        attempt = 0
        while True:
            attempt += 1
            if self._stop_evt.is_set():
                raise TransportError("Stopped before the connection was made.")
            try:
                transport.open(machine)
                if attempt > 1:
                    log.info("Connected on attempt %d.", attempt)
                return
            except TransportError as exc:
                with self._lock:
                    self._progress.errors += 1
                if not machine.get("auto_reconnect", True) or time.monotonic() + delay > deadline:
                    raise
                log.warning("Connect attempt %d failed (%s); retrying in %.0fs.", attempt, exc, delay)
                self._set_state(
                    ReceiveState.CONNECTING, f"Reconnecting (attempt {attempt + 1}) - {exc}"
                )
                end = time.monotonic() + delay
                while time.monotonic() < end and not self._stop_evt.is_set():
                    time.sleep(0.05)

    # -- control ---------------------------------------------------------
    def start(
        self,
        machine: dict[str, Any],
        transport: Transport | None = None,
        filename_override: str = "",
    ) -> bool:
        if self.is_running:
            log.warning("Receive requested while one is already running - ignored.")
            return False
        self._stop_evt.clear()
        self._overwrite_evt.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(dict(machine), transport, filename_override),
            daemon=True,
            name="moxa-receiver",
        )
        self._thread.start()
        return True

    def stop(self, wait: bool = False, timeout: float = 5.0) -> None:
        self._stop_evt.set()
        self._overwrite_evt.set()
        t = self._thread
        if wait and t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    def resolve_overwrite(self, token: str, decision: str) -> bool:
        """UI answer to an ``ask`` prompt: overwrite | rename | cancel."""
        with self._lock:
            if token != self._overwrite_token:
                log.warning("Stale overwrite response for token %s - ignored.", token)
                return False
            self._overwrite_answer = decision
        self._overwrite_evt.set()
        return True

    # -- the job ---------------------------------------------------------
    def _run(
        self,
        machine: dict[str, Any],
        transport: Transport | None,
        filename_override: str,
    ) -> None:
        started = time.monotonic()
        owns_transport = transport is None
        cfg = machine.get("receive", {})
        name = machine.get("name", "?")
        try:
            with self._lock:
                self._progress = ReceiveProgress(
                    machine_id=str(machine.get("id", "")),
                    machine_name=str(name),
                )
            self._set_state(ReceiveState.CONNECTING, f"Connecting to {name}")
            if transport is None:
                transport = self._make_transport(machine)
            self._transport = transport
            self._cfg = dict(cfg)
            if not transport.is_open:
                self._open_with_retry(transport, machine, cfg)
            transport.purge(rx=True, tx=False)
            self._announce_ready(transport, cfg)

            buf = self._capture(transport, cfg, started, machine)
            if buf is None:
                return  # stopped / errored, state already set
            self.last_raw = bytes(buf)
            log.info(
                "Raw capture: %d bytes, first 48: %s", len(buf), bytes(buf[:48]).hex(" ")
            )

            if not buf:
                self._fail("No data was received from the control.")
                return

            path = self._write_file(machine, cfg, buf, filename_override)
            if path is None:
                return

            self.last_path = str(path)
            self._update(target_path=str(path), target_name=path.name)
            self._set_state(ReceiveState.DONE, f"Saved {path.name}")
            log.info("Received %d bytes -> %s", len(buf), path)
            self.bus.publish(
                "receive.done",
                {**self.snapshot(), "path": str(path), "bytes": len(buf)},
            )
        except OverwriteDenied as exc:
            self._fail(str(exc))
        except TransportError as exc:
            self._fail(str(exc))
        except OSError as exc:
            self._fail(f"Could not write the received file: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("Unhandled error during receive")
            self._fail(f"Unexpected error: {exc}")
        finally:
            if owns_transport and self._transport is not None:
                try:
                    self._transport.close()
                except Exception:
                    pass
            self._transport = None

    def _make_transport(self, machine: dict[str, Any]) -> Transport:
        if self._transport_factory is not None:
            return self._transport_factory(machine, self.bus)
        from moxaserial.transport import create_transport

        return create_transport(machine, self.bus)

    # -- handshake -------------------------------------------------------
    def _announce_ready(self, transport: Transport, cfg: dict[str, Any]) -> None:
        """CIMCO "Send XOn" + "Send at start of reception".

        Both are best-effort: a control that ignores them is normal, and a
        transport that refuses the write must not sink the whole receive.
        """
        blob = b""
        if cfg.get("send_xon", False):
            blob += bytes([transport.flow_control.xon & 0xFF])
        start_chars = unescape(str(cfg.get("start_chars", "")))
        if start_chars:
            blob += start_chars.encode(WIRE_ENCODING, errors="replace")
        if not blob:
            return
        try:
            transport.write(blob)
            log.info("Sent %d byte(s) of start-of-reception data.", len(blob))
        except TransportError as exc:
            log.warning("Could not send the start-of-reception data: %s", exc)

    # -- capture ---------------------------------------------------------
    def _capture(
        self,
        transport: Transport,
        cfg: dict[str, Any],
        started: float,
        machine: dict[str, Any] | None = None,
    ) -> bytearray | None:
        start_trigger = unescape(str(cfg.get("start_trigger", ""))).encode(
            WIRE_ENCODING, "replace"
        )
        end_trigger = unescape(str(cfg.get("end_trigger", ""))).encode(
            WIRE_ENCODING, "replace"
        )
        # CIMCO PCFG_RECEIVE_TRIGGERMODE_*: mode 2 drops the start trigger
        # itself ("Save after trigger"), mode 5 drops the end trigger
        # ("Save until trigger"). The defaults keep both, which is mode 1
        # and mode 4.
        drop_start = str(cfg.get("start_trigger_mode", "")) == "save_after_trigger"
        drop_end = str(cfg.get("end_trigger_mode", "")) == "save_until_trigger"
        # 0 = no idle timeout (CIMCO RECV_TIMEOUT).
        idle_timeout = float(cfg.get("idle_timeout_s", 5) or 0)
        overall_timeout = float(cfg.get("overall_timeout_s", 1800))
        serial = (machine or {}).get("serial", {})
        parity_check = bool(serial.get("check_parity", False))
        parity_mode = str(serial.get("parity", "none"))
        parity_data_bits = int(serial.get("data_bits", 8) or 8)
        parity_marker = unescape(str(serial.get("parity_insert", "")))

        buf = bytearray()
        pending = bytearray()  # pre-trigger scratch
        armed = not start_trigger
        last_data = time.monotonic()
        self._set_state(
            ReceiveState.WAITING,
            "Waiting for the control to start sending"
            + (f" (trigger '{cfg.get('start_trigger')}')" if start_trigger else ""),
        )

        while True:
            if self._stop_evt.is_set():
                if buf:
                    log.info("Receive stopped by operator with %d bytes buffered.", len(buf))
                    return buf
                self._set_state(ReceiveState.STOPPED, "Stopped before any data arrived")
                return None

            now = time.monotonic()
            if now - started > overall_timeout:
                if buf:
                    log.warning("Receive hit the overall timeout; keeping %d bytes.", len(buf))
                    return buf
                self._fail(f"Timed out after {overall_timeout:.0f}s with no data.")
                return None
            if buf and idle_timeout > 0 and (now - last_data) >= idle_timeout:
                log.info("Line idle for %.0fs - ending receive.", idle_timeout)
                return buf

            try:
                data = transport.read(4096, timeout=0.2)
            except TransportError as exc:
                if buf:
                    log.warning("Transport ended mid-receive (%s); keeping %d bytes.", exc, len(buf))
                    return buf
                raise

            if not data:
                self._update(rx=False, idle_s=(now - last_data) if buf else 0.0)
                self._emit_progress(force=False)
                continue

            last_data = time.monotonic()
            self._update(rx=True, idle_s=0.0)

            if parity_check:
                data, bad = check_parity(
                    data, parity_mode, parity_data_bits, parity_marker
                )
                if bad:
                    with self._lock:
                        self._progress.errors += bad
                    log.warning("%d parity error(s) in the incoming data.", bad)

            if not armed:
                pending.extend(data)
                pos = pending.find(start_trigger)
                if pos < 0:
                    # Keep just enough to catch a trigger split across reads.
                    if len(pending) > len(start_trigger) * 4 + 64:
                        del pending[: -(len(start_trigger) * 4 + 64)]
                    self._emit_progress(force=False)
                    continue
                armed = True
                data = bytes(pending[pos + len(start_trigger):]) if drop_start else bytes(
                    pending[pos:]
                )
                pending.clear()
                log.info("Start trigger seen - capturing.")
                self._set_state(ReceiveState.RECEIVING, "Receiving")
            elif self.state is not ReceiveState.RECEIVING:
                self._set_state(ReceiveState.RECEIVING, "Receiving")

            buf.extend(data)
            self._note_bytes(buf)
            elapsed = time.monotonic() - started
            self._update(
                elapsed_s=elapsed,
                cps=(len(buf) / elapsed) if elapsed > 0.2 else 0.0,
            )
            self._emit_progress(force=False)

            if end_trigger:
                # Only look for the end trigger past the start trigger itself.
                search_from = len(start_trigger) if start_trigger and not drop_start else 0
                found = buf.find(end_trigger, search_from)
                if found >= 0:
                    cut = found if drop_end else found + len(end_trigger)
                    log.info("End trigger seen - ending receive.")
                    return buf[:cut]

    def _note_bytes(self, buf: bytearray, cfg: dict[str, Any] | None = None) -> None:
        """Update counters and the live tail shown in the palette.

        The tail goes through the same block-boundary, trim and spacing
        rules as the saved file, so the preview matches what lands on disk.
        Only the last few KB are converted, so a long capture stays cheap.
        """
        cfg = cfg or self._cfg or {}
        window = bytes(buf[-4096:]).decode(WIRE_ENCODING)
        lines, _ = wire_lines(window, cfg)
        if len(buf) > 4096 and lines:
            lines = lines[1:]  # first line of the window is probably cut
        if bool(cfg.get("trim_trailing_spaces", True)):
            lines = [ln.rstrip(" \t") for ln in lines]
        if bool(cfg.get("insert_spaces", False)):
            dialect = str(cfg.get("dialect", "iso_mill"))
            lines = [insert_spaces(ln, dialect) for ln in lines]
        mode = str(cfg.get("line_ending", "AUTO")).upper()
        if mode in ("AUTO", "", "CUSTOM"):
            count = len(re.findall(rb"[\r\n]+", bytes(buf)))
        else:
            sep = LINE_ENDINGS.get(mode, "\n").encode(WIRE_ENCODING)
            count = bytes(buf).count(sep)
        with self._lock:
            self._progress.bytes_received = len(buf)
            self._progress.lines_received = count
            self._progress.tail = lines[-PREVIEW_TAIL:]

    # -- file writing ----------------------------------------------------
    def _write_file(
        self,
        machine: dict[str, Any],
        cfg: dict[str, Any],
        buf: bytearray,
        filename_override: str,
    ) -> Path | None:
        text = postprocess_received(buf, cfg)

        folder = Path(os.path.expanduser(str(cfg.get("folder", "")).strip() or "."))
        folder.mkdir(parents=True, exist_ok=True)

        if filename_override.strip():
            filename = sanitize(filename_override.strip())
        else:
            filename = render_filename(
                str(cfg.get("filename_pattern", "{machine}_{date}_{time}.nc")),
                str(machine.get("name", "machine")),
                program_name(text),
            )
        ext = str(cfg.get("append_extension", "")).strip()
        if ext and not Path(filename).suffix:
            filename += ext if ext.startswith(".") else f".{ext}"

        path = folder / filename
        policy = str(cfg.get("overwrite", "ask")).lower()
        try:
            path = self._apply_overwrite_policy(path, policy)
        except OverwriteDenied as exc:
            # The capture is complete and in memory - never throw it away.
            fallback = exc.fallback or next_free_name(path)
            fallback.write_text(text, encoding=WIRE_ENCODING, errors="replace", newline="")
            self.last_path = str(fallback)
            self._update(target_path=str(fallback), target_name=fallback.name)
            log.warning("%s Saved the received program as %s instead.", exc, fallback)
            raise OverwriteDenied(f"{exc} Saved as {fallback.name} instead.", fallback) from exc
        if path is None:
            return None

        path.write_text(text, encoding=WIRE_ENCODING, errors="replace", newline="")
        return path

    def _apply_overwrite_policy(self, path: Path, policy: str) -> Path | None:
        if not path.exists():
            return path
        if policy == "allow":
            log.info("Overwriting existing %s (policy: allow).", path.name)
            return path
        if policy == "rename":
            new = next_free_name(path)
            log.info("%s exists - saving as %s (policy: rename).", path.name, new.name)
            return new
        if policy == "deny":
            raise OverwriteDenied(
                f"{path.name} already exists and this machine's overwrite policy is 'deny'.",
                fallback=next_free_name(path),
            )
        # ask - round-trip to the UI
        token = uuid.uuid4().hex[:8]
        with self._lock:
            self._overwrite_token = token
            self._overwrite_answer = ""
        self._overwrite_evt.clear()
        log.info("Asking the operator what to do about the existing %s", path.name)
        self.bus.publish(
            "receive.overwrite_request",
            {
                "token": token,
                "path": str(path),
                "name": path.name,
                "suggested": next_free_name(path).name,
            },
        )
        if not self._overwrite_evt.wait(OVERWRITE_PROMPT_TIMEOUT):
            raise OverwriteDenied(
                f"No answer about overwriting {path.name} within "
                f"{OVERWRITE_PROMPT_TIMEOUT:.0f}s.",
                fallback=next_free_name(path),
            )
        if self._stop_evt.is_set() and not self._overwrite_answer:
            self._set_state(ReceiveState.STOPPED, "Stopped")
            return None
        with self._lock:
            answer = (self._overwrite_answer or "cancel").lower()
        if answer == "overwrite":
            return path
        if answer == "rename":
            return next_free_name(path)
        raise OverwriteDenied(
            f"Operator cancelled - {path.name} was not replaced.", fallback=next_free_name(path)
        )

    # -- state / progress plumbing ---------------------------------------
    def _update(self, **fields: Any) -> None:
        with self._lock:
            for k, v in fields.items():
                if hasattr(self._progress, k):
                    setattr(self._progress, k, v)

    def _set_state(self, state: ReceiveState, message: str = "") -> None:
        with self._lock:
            self._state = state
            self._progress.state = state.value
            if message:
                self._progress.message = message
        self.bus.publish("receive.state", self.snapshot())
        self._emit_progress(force=True)

    def _emit_progress(self, force: bool = True) -> None:
        now = time.monotonic()
        if not force and (now - self._last_emit) < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        self.bus.publish("receive.progress", self.snapshot())

    def _fail(self, message: str) -> None:
        log.error("Receive failed: %s", message)
        with self._lock:
            self._progress.error = message
            self._progress.rx = False
        self._set_state(ReceiveState.ERROR, message)
        self.bus.publish("receive.error", {**self.snapshot(), "message": message})
