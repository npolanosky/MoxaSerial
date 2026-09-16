"""Add-in lifecycle: wire the Fusion host, the bridge, the palette, toolbar.

``MoxaSerial.py`` only calls :func:`start` and :func:`stop`.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import traceback
from typing import Any

from moxaserial.bridge import Bridge, Host
from moxaserial.config import ConfigStore
from moxaserial.events import default_bus
from moxaserial.log import LogManager, get_logger
from moxaserial.ui import commands, lastpost, toast
from moxaserial.ui.palette import PaletteBridge

log = get_logger("app")

_state: dict[str, Any] = {
    "bridge": None,
    "palette": None,
    "app": None,
    "handlers": None,
}


class FusionHost(Host):
    """Everything :class:`moxaserial.bridge.Bridge` needs that only Fusion can do."""

    name = "fusion"

    def __init__(self, store: ConfigStore) -> None:
        self.store = store
        # Remembered for the lifetime of the session so a second Browse
        # starts where the first one left off. Deliberately not persisted:
        # it is a convenience, not a setting.
        self._last_browse_dir = ""

    def pick_file(
        self, title: str = "Select an NC file", initial_dir: str = "", extensions: str = ""
    ) -> str:
        try:
            import adsk.core  # type: ignore

            ui = adsk.core.Application.get().userInterface
            dlg = ui.createFileDialog()
            dlg.title = title
            exts = ["nc", "ncf", "tap", "cnc", "gcode", "h", "txt", "min", "eia"]
            for extra in re.split(r"[,; ]+", extensions or ""):
                extra = extra.strip().lstrip("*").lstrip(".").lower()
                if extra and extra not in exts:
                    exts.append(extra)
            dlg.filter = (
                "NC programs (" + ";".join("*." + e for e in exts) + ");;All files (*.*)"
            )
            dlg.isMultiSelectEnabled = False
            start_dir = self._last_browse_dir or initial_dir
            if initial_dir and os.path.isdir(initial_dir) and not self._last_browse_dir:
                start_dir = initial_dir
            if start_dir and os.path.isdir(start_dir):
                dlg.initialDirectory = start_dir
            if dlg.showOpen() == adsk.core.DialogResults.DialogOK:
                self._last_browse_dir = os.path.dirname(dlg.filename)
                return dlg.filename
        except Exception:
            log.debug("File dialog failed", exc_info=True)
        return ""

    def pick_folder(self, title: str = "Select a folder") -> str:
        try:
            import adsk.core  # type: ignore

            ui = adsk.core.Application.get().userInterface
            dlg = ui.createFolderDialog()
            dlg.title = title
            if dlg.showDialog() == adsk.core.DialogResults.DialogOK:
                return dlg.folder
        except Exception:
            log.debug("Folder dialog failed", exc_info=True)
        return ""

    def last_posted_file(self) -> dict[str, Any]:
        return lastpost.find_last_posted(self.store.get("watch_folders", []))

    def toast(self, message: str, level: str = "info", title: str = "MoxaSerial") -> None:
        toast.show(message, level=level, title=title)

    def reveal(self, path: str) -> bool:
        if not path or not os.path.exists(path):
            return False
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["/usr/bin/open", "-R", path])
            elif os.name == "nt":
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                return False
            return True
        except Exception:
            log.debug("Reveal failed for %s", path, exc_info=True)
            return False

    def describe(self) -> dict[str, Any]:
        info: dict[str, Any] = {"host": self.name}
        try:
            import adsk.core  # type: ignore

            app = adsk.core.Application.get()
            info["fusionVersion"] = app.version
            doc = app.activeDocument
            info["document"] = doc.name if doc else ""
        except Exception:
            pass
        return info


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def start(handlers: list) -> None:
    import adsk.core  # type: ignore

    app = adsk.core.Application.get()
    ui = app.userInterface

    bus = default_bus()
    LogManager.instance(bus)
    store = ConfigStore()
    host = FusionHost(store)
    bridge = Bridge(bus=bus, store=store, host=host)
    palette = PaletteBridge(bridge)

    _state.update({"bridge": bridge, "palette": palette, "app": app, "handlers": handlers})

    toast.install(app, handlers)
    palette.install(app, handlers)
    commands.create(
        ui,
        handlers,
        {
            "send_last": send_last_program,
            "open_panel": open_panel,
            "receive": receive_now,
        },
    )
    log.info("MoxaSerial %s started.", bridge.state()["version"])


def stop() -> None:
    app = _state.get("app")
    bridge: Bridge | None = _state.get("bridge")
    palette: PaletteBridge | None = _state.get("palette")
    try:
        if bridge is not None:
            bridge.shutdown()
        if app is not None:
            if palette is not None:
                palette.destroy(app)
            toast.uninstall(app)
            commands.destroy(app.userInterface)
    except Exception:
        log.error("Shutdown problem:\n%s", traceback.format_exc())
    finally:
        _state.update({"bridge": None, "palette": None, "app": None, "handlers": None})
        log.info("MoxaSerial stopped.")


# --------------------------------------------------------------------------
# Toolbar actions
# --------------------------------------------------------------------------

def open_panel() -> None:
    app = _state.get("app")
    palette: PaletteBridge | None = _state.get("palette")
    handlers = _state.get("handlers") or []
    if app is None or palette is None:
        return
    palette.show(app, handlers)


def send_last_program() -> None:
    """One-click: newest posted file -> active machine. No palette needed."""
    bridge: Bridge | None = _state.get("bridge")
    if bridge is None:
        return
    machine = bridge.store.active_machine()
    info = bridge.host.last_posted_file()
    path = info.get("path", "")
    if not path:
        bridge.host.toast(
            info.get("error", "No posted NC program found."), level="warning"
        )
        return
    if bridge.sender.is_running:
        bridge.host.toast("A send is already in progress.", level="warning")
        return
    confirmed = False
    if bridge.store.get("confirm_before_send", False):
        try:
            import adsk.core  # type: ignore

            ui = adsk.core.Application.get().userInterface
            answer = ui.messageBox(
                f"Send {os.path.basename(path)} to {machine['name']}?",
                "MoxaSerial",
                adsk.core.MessageBoxButtonTypes.YesNoButtonType,
                adsk.core.MessageBoxIconTypes.QuestionIconType,
            )
            if answer != adsk.core.DialogResults.DialogYes:
                return
        except Exception:
            log.debug("Confirm dialog failed; sending without confirmation", exc_info=True)
        confirmed = True
    bridge.host.toast(f"Sending {os.path.basename(path)} to {machine['name']}...", "info")
    reply = bridge.handle(
        "send.start", {"machineId": machine["id"], "path": path, "confirmed": confirmed}
    )
    if not reply.get("ok"):
        bridge.host.toast(reply.get("error", "Send failed to start."), level="error")


def receive_now() -> None:
    bridge: Bridge | None = _state.get("bridge")
    if bridge is None:
        return
    machine = bridge.store.active_machine()
    reply = bridge.handle("receive.start", {"machineId": machine["id"]})
    if reply.get("ok"):
        bridge.host.toast(f"Waiting for a program from {machine['name']}...", "info")
        open_panel()
    else:
        bridge.host.toast(reply.get("error", "Could not start receiving."), level="error")
