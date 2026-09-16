"""Send engine against FakeTransport: progress, flow control, controls."""

from __future__ import annotations

import time

import pytest

from moxaserial.dnc.sender import Sender, SendState
from moxaserial.transport.base import ModemStatus, Transport, TransportError
from tests.conftest import wait_until

PROGRAM = "".join(f"N{n} G01 X{n}.0 Y{n}.0 F500.\n" for n in range(1, 41))


def run_send(bus, machine, transport, text=PROGRAM, timeout=10.0, **kwargs):
    sender = Sender(bus)
    assert sender.start(machine, text=text, transport=transport, **kwargs)
    assert wait_until(lambda: not sender.is_running, timeout), (
        f"send did not finish (state={sender.state})"
    )
    return sender


# -- happy path -------------------------------------------------------------

def test_send_delivers_every_byte(bus, machine, fake_transport):
    t = fake_transport()
    sender = run_send(bus, machine, t)
    assert sender.state is SendState.DONE
    assert t.written_text == PROGRAM.replace("\n", "\r\n")


def test_send_reports_100_percent_and_all_lines(bus, machine, fake_transport, collector):
    sender = run_send(bus, machine, fake_transport())
    snap = sender.snapshot()
    assert snap["percent"] == pytest.approx(100.0)
    assert snap["lines_sent"] == snap["lines_total"] == 40
    assert snap["bytes_sent"] == snap["bytes_total"]
    assert collector.last("send.done") is not None


def test_send_state_sequence(bus, machine, fake_transport, collector):
    run_send(bus, machine, fake_transport())
    states = collector.states("send.state")
    assert states[0] == "CONNECTING"
    assert "SENDING" in states
    assert states[-1] == "DONE"


def test_progress_is_monotonic_and_carries_a_line_window(bus, machine, fake_transport, collector):
    run_send(bus, machine, fake_transport())
    progress = collector.of("send.progress")
    sent = [p["bytes_sent"] for p in progress]
    assert sent == sorted(sent)

    windowed = [p for p in progress if p["window"] and p["line_index"] >= 0]
    assert windowed, "no progress event carried a preview window"
    sample = windowed[len(windowed) // 2]
    current = [w for w in sample["window"] if w["current"]]
    assert len(current) == 1
    assert current[0]["i"] == sample["line_index"]


def test_start_and_end_characters_wrap_the_transfer(bus, machine, fake_transport):
    machine["send"]["start_chars"] = r"%\n"
    machine["send"]["end_chars"] = r"%\n"
    t = fake_transport()
    run_send(bus, machine, t)
    assert t.written_text.startswith("%\n")
    assert t.written_text.endswith("%\n")


def test_preprocessing_is_applied_on_the_wire(bus, machine, fake_transport):
    machine["send"].update(uppercase=True, strip_comments=True, line_ending="LF")
    t = fake_transport()
    run_send(bus, machine, t, text="g0 x1 (rapid)\ng1 y2\n")
    assert t.written_text == "G0 X1\nG1 Y2\n"  # comment gone, trailing space trimmed


def test_transport_is_closed_when_the_sender_owns_it(bus, machine):
    sender = Sender(bus)  # no transport passed -> sender creates and owns one
    assert sender.start(machine, text="G0\n")
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.DONE


# -- flow control -----------------------------------------------------------

def test_xoff_pauses_and_xon_resumes(bus, machine, fake_transport, collector):
    """A small buffer forces the simulated control through XOFF/XON cycles."""
    machine["serial"]["flow_control"] = "xonxoff"
    t = fake_transport(buffer_size=64, drain_bytes_per_s=4000.0)
    sender = run_send(bus, machine, t, timeout=20.0)

    assert sender.state is SendState.DONE
    assert t.written_text == PROGRAM.replace("\n", "\r\n")
    assert any(p["xoff"] for p in collector.of("send.progress")), "XOFF was never observed"
    assert sender.snapshot()["xoff"] is False


def test_xoff_without_xon_times_out_as_an_error(bus, machine, fake_transport, collector):
    machine["serial"]["flow_control"] = "xonxoff"
    machine["send"]["handshake_timeout_s"] = 1  # CIMCO TRAN_TIMEOUT
    t = fake_transport(buffer_size=64, drain_bytes_per_s=0.0)  # never drains -> XON never comes
    sender = run_send(bus, machine, t, timeout=20.0)
    assert sender.state is SendState.ERROR
    assert "XOFF" in sender.snapshot()["error"]
    assert sender.snapshot()["errors"] >= 1


def test_dc4_from_the_control_aborts_the_send(bus, machine, fake_transport):
    machine["serial"]["flow_control"] = "xonxoff"
    t = fake_transport(realtime=True)  # ~1 s at 9600 baud, room to interrupt
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    t.inject(b"\x14")  # DC4 - "stop sending"
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.STOPPED
    assert len(t.written) < len(PROGRAM)


# -- wait for ready ---------------------------------------------------------

def test_wait_for_cts_blocks_until_the_line_comes_up(bus, machine, fake_transport, collector):
    machine["send"]["wait_for_ready"] = "cts"
    t = fake_transport(ready_delay_s=0.4)
    started = time.monotonic()
    sender = run_send(bus, machine, t, timeout=20.0)
    assert sender.state is SendState.DONE
    assert time.monotonic() - started >= 0.35
    assert "WAITING_READY" in collector.states("send.state")


def test_wait_for_cts_times_out(bus, machine, fake_transport):
    machine["send"]["wait_for_ready"] = "cts"
    machine["send"]["ready_timeout_s"] = 1
    t = fake_transport(cts=False)
    sender = run_send(bus, machine, t, timeout=20.0)
    assert sender.state is SendState.ERROR
    assert "CTS" in sender.snapshot()["error"]
    assert t.written == b""


def test_wait_for_xon_starts_on_the_control_character(bus, machine, fake_transport):
    machine["send"]["wait_for_ready"] = "xon"
    machine["serial"]["flow_control"] = "xonxoff"
    t = fake_transport(send_initial_xon=True, xon_delay_s=0.3)
    sender = run_send(bus, machine, t, timeout=20.0)
    assert sender.state is SendState.DONE


def test_wait_for_xon_accepts_dc2(bus, machine, fake_transport):
    machine["send"]["wait_for_ready"] = "xon"
    machine["serial"]["flow_control"] = "xonxoff"
    t = fake_transport()
    sender = Sender(bus)
    sender.start(machine, text="G0\n", transport=t)
    assert wait_until(lambda: sender.state is SendState.WAITING_READY, 5.0)
    t.inject(b"\x12")  # DC2 - punch request
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.DONE


def test_hardware_wait_degrades_when_modem_status_is_unavailable(bus, machine, fake_transport):
    """A transport that admits it cannot read CTS must not hang the send."""

    class NoModemTransport(type(fake_transport())):
        def capabilities(self):
            return {"can_read_modem_status": False}

        def _do_get_modem_status(self):
            return ModemStatus()  # everything low, forever

    t = NoModemTransport()
    machine["send"]["wait_for_ready"] = "cts"
    machine["send"]["ready_timeout_s"] = 30
    sender = run_send(bus, machine, t, text="G0\n", timeout=10.0)
    assert sender.state is SendState.DONE


# -- pause / resume / stop / resend -----------------------------------------

def test_pause_holds_then_resume_completes(bus, machine, fake_transport):
    t = fake_transport(realtime=True, time_scale=0.02)  # slow enough to catch mid-flight
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)

    sender.pause()
    assert wait_until(lambda: sender.state is SendState.PAUSED, 3.0)
    frozen = len(t.written)
    time.sleep(0.3)
    assert len(t.written) - frozen <= 64, "kept sending while paused"

    sender.resume()
    assert wait_until(lambda: not sender.is_running, 20.0)
    assert sender.state is SendState.DONE
    assert t.written_text == PROGRAM.replace("\n", "\r\n")


def test_stop_ends_the_transfer_early(bus, machine, fake_transport):
    t = fake_transport(realtime=True, time_scale=0.02)
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    sender.stop()
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.STOPPED
    assert len(t.written) < len(PROGRAM)


def test_stop_while_paused_releases_the_thread(bus, machine, fake_transport):
    t = fake_transport(realtime=True, time_scale=0.02)
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    sender.pause()
    assert wait_until(lambda: sender.state is SendState.PAUSED, 3.0)
    sender.stop()
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.STOPPED


def test_resend_replays_the_whole_program(bus, machine, fake_transport, nc_file):
    path = nc_file(PROGRAM)
    sender = Sender(bus)
    assert not sender.can_resend

    t1 = fake_transport()
    sender.start(machine, file_path=path, transport=t1)
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.can_resend

    assert sender.resend()
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.state is SendState.DONE
    # The same transport object is reused, so it holds two full copies.
    assert t1.written_text == 2 * PROGRAM.replace("\n", "\r\n")


def test_resend_without_a_previous_job_is_refused(bus):
    assert Sender(bus).resend() is False


def test_a_second_start_while_running_is_refused(bus, machine, fake_transport):
    t = fake_transport(realtime=True, time_scale=0.02)
    sender = Sender(bus)
    assert sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    assert sender.start(machine, text=PROGRAM, transport=t) is False
    sender.stop(wait=True)


# -- failures ---------------------------------------------------------------

def test_missing_file_is_an_error_not_an_exception(bus, machine, collector):
    sender = Sender(bus)
    assert sender.start(machine, file_path="/definitely/not/here.nc") is False
    assert sender.state is SendState.ERROR
    assert "File not found" in (collector.last("send.error") or {}).get("message", "")


def test_open_failure_is_reported(bus, machine, fake_transport, collector):
    t = fake_transport(fail_on_open="NPort refused the connection")
    sender = run_send(bus, machine, t)
    assert sender.state is SendState.ERROR
    assert "refused" in collector.last("send.error")["message"]


def test_mid_transfer_write_failure_is_reported(bus, machine, fake_transport):
    t = fake_transport(fail_after_bytes=200, fail_message="cable yanked")
    sender = run_send(bus, machine, t)
    assert sender.state is SendState.ERROR
    assert "cable yanked" in sender.snapshot()["error"]


def test_unexpected_exception_becomes_an_error_state(bus, machine):
    class ExplodingTransport(Transport):
        kind = "boom"

        def _do_open(self, machine):
            return None

        def _do_close(self):
            return None

        def _do_write(self, data):
            raise RuntimeError("kaboom")

        def _do_read(self, n, timeout):
            return b""

    sender = run_send(bus, machine, ExplodingTransport(), text="G0\n")
    assert sender.state is SendState.ERROR
    assert "kaboom" in sender.snapshot()["error"]


def test_transport_error_subclass_is_caught(bus, machine):
    class FailingOpen(Transport):
        kind = "nope"

        def _do_open(self, machine):
            raise TransportError("no route to host")

        def _do_close(self):
            return None

        def _do_write(self, data):
            return 0

        def _do_read(self, n, timeout):
            return b""

    sender = run_send(bus, machine, FailingOpen(), text="G0\n")
    assert sender.state is SendState.ERROR
    assert "no route to host" in sender.snapshot()["error"]


# -- delays -----------------------------------------------------------------

def test_line_delay_is_honoured(bus, machine, fake_transport):
    machine["send"]["line_delay_ms"] = 20
    t = fake_transport()
    started = time.monotonic()
    sender = run_send(bus, machine, t, text="A\nB\nC\nD\nE\n", timeout=10.0)
    assert sender.state is SendState.DONE
    assert time.monotonic() - started >= 0.09  # 5 lines x 20 ms


def test_char_delay_switches_to_byte_at_a_time(bus, machine, fake_transport):
    machine["send"]["char_delay_ms"] = 1
    t = fake_transport()
    sender = run_send(bus, machine, t, text="ABCDE\n", timeout=10.0)
    assert sender.state is SendState.DONE
    assert t.written_text == "ABCDE\r\n"
