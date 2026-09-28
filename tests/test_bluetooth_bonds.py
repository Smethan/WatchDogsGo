import json
import os
from types import SimpleNamespace as NS

import pytest

from watchdogs import bluetooth_bonds as bonds

CONTROLLER = "11:22:33:44:55:66"
PHONE = "AA:BB:CC:DD:EE:FF"


def _store(tmp_path, *, clock=lambda: 1234.5):
    directory = tmp_path / ".watchdogs"
    directory.mkdir(mode=0o700)
    directory.chmod(0o700)
    return bonds.BluetoothPhoneBondStore(
        directory / "bluetooth_bonds.json",
        owner_uid=os.geteuid(), owner_gid=os.getegid(), clock=clock)


def test_private_store_round_trip_replaces_one_phone_per_controller(tmp_path):
    store = _store(tmp_path)

    first = store.commit(
        CONTROLLER.lower(), PHONE.lower(), "Pixel", "meshcore")
    assert first.controller == CONTROLLER
    assert first.phone_address == PHONE
    assert first.phone_name == "Pixel"
    assert first.authentication == "random_pin"
    assert first.state == "active"
    assert first.timestamp == 1234.5

    replacement = store.commit(
        CONTROLLER, "10:20:30:40:50:60", "New phone", "meshtastic")
    assert store.get(CONTROLLER) == replacement
    assert len(store.load()) == 1
    assert store.path.stat().st_mode & 0o777 == 0o600
    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    text = store.path.read_text(encoding="utf-8")
    assert "passkey" not in text.lower()
    assert "pin" not in text.lower().replace("random_pin", "")
    assert "key" not in text.lower()


def test_commit_creates_private_store_directory(tmp_path):
    store = bonds.BluetoothPhoneBondStore(
        tmp_path / ".watchdogs" / "bluetooth_bonds.json",
        owner_uid=os.geteuid(), owner_gid=os.getegid())

    store.commit(CONTROLLER, PHONE)

    assert store.path.parent.stat().st_mode & 0o777 == 0o700
    assert store.path.stat().st_mode & 0o777 == 0o600


def test_mark_pending_and_exact_remove_never_remove_another_phone(tmp_path):
    store = _store(tmp_path, clock=iter((1.0, 2.0)).__next__)
    store.commit(CONTROLLER, PHONE, "Pixel", "meshcore")

    pending = store.mark_cleanup_pending(CONTROLLER)
    assert pending.state == "cleanup_pending"
    assert pending.phone_address == PHONE
    assert pending.timestamp == 2.0
    assert not store.remove(CONTROLLER, "00:00:00:00:00:01")
    assert store.get(CONTROLLER) == pending
    assert store.remove(CONTROLLER, PHONE.lower())
    assert store.get(CONTROLLER) is None


def test_pending_record_can_be_created_for_uncertain_cleanup(tmp_path):
    store = _store(tmp_path)
    record = store.mark_cleanup_pending(
        CONTROLLER, PHONE, "Pixel", "meshcore")

    assert record.state == "cleanup_pending"
    assert store.get(CONTROLLER) == record


@pytest.mark.parametrize("mode", [0o644, 0o660, 0o400])
def test_store_rejects_unsafe_file_permissions(tmp_path, mode):
    store = _store(tmp_path)
    store.commit(CONTROLLER, PHONE)
    store.path.chmod(mode)

    with pytest.raises(bonds.UnsafeBluetoothBondStore, match="0600"):
        store.load()


def test_store_hardens_existing_logging_directory_and_rejects_symlinks(
        tmp_path):
    store = _store(tmp_path)
    store.path.parent.chmod(0o755)
    assert store.load() == {}
    assert store.path.parent.stat().st_mode & 0o777 == 0o700

    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    store.path.symlink_to(target)
    with pytest.raises(bonds.UnsafeBluetoothBondStore, match="regular file"):
        store.load()


def test_directory_creation_race_never_follows_or_mutates_symlink(
        tmp_path, monkeypatch):
    outside = tmp_path / "outside"
    outside.mkdir(mode=0o755)
    private = tmp_path / ".watchdogs"
    store = bonds.BluetoothPhoneBondStore(
        private / "bluetooth_bonds.json",
        owner_uid=os.geteuid(), owner_gid=os.getegid())
    def raced_mkdir(path, mode):
        assert path == private
        private.symlink_to(outside, target_is_directory=True)
        raise FileExistsError

    fchown = []
    fchmod = []
    monkeypatch.setattr(bonds.os, "mkdir", raced_mkdir)
    monkeypatch.setattr(
        bonds.os, "fchown", lambda *args: fchown.append(args))
    monkeypatch.setattr(
        bonds.os, "fchmod", lambda *args: fchmod.append(args))

    with pytest.raises(
            bonds.UnsafeBluetoothBondStore, match="not a real directory"):
        store.commit(CONTROLLER, PHONE)
    assert not fchown
    assert not fchmod
    assert outside.stat().st_mode & 0o777 == 0o755


@pytest.mark.parametrize(
    "payload",
    [
        b"not-json",
        json.dumps({"version": 2, "controllers": {}}).encode(),
        json.dumps({
            "version": 1,
            "controllers": {CONTROLLER: {
                "address": PHONE,
                "name": "Pixel",
                "authentication": "just_works",
                "source": "meshcore",
                "state": "active",
                "timestamp": 1,
            }},
        }).encode(),
    ],
)
def test_store_rejects_corrupt_json_and_entries(tmp_path, payload):
    store = _store(tmp_path)
    store.path.write_bytes(payload)
    store.path.chmod(0o600)

    with pytest.raises(bonds.CorruptBluetoothBondStore):
        store.load()


def test_legacy_migration_is_fail_closed_until_exact_bond_is_verified(tmp_path):
    store = _store(tmp_path)
    calls = []

    def verifier(controller, address):
        calls.append((controller, address))
        return {
            "controller": controller,
            "address": address,
            "name": "Verified Pixel",
            "paired": True,
            "bonded": True,
            "trusted": False,
            "path": "/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF",
        }

    assert store.migrate_legacy(
        CONTROLLER.lower(), PHONE.lower(), "Legacy name", verifier) is None
    assert store.get(CONTROLLER) is None
    assert calls == [(CONTROLLER, PHONE)]

    def trusted(controller, address):
        value = verifier(controller, address)
        value["trusted"] = True
        return value

    record = store.migrate_legacy(
        CONTROLLER, PHONE, "Legacy name", trusted)
    assert record.phone_address == PHONE
    assert record.phone_name == "Legacy name"
    assert record.authentication == "random_pin"


def test_verify_bluez_bond_matches_exact_controller_device_and_flags():
    adapter = "/org/bluez/hci7"
    device = adapter + "/dev_AA_BB_CC_DD_EE_FF"
    objects = {
        adapter: {
            "org.bluez.Adapter1": {"Address": CONTROLLER.lower()},
        },
        device: {
            "org.bluez.Device1": {
                "Adapter": adapter,
                "Address": PHONE.lower(),
                "Alias": "Pixel",
                "Paired": True,
                "Bonded": True,
                "Trusted": True,
            },
        },
        "/org/bluez/hci8/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {
                "Address": PHONE,
                "Paired": True,
                "Bonded": True,
                "Trusted": True,
            },
        },
    }

    verified = bonds.verify_bluez_bond(
        CONTROLLER, PHONE, managed_objects=objects)
    assert verified == {
        "controller": CONTROLLER,
        "address": PHONE,
        "name": "Pixel",
        "paired": True,
        "bonded": True,
        "trusted": True,
        "path": device,
    }

    objects[device]["org.bluez.Device1"]["Bonded"] = False
    assert bonds.verify_bluez_bond(
        CONTROLLER, PHONE, managed_objects=objects) is None


def test_verify_bluez_bond_accepts_injected_object_manager():
    adapter = "/org/bluez/hci0"
    manager = NS(GetManagedObjects=lambda: {
        adapter: {"org.bluez.Adapter1": {"Address": CONTROLLER}},
        adapter + "/dev_AA_BB_CC_DD_EE_FF": {
            "org.bluez.Device1": {
                "Address": PHONE,
                "Paired": 1,
                "Bonded": 1,
                "Trusted": 1,
            },
        },
    })

    assert bonds.verify_bluez_bond(
        CONTROLLER, PHONE, object_manager=manager)["address"] == PHONE


def test_default_store_uses_validated_real_sudo_user(monkeypatch, tmp_path):
    account = NS(pw_dir=str(tmp_path), pw_uid=1000, pw_gid=1001)
    monkeypatch.setattr(bonds.os, "geteuid", lambda: 0)
    monkeypatch.setenv("SUDO_USER", "pi")
    monkeypatch.setenv("SUDO_UID", "1000")
    monkeypatch.setattr(bonds.pwd, "getpwnam", lambda user: account)

    store = bonds.BluetoothPhoneBondStore.default()
    assert store.path == tmp_path / ".watchdogs" / "bluetooth_bonds.json"
    assert (store.owner_uid, store.owner_gid) == (1000, 1001)

    monkeypatch.setenv("SUDO_UID", "1002")
    with pytest.raises(bonds.UnsafeBluetoothBondStore, match="does not match"):
        bonds.BluetoothPhoneBondStore.default()


def test_canonical_helpers_reject_non_mac_controller_keys():
    assert bonds.canonical_controller(CONTROLLER.lower()) == CONTROLLER
    assert bonds.canonical_address(PHONE.lower()) == PHONE
    with pytest.raises(bonds.CorruptBluetoothBondStore):
        bonds.canonical_controller("auto")
