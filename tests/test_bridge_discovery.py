"""Bridge actions added by the discovery feature.

Kept in its own file so it does not collide with the existing bridge
tests. Everything runs against the fake UDP responder, the ASPP
simulator and the file-backed secret store, never a real device.
"""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from moxaserial import discovery  # noqa: E402
from moxaserial.bridge import Bridge, Host  # noqa: E402
from moxaserial.config import ConfigStore, default_machine  # noqa: E402
from moxaserial.secrets import FileBackend, SecretStore  # noqa: E402
from tests.conftest import wait_until  # noqa: E402
from tools.nport_sim import DiscoveryResponder, NPortSimulator  # noqa: E402


@pytest.fixture
def bridge(bus, tmp_path):
    store = ConfigStore(tmp_path / "settings.json")
    machine = default_machine("KIA Lathe")
    machine["id"] = "kia"
    machine["host"] = "127.0.0.1"
    store.upsert_machine(machine)
    secrets = SecretStore(FileBackend(tmp_path / "credentials.json"))
    bridge = Bridge(bus=bus, store=store, host=Host(), secrets=secrets)
    bridge.pushes = []
    bridge.subscribe_outbound(lambda action, payload: bridge.pushes.append((action, payload)))
    try:
        yield bridge
    finally:
        bridge.shutdown()


def ok(reply):
    assert reply["ok"] is True, reply.get("error")
    return reply["data"]


def pushes(bridge, action):
    return [p for a, p in bridge.pushes if a == action]


class TestDiscoverAction:
    def test_sync_discover_returns_the_devices_inline(self, bridge, monkeypatch):
        """The bridge always broadcasts on the real 4800, so the responder
        is injected at the discovery layer rather than by port number."""
        responder = DiscoveryResponder(ip="127.0.0.1")
        try:
            def fake_discover(**kw):
                return [discovery.parse_search_reply(
                    responder.build_reply(discovery.encode_request(discovery.OP_SEARCH))
                )]

            monkeypatch.setattr(discovery, "discover", fake_discover)
            data = ok(bridge.handle("machines.discover", {"sync": True, "timeout": 1.0}))
        finally:
            responder.stop()
        assert set(data) == {"scanId", "devices", "count"}
        assert data["count"] == 1
        assert data["devices"][0]["ip"] == "127.0.0.1"
        assert data["devices"][0]["model"] == "NPort W2250A"

    def test_async_discover_pushes_each_device_then_a_done_message(self, bridge, monkeypatch):
        fake = discovery.NPortDevice(
            ip="10.0.0.9", mac="00:90:e8:11:22:33", model="NPort 5210", ports=2
        )
        monkeypatch.setattr(discovery, "discover", lambda **kw: [fake])
        started = ok(bridge.handle("machines.discover", {}))
        assert started["started"] is True
        assert wait_until(lambda: any(
            p.get("done") for p in pushes(bridge, "machines.discoverResult")
        ), timeout=5.0)
        messages = pushes(bridge, "machines.discoverResult")
        device_messages = [m for m in messages if m.get("device")]
        assert [m["device"]["ip"] for m in device_messages] == ["10.0.0.9"]
        final = messages[-1]
        assert final["done"] is True
        assert final["count"] == 1
        assert final["devices"][0]["model"] == "NPort 5210"
        assert final["scanId"] == started["scanId"]

    def test_duplicate_devices_are_reported_once(self, bridge, monkeypatch):
        fake = discovery.NPortDevice(ip="10.0.0.9", mac="a", model="m")
        monkeypatch.setattr(discovery, "discover", lambda **kw: [fake, fake])
        ok(bridge.handle("machines.discover", {}))
        assert wait_until(lambda: any(
            p.get("done") for p in pushes(bridge, "machines.discoverResult")
        ), timeout=5.0)
        assert pushes(bridge, "machines.discoverResult")[-1]["count"] == 1

    def test_a_subnet_triggers_the_tcp_fallback(self, bridge, monkeypatch):
        calls = {}
        monkeypatch.setattr(discovery, "discover", lambda **kw: [])

        def fake_scan(subnet, **kw):
            calls["subnet"] = subnet
            kw["on_device"](discovery.NPortDevice(ip="10.0.0.5", source="tcp"))
            return []

        monkeypatch.setattr(discovery, "tcp_scan", fake_scan)
        ok(bridge.handle("machines.discover", {"sync": True, "subnet": "10.0.0.0/29"}))
        assert calls["subnet"] == "10.0.0.0/29"

    def test_a_failing_scan_pushes_an_error_rather_than_dying(self, bridge, monkeypatch):
        def boom(**kw):
            raise OSError("no network")

        monkeypatch.setattr(discovery, "discover", boom)
        ok(bridge.handle("machines.discover", {}))
        assert wait_until(lambda: any(
            p.get("done") for p in pushes(bridge, "machines.discoverResult")
        ), timeout=5.0)
        final = pushes(bridge, "machines.discoverResult")[-1]
        assert final["devices"] == []
        assert "no network" in final["error"]

    def test_stopping_with_nothing_running_is_a_no_op(self, bridge):
        data = ok(bridge.handle("machines.discoverStop", {}))
        assert data == {"stopped": False, "count": 0}

    def test_stop_only_aborts_the_scan_it_names(self, bridge, monkeypatch):
        """Two scans must not be able to cancel each other."""
        seen: list[threading.Event] = []
        release = threading.Event()

        def fake_scan(subnet, **kw):
            seen.append(kw["stop"])
            release.wait(5.0)
            return []

        monkeypatch.setattr(discovery, "discover", lambda **kw: [])
        monkeypatch.setattr(discovery, "tcp_scan", fake_scan)
        first = ok(bridge.handle("machines.discover", {"subnet": "10.0.0.0/30"}))
        second = ok(bridge.handle("machines.discover", {"subnet": "10.0.1.0/30"}))
        assert wait_until(lambda: len(seen) == 2, timeout=5.0)
        try:
            data = ok(bridge.handle("machines.discoverStop", {"scanId": first["scanId"]}))
            assert data["count"] == 1
            assert seen[0].is_set()
            assert not seen[1].is_set()
            assert second["scanId"] != first["scanId"]
        finally:
            release.set()

    def test_a_finished_scan_stops_being_cancellable(self, bridge, monkeypatch):
        monkeypatch.setattr(discovery, "discover", lambda **kw: [])
        ok(bridge.handle("machines.discover", {"sync": True}))
        assert ok(bridge.handle("machines.discoverStop", {}))["count"] == 0

    def test_timeout_is_clamped(self, bridge, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            discovery, "discover", lambda **kw: seen.setdefault("timeout", kw["timeout"]) and []
        )
        ok(bridge.handle("machines.discover", {"sync": True, "timeout": 900}))
        assert seen["timeout"] == 15.0


@pytest.fixture
def sim():
    sim = NPortSimulator(
        "127.0.0.1", cmd_base=0, data_base=0, ports=2, instant=True,
        polling_interval=30.0, strict_aspp=True,
    )
    sim.start()
    try:
        yield sim
    finally:
        sim.stop()


class TestProbePortsAction:
    def test_probe_ports_needs_a_host(self, bridge):
        # The simulator machine has no NPort address to probe.
        reply = bridge.handle("machines.probePorts", {"machineId": "simulator", "host": " "})
        assert reply["ok"] is False
        assert "IP address" in reply["error"]

    def test_probe_ports_sync_uses_the_machines_serial_settings(self, bridge, sim, monkeypatch):
        captured = {}

        def fake_probe_ports(host, **kw):
            captured.update(host=host, **kw)
            return [discovery.PortProbe(port_index=1, reachable=True)]

        monkeypatch.setattr(discovery, "probe_ports", fake_probe_ports)
        data = ok(bridge.handle("machines.probePorts", {"machineId": "kia", "sync": True}))
        assert data["host"] == "127.0.0.1"
        assert data["ports"][0]["portIndex"] == 1
        # CIMCO's shipped defaults for a new Moxa machine.
        assert captured["line"]["baud"] == 9600
        assert captured["line"]["parity"] == "even"

    def test_probe_ports_pushes_progress_then_done(self, bridge, monkeypatch):
        def fake_probe_ports(host, **kw):
            probes = [
                discovery.PortProbe(port_index=1, reachable=True),
                discovery.PortProbe(port_index=2, reachable=False),
            ]
            for probe in probes:
                kw["on_port"](probe)
            return probes

        monkeypatch.setattr(discovery, "probe_ports", fake_probe_ports)
        ok(bridge.handle("machines.probePorts", {"host": "10.0.0.9", "count": 2}))
        assert wait_until(lambda: any(
            p.get("done") for p in pushes(bridge, "machines.probePortsResult")
        ), timeout=5.0)
        messages = pushes(bridge, "machines.probePortsResult")
        assert [m["port"]["portIndex"] for m in messages if "port" in m] == [1, 2]
        assert messages[-1]["done"] is True
        assert len(messages[-1]["ports"]) == 2

    def test_probe_ports_against_the_simulator(self, bridge, sim, monkeypatch):
        """End to end through the bridge with a real ASPP handshake."""
        real = discovery.probe_port

        def by_index(host, port_index=1, **kw):
            kw.pop("cmd_port", None)
            kw.pop("data_port", None)
            return real(
                host, port_index,
                cmd_port=sim.cmd_ports[port_index - 1],
                data_port=sim.data_ports[port_index - 1],
                **kw,
            )

        monkeypatch.setattr(discovery, "probe_port", by_index)
        data = ok(bridge.handle(
            "machines.probePorts", {"host": "127.0.0.1", "count": 2, "sync": True}
        ))
        assert [p["portIndex"] for p in data["ports"]] == [1, 2]
        assert all(p["reachable"] for p in data["ports"])
        assert data["ports"][0]["modem"] == {"dsr": True, "cts": True, "dcd": False}

    def test_credentials_are_used_when_stored(self, bridge, monkeypatch):
        from moxaserial import nport_console

        bridge.secrets.set("kia", "admin", "pw")
        seen = {}

        def fake_read(host, username, password, port_count=2, timeout=6.0):
            seen.update(host=host, username=username, password=password, count=port_count)
            return ({1: {"baud": 4800}}, {1: "Real COM"})

        monkeypatch.setattr(nport_console, "read_port_details", fake_read)
        monkeypatch.setattr(discovery, "probe_ports", lambda host, **kw: [])
        data = ok(bridge.handle("machines.probePorts", {"machineId": "kia", "sync": True}))
        assert seen["username"] == "admin"
        assert data["consoleUsed"] is True

    def test_a_console_failure_does_not_stop_the_probe(self, bridge, monkeypatch):
        from moxaserial import nport_console

        bridge.secrets.set("kia", "admin", "pw")

        def boom(*a, **kw):
            raise nport_console.ConsoleError("device refused the login")

        monkeypatch.setattr(nport_console, "read_port_details", boom)
        monkeypatch.setattr(
            discovery, "probe_ports",
            lambda host, **kw: [discovery.PortProbe(port_index=1, reachable=True)],
        )
        data = ok(bridge.handle("machines.probePorts", {"machineId": "kia", "sync": True}))
        assert data["consoleUsed"] is False
        assert "refused the login" in data["consoleError"]
        assert data["ports"][0]["reachable"] is True


class TestProbeNeverDisturbsAPortInUse:
    """PORT_INIT *applies* line settings - it is not a read-only probe. So a
    probe must never touch a port this add-in is already using."""

    def test_probing_is_refused_while_a_send_is_running(self, bridge, monkeypatch):
        monkeypatch.setattr(type(bridge.sender), "is_running", property(lambda self: True))
        reply = bridge.handle("machines.probePorts", {"host": "10.0.0.9", "count": 2})
        assert reply["ok"] is False
        assert "send is in progress" in reply["error"]

    def test_probing_is_refused_while_a_receive_is_running(self, bridge, monkeypatch):
        monkeypatch.setattr(type(bridge.receiver), "is_running", property(lambda self: True))
        reply = bridge.handle("machines.probePorts", {"host": "10.0.0.9", "count": 2})
        assert reply["ok"] is False
        assert "receive is in progress" in reply["error"]

    def test_a_port_with_an_open_transport_is_reported_without_being_probed(
        self, bridge, monkeypatch
    ):
        # An open transport on port 2 of this host, as transport.open reports it.
        bridge.bus.publish("transport.open",
                           {"kind": "moxa", "host": "10.0.0.9", "portIndex": 2, "device": ""})
        probed: list[int] = []

        def fake_probe_port(host, port_index=1, **kw):
            probed.append(port_index)
            return discovery.PortProbe(port_index=port_index, reachable=True)

        monkeypatch.setattr(discovery, "probe_port", fake_probe_port)
        data = ok(bridge.handle(
            "machines.probePorts", {"host": "10.0.0.9", "count": 3, "sync": True}
        ))
        assert probed == [1, 3], "port 2 was probed despite being open"
        busy = [p for p in data["ports"] if p["portIndex"] == 2][0]
        assert busy["busy"] is True
        assert "this add-in" in busy["error"]

    def test_the_port_is_probed_again_once_the_transport_closes(self, bridge, monkeypatch):
        payload = {"kind": "moxa", "host": "10.0.0.9", "portIndex": 2, "device": ""}
        bridge.bus.publish("transport.open", payload)
        assert bridge.ports_in_use("10.0.0.9") == {2}
        bridge.bus.publish("transport.close", payload)
        assert bridge.ports_in_use("10.0.0.9") == set()

        probed: list[int] = []
        monkeypatch.setattr(
            discovery, "probe_port",
            lambda host, port_index=1, **kw: (
                probed.append(port_index),
                discovery.PortProbe(port_index=port_index),
            )[1],
        )
        ok(bridge.handle("machines.probePorts", {"host": "10.0.0.9", "count": 2, "sync": True}))
        assert probed == [1, 2]


class TestCredentialActions:
    def test_set_then_report_stored(self, bridge):
        data = ok(bridge.handle(
            "machines.setCredentials", {"id": "kia", "username": "admin", "password": "pw"}
        ))
        assert data["has_credentials"] is True
        assert data["username"] == "admin"
        assert "password" not in data

    def test_the_password_never_comes_back(self, bridge):
        bridge.handle("machines.setCredentials",
                      {"id": "kia", "username": "admin", "password": "hunter2"})
        blob = json.dumps([
            bridge.handle("machines.credentials", {"id": "kia"}),
            bridge.state(),
            [p for _a, p in bridge.pushes],
        ])
        assert "hunter2" not in blob

    def test_the_password_never_reaches_settings_json(self, bridge):
        bridge.handle("machines.setCredentials",
                      {"id": "kia", "username": "admin", "password": "hunter2"})
        assert "hunter2" not in Path(bridge.store.path).read_text(encoding="utf-8")

    def test_an_empty_username_is_refused(self, bridge):
        reply = bridge.handle("machines.setCredentials", {"id": "kia", "password": "pw"})
        assert reply["ok"] is False
        assert "account name" in reply["error"]

    def test_clear(self, bridge):
        bridge.handle("machines.setCredentials",
                      {"id": "kia", "username": "admin", "password": "pw"})
        data = ok(bridge.handle("machines.clearCredentials", {"id": "kia"}))
        assert data["removed"] is True
        assert data["has_credentials"] is False

    def test_clearing_nothing_is_not_an_error(self, bridge):
        data = ok(bridge.handle("machines.clearCredentials", {"id": "kia"}))
        assert data["removed"] is False

    def test_an_unknown_machine_is_rejected(self, bridge):
        reply = bridge.handle("machines.setCredentials",
                              {"id": "nope", "username": "a", "password": "b"})
        assert reply["ok"] is False

    def test_setting_credentials_pushes_the_boolean(self, bridge):
        bridge.handle("machines.setCredentials",
                      {"id": "kia", "username": "admin", "password": "pw"})
        pushed = pushes(bridge, "machines.credentials")
        assert pushed[-1]["has_credentials"] is True
        assert pushed[-1]["machineId"] == "kia"

    def test_state_reports_booleans_only(self, bridge):
        bridge.handle("machines.setCredentials",
                      {"id": "kia", "username": "admin", "password": "pw"})
        state = bridge.state()
        assert state["credentials"]["kia"] is True
        assert state["credentials"]["simulator"] is False
        assert state["credentialStore"]["backend"] == "file"
        assert state["credentialStore"]["secure"] is False

    def test_about_advertises_discovery(self, bridge):
        about = ok(bridge.handle("about.get", {}))
        assert about["discovery"]["udp_search"] is True
        assert about["credentialStore"]["backend"] == "file"
