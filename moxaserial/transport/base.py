"""Abstract byte transport to a serial-attached CNC control.

The send/receive engines talk only to this interface, so the Moxa wire
protocol can be developed and swapped in without touching anything above
it, and the whole add-in can be exercised against
:class:`moxaserial.transport.fake.FakeTransport` with no hardware.

Threading contract
------------------
A transport is used from one engine thread at a time, but ``close()``,
``purge()`` and ``get_modem_status()`` may be called from another thread
(the UI asking to abort). Implementations must guard their own state with
a lock and make ``close()`` idempotent and safe to call while a
``read()`` is blocked.
"""

from __future__ import annotations

import abc
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from moxaserial.events import EventBus


class TransportError(Exception):
    """Any transport-level failure (connect, write, read, protocol)."""


class TransportNotOpen(TransportError):
    """Raised when an operation needs an open transport and there isn't one."""


class TransportTimeout(TransportError):
    """A read or a handshake did not complete inside its deadline."""


@dataclass(frozen=True)
class ModemStatus:
    """RS-232 input control lines as seen by us (the DTE)."""

    cts: bool = False
    dsr: bool = False
    dcd: bool = False
    ri: bool = False
    #: Our own outputs, as last commanded (shown as LEDs like CIMCO does).
    dtr: bool = False
    rts: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {
            "cts": self.cts, "dsr": self.dsr, "dcd": self.dcd, "ri": self.ri,
            "dtr": self.dtr, "rts": self.rts,
        }


@dataclass
class LineParams:
    """Serial line settings applied to the far-end UART."""

    baud: int = 9600
    data_bits: int = 8
    parity: str = "none"  # none|odd|even|mark|space
    stop_bits: str = "1"  # "1"|"1.5"|"2"

    @classmethod
    def from_machine(cls, machine: dict[str, Any]) -> LineParams:
        s = machine.get("serial", {})
        return cls(
            baud=int(s.get("baud", 9600)),
            data_bits=int(s.get("data_bits", 8)),
            parity=str(s.get("parity", "none")),
            stop_bits=str(s.get("stop_bits", "1")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "baud": self.baud,
            "data_bits": self.data_bits,
            "parity": self.parity,
            "stop_bits": self.stop_bits,
        }


@dataclass
class FlowControl:
    """Flow-control selection, expanded from the config's single enum."""

    mode: str = "none"  # none|xonxoff|rtscts|dtrdsr|both
    xon: int = 0x11
    xoff: int = 0x13

    @property
    def software(self) -> bool:
        return self.mode in ("xonxoff", "both")

    @property
    def hardware(self) -> bool:
        return self.mode in ("rtscts", "dtrdsr", "both")

    @classmethod
    def from_machine(cls, machine: dict[str, Any]) -> FlowControl:
        s = machine.get("serial", {})
        return cls(
            mode=str(s.get("flow_control", "none")),
            xon=int(s.get("xon_char", 0x11)),
            xoff=int(s.get("xoff_char", 0x13)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {"mode": self.mode, "xon": self.xon, "xoff": self.xoff}


@dataclass
class TransportStats:
    """Counters the UI's activity LEDs and the log read."""

    bytes_written: int = 0
    bytes_read: int = 0
    opened_at: float = 0.0
    last_tx: float = 0.0
    last_rx: float = 0.0
    errors: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "bytes_written": self.bytes_written,
            "bytes_read": self.bytes_read,
            "opened_at": self.opened_at,
            "last_tx": self.last_tx,
            "last_rx": self.last_rx,
            "errors": list(self.errors),
        }


class Transport(abc.ABC):
    """Abstract byte pipe to the control.

    Subclasses implement the ``_do_*`` hooks; the public methods here add
    the shared locking, state checks and stats/event bookkeeping so every
    transport behaves identically from the engines' point of view.
    """

    #: Human-readable name used in logs and the UI.
    kind = "abstract"

    def __init__(self, bus: EventBus | None = None) -> None:
        self._lock = threading.RLock()
        self._open = False
        self._bus = bus
        self._machine: dict[str, Any] = {}
        self._line = LineParams()
        self._flow = FlowControl()
        self.stats = TransportStats()

    # -- lifecycle -------------------------------------------------------
    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._open

    def open(self, machine: dict[str, Any]) -> None:
        """Connect and apply the machine's line settings."""
        with self._lock:
            if self._open:
                return
            self._machine = dict(machine)
            self._line = LineParams.from_machine(machine)
            self._flow = FlowControl.from_machine(machine)
            self.stats = TransportStats()
        # Not under the lock: a transport's reader thread may need it
        # (modem-status cache, error reporting) while the open handshake
        # is still waiting for replies.
        self._do_open(machine)
        import time

        with self._lock:
            self._open = True
            self.stats.opened_at = time.time()
        self.set_line_params(self._line)
        self.set_flow_control(self._flow)
        serial = machine.get("serial", {})
        self.set_dtr(bool(serial.get("assert_dtr", True)))
        self.set_rts(bool(serial.get("assert_rts", True)))
        self._emit("transport.open", {"kind": self.kind, "machine": machine.get("name", "")})

    def close(self) -> None:
        """Disconnect. Idempotent; safe from another thread."""
        with self._lock:
            if not self._open:
                return
            self._open = False
        try:
            self._do_close()
        finally:
            self._emit("transport.close", {"kind": self.kind, "stats": self.stats.to_dict()})

    def __enter__(self) -> Transport:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- io --------------------------------------------------------------
    def write(self, data: bytes) -> int:
        """Write *data*; returns bytes accepted. Blocks until handed off."""
        self._require_open()
        n = self._do_write(data)
        import time

        with self._lock:
            self.stats.bytes_written += n
            self.stats.last_tx = time.time()
        return n

    def read(self, n: int = 4096, timeout: float = 0.2) -> bytes:
        """Read up to *n* bytes, waiting at most *timeout* seconds.

        Returns ``b""`` on timeout - a quiet line is normal, not an error.
        """
        self._require_open()
        data = self._do_read(n, timeout)
        if data:
            import time

            with self._lock:
                self.stats.bytes_read += len(data)
                self.stats.last_rx = time.time()
        return data

    def purge(self, rx: bool = True, tx: bool = True) -> None:
        """Discard buffered data in either direction."""
        if not self.is_open:
            return
        self._do_purge(rx=rx, tx=tx)

    # -- line control ----------------------------------------------------
    def set_line_params(self, params: LineParams) -> None:
        with self._lock:
            self._line = params
        self._do_set_line_params(params)

    def set_flow_control(self, flow: FlowControl) -> None:
        with self._lock:
            self._flow = flow
        self._do_set_flow_control(flow)

    def set_dtr(self, state: bool) -> None:
        self._do_set_dtr(state)

    def set_rts(self, state: bool) -> None:
        self._do_set_rts(state)

    def get_modem_status(self) -> ModemStatus:
        if not self.is_open:
            return ModemStatus()
        return self._do_get_modem_status()

    # -- optional queue awareness -----------------------------------------
    def pending_tx(self) -> int | None:
        """Bytes accepted but not yet on the serial line, or ``None`` if unknown."""
        return None

    def drain(self, timeout: float = 60.0, should_abort: Callable[[], bool] | None = None) -> bool:
        """Block until everything written has left the serial port.

        Returns ``False`` on timeout or abort. Transports without a queue
        view return ``True`` immediately.
        """
        return True

    @property
    def line_params(self) -> LineParams:
        with self._lock:
            return self._line

    @property
    def flow_control(self) -> FlowControl:
        with self._lock:
            return self._flow

    # -- helpers ---------------------------------------------------------
    def _require_open(self) -> None:
        if not self.is_open:
            raise TransportNotOpen(f"{self.kind} transport is not open")

    def _emit(self, topic: str, payload: dict[str, Any] | None = None) -> None:
        if self._bus is not None:
            self._bus.publish(topic, payload or {})

    def describe(self) -> dict[str, Any]:
        """Snapshot for the UI / log."""
        return {
            "kind": self.kind,
            "open": self.is_open,
            "line": self.line_params.to_dict(),
            "flow": self.flow_control.to_dict(),
            "stats": self.stats.to_dict(),
            "modem": self.get_modem_status().to_dict(),
        }

    # -- subclass hooks --------------------------------------------------
    @abc.abstractmethod
    def _do_open(self, machine: dict[str, Any]) -> None: ...

    @abc.abstractmethod
    def _do_close(self) -> None: ...

    @abc.abstractmethod
    def _do_write(self, data: bytes) -> int: ...

    @abc.abstractmethod
    def _do_read(self, n: int, timeout: float) -> bytes: ...

    def _do_purge(self, rx: bool, tx: bool) -> None:
        return None

    def _do_set_line_params(self, params: LineParams) -> None:
        return None

    def _do_set_flow_control(self, flow: FlowControl) -> None:
        return None

    def _do_set_dtr(self, state: bool) -> None:
        return None

    def _do_set_rts(self, state: bool) -> None:
        return None

    def _do_get_modem_status(self) -> ModemStatus:
        return ModemStatus()
