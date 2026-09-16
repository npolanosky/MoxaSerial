"""Logging: rotating file handler + in-memory ring buffer for the UI Log page.

The ring buffer is what the palette's Log tab renders; the rotating file
is what the user opens when they need to send something to support. Every
record at WARNING or above is also republished on the event bus so the UI
can raise a toast without polling.
"""

from __future__ import annotations

import logging
import logging.handlers
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from moxaserial import paths
from moxaserial.events import EventBus

LOGGER_NAME = "moxaserial"
RING_CAPACITY = 2000
_MAX_BYTES = 1_000_000
_BACKUPS = 3

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


class RingBufferHandler(logging.Handler):
    """Keeps the last N records in memory as plain dicts for the UI."""

    def __init__(self, capacity: int = RING_CAPACITY) -> None:
        super().__init__()
        self._buf: deque[dict[str, Any]] = deque(maxlen=capacity)
        self._lock = threading.RLock()
        self._seq = 0

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = record.getMessage()
        except Exception:  # pragma: no cover - defensive
            msg = str(record.msg)
        with self._lock:
            self._seq += 1
            self._buf.append(
                {
                    "seq": self._seq,
                    "ts": record.created,
                    "time": time.strftime("%H:%M:%S", time.localtime(record.created)),
                    "level": record.levelname,
                    "source": record.name.split(".")[-1],
                    "message": msg,
                }
            )

    def records(
        self,
        min_level: str = "DEBUG",
        text: str = "",
        limit: int = 0,
        since_seq: int = 0,
    ) -> list[dict[str, Any]]:
        """Filtered snapshot, oldest first."""
        threshold = logging.getLevelName(min_level.upper())
        if not isinstance(threshold, int):
            threshold = logging.DEBUG
        needle = text.lower().strip()
        with self._lock:
            items = list(self._buf)
        out = []
        for r in items:
            if r["seq"] <= since_seq:
                continue
            lvl = logging.getLevelName(r["level"])
            if isinstance(lvl, int) and lvl < threshold:
                continue
            if needle and needle not in r["message"].lower():
                continue
            out.append(r)
        if limit and len(out) > limit:
            out = out[-limit:]
        return out

    def clear(self) -> None:
        with self._lock:
            self._buf.clear()

    def counts(self) -> dict[str, int]:
        with self._lock:
            items = list(self._buf)
        counts = dict.fromkeys(LEVELS, 0)
        for r in items:
            if r["level"] in counts:
                counts[r["level"]] += 1
        return counts


class _BusHandler(logging.Handler):
    """Re-publishes records onto the event bus so the UI updates live."""

    def __init__(self, bus: EventBus, ring: RingBufferHandler) -> None:
        super().__init__()
        self._bus = bus
        self._ring = ring

    def emit(self, record: logging.LogRecord) -> None:
        try:
            entries = self._ring.records(limit=1)
            entry = entries[-1] if entries else None
            if entry is None:
                return
            self._bus.publish("log.entry", dict(entry))
        except Exception:  # pragma: no cover - never let logging break the app
            pass


class LogManager:
    """Owns the logger, the ring buffer and the rotating file handler."""

    _instance: LogManager | None = None

    def __init__(self, bus: EventBus | None = None, log_file: Path | None = None) -> None:
        self.bus = bus
        self.ring = RingBufferHandler()
        self._logger = logging.getLogger(LOGGER_NAME)
        self._logger.propagate = False
        self._logger.setLevel(logging.DEBUG)
        self._file_handler: logging.Handler | None = None
        self._log_file = log_file
        self._configure(log_file)

    # -- construction ----------------------------------------------------
    @classmethod
    def instance(cls, bus: EventBus | None = None) -> LogManager:
        if cls._instance is None:
            cls._instance = cls(bus=bus)
        elif bus is not None and cls._instance.bus is None:
            cls._instance.attach_bus(bus)
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Test hook - drop the singleton and detach handlers."""
        if cls._instance is not None:
            cls._instance.shutdown()
        cls._instance = None

    def _configure(self, log_file: Path | None) -> None:
        for h in list(self._logger.handlers):
            self._logger.removeHandler(h)
        self._logger.addHandler(self.ring)

        target = log_file
        if target is None:
            try:
                paths.log_dir().mkdir(parents=True, exist_ok=True)
                target = paths.log_path()
            except Exception:
                target = None
        if target is not None:
            try:
                fh = logging.handlers.RotatingFileHandler(
                    str(target), maxBytes=_MAX_BYTES, backupCount=_BACKUPS, encoding="utf-8"
                )
                fh.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(levelname)-8s %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S",
                    )
                )
                self._logger.addHandler(fh)
                self._file_handler = fh
                self._log_file = target
            except Exception:  # pragma: no cover - read-only volume etc.
                self._file_handler = None

        if self.bus is not None:
            self._logger.addHandler(_BusHandler(self.bus, self.ring))

    def attach_bus(self, bus: EventBus) -> None:
        self.bus = bus
        self._logger.addHandler(_BusHandler(bus, self.ring))

    # -- api -------------------------------------------------------------
    def get(self, name: str = "") -> logging.Logger:
        return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)

    def set_level(self, level: str) -> None:
        lvl = logging.getLevelName(str(level).upper())
        if isinstance(lvl, int):
            self._logger.setLevel(lvl)

    @property
    def level(self) -> str:
        return logging.getLevelName(self._logger.level)

    @property
    def log_file(self) -> str:
        return str(self._log_file) if self._log_file else ""

    def clear(self) -> None:
        self.ring.clear()

    def shutdown(self) -> None:
        for h in list(self._logger.handlers):
            try:
                h.close()
            except Exception:
                pass
            self._logger.removeHandler(h)


def get_logger(name: str = "") -> logging.Logger:
    """Convenience accessor used across the library."""
    return logging.getLogger(f"{LOGGER_NAME}.{name}" if name else LOGGER_NAME)
