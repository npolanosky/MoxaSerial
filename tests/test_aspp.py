"""ASPP codec: framing, tables, response splitting, decoders."""

from __future__ import annotations

import struct

import pytest

from moxaserial.transport import aspp
from moxaserial.transport.aspp import Cmd


def test_port_numbers():
    assert aspp.data_port_for(1) == 4001
    assert aspp.data_port_for(2) == 4002
    assert aspp.data_port_for(1, aspp.REALCOM_DATA_PORT_BASE) == 950
    assert aspp.cmd_port_for(1) == 966
    assert aspp.cmd_port_for(2) == 967
    assert aspp.cmd_port_for(0) == 966  # clamps to port 1


def test_mode_byte_matches_npreal2_constants():
    # 8N1 = BITS8(3) | STOP1(0) | NONE(0)
    assert aspp.mode_byte(8, "none", "1") == 3
    # 7E2 = BITS7(2) | STOP2(4) | EVEN(8)
    assert aspp.mode_byte(7, "even", "2") == 14
    assert aspp.mode_byte(8, "odd", "1") == 3 | 16
    assert aspp.mode_byte(8, "mark", "1") == 3 | 24
    assert aspp.mode_byte(8, "space", "1") == 3 | 32
    with pytest.raises(ValueError):
        aspp.mode_byte(9, "none", "1")


def test_baud_index_table():
    assert aspp.baud_index(9600) == 6
    assert aspp.baud_index(115200) == 10
    assert aspp.baud_index(50) == 18
    assert aspp.baud_index(12345) == aspp.BAUD_CUSTOM


def test_encode_port_init_layout():
    f = aspp.encode_port_init(9600, 7, "even", "2", dtr=True, rts=False, rtscts=True, xon=True, xoff=False)
    assert f == bytes([Cmd.PORT_INIT, 8, 6, 14, 1, 0, 1, 1, 1, 0])


def test_encode_misc():
    assert aspp.encode_setbaud(19200) == bytes([Cmd.SETBAUD, 4]) + struct.pack("<i", 19200)
    assert aspp.encode_linectrl(False, True) == bytes([Cmd.LINECTRL, 2, 0, 1])
    assert aspp.encode_xonxoff(0x11, 0x13) == bytes([Cmd.XONXOFF, 2, 0x11, 0x13])
    assert aspp.encode_flush(aspp.FLUSH_TX) == bytes([Cmd.FLUSH, 1, 1])
    assert aspp.encode_tx_fifo(16) == bytes([Cmd.TX_FIFO, 1, 16])
    assert aspp.encode_tx_fifo(0) == bytes([Cmd.TX_FIFO, 1, 1])
    assert aspp.encode_lstatus() == bytes([Cmd.LSTATUS, 0])
    assert aspp.encode_oqueue() == bytes([Cmd.OQUEUE, 0])
    assert aspp.encode_alive(0xAB) == bytes([Cmd.ALIVE, 1, 0xAB])
    assert aspp.encode_simple(Cmd.START_BREAK) == bytes([Cmd.START_BREAK, 0])
    assert aspp.encode_wait_oqueue()[:2] == bytes([Cmd.WAIT_OQUEUE, 4])
    assert aspp.flowctrl_mask("rtscts") == aspp.F_RTS | aspp.F_CTS
    assert aspp.flowctrl_mask("both") == 0x0F
    assert aspp.flowctrl_mask("none") == 0


def test_split_frames_mixed_stream():
    stream = (
        bytes([Cmd.LINECTRL]) + b"OK"
        + bytes([Cmd.POLLING, 1, 0x42])
        + bytes([Cmd.NOTIFY, aspp.NOTIFY_MSR_CHG, aspp.MSR_CTS | aspp.MSR_DSR, 0])
        + bytes([Cmd.PORT_INIT, 3, 1, 1, 0])
        + bytes([Cmd.OQUEUE, 2, 0x34, 0x12])
        + bytes([Cmd.LSTATUS, 3])  # incomplete tail
    )
    frames, rest = aspp.split_frames(stream)
    assert [f.opcode for f in frames] == [Cmd.LINECTRL, Cmd.POLLING, Cmd.NOTIFY, Cmd.PORT_INIT, Cmd.OQUEUE]
    assert frames[0].ok
    assert aspp.polling_token(frames[1]) == 0x42
    note = aspp.decode_notify(frames[2])
    assert note.modem_changed and note.modem == {"cts": True, "dsr": True, "dcd": False, "ri": False}
    assert note.errors == []
    assert aspp.decode_lines(frames[3]) == {"dsr": True, "cts": True, "dcd": False}
    assert aspp.decode_queue(frames[4]) == 0x1234
    assert rest == bytes([Cmd.LSTATUS, 3])


def test_split_frames_unknown_opcode_is_desync():
    with pytest.raises(aspp.ProtocolError):
        aspp.split_frames(bytes([0x99, 0, 0]))


def test_decode_lines_rejected_baud():
    resp = aspp.Response(Cmd.PORT_INIT, bytes([Cmd.PORT_INIT, 3, 0xFF, 0xFF, 0xFF]))
    assert aspp.decode_lines(resp) is None


def test_notify_errors():
    resp = aspp.Response(Cmd.NOTIFY, bytes([Cmd.NOTIFY, aspp.NOTIFY_PARITY | aspp.NOTIFY_BREAK, 0, 0]))
    note = aspp.decode_notify(resp)
    assert not note.modem_changed
    assert note.errors == ["parity error", "break received"]


def test_describe_support():
    d = aspp.describe_support()
    assert d["implemented"] and d["command_channel"]


def test_late_reply_is_consumed_before_the_pending_request():
    """A reply to a timed-out request must not be taken as the answer to
    the next request of the same opcode (would report a stale queue count)."""
    from moxaserial.transport.moxa import MoxaTransport

    t = MoxaTransport()
    t._stale_ops[Cmd.OQUEUE] = 1
    t._pending_op = Cmd.OQUEUE
    late = aspp.Response(Cmd.OQUEUE, bytes([Cmd.OQUEUE, 2, 0, 0]))
    t._dispatch(late)
    assert t._pending_resp is None
    assert t._stale_ops[Cmd.OQUEUE] == 0
    fresh = aspp.Response(Cmd.OQUEUE, bytes([Cmd.OQUEUE, 2, 7, 0]))
    t._dispatch(fresh)
    assert t._pending_resp is fresh
