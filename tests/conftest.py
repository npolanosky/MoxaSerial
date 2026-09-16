"""Shared fixtures. Keeps every test off the real user settings and log."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def isolated_app_data(tmp_path, monkeypatch):
    """Redirect settings/logs into a per-test temp dir."""
    data = tmp_path / "appdata"
    data.mkdir()
    monkeypatch.setenv("MOXASERIAL_DATA_DIR", str(data))
    # discovery: never let a test reach the real macOS keychain or the
    # Windows Credential Manager - the file backend lands inside tmp_path.
    monkeypatch.setenv("MOXASERIAL_SECRET_BACKEND", "file")
    from moxaserial.log import LogManager

    LogManager.reset()
    yield data
    LogManager.reset()


@pytest.fixture
def bus():
    from moxaserial.events import EventBus

    return EventBus()


@pytest.fixture
def collector(bus):
    """Records every event published on the bus."""

    class Collector:
        def __init__(self) -> None:
            self.events = []
            bus.subscribe("*", self.events.append)

        def topics(self) -> list[str]:
            return [e.topic for e in self.events]

        def of(self, topic: str) -> list[dict]:
            return [e.payload for e in self.events if e.topic == topic]

        def last(self, topic: str) -> dict | None:
            found = self.of(topic)
            return found[-1] if found else None

        def states(self, topic: str = "send.state") -> list[str]:
            return [p.get("state") for p in self.of(topic)]

    return Collector()


@pytest.fixture
def machine():
    from moxaserial.config import simulator_machine

    m = simulator_machine()
    m["send"]["wait_for_ready"] = "immediate"
    m["send"]["start_chars"] = ""
    m["send"]["end_chars"] = ""
    return m


@pytest.fixture
def fake_transport():
    from moxaserial.transport.fake import FakeProfile, FakeTransport

    def _make(**profile_kwargs):
        profile = FakeProfile(**profile_kwargs)
        return FakeTransport(profile=profile)

    return _make


def wait_until(predicate, timeout: float = 5.0, interval: float = 0.01) -> bool:
    """Poll *predicate* until true or *timeout*. Returns the final result."""
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


@pytest.fixture
def nc_file(tmp_path):
    def _make(text: str, name: str = "prog.nc") -> str:
        path = tmp_path / name
        path.write_text(text, encoding="ascii")
        return str(path)

    return _make


os.environ.setdefault("MOXASERIAL_DATA_DIR", "/tmp/moxaserial-tests")
