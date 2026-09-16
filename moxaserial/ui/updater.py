"""Restart the add-in from inside Fusion, after an update has been applied.

Why this exists
---------------
Fusion has no "reload add-in" API. What it *does* have, confirmed by the
sibling add-in that already ships this, is the ``Script`` object:

* ``adsk.core.Application.scripts`` - a ``ScriptDefinitions`` collection that
  lists every registered script **and add-in**;
* ``scripts.itemByPath(folder)`` / ``scripts.itemsByName(name)`` to find one;
* ``script.stop()`` / ``script.run(False)`` / ``script.isRunning`` to cycle it.

Evidence (both files read, not guessed):

* ``Fusion-Essentials-P3D/commands/addinManager/reload.py:64-83`` -
  ``reload_script()``: ``script.stop()`` -> purge -> ``script.run(False)`` ->
  re-read ``script.isRunning`` to prove it came back.
* ``Fusion-Essentials-P3D/commands/addinManager/reload.py:184-199`` -
  ``_find_script_by_folder()``: ``app.scripts.itemByPath(folder)`` first, then
  a linear scan comparing ``script.folder``.
* ``Fusion-Essentials-P3D/commands/mcpServer/tools/sys_reload_addin.py:74-106,
  109-133`` - ``_purge_addin_modules()`` and ``_ReloadEventHandler``.

Two facts that dictate the shape of this module
-----------------------------------------------
**1. stop() + run() alone reloads nothing.** Every Fusion add-in shares one
interpreter, so ``run()`` re-imports the *cached* modules and the new code on
disk is ignored. The modules under the add-in root must be deleted from
``sys.modules`` **between** stop and run
(``sys_reload_addin.py:121-123`` is emphatic about this). Our own
``moxaserial_loader.purge()`` covers ``moxaserial.*`` but not the
``__main__<encoded-path>`` namespace Fusion uses for ``MoxaSerial.py``
itself, so :func:`purge_modules_under` purges by file path instead.

**2. Self-reload cannot happen inline.** ``script.stop()`` tears down the very
code running the call - including the palette, the custom events and the
thread that asked for the restart. So the restart is *deferred*: a short timer
fires a custom event, and the handler runs on Fusion's main thread after the
caller has returned. This is the same hop the palette push and the toast hide
already use, and the same one the sibling defers self-reload through
(``reload.py:141-181``).

Nothing here is ever called from a worker thread except
:func:`restart_addin`, and all it does there is start a timer that calls
``app.fireCustomEvent`` - the one adsk call the rest of this add-in already
makes off the main thread.
"""

from __future__ import annotations

import os
import sys
import threading
import traceback
from typing import Any

from moxaserial.log import get_logger

log = get_logger("updater")

#: Custom event that performs the stop/purge/run cycle on the main thread.
RESTART_EVENT_ID = "P3D_MoxaSerial_Restart"

#: Delay before the restart fires, so the bridge reply and the final push
#: reach the palette before the palette is torn down.
RESTART_DELAY_S = 1.0

_state: dict[str, Any] = {
    "installed": False,
    "event": None,
    "handler": None,
    "app": None,
    "root": "",
    # True while _restart_now() is inside script.stop(). stop() calls
    # uninstall(), and unregistering the very custom event whose handler is
    # still on the stack is not something to find out about the hard way -
    # the incoming instance's install() unregisters defensively anyway.
    "restarting": False,
}


def addin_root() -> str:
    """Absolute path of the add-in folder (where ``MoxaSerial.manifest`` is).

    This file is ``<root>/moxaserial/ui/updater.py``, so the root is three
    levels up.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.normpath(os.path.join(here, "..", ".."))


# --------------------------------------------------------------------------
# Module cache
# --------------------------------------------------------------------------

def purge_modules_under(root: str) -> int:
    """Delete every loaded module whose file lives under *root*. Returns the count.

    Never touches ``adsk.*``, the stdlib or another add-in: a module with no
    ``__file__`` is skipped and only files genuinely under *root* are removed.
    This module purges itself too, which is harmless - the live frame runs to
    completion and the next ``run()`` imports a fresh copy.
    """
    if not root:
        return 0
    root_cmp = os.path.normcase(os.path.abspath(root))
    doomed = []
    for name, mod in list(sys.modules.items()):
        if mod is None:
            continue
        path = getattr(mod, "__file__", None)
        if not path:
            continue
        try:
            if os.path.normcase(os.path.abspath(path)).startswith(root_cmp + os.sep):
                doomed.append(name)
        except Exception:  # noqa: BLE001 - a weird __file__ is not worth a crash
            continue
    for name in doomed:
        try:
            del sys.modules[name]
        except KeyError:
            pass
    return len(doomed)


# --------------------------------------------------------------------------
# Finding our own Script object
# --------------------------------------------------------------------------

def find_self_script(app: Any, root: str = "") -> Any:
    """The ``Script``/add-in entry for this add-in, or ``None``.

    By folder first (precise), then by name, then a linear scan of
    ``app.scripts`` comparing ``script.folder`` - exactly the ladder
    ``reload.py:184-199`` uses, because ``itemByPath`` has been seen to miss
    when the registered path differs in case or trailing separator.
    """
    root = root or addin_root()
    try:
        scripts = app.scripts
    except Exception:  # noqa: BLE001
        log.debug("app.scripts is unavailable", exc_info=True)
        return None
    try:
        found = scripts.itemByPath(root)
        if found is not None:
            return found
    except Exception:  # noqa: BLE001
        pass
    try:
        matches = scripts.itemsByName(os.path.basename(root))
        if matches:
            return matches[0]
    except Exception:  # noqa: BLE001
        pass
    target = _norm(root)
    try:
        for i in range(scripts.count):
            script = scripts.item(i)
            try:
                if _norm(script.folder) == target:
                    return script
            except Exception:  # noqa: BLE001
                continue
    except Exception:  # noqa: BLE001
        pass
    return None


def _norm(path: str) -> str:
    if not path:
        return ""
    return path.replace("\\", "/").rstrip("/").lower()


# --------------------------------------------------------------------------
# Lifecycle
# --------------------------------------------------------------------------

def install(app: Any, handlers: list) -> bool:
    """Register the restart custom event. Called once from ``app.start()``."""
    if _state["installed"]:
        return True
    try:
        import adsk.core  # type: ignore

        class _RestartHandler(adsk.core.CustomEventHandler):
            def notify(self, args: Any) -> None:  # MAIN THREAD
                _restart_now()

        try:
            app.unregisterCustomEvent(RESTART_EVENT_ID)
        except Exception:  # noqa: BLE001 - a crashed session can leave it claimed
            pass
        evt = app.registerCustomEvent(RESTART_EVENT_ID)
        handler = _RestartHandler()
        evt.add(handler)
        handlers.append(handler)
        _state.update(
            {"installed": True, "event": evt, "handler": handler, "app": app,
             "root": addin_root()}
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("Restart custom event unavailable: %s", exc)
        return False


def uninstall(app: Any) -> None:
    if _state["installed"] and not _state.get("restarting"):
        try:
            app.unregisterCustomEvent(RESTART_EVENT_ID)
        except Exception:  # noqa: BLE001
            pass
    if _state.get("restarting"):
        # Mid-restart: keep the event alive until the new instance replaces it.
        _state["installed"] = False
        return
    _state.update({"installed": False, "event": None, "handler": None, "app": None})


def restart_addin(delay: float = RESTART_DELAY_S) -> bool:
    """Schedule the stop/purge/run cycle. Safe from any thread.

    Returns ``True`` when the restart was *scheduled* - not when it
    succeeded, which cannot be known here because this code stops existing
    partway through it. ``False`` means the caller should fall back to
    telling the operator to restart Fusion; :class:`UpdateService` does
    exactly that.
    """
    app = _state.get("app")
    if not _state["installed"] or app is None:
        log.warning("Restart requested but the restart event is not installed.")
        return False

    def _fire() -> None:
        # Timer thread: fireCustomEvent is the only adsk call made off the
        # main thread anywhere in this add-in, and it is the documented way in.
        try:
            app.fireCustomEvent(RESTART_EVENT_ID, "")
        except Exception:  # noqa: BLE001
            log.warning("Could not fire the restart event", exc_info=True)

    timer = threading.Timer(max(0.0, delay), _fire)
    timer.daemon = True
    timer.name = "moxa-update-restart"
    timer.start()
    log.info("Add-in restart scheduled in %.1f s.", delay)
    return True


def _restart_now() -> None:
    """MAIN THREAD. stop -> purge the module cache -> run.

    The purge between the two is the whole point: without it ``run()``
    re-imports the modules already cached in the shared interpreter and the
    freshly installed files on disk are never read.
    """
    root = _state.get("root") or addin_root()
    app = _state.get("app")
    try:
        script = find_self_script(app, root)
        if script is None:
            log.error(
                "Add-in restart: could not find our own entry in app.scripts; "
                "restart Fusion to load the update."
            )
            _fallback_message(
                "MoxaSerial was updated. Restart Fusion to load the new version."
            )
            return
        _state["restarting"] = True
        try:
            script.stop()
        except Exception:  # noqa: BLE001 - stop() may already have happened
            log.debug("script.stop() raised during restart", exc_info=True)
        # CRITICAL: bust the module cache BETWEEN stop and run, or run()
        # re-imports the stale cached modules and the update never loads.
        purged = purge_modules_under(root)
        log.info("Add-in restart: purged %d cached module(s).", purged)
        script.run(False)
    except Exception:
        log.error("Add-in restart failed:\n%s", traceback.format_exc())
        _fallback_message(
            "MoxaSerial was updated but could not reload itself. Restart Fusion "
            "to load the new version."
        )


def _fallback_message(text: str) -> None:
    """Last resort: say it in the status bar rather than lose the news."""
    try:
        import adsk.core  # type: ignore

        ui = adsk.core.Application.get().userInterface
        ui.statusMessage = f"MoxaSerial: {text}"
    except Exception:  # noqa: BLE001
        log.info("MoxaSerial: %s", text)
