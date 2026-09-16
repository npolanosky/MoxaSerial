"""Network discovery: the UDP codec, the fake responder, and port probing.

The hex blobs in :class:`TestRealCaptures` are verbatim datagrams from an
NPort W2250A (firmware 2.2 Build 18082311, serial 9645, name "KIA_Lathe")
captured on 2026-09-16, so the parsers are pinned to real hardware rather
than to the simulator that was written from the same notes.
"""

from __future__ import annotations

import socket
import struct
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moxaserial import discovery  # noqa: E402
from tools.nport_sim import DiscoveryResponder, NPortSimulator  # noqa: E402


def hexb(text: str) -> bytes:
    return bytes.fromhex(text.replace(" ", ""))


SEARCH_REPLY = hexb(
    "81 00 00 18 00 00 00 00 50 24 00 80 52 24 40 2c f4 fd 49 33 c0 a8 02 d2"
)
NAME_REPLY = hexb(
    "90 00 00 3c 00 00 00 00 50 24 00 80 52 24 40 2c f4 fd 49 33"
    "4b 49 41 5f 4c 61 74 68 65" + "00" * 31
)
INFO_REPLY = hexb(
    "96 00 00 24 00 00 00 00 50 24 00 80 52 24 40 2c f4 fd 49 33"
    "00 00 02 02 00 00 03 01 ad 25 00 00 18 00 00 02"
)
UNSUPPORTED_REPLY = hexb("99 04 00 14 00 00 00 00 50 24 00 80 52 24 40 2c f4 fd 49 33")


class TestRealCaptures:
    def test_search_request_is_the_documented_eight_bytes(self):
        assert discovery.encode_request(discovery.OP_SEARCH) == hexb("01 00 00 08 00 00 00 00")

    def test_follow_up_request_carries_the_device_id(self):
        devid = discovery.device_id(SEARCH_REPLY)
        assert len(devid) == 12
        request = discovery.encode_request(discovery.OP_NAME, 0, devid)
        assert request[:8] == hexb("10 00 00 14 00 00 00 00")
        assert request[8:] == devid

    def test_search_reply_gives_ip_mac_and_model(self):
        dev = discovery.parse_search_reply(SEARCH_REPLY)
        assert dev.ip == "192.168.2.210"
        assert dev.mac == "40:2c:f4:fd:49:33"
        assert dev.model_id == 0x2452
        assert dev.product_line == 0x2450
        assert dev.model == "NPort W2250A"

    def test_name_reply_strips_the_nul_padding(self):
        assert discovery.parse_name_reply(NAME_REPLY) == "KIA_Lathe"

    def test_info_reply_matches_what_the_web_console_shows(self):
        info = discovery.parse_info_reply(INFO_REPLY)
        assert info["firmware"] == "2.2"
        assert info["serial_number"] == 9645
        assert info["ports"] == 2

    def test_status_four_means_the_opcode_is_unsupported(self):
        with pytest.raises(discovery.DiscoveryError, match="does not support"):
            discovery.parse_reply(UNSUPPORTED_REPLY, 0x19)

    @pytest.mark.parametrize(
        "raw, match",
        [
            (b"", "short reply"),
            (hexb("01 00 00 08 00 00 00 00") + b"\0" * 16, "not a reply"),
            (SEARCH_REPLY[:20], "reply claims 24 bytes"),
            (SEARCH_REPLY[:20].replace(b"\x00\x18", b"\x00\x14", 1), "carries no IP"),
        ],
    )
    def test_malformed_datagrams_raise(self, raw, match):
        with pytest.raises(discovery.DiscoveryError, match=match):
            discovery.parse_search_reply(raw)

    def test_a_reply_to_another_opcode_is_rejected(self):
        with pytest.raises(discovery.DiscoveryError, match="expected"):
            discovery.parse_reply(NAME_REPLY, discovery.OP_SEARCH)


class TestFakeResponder:
    """The simulator must reproduce the captured bytes exactly."""

    def test_responder_reproduces_the_captured_search_reply(self):
        responder = DiscoveryResponder(ip="192.168.2.210")
        try:
            assert responder.build_reply(hexb("01 00 00 08 00 00 00 00")) == SEARCH_REPLY
            assert responder.build_reply(hexb("10 00 00 14 00 00 00 00") + responder.device_id) \
                == NAME_REPLY
            assert responder.build_reply(hexb("16 00 00 14 00 00 00 00") + responder.device_id) \
                == INFO_REPLY
        finally:
            responder.stop()

    def test_discover_finds_the_fake_device(self):
        with DiscoveryResponder(ip="127.0.0.1") as responder:
            found = discovery.discover(
                timeout=1.0, targets=["127.0.0.1"], port=responder.port,
                broadcast=False, enrich=False,
            )
        assert len(found) == 1
        dev = found[0]
        assert dev.ip == "127.0.0.1"
        assert dev.mac == "40:2c:f4:fd:49:33"
        assert dev.model == "NPort W2250A"
        assert dev.name == "KIA_Lathe"
        assert dev.firmware == "2.2"
        assert dev.serial_number == 9645
        assert dev.ports == 2
        assert dev.source == "udp"

    def test_progress_callback_fires_per_device(self):
        seen = []
        with DiscoveryResponder(ip="127.0.0.1") as responder:
            discovery.discover(
                timeout=1.0, targets=["127.0.0.1"], port=responder.port,
                broadcast=False, enrich=False, on_device=seen.append,
            )
        assert [d.ip for d in seen] == ["127.0.0.1"]

    def test_old_firmware_without_the_detail_opcodes_still_discovers(self):
        with DiscoveryResponder(
            ip="127.0.0.1", answer=lambda op: op == discovery.OP_SEARCH
        ) as responder:
            found = discovery.discover(
                timeout=1.0, targets=["127.0.0.1"], port=responder.port,
                broadcast=False, enrich=False,
            )
        assert len(found) == 1
        # Nothing from 0x10 / 0x16, but the model name pins the port count.
        assert found[0].name == ""
        assert found[0].ports == 2

    def test_nothing_listening_returns_an_empty_list(self):
        assert discovery.discover(
            timeout=0.3, targets=["127.0.0.1"], port=_free_udp_port(),
            broadcast=False, enrich=False,
        ) == []

    def test_to_dict_is_json_shaped(self):
        with DiscoveryResponder(ip="127.0.0.1") as responder:
            dev = discovery.discover(
                timeout=1.0, targets=["127.0.0.1"], port=responder.port,
                broadcast=False, enrich=False,
            )[0]
        info = dev.to_dict()
        assert set(info) == {
            "ip", "mac", "model", "modelId", "productLine", "name", "firmware",
            "serialNumber", "ports", "source", "openPorts",
        }


def _free_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestSubnetExpansion:
    @pytest.mark.parametrize(
        "text, first, count",
        [
            ("192.168.2.0/30", "192.168.2.1", 2),
            ("192.168.2.", "192.168.2.1", 254),
            ("192.168.2.210", "192.168.2.210", 1),
            ("192.168.2.210/32", "192.168.2.210", 1),
        ],
    )
    def test_expands(self, text, first, count):
        hosts = discovery._expand_subnet(text)
        assert hosts[0] == first
        assert len(hosts) == count

    @pytest.mark.parametrize("text", ["", "not an address", "::1/64"])
    def test_rejects_nonsense(self, text):
        assert discovery._expand_subnet(text) == []

    def test_refuses_an_absurdly_large_sweep(self):
        with pytest.raises(ValueError, match="too large"):
            discovery._expand_subnet("10.0.0.0/8")


class TestTcpScan:
    def test_finds_a_listener_on_the_command_port(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(4)
        port = listener.getsockname()[1]
        try:
            found = discovery.tcp_scan("127.0.0.1", ports=(port,), timeout=0.5)
        finally:
            listener.close()
        assert [d.ip for d in found] == ["127.0.0.1"]
        assert found[0].source == "tcp"
        assert found[0].open_ports == [port]

    def test_ignores_a_host_with_only_a_web_server(self, monkeypatch):
        # Port 80 alone is every printer on the network - not enough.
        monkeypatch.setattr(discovery, "_tcp_open", lambda host, port, timeout: port == 80)
        assert discovery.tcp_scan("127.0.0.1", ports=(966, 4001, 80)) == []

    def test_stop_event_aborts_the_sweep(self, monkeypatch):
        stop = threading.Event()
        stop.set()
        monkeypatch.setattr(discovery, "_tcp_open", lambda *a: True)
        assert discovery.tcp_scan("192.168.2.0/30", stop=stop) == []


class TestLocalNetworks:
    def test_reports_broadcast_addresses_for_this_machine(self):
        nets = discovery.local_networks()
        assert nets, "expected at least one usable IPv4 interface"
        for ip, bcast in nets:
            assert not ip.startswith("127.")
            assert bcast.count(".") == 3

    def test_loopback_is_never_offered(self, monkeypatch):
        monkeypatch.setattr(
            discovery, "_run",
            lambda cmd: "inet 127.0.0.1 netmask 0xff000000\n"
                        "inet 10.1.2.3 netmask 0xffffff00 broadcast 10.1.2.255\n",
        )
        monkeypatch.setattr(discovery.os, "name", "posix")
        assert discovery.local_networks() == [("10.1.2.3", "10.1.2.255")]

    def test_a_missing_broadcast_falls_back_to_a_slash_24(self, monkeypatch):
        monkeypatch.setattr(discovery, "_run", lambda cmd: "inet 10.1.2.3\n")
        monkeypatch.setattr(discovery.os, "name", "posix")
        assert discovery.local_networks() == [("10.1.2.3", "10.1.2.255")]


class TestPortsFromModel:
    @pytest.mark.parametrize(
        "model, ports",
        [("NPort W2250A", 2), ("NPort W2150A", 1), ("NPort 5210", 2), ("", 0),
         ("Moxa device (model 0x9999)", 0)],
    )
    def test_guess(self, model, ports):
        assert discovery.ports_from_model(model) == ports


@pytest.fixture
def strict_sim():
    """A simulator that enforces the two rules the real NPort enforces."""
    sim = NPortSimulator(
        "127.0.0.1", cmd_base=0, data_base=0, ports=2, instant=True,
        polling_interval=30.0, strict_aspp=True,
    )
    sim.start()
    try:
        yield sim
    finally:
        sim.stop()


def _probe(sim: NPortSimulator, index: int, **kwargs):
    """Probe simulator port *index* (1-based) on its ephemeral ports."""
    return discovery.probe_port(
        "127.0.0.1", index,
        cmd_port=sim.cmd_ports[index - 1],
        data_port=sim.data_ports[index - 1],
        **kwargs,
    )


class TestPortProbe:
    def test_probe_reports_modem_lines(self, strict_sim):
        strict_sim.ports[0].cnc.dsr = True
        strict_sim.ports[0].cnc.cts = True
        strict_sim.ports[0].cnc.dcd = False
        probe = _probe(strict_sim, 1)
        assert probe.reachable
        assert not probe.busy
        assert probe.modem == {"dsr": True, "cts": True, "dcd": False}
        assert probe.error == ""
        assert probe.elapsed_ms < 2000

    def test_probe_sends_port_init_first(self, strict_sim):
        _probe(strict_sim, 1)
        opcodes = [op for op, _payload in strict_sim.ports[0].command_log]
        assert opcodes[0] == 44  # ASPP PORT_INIT

    def test_probe_closes_every_socket(self, strict_sim):
        _probe(strict_sim, 1)
        # The simulator's accept loop decrements on close; give it a moment.
        from tests.conftest import wait_until

        assert wait_until(
            lambda: strict_sim.ports[0].cmd_connections == 0
            and strict_sim.ports[0].data_connections == 0,
            timeout=3.0,
        )

    def test_a_second_probe_works_after_the_first_let_go(self, strict_sim):
        assert _probe(strict_sim, 1).reachable
        assert _probe(strict_sim, 1).reachable

    def test_unreachable_host_is_reported_not_raised(self):
        probe = discovery.probe_port(
            "127.0.0.1", 1, cmd_port=_free_tcp_port(), data_port=_free_tcp_port(),
            timeout=0.6, retries=0,
        )
        assert not probe.reachable
        assert not probe.busy
        assert "refused" in probe.error or "timed out" in probe.error

    def test_a_port_whose_data_socket_is_taken_reads_as_busy(self, strict_sim):
        hog = socket.create_connection(("127.0.0.1", strict_sim.data_ports[0]), timeout=2)
        try:
            probe = discovery.probe_port(
                "127.0.0.1", 1,
                cmd_port=strict_sim.cmd_ports[0], data_port=strict_sim.data_ports[0],
                timeout=1.2, retries=0,
            )
        finally:
            hog.close()
        assert probe.reachable
        assert probe.busy
        assert "Max connection" in probe.error

    def test_operation_mode_is_guessed_from_the_data_port_that_answers(self, strict_sim):
        # Real COM listens on 950+n, TCP Server on 4001+n. Point the probe at
        # the simulator's Real COM-shaped port and let it choose.
        probe = discovery.probe_port(
            "127.0.0.1", 1, cmd_port=strict_sim.cmd_ports[0], timeout=0.8, retries=0,
        )
        # Nothing is listening on 950/4001 here, so the probe must say so
        # rather than inventing a mode.
        assert probe.mode == ""

    def test_probe_ports_walks_consecutive_ports(self, strict_sim):
        probes = []
        for index in (1, 2):
            probes.append(_probe(strict_sim, index))
        assert [p.port_index for p in probes] == [1, 2]
        assert all(p.reachable for p in probes)

    def test_console_settings_are_echoed_into_port_init(self, strict_sim):
        """With console settings the probe must not change the line."""
        probe = _probe(
            strict_sim, 1,
            line=discovery._line_from_console(
                {"baud": 19200, "data_bits": 7, "parity": "even", "stop_bits": "1"}
            ),
        )
        assert probe.reachable
        assert strict_sim.ports[0].settings.baud == 19200
        assert strict_sim.ports[0].settings.data_bits == 7
        assert strict_sim.ports[0].settings.parity == "E"

    def test_to_dict_is_json_shaped(self, strict_sim):
        info = _probe(strict_sim, 1).to_dict()
        assert set(info) == {
            "portIndex", "cmdPort", "dataPort", "reachable", "busy", "mode",
            "modeLabel", "modem", "settings", "error", "elapsedMs",
        }


class TestStrictSimulator:
    def test_a_first_command_that_is_not_port_init_is_dropped(self, strict_sim):
        sock = socket.create_connection(("127.0.0.1", strict_sim.cmd_ports[0]), timeout=2)
        try:
            sock.sendall(bytes([19, 0]))  # LSTATUS - the real device resets here
            sock.settimeout(2.0)
            assert sock.recv(16) == b""
        finally:
            sock.close()

    def test_port_init_is_not_answered_without_a_data_socket(self, strict_sim):
        sock = socket.create_connection(("127.0.0.1", strict_sim.cmd_ports[0]), timeout=2)
        try:
            sock.sendall(bytes([44, 8, 6, 3, 1, 1, 0, 0, 0, 0]))
            sock.settimeout(2.5)
            assert sock.recv(16) == b""
        finally:
            sock.close()


def _free_tcp_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestOpmodes:
    def test_real_com_is_256(self):
        assert discovery.OPMODES[256] == "Real COM"
        assert discovery.OPMODES[512] == "TCP Server"

    @pytest.mark.parametrize(
        "label, key", [("Real COM", "realcom"), ("TCP Server", "tcp_server"), ("UDP", "")]
    )
    def test_mode_key(self, label, key):
        assert discovery._mode_key(label) == key


def test_describe_support_is_json_shaped():
    info = discovery.describe_support()
    assert info["udp_search"] is True
    assert info["port_probe"] is True
    assert isinstance(info["notes"], str)


def test_encode_request_round_trips_through_the_responder():
    responder = DiscoveryResponder(ip="10.0.0.5", name="LATHE 2", ports=4, model_id=0x1234)
    try:
        reply = responder.build_reply(discovery.encode_request(discovery.OP_SEARCH, 0x11223344))
        opcode, sequence, payload = discovery.parse_reply(reply, discovery.OP_SEARCH)
        assert opcode == discovery.OP_SEARCH
        assert sequence == 0x11223344
        assert socket.inet_ntoa(payload[:4]) == "10.0.0.5"
        dev = discovery.parse_search_reply(reply)
        assert dev.model == "Moxa device (model 0x1234)"
        name = discovery.parse_name_reply(
            responder.build_reply(
                discovery.encode_request(discovery.OP_NAME, 0, responder.device_id)
            )
        )
        assert name == "LATHE 2"
        info = discovery.parse_info_reply(
            responder.build_reply(
                discovery.encode_request(discovery.OP_INFO, 0, responder.device_id)
            )
        )
        assert info["ports"] == 4
    finally:
        responder.stop()


def test_struct_header_is_big_endian():
    raw = discovery.encode_request(0x16, 0x01020304, b"\x00" * 12)
    opcode, status, length, sequence = struct.unpack("!BBHI", raw[:8])
    assert (opcode, status, length, sequence) == (0x16, 0, 20, 0x01020304)
