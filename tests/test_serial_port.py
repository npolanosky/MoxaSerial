"""SerialTransport: the POSIX backend against a pty pair, the Windows
backend against a ctypes double, and both engines end-to-end over a pty.

The pty is the closest thing to a serial port that a test can have
without hardware: same ``termios`` calls, same ``select`` behaviour, same
raw byte pipe. It has no modem lines, so the DTR/RTS and CTS/DSR cases
skip themselves rather than pretending.

**No real device is ever opened.** A USB-serial adapter plugged into this
machine may well have a control on the other end of it.
"""

from __future__ import annotations

import ctypes
import os
import threading
import time

import pytest

from moxaserial.config import default_machine, normalize_machine, validate_machine
from moxaserial.transport import create_transport
from moxaserial.transport.base import (
    FlowControl,
    TransportError,
    TransportNotOpen,
)
from moxaserial.transport.serial_port import (
    CLRDTR,
    CLRRTS,
    COMSTAT,
    DCB,
    DTR_CONTROL_ENABLE,
    EVENPARITY,
    MS_CTS_ON,
    MS_DSR_ON,
    MS_RING_ON,
    MS_RLSD_ON,
    ONESTOPBIT,
    PURGE_RXCLEAR,
    PURGE_TXCLEAR,
    RTS_CONTROL_ENABLE,
    RTS_CONTROL_HANDSHAKE,
    SETDTR,
    SETRTS,
    TWOSTOPBITS,
    SerialTransport,
    _PosixSerialBackend,
    _windows_device_path,
    _WindowsSerialBackend,
    list_serial_ports,
)
from tests.conftest import wait_until

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX backend")


# ==========================================================================
# Fixtures
# ==========================================================================

@pytest.fixture
def pty_pair():
    """(master_fd, slave_device_name).

    The original slave descriptor is kept open - on Linux a pty whose
    slave has no openers makes the master read EIO - but nothing ever
    reads from it, so it cannot steal bytes from the transport.
    """
    import pty

    master, slave = pty.openpty()
    name = os.ttyname(slave)
    try:
        yield master, name
    finally:
        for fd in (master, slave):
            try:
                os.close(fd)
            except OSError:
                pass


@pytest.fixture
def serial_machine(pty_pair):
    _, device = pty_pair
    m = default_machine("Pty control", "serial")
    m["serial_device"] = device
    m["serial"].update(
        baud=19200, data_bits=8, parity="none", stop_bits="1", flow_control="none"
    )
    m["send"].update(wait_for_ready="immediate", start_chars="", end_chars="")
    return m


@pytest.fixture
def open_transport(serial_machine):
    made: list[SerialTransport] = []

    def _open(machine=None):
        t = SerialTransport()
        t.open(machine or serial_machine)
        made.append(t)
        return t

    yield _open
    for t in made:
        t.close()


# ==========================================================================
# Config / factory wiring
# ==========================================================================

def test_the_factory_builds_a_serial_transport():
    m = default_machine("Direct", "serial")
    assert create_transport(m).kind == "serial"


def test_a_serial_machine_normalises_and_keeps_its_device():
    m = normalize_machine({"name": "Lathe", "type": "serial", "serial_device": " COM3 "})
    assert m["type"] == "serial"
    assert m["serial_device"] == "COM3"
    assert validate_machine(m) == []


def test_a_serial_machine_without_a_device_is_rejected():
    m = normalize_machine({"name": "Lathe", "type": "serial"})
    problems = validate_machine(m)
    assert any("serial port is required" in p for p in problems), problems


def test_a_serial_machine_does_not_need_a_host():
    m = normalize_machine({"name": "Lathe", "type": "serial", "host": "", "serial_device": "COM1"})
    assert validate_machine(m) == []


def test_opening_without_a_device_is_a_clear_error():
    m = default_machine("Direct", "serial")
    m["serial_device"] = ""
    with pytest.raises(TransportError, match="No serial port"):
        SerialTransport().open(m)


# ==========================================================================
# POSIX backend against a pty
# ==========================================================================

@posix_only
def test_open_close_and_reopen(serial_machine, pty_pair):
    t = SerialTransport()
    t.open(serial_machine)
    assert t.is_open and t.device == pty_pair[1]
    t.close()
    assert not t.is_open
    t.close()  # idempotent
    t.open(serial_machine)
    assert t.is_open
    t.close()


@posix_only
def test_operations_before_open_are_refused():
    t = SerialTransport()
    with pytest.raises(TransportNotOpen):
        t.write(b"x")
    with pytest.raises(TransportNotOpen):
        t.read(1)


@posix_only
def test_opening_a_device_that_is_not_there():
    m = default_machine("Ghost", "serial")
    m["serial_device"] = "/dev/cu.definitely-not-a-port"
    with pytest.raises(TransportError, match="Could not open serial port"):
        SerialTransport().open(m)


@posix_only
def test_line_parameters_reach_the_tty(serial_machine, open_transport):
    import termios

    serial_machine["serial"].update(
        baud=19200, data_bits=7, parity="even", stop_bits="2"
    )
    t = open_transport(serial_machine)
    attrs = termios.tcgetattr(t.backend.fd)
    cflag = attrs[2]
    assert attrs[4] == attrs[5]                       # in and out speeds agree
    assert (cflag & termios.CSIZE) == termios.CS7
    assert cflag & termios.PARENB and not cflag & termios.PARODD   # even
    assert cflag & termios.CSTOPB                     # 2 stop bits
    assert cflag & termios.CLOCAL and cflag & termios.CREAD
    assert not attrs[3] & termios.ECHO                # raw: no echo, ever
    assert t.line_params.baud == 19200


@posix_only
def test_odd_parity_and_one_stop_bit(serial_machine, open_transport):
    import termios

    serial_machine["serial"].update(data_bits=8, parity="odd", stop_bits="1")
    t = open_transport(serial_machine)
    cflag = termios.tcgetattr(t.backend.fd)[2]
    assert cflag & termios.PARENB and cflag & termios.PARODD
    assert not cflag & termios.CSTOPB
    assert (cflag & termios.CSIZE) == termios.CS8


@posix_only
def test_an_impossible_baud_rate_is_reported_not_ignored(serial_machine):
    serial_machine["serial"]["baud"] = 9601
    t = SerialTransport()
    if t.backend.__class__ is _PosixSerialBackend and os.uname().sysname == "Darwin":
        # Darwin takes the literal rate, so 9601 is the driver's problem,
        # not ours - it either works or tcsetattr fails loudly.
        pytest.skip("Darwin passes arbitrary rates straight to the driver")
    with pytest.raises(TransportError):
        t.open(serial_machine)


@posix_only
def test_software_flow_control_sets_ixon_ixoff_and_the_characters(
    serial_machine, open_transport
):
    import termios

    serial_machine["serial"].update(flow_control="xonxoff", xon_char=0x11, xoff_char=0x13)
    t = open_transport(serial_machine)
    attrs = termios.tcgetattr(t.backend.fd)
    assert attrs[0] & termios.IXON and attrs[0] & termios.IXOFF
    assert attrs[6][termios.VSTART] == b"\x11"
    assert attrs[6][termios.VSTOP] == b"\x13"


@posix_only
def test_hardware_flow_control_sets_crtscts(serial_machine, open_transport):
    import termios

    crtscts = getattr(termios, "CRTSCTS", None)
    if crtscts is None:
        pytest.skip("no CRTSCTS on this platform")
    serial_machine["serial"]["flow_control"] = "rtscts"
    t = open_transport(serial_machine)
    assert termios.tcgetattr(t.backend.fd)[2] & crtscts
    # ...and switching back clears it again.
    t.set_flow_control(FlowControl(mode="none"))
    assert not termios.tcgetattr(t.backend.fd)[2] & crtscts


@posix_only
def test_dtr_dsr_flow_control_warns_where_the_driver_cannot_do_it(
    serial_machine, open_transport
):
    serial_machine["serial"]["flow_control"] = "dtrdsr"
    t = open_transport(serial_machine)
    caps = t.capabilities()
    # Either the driver can do it (macOS CDTR_IFLOW) or we said so out loud.
    import termios

    supported = getattr(termios, "CDTR_IFLOW", 0) or getattr(termios, "CDSR_OFLOW", 0)
    assert supported or any("DTR/DSR" in w for w in caps["warnings"])


@posix_only
def test_bytes_written_arrive_at_the_other_end(pty_pair, open_transport):
    master, _ = pty_pair
    t = open_transport()
    assert t.write(b"N10 G0 X1\r\n") == 11
    assert wait_until(lambda: True, 0.05)
    assert os.read(master, 64) == b"N10 G0 X1\r\n"
    assert t.stats.bytes_written == 11


@posix_only
def test_bytes_from_the_control_are_read_back(pty_pair, open_transport):
    master, _ = pty_pair
    t = open_transport()
    os.write(master, b"%\r\n")
    got = bytearray()
    deadline = time.monotonic() + 2
    while len(got) < 3 and time.monotonic() < deadline:
        got.extend(t.read(64, 0.2))
    assert bytes(got) == b"%\r\n"
    assert t.stats.bytes_read == 3


@posix_only
def test_read_returns_empty_on_timeout_rather_than_raising(open_transport):
    t = open_transport()
    started = time.monotonic()
    assert t.read(64, timeout=0.2) == b""
    assert time.monotonic() - started >= 0.15


@posix_only
def test_a_zero_timeout_read_does_not_block(open_transport):
    t = open_transport()
    started = time.monotonic()
    assert t.read(64, timeout=0.0) == b""
    assert time.monotonic() - started < 0.2


@posix_only
def test_close_releases_a_blocked_reader(open_transport):
    t = open_transport()
    result: list[bytes] = []

    def reader():
        result.append(t.read(64, timeout=10.0))

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.15)
    started = time.monotonic()
    t.close()
    thread.join(timeout=3)
    assert not thread.is_alive(), "read() did not return after close()"
    assert result == [b""]
    assert time.monotonic() - started < 2.0, "close() took too long to unblock the read"


@posix_only
def test_opening_a_non_tty_reports_it_and_leaks_no_descriptor():
    """termios.error is not an OSError, so a handler naming only OSError lets
    it escape with the descriptor still open - and because the base class
    marks itself open only after _do_open returns, close() is then a no-op."""
    before = set(os.listdir("/dev/fd"))
    backend = _PosixSerialBackend()
    with pytest.raises(TransportError, match="not a serial port"):
        backend.open("/dev/null")
    leaked = set(os.listdir("/dev/fd")) - before
    assert not leaked, f"open() leaked file descriptors {sorted(leaked)}"


@posix_only
def test_two_threads_closing_at_once_do_not_double_close(pty_pair):
    """The self-pipe descriptors must be taken under the same lock as the
    port fd: a second os.close can land on a number the kernel has already
    handed to another part of Fusion."""
    _master, device = pty_pair
    backend = _PosixSerialBackend()
    backend.open(device)

    closed: list[int] = []
    real_close = os.close

    def spy(fd: int) -> None:
        closed.append(fd)
        return real_close(fd)

    os.close = spy
    try:
        threads = [threading.Thread(target=backend.close) for _ in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=3)
    finally:
        os.close = real_close

    dupes = sorted({fd for fd in closed if closed.count(fd) > 1})
    assert not dupes, f"descriptors closed twice: {dupes}"


@posix_only
def test_purge_drops_what_the_control_already_sent(pty_pair, open_transport):
    master, _ = pty_pair
    t = open_transport()
    os.write(master, b"stale bytes")
    time.sleep(0.1)
    t.purge(rx=True, tx=False)
    assert t.read(64, 0.1) == b""


@posix_only
def test_pending_tx_and_drain(pty_pair, open_transport):
    """drain() means "the bytes have left the port", not "we handed them over".

    On a pty the queue only empties when the far end reads, which is
    exactly the property the send engine relies on: DONE must not arrive
    before the control has the program.
    """
    master, _ = pty_pair
    t = open_transport()
    t.write(b"x" * 64)
    pending = t.pending_tx()
    assert pending is None or pending >= 0      # TIOCOUTQ is optional
    got = bytearray()

    def control():
        time.sleep(0.15)
        while len(got) < 64:
            got.extend(os.read(master, 128))

    reader = threading.Thread(target=control, daemon=True)
    reader.start()
    assert t.drain(timeout=10.0) is True
    reader.join(timeout=5)
    assert bytes(got) == b"x" * 64


@posix_only
def test_drain_gives_up_when_the_far_end_never_reads(open_transport):
    t = open_transport()
    t.write(b"y" * 64)          # nobody is reading the master
    if t.pending_tx() in (None, 0):
        pytest.skip("this platform does not report the output queue")
    assert t.drain(timeout=0.5) is False
    assert t.drain(timeout=5.0, should_abort=lambda: True) is False


@posix_only
def test_drain_returns_at_once_with_an_empty_queue(open_transport):
    t = open_transport()
    started = time.monotonic()
    assert t.drain(timeout=5.0, should_abort=lambda: False) is True
    assert time.monotonic() - started < 1.0


@posix_only
def test_dtr_rts_and_modem_status(pty_pair, open_transport):
    master, _ = pty_pair
    t = open_transport()
    # A pty has no modem lines. Where the platform does support them the
    # readback must agree with what we just set; where it does not, the
    # transport still has to answer with our own DTR/RTS view.
    t.set_dtr(True)
    t.set_rts(False)
    status = t.get_modem_status()
    assert status.dtr is True and status.rts is False
    if t.backend.get_modem_status() is None:
        pytest.skip("this device has no modem lines (a pty never does)")
    t.set_dtr(False)
    assert t.get_modem_status().dtr is False


@posix_only
def test_capabilities_describe_a_direct_port(open_transport):
    t = open_transport()
    caps = t.capabilities()
    assert caps["data_channel"] is True
    assert caps["command_channel"] is True       # one handle does everything
    assert caps["can_set_line_params"] is True
    assert caps["can_set_dtr_rts"] is True
    assert isinstance(caps["can_read_modem_status"], bool)
    assert caps["device"]


@posix_only
def test_send_break_does_not_raise(open_transport):
    t = open_transport()
    t.send_break(0.01)


@posix_only
def test_describe_snapshot_shape(open_transport):
    t = open_transport()
    info = t.describe()
    assert info["kind"] == "serial"
    assert info["open"] is True
    assert set(info["modem"]) == {"cts", "dsr", "dcd", "ri", "dtr", "rts"}


@posix_only
def test_a_second_transport_cannot_steal_an_open_port(serial_machine, open_transport):
    open_transport(serial_machine)
    other = SerialTransport()
    with pytest.raises(TransportError, match="already in use|Could not open"):
        other.open(serial_machine)


@posix_only
def test_writing_after_close_is_refused(open_transport):
    t = open_transport()
    t.close()
    with pytest.raises(TransportNotOpen):
        t.write(b"x")


# ==========================================================================
# Port enumeration
# ==========================================================================

def test_list_serial_ports_returns_a_list_of_entries():
    ports = list_serial_ports()
    assert isinstance(ports, list)
    for port in ports:
        assert set(port) == {"device", "label", "description"}
        assert port["device"]
        assert port["device"] in port["label"]


def test_list_serial_ports_never_raises(monkeypatch):
    import moxaserial.transport.serial_port as sp

    monkeypatch.setattr(sp, "_run", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    assert isinstance(sp.list_serial_ports(), list)


def test_the_bridge_publishes_the_port_list(bus, tmp_path):
    from moxaserial.bridge import Bridge

    reply = Bridge(bus=bus).handle("serial.listPorts", {})
    assert reply["ok"] is True
    assert isinstance(reply["data"]["ports"], list)


# ==========================================================================
# Windows backend against a ctypes double
# ==========================================================================

class FakeKernel32:
    """Records every kernel32 call the Windows backend makes and answers
    it the way the real API would. The backend passes ``ctypes.pointer``
    rather than ``byref``, so this double can read and fill structures."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.path = ""
        self.handle = 0x1234
        self.create_fails = False
        self.dcb: dict[str, int] = {}
        self.timeouts: list[int] = []
        self.rx = bytearray()
        self.written = bytearray()
        self.escapes: list[int] = []
        self.purges: list[int] = []
        self.modem_bits = 0
        self.out_queue = 0
        self.breaks: list[str] = []
        self.closed = False
        self.last_error = 5

    # -- lifecycle
    def CreateFileW(self, path, access, share, sa, disposition, flags, template):
        self.calls.append("CreateFileW")
        self.path = path.value
        self.access = access
        self.share = share
        self.disposition = disposition
        return 0 if self.create_fails else self.handle

    def CloseHandle(self, handle):
        self.calls.append("CloseHandle")
        self.closed = True
        return 1

    def GetLastError(self):
        return self.last_error

    # -- settings
    def GetCommState(self, handle, pdcb):
        self.calls.append("GetCommState")
        for key, value in self.dcb.items():
            setattr(pdcb.contents, key, value)
        return 1

    def SetCommState(self, handle, pdcb):
        self.calls.append("SetCommState")
        dcb = pdcb.contents
        self.dcb = {
            name: getattr(dcb, name)
            for name, *_ in DCB._fields_
            if not name.startswith(("wReserved", "fDummy"))
        }
        return 1

    def SetCommTimeouts(self, handle, ptimeouts):
        self.calls.append("SetCommTimeouts")
        self.timeouts.append(ptimeouts.contents.ReadTotalTimeoutConstant)
        return 1

    # -- io
    def ReadFile(self, handle, buf, n, pgot, overlapped):
        self.calls.append("ReadFile")
        count = min(int(n.value), len(self.rx))
        if count:
            ctypes.memmove(buf, bytes(self.rx[:count]), count)
            del self.rx[:count]
        pgot.contents.value = count
        return 1

    def WriteFile(self, handle, buf, n, pwritten, overlapped):
        self.calls.append("WriteFile")
        count = int(n.value)
        self.written += buf.raw[:count]
        pwritten.contents.value = count
        return 1

    def PurgeComm(self, handle, flags):
        self.calls.append("PurgeComm")
        self.purges.append(int(flags.value))
        return 1

    def FlushFileBuffers(self, handle):
        self.calls.append("FlushFileBuffers")
        return 1

    # -- lines
    def EscapeCommFunction(self, handle, func):
        self.calls.append("EscapeCommFunction")
        self.escapes.append(int(func.value))
        return 1

    def GetCommModemStatus(self, handle, pbits):
        self.calls.append("GetCommModemStatus")
        pbits.contents.value = self.modem_bits
        return 1

    def ClearCommError(self, handle, perrors, pstat):
        self.calls.append("ClearCommError")
        perrors.contents.value = 0
        pstat.contents.cbOutQue = self.out_queue
        return 1

    def SetCommBreak(self, handle):
        self.calls.append("SetCommBreak")
        self.breaks.append("set")
        return 1

    def ClearCommBreak(self, handle):
        self.calls.append("ClearCommBreak")
        self.breaks.append("clear")
        return 1


@pytest.fixture
def win_transport():
    """A SerialTransport driven by the fake kernel32, plus the double."""
    k32 = FakeKernel32()

    def _open(**serial):
        m = default_machine("Windows control", "serial")
        m["serial_device"] = "COM3"
        m["serial"].update(serial)
        t = SerialTransport(backend=_WindowsSerialBackend(kernel32=k32))
        t.open(m)
        return t, k32

    return _open


def test_windows_device_path_prefixes_double_digit_ports():
    assert _windows_device_path("COM3") == r"\\.\COM3"
    assert _windows_device_path("com12") == r"\\.\COM12"
    assert _windows_device_path(r"\\.\COM12") == r"\\.\COM12"
    assert _windows_device_path("/dev/cu.usb") == "/dev/cu.usb"


def test_windows_open_uses_createfilew_exclusively(win_transport):
    t, k32 = win_transport()
    assert k32.path == r"\\.\COM3"
    assert k32.share == 0                      # a serial port has one owner
    assert k32.disposition == 3                # OPEN_EXISTING
    assert "SetCommTimeouts" in k32.calls
    assert t.is_open
    t.close()
    assert k32.closed is True


def test_windows_open_failure_is_reported():
    k32 = FakeKernel32()
    k32.create_fails = True
    m = default_machine("Windows control", "serial")
    m["serial_device"] = "COM9"
    t = SerialTransport(backend=_WindowsSerialBackend(kernel32=k32))
    with pytest.raises(TransportError, match="Could not open serial port COM9"):
        t.open(m)
    assert not t.is_open


def test_windows_dcb_carries_the_line_parameters(win_transport):
    t, k32 = win_transport(baud=19200, data_bits=7, parity="even", stop_bits="2")
    assert k32.dcb["BaudRate"] == 19200
    assert k32.dcb["ByteSize"] == 7
    assert k32.dcb["Parity"] == EVENPARITY
    assert k32.dcb["StopBits"] == TWOSTOPBITS
    assert k32.dcb["fBinary"] == 1
    t.close()


def test_windows_software_flow_control_sets_foutx_finx(win_transport):
    t, k32 = win_transport(flow_control="xonxoff", xon_char=0x11, xoff_char=0x13)
    assert k32.dcb["fOutX"] == 1 and k32.dcb["fInX"] == 1
    assert k32.dcb["XonChar"] == b"\x11" and k32.dcb["XoffChar"] == b"\x13"
    assert k32.dcb["fOutxCtsFlow"] == 0
    assert k32.dcb["fRtsControl"] == RTS_CONTROL_ENABLE
    t.close()


def test_windows_hardware_flow_control_sets_cts_and_rts_handshake(win_transport):
    t, k32 = win_transport(flow_control="rtscts")
    assert k32.dcb["fOutxCtsFlow"] == 1
    assert k32.dcb["fRtsControl"] == RTS_CONTROL_HANDSHAKE
    assert k32.dcb["fOutX"] == 0 and k32.dcb["fInX"] == 0
    t.close()


def test_windows_dtr_dsr_flow_control_sets_the_dsr_bits(win_transport):
    t, k32 = win_transport(flow_control="dtrdsr")
    assert k32.dcb["fOutxDsrFlow"] == 1
    assert k32.dcb["fDsrSensitivity"] == 1
    t.close()


def test_windows_no_flow_control_leaves_dtr_asserted(win_transport):
    """With no handshake the driver must hold DTR up rather than drive it:
    a control watching DSR would otherwise see the line drop."""
    t, k32 = win_transport(flow_control="none", stop_bits="1")
    assert k32.dcb["fDtrControl"] == DTR_CONTROL_ENABLE
    assert k32.dcb["fOutxDsrFlow"] == 0 and k32.dcb["fDsrSensitivity"] == 0
    assert k32.dcb["StopBits"] == ONESTOPBIT
    t.close()


def test_windows_dtr_and_rts_use_escapecommfunction(win_transport):
    t, k32 = win_transport()
    k32.escapes.clear()
    t.set_dtr(True)
    t.set_rts(True)
    t.set_dtr(False)
    t.set_rts(False)
    assert k32.escapes == [SETDTR, SETRTS, CLRDTR, CLRRTS]
    t.close()


def test_windows_modem_status_decodes_every_line(win_transport):
    t, k32 = win_transport()
    k32.modem_bits = MS_CTS_ON | MS_DSR_ON | MS_RING_ON | MS_RLSD_ON
    status = t.get_modem_status()
    assert (status.cts, status.dsr, status.ri, status.dcd) == (True, True, True, True)
    k32.modem_bits = MS_DSR_ON
    status = t.get_modem_status()
    assert status.cts is False and status.dsr is True and status.dcd is False
    t.close()


def test_windows_write_and_read_round_trip(win_transport):
    t, k32 = win_transport()
    assert t.write(b"N10 G0\r\n") == 8
    assert k32.written == b"N10 G0\r\n"
    k32.rx += b"%\r\n"
    assert t.read(64, 0.5) == b"%\r\n"
    assert t.read(64, 0.05) == b""       # nothing left: a timeout, not an error
    t.close()


def test_windows_pending_tx_and_drain_poll_the_out_queue(win_transport):
    t, k32 = win_transport()
    k32.out_queue = 40
    assert t.pending_tx() == 40

    def empty_it():
        time.sleep(0.1)
        k32.out_queue = 0

    threading.Thread(target=empty_it, daemon=True).start()
    assert t.drain(timeout=5.0) is True
    assert "FlushFileBuffers" in k32.calls
    t.close()


def test_windows_drain_gives_up_when_asked_to_abort(win_transport):
    t, k32 = win_transport()
    k32.out_queue = 999
    assert t.drain(timeout=5.0, should_abort=lambda: True) is False
    t.close()


def test_windows_purge_maps_both_directions(win_transport):
    t, k32 = win_transport()
    k32.purges.clear()
    t.purge(rx=True, tx=False)
    t.purge(rx=False, tx=True)
    assert k32.purges[0] & PURGE_RXCLEAR and not k32.purges[0] & PURGE_TXCLEAR
    assert k32.purges[1] & PURGE_TXCLEAR and not k32.purges[1] & PURGE_RXCLEAR
    t.close()


def test_windows_break_is_set_then_cleared(win_transport):
    t, k32 = win_transport()
    t.send_break(0.01)
    assert k32.breaks == ["set", "clear"]
    t.close()


def test_windows_comstat_and_dcb_are_the_right_size():
    # A wrong DCBlength makes SetCommState fail on the real API, and the
    # bitfields must still add up to one 32-bit flag word.
    assert ctypes.sizeof(DCB) >= 28
    assert ctypes.sizeof(COMSTAT) == 12


# ==========================================================================
# End to end over the pty: the real engines, the real transport
# ==========================================================================

PROGRAM = "".join(f"N{n} G01 X{n}.0 Y{n}.0 F500.\n" for n in range(1, 31))


@posix_only
def test_send_over_a_serial_port_with_xon_xoff(bus, serial_machine, pty_pair):
    """The send engine, the real SerialTransport and a 'control' that
    stops the flow with XOFF half way through and releases it again."""
    from moxaserial.dnc.sender import Sender, SendState

    master, _ = pty_pair
    serial_machine["serial"]["flow_control"] = "xonxoff"
    serial_machine["send"].update(line_ending="LF", handshake_timeout_s=10)

    received = bytearray()
    stop = threading.Event()
    xoff_sent = threading.Event()

    def control():
        """Read continuously - a real control drains its UART - and throttle
        once, so the XON/XOFF path is exercised rather than described."""
        import select as _select

        while not stop.is_set():
            ready, _, _ = _select.select([master], [], [], 0.1)
            if not ready:
                continue
            try:
                chunk = os.read(master, 256)
            except OSError:
                return
            if not chunk:
                return
            received.extend(chunk)
            if len(received) > 200 and not xoff_sent.is_set():
                xoff_sent.set()
                os.write(master, b"\x13")      # XOFF
                time.sleep(0.2)
                os.write(master, b"\x11")      # XON

    thread = threading.Thread(target=control, daemon=True)
    thread.start()
    transport = SerialTransport(bus=bus)
    sender = Sender(bus)
    try:
        assert sender.start(serial_machine, text=PROGRAM, transport=transport)
        assert wait_until(lambda: not sender.is_running, 30.0), (
            f"send did not finish (state={sender.state})"
        )
        assert sender.state is SendState.DONE, sender.snapshot().get("error")
        assert wait_until(lambda: bytes(received) == PROGRAM.encode(), 5.0), (
            f"got {len(received)} of {len(PROGRAM)} bytes"
        )
        assert xoff_sent.is_set(), "the control never got far enough to throttle"
    finally:
        stop.set()
        thread.join(timeout=2)
        transport.close()


@posix_only
def test_receive_over_a_serial_port(bus, serial_machine, pty_pair, tmp_path):
    """The receive engine capturing a program punched out of the pty."""
    from moxaserial.dnc.receiver import Receiver, ReceiveState

    master, _ = pty_pair
    payload = b"%\r\nO0042 (PUNCHED OUT)\r\nN10 G0 X1\r\nN20 M30\r\n%\r\n"
    serial_machine["receive"].update(
        folder=str(tmp_path / "in"),
        filename_pattern="{program}.nc",
        overwrite="rename",
        idle_timeout_s=1,
        overall_timeout_s=25,
        start_trigger="",
        end_trigger="",
        line_ending="AUTO",
    )
    transport = SerialTransport(bus=bus)
    receiver = Receiver(bus)
    try:
        assert receiver.start(serial_machine, transport=transport)
        assert wait_until(lambda: transport.is_open, 5.0)
        os.write(master, payload)
        assert wait_until(lambda: not receiver.is_running, 30.0), (
            f"receive did not finish (state={receiver.state})"
        )
        assert receiver.state is ReceiveState.DONE, receiver.snapshot().get("error")
        saved = list((tmp_path / "in").glob("*.nc"))
        assert len(saved) == 1
        text = saved[0].read_text()
        assert "O0042" in text and "M30" in text
    finally:
        transport.close()
