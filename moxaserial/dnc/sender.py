"""Send engine - streams a preprocessed NC program to a control.

Runs on its own daemon thread and publishes everything the UI needs onto
the event bus; it never imports ``adsk`` and never touches the UI
directly. The palette bridge (or the dev server) subscribes and forwards.

States::

    IDLE -> CONNECTING -> WAITING_READY -> SENDING -> DONE
                                  |          |  ^
                                  |          v  |
                                  |        PAUSED
                                  +-----> STOPPED / ERROR

Flow control
------------
* **XON/XOFF** is enforced here, above the transport: the read side of
  the wire is polled between chunks and an XOFF suspends writing until
  the matching XON arrives, or until ``handshake_timeout_s`` elapses
  (CIMCO's "Handshake timeout (seconds)"; 0 = wait indefinitely, which is
  CIMCO's default and the right answer during a long tool change).
* **Break after receiving characters** (``break_count``) aborts the send
  once the control has sent back that many characters, exactly as CIMCO's
  ``TRAN_BREAKCOUNT`` does. 0 means inbound data is ignored.
* **Wait for ready** (``immediate`` | ``cts`` | ``dsr`` | ``xon``) gates
  the very first byte. If the transport cannot read modem status - which
  is the case for the Moxa transport until the ASPP command channel
  lands - a hardware wait degrades to a logged warning plus an immediate
  start rather than a guaranteed hang.

Events published
----------------
``send.state``     {state, machine, file, ...}
``send.progress``  the full progress snapshot (throttled)
``send.log``       human-readable milestones
``send.done``      terminal success
``send.error``     terminal failure {message}
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from moxaserial.dnc.preprocess import (
    PreprocessOptions,
    PreprocessResult,
    preprocess,
    preprocess_file,
)
from moxaserial.events import EventBus
from moxaserial.log import get_logger
from moxaserial.transport.base import FlowControl, ModemStatus, Transport, TransportError

log = get_logger("sender")

#: Controls commonly use DC2 to request a punch-out and DC4 to abort it.
DC2 = 0x12
DC4 = 0x14

#: Minimum seconds between throttled progress events.
PROGRESS_INTERVAL = 0.08

#: Number of lines of context sent either side of the in-flight line.
PREVIEW_BEFORE = 6
PREVIEW_AFTER = 8


class SendState(StrEnum):
    IDLE = "IDLE"
    CONNECTING = "CONNECTING"
    WAITING_READY = "WAITING_READY"
    SENDING = "SENDING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"
    DONE = "DONE"
    ERROR = "ERROR"

    @property
    def is_terminal(self) -> bool:
        return self in (SendState.STOPPED, SendState.DONE, SendState.ERROR)

    @property
    def is_active(self) -> bool:
        return self in (
            SendState.CONNECTING,
            SendState.WAITING_READY,
            SendState.SENDING,
            SendState.PAUSED,
        )


@dataclass
class SendProgress:
    """Snapshot handed to the UI on every progress event."""

    state: str = SendState.IDLE.value
    machine_id: str = ""
    machine_name: str = ""
    file_path: str = ""
    file_name: str = ""
    bytes_sent: int = 0
    bytes_total: int = 0
    lines_sent: int = 0
    lines_total: int = 0
    line_index: int = -1
    line_text: str = ""
    percent: float = 0.0
    elapsed_s: float = 0.0
    #: CIMCO's "Remaining time:" on the transmit status dialog.
    eta_s: float = -1.0
    rate_bps: float = 0.0
    #: CIMCO's "CPS:" - characters per second, which for 8-bit data is the
    #: same number as ``rate_bps``; carried separately because the status
    #: panel labels it the way an operator expects to read it.
    cps: float = 0.0
    #: Bytes accepted by the device but not yet on the serial line.
    device_pending: int = 0
    #: CIMCO's "Errors:" counter - failed connect attempts, characters the
    #: control sent back, and aborts requested by the control.
    errors: int = 0
    #: Characters received back from the control during the send, which is
    #: what CIMCO's "Break after receiving characters" counts.
    inbound_chars: int = 0
    tx: bool = False
    rx: bool = False
    xoff: bool = False
    paused: bool = False
    modem: dict[str, bool] = field(default_factory=lambda: ModemStatus().to_dict())
    window: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Sender:
    """One send job at a time. Reusable: ``resend()`` replays the last job."""

    def __init__(self, bus: EventBus, transport_factory=None) -> None:
        self.bus = bus
        self._transport_factory = transport_factory
        self._lock = threading.RLock()
        self._thread: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self._resume_evt = threading.Event()
        self._resume_evt.set()
        self._state = SendState.IDLE
        self._progress = SendProgress()
        self._last_emit = 0.0
        self._last_job: dict[str, Any] | None = None
        self._transport: Transport | None = None
        self._xoff = False
        self._inbound = 0
        self._break_count = 0
        self._queue_limit = 0
        bus.subscribe("transport.line_error", self._on_line_error)

    def _on_line_error(self, evt: Any) -> None:
        """Device-reported parity/framing/overrun/break during a send."""
        if not self.is_running or self.state.is_terminal:
            return
        # Runs on the transport's reader thread: touch counters only. Any
        # transport call from here (e.g. a modem-status query inside
        # _emit_progress) would wait for a reply that this very thread must
        # deliver - a deadlock until timeout, and the heartbeat stops.
        err = str((evt.payload or {}).get("error", "line error"))
        with self._lock:
            self._progress.errors += 1
        self._emit_log(f"Serial line error reported by the device: {err}", level="WARNING")

    # -- introspection ---------------------------------------------------
    @property
    def state(self) -> SendState:
        with self._lock:
            return self._state

    @property
    def is_running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return self._progress.to_dict()

    @property
    def can_resend(self) -> bool:
        return self._last_job is not None

    # -- control ---------------------------------------------------------
    def start(
        self,
        machine: dict[str, Any],
        file_path: str = "",
        text: str | None = None,
        transport: Transport | None = None,
    ) -> bool:
        """Begin a send. Returns False if one is already running."""
        if self.is_running:
            log.warning("Send requested while a send is already running - ignored.")
            return False

        if text is None and (not file_path or not os.path.isfile(file_path)):
            self._fail(f"File not found: {file_path or '(none)'}", machine, file_path)
            return False

        self._last_job = {
            "machine": dict(machine),
            "file_path": file_path,
            "text": text,
            "transport": transport,
        }
        self._stop_evt.clear()
        self._resume_evt.set()
        self._xoff = False
        self._inbound = 0
        self._break_count = max(0, int(machine.get("send", {}).get("break_count", 0) or 0))
        self._thread = threading.Thread(
            target=self._run,
            args=(dict(machine), file_path, text, transport),
            daemon=True,
            name="moxa-sender",
        )
        self._thread.start()
        return True

    def resend(self) -> bool:
        """Restart the last job from the beginning."""
        job = self._last_job
        if job is None:
            log.warning("Resend requested but nothing has been sent yet.")
            return False
        if self.is_running:
            self.stop(wait=True)
        return self.start(
            job["machine"], job["file_path"], job["text"], job.get("transport")
        )

    def pause(self) -> None:
        if self.state in (SendState.SENDING, SendState.WAITING_READY):
            self._resume_evt.clear()
            self._set_state(SendState.PAUSED, message="Paused by operator")
            log.info("Send paused.")

    def resume(self) -> None:
        if self.state is SendState.PAUSED:
            self._resume_evt.set()
            self._set_state(SendState.SENDING, message="Resumed")
            log.info("Send resumed.")

    def stop(self, wait: bool = False, timeout: float = 5.0) -> None:
        """Ask the job to stop; the thread ends at the next chunk boundary."""
        self._stop_evt.set()
        self._resume_evt.set()  # release a paused loop so it can see the stop
        t = self._thread
        if wait and t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=timeout)

    # -- the job ---------------------------------------------------------
    def _run(
        self,
        machine: dict[str, Any],
        file_path: str,
        text: str | None,
        transport: Transport | None,
    ) -> None:
        started = time.monotonic()
        owns_transport = transport is None
        name = machine.get("name", "?")
        try:
            opts = PreprocessOptions.from_machine(machine)
            result = (
                preprocess(text, opts) if text is not None else preprocess_file(file_path, opts)
            )
            self._init_progress(machine, file_path, result)
            log.info(
                "Preparing %s for %s: %d lines (%d dropped), %d bytes",
                os.path.basename(file_path) or "in-memory program",
                name,
                result.line_count,
                result.dropped_lines,
                result.byte_count,
            )

            self._set_state(SendState.CONNECTING, message=f"Connecting to {name}")
            if transport is None:
                transport = self._make_transport(machine)
            self._transport = transport
            send_cfg = machine.get("send", {})
            if (
                str(send_cfg.get("wait_for_ready", "immediate")).lower() == "xon"
                and FlowControl.from_machine(machine).software
            ):
                # The NPort firmware / OS driver would consume the control's
                # XON itself and we would never see it (GitHub issue #2).
                # Pass-through until the XON arrives; host-side XON/XOFF
                # handling below covers the wait.
                transport.suspend_software_flow()
                log.info("Device-side XON/XOFF suspended until the control sends XON.")
            if not transport.is_open:
                self._open_with_retry(transport, machine)
            transport.purge(rx=True, tx=False)

            if not self._wait_for_ready(transport, machine, send_cfg):
                return  # _wait_for_ready set the terminal state already

            self._set_state(SendState.SENDING, message="Sending")
            # Elapsed / CPS measure the transfer, not the time spent waiting
            # for the control to say go (CIMCO's status dialog does the same).
            started = time.monotonic()
            self._stream(transport, machine, result, started)

        except TransportError as exc:
            self._fail(str(exc), machine, file_path)
        except OSError as exc:
            self._fail(f"File error: {exc}", machine, file_path)
        except Exception as exc:  # noqa: BLE001 - surface anything unexpected
            log.exception("Unhandled error during send")
            self._fail(f"Unexpected error: {exc}", machine, file_path)
        finally:
            if owns_transport and self._transport is not None:
                # Close first: resuming on a dead link would block on the
                # command timeout. On a closed transport resume only clears
                # the flag.
                try:
                    self._transport.close()
                except Exception:
                    pass
            if self._transport is not None:
                # A transport handed in from outside is reused: never leave it
                # in pass-through. (No-op unless we suspended it.)
                try:
                    self._transport.resume_software_flow()
                except Exception:  # noqa: BLE001
                    pass
            self._transport = None

    def _resume_device_flow(self, transport: Transport) -> None:
        """Hand XON/XOFF back to the device once the control's XON was seen."""
        if not transport.software_flow_suspended:
            return
        try:
            transport.resume_software_flow()
            log.info("Device-side XON/XOFF re-enabled.")
        except TransportError as exc:
            # Not fatal: the host-side handling in _stream still honours
            # XOFF/XON; the device just will not stop on its own.
            log.warning("Could not re-enable device-side XON/XOFF: %s", exc)
            self._emit_log(
                "Could not re-enable flow control on the device; using host-side XON/XOFF.",
                level="WARNING",
            )

    def _make_transport(self, machine: dict[str, Any]) -> Transport:
        if self._transport_factory is not None:
            return self._transport_factory(machine, self.bus)
        from moxaserial.transport import create_transport

        return create_transport(machine, self.bus)

    def _open_with_retry(self, transport: Transport, machine: dict[str, Any]) -> None:
        """Open the transport, retrying per CIMCO's "Attempt auto re-connect".

        ``auto_reconnect`` off, or ``reconnect_attempts`` 0, means one try -
        the previous behaviour. Each failed attempt bumps the error counter
        the status panel shows and is logged, so a flaky network hub is
        visible rather than silently papered over.
        """
        attempts = 1
        if machine.get("auto_reconnect", True):
            attempts += max(0, int(machine.get("reconnect_attempts", 0) or 0))
        delay = max(0.0, float(machine.get("reconnect_delay_s", 0) or 0))
        last: TransportError | None = None
        for attempt in range(1, attempts + 1):
            if self._stop_evt.is_set():
                raise TransportError("Stopped before the connection was made.")
            try:
                transport.open(machine)
                if attempt > 1:
                    log.info("Connected on attempt %d/%d.", attempt, attempts)
                    self._emit_log(f"Connected on attempt {attempt} of {attempts}.")
                return
            except TransportError as exc:
                last = exc
                with self._lock:
                    self._progress.errors += 1
                if attempt >= attempts:
                    break
                log.warning(
                    "Connect attempt %d/%d failed (%s); retrying in %.0fs.",
                    attempt, attempts, exc, delay,
                )
                self._emit_log(
                    f"Connect attempt {attempt} of {attempts} failed: {exc}",
                    level="WARNING",
                )
                self._set_state(
                    SendState.CONNECTING,
                    message=f"Reconnecting ({attempt + 1}/{attempts})",
                )
                deadline = time.monotonic() + delay
                while time.monotonic() < deadline and not self._stop_evt.is_set():
                    time.sleep(0.05)
        raise last if last is not None else TransportError("Could not open the port.")

    # -- phases ----------------------------------------------------------
    def _wait_for_ready(
        self, transport: Transport, machine: dict[str, Any], send_cfg: dict[str, Any]
    ) -> bool:
        """Block until the control says it is ready. False == job finished."""
        mode = str(send_cfg.get("wait_for_ready", "immediate")).lower()
        if mode == "immediate":
            return True

        timeout = float(send_cfg.get("ready_timeout_s", 60))
        caps = getattr(transport, "capabilities", None)
        if mode in ("cts", "dsr") and callable(caps):
            info = caps()
            if not info.get("can_read_modem_status", True):
                log.warning(
                    "Machine is set to wait for %s, but this transport cannot read "
                    "modem status yet - starting immediately.",
                    mode.upper(),
                )
                self._emit_log(
                    f"Cannot read {mode.upper()} on this transport; sending immediately.",
                    level="WARNING",
                )
                return True

        label = {"cts": "CTS", "dsr": "DSR", "xon": "XON from the control"}.get(mode, mode)
        self._set_state(SendState.WAITING_READY, message=f"Waiting for {label}")
        log.info("Waiting up to %.0fs for %s", timeout, label)

        flow = transport.flow_control
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._stop_evt.is_set():
                self._set_state(SendState.STOPPED, message="Stopped before sending")
                return False
            if not self._resume_evt.is_set():
                paused_at = time.monotonic()
                self._wait_if_paused()
                deadline += time.monotonic() - paused_at  # pause does not eat the timeout
                if self._stop_evt.is_set():
                    self._set_state(SendState.STOPPED, message="Stopped before sending")
                    return False
                self._set_state(SendState.WAITING_READY, message=f"Waiting for {label}")
            time.sleep(0.05)

            try:
                if mode in ("cts", "dsr"):
                    status = transport.get_modem_status()
                    self._update(modem=status.to_dict())
                    if (mode == "cts" and status.cts) or (mode == "dsr" and status.dsr):
                        log.info("%s asserted - starting.", label)
                        return True
                else:  # xon
                    data = transport.read(64, timeout=0.15)
                    if data:
                        self._update(rx=True)
                        log.info("RX while waiting for %s: %s", label, data.hex(" "))
                        self._emit_log(f"Control sent: {data.hex(' ')}")
                        # Last control character wins, as a UART sees it:
                        # "XON ... XOFF" in one read means the control is
                        # holding again, so keep waiting for the next XON.
                        for byte in data:
                            if byte == flow.xon or byte == DC2:
                                self._xoff = False
                            elif byte == flow.xoff:
                                self._xoff = True
                        if (flow.xon in data or DC2 in data) and not self._xoff:
                            log.info("Received XON/DC2 - starting.")
                            self._resume_device_flow(transport)
                            return True
            except TransportError as exc:
                # A device server on Wi-Fi, or a port restart, can drop us
                # while we sit here for minutes. Reconnect and keep waiting
                # rather than failing a job that has not sent a byte yet.
                log.warning("Connection lost while waiting for %s: %s", label, exc)
                self._emit_log(f"Connection lost while waiting for {label}; reconnecting.", "WARNING")
                with self._lock:
                    self._progress.errors += 1
                try:
                    transport.close()
                except Exception:  # noqa: BLE001
                    pass
                self._set_state(SendState.CONNECTING, message="Reconnecting")
                self._open_with_retry(transport, machine)
                transport.purge(rx=True, tx=False)
                flow = transport.flow_control
                self._set_state(SendState.WAITING_READY, message=f"Waiting for {label}")
                continue
            self._emit_progress(force=False)
            time.sleep(0.05)

        self._fail(
            f"Timed out after {timeout:.0f}s waiting for {label}.",
            machine,
            self._progress.file_path,
        )
        return False

    def _stream(
        self,
        transport: Transport,
        machine: dict[str, Any],
        result: PreprocessResult,
        started: float,
    ) -> None:
        send_cfg = machine.get("send", {})
        chunk_size = max(1, int(send_cfg.get("chunk_size", 256)))
        self._queue_limit = max(0, int(send_cfg.get("device_queue_limit", 512)))
        char_delay = max(0, int(send_cfg.get("char_delay_ms", 0))) / 1000.0
        line_delay = max(0, int(send_cfg.get("line_delay_ms", 0))) / 1000.0
        # CIMCO TRAN_TIMEOUT: how long to sit on an XOff / CTS-low before
        # giving up. 0 means wait indefinitely ("it will wait until a start
        # flow is received") - a long tool change must not fail a drip feed.
        hold_timeout = max(0.0, float(send_cfg.get("handshake_timeout_s", 0) or 0))
        flow = transport.flow_control
        software_flow = flow.software

        if result.prologue:
            if not self._write_block(
                transport, result.prologue, chunk_size, char_delay, software_flow, hold_timeout
            ):
                return
            self._update(bytes_sent=self._progress.bytes_sent)

        for idx, blob in enumerate(result.line_blobs):
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return
            self._wait_if_paused()
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return

            self._update(
                line_index=idx,
                line_text=result.lines[idx] if idx < len(result.lines) else "",
                lines_sent=idx,
                window=self._window(result.lines, idx),
            )
            if line_delay:
                # CIMCO: "Delay before each line (ms)".
                time.sleep(line_delay)
            if not self._write_block(
                transport, blob, chunk_size, char_delay, software_flow, hold_timeout
            ):
                return
            self._update(lines_sent=idx + 1)
            self._recompute(started)
            self._emit_progress()

        if result.epilogue and not self._write_block(
            transport, result.epilogue, chunk_size, char_delay, software_flow, hold_timeout
        ):
            return

        # Everything is handed to the transport; wait for it to actually
        # leave the serial port (an NPort buffers kilobytes) so "complete"
        # means complete and Stop still works while the queue drains.
        if transport.pending_tx():
            self._update(tx=True, line_index=-1, message="Waiting for the device buffer to empty")
            self._emit_progress()
        # Worst case the device holds a few KB; at the line rate that is
        # seconds, but a control holding XOFF/CTS can stretch it, so give
        # it the same patience as the ready wait.
        baud = max(int(transport.line_params.baud), 300)
        buffered_time = (8192 * 11) / baud + 5.0
        def _abort_or_progress() -> bool:
            if self._stop_evt.is_set():
                return True
            pending = transport.pending_tx()
            if pending is not None:
                with self._lock:
                    self._progress.device_pending = pending
                self._recompute(started)
                self._emit_progress(force=False)
            return False

        drained = transport.drain(
            timeout=max(30.0, buffered_time, float(send_cfg.get("ready_timeout_s", 60))),
            should_abort=_abort_or_progress,
        )
        with self._lock:
            self._progress.device_pending = 0
        if self._stop_evt.is_set():
            self._finish_stopped(transport)
            return
        if not drained:
            log.warning("Device TX queue did not empty in time; reporting complete anyway.")
            self._emit_log("Device buffer did not report empty; the control may still be receiving.", level="WARNING")

        self._recompute(started)
        # A trailing XOFF from the control after our last byte is not a hold
        # on anything; the LED must not stay lit on a finished job.
        self._xoff = False
        self._update(percent=100.0, tx=False, xoff=False, line_index=-1, message="Transfer complete")
        self._set_state(SendState.DONE, message="Transfer complete")
        elapsed = time.monotonic() - started
        log.info(
            "Send complete: %d lines / %d bytes in %.1fs (%.0f B/s)",
            result.line_count,
            self._progress.bytes_sent,
            elapsed,
            self._progress.rate_bps,
        )
        self.bus.publish(
            "send.done",
            {
                **self.snapshot(),
                "elapsed_s": elapsed,
            },
        )

    # -- writing ---------------------------------------------------------
    def _write_block(
        self,
        transport: Transport,
        blob: bytes,
        chunk_size: int,
        char_delay: float,
        software_flow: bool,
        hold_timeout: float,
    ) -> bool:
        """Write one line (or the prologue/epilogue). False == job over."""
        step = 1 if char_delay > 0 else chunk_size
        for i in range(0, len(blob), step):
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return False
            self._wait_if_paused()
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return False
            if software_flow and not self._honor_xoff(transport, hold_timeout):
                return False
            if not self._throttle_device_queue(transport, software_flow, hold_timeout):
                return False

            chunk = blob[i : i + step]
            try:
                transport.write(chunk)
            except TransportError as exc:
                self._fail(str(exc), {}, self._progress.file_path)
                return False
            with self._lock:
                self._progress.bytes_sent += len(chunk)
                self._progress.tx = True
            if char_delay:
                time.sleep(char_delay)
            self._drain_incoming(transport, software_flow)
            self._emit_progress(force=False)
        return True

    def _throttle_device_queue(
        self, transport: Transport, software_flow: bool, timeout: float
    ) -> bool:
        """Wait while the device holds more than ``device_queue_limit`` bytes.

        A TCP-attached device server accepts a whole program instantly and
        then dribbles it out at the serial rate; without this the bar would
        hit 100% at once and Stop could not stop much. False == job over.
        """
        limit = self._queue_limit
        if limit <= 0:
            return True
        pending = transport.pending_tx()
        if pending is None:
            return True
        with self._lock:
            self._progress.device_pending = pending
        if pending <= limit:
            return True
        started = time.monotonic()
        while pending is not None and pending > limit:
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return False
            self._wait_if_paused()
            if timeout > 0 and time.monotonic() - started > timeout:
                self._fail(
                    f"The device did not transmit for {timeout:.0f}s "
                    f"({pending} bytes still queued) - is the control ready?",
                    {},
                    self._progress.file_path,
                )
                return False
            with self._lock:
                self._progress.device_pending = pending
                self._progress.tx = False
            self._drain_incoming(transport, software_flow)
            self._emit_progress(force=False)
            time.sleep(0.05)
            pending = transport.pending_tx()
        with self._lock:
            self._progress.device_pending = pending or 0
        return True

    def _drain_incoming(self, transport: Transport, software_flow: bool) -> None:
        """Non-blocking peek at the wire: XOFF/XON, DC4 abort, break count."""
        try:
            data = transport.read(64, timeout=0.0)
        except TransportError:
            raise
        if not data:
            with self._lock:
                self._progress.rx = False
            return
        self._inbound += len(data)
        with self._lock:
            self._progress.rx = True
            self._progress.inbound_chars = self._inbound
        flow = transport.flow_control
        if software_flow:
            # Last control character wins, which is what a UART sees.
            for byte in data:
                if byte == flow.xoff:
                    self._xoff = True
                elif byte == flow.xon:
                    self._xoff = False
        if DC4 in data:
            log.warning("Control sent DC4 (stop) - aborting the send.")
            self._emit_log("Control requested stop (DC4).", level="WARNING")
            with self._lock:
                self._progress.errors += 1
            self._stop_evt.set()
        with self._lock:
            self._progress.xoff = self._xoff
        if self._break_count and self._inbound >= self._break_count:
            # CIMCO EMSG_ABORT_SEND_BREAK_COUNT / DNC_TRANSFER_COMPLETED_FAILED_BREAK.
            with self._lock:
                self._progress.errors += 1
            self._fail(
                f"Break count exceeded: the control sent back {self._inbound} "
                f"characters (limit {self._break_count}).",
                {},
                self._progress.file_path,
            )
            self._stop_evt.set()

    def _honor_xoff(self, transport: Transport, timeout: float) -> bool:
        """Block while the control holds XOFF. False == job over.

        *timeout* is CIMCO's "Handshake timeout (seconds)". 0 (the default)
        means wait for as long as the control needs; the operator can still
        press Stop, and the UI shows the XOFF hold the whole time.
        """
        if not self._xoff:
            return True
        log.info("XOFF received - holding transmission.")
        self._emit_log("XOFF - holding.", level="INFO")
        deadline = time.monotonic() + max(timeout, 1.0) if timeout > 0 else None
        with self._lock:
            self._progress.xoff = True
            self._progress.tx = False
        self._emit_progress(force=True)
        while self._xoff:
            if self._stop_evt.is_set():
                self._finish_stopped(transport)
                return False
            if deadline is not None and time.monotonic() > deadline:
                # CIMCO EMSG_ABORT_SEND_HANDSHAKE_TIMEOUT.
                with self._lock:
                    self._progress.errors += 1
                self._fail(
                    f"Handshake timeout: the control held XOFF for more than "
                    f"{timeout:.0f}s.",
                    {},
                    self._progress.file_path,
                )
                return False
            self._wait_if_paused()
            try:
                data = transport.read(64, timeout=0.1)
            except TransportError as exc:
                self._fail(str(exc), {}, self._progress.file_path)
                return False
            if data:
                flow = transport.flow_control
                for byte in data:
                    if byte == flow.xon:
                        self._xoff = False
                    elif byte == flow.xoff:
                        self._xoff = True
            self._emit_progress(force=False)
        log.info("XON received - resuming transmission.")
        self._emit_log("XON - resuming.", level="INFO")
        with self._lock:
            self._progress.xoff = False
        self._emit_progress(force=True)
        return True

    def _wait_if_paused(self) -> None:
        while not self._resume_evt.is_set():
            if self._stop_evt.is_set():
                return
            self._emit_progress(force=False)
            self._resume_evt.wait(0.1)

    # -- state / progress plumbing ---------------------------------------
    def _init_progress(
        self, machine: dict[str, Any], file_path: str, result: PreprocessResult
    ) -> None:
        with self._lock:
            self._progress = SendProgress(
                state=SendState.IDLE.value,
                machine_id=str(machine.get("id", "")),
                machine_name=str(machine.get("name", "")),
                file_path=file_path,
                file_name=os.path.basename(file_path) if file_path else "(in-memory)",
                bytes_total=result.byte_count,
                lines_total=result.line_count,
                window=self._window(result.lines, 0),
            )

    def _window(self, lines: list[str], index: int) -> list[dict[str, Any]]:
        lo = max(0, index - PREVIEW_BEFORE)
        hi = min(len(lines), index + PREVIEW_AFTER + 1)
        return [
            {"i": i, "text": lines[i], "current": i == index} for i in range(lo, hi)
        ]

    def _update(self, **fields: Any) -> None:
        with self._lock:
            for k, v in fields.items():
                if hasattr(self._progress, k):
                    setattr(self._progress, k, v)

    def _recompute(self, started: float) -> None:
        with self._lock:
            p = self._progress
            p.elapsed_s = time.monotonic() - started
            on_wire = max(0, p.bytes_sent - p.device_pending)
            if p.bytes_total:
                p.percent = min(100.0, 100.0 * on_wire / p.bytes_total)
            if p.elapsed_s > 0.2 and on_wire > 0:
                p.rate_bps = on_wire / p.elapsed_s
                p.cps = p.rate_bps  # CIMCO "CPS:"
                remaining = max(0, p.bytes_total - on_wire)
                p.eta_s = remaining / p.rate_bps if p.rate_bps > 0 else -1.0

    def _set_state(self, state: SendState, message: str = "") -> None:
        with self._lock:
            self._state = state
            self._progress.state = state.value
            self._progress.paused = state is SendState.PAUSED
            if message:
                self._progress.message = message
        self.bus.publish("send.state", self.snapshot())
        self._emit_progress(force=True)

    def _emit_progress(self, force: bool = True) -> None:
        now = time.monotonic()
        if not force and (now - self._last_emit) < PROGRESS_INTERVAL:
            return
        self._last_emit = now
        transport = self._transport
        on_engine_thread = threading.current_thread() is self._thread
        if transport is not None and transport.is_open and on_engine_thread:
            try:
                self._update(modem=transport.get_modem_status().to_dict())
            except Exception:
                pass
        self.bus.publish("send.progress", self.snapshot())

    def _emit_log(self, message: str, level: str = "INFO") -> None:
        self.bus.publish("send.log", {"message": message, "level": level})

    def _finish_stopped(self, transport: Transport | None = None) -> None:
        if self.state.is_terminal:
            return
        # Drop whatever the device still has queued so the control does not
        # keep receiving after the operator pressed Stop.
        if transport is not None and transport.is_open:
            try:
                transport.purge(rx=False, tx=True)
            except TransportError as exc:
                log.warning("Could not flush the device TX buffer on stop: %s", exc)
        self._update(tx=False, message="Stopped")
        self._set_state(SendState.STOPPED, message="Stopped by operator")
        log.info("Send stopped by operator at %d bytes.", self._progress.bytes_sent)

    def _fail(self, message: str, machine: dict[str, Any], file_path: str) -> None:
        log.error("Send failed: %s", message)
        with self._lock:
            if not self._progress.machine_name and machine:
                self._progress.machine_name = str(machine.get("name", ""))
                self._progress.machine_id = str(machine.get("id", ""))
            if not self._progress.file_path and file_path:
                self._progress.file_path = file_path
                self._progress.file_name = os.path.basename(file_path)
            self._progress.error = message
            self._progress.tx = False
        self._set_state(SendState.ERROR, message=message)
        self.bus.publish("send.error", {**self.snapshot(), "message": message})
