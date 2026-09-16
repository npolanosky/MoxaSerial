"""MoxaTransport - a Moxa NPort serial port over raw TCP, no driver needed.

Two sockets per serial port (see :mod:`moxaserial.transport.aspp`):

* the **data socket** is a transparent byte pipe to the UART;
* the **command socket** carries ASPP frames: line settings, DTR/RTS,
  CTS/DSR/DCD readback, flush, TX-queue drain, and the device's
  heartbeat and modem-change notifications.

Threading
---------
A reader thread owns the command socket's receive side. It demultiplexes
the stream into (a) heartbeat ``POLLING`` frames, answered immediately,
(b) ``NOTIFY`` frames, folded into the cached modem status and published
on the event bus, and (c) replies, handed to whichever :meth:`_command`
call is waiting. Commands are serialised with a lock, so at most one
request is in flight at a time - which is what Moxa's own daemon does.

Hardware notes
--------------
* Set ``Max connection`` to 1 on the NPort. With 2+ the device ACKs every
  command but silently ignores line settings.
* ``Allow driver control`` must be enabled for Real COM mode.
* The command port is 966 + (port - 1) on every NPort we have seen; one
  manual revision prints 996. Both are configurable per machine.
* Points still unverified across NPort firmware revisions are listed under
  "Still unverified" in ARCHITECTURE.md §7.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Callable
from typing import Any

from moxaserial.events import EventBus
from moxaserial.log import get_logger
from moxaserial.transport import aspp
from moxaserial.transport.base import (
    FlowControl,
    LineParams,
    ModemStatus,
    Transport,
    TransportError,
    TransportTimeout,
)

log = get_logger("moxa")

COMMAND_TIMEOUT_S = 3.0
MODEM_CACHE_S = 0.5


class MoxaTransport(Transport):
    """Native NPort (ASPP) transport."""

    kind = "moxa"

    def __init__(self, bus: EventBus | None = None) -> None:
        super().__init__(bus=bus)
        self._sock: socket.socket | None = None
        self._cmd_sock: socket.socket | None = None
        self._io_lock = threading.RLock()
        self._rx_buf = bytearray()

        # command channel
        self._cmd_lock = threading.Lock()        # one command in flight
        self._cmd_write_lock = threading.Lock()  # ALIVE vs request writes
        self._reply_cv = threading.Condition()
        self._pending_op: int | None = None
        self._pending_resp: aspp.Response | None = None
        self._reader: threading.Thread | None = None
        self._reader_stop = threading.Event()
        self._reader_error: str | None = None
        self._stale_ops: dict[int, int] = {}
        self.command_channel_available = False

        # shadow state
        self._dtr = True
        self._rts = True
        self._device_flow = True
        self._tx_fifo = 16
        self._applied_init: tuple | None = None
        self._modem = ModemStatus()
        self._modem_at = 0.0
        self.polls_answered = 0
        self.line_errors: list[str] = []
        self.host = ""
        self.data_port = 0
        self.cmd_port = 0

    # -- lifecycle -------------------------------------------------------
    def _do_open(self, machine: dict[str, Any]) -> None:
        host = str(machine.get("host", "")).strip()
        if not host:
            raise TransportError("No host / IP address configured for this machine.")
        port_index = int(machine.get("port_index", 1))
        data_port = int(machine.get("data_port") or aspp.data_port_for(port_index))
        cmd_port = int(machine.get("cmd_port") or aspp.cmd_port_for(port_index))
        timeout = float(machine.get("connect_timeout_s", 8))
        serial = machine.get("serial", {})
        self._dtr = bool(serial.get("assert_dtr", True))
        self._rts = bool(serial.get("assert_rts", True))
        self._device_flow = bool(serial.get("device_flow_control", True))
        self._tx_fifo = int(serial.get("tx_fifo", 16) or 16)
        self.host, self.data_port, self.cmd_port = host, data_port, cmd_port

        log.info(
            "Connecting to NPort %s port %d (cmd %d, data %d)", host, port_index, cmd_port, data_port
        )
        # Moxa's daemon connects the command socket first, then data.
        try:
            cmd = socket.create_connection((host, cmd_port), timeout=timeout)
        except OSError as exc:
            raise TransportError(
                f"Could not open the NPort command port {host}:{cmd_port} - {exc}. "
                "Check the IP, that the serial port is in Real COM or TCP Server mode, "
                "and the command port number (966 + port - 1 on most firmware)."
            ) from exc
        cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        cmd.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        cmd.settimeout(0.5)
        self._cmd_sock = cmd

        try:
            data = socket.create_connection((host, data_port), timeout=timeout)
        except OSError as exc:
            self._close_sockets()
            raise TransportError(
                f"Could not open the NPort data port {host}:{data_port} - {exc}. "
                "Is another host already connected (Max connection = 1)?"
            ) from exc
        data.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        data.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        self._sock = data
        self._rx_buf.clear()

        self._reader_stop.clear()
        self._reader_error = None
        self._reader = threading.Thread(
            target=self._reader_loop, name="moxa-cmd-reader", daemon=True
        )
        self._reader.start()

        # PORT_INIT must be the first command. Base.open() will call
        # set_line_params / set_flow_control / set_dtr / set_rts right after
        # us; _applied_init makes those no-ops when nothing changed.
        try:
            self._port_init(force=True)
            if self._flow.software:
                self._command(aspp.encode_xonxoff(self._flow.xon, self._flow.xoff))
            self._command(aspp.encode_tx_fifo(self._tx_fifo))
        except TransportError:
            self._close_sockets()
            raise
        except Exception as exc:  # ProtocolError, OSError, ... - never leak sockets/threads
            self._close_sockets()
            raise TransportError(f"NPort handshake failed: {exc}") from exc
        self.command_channel_available = True

    def _do_close(self) -> None:
        self._close_sockets()

    def _close_sockets(self) -> None:
        self._reader_stop.set()
        with self._io_lock:
            for sock in (self._cmd_sock, self._sock):
                if sock is None:
                    continue
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    sock.close()
                except OSError:
                    pass
            self._sock = None
            self._cmd_sock = None
        with self._reply_cv:
            self._reply_cv.notify_all()
        reader = self._reader
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=2.0)
        self._reader = None
        self.command_channel_available = False
        self._applied_init = None

    # -- command channel -------------------------------------------------
    def _reader_loop(self) -> None:
        buf = bytearray()
        sock = self._cmd_sock
        while not self._reader_stop.is_set() and sock is not None:
            try:
                chunk = sock.recv(256)
            except TimeoutError:
                continue
            except OSError as exc:
                if not self._reader_stop.is_set():
                    self._reader_died(f"command socket error: {exc}")
                return
            if not chunk:
                if not self._reader_stop.is_set():
                    self._reader_died("the NPort closed the command connection")
                return
            buf += chunk
            try:
                frames, rest = aspp.split_frames(buf)
            except aspp.ProtocolError as exc:
                self._reader_died(str(exc))
                return
            buf = bytearray(rest)
            try:
                for resp in frames:
                    self._dispatch(resp)
            except Exception as exc:  # noqa: BLE001 - reader must die loudly, not silently
                if not self._reader_stop.is_set():
                    self._reader_died(f"command channel handler failed: {exc}")
                return

    def _reader_died(self, why: str) -> None:
        log.error("NPort command channel lost: %s", why)
        self._reader_error = why
        self.command_channel_available = False
        # Nobody answers POLLING any more, so the device will drop this
        # socket anyway; close it now so the failure is immediate and
        # visible rather than a silent stall. The data socket stays up so a
        # transfer in flight can still finish.
        with self._io_lock:
            sock, self._cmd_sock = self._cmd_sock, None
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        with self._lock:
            self.stats.errors.append(why)
        with self._reply_cv:
            self._reply_cv.notify_all()
        self._emit("transport.error", {"kind": self.kind, "message": why})

    def _dispatch(self, resp: aspp.Response) -> None:
        if resp.opcode == aspp.Cmd.POLLING:
            token = aspp.polling_token(resp)
            self._raw_cmd_write(aspp.encode_alive(token))
            self.polls_answered += 1
            return
        if resp.opcode == aspp.Cmd.NOTIFY:
            note = aspp.decode_notify(resp)
            if note.modem_changed:
                self._set_modem(note.modem)
            for err in note.errors:
                log.warning("NPort reports %s on the serial line", err)
                with self._lock:
                    self.line_errors.append(err)
                    self.stats.errors.append(err)
                self._emit("transport.line_error", {"kind": self.kind, "error": err})
            return
        with self._reply_cv:
            if self._stale_ops.get(resp.opcode, 0) > 0:
                # A reply to a request we already gave up waiting for. The
                # device answers in order, so this cannot be the answer to
                # the request currently pending - drop it first.
                self._stale_ops[resp.opcode] -= 1
                log.debug("Late ASPP reply 0x%02x dropped", resp.opcode)
            elif self._pending_op == resp.opcode:
                self._pending_resp = resp
                self._reply_cv.notify_all()
            else:
                log.warning("Unexpected ASPP reply 0x%02x %r", resp.opcode, resp.raw)

    def _raw_cmd_write(self, data: bytes) -> None:
        with self._cmd_write_lock:
            sock = self._cmd_sock
            if sock is None:
                raise TransportError("NPort command socket is closed.")
            try:
                sock.sendall(data)
            except OSError as exc:
                raise TransportError(f"Write to the NPort command port failed - {exc}") from exc

    def _command(self, request: bytes, timeout: float = COMMAND_TIMEOUT_S) -> aspp.Response:
        opcode = request[0]
        with self._cmd_lock:
            if self._reader_error:
                raise TransportError(f"NPort command channel lost: {self._reader_error}")
            with self._reply_cv:
                self._pending_op = opcode
                self._pending_resp = None
            self._raw_cmd_write(request)
            deadline = time.monotonic() + timeout
            with self._reply_cv:
                while self._pending_resp is None:
                    remaining = deadline - time.monotonic()
                    if self._reader_error:
                        self._pending_op = None
                        raise TransportError(f"NPort command channel lost: {self._reader_error}")
                    if remaining <= 0 or self._cmd_sock is None:
                        self._pending_op = None
                        self._stale_ops[opcode] = self._stale_ops.get(opcode, 0) + 1
                        raise TransportTimeout(
                            f"The NPort did not answer ASPP command 0x{opcode:02x} "
                            f"within {timeout:.1f}s."
                        )
                    self._reply_cv.wait(min(remaining, 0.2))
                resp = self._pending_resp
                self._pending_op = None
                self._pending_resp = None
        if aspp.RESPONSE_LENGTHS.get(opcode) == 3 and not resp.ok:
            raise TransportError(f"NPort rejected ASPP command 0x{opcode:02x}: {resp.raw!r}")
        return resp

    def _port_init(self, force: bool = False) -> None:
        line, flow = self._line, self._flow
        rtscts = flow.mode in ("rtscts", "both")
        sw = flow.software and self._device_flow
        key = (
            line.baud, line.data_bits, line.parity, line.stop_bits,
            self._dtr, self._rts, rtscts, sw,
        )
        if not force and key == self._applied_init:
            return
        if flow.mode in ("dtrdsr", "both") and not getattr(self, "_warned_dtrdsr", False):
            self._warned_dtrdsr = True
            log.warning(
                "DTR/DSR flow control is not controllable over ASPP; the NPort's own "
                "web setting applies. Wait-for-DSR still works via modem status."
            )
        resp = self._command(
            aspp.encode_port_init(
                line.baud, line.data_bits, line.parity, line.stop_bits,
                self._dtr, self._rts, rtscts, sw, sw,
            )
        )
        lines = aspp.decode_lines(resp)
        if lines is None:
            raise TransportError(f"The NPort rejected baud rate {line.baud}.")
        self._set_modem(lines)
        if aspp.baud_index(line.baud) == aspp.BAUD_CUSTOM:
            self._command(aspp.encode_setbaud(line.baud))
        self._applied_init = key
        log.info(
            "NPort line set: %d %d%s%s dtr=%d rts=%d rtscts=%d xonxoff=%d",
            line.baud, line.data_bits, line.parity[0].upper(), line.stop_bits,
            self._dtr, self._rts, rtscts, sw,
        )

    def _set_modem(self, lines: dict[str, bool]) -> None:
        old = self._modem
        new = ModemStatus(
            cts=bool(lines.get("cts", old.cts)),
            dsr=bool(lines.get("dsr", old.dsr)),
            dcd=bool(lines.get("dcd", old.dcd)),
            ri=bool(lines.get("ri", old.ri)),
            dtr=self._dtr,
            rts=self._rts,
        )
        with self._lock:
            self._modem = new
            self._modem_at = time.monotonic()
        if new != old:
            log.info("Modem lines: CTS=%d DSR=%d DCD=%d", new.cts, new.dsr, new.dcd)
            self._emit("transport.modem", {"kind": self.kind, **new.to_dict()})

    # -- data io ---------------------------------------------------------
    def _do_write(self, data: bytes) -> int:
        # Not under _io_lock while blocked: close()/purge() must be able to
        # take the lock and shut the socket down, which unblocks us.
        with self._io_lock:
            sock = self._sock
        if sock is None:
            raise TransportError("NPort data socket is closed.")
        view = memoryview(data)
        sent = 0
        while sent < len(view):
            if not self.is_open:
                raise TransportError("NPort connection closed while writing.")
            try:
                sock.settimeout(1.0)
                n = sock.send(view[sent:])
            except TimeoutError:
                continue  # device not draining; keep checking is_open
            except OSError as exc:
                raise TransportError(f"Write to the NPort failed - {exc}") from exc
            if n == 0:
                raise TransportError("The NPort closed the data connection.")
            sent += n
        return sent

    def _do_read(self, n: int, timeout: float) -> bytes:
        with self._io_lock:
            if self._rx_buf:
                out = bytes(self._rx_buf[:n])
                del self._rx_buf[: len(out)]
                return out
            sock = self._sock
            if sock is None:
                raise TransportError("NPort data socket is closed.")
            try:
                sock.settimeout(max(timeout, 0.0) or 0.0001)
                chunk = sock.recv(max(n, 1))
            except TimeoutError:
                return b""
            except BlockingIOError:
                return b""
            except OSError as exc:
                if not self.is_open:
                    raise TransportError("NPort connection closed.") from exc
                raise TransportError(f"Read from the NPort failed - {exc}") from exc
            if chunk == b"":
                raise TransportError("The NPort closed the data connection.")
            return chunk

    def _do_purge(self, rx: bool, tx: bool) -> None:
        which = aspp.FLUSH_ALL if (rx and tx) else (aspp.FLUSH_TX if tx else aspp.FLUSH_RX)
        try:
            self._command(aspp.encode_flush(which))
        except TransportError as exc:
            log.warning("NPort flush failed: %s", exc)
        if rx:
            with self._io_lock:
                self._rx_buf.clear()
                sock = self._sock
                if sock is not None:
                    deadline = time.monotonic() + 0.2
                    while time.monotonic() < deadline:
                        try:
                            sock.settimeout(0.01)
                            if not sock.recv(4096):
                                break
                        except (TimeoutError, OSError):
                            break

    # -- line control ----------------------------------------------------
    def _do_set_line_params(self, params: LineParams) -> None:
        if self.is_open or self._cmd_sock is not None:
            self._port_init()

    def _do_set_flow_control(self, flow: FlowControl) -> None:
        if self._cmd_sock is None:
            return
        self._port_init()
        if flow.software:
            self._command(aspp.encode_xonxoff(flow.xon, flow.xoff))

    def _do_set_dtr(self, state: bool) -> None:
        self._dtr = bool(state)
        self._linectrl()

    def _do_set_rts(self, state: bool) -> None:
        self._rts = bool(state)
        self._linectrl()

    def _linectrl(self) -> None:
        if self._cmd_sock is None:
            return
        if self._applied_init and self._applied_init[4:6] == (self._dtr, self._rts):
            return
        self._command(aspp.encode_linectrl(self._dtr, self._rts))
        if self._applied_init:
            k = list(self._applied_init)
            k[4], k[5] = self._dtr, self._rts
            self._applied_init = tuple(k)
        self._set_modem({})  # refresh the DTR/RTS view

    def _do_get_modem_status(self) -> ModemStatus:
        with self._lock:
            fresh = (time.monotonic() - self._modem_at) < MODEM_CACHE_S
            cached = self._modem
        if fresh or not self.command_channel_available:
            return cached
        try:
            resp = self._command(aspp.encode_lstatus(), timeout=1.5)
            lines = aspp.decode_lines(resp)
            if lines is not None:
                self._set_modem(lines)
        except TransportError as exc:
            log.debug("LSTATUS failed, using cached modem status: %s", exc)
        with self._lock:
            return self._modem

    # -- queue / drain ---------------------------------------------------
    def pending_tx(self) -> int | None:
        """Bytes still queued inside the NPort for transmission."""
        if not self.command_channel_available:
            return None
        try:
            return aspp.decode_queue(self._command(aspp.encode_oqueue(), timeout=1.5))
        except TransportError as exc:
            log.debug("OQUEUE failed: %s", exc)
            return None

    def drain(self, timeout: float = 60.0, should_abort: Callable[[], bool] | None = None) -> bool:
        """Block until the NPort's TX queue is empty. False on timeout/abort.

        Polls ``OQUEUE`` (answers immediately) rather than ``WAIT_OQUEUE``,
        whose device-side blocking and timeout units are unverified and
        whose late replies would desynchronise the command stream.
        """
        if not self.command_channel_available:
            return True
        deadline = time.monotonic() + timeout
        t0 = time.monotonic()
        last_logged = -1
        polls = 0
        while time.monotonic() < deadline:
            if should_abort is not None and should_abort():
                return False
            try:
                pending = aspp.decode_queue(self._command(aspp.encode_oqueue(), timeout=1.5))
                polls += 1
                if pending != last_logged:
                    log.debug("Device TX queue: %d bytes", pending)
                    last_logged = pending
                if pending == 0:
                    if polls > 2:
                        log.info("Device TX queue drained in %.1fs (%d polls)", time.monotonic() - t0, polls)
                    return True
            except TransportTimeout:
                log.debug("OQUEUE poll timed out while draining")
            except TransportError as exc:
                log.warning("OQUEUE failed while draining: %s", exc)
                return True  # cannot know; do not hang the job
            time.sleep(0.1)
        return False

    def send_break(self, duration_s: float = 0.25) -> None:
        self._command(aspp.encode_simple(aspp.Cmd.START_BREAK))
        time.sleep(duration_s)
        self._command(aspp.encode_simple(aspp.Cmd.STOP_BREAK))

    # -- capability reporting --------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        ok = self.command_channel_available
        return {
            "data_channel": self._sock is not None,
            "command_channel": ok,
            "can_set_line_params": ok,
            "can_read_modem_status": ok,
            "can_set_dtr_rts": ok,
            "can_drain": ok,
            "aspp": aspp.describe_support(),
        }
