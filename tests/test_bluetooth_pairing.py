"""Shared BlueZ pairing-agent ownership tests."""

from unittest.mock import Mock

from watchdogs.bluetooth_pairing import BluetoothPairingCoordinator


def test_named_pairing_owner_is_exclusive_and_renewable():
    acquire = Mock(return_value=True)
    release = Mock(return_value=True)
    clock = Mock(return_value=10.0)
    coordinator = BluetoothPairingCoordinator(
        acquire, release, clock=clock)

    assert coordinator.acquire("meshcore", 120)
    assert coordinator.owner == "meshcore"
    assert coordinator.remaining == 120
    assert not coordinator.acquire("watch", 120)

    clock.return_value = 20.0
    assert coordinator.acquire("meshcore", 60)
    assert coordinator.remaining == 60
    assert acquire.call_args_list[0].args == (120,)
    assert acquire.call_args_list[1].args == (60,)

    assert not coordinator.release("watch")
    assert coordinator.release("meshcore")
    assert coordinator.owner == ""
    release.assert_called_once_with()


def test_failed_daemon_grant_never_exposes_local_owner():
    coordinator = BluetoothPairingCoordinator(
        Mock(return_value=False), Mock(return_value=True))

    assert not coordinator.acquire("meshcore", 120)
    assert not coordinator.active
    assert not coordinator.release_uncertain


def test_uncertain_release_blocks_every_new_pairing_owner():
    release = Mock(return_value=False)
    coordinator = BluetoothPairingCoordinator(
        Mock(return_value=True), release)

    assert coordinator.acquire("meshcore", 120)
    assert not coordinator.release("meshcore")
    assert coordinator.owner == "meshcore"
    assert coordinator.release_uncertain
    assert not coordinator.acquire("meshcore", 120)
    assert not coordinator.acquire("watch", 120)


def test_coordinator_without_daemon_callbacks_still_serializes_agents():
    coordinator = BluetoothPairingCoordinator()

    assert coordinator.acquire("watch", 30)
    assert not coordinator.acquire("meshcore", 30)
    assert coordinator.release("watch")
    assert coordinator.acquire("meshcore", 30)
