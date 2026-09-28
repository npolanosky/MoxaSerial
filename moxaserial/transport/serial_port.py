"""SerialTransport - a local RS-232 port, with no device server in between.

This is the "plug a USB-serial adapter into the laptop" path: an onboard
UART, a USB-serial adapter, or a virtual COM port created by some other
driver. It implements exactly the same :class:`~moxaserial.transport.base.Transport`
contract as :mod:`moxaserial.transport.moxa`, so the send and receive
engines cannot tell the two apart.

Stdlib only, on purpose: Fusion embeds its own CPython and ``pip install
pyserial`` into it is not something a shop should have to do. Everything
here is ``os``/``termios``/``fcntl``/``select`` on POSIX and ``ctypes``
against ``kernel32`` on Windows.

Platform split
--------------
Two private backends, one public transport:

* :class:`_PosixSerialBackend` - macOS and Linux. ``os.open`` with
  ``O_RDWR | O_NOCTTY | O_NONBLOCK``, raw ``termios`` settings,
  ``select`` for timed reads and writes, ``TIOCM*`` ioctls for the modem
  lines, ``TIOCOUTQ`` for the output queue, ``tcdrain`` to flush it.
* :class:`_WindowsSerialBackend` - ``CreateFileW`` on ``\\\\.\\COMn``,
  ``GetCommState``/``SetCommState`` (DCB), ``SetCommTimeouts``, blocking
  ``ReadFile``/``WriteFile`` with short comm timeouts,
  ``GetCommModemStatus``, ``EscapeCommFunction``, ``ClearCommError``
  (COMSTAT.cbOutQue), ``PurgeComm``, ``SetCommBreak``.

:class:`SerialTransport` owns the state that is common to both and never
touches a platform API itself.

Threading
---------
Per :mod:`moxaserial.transport.base`: one engine thread does the I/O,
while ``close()``, ``purge()`` and ``get_modem_status()`` may arrive from
another thread. ``close()`` must unblock a read that is in flight. POSIX
does that with a self-pipe that is included in every ``select``; Windows
by keeping each ``ReadFile`` bounded to ``_READ_SLICE_S`` and re-checking
the stop flag (plus a best-effort ``CancelIoEx``). Both wait briefly for
the reader to leave before the handle is actually closed, so a descriptor
is never pulled out from under a blocked ``select``.

Verified on macOS against a ``pty`` pair (``tests/test_serial_port.py``).
The Windows backend is exercised by a ctypes test double that records the
calls; **it has not been run against a real Windows COM port.**
"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import subprocess
import sys
import threading
import time
from collections.abc import Callable
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

log = get_logger("serial")

IS_WINDOWS = os.name == "nt"
IS_DARWIN = sys.platform == "darwin"

#: Longest a single blocking read may sit before the stop flag is re-checked.
_READ_SLICE_S = 0.05
#: How long close() waits for an in-flight read/write to notice and leave.
_CLOSE_GRACE_S = 1.0


# ==========================================================================
# POSIX backend
# ==========================================================================

if not IS_WINDOWS:  # pragma: no branch - the import itself is the platform test
    import fcntl
    import select
    import struct
    import termios

    #: ``termios.error`` is **not** an ``OSError`` subclass, so every handler
    #: around a ``tcgetattr``/``tcsetattr``/``tcflush`` call has to name it
    #: explicitly or the exception escapes as-is - past the ``TransportError``
    #: handlers, with the descriptor still open.
    _TTY_ERRORS: tuple[type[BaseException], ...] = (OSError, termios.error)
else:  # pragma: no cover - Windows has no termios
    _TTY_ERRORS = (OSError,)


def _ioctl_const(name: str, darwin: int, linux: int) -> int | None:
    """``termios.<name>`` if Python exposes it, else the platform's value.

    Python's ``termios`` module publishes a different subset per platform
    (macOS has no ``TIOCSBRK``, some Linux builds no ``TIOCOUTQ``), so the
    numeric values are carried here as a fallback rather than letting a
    missing attribute disable a feature that the kernel supports.
    """
    if IS_WINDOWS:
        return None
    value = getattr(termios, name, None)
    if isinstance(value, int):
        return value
    if IS_DARWIN:
        return darwin
    if sys.platform.startswith("linux"):
        return linux
    return None


if IS_WINDOWS:
    TIOCMGET = TIOCMBIS = TIOCMBIC = TIOCOUTQ = TIOCSBRK = TIOCCBRK = None
else:
    TIOCMGET = _ioctl_const("TIOCMGET", 0x4004746A, 0x5415)
    TIOCMBIS = _ioctl_const("TIOCMBIS", 0x8004746C, 0x5416)
    TIOCMBIC = _ioctl_const("TIOCMBIC", 0x8004746B, 0x5417)
    TIOCOUTQ = _ioctl_const("TIOCOUTQ", 0x40047473, 0x5411)
    TIOCSBRK = _ioctl_const("TIOCSBRK", 0x2000747B, 0x5427)
    TIOCCBRK = _ioctl_const("TIOCCBRK", 0x2000747A, 0x5428)

#: Modem-line bits. macOS and Linux agree on these (both inherit the BSD
#: values), so one table covers both.
TIOCM_DTR = 0x002
TIOCM_RTS = 0x004
TIOCM_CTS = 0x020
TIOCM_CAR = 0x040  # DCD
TIOCM_RNG = 0x080  # RI
TIOCM_DSR = 0x100


def _baud_constant(baud: int) -> int | None:
    """``termios.B<baud>`` for *baud*, or ``None`` when there is no constant.

    On macOS the speed constants *are* the literal bit rates, so any rate
    the driver accepts can be set directly. On Linux they are small
    indices and a rate with no constant needs ``BOTHER``/``termios2``,
    which is out of scope - we report it instead of silently running at
    the wrong speed.
    """
    if IS_WINDOWS:
        return None
    value = getattr(termios, f"B{int(baud)}", None)
    if isinstance(value, int):
        return value
    if IS_DARWIN:
        # Darwin's tcsetattr takes the literal rate; IOSSIOSPEED is only
        # needed for rates the driver has to synthesise.
        return int(baud)
    return None


class _PosixSerialBackend:
    """termios/fcntl/select over a tty opened by path."""

    def __init__(self) -> None:
        self._fd = -1
        self._wake_r = -1
        self._wake_w = -1
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._busy = 0                      # threads inside read()/write()
        self._idle = threading.Condition(threading.Lock())
        self.device = ""
        self.warnings: list[str] = []

    # -- lifecycle -------------------------------------------------------
    def open(self, device: str) -> None:
        self._stop.clear()
        self.warnings = []
        try:
            fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        except OSError as exc:
            raise TransportError(
                f"Could not open serial port {device} - {exc}. "
                "Check the device name, that the adapter is plugged in, and that "
                "nothing else (a terminal program, another DNC) already has it open."
            ) from exc
        try:
            # Exclusive use where the kernel offers it, so two sends cannot
            # interleave on one adapter. Not fatal if the fd is not a tty
            # that supports it.
            if hasattr(fcntl, "LOCK_EX") and hasattr(fcntl, "LOCK_NB"):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except OSError:
                    os.close(fd)
                    raise TransportError(
                        f"Serial port {device} is already in use by another program."
                    ) from None
            termios.tcgetattr(fd)  # fails loudly if this is not a tty at all
            # termios.error is NOT an OSError subclass, so it has to be named
            # explicitly - otherwise it escapes with the descriptor still open
            # and, because base.Transport only marks itself open *after*
            # _do_open returns, close() is a no-op and the fd leaks for the
            # life of the Fusion process.
        except TransportError:
            raise
        except (OSError, termios.error) as exc:
            os.close(fd)
            raise TransportError(f"{device} is not a serial port - {exc}.") from exc
        try:
            self._wake_r, self._wake_w = os.pipe()
            os.set_blocking(self._wake_r, False)
            os.set_blocking(self._wake_w, False)
        except OSError as exc:  # EMFILE here would otherwise leak the port fd
            os.close(fd)
            self._wake_r = self._wake_w = -1
            raise TransportError(f"Could not open serial port {device} - {exc}.") from exc
        with self._lock:
            self._fd = fd
            self.device = device
        log.info("Opened serial port %s (fd %d)", device, fd)

    def close(self) -> None:
        self._stop.set()
        self._wake()
        # Let a blocked read/write leave select() before the descriptor
        # disappears; otherwise select() would be waiting on a number the
        # kernel may hand to some other part of Fusion.
        deadline = time.monotonic() + _CLOSE_GRACE_S
        with self._idle:
            while self._busy and time.monotonic() < deadline:
                self._idle.wait(0.02)
        # All three descriptors are taken in one critical section: two
        # threads in close() would otherwise both capture the same self-pipe
        # fds and both close them, and the second close can land on a number
        # the kernel has already handed to some other part of Fusion.
        with self._lock:
            fd, self._fd = self._fd, -1
            wake_r, self._wake_r = self._wake_r, -1
            wake_w, self._wake_w = self._wake_w, -1
        for handle in (fd, wake_r, wake_w):
            if handle >= 0:
                try:
                    os.close(handle)
                except OSError:
                    pass

    def _wake(self) -> None:
        if self._wake_w >= 0:
            try:
                os.write(self._wake_w, b"\x00")
            except OSError:
                pass

    def _drain_wake(self) -> None:
        try:
            os.read(self._wake_r, 64)
        except OSError:
            pass

    @property
    def fd(self) -> int:
        with self._lock:
            return self._fd

    def _enter(self) -> int:
        fd = self.fd
        if fd < 0 or self._stop.is_set():
            return -1
        with self._idle:
            self._busy += 1
        return fd

    def _leave(self) -> None:
        with self._idle:
            self._busy -= 1
            if not self._busy:
                self._idle.notify_all()

    # -- io --------------------------------------------------------------
    def read(self, n: int, timeout: float) -> bytes:
        fd = self._enter()
        if fd < 0:
            return b""
        try:
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                if self._stop.is_set():
                    return b""
                remaining = deadline - time.monotonic()
                if remaining < 0:
                    remaining = 0.0
                try:
                    ready, _, _ = select.select(
                        [fd, self._wake_r], [], [], min(remaining, _READ_SLICE_S)
                    )
                except (OSError, ValueError):
                    # OSError: the fd went away under us. ValueError: close()
                    # reset the self-pipe to -1 between our stop check and
                    # here. Both mean the same thing - the port is closing.
                    return b""
                if self._wake_r in ready:
                    self._drain_wake()
                    return b""
                if fd in ready:
                    try:
                        data = os.read(fd, max(n, 1))
                    except BlockingIOError:
                        data = b""
                    except OSError as exc:
                        if self._stop.is_set():
                            return b""
                        raise TransportError(
                            f"Read from {self.device} failed - {exc}. "
                            "Was the adapter unplugged?"
                        ) from exc
                    if data:
                        return data
                    # A readable fd that yields nothing is end-of-file: on a
                    # USB adapter that means the device vanished.
                    if self._stop.is_set():
                        return b""
                    raise TransportError(
                        f"{self.device} reported end of file - the port was closed "
                        "or the adapter was unplugged."
                    )
                if remaining <= 0:
                    return b""
        finally:
            self._leave()

    def write(self, data: bytes, should_abort: Callable[[], bool] | None = None) -> int:
        fd = self._enter()
        if fd < 0:
            raise TransportError(f"Serial port {self.device} is closed.")
        try:
            view = memoryview(data)
            sent = 0
            while sent < len(view):
                if self._stop.is_set() or (should_abort is not None and should_abort()):
                    raise TransportError("The serial port was closed while writing.")
                try:
                    _, writable, _ = select.select([], [fd], [], _READ_SLICE_S)
                except OSError as exc:
                    raise TransportError(f"Serial port {self.device} is closed - {exc}.") from exc
                if not writable:
                    # Hardware or software flow control is holding us off.
                    continue
                try:
                    sent += os.write(fd, view[sent:])
                except BlockingIOError:
                    continue
                except OSError as exc:
                    raise TransportError(f"Write to {self.device} failed - {exc}.") from exc
            return sent
        finally:
            self._leave()

    def purge(self, rx: bool, tx: bool) -> None:
        fd = self.fd
        if fd < 0:
            return
        if rx and tx:
            which = termios.TCIOFLUSH
        elif rx:
            which = termios.TCIFLUSH
        elif tx:
            which = termios.TCOFLUSH
        else:
            return
        try:
            termios.tcflush(fd, which)
        except _TTY_ERRORS as exc:
            log.warning("tcflush on %s failed: %s", self.device, exc)

    # -- line settings ---------------------------------------------------
    def set_line_params(self, params: LineParams) -> None:
        fd = self.fd
        if fd < 0:
            return
        try:
            attrs = termios.tcgetattr(fd)
        except _TTY_ERRORS as exc:
            raise TransportError(f"Could not read the settings of {self.device} - {exc}.") from exc
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs

        # Raw: no canonical mode, no echo, no signal characters, no output
        # post-processing, no CR/LF translation. NC data is binary.
        iflag &= ~(
            termios.IGNBRK | termios.BRKINT | termios.PARMRK | termios.ISTRIP
            | termios.INLCR | termios.IGNCR | termios.ICRNL | termios.INPCK
            | termios.IGNPAR
        )
        oflag &= ~termios.OPOST
        lflag &= ~(
            termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
        )
        if hasattr(termios, "ECHOE"):
            lflag &= ~termios.ECHOE
        if hasattr(termios, "ECHOK"):
            lflag &= ~termios.ECHOK

        cflag |= termios.CLOCAL | termios.CREAD   # ignore DCD, enable the receiver
        cflag &= ~termios.CSIZE
        cflag |= {5: termios.CS5, 6: termios.CS6, 7: termios.CS7, 8: termios.CS8}.get(
            int(params.data_bits), termios.CS8
        )

        cflag &= ~(termios.PARENB | termios.PARODD)
        cmspar = getattr(termios, "CMSPAR", None)
        if cmspar is not None:
            cflag &= ~cmspar
        parity = str(params.parity).lower()
        if parity == "even":
            cflag |= termios.PARENB
        elif parity == "odd":
            cflag |= termios.PARENB | termios.PARODD
        elif parity in ("mark", "space"):
            if cmspar is None:
                self._warn(
                    f"{parity.capitalize()} parity is not supported by this operating "
                    "system's serial driver; the port will run with no parity."
                )
            else:
                cflag |= termios.PARENB | cmspar
                if parity == "mark":
                    cflag |= termios.PARODD

        stop_bits = str(params.stop_bits)
        if stop_bits == "1":
            cflag &= ~termios.CSTOPB
        else:
            if stop_bits == "1.5":
                self._warn(
                    "1.5 stop bits is a 5-data-bit-only mode that POSIX cannot express; "
                    "using 2 stop bits."
                )
            cflag |= termios.CSTOPB

        speed = _baud_constant(params.baud)
        if speed is None:
            raise TransportError(
                f"{params.baud} baud is not one of the rates this operating system "
                "can set on a serial port."
            )

        cc = list(cc)
        cc[termios.VMIN] = 0      # select() does the waiting, never read()
        cc[termios.VTIME] = 0

        try:
            termios.tcsetattr(
                fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, speed, speed, cc]
            )
        except _TTY_ERRORS as exc:
            raise TransportError(
                f"Could not apply {params.baud} {params.data_bits}"
                f"{parity[:1].upper() or 'N'}{params.stop_bits} to {self.device} - {exc}."
            ) from exc
        log.info(
            "Serial line set: %s %d %d%s%s",
            self.device, params.baud, params.data_bits,
            (parity[:1].upper() or "N"), params.stop_bits,
        )

    def set_flow_control(self, flow: FlowControl) -> None:
        fd = self.fd
        if fd < 0:
            return
        try:
            attrs = termios.tcgetattr(fd)
        except _TTY_ERRORS as exc:
            raise TransportError(f"Could not read the settings of {self.device} - {exc}.") from exc
        iflag, oflag, cflag, lflag, ispeed, ospeed, cc = attrs
        cc = list(cc)

        iflag &= ~(termios.IXON | termios.IXOFF)
        if hasattr(termios, "IXANY"):
            iflag &= ~termios.IXANY
        crtscts = getattr(termios, "CRTSCTS", None)
        if crtscts is None:
            crtscts = getattr(termios, "CCTS_OFLOW", 0) | getattr(termios, "CRTS_IFLOW", 0)
        if crtscts:
            cflag &= ~crtscts
        # macOS can also do DTR/DSR in the driver; Linux cannot.
        dtrflow = getattr(termios, "CDTR_IFLOW", 0) | getattr(termios, "CDSR_OFLOW", 0)
        if dtrflow:
            cflag &= ~dtrflow

        mode = str(flow.mode).lower()
        if mode in ("xonxoff", "both"):
            iflag |= termios.IXON | termios.IXOFF
            cc[termios.VSTART] = bytes([flow.xon & 0xFF])
            cc[termios.VSTOP] = bytes([flow.xoff & 0xFF])
        if mode in ("rtscts", "both"):
            if crtscts:
                cflag |= crtscts
            else:
                self._warn("RTS/CTS flow control is not available on this operating system.")
        if mode == "dtrdsr":
            if dtrflow:
                cflag |= dtrflow
            else:
                self._warn(
                    "DTR/DSR flow control is not supported by this operating system's "
                    "serial driver. 'Wait for DSR' still works from the modem lines."
                )
        try:
            termios.tcsetattr(fd, termios.TCSANOW, [iflag, oflag, cflag, lflag, ispeed, ospeed, cc])
        except _TTY_ERRORS as exc:
            raise TransportError(
                f"Could not set {mode} flow control on {self.device} - {exc}."
            ) from exc

    # -- modem lines -----------------------------------------------------
    def _modem_bits(self, bits: int, on: bool) -> None:
        fd = self.fd
        if fd < 0:
            return
        request = TIOCMBIS if on else TIOCMBIC
        if request is None:
            return
        try:
            fcntl.ioctl(fd, request, struct.pack("I", bits))
        except _TTY_ERRORS as exc:
            # A pty has no modem lines at all; a real port that refuses is
            # worth one line in the log, not a failed job.
            log.debug("Setting modem bits 0x%x on %s failed: %s", bits, self.device, exc)

    def set_dtr(self, state: bool) -> None:
        self._modem_bits(TIOCM_DTR, state)

    def set_rts(self, state: bool) -> None:
        self._modem_bits(TIOCM_RTS, state)

    def get_modem_status(self) -> dict[str, bool] | None:
        """CTS/DSR/DCD/RI, or ``None`` when the device has no modem lines."""
        fd = self.fd
        if fd < 0 or TIOCMGET is None:
            return None
        try:
            raw = fcntl.ioctl(fd, TIOCMGET, struct.pack("I", 0))
        except _TTY_ERRORS as exc:
            log.debug("TIOCMGET on %s failed: %s", self.device, exc)
            return None
        bits = struct.unpack("I", raw)[0]
        return {
            "cts": bool(bits & TIOCM_CTS),
            "dsr": bool(bits & TIOCM_DSR),
            "dcd": bool(bits & TIOCM_CAR),
            "ri": bool(bits & TIOCM_RNG),
        }

    # -- queue -----------------------------------------------------------
    def pending_tx(self) -> int | None:
        fd = self.fd
        if fd < 0 or TIOCOUTQ is None:
            return None
        try:
            raw = fcntl.ioctl(fd, TIOCOUTQ, struct.pack("I", 0))
        except OSError:
            return None
        return int(struct.unpack("I", raw)[0])

    def drain(self, timeout: float, should_abort: Callable[[], bool] | None) -> bool:
        """Poll ``TIOCOUTQ`` to zero, then ``tcdrain`` as the final word.

        ``tcdrain`` on its own is uninterruptible - a control holding CTS
        low would wedge Stop - so the queue is polled first and tcdrain
        only runs once there is nothing left for it to wait for.
        """
        deadline = time.monotonic() + max(timeout, 0.0)
        pending = self.pending_tx()
        while pending is not None and pending > 0:
            if should_abort is not None and should_abort():
                return False
            if time.monotonic() > deadline or self._stop.is_set():
                return False
            time.sleep(0.02)
            pending = self.pending_tx()
        fd = self.fd
        if fd < 0:
            return True
        if pending is None and should_abort is not None and should_abort():
            return False
        try:
            termios.tcdrain(fd)
        except _TTY_ERRORS as exc:
            log.debug("tcdrain on %s failed: %s", self.device, exc)
        return True

    def send_break(self, duration_s: float) -> None:
        fd = self.fd
        if fd < 0:
            return
        if TIOCSBRK is not None and TIOCCBRK is not None and duration_s > 0:
            try:
                fcntl.ioctl(fd, TIOCSBRK)
                time.sleep(duration_s)
                fcntl.ioctl(fd, TIOCCBRK)
                return
            except OSError as exc:
                log.debug("TIOCSBRK on %s failed (%s); falling back to tcsendbreak", self.device, exc)
        try:
            termios.tcsendbreak(fd, 0)
        except _TTY_ERRORS as exc:
            raise TransportError(f"Could not send a break on {self.device} - {exc}.") from exc

    def _warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)
            log.warning(message)


# ==========================================================================
# Windows backend
# ==========================================================================
# ctypes structure definitions are portable - they are declared here on
# every platform so the test double can build and inspect them on this Mac.

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x80
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

PURGE_TXABORT = 0x0001
PURGE_RXABORT = 0x0002
PURGE_TXCLEAR = 0x0004
PURGE_RXCLEAR = 0x0008

SETXOFF, SETXON, SETRTS, CLRRTS, SETDTR, CLRDTR = 1, 2, 3, 4, 5, 6

MS_CTS_ON = 0x0010
MS_DSR_ON = 0x0020
MS_RING_ON = 0x0040
MS_RLSD_ON = 0x0080

NOPARITY, ODDPARITY, EVENPARITY, MARKPARITY, SPACEPARITY = 0, 1, 2, 3, 4
ONESTOPBIT, ONE5STOPBITS, TWOSTOPBITS = 0, 1, 2

#: DCB.fRtsControl
RTS_CONTROL_DISABLE, RTS_CONTROL_ENABLE, RTS_CONTROL_HANDSHAKE = 0, 1, 2
#: DCB.fDtrControl
DTR_CONTROL_DISABLE, DTR_CONTROL_ENABLE, DTR_CONTROL_HANDSHAKE = 0, 1, 2


class DCB(ctypes.Structure):
    """Win32 ``DCB``. The flag word is expanded into its real bitfields so
    the backend (and the tests) can set one control at a time."""

    _fields_ = [
        ("DCBlength", ctypes.c_uint32),
        ("BaudRate", ctypes.c_uint32),
        ("fBinary", ctypes.c_uint32, 1),
        ("fParity", ctypes.c_uint32, 1),
        ("fOutxCtsFlow", ctypes.c_uint32, 1),
        ("fOutxDsrFlow", ctypes.c_uint32, 1),
        ("fDtrControl", ctypes.c_uint32, 2),
        ("fDsrSensitivity", ctypes.c_uint32, 1),
        ("fTXContinueOnXoff", ctypes.c_uint32, 1),
        ("fOutX", ctypes.c_uint32, 1),
        ("fInX", ctypes.c_uint32, 1),
        ("fErrorChar", ctypes.c_uint32, 1),
        ("fNull", ctypes.c_uint32, 1),
        ("fRtsControl", ctypes.c_uint32, 2),
        ("fAbortOnError", ctypes.c_uint32, 1),
        ("fDummy2", ctypes.c_uint32, 17),
        ("wReserved", ctypes.c_uint16),
        ("XonLim", ctypes.c_uint16),
        ("XoffLim", ctypes.c_uint16),
        ("ByteSize", ctypes.c_uint8),
        ("Parity", ctypes.c_uint8),
        ("StopBits", ctypes.c_uint8),
        ("XonChar", ctypes.c_char),
        ("XoffChar", ctypes.c_char),
        ("ErrorChar", ctypes.c_char),
        ("EofChar", ctypes.c_char),
        ("EvtChar", ctypes.c_char),
        ("wReserved1", ctypes.c_uint16),
    ]


class COMMTIMEOUTS(ctypes.Structure):
    _fields_ = [
        ("ReadIntervalTimeout", ctypes.c_uint32),
        ("ReadTotalTimeoutMultiplier", ctypes.c_uint32),
        ("ReadTotalTimeoutConstant", ctypes.c_uint32),
        ("WriteTotalTimeoutMultiplier", ctypes.c_uint32),
        ("WriteTotalTimeoutConstant", ctypes.c_uint32),
    ]


class COMSTAT(ctypes.Structure):
    _fields_ = [
        ("fCtsHold", ctypes.c_uint32, 1),
        ("fDsrHold", ctypes.c_uint32, 1),
        ("fRlsdHold", ctypes.c_uint32, 1),
        ("fXoffHold", ctypes.c_uint32, 1),
        ("fXoffSent", ctypes.c_uint32, 1),
        ("fEof", ctypes.c_uint32, 1),
        ("fTxim", ctypes.c_uint32, 1),
        ("fReserved", ctypes.c_uint32, 25),
        ("cbInQue", ctypes.c_uint32),
        ("cbOutQue", ctypes.c_uint32),
    ]


def _windows_device_path(device: str) -> str:
    r"""``COM12`` -> ``\\.\COM12``. Ports above COM9 need the prefix."""
    name = device.strip()
    if not name:
        return name
    if name.startswith("\\\\"):
        return name
    if re.fullmatch(r"(?i)com\d+", name):
        return "\\\\.\\" + name.upper()
    return name


def _load_kernel32():  # pragma: no cover - Windows only
    if not IS_WINDOWS:
        raise TransportError("The Windows serial backend needs Windows.")
    return ctypes.WinDLL("kernel32", use_last_error=True)


class _WindowsSerialBackend:
    """``kernel32`` over a ``CreateFileW`` handle.

    *kernel32* is injectable so the whole backend can be driven by a test
    double off Windows. Every call passes ``ctypes.pointer(...)`` rather
    than ``ctypes.byref(...)`` - identical to the OS, but a Python double
    can follow ``.contents`` to read what we set and to fill in what we
    read back.
    """

    def __init__(self, kernel32: Any = None) -> None:
        self._k32 = kernel32 if kernel32 is not None else _load_kernel32()
        self._handle: int | None = None
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._busy = 0
        self._idle = threading.Condition(threading.Lock())
        self.device = ""
        self.warnings: list[str] = []
        self._timeouts_read_ms = -1

    # -- lifecycle -------------------------------------------------------
    def open(self, device: str) -> None:
        self._stop.clear()
        self.warnings = []
        # The cache is per *handle*, not per backend, and SerialTransport
        # reuses one backend across open/close cycles. Without this reset a
        # reopen skips SetCommTimeouts, and a fresh CreateFileW handle has
        # all-zero COMMTIMEOUTS - meaning ReadFile blocks until a byte
        # arrives, never re-checks the stop flag, and close() lands on a
        # pending read.
        self._timeouts_read_ms = -1
        path = _windows_device_path(device)
        handle = self._k32.CreateFileW(
            ctypes.c_wchar_p(path),
            GENERIC_READ | GENERIC_WRITE,
            0,                      # no sharing: a serial port has one owner
            None,
            OPEN_EXISTING,
            FILE_ATTRIBUTE_NORMAL,  # synchronous; reads are bounded by timeouts
            None,
        )
        if handle in (INVALID_HANDLE_VALUE, 0, None, -1):
            raise TransportError(
                f"Could not open serial port {device} - {self._last_error()}. "
                "Check the COM number in Device Manager and that nothing else has it open."
            )
        with self._lock:
            self._handle = handle
            self.device = device
        self._apply_timeouts(int(_READ_SLICE_S * 1000))
        log.info("Opened serial port %s", device)

    def close(self) -> None:
        self._stop.set()
        handle = self._handle
        if handle is not None:
            cancel = getattr(self._k32, "CancelIoEx", None)
            if cancel is not None:
                try:
                    cancel(handle, None)
                except Exception:  # noqa: BLE001 - best effort only
                    log.debug("CancelIoEx failed on %s", self.device, exc_info=True)
        deadline = time.monotonic() + _CLOSE_GRACE_S
        with self._idle:
            while self._busy and time.monotonic() < deadline:
                self._idle.wait(0.02)
        with self._lock:
            handle, self._handle = self._handle, None
        if handle is not None:
            self._k32.CloseHandle(handle)

    @property
    def handle(self) -> int | None:
        with self._lock:
            return self._handle

    def _enter(self) -> int | None:
        handle = self.handle
        if handle is None or self._stop.is_set():
            return None
        with self._idle:
            self._busy += 1
        return handle

    def _leave(self) -> None:
        with self._idle:
            self._busy -= 1
            if not self._busy:
                self._idle.notify_all()

    def _last_error(self) -> str:
        try:
            code = ctypes.get_last_error() if IS_WINDOWS else 0
        except Exception:  # noqa: BLE001
            code = 0
        if not code:
            getter = getattr(self._k32, "GetLastError", None)
            if getter is not None:
                try:
                    code = int(getter())
                except Exception:  # noqa: BLE001
                    code = 0
        return f"Windows error {code}" if code else "the driver refused the request"

    # -- timeouts --------------------------------------------------------
    def _apply_timeouts(self, read_ms: int) -> None:
        if read_ms == self._timeouts_read_ms:
            return
        handle = self.handle
        if handle is None:
            return
        timeouts = COMMTIMEOUTS(
            ReadIntervalTimeout=0xFFFFFFFF if read_ms == 0 else 0,
            ReadTotalTimeoutMultiplier=0,
            ReadTotalTimeoutConstant=max(int(read_ms), 0),
            WriteTotalTimeoutMultiplier=0,
            # A write that cannot proceed because the control is holding
            # XOFF/CTS must come back so the stop flag gets re-checked.
            WriteTotalTimeoutConstant=500,
        )
        if not self._k32.SetCommTimeouts(handle, ctypes.pointer(timeouts)):
            raise TransportError(
                f"Could not set the timeouts on {self.device} - {self._last_error()}."
            )
        self._timeouts_read_ms = read_ms

    # -- io --------------------------------------------------------------
    def read(self, n: int, timeout: float) -> bytes:
        handle = self._enter()
        if handle is None:
            return b""
        try:
            deadline = time.monotonic() + max(timeout, 0.0)
            buf = ctypes.create_string_buffer(max(n, 1))
            got = ctypes.c_uint32(0)
            while True:
                if self._stop.is_set():
                    return b""
                remaining = deadline - time.monotonic()
                slice_ms = 0 if remaining <= 0 else int(min(remaining, _READ_SLICE_S) * 1000)
                self._apply_timeouts(slice_ms)
                ok = self._k32.ReadFile(
                    handle, buf, ctypes.c_uint32(max(n, 1)), ctypes.pointer(got), None
                )
                if not ok:
                    if self._stop.is_set():
                        return b""
                    raise TransportError(
                        f"Read from {self.device} failed - {self._last_error()}."
                    )
                if got.value:
                    return buf.raw[: got.value]
                if remaining <= 0:
                    return b""
        finally:
            self._leave()

    def write(self, data: bytes, should_abort: Callable[[], bool] | None = None) -> int:
        handle = self._enter()
        if handle is None:
            raise TransportError(f"Serial port {self.device} is closed.")
        try:
            sent = 0
            total = len(data)
            written = ctypes.c_uint32(0)
            while sent < total:
                if self._stop.is_set() or (should_abort is not None and should_abort()):
                    raise TransportError("The serial port was closed while writing.")
                chunk = data[sent:]
                buf = ctypes.create_string_buffer(chunk, len(chunk))
                ok = self._k32.WriteFile(
                    handle, buf, ctypes.c_uint32(len(chunk)), ctypes.pointer(written), None
                )
                if not ok:
                    raise TransportError(f"Write to {self.device} failed - {self._last_error()}.")
                if written.value == 0:
                    # The write timed out: flow control is holding us off.
                    continue
                sent += written.value
            return sent
        finally:
            self._leave()

    def purge(self, rx: bool, tx: bool) -> None:
        handle = self.handle
        if handle is None:
            return
        flags = 0
        if rx:
            flags |= PURGE_RXABORT | PURGE_RXCLEAR
        if tx:
            flags |= PURGE_TXABORT | PURGE_TXCLEAR
        if flags and not self._k32.PurgeComm(handle, ctypes.c_uint32(flags)):
            log.warning("PurgeComm on %s failed: %s", self.device, self._last_error())

    # -- line settings ---------------------------------------------------
    def _get_dcb(self) -> tuple[int, DCB]:
        handle = self.handle
        if handle is None:
            raise TransportError(f"Serial port {self.device} is closed.")
        dcb = DCB()
        dcb.DCBlength = ctypes.sizeof(DCB)
        if not self._k32.GetCommState(handle, ctypes.pointer(dcb)):
            raise TransportError(
                f"Could not read the settings of {self.device} - {self._last_error()}."
            )
        dcb.DCBlength = ctypes.sizeof(DCB)
        return handle, dcb

    def _set_dcb(self, handle: int, dcb: DCB) -> None:
        if not self._k32.SetCommState(handle, ctypes.pointer(dcb)):
            raise TransportError(
                f"The driver rejected the settings for {self.device} - {self._last_error()}."
            )

    def set_line_params(self, params: LineParams) -> None:
        if self.handle is None:
            return
        handle, dcb = self._get_dcb()
        dcb.BaudRate = int(params.baud)
        dcb.ByteSize = int(params.data_bits)
        dcb.fBinary = 1                      # required: Windows supports nothing else
        parity = str(params.parity).lower()
        dcb.Parity = {
            "none": NOPARITY, "odd": ODDPARITY, "even": EVENPARITY,
            "mark": MARKPARITY, "space": SPACEPARITY,
        }.get(parity, NOPARITY)
        dcb.fParity = 0                      # we check parity above the transport
        dcb.StopBits = {
            "1": ONESTOPBIT, "1.5": ONE5STOPBITS, "2": TWOSTOPBITS,
        }.get(str(params.stop_bits), ONESTOPBIT)
        dcb.fNull = 0                        # never discard ASCII 0 for us
        dcb.fAbortOnError = 0
        dcb.fErrorChar = 0
        self._set_dcb(handle, dcb)

    def set_flow_control(self, flow: FlowControl) -> None:
        if self.handle is None:
            return
        handle, dcb = self._get_dcb()
        mode = str(flow.mode).lower()
        software = mode in ("xonxoff", "both")
        rtscts = mode in ("rtscts", "both")
        dtrdsr = mode == "dtrdsr"

        dcb.fOutX = 1 if software else 0          # honour XOFF from the control
        dcb.fInX = 1 if software else 0           # send XOFF when our buffer fills
        dcb.fTXContinueOnXoff = 1
        dcb.XonChar = bytes([flow.xon & 0xFF])
        dcb.XoffChar = bytes([flow.xoff & 0xFF])
        dcb.XonLim = 2048
        dcb.XoffLim = 512

        dcb.fOutxCtsFlow = 1 if rtscts else 0
        dcb.fRtsControl = RTS_CONTROL_HANDSHAKE if rtscts else RTS_CONTROL_ENABLE
        dcb.fOutxDsrFlow = 1 if dtrdsr else 0
        dcb.fDsrSensitivity = 1 if dtrdsr else 0
        dcb.fDtrControl = DTR_CONTROL_HANDSHAKE if dtrdsr else DTR_CONTROL_ENABLE
        self._set_dcb(handle, dcb)

    # -- modem lines -----------------------------------------------------
    def _escape(self, func: int) -> None:
        handle = self.handle
        if handle is None:
            return
        if not self._k32.EscapeCommFunction(handle, ctypes.c_uint32(func)):
            log.debug("EscapeCommFunction(%d) on %s failed", func, self.device)

    def set_dtr(self, state: bool) -> None:
        self._escape(SETDTR if state else CLRDTR)

    def set_rts(self, state: bool) -> None:
        self._escape(SETRTS if state else CLRRTS)

    def get_modem_status(self) -> dict[str, bool] | None:
        handle = self.handle
        if handle is None:
            return None
        bits = ctypes.c_uint32(0)
        if not self._k32.GetCommModemStatus(handle, ctypes.pointer(bits)):
            log.debug("GetCommModemStatus on %s failed", self.device)
            return None
        value = bits.value
        return {
            "cts": bool(value & MS_CTS_ON),
            "dsr": bool(value & MS_DSR_ON),
            "dcd": bool(value & MS_RLSD_ON),
            "ri": bool(value & MS_RING_ON),
        }

    # -- queue -----------------------------------------------------------
    def _comstat(self) -> COMSTAT | None:
        handle = self.handle
        if handle is None:
            return None
        errors = ctypes.c_uint32(0)
        stat = COMSTAT()
        if not self._k32.ClearCommError(handle, ctypes.pointer(errors), ctypes.pointer(stat)):
            return None
        return stat

    def pending_tx(self) -> int | None:
        stat = self._comstat()
        return None if stat is None else int(stat.cbOutQue)

    def drain(self, timeout: float, should_abort: Callable[[], bool] | None) -> bool:
        """Poll ``COMSTAT.cbOutQue`` to zero, then ``FlushFileBuffers``."""
        deadline = time.monotonic() + max(timeout, 0.0)
        pending = self.pending_tx()
        while pending is not None and pending > 0:
            if should_abort is not None and should_abort():
                return False
            if time.monotonic() > deadline or self._stop.is_set():
                return False
            time.sleep(0.02)
            pending = self.pending_tx()
        handle = self.handle
        if handle is None:
            return True
        flush = getattr(self._k32, "FlushFileBuffers", None)
        if flush is not None:
            flush(handle)
        return True

    def send_break(self, duration_s: float) -> None:
        handle = self.handle
        if handle is None:
            return
        if not self._k32.SetCommBreak(handle):
            raise TransportError(f"Could not start a break on {self.device}.")
        time.sleep(max(duration_s, 0.0))
        self._k32.ClearCommBreak(handle)


# ==========================================================================
# The transport
# ==========================================================================

def _make_backend() -> Any:
    return _WindowsSerialBackend() if IS_WINDOWS else _PosixSerialBackend()


class SerialTransport(Transport):
    """A local serial port - onboard UART, USB adapter, or virtual COM."""

    kind = "serial"

    def __init__(self, bus: EventBus | None = None, backend: Any = None) -> None:
        super().__init__(bus=bus)
        self._backend = backend if backend is not None else _make_backend()
        self.device = ""
        self._dtr = True
        self._rts = True
        self._modem = ModemStatus()

    @property
    def backend(self) -> Any:
        """The platform backend. Exposed for tests and the About page."""
        return self._backend

    # -- lifecycle -------------------------------------------------------
    def _do_open(self, machine: dict[str, Any]) -> None:
        device = str(machine.get("serial_device", "")).strip()
        if not device:
            raise TransportError(
                "No serial port is configured for this machine. Pick one from the "
                "port list on the machine's settings page."
            )
        serial_cfg = machine.get("serial", {})
        self._dtr = bool(serial_cfg.get("assert_dtr", True))
        self._rts = bool(serial_cfg.get("assert_rts", True))
        self.device = device
        self._backend.open(device)
        # Configure before the base class calls the set_* hooks, so the port
        # is never briefly live at whatever the last program left behind.
        try:
            self._backend.set_line_params(LineParams.from_machine(machine))
            self._backend.set_flow_control(FlowControl.from_machine(machine))
        except BaseException:
            # Anything at all: the base class only marks itself open *after*
            # _do_open returns, so a failure that skipped this would leave
            # close() a no-op and leak the descriptor for the life of the
            # process. from_machine() can raise ValueError on a bad setting,
            # and a termios failure is not an OSError either.
            self._backend.close()
            raise
        self._backend.purge(rx=True, tx=True)

    def _do_close(self) -> None:
        self._backend.close()

    # -- io --------------------------------------------------------------
    def _do_write(self, data: bytes) -> int:
        return self._backend.write(data, should_abort=lambda: not self.is_open)

    def _do_read(self, n: int, timeout: float) -> bytes:
        return self._backend.read(n, timeout)

    def _do_purge(self, rx: bool, tx: bool) -> None:
        self._backend.purge(rx=rx, tx=tx)

    # -- line control ----------------------------------------------------
    def _do_set_line_params(self, params: LineParams) -> None:
        if self.is_open:
            self._backend.set_line_params(params)

    def _do_set_flow_control(self, flow: FlowControl) -> None:
        if not self.is_open:
            return
        self._backend.set_flow_control(flow)
        # The Windows backend rewrites the whole DCB, and its non-handshake
        # setting for DTR/RTS is "asserted". At open the base class sets the
        # lines right after this; a mid-session change (suspend/resume of
        # XON/XOFF) has nothing after it, so restore the operator's choice.
        mode = str(flow.mode).lower()
        if mode != "dtrdsr":
            self._backend.set_dtr(bool(self._dtr))
        if mode not in ("rtscts", "both"):
            self._backend.set_rts(bool(self._rts))

    def _do_set_dtr(self, state: bool) -> None:
        self._dtr = bool(state)
        self._backend.set_dtr(bool(state))

    def _do_set_rts(self, state: bool) -> None:
        self._rts = bool(state)
        self._backend.set_rts(bool(state))

    def _do_get_modem_status(self) -> ModemStatus:
        lines = self._backend.get_modem_status()
        if lines is None:
            # No modem lines (a pty, or a driver that will not report):
            # keep our own DTR/RTS view so the LEDs still mean something.
            status = ModemStatus(dtr=self._dtr, rts=self._rts)
        else:
            status = ModemStatus(
                cts=bool(lines.get("cts")), dsr=bool(lines.get("dsr")),
                dcd=bool(lines.get("dcd")), ri=bool(lines.get("ri")),
                dtr=self._dtr, rts=self._rts,
            )
        if status != self._modem:
            self._modem = status
            self._emit("transport.modem", {"kind": self.kind, **status.to_dict()})
        return status

    # -- queue / drain ---------------------------------------------------
    def pending_tx(self) -> int | None:
        if not self.is_open:
            return None
        return self._backend.pending_tx()

    def drain(self, timeout: float = 60.0, should_abort: Callable[[], bool] | None = None) -> bool:
        if not self.is_open:
            return True
        return self._backend.drain(timeout, should_abort)

    def send_break(self, duration_s: float = 0.25) -> None:
        self._require_open()
        self._backend.send_break(duration_s)

    # -- capability reporting --------------------------------------------
    def capabilities(self) -> dict[str, Any]:
        open_now = self.is_open
        modem = self._backend.get_modem_status() if open_now else None
        return {
            "data_channel": open_now,
            # There is no separate command channel: the same file handle
            # carries the data and every control operation.
            "command_channel": open_now,
            "can_set_line_params": True,
            "can_read_modem_status": modem is not None,
            "can_set_dtr_rts": True,
            "can_drain": open_now and self._backend.pending_tx() is not None,
            "platform": "windows" if IS_WINDOWS else sys.platform,
            "device": self.device,
            "warnings": list(getattr(self._backend, "warnings", [])),
        }


# ==========================================================================
# Port enumeration
# ==========================================================================

def list_serial_ports() -> list[dict[str, str]]:
    """Every serial port we can see, newest-looking USB adapters first.

    Each entry is ``{"device", "label", "description"}``. Never raises:
    enumeration is a convenience, and a machine can always be configured
    by typing the device name.
    """
    try:
        if IS_WINDOWS:
            ports = _list_windows_ports()
        elif IS_DARWIN:
            ports = _list_darwin_ports()
        else:
            ports = _list_linux_ports()
    except Exception as exc:  # noqa: BLE001 - a broken listing must not break the UI
        log.warning("Serial port enumeration failed: %s", exc, exc_info=True)
        return []
    ports.sort(key=lambda p: (not p.get("usb"), p["device"]))
    for port in ports:
        port.pop("usb", None)
    return ports


def _entry(device: str, description: str = "", usb: bool = False) -> dict[str, Any]:
    # A description that only repeats the node name is noise in a dropdown.
    if description and description.lower() in device.lower():
        description = ""
    label = f"{device} - {description}" if description else device
    return {"device": device, "label": label, "description": description, "usb": usb}


def _run(cmd: list[str], timeout: float = 4.0) -> str:
    """A short-lived helper process, or ``""`` if it is unavailable."""
    try:
        out = subprocess.run(  # fixed argv, never a shell
            cmd, capture_output=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("%s failed: %s", cmd[0], exc)
        return ""
    return out.stdout.decode("utf-8", "replace")


def _list_darwin_ports() -> list[dict[str, Any]]:
    """``/dev/cu.*``, described from ``ioreg`` where it is cheap to do so.

    ``cu.*`` ("call-up") rather than ``tty.*``: opening a ``tty.*`` device
    blocks until DCD is asserted, which a CNC control generally never
    does.
    """
    descriptions = _darwin_ioreg_descriptions()
    ports: list[dict[str, Any]] = []
    for device in sorted(glob.glob("/dev/cu.*")):
        name = device.rsplit("/", 1)[-1]
        if name in ("cu.Bluetooth-Incoming-Port", "cu.debug-console"):
            continue
        usb = bool(re.search(r"usb|serial|uart|ftdi|slab|wch", name, re.I))
        ports.append(_entry(device, descriptions.get(device, ""), usb))
    return ports


def _darwin_ioreg_descriptions() -> dict[str, str]:
    """Map ``/dev/cu.X`` to a USB product name via one ``ioreg`` call."""
    text = _run(["ioreg", "-r", "-c", "IOSerialBSDClient", "-l"])
    if not text:
        return {}
    out: dict[str, str] = {}
    device = ""
    product = ""
    for line in text.splitlines():
        match = re.search(r'"IOCalloutDevice"\s*=\s*"([^"]+)"', line)
        if match:
            device = match.group(1)
            if device and product:
                out.setdefault(device, product)
            continue
        match = re.search(r'"(?:USB Product Name|Product Name)"\s*=\s*"([^"]+)"', line)
        if match:
            product = match.group(1)
            if device:
                out.setdefault(device, product)
        if line.strip() in ("}", "+-o"):
            device = product = ""
    return out


def _list_linux_ports() -> list[dict[str, Any]]:
    """``/dev/serial/by-id`` first (it carries the adapter's own name),
    then the raw ``ttyUSB*``/``ttyACM*``/``ttyS*`` nodes."""
    ports: list[dict[str, Any]] = []
    seen: set[str] = set()
    for link in sorted(glob.glob("/dev/serial/by-id/*")):
        try:
            target = os.path.realpath(link)
        except OSError:
            continue
        if target in seen:
            continue
        seen.add(target)
        ports.append(_entry(target, link.rsplit("/", 1)[-1], usb=True))
    for pattern, usb in (("/dev/ttyUSB*", True), ("/dev/ttyACM*", True), ("/dev/ttyS*", False)):
        for device in sorted(glob.glob(pattern)):
            if device in seen:
                continue
            seen.add(device)
            ports.append(_entry(device, "", usb))
    return ports


def _list_windows_ports() -> list[dict[str, Any]]:
    """``HKLM\\HARDWARE\\DEVICEMAP\\SERIALCOMM`` - every COM port the
    drivers have registered. The value *name* is the driver's device path
    (``\\Device\\Silabser0``), which is the only friendly-ish name
    available without SetupAPI, so it is used as the description."""
    import winreg

    ports: list[dict[str, Any]] = []
    try:
        key = winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DEVICEMAP\SERIALCOMM")
    except OSError:
        return ports
    try:
        index = 0
        while True:
            try:
                name, value, _ = winreg.EnumValue(key, index)
            except OSError:
                break
            index += 1
            device = str(value).strip()
            if not device:
                continue
            driver = str(name).rsplit("\\", 1)[-1]
            usb = bool(re.search(r"usb|ftdi|slab|ch34|prolific|cp21", str(name), re.I))
            ports.append(_entry(device, driver, usb))
    finally:
        key.Close()
    return ports


__all__ = ["SerialTransport", "list_serial_ports"]
