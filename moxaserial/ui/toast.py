"""Non-blocking notifications inside Fusion.

Fusion has no toast API. Verified options, worst to best for our purpose:

* ``ui.messageBox(...)`` - **modal**. It stops the send loop's UI thread
  and forces a click, so it is only used as the last-resort fallback for
  errors, and never during a transfer.
* ``ui.statusMessage = "..."`` - text in the lower-right corner. Cheap,
  non-blocking, but has no styling and an indeterminate lifetime.
* ``ui.progressBar`` - lower-right, **non-modal** when
  ``show(msg, min, max, isModal=False)``, supports ``%p %v %m`` in the
  message and can be hidden programmatically. This is the closest thing
  Fusion has to a transient toast that the operator will actually notice.

Strategy implemented here: a short-lived non-modal progress bar showing
the message, auto-hidden after a few seconds by a timer, with
``statusMessage`` set as well (it survives the bar disappearing), falling
back to ``statusMessage`` alone and finally to a message box for errors
if the progress bar is unavailable.

Threading: the Fusion API must only be touched from the main thread -
calling ``ui.progressBar`` from the send engine's thread crashed Fusion
outright. :func:`show` therefore checks the calling thread and, when it
is not the main thread, queues the message and fires a custom event so
the actual ``progressBar``/``statusMessage`` calls run on the main thread
(:data:`SHOW_EVENT_ID`). The auto-hide timer hops back the same way
(:data:`HIDE_EVENT_ID`) and never calls adsk itself.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from typing import Any

from moxaserial.log import get_logger

log = get_logger("toast")

#: Custom event used to hop the "hide the toast" call back onto the main thread.
HIDE_EVENT_ID = "P3D_MoxaSerial_ToastHide"
#: Custom event used to hop a "show the toast" call from a worker thread.
SHOW_EVENT_ID = "P3D_MoxaSerial_ToastShow"

DEFAULT_SECONDS = 4.0

_ICON = {"info": "", "success": "", "warning": "! ", "error": "!! "}

_state: dict[str, Any] = {
    "installed": False,
    "event": None,
    "show_event": None,
    "handlers": [],
    "timer": None,
    "token": 0,
    "app": None,
}
_pending: deque = deque(maxlen=20)
_pending_lock = threading.Lock()


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def install(app: Any, handlers: list) -> None:
    """Register the hide custom event. Call once from the add-in's run()."""
    if _state["installed"]:
        return
    try:
        import adsk.core  # type: ignore

        class _HideHandler(adsk.core.CustomEventHandler):
            def notify(self, args: Any) -> None:
                _hide_now()

        class _ShowHandler(adsk.core.CustomEventHandler):
            def notify(self, args: Any) -> None:  # main thread
                _drain_pending()

        for eid in (HIDE_EVENT_ID, SHOW_EVENT_ID):
            try:
                app.unregisterCustomEvent(eid)
            except Exception:
                pass
        evt = app.registerCustomEvent(HIDE_EVENT_ID)
        handler = _HideHandler()
        evt.add(handler)
        show_evt = app.registerCustomEvent(SHOW_EVENT_ID)
        show_handler = _ShowHandler()
        show_evt.add(show_handler)
        handlers.extend([handler, show_handler])
        _state["event"] = evt
        _state["show_event"] = show_evt
        _state["handlers"].extend([handler, show_handler])
        _state["app"] = app
        _state["installed"] = True
    except Exception as exc:  # noqa: BLE001
        log.debug("Toast custom event unavailable: %s", exc)


def uninstall(app: Any) -> None:
    timer = _state.get("timer")
    if timer is not None:
        timer.cancel()
    _state["timer"] = None
    try:
        _hide_now()
    except Exception:
        pass
    if _state["installed"]:
        for eid in (HIDE_EVENT_ID, SHOW_EVENT_ID):
            try:
                app.unregisterCustomEvent(eid)
            except Exception:
                pass
    _state["installed"] = False
    _state["event"] = None
    _state["show_event"] = None
    _state["app"] = None
    _state["handlers"].clear()
    with _pending_lock:
        _pending.clear()


def show(
    message: str,
    level: str = "info",
    title: str = "MoxaSerial",
    seconds: float = DEFAULT_SECONDS,
) -> None:
    """Show a transient notification. Never raises; safe from any thread.

    Off the main thread the call is queued and replayed on the main thread
    via a custom event; the Fusion API is never touched from a worker.
    """
    if not _on_main_thread():
        app = _state.get("app")
        if not _state["installed"] or app is None:
            log.info("[toast:%s] %s", level, message)
            return
        with _pending_lock:
            _pending.append((message, level, title, seconds))
        try:
            app.fireCustomEvent(SHOW_EVENT_ID, "")
        except Exception as exc:  # noqa: BLE001
            log.debug("Could not fire toast event: %s", exc)
        return
    _show_now(message, level, title, seconds)


def _drain_pending() -> None:
    """Main thread: show everything queued by worker threads."""
    while True:
        with _pending_lock:
            if not _pending:
                return
            message, level, title, seconds = _pending.popleft()
        _show_now(message, level, title, seconds)


def _show_now(message: str, level: str, title: str, seconds: float) -> None:
    text = f"{_ICON.get(level, '')}{title}: {message}"
    try:
        import adsk.core  # type: ignore

        app = adsk.core.Application.get()
        ui = app.userInterface
    except Exception:
        log.info("[toast:%s] %s", level, message)
        return

    shown = False
    try:
        bar = ui.progressBar
        # Non-modal: appears lower-right and does not steal focus or block.
        bar.show(text, 0, 1, False)
        bar.progressValue = 1
        shown = True
    except Exception as exc:  # noqa: BLE001
        log.debug("progressBar toast unavailable: %s", exc)

    try:
        ui.statusMessage = text
    except Exception:
        pass

    if shown:
        _schedule_hide(app, seconds)
    elif level == "error":
        # Nothing transient worked - an error is worth a modal box.
        try:
            ui.messageBox(message, title)
        except Exception:
            pass

    log.debug("Toast (%s): %s", level, message)


def _schedule_hide(app: Any, seconds: float) -> None:
    timer = _state.get("timer")
    if timer is not None:
        timer.cancel()
    _state["token"] = int(time.time() * 1000)

    def _fire() -> None:
        # Timer thread: only ever hop to the main thread, never call adsk.
        if _state["installed"]:
            try:
                app.fireCustomEvent(HIDE_EVENT_ID, "")
            except Exception:
                pass

    t = threading.Timer(max(0.5, seconds), _fire)
    t.daemon = True
    _state["timer"] = t
    t.start()


def _hide_now() -> None:
    try:
        import adsk.core  # type: ignore

        ui = adsk.core.Application.get().userInterface
        ui.progressBar.hide()
    except Exception:
        pass
