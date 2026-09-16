"""Transport base contract and the FakeTransport simulation."""

from __future__ import annotations

import threading
import time

import pytest

from moxaserial.config import default_machine, simulator_machine
from moxaserial.transport import create_transport
from moxaserial.transport.base import (
    FlowControl,
    LineParams,
    ModemStatus,
    Transport,
    TransportNotOpen,
)
from moxaserial.transport.fake import FakeProfile, FakeTransport


def test_factory_picks_the_right_transport():
    assert create_transport(simulator_machine()).kind == "simulator"
    assert create_transport(default_machine()).kind == "moxa"


def test_line_params_and_flow_control_from_machine():
    m = default_machine()
    m["serial"].update(baud=19200, parity="even", stop_bits="2", flow_control="both")
    assert LineParams.from_machine(m).baud == 19200
    assert LineParams.from_machine(m).parity == "even"
    flow = FlowControl.from_machine(m)
    assert flow.software and flow.hardware


@pytest.mark.parametrize(
    "mode,software,hardware",
    [
        ("none", False, False),
        ("xonxoff", True, False),
        ("rtscts", False, True),
        ("dtrdsr", False, True),
        ("both", True, True),
    ],
)
def test_flow_control_classification(mode, software, hardware):
    flow = FlowControl(mode=mode)
    assert flow.software is software
    assert flow.hardware is hardware


def test_operations_before_open_are_refused():
    t = FakeTransport()
    with pytest.raises(TransportNotOpen):
        t.write(b"x")
    with pytest.raises(TransportNotOpen):
        t.read(1)


def test_open_applies_line_settings_and_control_lines():
    m = simulator_machine()
    m["serial"].update(baud=19200, assert_dtr=False, assert_rts=True)
    t = FakeTransport()
    t.open(m)
    kinds = dict(t.control_log)
    assert kinds["line"]["baud"] == 19200
    assert kinds["dtr"] is False
    assert kinds["rts"] is True
    t.close()


def test_open_is_idempotent_and_close_is_safe_twice():
    t = FakeTransport()
    m = simulator_machine()
    t.open(m)
    t.open(m)
    assert t.is_open
    t.close()
    t.close()
    assert not t.is_open


def test_context_manager_closes():
    t = FakeTransport()
    t.open(simulator_machine())
    with t:
        assert t.is_open
    assert not t.is_open


def test_stats_track_both_directions():
    t = FakeTransport()
    t.open(simulator_machine())
    t.write(b"hello")
    t.inject(b"world!")
    assert t.read(10, 0.5) == b"world!"
    assert t.stats.bytes_written == 5
    assert t.stats.bytes_read == 6
    assert t.stats.last_tx > 0 and t.stats.last_rx > 0
    t.close()


def test_read_returns_empty_on_timeout_rather_than_raising():
    t = FakeTransport()
    t.open(simulator_machine())
    started = time.monotonic()
    assert t.read(10, timeout=0.15) == b""
    assert time.monotonic() - started >= 0.1
    t.close()


def test_purge_drops_pending_receive_data():
    t = FakeTransport()
    t.open(simulator_machine())
    t.inject(b"stale")
    t.purge(rx=True, tx=False)
    assert t.read(10, 0.05) == b""
    t.close()


def test_describe_snapshot_shape():
    t = FakeTransport()
    t.open(simulator_machine())
    info = t.describe()
    assert info["kind"] == "simulator"
    assert info["open"] is True
    assert set(info["modem"]) == {"cts", "dsr", "dcd", "ri", "dtr", "rts"}
    t.close()


# -- simulation behaviours --------------------------------------------------

def test_modem_lines_come_up_after_the_ready_delay():
    t = FakeTransport(profile=FakeProfile(ready_delay_s=0.3))
    t.open(simulator_machine())
    assert t.get_modem_status().cts is False
    time.sleep(0.35)
    assert t.get_modem_status().cts is True
    t.close()


def test_modem_lines_can_be_forced_low():
    t = FakeTransport()
    t.open(simulator_machine())
    t.set_modem(cts=False, dsr=False)
    status = t.get_modem_status()
    assert status.cts is False and status.dsr is False and status.dcd is True
    t.close()


def test_small_buffer_produces_xoff_then_xon():
    m = simulator_machine()
    m["serial"]["flow_control"] = "xonxoff"
    t = FakeTransport(profile=FakeProfile(buffer_size=32, drain_bytes_per_s=1000.0))
    t.open(m)
    t.write(b"x" * 64)
    assert bytes([t.flow_control.xoff]) in t.read(16, 0.2)

    assert any(
        bytes([t.flow_control.xon]) in t.read(16, 0.2) for _ in range(20)
    ), "the simulated control never released XOFF"
    t.close()


def test_no_flow_control_means_no_control_characters():
    m = simulator_machine()
    m["serial"]["flow_control"] = "none"
    t = FakeTransport(profile=FakeProfile(buffer_size=8, drain_bytes_per_s=1.0))
    t.open(m)
    t.write(b"x" * 64)
    assert t.read(16, 0.1) == b""
    t.close()


def test_echo_mode_returns_what_was_written():
    t = FakeTransport(profile=FakeProfile(echo=True))
    t.open(simulator_machine())
    t.write(b"ABC")
    assert t.read(8, 0.5) == b"ABC"
    t.close()


def test_realtime_mode_takes_roughly_the_wire_time():
    m = simulator_machine()
    m["serial"]["baud"] = 1200  # 120 bytes/s
    t = FakeTransport(profile=FakeProfile(realtime=True))
    t.open(m)
    started = time.monotonic()
    t.write(b"x" * 60)  # ~0.5 s
    assert time.monotonic() - started >= 0.4
    t.close()


def test_injected_open_failure():
    from moxaserial.transport.base import TransportError

    t = FakeTransport(profile=FakeProfile(fail_on_open="no such host"))
    with pytest.raises(TransportError, match="no such host"):
        t.open(simulator_machine())
    assert not t.is_open


def test_injected_write_failure_after_n_bytes():
    from moxaserial.transport.base import TransportError

    t = FakeTransport(profile=FakeProfile(fail_after_bytes=10, fail_message="wire cut"))
    t.open(simulator_machine())
    t.write(b"x" * 8)
    with pytest.raises(TransportError, match="wire cut"):
        t.write(b"x" * 8)
    t.close()


def test_connection_drop_after_n_bytes():
    t = FakeTransport(profile=FakeProfile(drop_after_bytes=10))
    t.open(simulator_machine())
    t.write(b"x" * 12)
    assert not t.is_open


def test_punch_out_delivers_the_whole_program():
    payload = b"%\r\nO1\r\nM30\r\n%\r\n"
    t = FakeTransport(profile=FakeProfile(outgoing_delay_s=0.0, outgoing_gap_s=0.0))
    t.open(simulator_machine())
    t.arm_receive(payload)
    got = bytearray()
    deadline = time.monotonic() + 3
    while len(got) < len(payload) and time.monotonic() < deadline:
        got.extend(t.read(64, 0.1))
    assert bytes(got) == payload
    t.close()


def test_close_releases_a_blocked_reader():
    t = FakeTransport()
    t.open(simulator_machine())
    result = []

    def reader():
        result.append(t.read(16, timeout=5.0))

    thread = threading.Thread(target=reader)
    thread.start()
    time.sleep(0.1)
    t.close()
    thread.join(timeout=3)
    assert not thread.is_alive(), "read() did not return after close()"
    assert result == [b""]


# -- Moxa stub --------------------------------------------------------------

def test_moxa_transport_reports_capabilities_before_open():
    from moxaserial.transport.moxa import MoxaTransport

    t = MoxaTransport()
    caps = t.capabilities()
    # Nothing is connected yet, so the command channel is not usable...
    assert caps["command_channel"] is False
    assert caps["can_read_modem_status"] is False
    # ...but the protocol itself is implemented.
    assert caps["aspp"]["implemented"] is True
    assert caps["aspp"]["verified_on_hardware"] is True


def test_moxa_transport_refuses_an_empty_host():
    from moxaserial.transport.base import TransportError
    from moxaserial.transport.moxa import MoxaTransport

    m = default_machine()
    m["host"] = ""
    with pytest.raises(TransportError, match="No host"):
        MoxaTransport().open(m)


def test_aspp_port_helpers():
    from moxaserial.transport import aspp

    assert aspp.data_port_for(1) == 4001
    assert aspp.data_port_for(4) == 4004
    assert aspp.cmd_port_for(1) == 966
    assert aspp.cmd_port_for(2) == 967


def test_aspp_framing_roundtrip():
    from moxaserial.transport import aspp

    req = aspp.encode_port_init(9600, 8, "none", "1", True, True, False, False, False)
    assert req[:2] == bytes([aspp.Cmd.PORT_INIT, 8])
    frames, rest = aspp.split_frames(bytes([aspp.Cmd.PORT_INIT, 3, 1, 1, 0]))
    assert rest == b"" and aspp.decode_lines(frames[0]) == {"dsr": True, "cts": True, "dcd": False}


# -- base class contract ----------------------------------------------------

def test_a_minimal_subclass_gets_working_defaults():
    class Minimal(Transport):
        kind = "minimal"

        def _do_open(self, machine):
            self.opened = True

        def _do_close(self):
            self.closed = True

        def _do_write(self, data):
            return len(data)

        def _do_read(self, n, timeout):
            return b""

    t = Minimal()
    t.open(simulator_machine())
    assert t.write(b"abc") == 3
    assert t.get_modem_status() == ModemStatus()
    t.purge()
    t.close()
    assert t.closed is True
