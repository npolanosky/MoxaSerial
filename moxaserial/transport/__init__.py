"""Transport implementations and the factory that picks one per machine."""

from __future__ import annotations

from typing import Any

from moxaserial.transport.base import (
    ModemStatus,
    Transport,
    TransportError,
    TransportNotOpen,
    TransportTimeout,
)


def create_transport(machine: dict[str, Any], bus=None) -> Transport:
    """Return the transport appropriate for *machine*.

    ``type == "simulator"`` -> :class:`moxaserial.transport.fake.FakeTransport`
    ``type == "moxa"``      -> :class:`moxaserial.transport.moxa.MoxaTransport`
    ``type == "serial"``    -> :class:`moxaserial.transport.serial_port.SerialTransport`
    """
    kind = str(machine.get("type", "moxa")).lower()
    if kind == "simulator":
        from moxaserial.transport.fake import FakeProfile, FakeTransport

        sim_cfg = machine.get("simulator") or {}
        profile = FakeProfile(realtime=bool(sim_cfg.get("realtime", False)))
        return FakeTransport(bus=bus, profile=profile)
    # --- direct serial ports ---------------------------------
    if kind == "serial":
        from moxaserial.transport.serial_port import SerialTransport

        return SerialTransport(bus=bus)
    # --- end direct serial ports ---------------------------------------------
    from moxaserial.transport.moxa import MoxaTransport

    return MoxaTransport(bus=bus)


__all__ = [
    "ModemStatus",
    "Transport",
    "TransportError",
    "TransportNotOpen",
    "TransportTimeout",
    "create_transport",
]
