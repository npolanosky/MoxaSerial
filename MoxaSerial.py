"""MoxaSerial - send posted NC programs to a CNC over RS-232 via a Moxa NPort.

Fusion add-in entry point. Everything of substance lives in ``moxaserial``; this
file only bootstraps the UNC-safe loader and delegates to ``moxaserial.ui.app``.
"""

import os
import sys
import traceback

import adsk.core  # type: ignore  # noqa: F401  (required so Fusion binds the add-in)

_ADDIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _ADDIN_DIR not in sys.path:
    sys.path.insert(0, _ADDIN_DIR)


def _import_loader():
    """Load our loader by explicit path.

    Every Fusion add-in shares one interpreter, and several sibling add-ins
    ship a top-level ``loader.py`` / ``moxaserial`` package of their own. Loading by
    path under a unique module name keeps us from picking up theirs.
    """
    import importlib.util

    name = "moxaserial_loader"
    path = os.path.join(_ADDIN_DIR, name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


loader = _import_loader()

# Keep strong references so Fusion event handlers are not garbage collected.
_handlers: list = []


def run(context: dict) -> None:  # noqa: D401 - Fusion API signature
    """Called by Fusion when the add-in starts."""
    try:
        loader.purge()
        app_mod = loader.load("moxaserial.ui.app")
        app_mod.start(_handlers)
    except Exception:
        _show_error(traceback.format_exc())


def stop(context: dict) -> None:  # noqa: D401 - Fusion API signature
    """Called by Fusion when the add-in stops."""
    try:
        app_mod = loader.load("moxaserial.ui.app")
        app_mod.stop()
    except Exception:
        _show_error(traceback.format_exc())
    finally:
        _handlers.clear()


def _show_error(tb: str) -> None:
    """Best-effort error surfacing: Text Commands window plus a message box."""
    print(f"[MoxaSerial] ERROR:\n{tb}")
    try:
        import adsk.core as _core  # type: ignore

        app = _core.Application.get()
        if app and app.userInterface:
            app.userInterface.messageBox(f"MoxaSerial error:\n{tb}")
    except Exception:
        pass
