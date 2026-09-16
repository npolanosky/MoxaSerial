"""FakeTransport - a simulated CNC control on the other end of the wire.

This is what makes the add-in developable and testable with no Moxa and
no machine: it behaves like a slow serial device with a finite receive
buffer, optional XON/XOFF throttling, toggling modem lines, and a
scripted "operator punches out a program" mode for testing receive.

It is used by:
  * the unit tests,
  * the ``Simulator`` machine that ships in the default settings,
  * ``tools/dev_server.py`` so the whole palette UI can be driven in a
    normal browser.

Nothing here talks to a socket.
"""

from __future__ import annotations

import random
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from moxaserial.events import EventBus
from moxaserial.log import get_logger
from moxaserial.transport.base import (
    FlowControl,
    LineParams,
    ModemStatus,
    Transport,
    TransportError,
)

log = get_logger("fake")


@dataclass
class FakeProfile:
    """Everything about how the simulated control misbehaves."""

    #: Emulate the wire: bytes really do take baud/10 seconds each.
    realtime: bool = False
    #: Multiplier on emulated wire time (0.05 = 20x faster than real).
    time_scale: float = 1.0
    #: Size of the control's input buffer, in bytes.
    buffer_size: int = 512
    #: Send XOFF when the buffer passes this fraction, XON when it drains.
    xoff_high_water: float = 0.8
    xon_low_water: float = 0.3
    #: Drain rate in bytes/sec when not in realtime mode.
    drain_bytes_per_s: float = 20_000.0
    #: Modem lines the control asserts once "ready".
    cts: bool = True
    dsr: bool = True
    dcd: bool = True
    ri: bool = False
    #: Seconds after open before CTS/DSR come up (tests wait-for-ready).
    ready_delay_s: float = 0.0
    #: Seconds after open before the control sends its first XON.
    xon_delay_s: float = 0.0
    #: Emit XON once at the delay above even when flow control is off.
    send_initial_xon: bool = False
    #: Random extra delay per chunk, seconds, uniform in [0, jitter].
    jitter_s: float = 0.0
    #: Bytes the control echoes back for every byte received (0 = silent).
    echo: bool = False
    #: Fail the next open() with this message.
    fail_on_open: str = ""
    #: Fail a write once this many bytes have been written.
    fail_after_bytes: int = 0
    fail_message: str = "Simulated wire fault"
    #: Drop the connection (close) after this many bytes.
    drop_after_bytes: int = 0
    #: Program the control "punches out" when receive mode is armed.
    outgoing: bytes = b""
    #: Delay before the control starts punching out, seconds.
    outgoing_delay_s: float = 0.3
    #: Chunk size and inter-chunk gap for the punched-out program.
    outgoing_chunk: int = 64
    outgoing_gap_s: float = 0.01
    #: Random seed for reproducible jitter in tests.
    seed: int | None = 1234

    extra: dict[str, Any] = field(default_factory=dict)


class FakeTransport(Transport):
    """In-process simulated control. See :class:`FakeProfile`."""

    kind = "simulator"

    def __init__(self, bus: EventBus | None = None, profile: FakeProfile | None = None) -> None:
        super().__init__(bus=bus)
        self.profile = profile or FakeProfile()
        self._rng = random.Random(self.profile.seed)
        self._rx: deque[int] = deque()          # bytes waiting for *us* to read
        self._buffer_level = 0.0                # the control's input buffer
        self._last_drain = 0.0
        self._xoff_sent = False
        self._opened_at = 0.0
        self._cv = threading.Condition(threading.RLock())
        self._out_thread: threading.Thread | None = None
        self._out_stop = threading.Event()
        #: Everything we were asked to write, for test assertions.
        self.written = bytearray()
        #: Log of set_dtr / set_rts / set_line_params calls, for tests.
        self.control_log: list[tuple[str, Any]] = []

    # -- lifecycle -------------------------------------------------------
    def _do_open(self, machine: dict[str, Any]) -> None:
        if self.profile.fail_on_open:
            raise TransportError(self.profile.fail_on_open)
        self._opened_at = time.monotonic()
        self._last_drain = self._opened_at
        self._buffer_level = 0.0
        self._xoff_sent = False
        self._rx.clear()
        self.written.clear()
        self._out_stop.clear()
        log.debug("FakeTransport open for %s", machine.get("name", "?"))
        if self.profile.send_initial_xon or self.profile.xon_delay_s > 0:
            threading.Timer(
                self._scaled(self.profile.xon_delay_s), self._inject_initial_xon
            ).start()

    def _do_close(self) -> None:
        self._out_stop.set()
        t = self._out_thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout=1.0)
        self._out_thread = None
        with self._cv:
            self._cv.notify_all()

    # -- io --------------------------------------------------------------
    def _do_write(self, data: bytes) -> int:
        p = self.profile
        if not data:
            return 0
        if p.fail_after_bytes and len(self.written) + len(data) > p.fail_after_bytes:
            raise TransportError(p.fail_message)

        self._drain()
        self.written.extend(data)

        if p.realtime:
            # 1 start + data + parity + stop bits ~= 10 bit-times per byte.
            bit_time = 10.0 / max(self.line_params.baud, 50)
            time.sleep(self._scaled(bit_time * len(data)))
        if p.jitter_s:
            time.sleep(self._scaled(self._rng.uniform(0.0, p.jitter_s)))

        self._buffer_level += len(data)
        self._maybe_flow_control()

        if p.echo:
            self._push_rx(data)

        if p.drop_after_bytes and len(self.written) >= p.drop_after_bytes:
            log.warning("FakeTransport dropping the connection after %d bytes", len(self.written))
            self.close()
        return len(data)

    def _do_read(self, n: int, timeout: float) -> bytes:
        # The control keeps consuming its input buffer whether or not we are
        # writing, so drain on every poll too. Without this, a sender that is
        # blocked holding XOFF would never see the matching XON - the buffer
        # would only ever drain from inside _do_write, which cannot run.
        self._drain()
        self._maybe_flow_control()

        deadline = time.monotonic() + max(timeout, 0.0)
        with self._cv:
            while not self._rx and self.is_open:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return b""
                self._cv.wait(min(remaining, 0.05))
            out = bytearray()
            while self._rx and len(out) < n:
                out.append(self._rx.popleft())
            return bytes(out)

    def _do_purge(self, rx: bool, tx: bool) -> None:
        with self._cv:
            if rx:
                self._rx.clear()
            if tx:
                self._buffer_level = 0.0

    # -- line control ----------------------------------------------------
    def _do_set_line_params(self, params: LineParams) -> None:
        self.control_log.append(("line", params.to_dict()))

    def _do_set_flow_control(self, flow: FlowControl) -> None:
        self.control_log.append(("flow", flow.to_dict()))

    def _do_set_dtr(self, state: bool) -> None:
        self.control_log.append(("dtr", state))

    def _do_set_rts(self, state: bool) -> None:
        self.control_log.append(("rts", state))

    def _do_get_modem_status(self) -> ModemStatus:
        p = self.profile
        ready = (time.monotonic() - self._opened_at) >= self._scaled(p.ready_delay_s)
        dtr = rts = True
        for name, state in self.control_log:
            if name == "dtr":
                dtr = bool(state)
            elif name == "rts":
                rts = bool(state)
        return ModemStatus(
            cts=p.cts and ready,
            dsr=p.dsr and ready,
            dcd=p.dcd and ready,
            ri=p.ri,
            dtr=dtr,
            rts=rts,
        )

    # -- simulation controls (used by tests and the dev server) ----------
    def arm_receive(self, data: bytes | None = None) -> None:
        """Make the control punch a program out to us, in the background."""
        payload = data if data is not None else self.profile.outgoing
        if not payload:
            return
        self._out_stop.clear()
        self._out_thread = threading.Thread(
            target=self._punch_out, args=(payload,), daemon=True, name="fake-punchout"
        )
        self._out_thread.start()

    def inject(self, data: bytes) -> None:
        """Push bytes into our receive queue right now (XON, junk, a program)."""
        self._push_rx(data)

    def send_xon(self) -> None:
        self.inject(bytes([self.flow_control.xon]))

    def send_xoff(self) -> None:
        self.inject(bytes([self.flow_control.xoff]))

    def set_modem(self, **lines: bool) -> None:
        """Override the asserted modem lines (``cts=False`` etc.)."""
        for key, value in lines.items():
            if hasattr(self.profile, key):
                setattr(self.profile, key, bool(value))

    @property
    def written_text(self) -> str:
        return self.written.decode("ascii", errors="replace")

    # -- internals -------------------------------------------------------
    def _scaled(self, seconds: float) -> float:
        return max(seconds * self.profile.time_scale, 0.0)

    def _push_rx(self, data: bytes) -> None:
        with self._cv:
            self._rx.extend(data)
            self._cv.notify_all()

    def _inject_initial_xon(self) -> None:
        if self.is_open:
            self.inject(bytes([self.flow_control.xon]))

    def _drain(self) -> None:
        now = time.monotonic()
        elapsed = now - self._last_drain
        self._last_drain = now
        if self.profile.realtime:
            rate = max(self.line_params.baud, 50) / 10.0
        else:
            rate = self.profile.drain_bytes_per_s
        self._buffer_level = max(0.0, self._buffer_level - rate * elapsed)

    def _maybe_flow_control(self) -> None:
        """Emulate the control's XON/XOFF buffer management."""
        if not self.flow_control.software:
            return
        p = self.profile
        high = p.buffer_size * p.xoff_high_water
        low = p.buffer_size * p.xon_low_water
        if not self._xoff_sent and self._buffer_level >= high:
            self._xoff_sent = True
            self._push_rx(bytes([self.flow_control.xoff]))
            self._emit("transport.sim.xoff", {"buffer": int(self._buffer_level)})
        elif self._xoff_sent and self._buffer_level <= low:
            self._xoff_sent = False
            self._push_rx(bytes([self.flow_control.xon]))
            self._emit("transport.sim.xon", {"buffer": int(self._buffer_level)})

    def _punch_out(self, payload: bytes) -> None:
        if self._out_stop.wait(self._scaled(self.profile.outgoing_delay_s)):
            return
        chunk = max(1, self.profile.outgoing_chunk)
        for i in range(0, len(payload), chunk):
            if self._out_stop.is_set() or not self.is_open:
                return
            self._push_rx(payload[i : i + chunk])
            if self._out_stop.wait(self._scaled(self.profile.outgoing_gap_s)):
                return


# --------------------------------------------------------------------------
# A small stock program the Simulator punches out when receive is armed.
# --------------------------------------------------------------------------
SAMPLE_PROGRAM = (
    "%\r\n"
    "O0001 (RECEIVED FROM SIMULATOR)\r\n"
    "N10 G90 G54 G17 G21\r\n"
    "N20 T1 M06\r\n"
    "N30 S2400 M03\r\n"
    "N40 G00 X0. Y0. Z25.\r\n"
    "N50 G01 Z-2. F120.\r\n"
    "N60 G01 X50. F400.\r\n"
    "N70 G01 Y50.\r\n"
    "N80 G01 X0.\r\n"
    "N90 G01 Y0.\r\n"
    "N100 G00 Z25.\r\n"
    "N110 M05\r\n"
    "N120 M30\r\n"
    "%\r\n"
).encode("ascii")
