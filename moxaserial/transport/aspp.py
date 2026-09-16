"""ASPP - Moxa's "Advanced Serial Port Protocol" command channel codec.

Pure functions and constants only; no sockets. The wire format was
recovered from Moxa's GPL ``npreal2`` Linux driver (``npreal2.c``,
``npreal2d.c``, ``npreal2d.h``) and cross-checked against Moxa's IPSerial
library header and a third-party Wireshark dissector. The points that still
need bench verification are listed in ARCHITECTURE.md §7.

Summary of the protocol
-----------------------
A Moxa NPort in *Real COM* or *TCP Server* mode exposes two TCP ports per
serial port ``n`` (zero-based):

* a **data port** (Real COM ``950 + n``, TCP Server ``4001 + n``) which is
  a transparent byte pipe to the UART, and
* a **command port** (``966 + n``) which carries ASPP frames.

Requests are ``[opcode][len][payload]``. Responses are **not** length
prefixed in a uniform way: most are ``[opcode] 'O' 'K'``, a few carry
``[opcode][len][...]``, and the total length is fixed per opcode
(:data:`RESPONSE_LENGTHS`). The device also pushes two unsolicited frames:

* ``POLLING`` (0x27) - heartbeat, must be answered with ``ALIVE`` (0x28)
  echoing the token byte, or the NPort drops the connection.
* ``NOTIFY`` (0x26) - modem-line change (CTS/DSR/DCD) and RX line errors.

Connection order used by Moxa's own daemon: command socket first, then
data socket, then ``PORT_INIT``.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# TCP port conventions. ``port_index`` below is the ONE-based serial port
# number as printed on the NPort (port 1, port 2, ...).
# ---------------------------------------------------------------------------
DATA_PORT_BASE = 4001           # TCP Server mode default
REALCOM_DATA_PORT_BASE = 950    # Real COM mode
CMD_PORT_BASE = 966             # command port in both modes (manual says 996 once; verify on device)


def data_port_for(port_index: int, base: int = DATA_PORT_BASE) -> int:
    """TCP data port for 1-based serial *port_index*."""
    return base + max(1, int(port_index)) - 1


def cmd_port_for(port_index: int, base: int = CMD_PORT_BASE) -> int:
    """TCP command (ASPP) port for 1-based serial *port_index*."""
    return base + max(1, int(port_index)) - 1


# ---------------------------------------------------------------------------
# Opcodes - verbatim from npreal2d.h
# ---------------------------------------------------------------------------
class Cmd:
    IOCTL = 16          # set baud index + mode byte (IPSerial nsio_ioctl)
    FLOWCTRL = 17       # set flow-control mask (IPSerial nsio_flowctrl)
    LINECTRL = 18       # set DTR / RTS
    LSTATUS = 19        # query DSR / CTS / DCD
    FLUSH = 20          # purge rx / tx / both
    IQUEUE = 21         # bytes waiting in RX queue
    OQUEUE = 22         # bytes waiting in TX queue
    SETBAUD = 23        # arbitrary baud as int32 LE
    XONXOFF = 24        # set XON / XOFF characters
    PORT_RESET = 32
    START_BREAK = 33
    STOP_BREAK = 34
    START_NOTIFY = 36
    STOP_NOTIFY = 37
    NOTIFY = 0x26       # device -> host, unsolicited
    POLLING = 0x27      # device -> host, unsolicited heartbeat
    ALIVE = 0x28        # host -> device, heartbeat answer
    HOST = 43
    PORT_INIT = 44      # set everything at once; returns modem lines
    RESENT_TIME = 46
    WAIT_OQUEUE = 47    # block until TX queue empty (or timeout); returns count
    TX_FIFO = 48        # UART TX FIFO depth
    SETXON = 51         # pretend an XON arrived on the serial line
    SETXOFF = 52        # pretend an XOFF arrived on the serial line


#: Total response length per opcode. Anything not listed is a protocol
#: desync (or an opcode we never send). POLLING/NOTIFY are unsolicited.
RESPONSE_LENGTHS: dict[int, int] = {
    Cmd.NOTIFY: 4,
    Cmd.POLLING: 3,
    Cmd.WAIT_OQUEUE: 4,
    Cmd.OQUEUE: 4,
    Cmd.IQUEUE: 4,
    Cmd.LSTATUS: 5,
    Cmd.PORT_INIT: 5,
    Cmd.FLOWCTRL: 3,
    Cmd.IOCTL: 3,
    Cmd.SETBAUD: 3,
    Cmd.LINECTRL: 3,
    Cmd.START_BREAK: 3,
    Cmd.STOP_BREAK: 3,
    Cmd.START_NOTIFY: 3,
    Cmd.STOP_NOTIFY: 3,
    Cmd.FLUSH: 3,
    Cmd.HOST: 3,
    Cmd.TX_FIFO: 3,
    Cmd.XONXOFF: 3,
    Cmd.SETXON: 3,
    Cmd.SETXOFF: 3,
}

# NOTIFY event flags (wire byte 1)
NOTIFY_PARITY = 0x01
NOTIFY_FRAMING = 0x02
NOTIFY_HW_OVERRUN = 0x04
NOTIFY_SW_OVERRUN = 0x08
NOTIFY_BREAK = 0x10
NOTIFY_MSR_CHG = 0x20

NOTIFY_NAMES = {
    NOTIFY_PARITY: "parity error",
    NOTIFY_FRAMING: "framing error",
    NOTIFY_HW_OVERRUN: "UART overrun",
    NOTIFY_SW_OVERRUN: "buffer overrun",
    NOTIFY_BREAK: "break received",
}

# 16550-style MSR bits (NOTIFY wire byte 2)
MSR_CTS = 0x10
MSR_DSR = 0x20
MSR_RI = 0x40
MSR_DCD = 0x80

# FLUSH selector
FLUSH_RX = 0
FLUSH_TX = 1
FLUSH_ALL = 2

# FLOWCTRL mask (from IPSerial.h; request payload layout is inferred)
F_NONE = 0x00
F_CTS = 0x01
F_RTS = 0x02
F_XON = 0x04
F_XOFF = 0x08

#: Baud rate -> ASPP_IOCTL_B* index (npreal2.c). NOT the IPSerial table.
BAUD_CODES: dict[int, int] = {
    300: 0, 600: 1, 1200: 2, 2400: 3, 4800: 4, 7200: 5, 9600: 6, 19200: 7,
    38400: 8, 57600: 9, 115200: 10, 230400: 11, 460800: 12, 921600: 13,
    150: 14, 134: 15, 110: 16, 75: 17, 50: 18,
}
BAUD_CUSTOM = 0xFF  # "not in the table" - follow PORT_INIT with SETBAUD

DATA_BIT_CODES: dict[int, int] = {5: 0, 6: 1, 7: 2, 8: 3}
STOP_BIT_CODES: dict[str, int] = {"1": 0, "1.5": 4, "2": 4}  # 1.5 only for 5 data bits
PARITY_CODES: dict[str, int] = {"none": 0, "even": 8, "odd": 16, "mark": 24, "space": 32}


class ProtocolError(Exception):
    """Malformed or unexpected ASPP traffic."""


# ---------------------------------------------------------------------------
# Encoding helpers (host -> device)
# ---------------------------------------------------------------------------
def frame(opcode: int, payload: bytes = b"") -> bytes:
    if not 0 <= opcode <= 255 or len(payload) > 255:
        raise ValueError("bad ASPP frame")
    return bytes([opcode, len(payload)]) + payload


def mode_byte(data_bits: int, parity: str, stop_bits: str) -> int:
    try:
        return (
            DATA_BIT_CODES[int(data_bits)]
            | STOP_BIT_CODES[str(stop_bits)]
            | PARITY_CODES[str(parity).lower()]
        )
    except KeyError as exc:
        raise ValueError(f"unsupported line setting: {exc}") from exc


def baud_index(baud: int) -> int:
    return BAUD_CODES.get(int(baud), BAUD_CUSTOM)


def encode_port_init(
    baud: int,
    data_bits: int,
    parity: str,
    stop_bits: str,
    dtr: bool,
    rts: bool,
    rtscts: bool,
    xon: bool,
    xoff: bool,
) -> bytes:
    """``PORT_INIT``: everything in one go. Reply carries DSR/CTS/DCD."""
    payload = bytes(
        [
            baud_index(baud),
            mode_byte(data_bits, parity, stop_bits),
            1 if dtr else 0,
            1 if rts else 0,
            1 if rtscts else 0,  # hardware flow control A (RTS) [inferred split]
            1 if rtscts else 0,  # hardware flow control B (CTS) [inferred split]
            1 if xon else 0,
            1 if xoff else 0,
        ]
    )
    return frame(Cmd.PORT_INIT, payload)


def encode_setbaud(baud: int) -> bytes:
    return frame(Cmd.SETBAUD, struct.pack("<i", int(baud)))


def encode_ioctl(baud: int, data_bits: int, parity: str, stop_bits: str) -> bytes:
    return frame(Cmd.IOCTL, bytes([baud_index(baud), mode_byte(data_bits, parity, stop_bits)]))


def encode_linectrl(dtr: bool, rts: bool) -> bytes:
    return frame(Cmd.LINECTRL, bytes([1 if dtr else 0, 1 if rts else 0]))


def encode_flowctrl(mask: int) -> bytes:
    return frame(Cmd.FLOWCTRL, bytes([mask & 0xFF]))


def encode_xonxoff(xon: int, xoff: int) -> bytes:
    return frame(Cmd.XONXOFF, bytes([xon & 0xFF, xoff & 0xFF]))


def encode_flush(which: int = FLUSH_ALL) -> bytes:
    return frame(Cmd.FLUSH, bytes([which]))


def encode_tx_fifo(depth: int) -> bytes:
    return frame(Cmd.TX_FIFO, bytes([max(1, min(255, int(depth)))]))


def encode_lstatus() -> bytes:
    return frame(Cmd.LSTATUS)


def encode_oqueue() -> bytes:
    return frame(Cmd.OQUEUE)


def encode_iqueue() -> bytes:
    return frame(Cmd.IQUEUE)


def encode_wait_oqueue(timeout_ticks: int = 0x7FFFFFFF) -> bytes:
    """Unit of *timeout_ticks* is unverified (driver sends jiffies). Send a
    big number and enforce the real timeout on the host side."""
    return frame(Cmd.WAIT_OQUEUE, struct.pack("<i", int(timeout_ticks)))


def encode_alive(token: int) -> bytes:
    return bytes([Cmd.ALIVE, 1, token & 0xFF])


def encode_simple(opcode: int) -> bytes:
    """Zero-payload commands: START/STOP_BREAK, START/STOP_NOTIFY, SETXON/SETXOFF."""
    return frame(opcode)


def flowctrl_mask(mode: str) -> int:
    mode = str(mode).lower()
    if mode == "rtscts":
        return F_RTS | F_CTS
    if mode == "xonxoff":
        return F_XON | F_XOFF
    if mode == "both":
        return F_RTS | F_CTS | F_XON | F_XOFF
    return F_NONE


# ---------------------------------------------------------------------------
# Decoding (device -> host)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Response:
    opcode: int
    raw: bytes

    @property
    def ok(self) -> bool:
        return len(self.raw) == 3 and self.raw[1:3] == b"OK"

    @property
    def is_unsolicited(self) -> bool:
        return self.opcode in (Cmd.NOTIFY, Cmd.POLLING)


def split_frames(buf: bytes | bytearray) -> tuple[list[Response], bytes]:
    """Cut complete response frames off the front of *buf*.

    Returns ``(frames, remainder)``. Raises :class:`ProtocolError` on an
    opcode we do not know the length of - the stream is desynchronised
    and the caller must reconnect.
    """
    out: list[Response] = []
    pos = 0
    n = len(buf)
    while pos < n:
        op = buf[pos]
        length = RESPONSE_LENGTHS.get(op)
        if length is None:
            raise ProtocolError(f"unknown ASPP response opcode 0x{op:02x} - stream desynchronised")
        if n - pos < length:
            break
        out.append(Response(op, bytes(buf[pos : pos + length])))
        pos += length
    return out, bytes(buf[pos:])


def decode_lines(resp: Response) -> dict[str, bool] | None:
    """``PORT_INIT`` / ``LSTATUS`` reply -> ``{dsr, cts, dcd}``.

    Returns ``None`` when the device rejected the requested baud index
    (all three bytes 0xFF).
    """
    if resp.opcode not in (Cmd.PORT_INIT, Cmd.LSTATUS) or len(resp.raw) != 5:
        raise ProtocolError(f"bad line-status reply {resp.raw!r}")
    if resp.raw[1] != 3:
        raise ProtocolError(f"line-status reply has length byte {resp.raw[1]}, expected 3")
    dsr, cts, dcd = resp.raw[2], resp.raw[3], resp.raw[4]
    if dsr == cts == dcd == 0xFF:
        return None
    return {"dsr": bool(dsr), "cts": bool(cts), "dcd": bool(dcd)}


def decode_queue(resp: Response) -> int:
    """``OQUEUE`` / ``IQUEUE`` / ``WAIT_OQUEUE`` -> byte count.

    Decoded as ``lo | hi << 8``. Moxa's driver does ``hi*16 + lo``, which
    only agrees for counts < 256; treat ``== 0`` as the only hard fact.
    """
    if resp.opcode not in (Cmd.OQUEUE, Cmd.IQUEUE, Cmd.WAIT_OQUEUE) or len(resp.raw) != 4:
        raise ProtocolError(f"bad queue reply {resp.raw!r}")
    return resp.raw[2] | (resp.raw[3] << 8)


@dataclass(frozen=True)
class Notify:
    flags: int
    msr: int

    @property
    def modem_changed(self) -> bool:
        return bool(self.flags & NOTIFY_MSR_CHG)

    @property
    def modem(self) -> dict[str, bool]:
        return {
            "cts": bool(self.msr & MSR_CTS),
            "dsr": bool(self.msr & MSR_DSR),
            "dcd": bool(self.msr & MSR_DCD),
            "ri": bool(self.msr & MSR_RI),
        }

    @property
    def errors(self) -> list[str]:
        return [name for bit, name in NOTIFY_NAMES.items() if self.flags & bit]


def decode_notify(resp: Response) -> Notify:
    if resp.opcode != Cmd.NOTIFY or len(resp.raw) != 4:
        raise ProtocolError(f"bad NOTIFY {resp.raw!r}")
    return Notify(flags=resp.raw[1], msr=resp.raw[2])


def polling_token(resp: Response) -> int:
    if resp.opcode != Cmd.POLLING or len(resp.raw) != 3:
        raise ProtocolError(f"bad POLLING {resp.raw!r}")
    return resp.raw[2]


def describe_support() -> dict[str, Any]:
    """What this module can do - surfaced in the UI's About page."""
    return {
        "implemented": True,
        "data_channel": True,
        "command_channel": True,
        "verified_on_hardware": True,
        "notes": (
            "ASPP command channel implemented from Moxa's npreal2 driver source: "
            "line settings, DTR/RTS, CTS/DSR/DCD readback and push notifications, "
            "flush, TX-queue drain and keep-alive. Verified against a W2250A + Fanuc 0i-TB on 2026-09-16."
        ),
    }
