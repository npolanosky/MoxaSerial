"""Ring buffer, level filtering and the bus republish used by the Log page."""

from __future__ import annotations

import logging

from moxaserial.log import LogManager, RingBufferHandler, get_logger


def test_ring_buffer_keeps_records_in_order():
    ring = RingBufferHandler(capacity=10)
    log = logging.getLogger("ringtest.order")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    for i in range(4):
        log.info("message %d", i)

    records = ring.records()
    assert [r["message"] for r in records] == [f"message {i}" for i in range(4)]
    assert [r["seq"] for r in records] == [1, 2, 3, 4]


def test_ring_buffer_evicts_the_oldest_past_capacity():
    ring = RingBufferHandler(capacity=3)
    log = logging.getLogger("ringtest.evict")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    for i in range(6):
        log.info("m%d", i)

    assert [r["message"] for r in ring.records()] == ["m3", "m4", "m5"]


def test_ring_buffer_level_and_text_filters():
    ring = RingBufferHandler()
    log = logging.getLogger("ringtest.filter")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    log.debug("quiet detail")
    log.info("sending program")
    log.warning("XOFF held too long")
    log.error("cable fault")

    assert len(ring.records()) == 4
    assert len(ring.records(min_level="WARNING")) == 2
    assert len(ring.records(min_level="ERROR")) == 1
    assert [r["message"] for r in ring.records(text="cable")] == ["cable fault"]
    assert ring.records(text="CABLE")  # case-insensitive


def test_ring_buffer_since_seq_returns_only_new_records():
    ring = RingBufferHandler()
    log = logging.getLogger("ringtest.since")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    log.info("first")
    seq = ring.records()[-1]["seq"]
    log.info("second")
    assert [r["message"] for r in ring.records(since_seq=seq)] == ["second"]


def test_ring_buffer_limit_returns_the_tail():
    ring = RingBufferHandler()
    log = logging.getLogger("ringtest.limit")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    for i in range(10):
        log.info("m%d", i)

    assert [r["message"] for r in ring.records(limit=2)] == ["m8", "m9"]


def test_ring_buffer_counts_by_level():
    ring = RingBufferHandler()
    log = logging.getLogger("ringtest.counts")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)

    log.info("a")
    log.warning("b")
    log.warning("c")
    log.error("d")

    counts = ring.counts()
    assert counts["INFO"] == 1
    assert counts["WARNING"] == 2
    assert counts["ERROR"] == 1


def test_ring_buffer_clear():
    ring = RingBufferHandler()
    log = logging.getLogger("ringtest.clear")
    log.handlers = [ring]
    log.propagate = False
    log.setLevel(logging.DEBUG)
    log.info("x")
    ring.clear()
    assert ring.records() == []


# -- LogManager -------------------------------------------------------------

def test_manager_writes_a_rotating_file(tmp_path):
    LogManager.reset()
    manager = LogManager(log_file=tmp_path / "moxa.log")
    manager.get("unit").info("hello file")
    assert "hello file" in (tmp_path / "moxa.log").read_text()
    assert manager.log_file.endswith("moxa.log")
    manager.shutdown()


def test_manager_republishes_onto_the_bus(tmp_path, bus, collector):
    LogManager.reset()
    manager = LogManager(bus=bus, log_file=tmp_path / "moxa.log")
    manager.get("unit").warning("a problem")
    entries = collector.of("log.entry")
    assert entries and entries[-1]["message"] == "a problem"
    assert entries[-1]["level"] == "WARNING"
    assert entries[-1]["source"] == "unit"
    manager.shutdown()


def test_manager_level_gates_what_is_recorded(tmp_path):
    LogManager.reset()
    manager = LogManager(log_file=tmp_path / "moxa.log")
    manager.set_level("WARNING")
    assert manager.level == "WARNING"
    log = manager.get("unit")
    log.info("should not be recorded")
    log.warning("should be recorded")
    messages = [r["message"] for r in manager.ring.records()]
    assert "should not be recorded" not in messages
    assert "should be recorded" in messages
    manager.shutdown()


def test_manager_instance_is_a_singleton():
    LogManager.reset()
    first = LogManager.instance()
    assert LogManager.instance() is first
    LogManager.reset()
    assert LogManager.instance() is not first


def test_manager_clear_empties_the_view_only(tmp_path):
    LogManager.reset()
    manager = LogManager(log_file=tmp_path / "moxa.log")
    manager.get("unit").info("kept on disk")
    manager.clear()
    assert manager.ring.records() == []
    assert "kept on disk" in (tmp_path / "moxa.log").read_text()
    manager.shutdown()


def test_get_logger_namespaces_under_the_app_logger():
    assert get_logger("sender").name == "moxaserial.sender"
    assert get_logger().name == "moxaserial"
