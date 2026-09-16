"""MoxaSerial add-in library.

Nothing in ``moxaserial`` outside of ``moxaserial.ui`` may import ``adsk`` at module
scope: the whole engine (config, transports, preprocessing, sender,
receiver, logging) must stay importable and testable in a plain CPython
interpreter so it can be exercised without Fusion.
"""

__version__ = "0.1.0"
