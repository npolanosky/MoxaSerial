#!/usr/bin/env python3
"""Standalone dev harness for the MoxaSerial palette UI.

Serves ``resources/palette`` over HTTP and wires the *same*
:class:`moxaserial.bridge.Bridge` action router to the *same* engines, with
every machine forced onto :class:`moxaserial.transport.fake.FakeTransport`. The
result: the entire UI, send engine and receive engine can be exercised in
an ordinary browser on a laptop, with no Fusion and no hardware.

    python3 tools/dev_server.py --port 8765 --open

Then browse to http://127.0.0.1:8765/ .

Transport shim
--------------
``resources/palette/app.js`` checks for ``window.adsk``; in a browser it
falls back to ``POST /api`` (one JSON request/response per action) and
``GET /events`` (Server-Sent Events) for the push channel - the same two
directions the Fusion palette gets from ``incomingFromHTML`` and
``sendInfoToHTML``. No WebSocket, no third-party dependency.

Isolation
---------
The harness points ``MOXASERIAL_DATA_DIR`` at a scratch directory (by
default ``<repo>/.devdata``) so it can never touch real settings, and it
seeds a demo NC file plus a receive folder there.

Options:
  --port N          listen port (default 8765)
  --data DIR        settings/log/scratch directory
  --open            open a browser window
  --slow            make the simulator run at emulated wire speed
  --no-seed         do not create the demo NC file
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import queue
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PALETTE_DIR = ROOT / "resources" / "palette"


# --------------------------------------------------------------------------
# Demo content
# --------------------------------------------------------------------------
DEMO_NC = """%
O1042 (BRACKET OP1 - DEV SERVER DEMO)
(T1  10MM FLAT ENDMILL)
N10 G90 G94 G17 G21
N20 G53 G00 Z0.
N30 T1 M06
N40 S4500 M03
N50 G54
N60 M08
N70 G00 X-14.5 Y-32.  ; approach
N80 G43 Z15. H01
N90 G00 Z5.
N100 G01 Z-2.5 F300.
N110 G01 X-14.5 Y32. F1200.
N120 G02 X14.5 Y32. R14.5
N130 G01 X14.5 Y-32.
N140 G02 X-14.5 Y-32. R14.5
N150 G01 Z-5. F300.
N160 G01 X-14.5 Y32. F1200.
N170 G02 X14.5 Y32. R14.5
N180 G01 X14.5 Y-32.
N190 G02 X-14.5 Y-32. R14.5

N200 G00 Z15.
N210 M09
N220 G53 G00 Z0.
N230 M30
%
"""


def seed_files(data_dir: Path) -> Path:
    nc_dir = data_dir / "posted"
    nc_dir.mkdir(parents=True, exist_ok=True)
    demo = nc_dir / "1042_bracket_op1.nc"
    if not demo.exists():
        demo.write_text(DEMO_NC, encoding="ascii")
    (data_dir / "received").mkdir(parents=True, exist_ok=True)
    return demo


# --------------------------------------------------------------------------
# Host + transport wiring
# --------------------------------------------------------------------------

def build_bridge(data_dir: Path, slow: bool, seed: bool) -> Any:
    from moxaserial.bridge import Bridge, Host
    from moxaserial.config import ConfigStore
    from moxaserial.events import EventBus
    from moxaserial.transport.fake import SAMPLE_PROGRAM, FakeProfile, FakeTransport
    from moxaserial.ui import lastpost

    demo = seed_files(data_dir) if seed else None

    profile = FakeProfile(
        realtime=slow,
        time_scale=1.0 if slow else 0.25,
        buffer_size=256,
        drain_bytes_per_s=900.0,
        xoff_high_water=0.75,
        xon_low_water=0.25,
        ready_delay_s=0.8,
        outgoing=SAMPLE_PROGRAM,
        outgoing_gap_s=0.05,
    )

    def factory(machine: dict[str, Any], bus: EventBus) -> FakeTransport:
        """Every machine - even ones typed 'moxa' - runs on the simulator."""
        return FakeTransport(bus=bus, profile=profile)

    class DevHost(Host):
        name = "dev-server"

        def __init__(self, store: ConfigStore) -> None:
            self.store = store

        def pick_file(self, title: str = "", initial_dir: str = "", extensions: str = "") -> str:
            # No native dialog in a browser: hand back the newest demo file.
            info = self.last_posted_file()
            return info.get("path", "")

        def last_posted_file(self) -> dict[str, Any]:
            folders = [str(data_dir / "posted"), *self.store.get("watch_folders", [])]
            return lastpost.find_last_posted(folders)

        def toast(self, message: str, level: str = "info", title: str = "MoxaSerial") -> None:
            print(f"  [toast:{level}] {message}", flush=True)

        def reveal(self, path: str) -> bool:
            print(f"  [reveal] {path}", flush=True)
            return False

        def describe(self) -> dict[str, Any]:
            return {"host": self.name, "dataDir": str(data_dir), "demoFile": str(demo or "")}

    bus = EventBus()
    store = ConfigStore()

    # Point the built-in Simulator at the scratch receive folder and make
    # sure there is a 'moxa'-typed machine to look at in the UI too.
    sim = store.machine("simulator")
    if sim is not None:
        sim["receive"]["folder"] = str(data_dir / "received")
        sim["receive"]["overwrite"] = "ask"
        sim["send"]["wait_for_ready"] = "cts"
        store.upsert_machine(sim)
    if not any(m["type"] == "moxa" for m in store.machines()):
        from moxaserial.config import default_machine

        demo_machine = default_machine("NPort machine (demo)")
        demo_machine["host"] = "192.168.1.100"
        demo_machine["serial"]["baud"] = 19200
        demo_machine["receive"]["folder"] = str(data_dir / "received")
        store.upsert_machine(demo_machine)
    store.update_globals({"watch_folders": [str(data_dir / "posted")]})

    return Bridge(bus=bus, store=store, host=DevHost(store), transport_factory=factory)


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
class _Hub:
    """Fan-out of bridge push messages to every connected SSE client."""

    def __init__(self) -> None:
        self._clients: list[queue.Queue] = []
        self._lock = threading.Lock()

    def register(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=500)
        with self._lock:
            self._clients.append(q)
        return q

    def unregister(self, q: queue.Queue) -> None:
        with self._lock:
            if q in self._clients:
                self._clients.remove(q)

    def broadcast(self, action: str, payload: dict[str, Any]) -> None:
        frame = json.dumps({"action": action, "payload": payload}, default=str)
        with self._lock:
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(frame)
            except queue.Full:
                pass


def make_handler(bridge: Any, hub: _Hub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MoxaSerialDev/1.0"

        def log_message(self, fmt: str, *args: Any) -> None:
            if "/events" in (self.path or ""):
                return
            sys.stderr.write(f"  {self.address_string()} {fmt % args}\n")

        # -- GET --------------------------------------------------------
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            path = self.path.split("?", 1)[0]
            if path in ("/", ""):
                path = "/index.html"
            if path == "/events":
                self._serve_events()
                return
            if path == "/health":
                self._json({"ok": True, "state": bridge.sender.state.value})
                return
            self._serve_static(path)

        def _serve_static(self, path: str) -> None:
            rel = path.lstrip("/")
            target = (PALETTE_DIR / rel).resolve()
            if not str(target).startswith(str(PALETTE_DIR.resolve())) or not target.is_file():
                self.send_error(404, "Not found")
                return
            ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _serve_events(self) -> None:
            q = hub.register()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    try:
                        frame = q.get(timeout=10.0)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self.wfile.write(f"data: {frame}\n\n".encode())
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unregister(q)

        # -- POST -------------------------------------------------------
        def do_POST(self) -> None:  # noqa: N802
            if self.path.split("?", 1)[0] != "/api":
                self.send_error(404, "Not found")
                return
            length = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                msg = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._json({"ok": False, "error": "malformed JSON"}, status=400)
                return
            reply = bridge.handle(str(msg.get("action", "")), msg.get("payload") or {})
            self._json(reply)

        def _json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, default=str).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# --------------------------------------------------------------------------
# Simulated control behaviour
# --------------------------------------------------------------------------

def arm_simulated_punchout(bridge: Any) -> None:
    """When a receive starts, have the fake control punch a program out."""
    from moxaserial.transport.fake import SAMPLE_PROGRAM, FakeTransport

    def on_state(evt: Any) -> None:
        if evt.topic != "receive.state":
            return
        if evt.payload.get("state") != "WAITING":
            return

        def _arm() -> None:
            time.sleep(0.4)
            transport = getattr(bridge.receiver, "_transport", None)
            if isinstance(transport, FakeTransport) and transport.is_open:
                transport.arm_receive(SAMPLE_PROGRAM)

        threading.Thread(target=_arm, daemon=True).start()

    bridge.bus.subscribe("receive.state", on_state)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--data", default=str(ROOT / ".devdata"))
    parser.add_argument("--open", action="store_true", help="open a browser window")
    parser.add_argument("--slow", action="store_true", help="emulate real wire speed")
    parser.add_argument("--no-seed", dest="seed", action="store_false")
    args = parser.parse_args(argv)

    data_dir = Path(args.data).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["MOXASERIAL_DATA_DIR"] = str(data_dir)

    bridge = build_bridge(data_dir, slow=args.slow, seed=args.seed)
    hub = _Hub()
    bridge.subscribe_outbound(hub.broadcast)
    arm_simulated_punchout(bridge)

    server = ThreadingHTTPServer((args.host, args.port), make_handler(bridge, hub))
    server.daemon_threads = True
    url = f"http://{args.host}:{args.port}/"

    print("MoxaSerial dev server")
    print(f"  UI          {url}")
    print(f"  data dir    {data_dir}")
    print(f"  transport   FakeTransport ({'realtime' if args.slow else 'fast'})")
    print("  Ctrl-C to stop.\n", flush=True)

    if args.open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopping…")
    finally:
        bridge.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
