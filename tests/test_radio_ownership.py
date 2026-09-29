"""The WDG protocol layer owns broker leases, never SPI/GPIO locks."""

from unittest.mock import Mock

from watchdogs.lora_manager import LoRaManager
from watchdogs.sx1262_client import BrokerError


class Controller:
    def __init__(self, events, *, fail=False):
        self.events = events
        self.fail = fail

    def activate_mode(self, mode):
        self.events.append(("activate", mode))
        if self.fail:
            raise BrokerError("manager unavailable")
        return {"target_mode": mode}

    def release_mode(self):
        self.events.append(("release",))
        return {}


class Radio:
    def __init__(self, events):
        self.events = events

    def acknowledge_revoke(self):
        self.events.append(("quiesced",))
        return True

    def close(self):
        self.events.append(("close",))


def test_session_uses_manager_lease_and_never_legacy_owners(monkeypatch):
    events = []
    ownership = Mock()
    service_handoff = Mock()
    manager = LoRaManager(
        sx1262_controller=Controller(events),
        radio_ownership=ownership,
        service_handoff=service_handoff,
    )
    radio = Radio(events)
    monkeypatch.setattr(
        manager,
        "_init_radio",
        lambda: events.append(("protocol_connect",)) or radio,
    )

    assert manager._open_radio_session() is radio
    assert manager.radio_owned
    manager._cleanup_radio(radio)

    assert events == [
        ("activate", "meshcore"),
        ("protocol_connect",),
        ("release",),
        ("quiesced",),
        ("close",),
    ]
    ownership.assert_not_called()
    service_handoff.assert_not_called()
    assert not manager.radio_owned


def test_manager_failure_has_no_direct_hardware_fallback(monkeypatch):
    events = []
    manager = LoRaManager(
        sx1262_controller=Controller(events, fail=True),
        radio_ownership=Mock(),
        service_handoff=Mock(),
    )
    init = Mock()
    monkeypatch.setattr(manager, "_init_radio", init)

    assert manager._open_radio_session() is None

    init.assert_not_called()
    text, level = manager.queue.get_nowait()
    assert level == "error"
    assert "manager unavailable" in text
    assert not manager.radio_owned
