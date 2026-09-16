"""Find Moxa NPort devices on the network, and probe their serial ports.

Two independent jobs live here because they share the same hard-won
knowledge of how an NPort behaves on the wire:

1. :func:`discover` - Moxa's UDP "search" protocol on port 4800, the one
   NPort Administrator uses. One broadcast, every NPort on the segment
   answers with its MAC and IP; two cheap unicast follow-ups turn that
   into a name, a firmware version, a serial number and a port count.
   :func:`tcp_scan` is the fallback for when broadcast is blocked, which
   is common on Wi-Fi access points - and the W-series is a Wi-Fi device.

2. :func:`probe_ports` - for one device, which serial ports exist, which
   operation mode each is in, whether anything is already connected, and
   what the modem lines are doing.

The UDP wire format
-------------------
Every datagram starts with the same 8-byte big-endian header::

    struct.pack("!BBHI", opcode, status, total_length, sequence)

* ``opcode``  - the request opcode; a reply echoes it with bit 7 set.
* ``status``  - 0 on success, 4 = "I do not support that opcode".
* ``total_length`` - including the 8 header bytes.
* ``sequence`` - echoed verbatim, so replies can be matched to requests.

A **reply** then repeats a 12-byte *device id* at offset 8, which is what
follow-up requests must carry as their body::

    offset  8  uint32 LE   APID / product line
    offset 12  uint16 LE   model id
    offset 14  6 bytes     MAC address

Opcode-specific data starts at offset 20:

* ``0x01`` search  -> 4 bytes, the device's IPv4 address.
* ``0x10`` name    -> NUL-padded ASCII device name (the NPort's "Name").
* ``0x16`` info    -> firmware (uint32 LE at 20, top two bytes are
  major/minor), serial number (uint16 LE at 28), and a serial-port count
  in the last byte.

Verified on hardware
--------------------
An NPort W2250A (firmware 2.2 Build 18082311, serial 9645, name
"KIA_Lathe") answered on 2026-09-16::

    -> 01 00 00 08 00 00 00 00
    <- 81 00 00 18 00000000 5024 0080 5224 402cf4fd4933 c0a802d2
    -> 10 00 00 14 00000000 <device id>
    <- 90 00 00 3c ... "KIA_Lathe\0\0..."
    -> 16 00 00 14 00000000 <device id>
    <- 96 00 00 24 ... 00 00 02 02 | 00 00 03 01 | ad 25 00 00 | 18 00 00 02
                        fw 2.2         ?            serial 9645     ports 2

The model *name* is deliberately not decoded from the model id: no
published tool has a complete id -> name table, and the only id we can
check (0x2452 = W2250A) does not follow the "the hex digits are the model
number" rule the NPort 5000 series follows. The name comes from the web
console's login page instead, which serves it without authentication.
"""

from __future__ import annotations

import ipaddress
import os
import re
import socket
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from moxaserial.log import get_logger
from moxaserial.transport import aspp

log = get_logger("discovery")

DISCOVERY_PORT = 4800

OP_SEARCH = 0x01
OP_NAME = 0x10
OP_INFO = 0x16

#: ``status`` byte meaning "this firmware does not implement that opcode".
STATUS_UNSUPPORTED = 0x04

HEADER_LEN = 8
#: Header + the 12-byte device id that every reply repeats.
REPLY_PREFIX_LEN = 20

#: Model ids we have seen on real hardware. Everything else is reported as
#: a raw id - guessing a marketing name from the number gets it wrong.
KNOWN_MODELS: dict[int, str] = {
    0x2452: "NPort W2250A",
}

#: Serial ports per model name, used only when the device did not tell us.
_PORTS_FROM_NAME = re.compile(r"\bW?2(\d)50A?\b|\bNPort\s+5(\d)\d0", re.I)


def model_name(model_id: int, product_line: int = 0) -> str:
    """Best-effort human name for a numeric model id."""
    known = KNOWN_MODELS.get(model_id)
    if known:
        return known
    return f"Moxa device (model 0x{model_id:04X})"


@dataclass
class NPortDevice:
    """One device found on the network."""

    ip: str = ""
    mac: str = ""
    model: str = ""
    model_id: int = 0
    product_line: int = 0
    name: str = ""
    firmware: str = ""
    serial_number: int = 0
    ports: int = 0
    #: "udp" (answered the broadcast) or "tcp" (found by the port scan).
    source: str = "udp"
    #: TCP ports found open by :func:`tcp_scan`.
    open_ports: list[int] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ip": self.ip,
            "mac": self.mac,
            "model": self.model,
            "modelId": self.model_id,
            "productLine": self.product_line,
            "name": self.name,
            "firmware": self.firmware,
            "serialNumber": self.serial_number,
            "ports": self.ports,
            "source": self.source,
            "openPorts": list(self.open_ports),
        }


# ---------------------------------------------------------------------------
# Wire codec
# ---------------------------------------------------------------------------
def encode_request(opcode: int, sequence: int = 0, body: bytes = b"") -> bytes:
    return struct.pack("!BBHI", opcode, 0, HEADER_LEN + len(body), sequence & 0xFFFFFFFF) + body


class DiscoveryError(Exception):
    """A malformed or rejected discovery datagram."""


def parse_reply(data: bytes, expect: int | None = None) -> tuple[int, int, bytes]:
    """``(opcode, sequence, payload)`` for one reply datagram.

    *payload* is everything after the 20-byte header + device id block.
    Raises :class:`DiscoveryError` for anything that is not a well formed
    successful reply to *expect*.
    """
    if len(data) < REPLY_PREFIX_LEN:
        raise DiscoveryError(f"short reply ({len(data)} bytes)")
    opcode, status, length, sequence = struct.unpack("!BBHI", data[:HEADER_LEN])
    if not opcode & 0x80:
        raise DiscoveryError(f"not a reply (opcode 0x{opcode:02x})")
    op = opcode & 0x7F
    if expect is not None and op != expect:
        raise DiscoveryError(f"reply to 0x{op:02x}, expected 0x{expect:02x}")
    if status == STATUS_UNSUPPORTED:
        raise DiscoveryError(f"device does not support opcode 0x{op:02x}")
    if status != 0:
        raise DiscoveryError(f"device returned status {status} for opcode 0x{op:02x}")
    if length > len(data):
        raise DiscoveryError(f"reply claims {length} bytes, got {len(data)}")
    return op, sequence, data[REPLY_PREFIX_LEN:length]


def device_id(data: bytes) -> bytes:
    """The 12 bytes a follow-up request has to quote back."""
    if len(data) < REPLY_PREFIX_LEN:
        raise DiscoveryError("reply too short to contain a device id")
    return data[HEADER_LEN:REPLY_PREFIX_LEN]


def parse_search_reply(data: bytes) -> NPortDevice:
    """Decode a ``0x81`` search reply into a (partly filled) device."""
    _op, _seq, payload = parse_reply(data, OP_SEARCH)
    if len(payload) < 4:
        raise DiscoveryError("search reply carries no IP address")
    apid, model_id = struct.unpack("<IH", data[8:14])
    mac = ":".join(f"{b:02x}" for b in data[14:20])
    return NPortDevice(
        ip=socket.inet_ntoa(payload[:4]),
        mac=mac,
        model_id=model_id,
        product_line=apid & 0xFFFF,
        model=model_name(model_id, apid & 0xFFFF),
    )


def parse_name_reply(data: bytes) -> str:
    _op, _seq, payload = parse_reply(data, OP_NAME)
    return payload.split(b"\0", 1)[0].decode("latin-1").strip()


def parse_info_reply(data: bytes) -> dict[str, Any]:
    """Firmware / serial number / port count from a ``0x96`` reply."""
    _op, _seq, payload = parse_reply(data, OP_INFO)
    out: dict[str, Any] = {}
    if len(payload) >= 4:
        raw = struct.unpack("<I", payload[:4])[0]
        major, minor = (raw >> 24) & 0xFF, (raw >> 16) & 0xFF
        build = raw & 0xFFFF
        out["firmware"] = f"{major}.{minor}" + (f".{build}" if build else "")
    if len(payload) >= 10:
        out["serial_number"] = struct.unpack("<H", payload[8:10])[0]
    if len(payload) >= 16:
        # Last byte of the block: 2 on a two-port W2250A. Treated as a hint -
        # a zero or absurd value falls back to probing.
        ports = payload[15]
        if 1 <= ports <= 32:
            out["ports"] = ports
    return out


# ---------------------------------------------------------------------------
# Local interface enumeration
# ---------------------------------------------------------------------------
_IFCONFIG_RE = re.compile(
    r"inet\s+(\d+\.\d+\.\d+\.\d+)(?:\s+netmask\s+(\S+))?(?:\s+broadcast\s+(\d+\.\d+\.\d+\.\d+))?"
)
_IP_ADDR_RE = re.compile(r"inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)(?:\s+brd\s+(\d+\.\d+\.\d+\.\d+))?")
_IPCONFIG_RE = re.compile(
    r"IPv4 Address[^:]*:\s*(\d+\.\d+\.\d+\.\d+).*?Subnet Mask[^:]*:\s*(\d+\.\d+\.\d+\.\d+)",
    re.S,
)


def _run(cmd: Sequence[str]) -> str:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv, no shell
            list(cmd), capture_output=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.decode("utf-8", "replace")


def local_networks() -> list[tuple[str, str]]:
    """``[(local_ip, broadcast_ip), ...]`` for every usable IPv4 interface.

    Uses the OS's own tools because the standard library has no way to ask
    for an interface's netmask. Everything is best effort: a /24 is assumed
    when the mask cannot be determined, and the limited broadcast address
    is always tried as well.
    """
    found: dict[str, str] = {}

    def add(ip: str, broadcast: str) -> None:
        if ip.startswith("127.") or ip == "0.0.0.0":
            return
        if broadcast and broadcast != "255.255.255.255":
            found[ip] = broadcast
        else:
            found.setdefault(ip, ip.rsplit(".", 1)[0] + ".255")

    if os.name == "nt":
        text = _run(["ipconfig"])
        for ip, mask in _IPCONFIG_RE.findall(text):
            try:
                net = ipaddress.IPv4Network(f"{ip}/{mask}", strict=False)
                add(ip, str(net.broadcast_address))
            except ValueError:
                add(ip, "")
    else:
        text = _run(["ifconfig"]) or _run(["/sbin/ifconfig"])
        for ip, mask, bcast in _IFCONFIG_RE.findall(text):
            if bcast:
                add(ip, bcast)
            elif mask:
                try:
                    bits = int(mask, 16) if mask.startswith("0x") else int(mask)
                    net = ipaddress.IPv4Network(
                        (ip, str(ipaddress.IPv4Address(bits & 0xFFFFFFFF))), strict=False
                    )
                    add(ip, str(net.broadcast_address))
                except ValueError:
                    add(ip, "")
            else:
                add(ip, "")
        if not found:
            text = _run(["ip", "-4", "-o", "addr"])
            for ip, prefix, bcast in _IP_ADDR_RE.findall(text):
                if bcast:
                    add(ip, bcast)
                else:
                    try:
                        net = ipaddress.IPv4Network(f"{ip}/{prefix}", strict=False)
                        add(ip, str(net.broadcast_address))
                    except ValueError:
                        add(ip, "")

    if not found:
        # Last resort: whatever address the default route uses.
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.connect(("8.8.8.8", 53))
                add(probe.getsockname()[0], "")
            finally:
                probe.close()
        except OSError:
            pass
    return sorted(found.items())


# ---------------------------------------------------------------------------
# UDP discovery
# ---------------------------------------------------------------------------
def _open_udp(bind_ip: str = "") -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    except OSError:
        pass
    sock.bind((bind_ip, 0))
    return sock


def _details(
    sock: socket.socket, dev: NPortDevice, devid: bytes, timeout: float, port: int
) -> None:
    """Fill in name / firmware / serial / ports with two unicast follow-ups.

    Never raises: a device that does not answer simply keeps the fields it
    already has.
    """
    for opcode, apply in (
        (OP_NAME, lambda raw: setattr(dev, "name", parse_name_reply(raw))),
        (OP_INFO, lambda raw: _apply_info(dev, parse_info_reply(raw))),
    ):
        try:
            sock.settimeout(timeout)
            sock.sendto(encode_request(opcode, 0, devid), (dev.ip, port))
            deadline_tries = 3
            while deadline_tries > 0:
                deadline_tries -= 1
                raw, addr = sock.recvfrom(2048)
                if addr[0] != dev.ip:
                    continue
                if raw[0] & 0x7F != opcode:
                    continue
                apply(raw)
                break
        except (OSError, DiscoveryError, IndexError) as exc:
            log.debug("NPort %s did not answer opcode 0x%02x: %s", dev.ip, opcode, exc)


def _apply_info(dev: NPortDevice, info: dict[str, Any]) -> None:
    dev.firmware = info.get("firmware", dev.firmware)
    dev.serial_number = info.get("serial_number", dev.serial_number)
    dev.ports = info.get("ports", dev.ports)


def discover(
    timeout: float = 2.0,
    targets: Iterable[str] | None = None,
    port: int = DISCOVERY_PORT,
    broadcast: bool = True,
    details: bool = True,
    enrich: bool = True,
    on_device: Callable[[NPortDevice], None] | None = None,
) -> list[NPortDevice]:
    """Broadcast a Moxa search and collect every answer.

    *targets* adds extra destinations - unicast IPs, or directed broadcast
    addresses - on top of the automatic per-interface broadcast. Pass an
    explicit list when the network blocks broadcast but you know the IP.

    *on_device* is called once per newly seen device, so a UI can fill in
    progressively instead of waiting for the whole *timeout*.
    """
    destinations: list[str] = []
    if broadcast:
        destinations.append("255.255.255.255")
        for _ip, bcast in local_networks():
            if bcast not in destinations:
                destinations.append(bcast)
    for extra in targets or ():
        extra = str(extra).strip()
        if extra and extra not in destinations:
            destinations.append(extra)

    found: dict[str, NPortDevice] = {}
    devids: dict[str, bytes] = {}
    sock = _open_udp()
    try:
        request = encode_request(OP_SEARCH)
        for dest in destinations:
            try:
                sock.sendto(request, (dest, port))
            except OSError as exc:
                log.debug("Discovery send to %s failed: %s", dest, exc)
        log.info("NPort discovery: sent search to %s", ", ".join(destinations))

        deadline = time.monotonic() + max(0.1, timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sock.settimeout(remaining)
            try:
                raw, addr = sock.recvfrom(2048)
            except (TimeoutError, OSError):
                break
            try:
                dev = parse_search_reply(raw)
            except DiscoveryError as exc:
                log.debug("Ignoring datagram from %s: %s", addr[0], exc)
                continue
            if not dev.ip or dev.ip == "0.0.0.0":
                dev.ip = addr[0]
            if dev.ip in found:
                continue
            found[dev.ip] = dev
            devids[dev.ip] = device_id(raw)
            log.info("Found %s at %s (%s)", dev.model, dev.ip, dev.mac)
    finally:
        sock.close()

    if details and found:
        detail_sock = _open_udp()
        try:
            for ip, dev in found.items():
                _details(
                    detail_sock, dev, devids[ip],
                    timeout=min(1.0, max(0.3, timeout / 2)), port=port,
                )
                if not dev.ports:
                    dev.ports = ports_from_model(dev.model)
        finally:
            detail_sock.close()

    if enrich:
        # Only the web console knows the marketing model name for certain.
        for dev in found.values():
            if dev.model_id not in KNOWN_MODELS or not dev.ports:
                enrich_from_console(dev)

    if on_device is not None:
        for dev in found.values():
            on_device(dev)
    return list(found.values())


def enrich_from_console(dev: NPortDevice, timeout: float = 3.0) -> NPortDevice:
    """Fill in model / name / firmware from the device's own login page.

    The NPort prints all of that above the login form and serves it
    without authentication, which is the only reliable source of a
    marketing model name - and the only source at all for a device found
    by :func:`tcp_scan`, which never sees a UDP reply. Never raises.
    """
    from moxaserial import nport_console  # local: nport_console imports OPMODES from here

    try:
        info = nport_console.device_info(dev.ip, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - enrichment is always optional
        log.debug("Could not read the console login page on %s: %s", dev.ip, exc)
        return dev
    if info.get("model"):
        dev.model = info["model"]
    if info.get("name") and not dev.name:
        dev.name = info["name"]
    if info.get("firmware") and not dev.firmware:
        dev.firmware = info["firmware"]
    if info.get("mac") and not dev.mac:
        dev.mac = info["mac"]
    if info.get("serial_number") and not dev.serial_number:
        dev.serial_number = int(info["serial_number"])
    if not dev.ports:
        dev.ports = ports_from_model(dev.model)
    return dev


def ports_from_model(model: str) -> int:
    """Serial-port count guessed from a model name. 0 when unknown."""
    m = _PORTS_FROM_NAME.search(model or "")
    if not m:
        return 0
    digit = m.group(1) or m.group(2)
    try:
        n = int(digit)
    except (TypeError, ValueError):
        return 0
    return n if 1 <= n <= 16 else 0


# ---------------------------------------------------------------------------
# TCP fallback scan (for networks that swallow broadcast)
# ---------------------------------------------------------------------------
#: Ports that identify an NPort without having to talk any protocol:
#: the ASPP command port, the TCP Server data port, and the web console.
SCAN_PORTS = (aspp.CMD_PORT_BASE, aspp.DATA_PORT_BASE, 80)


def _tcp_open(host: str, port: int, timeout: float) -> bool:
    sock = socket.socket()
    sock.settimeout(timeout)
    try:
        sock.connect((host, port))
        return True
    except OSError:
        return False
    finally:
        try:
            sock.close()
        except OSError:
            pass


def tcp_scan(
    subnet: str,
    ports: Sequence[int] = SCAN_PORTS,
    timeout: float = 0.4,
    workers: int = 64,
    on_device: Callable[[NPortDevice], None] | None = None,
    stop: threading.Event | None = None,
) -> list[NPortDevice]:
    """Sweep *subnet* for hosts with an NPort-shaped port open.

    *subnet* is anything :mod:`ipaddress` accepts - ``192.168.1.0/24``, a
    bare ``192.168.1.`` prefix, or a single address. Only reports hosts
    where the **ASPP command port or the TCP Server data port** is open;
    port 80 alone is every printer on the network, so it is recorded but
    never enough on its own.
    """
    hosts = list(_expand_subnet(subnet))
    if not hosts:
        return []
    log.info("Scanning %d addresses on %s for NPort ports %s", len(hosts), subnet, list(ports))

    results: dict[str, NPortDevice] = {}
    lock = threading.Lock()
    queue = list(hosts)
    qlock = threading.Lock()

    def worker() -> None:
        while True:
            if stop is not None and stop.is_set():
                return
            with qlock:
                if not queue:
                    return
                host = queue.pop()
            open_ports = [p for p in ports if _tcp_open(host, p, timeout)]
            serial_ports = [p for p in open_ports if p != 80]
            if not serial_ports:
                continue
            dev = NPortDevice(ip=host, source="tcp", open_ports=open_ports)
            if 80 in open_ports:
                enrich_from_console(dev, timeout=max(1.0, timeout * 4))
            if not dev.ports:
                dev.ports = 1
            with lock:
                results[host] = dev
            log.info("TCP scan found a device at %s (open: %s)", host, open_ports)
            if on_device is not None:
                on_device(dev)

    threads = [
        threading.Thread(target=worker, name=f"moxa-scan-{i}", daemon=True)
        for i in range(max(1, min(workers, len(hosts))))
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return [results[h] for h in hosts if h in results]


def _expand_subnet(subnet: str) -> list[str]:
    text = str(subnet or "").strip()
    if not text:
        return []
    if text.endswith("."):
        text += "0/24"
    try:
        net = ipaddress.ip_network(text, strict=False)
    except ValueError:
        try:
            return [str(ipaddress.ip_address(text))]
        except ValueError:
            return []
    if net.version != 4:
        return []
    if net.num_addresses > 4096:
        raise ValueError(f"{subnet} is too large to scan ({net.num_addresses} addresses).")
    if net.num_addresses <= 2:
        return [str(net.network_address)]
    return [str(h) for h in net.hosts()]


# ---------------------------------------------------------------------------
# Per-port probing
# ---------------------------------------------------------------------------
#: NPort operation-mode codes, as the web console's ``opmode.asp`` reports
#: them. Verified on a W2250A: Real COM is 256.
OPMODES: dict[int, str] = {
    0: "Disabled",
    256: "Real COM",
    257: "RFC2217",
    512: "TCP Server",
    513: "TCP Client",
    514: "UDP",
    768: "Pair Connection Master",
    769: "Pair Connection Slave",
    1024: "Ethernet Modem",
    1536: "Reverse Terminal",
}


@dataclass
class PortProbe:
    """What one serial port on one NPort looks like from the outside."""

    port_index: int = 1
    cmd_port: int = 0
    data_port: int = 0
    #: The command port accepted a connection.
    reachable: bool = False
    #: The device accepted the TCP connection but hung up - on an NPort
    #: with ``Max connection = 1`` that means somebody else has the port.
    busy: bool = False
    #: "realcom" (950+n answered), "tcp_server" (4001+n answered) or "".
    mode: str = ""
    mode_label: str = ""
    modem: dict[str, bool] | None = None
    #: Line settings read from the web console, when credentials exist.
    settings: dict[str, Any] | None = None
    error: str = ""
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "portIndex": self.port_index,
            "cmdPort": self.cmd_port,
            "dataPort": self.data_port,
            "reachable": self.reachable,
            "busy": self.busy,
            "mode": self.mode,
            "modeLabel": self.mode_label,
            "modem": self.modem,
            "settings": self.settings,
            "error": self.error,
            "elapsedMs": self.elapsed_ms,
        }


#: A PORT_INIT that does not move the port off its current line settings
#: is impossible - the command carries them - so the probe sends the ones
#: the caller intends to use. These are the add-in's own defaults.
DEFAULT_LINE = {"baud": 9600, "data_bits": 8, "parity": "none", "stop_bits": "1"}


def probe_port(
    host: str,
    port_index: int = 1,
    cmd_port: int | None = None,
    data_port: int | None = None,
    line: dict[str, Any] | None = None,
    timeout: float = 2.0,
    retries: int = 1,
) -> PortProbe:
    """Probe one serial port. Never raises; the failure is in the result.

    The whole probe is bounded by *timeout* and every socket is closed
    before returning, because an NPort with ``Max connection = 1`` cannot
    be doing anything else while we hold one open.

    An NPort will **reset the command socket** unless the first frame it
    receives is ``PORT_INIT``, and it will not answer that ``PORT_INIT``
    until the matching data socket is also connected - both verified on a
    W2250A. So the probe opens the data socket too, and which data port
    accepts tells us the operation mode: Real COM listens on 950+n, TCP
    Server on 4001+n, and the other one is refused.
    """
    started = time.monotonic()
    line = {**DEFAULT_LINE, **(line or {})}
    cmd_port = int(cmd_port or aspp.cmd_port_for(port_index))
    result = PortProbe(port_index=port_index, cmd_port=cmd_port, data_port=int(data_port or 0))

    candidates: list[tuple[str, int]] = (
        [("", int(data_port))]
        if data_port
        else [
            ("realcom", aspp.data_port_for(port_index, aspp.REALCOM_DATA_PORT_BASE)),
            ("tcp_server", aspp.data_port_for(port_index, aspp.DATA_PORT_BASE)),
        ]
    )

    # *timeout* bounds the WHOLE call, retry included, so a UI probing
    # eight ports in a row has a predictable worst case.
    deadline = started + max(0.5, timeout)
    attempt = 0
    while True:
        attempt += 1
        _probe_once(host, result, candidates, line, max(0.4, deadline - time.monotonic()))
        if not result.reachable or not result.busy:
            break
        if attempt > max(0, retries) or deadline - time.monotonic() < 0.7:
            break
        # The device needs a breath between connections; one retry turns
        # most spurious "busy" answers into a clean probe.
        time.sleep(0.25)
    result.elapsed_ms = int((time.monotonic() - started) * 1000)
    return result


def _probe_once(
    host: str,
    result: PortProbe,
    candidates: list[tuple[str, int]],
    line: dict[str, Any],
    timeout: float,
) -> None:
    result.busy = False
    result.error = ""
    connect_timeout = max(0.3, timeout / 3)
    cmd = socket.socket()
    cmd.settimeout(connect_timeout)
    try:
        cmd.connect((host, result.cmd_port))
    except OSError as exc:
        result.reachable = False
        result.error = _friendly(exc, f"command port {result.cmd_port}")
        cmd.close()
        return
    result.reachable = True
    cmd.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)

    data: socket.socket | None = None
    try:
        for mode, port in candidates:
            sock = socket.socket()
            sock.settimeout(connect_timeout)
            try:
                sock.connect((host, port))
            except OSError:
                sock.close()
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            data = sock
            result.data_port = port
            if mode:
                result.mode = mode
                result.mode_label = "Real COM" if mode == "realcom" else "TCP Server"
            break
        if data is None:
            result.busy = True
            result.error = (
                "The command port answered but no data port did. The serial port may be "
                "disabled, or another host holds the connection (Max connection = 1)."
            )
            return

        request = aspp.encode_port_init(
            int(line.get("baud", 9600)),
            int(line.get("data_bits", 8)),
            str(line.get("parity", "none")),
            str(line.get("stop_bits", "1")),
            bool(line.get("assert_dtr", True)),
            bool(line.get("assert_rts", True)),
            str(line.get("flow_control", "none")) in ("rtscts", "both"),
            False,
            False,
        )
        try:
            cmd.sendall(request)
        except OSError as exc:
            result.busy = True
            result.error = _friendly(exc, "the command port")
            return

        reply = _read_port_init(cmd, deadline_s=max(0.4, timeout / 2))
        if reply is None:
            result.busy = True
            result.error = (
                "The NPort closed the connection instead of answering PORT_INIT - "
                "usually another host already has this port (Max connection = 1)."
            )
            return
        if not _still_open(data):
            # The NPort accepted the data connection and then dropped it,
            # which is what it does when the port's connection budget is
            # already spent. The command channel can stay up regardless.
            result.busy = True
            result.error = (
                f"The NPort closed the data port {result.data_port} straight away - "
                "another host already has this serial port (Max connection = 1)."
            )
            return
        try:
            lines = aspp.decode_lines(reply)
        except aspp.ProtocolError as exc:
            result.error = str(exc)
            return
        result.modem = lines if lines is not None else None
        if lines is None:
            result.error = f"The NPort rejected baud rate {line.get('baud')}."
    finally:
        for sock in (data, cmd):
            if sock is None:
                continue
            try:
                sock.close()
            except OSError:
                pass


def _still_open(sock: socket.socket) -> bool:
    """True unless the peer has already hung up on *sock*.

    A zero-timeout read: ``b""`` is a FIN, a timeout means the connection
    is alive and simply idle. Any bytes that arrive are serial traffic we
    are about to throw away by closing the socket anyway.
    """
    try:
        sock.settimeout(0.05)
        return sock.recv(64) != b""
    except TimeoutError:
        return True
    except BlockingIOError:
        return True
    except OSError:
        return False


def _read_port_init(sock: socket.socket, deadline_s: float) -> aspp.Response | None:
    deadline = time.monotonic() + deadline_s
    buf = bytearray()
    while time.monotonic() < deadline:
        sock.settimeout(max(0.05, deadline - time.monotonic()))
        try:
            chunk = sock.recv(64)
        except TimeoutError:
            break
        except OSError:
            return None
        if not chunk:
            return None
        buf += chunk
        try:
            frames, _rest = aspp.split_frames(buf)
        except aspp.ProtocolError:
            return None
        for frame in frames:
            if frame.opcode == aspp.Cmd.PORT_INIT:
                return frame
    return None


def _friendly(exc: OSError, what: str) -> str:
    if isinstance(exc, TimeoutError):  # socket.timeout is an alias since 3.10
        return f"No answer from {what} (timed out)."
    if isinstance(exc, ConnectionRefusedError):
        return f"{what.capitalize()} refused the connection."
    return f"{what.capitalize()}: {exc}"


def probe_ports(
    host: str,
    count: int = 2,
    first: int = 1,
    line: dict[str, Any] | None = None,
    timeout: float = 2.0,
    console_settings: dict[int, dict[str, Any]] | None = None,
    opmodes: dict[int, str] | None = None,
    on_port: Callable[[PortProbe], None] | None = None,
    stop: threading.Event | None = None,
    skip: set[int] | None = None,
) -> list[PortProbe]:
    """Probe *count* consecutive serial ports, sequentially.

    Sequential on purpose: the NPort is a small embedded device and
    hammering every port at once makes it drop connections.

    *console_settings* (from :mod:`moxaserial.nport_console`) makes the
    probe non-destructive - each port's ``PORT_INIT`` echoes back the
    settings the device already has instead of imposing new ones.

    *skip* is the set of port indices the caller already holds open. They
    are reported as busy without a single byte going out: ``PORT_INIT``
    applies line settings, so probing a port we are mid-transfer on would
    change the baud rate under our own job.
    """
    out: list[PortProbe] = []
    for index in range(first, first + max(1, count)):
        if stop is not None and stop.is_set():
            break
        if skip and index in skip:
            probe = PortProbe(
                port_index=index,
                cmd_port=aspp.CMD_PORT_BASE + index,
                reachable=True,
                busy=True,
                error="In use by this add-in - not probed.",
            )
            out.append(probe)
            if on_port is not None:
                on_port(probe)
            continue
        port_line = dict(line or {})
        stored = (console_settings or {}).get(index)
        if stored:
            port_line = {**port_line, **_line_from_console(stored)}
        probe = probe_port(host, index, line=port_line, timeout=timeout)
        if stored:
            probe.settings = stored
        label = (opmodes or {}).get(index)
        if label:
            probe.mode_label = label
            probe.mode = _mode_key(label)
        out.append(probe)
        if on_port is not None:
            on_port(probe)
    return out


def _mode_key(label: str) -> str:
    low = (label or "").lower()
    if "real com" in low or "realcom" in low:
        return "realcom"
    if "tcp server" in low:
        return "tcp_server"
    return ""


def _line_from_console(stored: dict[str, Any]) -> dict[str, Any]:
    """Console port settings -> the keys :func:`probe_port` wants."""
    out: dict[str, Any] = {}
    if stored.get("baud"):
        out["baud"] = int(stored["baud"])
    if stored.get("data_bits"):
        out["data_bits"] = int(stored["data_bits"])
    if stored.get("parity"):
        out["parity"] = str(stored["parity"]).lower()
    if stored.get("stop_bits"):
        out["stop_bits"] = str(stored["stop_bits"])
    flow = str(stored.get("flow_control", "")).lower()
    if flow:
        out["flow_control"] = flow
    return out


def describe_support() -> dict[str, Any]:
    """Surfaced in the UI's About page next to the ASPP summary."""
    return {
        "udp_search": True,
        "tcp_fallback": True,
        "port_probe": True,
        "platform": sys.platform,
        "notes": (
            "UDP search on port 4800 (opcodes 0x01 search, 0x10 name, 0x16 info) plus a "
            "TCP fallback sweep. Verified against an NPort W2250A on 2026-09-16."
        ),
    }
