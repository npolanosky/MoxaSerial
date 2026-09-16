"""The Fusion palette: HTML <-> Python message bridge.

Event flow, in one direction and then the other::

    JS  adsk.fusionSendData(action, jsonData)
      -> Palette.incomingFromHTML  (main thread)
      -> PaletteBridge._on_html
      -> Bridge.handle(action, data)
      -> returns a JSON string, which becomes the resolved value of the
         Promise that adsk.fusionSendData() returned in JS.

    engine thread  bus.publish("send.progress", {...})
      -> Bridge._on_event -> Bridge.push(action, payload)
      -> PaletteBridge._queue  (thread-safe deque)
      -> app.fireCustomEvent(PUSH_EVENT_ID)          [thread hop]
      -> _PushHandler.notify   (MAIN THREAD)
      -> Palette.sendInfoToHTML(action, jsonPayload)
      -> window.fusionJavaScriptHandler.handle(action, data) in JS

The queue plus a single custom event is deliberate: ``fireCustomEvent``
is cheap and coalescing many progress events into one main-thread drain
keeps a 115200-baud send from flooding Fusion's event loop.

``sendInfoToHTML`` is only legal on the main thread, hence the hop.
"""

from __future__ import annotations

import json
import os
import threading
from collections import deque
from typing import Any

from moxaserial.bridge import Bridge
from moxaserial.log import get_logger

log = get_logger("palette")

PALETTE_ID = "P3D_MoxaSerial_Palette"
PALETTE_NAME = "Moxa DNC"
PUSH_EVENT_ID = "P3D_MoxaSerial_Push"

#: Docked palettes in Fusion are usually narrow; the UI is designed for this.
DEFAULT_WIDTH = 460
DEFAULT_HEIGHT = 760

#: Cap on the outbound queue. Progress events are idempotent snapshots, so
#: dropping the oldest under pressure loses nothing that matters.
QUEUE_LIMIT = 400


def palette_html_path() -> str:
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(root, "resources", "palette", "index.html")


def palette_html_url() -> str:
    """The palette page as a ``file:///`` URL.

    Fusion accepts a plain path on macOS, but on Windows a backslash drive
    path becomes a malformed ``file:///`` URL that Chromium refuses
    (ERR_INVALID_URL). A proper RFC 8089 URI works on both.
    """
    from pathlib import Path

    return Path(palette_html_path()).resolve().as_uri()


class PaletteBridge:
    """Owns the palette object and both directions of the message bridge."""

    def __init__(self, bridge: Bridge) -> None:
        self.bridge = bridge
        self._queue: deque[tuple[str, dict[str, Any]]] = deque(maxlen=QUEUE_LIMIT)
        self._lock = threading.Lock()
        self._handlers: list = []
        self._event = None
        self._app = None
        self._palette = None
        self._unsub = None
        self._pending_fire = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    def install(self, app: Any, handlers: list) -> None:
        """Register the custom event used for the thread hop."""
        import adsk.core  # type: ignore

        outer = self

        class _PushHandler(adsk.core.CustomEventHandler):
            def notify(self, args: Any) -> None:  # runs on the MAIN thread
                try:
                    outer._drain()
                except Exception:
                    log.exception("Failed to drain the outbound palette queue")

        self._app = app
        try:
            app.unregisterCustomEvent(PUSH_EVENT_ID)
        except Exception:
            pass
        self._event = app.registerCustomEvent(PUSH_EVENT_ID)
        handler = _PushHandler()
        self._event.add(handler)
        handlers.append(handler)
        self._handlers.append(handler)
        self._unsub = self.bridge.subscribe_outbound(self._enqueue)

    def show(self, app: Any, handlers: list) -> Any:
        """Create (or reveal) the palette."""
        import adsk.core  # type: ignore

        ui = app.userInterface
        palette = ui.palettes.itemById(PALETTE_ID)
        if palette is not None and self._palette is None:
            # Left over from a previous add-in instance (stop() failed to
            # remove it, or Fusion kept it across a reload). Its HTML event
            # handler belongs to dead code, so recreate rather than reuse.
            try:
                palette.deleteMe()
            except Exception:
                log.debug("Could not delete stale palette", exc_info=True)
            palette = ui.palettes.itemById(PALETTE_ID)
        if palette is None:
            html = palette_html_path()
            if not os.path.isfile(html):
                raise FileNotFoundError(f"Palette HTML missing: {html}")
            palette = ui.palettes.add(
                PALETTE_ID,
                PALETTE_NAME,
                palette_html_url(),
                True,   # isVisible
                True,   # showCloseButton
                True,   # isResizable
                DEFAULT_WIDTH,
                DEFAULT_HEIGHT,
                True,   # useNewWebBrowser - Qt WebEngine
            )
            outer = self

            class _HTMLHandler(adsk.core.HTMLEventHandler):
                def notify(self, args: adsk.core.HTMLEventArgs) -> None:
                    try:
                        args.returnData = outer._on_html(args.action, args.data)
                    except Exception:
                        log.exception("Palette HTML event failed")
                        args.returnData = json.dumps(
                            {"ok": False, "error": "internal error - see the log"}
                        )

            html_handler = _HTMLHandler()
            palette.incomingFromHTML.add(html_handler)
            handlers.append(html_handler)
            self._handlers.append(html_handler)
            log.info("Palette created (%s).", html)
            # A fresh page pulls the current state itself; stale queued
            # events would only rewind it.
            with self._lock:
                self._queue.clear()
                self._pending_fire = False

        palette.isVisible = True
        self._palette = palette
        # Re-showing a hidden palette: flush what queued up meanwhile.
        with self._lock:
            has_backlog = bool(self._queue) and not self._pending_fire
            if has_backlog:
                self._pending_fire = True
        if has_backlog and self._app is not None:
            try:
                self._app.fireCustomEvent(PUSH_EVENT_ID, "")
            except Exception:
                log.debug("Could not fire palette flush", exc_info=True)
        return palette

    def hide(self, app: Any) -> None:
        try:
            palette = app.userInterface.palettes.itemById(PALETTE_ID)
            if palette:
                palette.isVisible = False
        except Exception:
            pass

    @property
    def is_visible(self) -> bool:
        try:
            if self._app is None:
                return False
            palette = self._app.userInterface.palettes.itemById(PALETTE_ID)
            return bool(palette and palette.isVisible)
        except Exception:
            return False

    def destroy(self, app: Any) -> None:
        if self._unsub is not None:
            self._unsub()
            self._unsub = None
        try:
            palette = app.userInterface.palettes.itemById(PALETTE_ID)
            if palette:
                palette.deleteMe()
        except Exception:
            pass
        try:
            app.unregisterCustomEvent(PUSH_EVENT_ID)
        except Exception:
            pass
        self._palette = None
        self._event = None
        self._handlers.clear()
        with self._lock:
            self._queue.clear()

    # ------------------------------------------------------------------
    # Inbound: JS -> Python
    # ------------------------------------------------------------------
    def _on_html(self, action: str, data: str) -> str:
        """Runs on the main thread. Returns the JSON reply for the Promise."""
        if action == "response":
            # Fusion's new palette browser echoes the JS handler's return
            # value from sendInfoToHTML back to us as an incoming "response"
            # action. It is not a request; acknowledge and drop it.
            return json.dumps({"ok": True})
        log.debug("HTML -> Python: %s", action)
        reply = self.bridge.handle_json(action, data)
        return reply or json.dumps({"ok": True})

    # ------------------------------------------------------------------
    # Outbound: Python (any thread) -> JS
    # ------------------------------------------------------------------
    def _enqueue(self, action: str, payload: dict[str, Any]) -> None:
        with self._lock:
            # Collapse consecutive progress snapshots - only the latest matters.
            if action in ("send.progress", "receive.progress") and self._queue:
                last_action, _ = self._queue[-1]
                if last_action == action:
                    self._queue[-1] = (action, payload)
                else:
                    self._queue.append((action, payload))
            else:
                self._queue.append((action, payload))
            already_pending = self._pending_fire
            self._pending_fire = True

        if already_pending:
            return
        app = self._app
        if app is None:
            return
        try:
            app.fireCustomEvent(PUSH_EVENT_ID, "")
        except Exception:
            with self._lock:
                self._pending_fire = False

    def _drain(self) -> None:
        """MAIN THREAD. Flush the queue into the palette."""
        palette = self._palette
        if palette is None or not self.is_visible:
            # Keep everything queued; show() flushes it when the palette is
            # back. Dropping here lost send.done / overwrite prompts fired
            # by toolbar-started jobs while the panel was closed.
            with self._lock:
                self._pending_fire = False
            return
        with self._lock:
            items = list(self._queue)
            self._queue.clear()
            self._pending_fire = False
        if not items:
            return
        for action, payload in items:
            try:
                palette.sendInfoToHTML(action, json.dumps(payload, default=str))
            except Exception:
                log.debug("sendInfoToHTML(%s) failed", action, exc_info=True)
