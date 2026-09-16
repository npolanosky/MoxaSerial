"""CIMCO Edit DNC parity: the options added in the parity-gap pass.

Every case here maps to a named option on one of CIMCO's four DNC setup
pages. Engine
behaviour is exercised against ``FakeTransport``; the pure filters are
called directly.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from moxaserial.config import (
    END_TRIGGER_MODES,
    RECEIVE_LINE_ENDINGS,
    RECEIVE_REMOVE_CHARS,
    SAVE_LINE_ENDINGS,
    SCHEMA_VERSION,
    START_TRIGGER_MODES,
    cimco_serial,
    default_machine,
    migrate,
    normalize_machine,
    simulator_machine,
    validate_machine,
)
from moxaserial.dnc.preprocess import (
    PreprocessOptions,
    apply_triggers,
    char_set,
    preprocess,
    unescape,
)
from moxaserial.dnc.receiver import (
    Receiver,
    ReceiveState,
    check_parity,
    detect_line_ending,
    filter_received_lines,
    postprocess_received,
    wire_lines,
)
from moxaserial.dnc.sender import Sender, SendState
from tests.conftest import wait_until

PROGRAM = "%\nO0001 (PARITY)\nN10 G0 X1\nN20 M30\n%\n"


# ==========================================================================
# Character-entry convention - CIMCO's \NN decimal escapes
# ==========================================================================

@pytest.mark.parametrize(
    "raw,expected",
    [
        (r"\36", "$"),                 # CIMCO's own example
        (r"\17", "\x11"),              # XOn
        (r"\19", "\x13"),              # XOff
        (r"\35", "#"),                 # default "Insert on parity error"
        (r"\13 \10", "\r\n"),          # the CR/LF combo entry, space = separator
        (r"\13\10\10", "\r\n\n"),      # juxtaposed, non-standard linefeed
        (r"\0", "\x00"),               # still NUL, as before
        (r"\n\r\t", "\n\r\t"),         # the old letter escapes keep working
        (r"\x12", "\x12"),             # the old hex escape keeps working
        (r"\\13", "\\13"),             # an escaped backslash is not a decimal
        (r"%\10", "%\n"),              # literal text mixed with an escape
        (r"\999", "\xff"),             # out of range is clamped, never raises
    ],
)
def test_decimal_escapes(raw, expected):
    assert unescape(raw) == expected


def test_decimal_escape_only_eats_one_separating_space():
    assert unescape(r"\36 \37 x") == "$%x"
    assert unescape(r"\36  x") == "$ x"


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("$%", {"$", "%"}),
        (r"\36 \37", {"$", "%"}),
        (r"\32", {" "}),        # an escaped space *is* a member
        ("( )", {"(", ")"}),    # a literal space is only a separator
        ("", set()),
    ],
)
def test_char_set_fields(raw, expected):
    assert char_set(raw) == expected


# ==========================================================================
# Defaults - CIMCO ships 9600 7E2, software flow control, DTR+RTS high
# ==========================================================================

def test_new_moxa_machine_uses_cimco_line_defaults():
    s = default_machine("Fanuc", "moxa")["serial"]
    assert (s["baud"], s["data_bits"], s["parity"], s["stop_bits"]) == (9600, 7, "even", "2")
    assert s["flow_control"] == "xonxoff"  # CIMCO SOFTWARE
    assert s["assert_dtr"] and s["assert_rts"]
    assert (s["xon_char"], s["xoff_char"]) == (17, 19)
    assert cimco_serial()["data_bits"] == 7


def test_the_simulator_keeps_its_own_8n1_defaults():
    s = simulator_machine()["serial"]
    assert (s["data_bits"], s["parity"], s["stop_bits"]) == (8, "none", "1")


def test_cimco_defaults_for_the_filter_options():
    m = default_machine()
    assert m["send"]["remove_nulls"] is True            # "Remove ASCII 0's"
    assert m["send"]["break_count"] == 0                 # inbound ignored
    assert m["send"]["handshake_timeout_s"] == 0         # wait for flow start
    assert m["receive"]["idle_timeout_s"] == 5           # RECV_TIMEOUT
    assert m["receive"]["line_ending"] == "AUTO"         # RECV_CRLF
    assert m["receive"]["save_line_ending"] == "CRLF"    # SAVECRLF
    assert m["receive"]["remove_chars"] == "ascii0to31"  # All below ASCII 32
    assert m["serial"]["parity_insert"] == "\\35"        # PARITYINSERT 035
    assert m["auto_reconnect"] is True                   # AUTORECONNECT


def test_enum_members_match_cimcos_wording():
    assert START_TRIGGER_MODES == ("save_from_trigger", "save_after_trigger")
    assert END_TRIGGER_MODES == ("save_including_trigger", "save_until_trigger")
    assert RECEIVE_REMOVE_CHARS == ("none", "ascii0", "ascii0to31", "custom")
    assert "AUTO" in RECEIVE_LINE_ENDINGS
    assert "KEEP" in SAVE_LINE_ENDINGS


# ==========================================================================
# Normalisation, validation and the 1 -> 2 migration
# ==========================================================================

def test_port_index_accepts_cimcos_full_1_to_32_range():
    assert normalize_machine({"name": "x", "port_index": 32})["port_index"] == 32
    assert normalize_machine({"name": "x", "port_index": 99})["port_index"] == 32


def test_receive_timeout_zero_is_allowed_and_means_no_timeout():
    m = normalize_machine({"name": "x", "receive": {"idle_timeout_s": 0}})
    assert m["receive"]["idle_timeout_s"] == 0


def test_validation_flags_the_cimco_combinations():
    m = default_machine()
    m["serial"]["check_parity"] = True
    m["serial"]["parity"] = "none"
    m["send"]["line_ending"] = "CUSTOM"
    m["send"]["line_ending_custom"] = ""
    m["receive"]["remove_chars"] = "custom"
    m["receive"]["remove_chars_custom"] = ""
    problems = " ".join(validate_machine(m))
    assert "Check parity" in problems
    assert "custom transmit CR/LF" in problems
    assert "no characters were listed" in problems


def test_manual_stop_warning_is_advisory_not_fatal(tmp_path):
    from moxaserial.config import ConfigStore

    store = ConfigStore(tmp_path / "settings.json")
    m = default_machine("No timeout")
    m["receive"]["idle_timeout_s"] = 0
    m["receive"]["end_trigger"] = ""
    saved = store.upsert_machine(m)  # must not raise
    assert saved["receive"]["idle_timeout_s"] == 0


def test_legacy_strip_control_chars_seeds_remove_chars():
    on = normalize_machine({"name": "x", "receive": {"strip_control_chars": True}})
    off = normalize_machine({"name": "x", "receive": {"strip_control_chars": False}})
    assert on["receive"]["remove_chars"] == "ascii0to31"
    assert off["receive"]["remove_chars"] == "none"


def test_migration_1_to_2_keeps_old_machines_behaving_the_same():
    old = {
        "schema_version": 1,
        "machines": [
            {"id": "m1", "name": "Old", "receive": {"strip_control_chars": False}},
        ],
    }
    data, notes = migrate(old)
    assert data["schema_version"] == SCHEMA_VERSION
    recv = data["machines"][0]["receive"]
    assert recv["remove_chars"] == "none"
    assert recv["save_line_ending"] == "KEEP"  # old receiver wrote the wire ending
    assert any("1 -> 2" in n for n in notes)


# ==========================================================================
# Transmit page
# ==========================================================================

def _opts(**kw) -> PreprocessOptions:
    base = {"uppercase": False, "strip_blank_lines": False, "line_ending": "LF"}
    base.update(kw)
    return PreprocessOptions(**base)


def test_transmit_start_trigger_starts_at_the_line_containing_it():
    out = preprocess("junk\nhead\n%\nN10\nN20\n", _opts(start_trigger="%"))
    assert out.lines == ["%", "N10", "N20"]


def test_transmit_end_trigger_line_is_not_transmitted():
    out = preprocess("N10\nN20\nM30 END\nN30\n", _opts(end_trigger="M30"))
    assert out.lines == ["N10", "N20"]


def test_transmit_triggers_that_never_match_send_the_whole_file():
    out = preprocess("N10\nN20\n", _opts(start_trigger="%%%", end_trigger="ZZZ"))
    assert out.lines == ["N10", "N20"]


def test_apply_triggers_is_pure_and_inclusive_at_the_start():
    assert apply_triggers(["a", "%b", "c", "END", "d"], "%", "END") == ["%b", "c"]


def test_omit_lines_containing_drops_whole_lines():
    out = preprocess("N10 X1\n/N20 SKIP\nN30 Y2\n", _opts(omit_lines_containing="/"))
    assert out.lines == ["N10 X1", "N30 Y2"]
    assert out.dropped_lines == 1


def test_transmit_remove_characters_strips_them_from_the_stream():
    out = preprocess("N10 X1;\nN20;\n", _opts(remove_chars=";"))
    assert out.lines == ["N10 X1", "N20"]


def test_remove_ascii_zeros_is_on_by_default():
    out = preprocess("N1\x000 X1\n", _opts())
    assert out.lines == ["N10 X1"]
    kept = preprocess("N1\x000\n", _opts(remove_nulls=False))
    assert "\x00" in kept.lines[0]


def test_replace_tabs_with_spaces():
    out = preprocess("N10\tX1\n", _opts(tabs_to_spaces=True))
    assert out.lines == ["N10 X1"]


def test_custom_line_ending_sends_a_non_standard_linefeed():
    out = preprocess("N10\n", _opts(line_ending="CUSTOM", line_ending_custom=r"\13 \10 \10"))
    assert out.payload == b"N10\r\n\n"


def test_transmit_filters_run_in_cimcos_order():
    """Trigger -> omit -> remove chars -> case -> whitespace -> line ending."""
    opts = _opts(
        start_trigger="%",
        end_trigger="M30",
        omit_lines_containing="/",
        remove_chars=";",
        uppercase=True,
        line_ending="CRLF",
    )
    out = preprocess("noise\n%\n/skip me\nn10 x1;\nM30\nafter\n", opts)
    assert out.lines == ["%", "N10 X1"]
    assert out.payload == b"%\r\nN10 X1\r\n"


# ==========================================================================
# Transmit engine - break count, handshake timeout, CPS / errors
# ==========================================================================

def test_break_count_aborts_the_send(bus, machine, fake_transport):
    machine["send"]["break_count"] = 4
    t = fake_transport(realtime=True)
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM * 40, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    t.inject(b"ABCDEFGH")  # the control talks back
    assert wait_until(lambda: not sender.is_running, 20.0)
    assert sender.state is SendState.ERROR
    snap = sender.snapshot()
    assert "Break count exceeded" in snap["error"]
    assert snap["inbound_chars"] >= 4
    assert snap["errors"] >= 1


def test_inbound_characters_are_ignored_when_break_count_is_zero(bus, machine, fake_transport):
    machine["send"]["break_count"] = 0
    machine["serial"]["flow_control"] = "none"
    t = fake_transport(realtime=True)
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: sender.state is SendState.SENDING, 5.0)
    t.inject(b"NOISE NOISE NOISE")
    assert wait_until(lambda: not sender.is_running, 20.0)
    assert sender.state is SendState.DONE


def test_progress_carries_cps_and_an_error_count(bus, machine, fake_transport):
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM * 50, transport=fake_transport())
    assert wait_until(lambda: not sender.is_running, 20.0)
    snap = sender.snapshot()
    assert snap["cps"] > 0
    assert snap["cps"] == pytest.approx(snap["rate_bps"])
    assert snap["errors"] == 0
    assert snap["eta_s"] >= 0  # CIMCO "Remaining time:"


def test_handshake_timeout_zero_holds_instead_of_failing(bus, machine, fake_transport):
    """CIMCO: with no timeout it waits until a start flow is received."""
    machine["serial"]["flow_control"] = "xonxoff"
    machine["send"]["handshake_timeout_s"] = 0
    t = fake_transport(buffer_size=64, drain_bytes_per_s=0.0)
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM * 20, transport=t)
    assert wait_until(lambda: sender.snapshot().get("xoff") is True, 10.0)
    # Still holding several seconds later - no error, no completion.
    assert not wait_until(lambda: not sender.is_running, 3.0)
    assert sender.state is SendState.SENDING
    sender.stop(wait=True)


# ==========================================================================
# Auto re-connect ([TCPIPDIRECT] AUTORECONNECT)
# ==========================================================================

def test_auto_reconnect_retries_the_configured_number_of_times(
    bus, machine, fake_transport, collector
):
    machine["auto_reconnect"] = True
    machine["reconnect_attempts"] = 2
    machine["reconnect_delay_s"] = 0
    t = fake_transport(fail_on_open="NPort refused the connection")
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: not sender.is_running, 20.0)
    assert sender.state is SendState.ERROR
    assert sender.snapshot()["errors"] == 3  # 1 attempt + 2 retries
    warnings = [e for e in collector.of("send.log") if "Connect attempt" in e["message"]]
    assert len(warnings) == 2


def test_auto_reconnect_off_tries_once(bus, machine, fake_transport):
    machine["auto_reconnect"] = False
    t = fake_transport(fail_on_open="nope")
    sender = Sender(bus)
    sender.start(machine, text=PROGRAM, transport=t)
    assert wait_until(lambda: not sender.is_running, 10.0)
    assert sender.snapshot()["errors"] == 1


# ==========================================================================
# Receive filters (pure)
# ==========================================================================

@pytest.mark.parametrize(
    "text,expected",
    [("a\r\nb", "\r\n"), ("a\rb", "\r"), ("a\nb", "\n"), ("ab", "\n")],
)
def test_auto_line_ending_detection(text, expected):
    assert detect_line_ending(text) == expected


def test_wire_lines_auto_handles_every_combination():
    lines, eol = wire_lines("a\r\nb\r\n", {"line_ending": "AUTO"})
    assert (lines, eol) == (["a", "b"], "\r\n")
    lines, eol = wire_lines("a\rb\r", {"line_ending": "AUTO"})
    assert (lines, eol) == (["a", "b"], "\r")


def test_wire_lines_explicit_does_not_split_on_the_other_character():
    lines, eol = wire_lines("a\r\nb\r\n", {"line_ending": "LF"})
    assert lines == ["a\r", "b\r"] and eol == "\n"


def test_wire_lines_custom_sequence():
    lines, _ = wire_lines(
        "a\r\n\nb\r\n\n", {"line_ending": "CUSTOM", "line_ending_custom": r"\13 \10 \10"}
    )
    assert lines == ["a", "b"]


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("none", "G0\x00\x01 X1"),
        ("ascii0", "G0\x01 X1"),
        ("ascii0to31", "G0 X1"),
    ],
)
def test_receive_remove_characters(mode, expected):
    cfg = {"remove_chars": mode, "line_ending": "AUTO", "save_line_ending": "LF"}
    assert postprocess_received(b"G0\x00\x01 X1\r\n", cfg) == expected + "\n"


def test_receive_remove_characters_custom_list():
    cfg = {
        "remove_chars": "custom",
        "remove_chars_custom": r"; \36",
        "save_line_ending": "LF",
    }
    assert postprocess_received(b"N10;$ X1\r\n", cfg) == "N10 X1\n"


def test_receive_omit_lines_containing_and_with_string():
    lines = ["N10 X1", "/N20 SKIP", "N30 (COMMENT)", "N40"]
    cfg = {"omit_lines_containing": "/", "omit_lines_with_string": "COMMENT"}
    assert filter_received_lines(lines, cfg) == ["N10 X1", "N40"]


def test_receive_omit_empty_lines():
    cfg = {"omit_empty_lines": True, "remove_chars": "none"}
    assert filter_received_lines(["N10", "", "  ", "N20"], cfg) == ["N10", "N20"]


def test_saved_line_ending_is_separate_from_the_wire_one():
    wire = b"N10\rN20\r"  # CR-only control
    assert postprocess_received(wire, {"save_line_ending": "CRLF"}) == "N10\r\nN20\r\n"
    assert postprocess_received(wire, {"save_line_ending": "LF"}) == "N10\nN20\n"
    assert postprocess_received(wire, {"save_line_ending": "KEEP"}) == "N10\rN20\r"


def test_every_saved_line_is_terminated():
    assert postprocess_received(b"N10\r\nN20", {"save_line_ending": "LF"}).endswith("\n")


# ==========================================================================
# Parity checking on receive
# ==========================================================================

def _with_parity(data: bytes, odd: bool = False) -> bytes:
    out = bytearray()
    for byte in data:
        bits = bin(byte & 0x7F).count("1") & 1
        high = (bits ^ 1) if odd else bits
        out.append((byte & 0x7F) | (high << 7))
    return bytes(out)


def test_clean_even_parity_stream_has_no_errors():
    clean, errors = check_parity(_with_parity(b"N10 X1"), "even", 7, "#")
    assert (clean, errors) == (b"N10 X1", 0)


def test_parity_error_inserts_the_marker_and_counts():
    data = bytearray(_with_parity(b"N10 X1"))
    data[2] ^= 0x80  # flip the parity bit of one character
    clean, errors = check_parity(bytes(data), "even", 7, "#")
    assert errors == 1
    assert clean == b"N1#0 X1"


def test_parity_marker_may_be_empty():
    data = bytearray(_with_parity(b"AB"))
    data[0] ^= 0x80
    clean, errors = check_parity(bytes(data), "even", 7, "")
    assert (clean, errors) == (b"AB", 1)


def test_odd_parity_is_checked_the_other_way_round():
    assert check_parity(_with_parity(b"AB", odd=True), "odd", 7, "#")[1] == 0
    assert check_parity(_with_parity(b"AB", odd=True), "even", 7, "#")[1] == 2


@pytest.mark.parametrize("parity,bits", [("none", 7), ("even", 8)])
def test_parity_check_is_a_no_op_without_a_parity_bit_in_the_data(parity, bits):
    raw = b"\x80N10"
    assert check_parity(raw, parity, bits, "#") == (raw, 0)


def test_receiver_inserts_the_parity_character_into_the_file(bus, machine, tmp_path,
                                                             fake_transport):
    machine["serial"].update(check_parity=True, parity="even", data_bits=7,
                             parity_insert=r"\35")
    machine["receive"].update(folder=str(tmp_path / "in"), filename_pattern="p.nc",
                              overwrite="allow", idle_timeout_s=1, remove_chars="none",
                              save_line_ending="LF")
    payload = bytearray(_with_parity(b"N10 X1\r\n"))
    payload[1] ^= 0x80
    t = fake_transport()
    receiver = Receiver(bus)
    assert receiver.start(machine, transport=t)
    assert wait_until(lambda: t.is_open, 5.0)
    t.arm_receive(bytes(payload))
    assert wait_until(lambda: not receiver.is_running, 25.0)
    assert receiver.state is ReceiveState.DONE
    assert Path(receiver.last_path).read_text().rstrip() == "N#10 X1"
    assert receiver.snapshot()["errors"] == 1


# ==========================================================================
# Receive engine - triggers modes, start-of-reception data, no idle timeout
# ==========================================================================

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
        save_line_ending="LF",
    )
    return machine


def _receive(bus, machine, transport, payload, timeout=25.0):
    receiver = Receiver(bus)
    assert receiver.start(machine, transport=transport)
    assert wait_until(lambda: transport.is_open, 5.0)
    transport.arm_receive(payload)
    assert wait_until(lambda: not receiver.is_running, timeout)
    return receiver


def test_start_trigger_mode_save_after_trigger_drops_the_trigger(
    bus, rx_machine, fake_transport
):
    rx_machine["receive"].update(start_trigger="%", start_trigger_mode="save_after_trigger")
    receiver = _receive(bus, rx_machine, fake_transport(), b"junk%\r\nN10 X1\r\n")
    text = Path(receiver.last_path).read_text()
    assert "%" not in text and "junk" not in text
    assert "N10 X1" in text


def test_end_trigger_mode_save_until_trigger_drops_the_trigger(
    bus, rx_machine, fake_transport
):
    rx_machine["receive"].update(
        end_trigger="M30", end_trigger_mode="save_until_trigger", idle_timeout_s=30
    )
    receiver = _receive(bus, rx_machine, fake_transport(), b"N10 X1\r\nM30\r\n")
    text = Path(receiver.last_path).read_text()
    assert "M30" not in text
    assert "N10 X1" in text


def test_send_xon_and_start_of_reception_data_go_out_first(
    bus, rx_machine, fake_transport
):
    rx_machine["receive"].update(send_xon=True, start_chars=r"\18")  # DC2
    rx_machine["serial"]["xon_char"] = 17
    t = fake_transport()
    _receive(bus, rx_machine, t, b"N10 X1\r\n")
    assert t.written.startswith(b"\x11\x12")


def test_nothing_is_sent_when_neither_option_is_set(bus, rx_machine, fake_transport):
    t = fake_transport()
    _receive(bus, rx_machine, t, b"N10 X1\r\n")
    assert t.written == b""


def test_receive_without_an_idle_timeout_runs_until_the_end_trigger(
    bus, rx_machine, fake_transport
):
    rx_machine["receive"].update(idle_timeout_s=0, end_trigger="M30")
    receiver = _receive(bus, rx_machine, fake_transport(), b"N10\r\nM30\r\n")
    assert receiver.state is ReceiveState.DONE
    assert "M30" in Path(receiver.last_path).read_text()


def test_receive_progress_reports_cps(bus, rx_machine, fake_transport):
    receiver = _receive(bus, rx_machine, fake_transport(), PROGRAM.encode() * 20)
    assert receiver.snapshot()["cps"] > 0
    assert receiver.snapshot()["errors"] == 0


# ==========================================================================
# The settings form must expose every option
# ==========================================================================

#: Kept in the schema for backwards compatibility, superseded by
#: ``receive.remove_chars``; deliberately absent from the form.
HIDDEN_KEYS = {"receive.strip_control_chars"}


def test_every_machine_setting_has_a_form_control():
    html = (Path(__file__).resolve().parent.parent / "resources/palette/index.html").read_text()
    names = set(re.findall(r'name="([^"]+)"', html))
    machine = default_machine()
    missing = []
    for key, value in machine.items():
        if isinstance(value, dict):
            missing += [
                f"{key}.{sub}" for sub in value if f"{key}.{sub}" not in names
            ]
        elif key != "id" and key not in names:
            missing.append(key)
    assert not [k for k in missing if k not in HIDDEN_KEYS], missing


def test_every_form_enum_is_published_by_the_bridge():
    from moxaserial.bridge import ENUMS

    html = (Path(__file__).resolve().parent.parent / "resources/palette/index.html").read_text()
    used = set(re.findall(r'data-enum="([^"]+)"', html))
    assert used <= set(ENUMS), used - set(ENUMS)
