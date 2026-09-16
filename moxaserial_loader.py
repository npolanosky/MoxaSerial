"""UNC-safe module loader for the MoxaSerial add-in.

Python's default import machinery breaks on Windows UNC paths
(``\\\\server\\share\\...``) because the standard path finders do not
recognise them, and Fusion add-ins are frequently installed on network
shares. This module provides :func:`load`, which imports any project
module by dotted name using explicit file paths via
``importlib.util.spec_from_file_location``, bypassing ``sys.path``
resolution entirely.

Usage from anywhere in the project::

    import moxaserial_loader as loader
    cfg = loader.load("moxaserial.config")
    store = cfg.ConfigStore()

Adapted from the SetupSheets add-in loader.
"""

from __future__ import annotations

import importlib.util
import os
import sys

# Root of the add-in (directory containing MoxaSerial.py / moxaserial_loader.py).
ROOT = os.path.dirname(os.path.abspath(__file__))

# Dotted prefixes owned by this add-in; purge() clears these from sys.modules.
_OWNED = ("moxaserial",)


def load(dotted_name: str):
    """Import and return the module identified by *dotted_name*.

    Examples: ``"moxaserial.config"``, ``"moxaserial.ui.palette"``, ``"moxaserial"``.

    Modules are cached in ``sys.modules`` so later calls (and ordinary
    ``import`` statements inside already-loaded modules) reuse them.
    """
    if dotted_name in sys.modules:
        return sys.modules[dotted_name]

    parts = dotted_name.split(".")
    for i in range(1, len(parts)):
        parent = ".".join(parts[:i])
        if parent not in sys.modules:
            _load_single(parent)

    return _load_single(dotted_name)


def purge() -> None:
    """Drop all add-in modules from ``sys.modules``.

    Called once at add-in startup so that a stop -> start cycle picks up
    edited code instead of stale cached modules (Fusion keeps a single
    interpreter alive for the life of the application).
    """
    stale = [
        k
        for k in sys.modules
        if k in _OWNED or any(k.startswith(p + ".") for p in _OWNED)
    ]
    for k in stale:
        del sys.modules[k]


def ensure_root_on_path() -> None:
    """Put the add-in root on ``sys.path`` (harmless on local installs)."""
    if ROOT not in sys.path:
        sys.path.insert(0, ROOT)


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


def _load_single(dotted_name: str):
    """Load exactly one module / package by explicit file path."""
    rel_path = dotted_name.replace(".", os.sep)
    pkg_init = os.path.join(ROOT, rel_path, "__init__.py")
    mod_file = os.path.join(ROOT, rel_path + ".py")

    if os.path.isfile(pkg_init):
        spec = importlib.util.spec_from_file_location(
            dotted_name,
            pkg_init,
            submodule_search_locations=[os.path.join(ROOT, rel_path)],
        )
    elif os.path.isfile(mod_file):
        spec = importlib.util.spec_from_file_location(dotted_name, mod_file)
    else:
        raise ModuleNotFoundError(
            f"MoxaSerial loader: cannot find '{dotted_name}' "
            f"(tried {pkg_init} and {mod_file})"
        )

    if spec is None or spec.loader is None:
        raise ImportError(f"MoxaSerial loader: spec creation failed for '{dotted_name}'")

    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod  # register before exec so circular refs resolve
    spec.loader.exec_module(mod)
    return mod
