"""Receive engine against FakeTransport: triggers, timeouts, overwrite policy."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from moxaserial.dnc.receiver import (
    Receiver,
    ReceiveState,
    next_free_name,
    render_filename,
    sanitize,
)
from moxaserial.transport.fake import SAMPLE_PROGRAM
from tests.conftest import wait_until

PROGRAM = b"%\r\nO0007 (RECEIVED)\r\nN10 G0 X1\r\nN20 M30\r\n%\r\n"


@pytest.fixture
def rx_machine(machine, tmp_path):
    machine["receive"].update(
        folder=str(tmp_path / "in"),
        filename_pattern="{program}.nc",
        overwrite="rename",
        idle_timeout_s=1,
        overall_timeout_s=20,
        start_trigger="",
        end_trigger="",
    )
    return machine


def run_receive(bus, machine, transport, payload=PROGRAM, timeout=25.0, **kwargs):
    receiver = Receiver(bus)
    assert receiver.start(machine, transport=transport, **kwargs)
    assert wait_until(lambda: receiver.state is not ReceiveState.IDLE, 5.0)
    if payload is not None:
        assert wait_until(lambda: transport.is_open, 5.0)
        transport.arm_receive(payload)
    assert wait_until(lambda: not receiver.is_running, timeout), (
        f"receive did not finish (state={receiver.state})"
    )
    return receiver


# -- filename helpers -------------------------------------------------------

@pytest.mark.parametrize(
    "raw,expected",
    [
        ("prog.nc", "prog.nc"),
        ("a/b:c*d?.nc", "a_b_c_d_.nc"),
        ("   ", "received"),
        ("...", "received"),
    ],
)
def test_sanitize(raw, expected):
    assert sanitize(raw) == expected


def test_render_filename_expands_every_placeholder():
    from datetime import datetime

    when = datetime(2026, 9, 16, 14, 5, 9)
    out = render_filename("{machine}-{program}-{date}-{time}-{n}.nc", "Haas VF/2", "O1234", when, 3)
    assert out == "Haas VF_2-O1234-2026-09-16-140509-3.nc"  # spaces are legal, slashes are not


def test_render_filename_without_a_program_uses_a_placeholder_word():
    out = render_filename("{program}.nc", "M", "")
    assert out == "program.nc"


def test_next_free_name_walks_a_suffix(tmp_path):
    base = tmp_path / "p.nc"
    assert next_free_name(base) == base
    base.write_text("x")
    assert next_free_name(base).name == "p_1.nc"
    (tmp_path / "p_1.nc").write_text("x")
    assert next_free_name(base).name == "p_2.nc"


# -- happy path -------------------------------------------------------------

def test_receive_writes_the_program_to_disk(bus, rx_machine, fake_transport):
    receiver = run_receive(bus, rx_machine, fake_transport())
    assert receiver.state is ReceiveState.DONE
    path = Path(receiver.last_path)
    assert path.is_file()
    assert path.name == "O0007.nc"
    assert "N20 M30" in path.read_text()


def test_receive_publishes_progress_with_a_live_tail(bus, rx_machine, fake_transport, collector):
    run_receive(bus, rx_machine, fake_transport())
    progress = collector.of("receive.progress")
    assert progress
    assert max(p["bytes_received"] for p in progress) == len(PROGRAM)
    tails = [p["tail"] for p in progress if p["tail"]]
    assert tails and "N20 M30" in "\n".join(tails[-1])
    assert collector.last("receive.done")["bytes"] == len(PROGRAM)


def test_receive_state_sequence(bus, rx_machine, fake_transport, collector):
    run_receive(bus, rx_machine, fake_transport())
    states = collector.states("receive.state")
    assert states[0] == "CONNECTING"
    assert "WAITING" in states and "RECEIVING" in states
    assert states[-1] == "DONE"


def test_idle_timeout_ends_the_capture(bus, rx_machine, fake_transport):
    rx_machine["receive"]["idle_timeout_s"] = 1
    receiver = run_receive(bus, rx_machine, fake_transport(), payload=b"G0 X1\r\n")
    assert receiver.state is ReceiveState.DONE
    assert Path(receiver.last_path).read_text().strip() == "G0 X1"


def test_no_data_at_all_times_out_as_an_error(bus, rx_machine, fake_transport, collector):
    rx_machine["receive"]["overall_timeout_s"] = 5
    receiver = run_receive(bus, rx_machine, fake_transport(), payload=None, timeout=20.0)
    assert receiver.state is ReceiveState.ERROR
    assert "Timed out" in collector.last("receive.error")["message"]


def test_filename_override_beats_the_pattern(bus, rx_machine, fake_transport):
    receiver = run_receive(bus, rx_machine, fake_transport(), filename_override="explicit.nc")
    assert Path(receiver.last_path).name == "explicit.nc"


def test_extension_is_appended_when_the_pattern_has_none(bus, rx_machine, fake_transport):
    rx_machine["receive"]["filename_pattern"] = "{program}"
    rx_machine["receive"]["append_extension"] = ".tap"
    receiver = run_receive(bus, rx_machine, fake_transport())
    assert Path(receiver.last_path).name == "O0007.tap"


def test_control_characters_are_stripped_when_asked(bus, rx_machine, fake_transport):
    rx_machine["receive"]["strip_control_chars"] = True
    receiver = run_receive(bus, rx_machine, fake_transport(), payload=b"G0\x00\x01 X1\r\n")
    assert Path(receiver.last_path).read_text().strip() == "G0 X1"


# -- triggers ---------------------------------------------------------------

def test_start_trigger_discards_the_leading_noise(bus, rx_machine, fake_transport):
    rx_machine["receive"]["start_trigger"] = "%"
    payload = b"garbage from the line\r\n" + PROGRAM
    receiver = run_receive(bus, rx_machine, fake_transport(), payload=payload)
    text = Path(receiver.last_path).read_text()
    assert text.startswith("%")
    assert "garbage" not in text


def test_end_trigger_cuts_the_capture_short(bus, rx_machine, fake_transport):
    rx_machine["receive"].update(start_trigger="%", end_trigger="M30", idle_timeout_s=30)
    receiver = run_receive(
        bus, rx_machine, fake_transport(), payload=PROGRAM + b"TRAILING JUNK\r\n", timeout=25.0
    )
    text = Path(receiver.last_path).read_text()
    assert text.rstrip().endswith("M30")
    assert "JUNK" not in text


def test_end_trigger_is_not_matched_by_the_start_trigger(bus, rx_machine, fake_transport):
    """'%' opens and closes the program - the closing one must win, not the opener."""
    rx_machine["receive"].update(start_trigger="%", end_trigger="%", idle_timeout_s=30)
    receiver = run_receive(bus, rx_machine, fake_transport(), payload=PROGRAM, timeout=25.0)
    text = Path(receiver.last_path).read_text()
    assert "O0007" in text
    assert text.count("%") == 2


# -- overwrite policies -----------------------------------------------------

def _existing(tmp_path: Path, name: str = "O0007.nc") -> Path:
    folder = tmp_path / "in"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_text("OLD CONTENT", encoding="ascii")
    return path


def test_overwrite_allow_replaces(bus, rx_machine, fake_transport, tmp_path):
    existing = _existing(tmp_path)
    rx_machine["receive"]["overwrite"] = "allow"
    receiver = run_receive(bus, rx_machine, fake_transport())
    assert Path(receiver.last_path) == existing
    assert "OLD CONTENT" not in existing.read_text()


def test_overwrite_rename_keeps_the_original(bus, rx_machine, fake_transport, tmp_path):
    existing = _existing(tmp_path)
    rx_machine["receive"]["overwrite"] = "rename"
    receiver = run_receive(bus, rx_machine, fake_transport())
    assert Path(receiver.last_path).name == "O0007_1.nc"
    assert existing.read_text() == "OLD CONTENT"


def test_overwrite_deny_refuses_and_errors(bus, rx_machine, fake_transport, tmp_path, collector):
    existing = _existing(tmp_path)
    rx_machine["receive"]["overwrite"] = "deny"
    receiver = run_receive(bus, rx_machine, fake_transport())
    assert receiver.state is ReceiveState.ERROR
    assert "deny" in collector.last("receive.error")["message"]
    assert existing.read_text() == "OLD CONTENT"


@pytest.mark.parametrize(
    "decision,expected_name,original_kept",
    [
        ("overwrite", "O0007.nc", False),
        ("rename", "O0007_1.nc", True),
    ],
)
def test_overwrite_ask_round_trips_to_the_ui(
    bus, rx_machine, fake_transport, tmp_path, decision, expected_name, original_kept
):
    existing = _existing(tmp_path)
    rx_machine["receive"]["overwrite"] = "ask"

    receiver = Receiver(bus)
    asked = threading.Event()

    def answer(evt):
        asked.set()
        receiver.resolve_overwrite(evt.payload["token"], decision)

    bus.subscribe("receive.overwrite_request", answer)

    t = fake_transport()
    assert receiver.start(rx_machine, transport=t)
    assert wait_until(lambda: t.is_open, 5.0)
    t.arm_receive(PROGRAM)
    assert wait_until(lambda: not receiver.is_running, 25.0)

    assert asked.is_set(), "the UI was never asked"
    assert receiver.state is ReceiveState.DONE
    assert Path(receiver.last_path).name == expected_name
    assert (existing.read_text() == "OLD CONTENT") is original_kept


def test_overwrite_ask_cancel_writes_nothing(bus, rx_machine, fake_transport, tmp_path, collector):
    existing = _existing(tmp_path)
    rx_machine["receive"]["overwrite"] = "ask"
    receiver = Receiver(bus)
    bus.subscribe(
        "receive.overwrite_request",
        lambda e: receiver.resolve_overwrite(e.payload["token"], "cancel"),
    )
    t = fake_transport()
    receiver.start(rx_machine, transport=t)
    assert wait_until(lambda: t.is_open, 5.0)
    t.arm_receive(PROGRAM)
    assert wait_until(lambda: not receiver.is_running, 25.0)
    assert receiver.state is ReceiveState.ERROR
    assert "cancelled" in collector.last("receive.error")["message"]
    assert existing.read_text() == "OLD CONTENT"


def test_a_stale_overwrite_token_is_ignored(bus, rx_machine):
    receiver = Receiver(bus)
    assert receiver.resolve_overwrite("not-a-real-token", "overwrite") is False


# -- stopping ---------------------------------------------------------------

def test_stop_before_any_data_leaves_no_file(bus, rx_machine, fake_transport):
    receiver = Receiver(bus)
    t = fake_transport()
    receiver.start(rx_machine, transport=t)
    assert wait_until(lambda: receiver.state is ReceiveState.WAITING, 5.0)
    receiver.stop()
    assert wait_until(lambda: not receiver.is_running, 10.0)
    assert receiver.state is ReceiveState.STOPPED
    assert receiver.last_path == ""


def test_stop_after_data_keeps_what_arrived(bus, rx_machine, fake_transport):
    rx_machine["receive"]["idle_timeout_s"] = 30
    receiver = Receiver(bus)
    t = fake_transport(outgoing_gap_s=0.2, outgoing_chunk=8)
    receiver.start(rx_machine, transport=t)
    assert wait_until(lambda: t.is_open, 5.0)
    t.arm_receive(SAMPLE_PROGRAM)
    assert wait_until(lambda: receiver.snapshot()["bytes_received"] > 16, 10.0)
    receiver.stop()
    assert wait_until(lambda: not receiver.is_running, 10.0)
    assert receiver.state is ReceiveState.DONE
    assert Path(receiver.last_path).is_file()


def test_a_second_receive_while_running_is_refused(bus, rx_machine, fake_transport):
    receiver = Receiver(bus)
    t = fake_transport()
    assert receiver.start(rx_machine, transport=t)
    assert wait_until(lambda: receiver.state is ReceiveState.WAITING, 5.0)
    assert receiver.start(rx_machine, transport=t) is False
    receiver.stop(wait=True)


def test_auto_line_ending_collapses_fanuc_lf_cr_cr():
    """A Fanuc punches EOB as LF CR CR by default; AUTO must not turn that
    into three line breaks."""
    from moxaserial.config import default_receive
    from moxaserial.dnc.receiver import postprocess_received

    cfg = default_receive()
    out = postprocess_received(b"%\n\r\rO1001(TUBES OP1)\n\r\rG0X0\n\r\r%\n\r\r", cfg)
    assert out == "%\r\nO1001(TUBES OP1)\r\nG0X0\r\n%\r\n"
    cfg["line_ending"] = "LF"
    out = postprocess_received(b"%\n\nO1\n", cfg)
    assert out == "%\r\n\r\nO1\r\n"  # explicit mode keeps blank lines


def test_insert_spaces_by_dialect():
    from moxaserial.dnc.receiver import insert_spaces

    assert insert_spaces("G01X10.Y-5.F100", "fanuc") == "G01 X10. Y-5. F100"
    assert insert_spaces("N10G0X0", "iso_mill") == "N10 G0 X0"
    assert insert_spaces("O1001(TUBES OP1)", "haas") == "O1001 (TUBES OP1)"
    assert insert_spaces("(T0303 D=9.525 SPOT)", "iso_lathe") == "(T0303 D=9.525 SPOT)"
    assert insert_spaces("G01 X10. Y-5.", "iso_mill") == "G01 X10. Y-5."
    assert insert_spaces("%", "fanuc") == "%"
    assert insert_spaces("L X+10 Y+20 R0 FMAX", "heidenhain") == "L X+10 Y+20 R0 FMAX"
    assert insert_spaces("G01X10.", "heidenhain") == "G01X10."


def test_trailing_spaces_before_eob_are_trimmed():
    from moxaserial.config import default_receive
    from moxaserial.dnc.receiver import postprocess_received

    cfg = default_receive()
    assert postprocess_received(b"% \n\r\rO1001 (TUBES OP1) \n\r\r", cfg) == "%\r\nO1001 (TUBES OP1)\r\n"
    cfg["trim_trailing_spaces"] = False
    assert postprocess_received(b"% \n", cfg) == "% \r\n"


def test_receive_retries_a_refused_connection(bus, rx_machine, fake_transport):
    """First connects fail (device still holds the slot); the receiver keeps
    trying instead of failing before the operator reaches the control."""
    import time as _t

    from moxaserial.dnc.receiver import Receiver, ReceiveState
    from moxaserial.transport.base import TransportError

    t = fake_transport()
    calls = {"n": 0}
    real_open = t._do_open

    def flaky_open(machine):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransportError("Could not open the NPort command port - timed out")
        return real_open(machine)

    t._do_open = flaky_open
    rx_machine["reconnect_delay_s"] = 1
    rx_machine["auto_reconnect"] = True
    r = Receiver(bus)
    assert r.start(rx_machine, transport=t)
    deadline = _t.monotonic() + 15
    while _t.monotonic() < deadline and r.state not in (ReceiveState.WAITING, ReceiveState.RECEIVING, ReceiveState.DONE):
        _t.sleep(0.05)
    assert calls["n"] == 3
    assert r.state in (ReceiveState.WAITING, ReceiveState.RECEIVING, ReceiveState.DONE), r.snapshot()
    r.stop(wait=True, timeout=5) if hasattr(r, "stop") else None


def test_live_tail_uses_the_receive_conversion(bus):
    from moxaserial.config import default_receive
    from moxaserial.dnc.receiver import Receiver

    r = Receiver(bus)
    cfg = default_receive()
    cfg.update({"dialect": "fanuc", "insert_spaces": True})
    r._note_bytes(bytearray(b"% \n\r\rO1001(TUBES OP1) \n\r\rG0X0 \n\r\r"), cfg)
    snap = r.snapshot()
    assert snap["tail"] == ["%", "O1001 (TUBES OP1)", "G0 X0"]
    assert snap["lines_received"] == 3
