"""NPort ASPP simulator - a fake Moxa NPort for tests and bench work.

Implements the device side of Moxa's ASPP command channel as reconstructed
from the GPL ``npreal2`` driver sources, plus a transparent data channel
with a small
serial-line model so the send/receive engines can be exercised with
realistic timing, XON/XOFF, CTS handshaking and the keep-alive dance.

Per emulated serial port ``n`` (0-based) the simulator listens on two TCP
sockets: a *command* port (966+n by default) and a *data* port (950+n by
default). Tests pass ``0`` for either base port to get ephemeral ports and
read the bound values back from :attr:`NPortSimulator.ports`.

Run standalone::

    python tools/nport_sim.py --host 127.0.0.1 --cmd-base 9660 --data-base 9500

Only the standard library is used so the same file can be imported from
Fusion's embedded Python if we ever want an in-app loopback machine.
"""

from __future__ import annotations

import argparse
import collections
import logging
import socket
import struct
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field

log = logging.getLogger("nport_sim")

# --- ASPP opcodes (verbatim from npreal2d.h) --------------------------------
CMD_IOCTL = 16
CMD_FLOWCTRL = 17
CMD_LINECTRL = 18
CMD_LSTATUS = 19
CMD_FLUSH = 20
CMD_IQUEUE = 21
CMD_OQUEUE = 22
CMD_SETBAUD = 23
CMD_XONXOFF = 24
CMD_PORT_RESET = 32
CMD_START_BREAK = 33
CMD_STOP_BREAK = 34
CMD_START_NOTIFY = 36
CMD_STOP_NOTIFY = 37
CMD_NOTIFY = 0x26
CMD_POLLING = 0x27
CMD_ALIVE = 0x28
CMD_HOST = 43
CMD_PORT_INIT = 44
CMD_RESENT_TIME = 46
CMD_WAIT_OQUEUE = 47
CMD_TX_FIFO = 48
CMD_SETXON = 51
CMD_SETXOFF = 52

NOTIFY_PARITY = 0x01
NOTIFY_FRAMING = 0x02
NOTIFY_HW_OVERRUN = 0x04
NOTIFY_SW_OVERRUN = 0x08
NOTIFY_BREAK = 0x10
NOTIFY_MSR_CHG = 0x20

MSR_CTS = 0x10
MSR_DSR = 0x20
MSR_RI = 0x40
MSR_DCD = 0x80

BAUD_BY_INDEX = {
    0: 300, 1: 600, 2: 1200, 3: 2400, 4: 4800, 5: 7200, 6: 9600, 7: 19200,
    8: 38400, 9: 57600, 10: 115200, 11: 230400, 12: 460800, 13: 921600,
    14: 150, 15: 134, 16: 110, 17: 75, 18: 50,
}

OK_COMMANDS = {
    CMD_IOCTL, CMD_FLOWCTRL, CMD_LINECTRL, CMD_FLUSH, CMD_SETBAUD, CMD_XONXOFF,
    CMD_START_BREAK, CMD_STOP_BREAK, CMD_START_NOTIFY, CMD_STOP_NOTIFY, CMD_HOST,
    CMD_TX_FIFO, CMD_SETXON, CMD_SETXOFF,
}


@dataclass
class PortSettings:
    baud: int = 38400
    data_bits: int = 8
    stop_bits: int = 1
    parity: str = "N"
    dtr: bool = True
    rts: bool = True
    hw_flow_a: bool = False
    hw_flow_b: bool = False
    xon_enabled: bool = False
    xoff_enabled: bool = False
    xon_char: int = 0x11
    xoff_char: int = 0x13
    tx_fifo: int = 16
    notify_enabled: bool = True
    flowctrl_mask: int | None = None  # from CMD_FLOWCTRL if the host uses it

    def mode_byte(self) -> int:
        bits = {5: 0, 6: 1, 7: 2, 8: 3}[self.data_bits]
        stop = 4 if self.stop_bits == 2 else 0
        par = {"N": 0, "E": 8, "O": 16, "M": 24, "S": 32}[self.parity]
        return bits | stop | par

    @staticmethod
    def decode_mode(mode: int) -> tuple[int, int, str]:
        bits = {0: 5, 1: 6, 2: 7, 3: 8}[mode & 0x03]
        stop = 2 if mode & 0x04 else 1
        par = {0: "N", 8: "E", 16: "O", 24: "M", 32: "S"}.get(mode & 0x38, "N")
        return bits, stop, par


@dataclass
class SimCNC:
    """Behaviour of the machine hanging off the emulated serial port.

    Everything is optional; defaults describe a control that is always
    ready and never talks back. Tests tweak these fields to model a
    Fanuc-style XON/XOFF reader, a CTS-handshake control, or a control
    that punches out a program.
    """

    #: Lines we (the DCE side, i.e. the CNC) drive towards the host.
    cts: bool = True
    dsr: bool = True
    dcd: bool = False
    ri: bool = False
    #: Software flow control: send XOFF when our receive buffer exceeds
    #: ``xoff_at`` bytes and XON once it drains below ``xon_at``.
    software_flow: bool = False
    xoff_at: int = 512
    xon_at: int = 128
    #: Bytes/sec at which the CNC "consumes" received data (0 = instant).
    consume_rate: float = 0.0
    #: Hardware flow: drop CTS instead of sending XOFF when buffer is full.
    hardware_flow: bool = False
    #: Optional callback ``(port, data) -> None`` for every byte batch
    #: the CNC receives.
    on_receive: Callable[[SimPort, bytes], None] | None = None
    #: Send an XON when the host first connects the data socket (some
    #: controls do this when the operator presses INPUT before the PC).
    xon_on_connect: bool = False


@dataclass
class SimPort:
    """One emulated serial port: state, sockets, serial line model."""

    index: int
    sim: NPortSimulator
    settings: PortSettings = field(default_factory=PortSettings)
    cnc: SimCNC = field(default_factory=SimCNC)
    #: What the CNC has received from the host, in order.
    received: bytearray = field(default_factory=bytearray)
    #: Serial TX queue: bytes accepted from the host data socket that are
    #: still "on the wire" (drained at the configured baud rate).
    tx_queue: collections.deque = field(default_factory=collections.deque)
    #: Bytes the CNC has sent that the host has not read yet (RX queue).
    rx_queue: bytearray = field(default_factory=bytearray)
    cmd_conn: socket.socket | None = None
    data_conn: socket.socket | None = None
    cmd_connections: int = 0
    data_connections: int = 0
    lock: threading.RLock = field(default_factory=threading.RLock)
    #: Commands seen on the command channel, for assertions.
    command_log: list[tuple[int, bytes]] = field(default_factory=list)
    alive_missed: int = 0
    last_alive: float = 0.0
    cnc_buffer_level: int = 0
    xoff_sent: bool = False
    port_init_count: int = 0
    _last_cnc_drain: float = 0.0
    _last_consume: float = 0.0

    # -- test-facing controls -------------------------------------------
    def set_modem(self, **lines: bool) -> None:
        """Change CTS/DSR/DCD/RI as driven by the CNC; emits NOTIFY."""
        with self.lock:
            changed = False
            for name, value in lines.items():
                if getattr(self.cnc, name) != value:
                    setattr(self.cnc, name, value)
                    changed = True
        if changed:
            self._notify(NOTIFY_MSR_CHG)

    def cnc_send(self, data: bytes) -> None:
        """The CNC transmits *data* towards the host."""
        with self.lock:
            self.rx_queue += data
        self._pump_rx()

    def modem_status_byte(self) -> int:
        msr = 0
        if self.cnc.cts:
            msr |= MSR_CTS
        if self.cnc.dsr:
            msr |= MSR_DSR
        if self.cnc.dcd:
            msr |= MSR_DCD
        if self.cnc.ri:
            msr |= MSR_RI
        return msr

    def raise_line_error(self, flags: int) -> None:
        """Inject parity/framing/overrun/break via NOTIFY."""
        self._notify(flags)

    # -- ASPP command channel --------------------------------------------
    def handle_command(self, op: int, payload: bytes) -> bytes | None:
        s = self.settings
        with self.lock:
            self.command_log.append((op, payload))
        if op == CMD_PORT_INIT:
            if len(payload) < 8:
                return None
            self.port_init_count += 1
            baud_idx, mode, dtr, rts, fa, fb, xon, xoff = payload[:8]
            if baud_idx != 0xFF:
                if baud_idx not in BAUD_BY_INDEX:
                    return bytes([CMD_PORT_INIT, 3, 0xFF, 0xFF, 0xFF])
                s.baud = BAUD_BY_INDEX[baud_idx]
            s.data_bits, s.stop_bits, s.parity = PortSettings.decode_mode(mode)
            s.dtr, s.rts = bool(dtr), bool(rts)
            s.hw_flow_a, s.hw_flow_b = bool(fa), bool(fb)
            s.xon_enabled, s.xoff_enabled = bool(xon), bool(xoff)
            return bytes([CMD_PORT_INIT, 3, int(self.cnc.dsr), int(self.cnc.cts), int(self.cnc.dcd)])
        if op == CMD_LSTATUS:
            return bytes([CMD_LSTATUS, 3, int(self.cnc.dsr), int(self.cnc.cts), int(self.cnc.dcd)])
        if op == CMD_LINECTRL:
            if len(payload) >= 2:
                s.dtr, s.rts = bool(payload[0]), bool(payload[1])
            return bytes([op]) + b"OK"
        if op == CMD_SETBAUD:
            if len(payload) >= 4:
                s.baud = struct.unpack("<i", payload[:4])[0]
            return bytes([op]) + b"OK"
        if op == CMD_XONXOFF:
            if len(payload) >= 2:
                s.xon_char, s.xoff_char = payload[0], payload[1]
            return bytes([op]) + b"OK"
        if op == CMD_TX_FIFO:
            if payload:
                s.tx_fifo = payload[0]
            return bytes([op]) + b"OK"
        if op == CMD_FLUSH:
            which = payload[0] if payload else 2
            with self.lock:
                if which in (0, 2):
                    self.rx_queue.clear()
                if which in (1, 2):
                    self.tx_queue.clear()
            return bytes([op]) + b"OK"
        if op == CMD_START_NOTIFY:
            s.notify_enabled = True
            return bytes([op]) + b"OK"
        if op == CMD_STOP_NOTIFY:
            s.notify_enabled = False
            return bytes([op]) + b"OK"
        if op == CMD_FLOWCTRL:
            if payload:
                s.flowctrl_mask = payload[0]
            return bytes([op]) + b"OK"
        if op == CMD_IOCTL:
            if len(payload) >= 2:
                if payload[0] in BAUD_BY_INDEX:
                    s.baud = BAUD_BY_INDEX[payload[0]]
                s.data_bits, s.stop_bits, s.parity = PortSettings.decode_mode(payload[1])
            return bytes([op]) + b"OK"
        if op in (CMD_OQUEUE, CMD_IQUEUE):
            with self.lock:
                n = len(self.tx_queue) if op == CMD_OQUEUE else len(self.rx_queue)
            n = min(n, 0xFFFF)
            return bytes([op, 2, n & 0xFF, n >> 8])
        if op == CMD_WAIT_OQUEUE:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with self.lock:
                    n = len(self.tx_queue)
                if n == 0:
                    break
                time.sleep(0.005)
            n = min(n, 0xFFFF)
            return bytes([op, 2, n & 0xFF, n >> 8])
        if op == CMD_ALIVE:
            with self.lock:
                self.last_alive = time.monotonic()
                self.alive_missed = 0
            return None
        if op in OK_COMMANDS:
            return bytes([op]) + b"OK"
        log.warning("port %d: unknown ASPP opcode %d", self.index, op)
        return None

    def _notify(self, flags: int) -> None:
        if not self.settings.notify_enabled:
            return
        conn = self.cmd_conn
        if conn is None:
            return
        frame = bytes([CMD_NOTIFY, flags, self.modem_status_byte(), 0])
        try:
            conn.sendall(frame)
        except OSError:
            pass

    def send_polling(self, token: int) -> None:
        conn = self.cmd_conn
        if conn is None:
            return
        try:
            conn.sendall(bytes([CMD_POLLING, 1, token & 0xFF]))
        except OSError:
            pass

    # -- serial line model ----------------------------------------------
    def host_wrote(self, data: bytes) -> None:
        """Bytes arrived on the data socket from the host."""
        with self.lock:
            self.tx_queue.extend(data)

    def drain(self, now: float) -> None:
        """Move bytes from the TX queue into the CNC at baud speed."""
        s = self.settings
        self._consume(now)
        with self.lock:
            if not self.tx_queue:
                self._last_cnc_drain = now
                return
            if self.sim.instant:
                budget = len(self.tx_queue)
            else:
                bits_per_byte = 1 + s.data_bits + s.stop_bits + (0 if s.parity == "N" else 1)
                elapsed = max(now - self._last_cnc_drain, 0.0)
                budget = int(elapsed * s.baud / bits_per_byte)
                if budget <= 0:
                    return
            self._last_cnc_drain = now
            # Hardware flow: a CNC with CTS low does not accept bytes at all.
            # (The NPort keeps them queued when RTS/CTS flow control is on.)
            if (s.hw_flow_a or s.hw_flow_b) and not self.cnc.cts:
                return
            # Software flow: NPort stops transmitting after it saw XOFF.
            if s.xon_enabled and self.xoff_sent:
                return
            chunk = bytes(self.tx_queue.popleft() for _ in range(min(budget, len(self.tx_queue))))
            self.received += chunk
            self.cnc_buffer_level += len(chunk)
            cb = self.cnc.on_receive
        if cb is not None:
            cb(self, chunk)
        self._flow_check()

    def _consume(self, now: float) -> None:
        with self.lock:
            if self.cnc.consume_rate <= 0:
                self.cnc_buffer_level = 0
                self._last_consume = now
            else:
                elapsed = max(now - self._last_consume, 0.0)
                eaten = int(elapsed * self.cnc.consume_rate)
                if eaten > 0:
                    self.cnc_buffer_level = max(0, self.cnc_buffer_level - eaten)
                    self._last_consume = now
        self._flow_check()

    def _flow_check(self) -> None:
        cnc = self.cnc
        with self.lock:
            level = self.cnc_buffer_level
            if cnc.software_flow:
                if not self.xoff_sent and level >= cnc.xoff_at:
                    self.xoff_sent = True
                    self.rx_queue.append(self.settings.xoff_char)
                elif self.xoff_sent and level <= cnc.xon_at:
                    self.xoff_sent = False
                    self.rx_queue.append(self.settings.xon_char)
            if cnc.hardware_flow:
                # Hysteresis: drop CTS at xoff_at, raise it again at xon_at.
                want_cts = level < cnc.xoff_at if cnc.cts else level <= cnc.xon_at
                if want_cts != cnc.cts:
                    cnc.cts = want_cts
                    changed = True
                else:
                    changed = False
            else:
                changed = False
        if changed:
            self._notify(NOTIFY_MSR_CHG)
        self._pump_rx()

    def _pump_rx(self) -> None:
        with self.lock:
            if not self.rx_queue:
                return
            conn = self.data_conn
            data = bytes(self.rx_queue)
            self.rx_queue.clear()
        if conn is None:
            # Nobody listening: the NPort would buffer; keep it for later.
            with self.lock:
                self.rx_queue[:0] = data
            return
        try:
            conn.sendall(data)
        except OSError:
            pass


class NPortSimulator:
    """Threaded fake NPort. Use as a context manager or call start/stop."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        cmd_base: int = 0,
        data_base: int = 0,
        ports: int = 2,
        polling_interval: float = 2.0,
        alive_timeout: float = 6.0,
        max_connections: int = 1,
        instant: bool = False,
        strict_aspp: bool = False,
    ) -> None:
        self.host = host
        self.cmd_base = cmd_base
        self.data_base = data_base
        self.n_ports = ports
        self.polling_interval = polling_interval
        self.alive_timeout = alive_timeout
        self.max_connections = max_connections
        #: When True the serial model ignores baud and drains instantly.
        self.instant = instant
        #: Model the two connection rules a real NPort W2250A enforces
        #: (verified 2026-09-16), which the permissive default does not:
        #:
        #: * the command socket is reset unless the FIRST frame on it is
        #:   ``PORT_INIT``;
        #: * that ``PORT_INIT`` is not answered until the matching data
        #:   socket is connected too.
        #:
        #: Off by default so the existing engine tests keep their simple
        #: command-only fixtures; the discovery probe tests turn it on.
        self.strict_aspp = strict_aspp
        self.ports: list[SimPort] = [SimPort(i, self) for i in range(ports)]
        self.cmd_ports: list[int] = []
        self.data_ports: list[int] = []
        self._listeners: list[socket.socket] = []
        self._threads: list[threading.Thread] = []
        self._stop = threading.Event()
        self._poll_token = 0

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> NPortSimulator:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        for port in self.ports:
            cmd_l = self._listen(self.cmd_base + port.index if self.cmd_base else 0)
            data_l = self._listen(self.data_base + port.index if self.data_base else 0)
            self.cmd_ports.append(cmd_l.getsockname()[1])
            self.data_ports.append(data_l.getsockname()[1])
            self._spawn(self._accept_loop, cmd_l, port, True)
            self._spawn(self._accept_loop, data_l, port, False)
        self._spawn(self._tick_loop)
        log.info("NPort simulator up: cmd=%s data=%s", self.cmd_ports, self.data_ports)

    def stop(self) -> None:
        self._stop.set()
        for lst in self._listeners:
            try:
                lst.close()
            except OSError:
                pass
        for port in self.ports:
            for conn in (port.cmd_conn, port.data_conn):
                if conn is not None:
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    try:
                        conn.close()
                    except OSError:
                        pass
        for t in self._threads:
            t.join(timeout=2.0)

    def _listen(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((self.host, port))
        s.listen(4)
        s.settimeout(0.2)
        self._listeners.append(s)
        return s

    def _spawn(self, target: Callable, *args: object) -> None:
        t = threading.Thread(target=target, args=args, daemon=True)
        t.start()
        self._threads.append(t)

    # -- loops -----------------------------------------------------------
    def _accept_loop(self, listener: socket.socket, port: SimPort, is_cmd: bool) -> None:
        while not self._stop.is_set():
            try:
                conn, _addr = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            with port.lock:
                if is_cmd:
                    if port.cmd_connections >= self.max_connections:
                        conn.close()
                        continue
                    port.cmd_connections += 1
                    port.cmd_conn = conn
                    port.last_alive = time.monotonic()
                    port.alive_missed = 0
                else:
                    if port.data_connections >= self.max_connections:
                        conn.close()
                        continue
                    port.data_connections += 1
                    port.data_conn = conn
            if is_cmd:
                self._spawn(self._cmd_loop, conn, port)
            else:
                if port.cnc.xon_on_connect:
                    port.cnc_send(bytes([port.settings.xon_char]))
                port._pump_rx()
                self._spawn(self._data_loop, conn, port)

    def _cmd_loop(self, conn: socket.socket, port: SimPort) -> None:
        conn.settimeout(0.2)
        buf = bytearray()
        first_frame = True
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(4096)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= 2:
                    op, length = buf[0], buf[1]
                    if len(buf) < 2 + length:
                        break
                    payload = bytes(buf[2 : 2 + length])
                    del buf[: 2 + length]
                    if self.strict_aspp and first_frame and op != CMD_PORT_INIT:
                        log.warning(
                            "port %d: first command was 0x%02x, not PORT_INIT - dropping",
                            port.index, op,
                        )
                        return
                    if self.strict_aspp and op == CMD_PORT_INIT and not self._await_data(port):
                        log.warning(
                            "port %d: PORT_INIT with no data socket - dropping", port.index
                        )
                        return
                    first_frame = False
                    resp = port.handle_command(op, payload)
                    if resp is not None:
                        try:
                            conn.sendall(resp)
                        except OSError:
                            break
        finally:
            with port.lock:
                if port.cmd_conn is conn:
                    port.cmd_conn = None
                port.cmd_connections = max(0, port.cmd_connections - 1)
            try:
                conn.close()
            except OSError:
                pass

    def _await_data(self, port: SimPort, timeout: float = 1.5) -> bool:
        """Wait for the port's data socket, as the real NPort does."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if port.data_conn is not None:
                return True
            if self._stop.is_set():
                return False
            time.sleep(0.01)
        return port.data_conn is not None

    def _data_loop(self, conn: socket.socket, port: SimPort) -> None:
        conn.settimeout(0.2)
        try:
            while not self._stop.is_set():
                try:
                    chunk = conn.recv(65536)
                except TimeoutError:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                port.host_wrote(chunk)
        finally:
            with port.lock:
                if port.data_conn is conn:
                    port.data_conn = None
                port.data_connections = max(0, port.data_connections - 1)
            try:
                conn.close()
            except OSError:
                pass

    def _tick_loop(self) -> None:
        next_poll = time.monotonic() + self.polling_interval
        while not self._stop.is_set():
            now = time.monotonic()
            for port in self.ports:
                port.drain(now)
                if (
                    port.cmd_conn is not None
                    and self.alive_timeout > 0
                    and now - port.last_alive > self.alive_timeout
                ):
                    log.warning("port %d: host missed keep-alive, dropping", port.index)
                    conn = port.cmd_conn
                    try:
                        conn.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass
                    with port.lock:
                        port.last_alive = now
            if now >= next_poll:
                self._poll_token = (self._poll_token + 1) & 0xFF
                for port in self.ports:
                    port.send_polling(self._poll_token)
                next_poll = now + self.polling_interval
            time.sleep(0.01)


# ---------------------------------------------------------------------------
# UDP discovery (NPort Administrator's "search", port 4800)
# ---------------------------------------------------------------------------
DISCOVERY_PORT = 4800
DISC_SEARCH = 0x01
DISC_NAME = 0x10
DISC_INFO = 0x16


class DiscoveryResponder:
    """A fake NPort answering Moxa's UDP search protocol.

    Byte-for-byte what a W2250A produced on 2026-09-16 (see
    ``moxaserial/discovery.py`` for the field-by-field derivation), with
    the identity fields parameterised so a test can stand up several
    "devices" at once. Binds an ephemeral port when *port* is 0 and
    publishes it as :attr:`port`, so tests never need UDP 4800 or root.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 0,
        ip: str = "127.0.0.1",
        mac: str = "40:2c:f4:fd:49:33",
        name: str = "KIA_Lathe",
        model_id: int = 0x2452,
        product_line: int = 0x2450,
        firmware: tuple[int, int] = (2, 2),
        serial_number: int = 9645,
        ports: int = 2,
        answer: Callable[[int], bool] | None = None,
    ) -> None:
        self.host = host
        self.ip = ip
        self.mac = bytes(int(b, 16) for b in mac.split(":"))
        self.name = name
        self.model_id = model_id
        self.product_line = product_line
        self.firmware = firmware
        self.serial_number = serial_number
        self.n_ports = ports
        #: ``answer(opcode) -> bool``: return False to make the device look
        #: like older firmware that does not implement that opcode.
        self.answer = answer or (lambda _op: True)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind((host, port))
        self._sock.settimeout(0.2)
        self.port: int = self._sock.getsockname()[1]
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -------------------------------------------------------
    def __enter__(self) -> DiscoveryResponder:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="nport-disc", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    # -- wire ------------------------------------------------------------
    @property
    def device_id(self) -> bytes:
        return struct.pack("<IH", (0x8000 << 16) | self.product_line, self.model_id) + self.mac

    def _reply(self, opcode: int, sequence: int, payload: bytes) -> bytes:
        body = self.device_id + payload
        return struct.pack("!BBHI", opcode | 0x80, 0, 8 + len(body), sequence) + body

    def _error(self, opcode: int, sequence: int) -> bytes:
        return struct.pack("!BBHI", opcode | 0x80, 4, 8 + 12, sequence) + self.device_id

    def build_reply(self, request: bytes) -> bytes | None:
        if len(request) < 8:
            return None
        opcode, _flags, _length, sequence = struct.unpack("!BBHI", request[:8])
        if opcode & 0x80:
            return None
        if not self.answer(opcode):
            return self._error(opcode, sequence)
        if opcode == DISC_SEARCH:
            return self._reply(opcode, sequence, socket.inet_aton(self.ip))
        if opcode == DISC_NAME:
            return self._reply(opcode, sequence, self.name.encode("latin-1").ljust(40, b"\0"))
        if opcode == DISC_INFO:
            major, minor = self.firmware
            payload = (
                struct.pack("<I", (major << 24) | (minor << 16))
                + b"\x00\x00\x03\x01"
                + struct.pack("<I", self.serial_number)
                + bytes([0x18, 0x00, 0x00, self.n_ports])
            )
            return self._reply(opcode, sequence, payload)
        return self._error(opcode, sequence)

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                data, addr = self._sock.recvfrom(2048)
            except TimeoutError:
                continue
            except OSError:
                return
            reply = self.build_reply(data)
            if reply is None:
                continue
            try:
                self._sock.sendto(reply, addr)
            except OSError:
                return


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--cmd-base", type=int, default=966)
    ap.add_argument("--data-base", type=int, default=950)
    ap.add_argument("--ports", type=int, default=2)
    ap.add_argument("--xonxoff", action="store_true", help="CNC uses XON/XOFF flow control")
    ap.add_argument("--rtscts", action="store_true", help="CNC drops CTS when its buffer is full")
    ap.add_argument("--consume-rate", type=float, default=200.0, help="CNC bytes/sec consumption")
    ap.add_argument("--instant", action="store_true", help="ignore baud timing")
    ap.add_argument(
        "--inbox",
        default="",
        help="folder to watch: any file dropped there is 'punched out' by the CNC on port 1, then deleted",
    )
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(asctime)s %(message)s")
    sim = NPortSimulator(args.host, args.cmd_base, args.data_base, args.ports, instant=args.instant)
    for port in sim.ports:
        port.cnc.software_flow = args.xonxoff
        port.cnc.hardware_flow = args.rtscts
        port.cnc.consume_rate = args.consume_rate

        def echo(p: SimPort, data: bytes) -> None:
            log.debug("port %d CNC got %d bytes: %r", p.index, len(data), data[:60])

        port.cnc.on_receive = echo
    sim.start()
    print(f"NPort simulator listening on {args.host}: cmd ports {sim.cmd_ports}, data ports {sim.data_ports}")
    inbox = args.inbox
    if inbox:
        import os

        os.makedirs(inbox, exist_ok=True)
        print(f"inbox: drop a file into {inbox} to have the CNC send it")
    try:
        while True:
            time.sleep(0.5)
            if not inbox:
                continue
            for name in sorted(os.listdir(inbox)):
                fp = os.path.join(inbox, name)
                if not os.path.isfile(fp) or name.startswith("."):
                    continue
                with open(fp, "rb") as fh:
                    data = fh.read()
                os.remove(fp)
                log.info("inbox: CNC punching out %s (%d bytes)", name, len(data))
                sim.ports[0].cnc_send(data)
    except KeyboardInterrupt:
        sim.stop()


if __name__ == "__main__":
    main()
