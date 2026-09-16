"""Per-user application data locations (macOS + Windows + POSIX fallback)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

APP_NAME = "MoxaSerial"

_OVERRIDE_ENV = "MOXASERIAL_DATA_DIR"


def app_data_dir() -> Path:
    """Directory for settings, logs and other per-user add-in state.

    * macOS   -> ``~/Library/Application Support/MoxaSerial``
    * Windows -> ``%APPDATA%/MoxaSerial``
    * other   -> ``$XDG_CONFIG_HOME/MoxaSerial`` or ``~/.config/MoxaSerial``

    ``MOXASERIAL_DATA_DIR`` overrides all of the above (used by the test
    suite and the dev server so they never touch real user settings).
    """
    override = os.environ.get(_OVERRIDE_ENV)
    if override:
        return Path(override).expanduser()

    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if os.name == "nt":
        base = os.environ.get("APPDATA") or str(Path.home() / "AppData" / "Roaming")
        return Path(base) / APP_NAME
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / APP_NAME


def ensure_app_data_dir() -> Path:
    d = app_data_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def settings_path() -> Path:
    return app_data_dir() / "settings.json"


def log_dir() -> Path:
    return app_data_dir() / "logs"


def log_path() -> Path:
    return log_dir() / "moxaserial.log"


def default_receive_dir() -> Path:
    """Sensible default landing zone for programs received from a control."""
    return Path.home() / "Documents" / APP_NAME / "Received"
