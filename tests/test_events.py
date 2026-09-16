"""The pub/sub bus that connects engines to the UI bridge."""

from __future__ import annotations

import threading

from moxaserial.events import EventBus


def test_exact_topic_delivery():
    bus = EventBus()
    seen = []
    bus.subscribe("send.progress", seen.append)
    bus.publish("send.progress", {"n": 1})
    bus.publish("send.state", {"n": 2})
    assert [e.payload["n"] for e in seen] == [1]


def test_prefix_and_wildcard_patterns():
    bus = EventBus()
    prefix, everything = [], []
    bus.subscribe("send.*", prefix.append)
    bus.subscribe("*", everything.append)
    bus.publish("send.progress", {})
    bus.publish("receive.progress", {})
    assert [e.topic for e in prefix] == ["send.progress"]
    assert len(everything) == 2


def test_unsubscribe_via_the_returned_callable():
    bus = EventBus()
    seen = []
    unsub = bus.subscribe("x", seen.append)
    bus.publish("x")
    unsub()
    bus.publish("x")
    assert len(seen) == 1
    assert bus.subscriber_count == 0


def test_a_raising_subscriber_cannot_break_the_publisher():
    bus = EventBus()
    errors = []
    bus.set_error_hook(lambda topic, exc: errors.append((topic, str(exc))))
    survivors = []
    bus.subscribe("x", lambda e: (_ for _ in ()).throw(RuntimeError("bad listener")))
    bus.subscribe("x", survivors.append)

    bus.publish("x", {"ok": True})  # must not raise

    assert len(survivors) == 1
    assert errors and errors[0][0] == "x"


def test_payload_is_copied_not_aliased():
    bus = EventBus()
    seen = []
    bus.subscribe("x", seen.append)
    payload = {"value": 1}
    bus.publish("x", payload)
    payload["value"] = 2
    assert seen[0].payload["value"] == 1


def test_publish_is_thread_safe():
    bus = EventBus()
    received = []
    lock = threading.Lock()

    def handler(evt):
        with lock:
            received.append(evt.payload["i"])

    bus.subscribe("t", handler)
    threads = [
        threading.Thread(target=lambda i=i: bus.publish("t", {"i": i})) for i in range(50)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(received) == list(range(50))


def test_clear_removes_every_subscriber():
    bus = EventBus()
    bus.subscribe("a", lambda e: None)
    bus.subscribe("b", lambda e: None)
    bus.clear()
    assert bus.subscriber_count == 0
