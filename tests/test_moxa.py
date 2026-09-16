"""MoxaTransport against the NPort ASPP simulator (tools/nport_sim.py)."""

from __future__ import annotations

import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from moxaserial.config import default_machine  # noqa: E402
from moxaserial.dnc.sender import Sender, SendState  # noqa: E402
from moxaserial.events import EventBus  # noqa: E402
from moxaserial.transport.base import TransportError  # noqa: E402
from moxaserial.transport.moxa import MoxaTransport  # noqa: E402
from tools.nport_sim import NPortSimulator  # noqa: E402


@pytest.fixture
def sim():
    with NPortSimulator(polling_interval=0.2, alive_timeout=2.0, instant=True) as s:
        yield s


def machine_for(sim: NPortSimulator, port: int = 0, **serial) -> dict:
    m = default_machine("Sim NPort", "moxa")
    m["host"] = "127.0.0.1"
    m["port_index"] = port + 1
    m["data_port"] = sim.data_ports[port]
    m["cmd_port"] = sim.cmd_ports[port]
    m["connect_timeout_s"] = 2
    m["serial"].update({"baud": 9600, "data_bits": 7, "parity": "even", "stop_bits": "2"})
    m["serial"].update(serial)
    return m


def wait_until(pred, timeout=3.0, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(step)
    return pred()


def test_open_applies_line_settings_and_reads_modem(sim):
    port = sim.ports[0]
    port.cnc.cts = True
    port.cnc.dsr = False
    m = machine_for(sim, flow_control="rtscts", assert_dtr=False, assert_rts=True, tx_fifo=1)
    t = MoxaTransport()
    t.open(m)
    try:
        assert t.command_channel_available
        assert port.settings.baud == 9600
        assert port.settings.data_bits == 7
        assert port.settings.parity == "E"
        assert port.settings.stop_bits == 2
        assert port.settings.hw_flow_a and port.settings.hw_flow_b
        assert not port.settings.xon_enabled
        assert port.settings.dtr is False and port.settings.rts is True
        assert port.settings.tx_fifo == 1
        assert port.port_init_count == 1  # base.open() re-calls were no-ops
        ms = t.get_modem_status()
        assert ms.cts is True and ms.dsr is False
        caps = t.capabilities()
        assert caps["can_read_modem_status"] and caps["can_set_line_params"]
    finally:
        t.close()
    assert not t.command_channel_available


def test_custom_baud_uses_setbaud(sim):
    m = machine_for(sim, baud=12345)
    t = MoxaTransport()
    t.open(m)
    try:
        assert sim.ports[0].settings.baud == 12345
        ops = [op for op, _ in sim.ports[0].command_log]
        assert 23 in ops  # SETBAUD
    finally:
        t.close()


def test_software_flow_sets_xonxoff_chars_on_device(sim):
    m = machine_for(sim, flow_control="xonxoff", xon_char=0x11, xoff_char=0x13)
    t = MoxaTransport()
    t.open(m)
    try:
        s = sim.ports[0].settings
        assert s.xon_enabled and s.xoff_enabled
        assert (s.xon_char, s.xoff_char) == (0x11, 0x13)
    finally:
        t.close()


def test_host_only_flow_control_leaves_device_flow_off(sim):
    m = machine_for(sim, flow_control="xonxoff", device_flow_control=False)
    t = MoxaTransport()
    t.open(m)
    try:
        s = sim.ports[0].settings
        assert not s.xon_enabled and not s.xoff_enabled
    finally:
        t.close()


def test_data_roundtrip_and_queue(sim):
    port = sim.ports[0]
    t = MoxaTransport()
    t.open(machine_for(sim))
    try:
        payload = b"%\nO0001\nG0 X0\n%\n"
        t.write(payload)
        assert wait_until(lambda: bytes(port.received) == payload)
        assert t.drain(timeout=2.0) is True
        assert t.pending_tx() == 0
        port.cnc_send(b"\x11")
        assert t.read(16, timeout=1.0) == b"\x11"
        assert t.stats.bytes_written == len(payload)
        assert t.stats.bytes_read == 1
    finally:
        t.close()


def test_notify_updates_modem_and_publishes_event(sim):
    bus = EventBus()
    seen = []
    bus.subscribe("transport.modem", lambda e: seen.append(e.payload))
    port = sim.ports[0]
    t = MoxaTransport(bus=bus)
    t.open(machine_for(sim))
    try:
        assert t.get_modem_status().cts is True
        port.set_modem(cts=False, dcd=True)
        assert wait_until(lambda: t.get_modem_status().cts is False)
        assert t.get_modem_status().dcd is True
        assert seen and seen[-1]["cts"] is False and seen[-1]["dcd"] is True
    finally:
        t.close()


def test_line_error_notify_is_logged(sim):
    bus = EventBus()
    errs = []
    bus.subscribe("transport.line_error", lambda e: errs.append(e.payload["error"]))
    t = MoxaTransport(bus=bus)
    t.open(machine_for(sim))
    try:
        sim.ports[0].raise_line_error(0x01 | 0x02)
        assert wait_until(lambda: len(errs) >= 2)
        assert set(errs) == {"parity error", "framing error"}
        assert "parity error" in t.stats.errors
    finally:
        t.close()


def test_keepalive_is_answered(sim):
    t = MoxaTransport()
    t.open(machine_for(sim))
    try:
        assert wait_until(lambda: t.polls_answered >= 3, timeout=3.0)
        # still connected after several polling rounds
        assert t.command_channel_available
        assert t.get_modem_status() is not None
    finally:
        t.close()


def test_dtr_rts_changes_use_linectrl(sim):
    port = sim.ports[0]
    t = MoxaTransport()
    t.open(machine_for(sim))
    try:
        t.set_dtr(False)
        assert port.settings.dtr is False and port.settings.rts is True
        t.set_rts(False)
        assert port.settings.rts is False
        assert any(op == 18 for op, _ in port.command_log)
    finally:
        t.close()


def test_purge_flushes_device_queues(sim):
    port = sim.ports[0]
    sim.instant = False  # slow line so bytes sit in the queue
    port.settings.baud = 300
    t = MoxaTransport()
    m = machine_for(sim, baud=300)
    t.open(m)
    try:
        t.write(b"X" * 2000)
        assert wait_until(lambda: len(port.tx_queue) > 0)
        t.purge(rx=False, tx=True)
        assert wait_until(lambda: len(port.tx_queue) == 0, timeout=1.0)
        assert len(port.received) < 2000
    finally:
        t.close()


def test_connect_refused_gives_helpful_error(sim):
    m = machine_for(sim)
    m["cmd_port"] = 1  # nothing listens there
    t = MoxaTransport()
    with pytest.raises(TransportError, match="command port"):
        t.open(m)
    assert not t.is_open


def test_command_channel_loss_is_reported(sim):
    bus = EventBus()
    errs = []
    bus.subscribe("transport.error", lambda e: errs.append(e.payload["message"]))
    t = MoxaTransport(bus=bus)
    t.open(machine_for(sim))
    try:
        conn = sim.ports[0].cmd_conn
        conn.shutdown(2)
        assert wait_until(lambda: not t.command_channel_available)
        assert errs
        # subsequent commands fail cleanly, data path still usable
        with pytest.raises(TransportError):
            t._command(b"\x13\x00")
    finally:
        t.close()


# --------------------------------------------------------------------------
# End-to-end: Sender over MoxaTransport through the simulator
# --------------------------------------------------------------------------
def run_send(sim, machine, text, timeout=15.0):
    bus = EventBus()
    sender = Sender(bus)
    sender.start(machine, file_path="", text=text)
    assert wait_until(lambda: sender.state.is_terminal, timeout=timeout), sender.state
    return sender


def test_sender_end_to_end_with_device_xonxoff(sim):
    sim.instant = False
    port = sim.ports[0]
    port.cnc.software_flow = True
    port.cnc.xoff_at = 300
    port.cnc.xon_at = 100
    port.cnc.consume_rate = 1500
    m = machine_for(sim, baud=19200, flow_control="xonxoff")
    m["send"].update({"uppercase": True, "start_chars": "%\n", "end_chars": "%\n", "line_ending": "LF",
                      "strip_blank_lines": True, "chunk_size": 64})
    program = "\n".join(f"n{i:04d} g01 x{i}.0 f200" for i in range(1, 120)) + "\n"
    sender = run_send(sim, m, program)
    assert sender.state is SendState.DONE, sender.snapshot()
    got = bytes(port.received).decode()
    assert got.startswith("%\n") and got.endswith("%\n")
    assert "N0001 G01 X1.0 F200\n" in got
    assert got.count("\n") == 119 + 2
    assert port.xoff_sent is False


def test_sender_stop_flushes_device_queue(sim):
    sim.instant = False
    port = sim.ports[0]
    m = machine_for(sim, baud=1200, flow_control="none")
    m["send"].update({"line_ending": "LF", "start_chars": "", "end_chars": "", "chunk_size": 4096})
    program = ("G01 X1.0 Y2.0 Z3.0 F100\n" * 400)
    bus = EventBus()
    sender = Sender(bus)
    sender.start(m, file_path="", text=program)
    assert wait_until(lambda: sender.state is SendState.SENDING and sender.snapshot()["bytes_sent"] > 0, timeout=5)
    time.sleep(0.3)
    sender.stop(wait=True, timeout=5)
    assert sender.state is SendState.STOPPED
    assert 20 in [op for op, _ in port.command_log]  # FLUSH
    assert len(port.received) < len(program)


def test_sender_waits_for_cts(sim):
    port = sim.ports[0]
    port.cnc.cts = False
    m = machine_for(sim, flow_control="rtscts")
    m["send"].update({"wait_for_ready": "cts", "ready_timeout_s": 5, "start_chars": "", "end_chars": ""})
    bus = EventBus()
    sender = Sender(bus)
    sender.start(m, file_path="", text="G0 X0\n")
    assert wait_until(lambda: sender.state is SendState.WAITING_READY, timeout=3)
    time.sleep(0.3)
    assert len(port.received) == 0
    port.set_modem(cts=True)
    assert wait_until(lambda: sender.state.is_terminal, timeout=5)
    assert sender.state is SendState.DONE
    assert bytes(port.received) == b"G0 X0\r\n" or bytes(port.received) == b"G0 X0\n"


# --------------------------------------------------------------------------
# End-to-end: Receiver over MoxaTransport (the CNC punches out a program)
# --------------------------------------------------------------------------
def test_receiver_end_to_end_saves_program(sim, tmp_path):
    from moxaserial.dnc.receiver import Receiver, ReceiveState

    port = sim.ports[0]
    m = machine_for(sim, flow_control="xonxoff")
    m["receive"].update({
        "folder": str(tmp_path),
        "filename_pattern": "punched.nc",
        "overwrite": "rename",
        "idle_timeout_s": 1,
        "start_trigger": "",
        "end_trigger": "",
    })
    bus = EventBus()
    states = []
    bus.subscribe("receive.state", lambda e: states.append(e.payload.get("state")))
    receiver = Receiver(bus)
    assert receiver.start(m)
    assert wait_until(lambda: receiver.state is ReceiveState.WAITING, timeout=5)
    program = b"%\r\nO0007\r\nN10 G00 X0\r\nN20 M30\r\n%\r\n"
    port.cnc_send(program[:12])
    time.sleep(0.2)
    port.cnc_send(program[12:])
    assert wait_until(lambda: receiver.state.is_terminal, timeout=10), receiver.state
    assert receiver.state is ReceiveState.DONE, receiver.snapshot()
    files = list(tmp_path.glob("*.nc"))
    assert len(files) == 1
    assert b"N20 M30" in files[0].read_bytes()


def test_sender_throttles_on_device_queue(sim):
    """With a slow line the host must not dump the whole program into the
    device; the device TX queue stays near the configured limit and the
    progress bar lags behind bytes handed to the socket."""
    sim.instant = False
    port = sim.ports[0]
    m = machine_for(sim, baud=1200, flow_control="none")
    m["send"].update({"line_ending": "LF", "start_chars": "", "end_chars": "",
                      "chunk_size": 32, "device_queue_limit": 64})
    bus = EventBus()
    peak = {"pending": 0, "percent_when_incomplete": 0.0}

    def on_progress(evt):
        p = evt.payload
        peak["pending"] = max(peak["pending"], len(port.tx_queue))
        if p.get("bytes_sent", 0) < p.get("bytes_total", 1):
            peak["percent_when_incomplete"] = max(peak["percent_when_incomplete"], p.get("percent", 0))

    bus.subscribe("send.progress", on_progress)
    sender = Sender(bus)
    program = ("G01 X1.0 Y2.0 Z3.0 F100\n" * 40)  # 960 bytes ~ 8.8 s at 1200 7E2
    sender.start(m, file_path="", text=program)
    assert wait_until(lambda: sender.state is SendState.SENDING, timeout=5)
    time.sleep(1.5)
    snap = sender.snapshot()
    assert snap["bytes_sent"] < len(program), "everything was dumped into the device at once"
    assert len(port.tx_queue) <= 64 + 32 + 16
    sender.stop(wait=True, timeout=5)
    assert sender.state is SendState.STOPPED
    assert peak["pending"] <= 64 + 32 + 16


def test_wait_for_xon_survives_a_connection_drop(sim):
    """The device drops both sockets while we wait for XON (Wi-Fi blip,
    port restart): the sender reconnects and still starts on the XON."""
    from moxaserial.transport.base import TransportError  # noqa: F401

    port = sim.ports[0]
    m = machine_for(sim, flow_control="xonxoff", device_flow_control=False)
    m["send"].update({"wait_for_ready": "xon", "ready_timeout_s": 20, "start_chars": "", "end_chars": "",
                      "line_ending": "LF"})
    # Generous on purpose. 4 attempts x 0.2 s is only ~0.8 s of reconnect
    # budget, and on a loaded machine the simulator can take longer than that
    # to re-accept - which fails the test for a reason that has nothing to do
    # with what it is checking.
    m["reconnect_attempts"] = 12
    m["reconnect_delay_s"] = 0.3
    bus = EventBus()
    logs = []
    bus.subscribe("send.log", lambda e: logs.append(e.payload["message"]))
    sender = Sender(bus)
    sender.start(m, file_path="", text="G0 X0\n")
    assert wait_until(lambda: sender.state is SendState.WAITING_READY, timeout=5)
    time.sleep(0.3)
    # device side: drop the host
    for conn in (port.data_conn, port.cmd_conn):
        if conn is not None:
            conn.shutdown(2)
    assert wait_until(lambda: any("reconnecting" in x.lower() for x in logs), timeout=5), logs
    assert wait_until(
        lambda: sender.state is SendState.WAITING_READY and port.data_conn is not None,
        timeout=20,
    ), sender.snapshot()
    time.sleep(0.2)
    port.cnc_send(b"\x11")
    assert wait_until(lambda: sender.state.is_terminal, timeout=10), sender.snapshot()
    assert sender.state is SendState.DONE
    assert bytes(port.received).endswith(b"G0 X0\n")
