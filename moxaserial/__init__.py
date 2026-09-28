"""MoxaSerial add-in library.

Nothing in ``moxaserial`` outside of ``moxaserial.ui`` may import ``adsk`` at module
scope: the whole engine (config, transports, preprocessing, sender,
receiver, logging) must stay importable and testable in a plain CPython
interpreter so it can be exercised without Fusion.
"""



def _manifest_version(fallback: str = "0.0.0") -> str:
    """The version Fusion shows: ``MoxaSerial.manifest`` next to this package.

    One number, one place (``tools/bump_version.py`` edits the manifest); the
    About page, the export envelope and the updater's User-Agent all follow
    it. The fallback only matters for a package copied out of the add-in.
    """
    import json
    import os

    try:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "MoxaSerial.manifest"), encoding="utf-8") as fh:
            value = json.load(fh).get("version")
        return str(value).strip() if value else fallback
    except (OSError, ValueError):
        return fallback


__version__ = _manifest_version()
