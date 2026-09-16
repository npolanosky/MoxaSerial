"""The action router that sits between the HTML UI and the engines.

This module is deliberately Fusion-free. Both hosts use it unchanged:

* ``moxaserial/ui/palette.py`` feeds it ``Palette.incomingFromHTML`` events and
  pushes outbound messages through ``Palette.sendInfoToHTML``;
* ``tools/dev_server.py`` feeds it HTTP POSTs and pushes outbound
  messages through Server-Sent Events.

Message schema
--------------
Inbound  (UI -> Python)::  action: str, payload: dict  -> reply dict
Outbound (Python -> UI)::  {"action": str, "payload": dict}

Every inbound call returns a reply dict of the shape::

    {"ok": true,  "action": "...", "data": {...}}
    {"ok": false, "action": "...", "error": "human readable"}

Asynchronous engine progress is *pushed* rather than returned; see
:meth:`Bridge.subscribe_outbound`.

The full action list is documented in ARCHITECTURE.md.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

from moxaserial import __version__, paths
from moxaserial.config import (
    COMMON_BAUDS,
    DATA_BITS,
    DIALECTS,
    END_TRIGGER_MODES,
    FLOW_CONTROLS,
    LINE_ENDINGS,
    MACHINE_SELECTION,
    OVERWRITE_POLICIES,
    PARITIES,
    RECEIVE_LINE_ENDINGS,
    RECEIVE_REMOVE_CHARS,
    SAVE_LINE_ENDINGS,
    START_TRIGGER_MODES,
    STOP_BITS,
    THEMES,
    WAIT_MODES,
    ConfigStore,
    ValidationError,
    default_machine,
    validate_machine,
)
from moxaserial.dnc.preprocess import PreprocessOptions, preprocess
from moxaserial.dnc.receiver import Receiver, ReceiveState
from moxaserial.dnc.sender import Sender, SendState
from moxaserial.events import Event, EventBus
from moxaserial.log import LogManager, get_logger
from moxaserial.secrets import SecretStore
from moxaserial.update import UpdateService

log = get_logger("bridge")

#: Engine topics mirrored straight through to the UI, with the topic as the
#: outbound action name.
FORWARDED_TOPICS = (
    "send.state",
    "send.progress",
    "send.done",
    "send.error",
    "send.log",
    "receive.state",
    "receive.progress",
    "receive.done",
    "receive.error",
    "receive.overwrite_request",
    "log.entry",
    "transport.modem",
    "transport.line_error",
)

PREVIEW_LINES = 200


class Host:
    """Capabilities only the embedding application can provide.

    The default implementation is the "no Fusion here" one used by the
    dev server and the tests; :class:`moxaserial.ui.app.FusionHost` overrides it.
    """

    name = "standalone"

    def pick_file(
        self, title: str = "Select an NC file", initial_dir: str = "", extensions: str = ""
    ) -> str:
        """Open a native file dialog. Returns "" when unavailable.

        *initial_dir* is the machine's default send folder (CIMCO's
        "Default directory"); *extensions* a comma/semicolon separated list
        of extra extensions (CIMCO's "Additional extensions").
        """
        return ""

    def pick_folder(self, title: str = "Select a folder") -> str:
        return ""

    def last_posted_file(self) -> dict[str, Any]:
        """Most recently posted NC program. See moxaserial/ui/lastpost.py."""
        return {"path": "", "name": "", "source": "unavailable", "candidates": []}

    def toast(self, message: str, level: str = "info", title: str = "MoxaSerial") -> None:
        """Non-blocking notification inside the host application."""
        log.info("[toast:%s] %s", level, message)

    def reveal(self, path: str) -> bool:
        """Show *path* in Finder / Explorer. False when unsupported."""
        return False

    # --- auto-update ---------------------------------
    def addin_dir(self) -> str:
        """Folder the add-in is installed in, "" when not applicable."""
        return ""

    def restart_addin(self) -> bool:
        """Stop and start the add-in so new files are loaded.

        False means the host cannot do it and the operator must restart the
        application; :class:`moxaserial.update.UpdateService` says so in a toast.
        """
        return False
    # -------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {"host": self.name}


class Bridge:
    """Owns the config store, the engines, and the inbound action table."""

    def __init__(
        self,
        bus: EventBus | None = None,
        store: ConfigStore | None = None,
        host: Host | None = None,
        transport_factory: Callable[..., Any] | None = None,
        secrets: SecretStore | None = None,
    ) -> None:
        self.bus = bus or EventBus()
        self.logs = LogManager.instance(self.bus)
        self.store = store or ConfigStore()
        # Credentials live in the OS keychain, never in the config store.
        self.secrets = secrets or SecretStore()
        # One stop flag per scan, not one shared flag: two scans must not be
        # able to cancel each other, and starting a second must not silently
        # un-cancel the first.
        self._discover_lock = threading.Lock()
        self._discover_stops: dict[str, threading.Event] = {}
        self._discover_seq = 0
        # Endpoints this add-in currently holds open, as a multiset keyed by
        # (host, port_index, device) and maintained from transport.open /
        # transport.close. Probing sends PORT_INIT, which *applies* line
        # settings, so a port in here must never be probed.
        self._open_endpoints: Counter[tuple[str, int, str]] = Counter()
        self._endpoint_lock = threading.Lock()
        self.host = host or Host()
        self.sender = Sender(self.bus, transport_factory=transport_factory)
        self.receiver = Receiver(self.bus, transport_factory=transport_factory)
        self._outbound: list[Callable[[str, dict[str, Any]], None]] = []
        self._last_file: dict[str, Any] = {}
        self.updates = UpdateService(store=self.store, push=self.push, host=self.host)

        self.logs.set_level(str(self.store.get("log_level", "INFO")))
        self.bus.subscribe("*", self._on_event)
        self.bus.set_error_hook(
            lambda topic, exc: log.error("Event subscriber failed on %s: %s", topic, exc)
        )

        self._actions: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "ui.ready": self._a_state,
            "state.get": self._a_state,
            "machines.list": self._a_machines_list,
            "machines.save": self._a_machines_save,
            "machines.delete": self._a_machines_delete,
            "machines.duplicate": self._a_machines_duplicate,
            "machines.setDefault": self._a_machines_set_default,
            "machines.new": self._a_machines_new,
            "machines.test": self._a_machines_test,
            "machines.discover": self._a_machines_discover,
            "machines.discoverStop": self._a_machines_discover_stop,
            "machines.probePorts": self._a_machines_probe_ports,
            "machines.setCredentials": self._a_machines_set_credentials,
            "machines.clearCredentials": self._a_machines_clear_credentials,
            "machines.credentials": self._a_machines_credentials,
            "serial.listPorts": self._a_serial_list_ports,
            "settings.get": self._a_settings_get,
            "settings.save": self._a_settings_save,
            "file.browse": self._a_file_browse,
            "file.lastPost": self._a_file_last_post,
            "file.preview": self._a_file_preview,
            "file.reveal": self._a_file_reveal,
            "send.start": self._a_send_start,
            "send.pause": self._a_send_pause,
            "send.resume": self._a_send_resume,
            "send.stop": self._a_send_stop,
            "send.resend": self._a_send_resend,
            "receive.start": self._a_receive_start,
            "receive.stop": self._a_receive_stop,
            "receive.overwriteResponse": self._a_receive_overwrite,
            "log.list": self._a_log_list,
            "log.clear": self._a_log_clear,
            "log.openFile": self._a_log_open_file,
            "theme.set": self._a_theme_set,
            "about.get": self._a_about,
            "update.check": self._a_update_check,
            "update.install": self._a_update_install,
            "update.status": self._a_update_status,
        }

    # ------------------------------------------------------------------
    # Outbound plumbing
    # ------------------------------------------------------------------
    def subscribe_outbound(
        self, sink: Callable[[str, dict[str, Any]], None]
    ) -> Callable[[], None]:
        """Register a ``sink(action, payload)`` for pushed messages."""
        self._outbound.append(sink)

        def _unsub() -> None:
            if sink in self._outbound:
                self._outbound.remove(sink)

        return _unsub

    def push(self, action: str, payload: dict[str, Any] | None = None) -> None:
        """Send a message to the UI. Never raises."""
        data = payload or {}
        for sink in list(self._outbound):
            try:
                sink(action, data)
            except Exception:  # noqa: BLE001
                log.debug("Outbound sink failed for %s", action, exc_info=True)

    def _on_event(self, evt: Event) -> None:
        if evt.topic in FORWARDED_TOPICS:
            self.push(evt.topic, evt.payload)
        if evt.topic in ("transport.open", "transport.close"):
            # Runs on whichever thread opened or closed the transport, so it
            # must stay to bookkeeping: no transport calls, no Fusion calls.
            self._track_endpoint(evt.topic, evt.payload or {})
        if evt.topic == "send.done":
            self.host.toast(
                f"Sent {evt.payload.get('file_name', 'program')} to "
                f"{evt.payload.get('machine_name', 'the machine')}.",
                level="success",
            )
            self.push("toast", {"level": "success", "message": "Transfer complete"})
        elif evt.topic == "send.error":
            self.host.toast(f"Send failed: {evt.payload.get('message', '')}", level="error")
            self.push(
                "toast", {"level": "error", "message": evt.payload.get("message", "Send failed")}
            )
        elif evt.topic == "receive.done":
            self.host.toast(f"Received {evt.payload.get('target_name', 'program')}.", "success")
            self.push(
                "toast",
                {"level": "success", "message": f"Saved {evt.payload.get('target_name', '')}"},
            )
        elif evt.topic == "receive.error":
            self.host.toast(f"Receive failed: {evt.payload.get('message', '')}", level="error")
            self.push(
                "toast",
                {"level": "error", "message": evt.payload.get("message", "Receive failed")},
            )

    # ------------------------------------------------------------------
    # Inbound dispatch
    # ------------------------------------------------------------------
    def handle(self, action: str, data: Any = None) -> dict[str, Any]:
        """Route one inbound message. *data* may be a dict or a JSON string."""
        if action == "response":
            # Fusion's palette browser echoes the JS push handler's return
            # value back as an incoming "response" action. Not a request -
            # and logging it would push a log entry, whose echo would log
            # again, forever.
            return {"ok": True, "action": action}
        payload = _coerce_payload(data)
        fn = self._actions.get(action)
        if fn is None:
            log.warning("Unknown action from the UI: %s", action)
            return {"ok": False, "action": action, "error": f"Unknown action '{action}'"}
        try:
            result = fn(payload)
            return {"ok": True, "action": action, "data": result or {}}
        except ValidationError as exc:
            log.warning("%s rejected: %s", action, exc)
            return {"ok": False, "action": action, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001
            log.exception("Action %s failed", action)
            return {"ok": False, "action": action, "error": str(exc)}

    def handle_json(self, action: str, data: Any = None) -> str:
        """``handle`` with a JSON-string result (what the palette needs)."""
        return json.dumps(self.handle(action, data))

    # ------------------------------------------------------------------
    # Actions
    # ------------------------------------------------------------------
    def state(self) -> dict[str, Any]:
        """The whole world, as the UI wants it on load."""
        settings = self.store.data
        return {
            "version": __version__,
            "host": self.host.describe(),
            "settings": settings,
            "machines": settings["machines"],
            "activeMachineId": self.store.active_machine()["id"],
            "enums": ENUMS,
            "send": self.sender.snapshot(),
            "sendState": self.sender.state.value,
            "canResend": self.sender.can_resend,
            "receive": self.receiver.snapshot(),
            "receiveState": self.receiver.state.value,
            "file": self._last_file,
            "log": {
                "entries": self.logs.ring.records(limit=400),
                "counts": self.logs.ring.counts(),
                "path": self.logs.log_file,
                "level": self.logs.level,
            },
            "paths": {
                "settings": str(self.store.path),
                "appData": str(paths.app_data_dir()),
                "log": self.logs.log_file,
            },
            # --- discovery: booleans only, never the credentials ---
            "credentials": self.credentials_map(),
            "credentialStore": self.secrets.describe(),
            # --- end discovery ---
        }

    def _a_state(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.state()

    # -- machines --------------------------------------------------------
    def _a_machines_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"machines": self.store.machines()}

    def _a_machines_new(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"machine": default_machine(str(payload.get("name") or "New Machine"))}

    def _a_machines_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = payload.get("machine") or payload
        saved = self.store.upsert_machine(machine)
        warnings = validate_machine(saved)
        log.info("Saved machine '%s'.", saved["name"])
        self.push("machines", {"machines": self.store.machines()})
        return {"machine": saved, "machines": self.store.machines(), "warnings": warnings}

    def _a_machines_delete(self, payload: dict[str, Any]) -> dict[str, Any]:
        mid = str(payload.get("id", ""))
        removed = self.store.delete_machine(mid)
        log.info("Deleted machine %s (%s).", mid, "ok" if removed else "not found")
        self.push("machines", {"machines": self.store.machines()})
        return {"removed": removed, "machines": self.store.machines()}

    def _a_machines_duplicate(self, payload: dict[str, Any]) -> dict[str, Any]:
        clone = self.store.duplicate_machine(str(payload.get("id", "")))
        if clone is None:
            raise ValidationError("No such machine to duplicate.")
        self.push("machines", {"machines": self.store.machines()})
        return {"machine": clone, "machines": self.store.machines()}

    def _a_machines_set_default(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.store.set_default_machine(str(payload.get("id", "")))
        return {"settings": self.store.data}

    def _a_machines_test(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Connectivity check. Runs on a worker thread (connect timeouts
        would otherwise freeze Fusion's UI thread); the result arrives as a
        ``machines.testResult`` push. ``sync: true`` keeps the old blocking
        behaviour for tests and the dev server."""
        machine = self._machine_or_die(payload.get("id"))
        if payload.get("sync"):
            return self._run_machine_test(machine)

        def worker() -> None:
            result = self._run_machine_test(machine)
            self.push("machines.testResult", {"machineId": machine["id"], **result})

        threading.Thread(target=worker, name="moxa-machine-test", daemon=True).start()
        return {"started": True, "machineId": machine["id"]}

    # --- direct serial ports -------------------------------
    def _a_serial_list_ports(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Serial ports visible on this computer, for the machine form's
        port dropdown. Enumeration never raises, so a machine can always
        be configured by typing the device name."""
        from moxaserial.transport.serial_port import list_serial_ports

        return {"ports": list_serial_ports()}

    # --- end direct serial ports -------------------------------------------

    def _run_machine_test(self, machine: dict[str, Any]) -> dict[str, Any]:
        from moxaserial.transport import TransportError, create_transport

        transport = create_transport(machine, self.bus)
        started = time.time()
        try:
            transport.open(machine)
            info = transport.describe()
            caps = getattr(transport, "capabilities", None)
            if callable(caps):
                info["capabilities"] = caps()
            info["elapsed_ms"] = int((time.time() - started) * 1000)
            log.info("Connection test to '%s' succeeded.", machine["name"])
            return {"ok": True, "info": info}
        except TransportError as exc:
            log.warning("Connection test to '%s' failed: %s", machine["name"], exc)
            return {"ok": False, "error": str(exc)}
        finally:
            transport.close()

    # ------------------------------------------------------------------
    # --- discovery: network discovery, port probing, credentials --
    # ------------------------------------------------------------------
    def _a_machines_discover(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Find NPorts on the network.

        Asynchronous by default: every device is pushed as it is found
        (``machines.discoverResult`` with ``device``), and a final message
        with ``done: true`` carries the whole list. ``sync: true`` blocks
        and returns the list instead, which is what the tests use.
        """
        from moxaserial import discovery

        timeout = max(0.2, min(float(payload.get("timeout", 2.0) or 2.0), 15.0))
        targets = [str(t).strip() for t in (payload.get("targets") or []) if str(t).strip()]
        subnet = str(payload.get("subnet", "") or "").strip()
        udp = bool(payload.get("udp", True))
        stop = threading.Event()
        with self._discover_lock:
            self._discover_seq += 1
            scan_id = f"scan{self._discover_seq}"
            self._discover_stops[scan_id] = stop

        def run(emit: Callable[[dict[str, Any]], None]) -> list[dict[str, Any]]:
            seen: dict[str, dict[str, Any]] = {}

            def add(dev: Any) -> None:
                info = dev.to_dict()
                if info["ip"] in seen:
                    return
                seen[info["ip"]] = info
                emit({"scanId": scan_id, "phase": dev.source, "device": info})

            if udp:
                emit({"scanId": scan_id, "phase": "udp", "message": "Broadcasting on UDP 4800…"})
                for dev in discovery.discover(timeout=timeout, targets=targets):
                    add(dev)
            if subnet and not stop.is_set():
                emit({
                    "scanId": scan_id,
                    "phase": "tcp",
                    "message": f"Scanning {subnet} for open NPort ports…",
                })
                discovery.tcp_scan(subnet, on_device=add, stop=stop)
            return list(seen.values())

        def forget() -> None:
            with self._discover_lock:
                self._discover_stops.pop(scan_id, None)

        if payload.get("sync"):
            try:
                devices = run(lambda _msg: None)
            finally:
                forget()
            return {"scanId": scan_id, "devices": devices, "count": len(devices)}

        def worker() -> None:
            try:
                devices = run(lambda msg: self.push("machines.discoverResult", msg))
                self.push(
                    "machines.discoverResult",
                    {"scanId": scan_id, "phase": "done", "done": True,
                     "devices": devices, "count": len(devices)},
                )
            except Exception as exc:  # noqa: BLE001 - a scan must never kill the thread silently
                log.exception("Discovery failed")
                self.push(
                    "machines.discoverResult",
                    {"scanId": scan_id, "phase": "done", "done": True,
                     "devices": [], "count": 0, "error": str(exc)},
                )
            finally:
                forget()

        threading.Thread(target=worker, name="moxa-discover", daemon=True).start()
        return {"started": True, "scanId": scan_id}

    def _a_machines_discover_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Abort one scan by ``scanId``, or every running scan."""
        wanted = str(payload.get("scanId", "") or "")
        with self._discover_lock:
            events = (
                [self._discover_stops[wanted]]
                if wanted and wanted in self._discover_stops
                else list(self._discover_stops.values())
            )
        for event in events:
            event.set()
        return {"stopped": bool(events), "count": len(events)}

    def _a_machines_probe_ports(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Probe a device's serial ports. See ``machines.discover`` for the
        sync/async split; results push as ``machines.probePortsResult``."""
        from moxaserial import discovery

        machine: dict[str, Any] = {}
        if payload.get("machineId"):
            machine = self._machine_or_die(payload.get("machineId"))
        host = str(payload.get("host", "") or machine.get("host", "")).strip()
        if not host:
            raise ValidationError("Enter the NPort's IP address first.")
        busy = self._transfer_in_progress()
        if busy:
            raise ValidationError(busy)
        # Belt and braces: a transport can be open without its engine thread
        # being alive (machines.test, or the gap either side of a job), so
        # skip those ports individually as well.
        skip = self.ports_in_use(host)
        count = max(1, min(int(payload.get("count", 0) or machine.get("ports", 0) or 2), 32))
        line = dict(machine.get("serial", {})) if machine else {}
        machine_id = str(machine.get("id", "") or payload.get("machineId", "") or "")

        def run(emit: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
            settings: dict[int, dict[str, Any]] = {}
            opmodes: dict[int, str] = {}
            console_error = ""
            creds = self.secrets.get(machine_id) if machine_id else None
            if creds and payload.get("useCredentials", True):
                from moxaserial import nport_console

                try:
                    settings, opmodes = nport_console.read_port_details(
                        host, creds["username"], creds["password"], port_count=count
                    )
                except Exception as exc:  # noqa: BLE001 - the probe works without it
                    console_error = str(exc)
                    log.warning("Web console read failed for %s: %s", host, exc)
            probes = discovery.probe_ports(
                host, count=count, line=line,
                console_settings=settings, opmodes=opmodes,
                skip=skip,
                on_port=lambda p: emit({"host": host, "port": p.to_dict()}),
            )
            return {
                "host": host,
                "machineId": machine_id,
                "ports": [p.to_dict() for p in probes],
                "consoleUsed": bool(settings or opmodes),
                "consoleError": console_error,
            }

        if payload.get("sync"):
            return run(lambda _msg: None)

        def worker() -> None:
            try:
                result = run(lambda msg: self.push("machines.probePortsResult", msg))
                self.push("machines.probePortsResult", {**result, "done": True})
            except Exception as exc:  # noqa: BLE001
                log.exception("Port probe failed")
                self.push(
                    "machines.probePortsResult",
                    {"host": host, "machineId": machine_id, "ports": [],
                     "done": True, "error": str(exc)},
                )

        threading.Thread(target=worker, name="moxa-probe-ports", daemon=True).start()
        return {"started": True, "host": host, "count": count}

    def _a_machines_set_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store the web-console login for one machine in the OS keychain.

        The password arrives here and goes straight to the secret store;
        it is never echoed back, logged, or written to settings.json.
        """
        machine = self._machine_or_die(payload.get("id") or payload.get("machineId"))
        username = str(payload.get("username", "") or "").strip()
        password = str(payload.get("password", "") or "")
        if not username:
            raise ValidationError("Enter the web-console account name.")
        self.secrets.set(machine["id"], username, password)
        self.push("machines.credentials", self._credentials_payload(machine["id"]))
        return self._credentials_payload(machine["id"])

    def _a_machines_clear_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = self._machine_or_die(payload.get("id") or payload.get("machineId"))
        removed = self.secrets.clear(machine["id"])
        result = {**self._credentials_payload(machine["id"]), "removed": removed}
        self.push("machines.credentials", result)
        return result

    def _a_machines_credentials(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = self._machine_or_die(payload.get("id") or payload.get("machineId"))
        return self._credentials_payload(machine["id"])

    def _credentials_payload(self, machine_id: str) -> dict[str, Any]:
        """Never contains a password - only whether one is stored."""
        creds = self.secrets.get(machine_id)
        return {
            "machineId": machine_id,
            "has_credentials": creds is not None,
            "username": creds["username"] if creds else "",
            "store": self.secrets.describe(),
        }

    def credentials_map(self) -> dict[str, bool]:
        """``{machine_id: has_credentials}`` for the whole settings file."""
        out: dict[str, bool] = {}
        for machine in self.store.machines():
            try:
                out[machine["id"]] = self.secrets.has(machine["id"])
            except Exception:  # noqa: BLE001 - a broken keychain must not break state()
                out[machine["id"]] = False
        return out

    # --- end discovery ---------------------------------------------

    # -- settings --------------------------------------------------------
    def _a_settings_get(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {"settings": self.store.data, "enums": ENUMS}

    def _a_settings_save(self, payload: dict[str, Any]) -> dict[str, Any]:
        values = payload.get("settings") or payload
        data = self.store.update_globals(values)
        self.logs.set_level(str(data.get("log_level", "INFO")))
        return {"settings": data}

    def _a_theme_set(self, payload: dict[str, Any]) -> dict[str, Any]:
        theme = str(payload.get("theme", "dark"))
        data = self.store.update_globals({"theme": theme})
        return {"settings": data}

    # -- files -----------------------------------------------------------
    def _a_file_browse(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine_id = payload.get("machineId")
        try:
            machine = self._machine_or_die(machine_id) if machine_id else self.store.active_machine()
        except Exception:  # noqa: BLE001 - browsing must still work
            machine = {}
        send_cfg = machine.get("send", {}) if machine else {}
        path = self.host.pick_file(
            initial_dir=str(send_cfg.get("default_folder", "") or ""),
            extensions=str(send_cfg.get("additional_extensions", "") or ""),
        )
        if not path:
            return {"path": "", "cancelled": True}
        return {"file": self._describe_file(path, source="browse")}

    def _a_file_last_post(self, payload: dict[str, Any]) -> dict[str, Any]:
        info = self.host.last_posted_file()
        path = info.get("path", "")
        if path and os.path.isfile(path):
            described = self._describe_file(path, source=info.get("source", "lastpost"))
            described["candidates"] = info.get("candidates", [])
            return {"file": described}
        return {"file": {}, "error": info.get("error", "No posted NC file found."),
                "candidates": info.get("candidates", [])}

    def _a_file_preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path", "") or self._last_file.get("path", ""))
        if not path or not os.path.isfile(path):
            raise ValidationError("Choose a file first.")
        machine = self._machine_or_die(payload.get("machineId"))
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        result = preprocess(text, PreprocessOptions.from_machine(machine))
        return {
            "path": path,
            "name": os.path.basename(path),
            "lines": result.lines[:PREVIEW_LINES],
            "lineCount": result.line_count,
            "byteCount": result.byte_count,
            "droppedLines": result.dropped_lines,
            "sourceLines": result.source_lines,
            "truncated": result.line_count > PREVIEW_LINES,
        }

    def _a_file_reveal(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = str(payload.get("path", ""))
        return {"revealed": self.host.reveal(path)}

    def _describe_file(self, path: str, source: str = "") -> dict[str, Any]:
        st = os.stat(path)
        info = {
            "path": path,
            "name": os.path.basename(path),
            "dir": os.path.dirname(path),
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtimeText": time.strftime("%Y-%m-%d %H:%M", time.localtime(st.st_mtime)),
            "source": source,
        }
        self._last_file = info
        self.push("file.info", {"file": info})
        return info

    # -- send ------------------------------------------------------------
    def _a_send_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = self._machine_or_die(payload.get("machineId"))
        path = str(payload.get("path", "") or self._last_file.get("path", ""))
        if payload.get("useLastPost"):
            info = self.host.last_posted_file()
            path = info.get("path", "")
        if not path:
            raise ValidationError("No file selected. Choose 'Last posted' or browse for one.")
        if not os.path.isfile(path):
            raise ValidationError(f"File not found: {path}")
        if self.sender.is_running:
            raise ValidationError("A send is already in progress.")
        if self.store.get("confirm_before_send", False) and not payload.get("confirmed"):
            return {
                "started": False,
                "needsConfirm": True,
                "machine": machine["name"],
                "path": path,
                "fileName": os.path.basename(path),
            }

        self.store.note_machine_used(machine["id"])
        self._describe_file(path, source=str(payload.get("source", "send")))
        log.info("Sending %s to '%s'.", os.path.basename(path), machine["name"])
        started = self.sender.start(machine, file_path=path)
        return {"started": started, "machine": machine["name"], "path": path}

    def _a_send_pause(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sender.pause()
        return {"state": self.sender.state.value}

    def _a_send_resume(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sender.resume()
        return {"state": self.sender.state.value}

    def _a_send_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.sender.stop()
        return {"state": self.sender.state.value}

    def _a_send_resend(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.sender.can_resend:
            raise ValidationError("Nothing has been sent yet.")
        return {"started": self.sender.resend()}

    # -- receive ---------------------------------------------------------
    def _a_receive_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        machine = self._machine_or_die(payload.get("machineId"))
        if self.receiver.is_running:
            raise ValidationError("A receive is already in progress.")
        self.store.note_machine_used(machine["id"])
        log.info("Arming receive from '%s'.", machine["name"])
        started = self.receiver.start(
            machine, filename_override=str(payload.get("filename", ""))
        )
        return {"started": started, "machine": machine["name"]}

    def _a_receive_stop(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.receiver.stop()
        return {"state": self.receiver.state.value}

    def _a_receive_overwrite(self, payload: dict[str, Any]) -> dict[str, Any]:
        token = str(payload.get("token", ""))
        decision = str(payload.get("decision", "cancel"))
        return {"accepted": self.receiver.resolve_overwrite(token, decision)}

    # -- log -------------------------------------------------------------
    def _a_log_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        entries = self.logs.ring.records(
            min_level=str(payload.get("level", "DEBUG")),
            text=str(payload.get("text", "")),
            limit=int(payload.get("limit", 500) or 500),
        )
        return {
            "entries": entries,
            "counts": self.logs.ring.counts(),
            "path": self.logs.log_file,
        }

    def _a_log_clear(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.logs.clear()
        log.info("Log view cleared by the operator (the log file is untouched).")
        return {"cleared": True}

    def _a_log_open_file(self, payload: dict[str, Any]) -> dict[str, Any]:
        path = self.logs.log_file
        return {"path": path, "revealed": self.host.reveal(path) if path else False}

    # -- about -----------------------------------------------------------
    def _a_about(self, payload: dict[str, Any]) -> dict[str, Any]:
        from moxaserial.transport import aspp

        return {
            "version": __version__,
            "host": self.host.describe(),
            "paths": {
                "settings": str(self.store.path),
                "appData": str(paths.app_data_dir()),
                "log": self.logs.log_file,
            },
            "protocol": aspp.describe_support(),
            "update": self.updates.status(),
            "discovery": _discovery_support(),
            "credentialStore": self.secrets.describe(),
        }

    # --- auto-update -----------------------------------
    def _a_update_status(self, payload: dict[str, Any]) -> dict[str, Any]:
        return self.updates.status()

    def _a_update_check(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Ask GitHub. Async by default - a network stall must not freeze
        Fusion's UI thread - with the result arriving as an ``update.checked``
        push. ``sync: true`` returns it inline (tests, dev server)."""
        if payload.get("sync"):
            return self.updates.check(force=True)
        self.updates.check_async(force=True)
        return {"started": True}

    def _a_update_install(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Download + verify + swap + restart, on a worker thread.

        Progress arrives as ``update.progress`` {stage, percent, message},
        the outcome as ``update.done`` or ``update.error``.
        """
        return self.updates.install_async(payload.get("release") or None)
    # ---------------------------------------------------------------------

    # -- helpers ---------------------------------------------------------
    @staticmethod
    def _endpoint_key(payload: dict[str, Any]) -> tuple[str, int, str]:
        return (
            str(payload.get("host", "") or "").strip().lower(),
            int(payload.get("portIndex", 1) or 1),
            str(payload.get("device", "") or "").strip(),
        )

    def _track_endpoint(self, topic: str, payload: dict[str, Any]) -> None:
        key = self._endpoint_key(payload)
        with self._endpoint_lock:
            if topic == "transport.open":
                self._open_endpoints[key] += 1
            else:
                if self._open_endpoints.get(key, 0) > 0:
                    self._open_endpoints[key] -= 1
                if self._open_endpoints.get(key, 0) <= 0:
                    self._open_endpoints.pop(key, None)

    def ports_in_use(self, host: str) -> set[int]:
        """Port indices on *host* that this add-in currently holds open."""
        want = str(host or "").strip().lower()
        with self._endpoint_lock:
            return {idx for (h, idx, _dev), n in self._open_endpoints.items() if h == want and n > 0}

    def _transfer_in_progress(self) -> str:
        """Why probing must wait, or "" when it may go ahead.

        Probing sends ``PORT_INIT``, which applies line settings to the
        port. Doing that while a job is running would change the baud rate
        under it - and on an NPort with ``Max connection = 1`` the extra
        connection alone can drop the live one.
        """
        if self.sender.is_running:
            return "A send is in progress. Probing changes a port's line settings, so it has to wait."
        if self.receiver.is_running:
            return (
                "A receive is in progress. Probing changes a port's line settings, "
                "so it has to wait."
            )
        return ""

    def _machine_or_die(self, machine_id: Any) -> dict[str, Any]:
        if machine_id:
            machine = self.store.machine(str(machine_id))
            if machine is None:
                raise ValidationError(f"No machine with id '{machine_id}'.")
            return machine
        return self.store.active_machine()

    def shutdown(self) -> None:
        """Stop the engines and drop subscriptions. Safe to call twice."""
        try:
            self.sender.stop(wait=True, timeout=2.0)
        except Exception:
            pass
        try:
            self.receiver.stop(wait=True, timeout=2.0)
        except Exception:
            pass
        try:
            self.updates.cancel()
        except Exception:
            pass
        self._outbound.clear()


# --- discovery ---------------------------------------------------------
def _discovery_support() -> dict[str, Any]:
    """Imported lazily so the About page never pays for a socket module."""
    from moxaserial import discovery

    return discovery.describe_support()


# --- end discovery -----------------------------------------------------


def _coerce_payload(data: Any) -> dict[str, Any]:
    if data is None or data == "":
        return {}
    if isinstance(data, dict):
        return data
    if isinstance(data, str):
        try:
            parsed = json.loads(data)
        except json.JSONDecodeError:
            return {"value": data}
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    return {"value": data}


#: Everything the settings form needs to build its dropdowns, so the HTML
#: never hard-codes an enum that lives in moxaserial/config.py.
ENUMS: dict[str, Any] = {
    "bauds": list(COMMON_BAUDS),
    "dataBits": list(DATA_BITS),
    "parities": list(PARITIES),
    "stopBits": list(STOP_BITS),
    "flowControls": list(FLOW_CONTROLS),
    "lineEndings": list(LINE_ENDINGS),
    "receiveLineEndings": list(RECEIVE_LINE_ENDINGS),
    "saveLineEndings": list(SAVE_LINE_ENDINGS),
    "receiveRemoveChars": list(RECEIVE_REMOVE_CHARS),
    "dialects": list(DIALECTS),
    "startTriggerModes": list(START_TRIGGER_MODES),
    "endTriggerModes": list(END_TRIGGER_MODES),
    "waitModes": list(WAIT_MODES),
    "overwritePolicies": list(OVERWRITE_POLICIES),
    "machineSelection": list(MACHINE_SELECTION),
    "themes": list(THEMES),
    "sendStates": [s.value for s in SendState],
    "receiveStates": [s.value for s in ReceiveState],
    "logLevels": ["DEBUG", "INFO", "WARNING", "ERROR"],
}
