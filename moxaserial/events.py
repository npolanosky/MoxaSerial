"""Tiny thread-safe publish/subscribe event bus.

Engines (sender, receiver, transports, logger) publish; the UI bridge
subscribes. Subscribers must be cheap and must not raise - the bus
swallows subscriber exceptions so one bad listener cannot kill an engine
thread mid-send.

Topics are dotted strings. A subscriber may register for an exact topic,
for a prefix using a trailing ``*`` (``"send.*"``), or for everything
(``"*"``).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Handler = Callable[["Event"], None]


@dataclass(frozen=True)
class Event:
    """A single published event."""

    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"topic": self.topic, "payload": self.payload, "ts": self.ts}


class EventBus:
    """Thread-safe pub/sub. Publishing is synchronous on the caller's thread."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._subs: list[tuple[str, Handler]] = []
        self._error_hook: Callable[[str, BaseException], None] | None = None

    # -- subscription ----------------------------------------------------
    def subscribe(self, pattern: str, handler: Handler) -> Callable[[], None]:
        """Register *handler* for *pattern*; returns an unsubscribe callable."""
        with self._lock:
            self._subs.append((pattern, handler))

        def _unsub() -> None:
            self.unsubscribe(pattern, handler)

        return _unsub

    def unsubscribe(self, pattern: str, handler: Handler) -> None:
        with self._lock:
            self._subs = [s for s in self._subs if s != (pattern, handler)]

    def clear(self) -> None:
        with self._lock:
            self._subs.clear()

    def set_error_hook(self, hook: Callable[[str, BaseException], None] | None) -> None:
        """Called when a subscriber raises. Must never raise itself."""
        self._error_hook = hook

    # -- publishing ------------------------------------------------------
    def publish(self, topic: str, payload: dict[str, Any] | None = None) -> Event:
        evt = Event(topic=topic, payload=dict(payload or {}))
        with self._lock:
            targets = [h for pat, h in self._subs if _matches(pat, topic)]
        for handler in targets:
            try:
                handler(evt)
            except BaseException as exc:  # noqa: BLE001 - a listener must never kill the engine
                hook = self._error_hook
                if hook is not None:
                    try:
                        hook(topic, exc)
                    except BaseException:
                        pass
        return evt

    # -- introspection ---------------------------------------------------
    @property
    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)


def _matches(pattern: str, topic: str) -> bool:
    if pattern == "*":
        return True
    if pattern.endswith("*"):
        return topic.startswith(pattern[:-1])
    return pattern == topic


# A process-wide default bus. Tests construct their own EventBus instead.
_DEFAULT = EventBus()


def default_bus() -> EventBus:
    """The shared bus used by the add-in at runtime."""
    return _DEFAULT
