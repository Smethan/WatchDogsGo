"""The in-game daemon updater selects only validated Smethan release tags."""

import time
from pathlib import Path
from queue import Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.app import WatchDogsGame
from watchdogs.meshtastic_service import MeshtasticInstallError


def _game(service):
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._meshtastic_service = service
    game._meshtastic_update_running = False
    game._meshtastic_update_result = Queue()
    game._lora_enabled = True
    game._lora = NS(running=False, worker_active=False, radio_owned=False)
    game._lora_transition_active = Mock(return_value=False)
    game._watch = NS(worker_active=False)
    game.wardrive = NS(
        host_ble=NS(worker_active=False), _meshtastic_action_thread=None)
    game._meshtastic = NS(
        running=False, connected=False, backend_mode="auto", active_backend="",
        service_restore_pending=False,
        ble_scan_lease_active=False, pairing_agent_lease_active=False,
        active_service_target=Mock(return_value="wdg"),
        resume_service=Mock(return_value=True),
        close=Mock(return_value=True), start=Mock(return_value=True),
        wait_connected=Mock(return_value=True))
    game.msg = Mock()
    game._term_add = Mock()
    return game


def _wait_result(game):
    deadline = time.monotonic() + 2
    while game._meshtastic_update_result.empty():
        if time.monotonic() >= deadline:
            raise AssertionError("Meshtastic update worker did not finish")
        time.sleep(0.01)


def test_meshtastic_update_reports_current_validated_release(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg2")),
        install_tag=Mock())
    game = _game(service)

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.install_tag.assert_not_called()
    game.msg.assert_any_call(
        "[MT] Already up to date (v2.8.1-wdg.2)", 11)


def test_meshtastic_update_resolves_retained_service_snapshot_first(
        monkeypatch):
    order = []

    def releases():
        order.append("release_lookup")
        return [{"tag_name": "v2.8.1-wdg.2"}]

    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases", releases)
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg2")),
        install_tag=Mock())
    game = _game(service)
    game._meshtastic.service_restore_pending = True

    def resume(*, timeout, connect):
        order.append(("resume", timeout, connect))
        game._meshtastic.service_restore_pending = False
        return True

    game._meshtastic.resume_service.side_effect = resume

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    assert order == [("resume", 15.0, False), "release_lookup"]
    service.install_tag.assert_not_called()


def test_meshtastic_update_aborts_when_retained_snapshot_cannot_restore(
        monkeypatch):
    releases = Mock(return_value=[{"tag_name": "v2.8.1-wdg.2"}])
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases", releases)
    service = NS(
        require_current=Mock(), status=Mock(), install_tag=Mock())
    game = _game(service)
    game._meshtastic.service_restore_pending = True
    game._meshtastic.resume_service.return_value = False

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    game._meshtastic.resume_service.assert_called_once_with(
        timeout=15.0, connect=False)
    releases.assert_not_called()
    service.require_current.assert_not_called()
    service.install_tag.assert_not_called()
    assert any(
        "update was not started" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_installs_exact_selected_tag(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}))
    game = _game(service)

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.install_tag.assert_called_once_with("v2.8.1-wdg.2")
    assert game._meshtastic_update_running is False


def test_meshtastic_update_disconnects_and_reconnects_existing_client(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        start=Mock())
    game = _game(service)
    game._meshtastic.running = True

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)

    game._meshtastic.close.assert_called_once_with()
    game._meshtastic.start.assert_called_once_with()
    game._meshtastic.wait_connected.assert_called_once_with(
        timeout=15.0, backend="fork_socket")
    service.start.assert_not_called()


def test_meshtastic_update_restores_explicit_legacy_backend(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        select=Mock())
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.backend_mode = "legacy_tcp"
    def close():
        game._meshtastic.running = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)

    service.select.assert_called_once_with("stock")
    game._meshtastic.start.assert_called_once_with()
    game._meshtastic.wait_connected.assert_called_once_with(
        timeout=15.0, backend="legacy_tcp")


def test_meshtastic_update_reports_failed_backend_negotiation(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        rollback=Mock())
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.wait_connected.side_effect = (False, True)

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.rollback.assert_called_once_with()
    assert game._meshtastic.start.call_count == 2
    assert game._meshtastic.wait_connected.call_count == 2
    assert any(
        "rolled back" in call.args[0]
        and "previous backend was restored" in call.args[0]
        for call in game.msg.call_args_list)
    assert not any(
        "service ready" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_reports_failed_rollback_backend_recovery(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        rollback=Mock())
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.wait_connected.side_effect = (False, False)

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.rollback.assert_called_once_with()
    assert any(
        "package was rolled back" in call.args[0]
        and "previous backend did not recover" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_restart_failure_rolls_back_then_verifies_old_backend(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        rollback=Mock())
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.start.side_effect = (False, True)

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.rollback.assert_called_once_with()
    assert game._meshtastic.start.call_count == 2
    game._meshtastic.wait_connected.assert_called_once_with(
        timeout=15.0, backend="fork_socket")
    assert any(
        "rolled back" in call.args[0]
        and "previous backend was restored" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_thread_start_failure_releases_transition():
    service = NS(require_current=Mock(), status=Mock(), install_tag=Mock())
    game = _game(service)
    game._meshtastic_update_thread_factory = lambda **_kwargs: NS(
        start=Mock(side_effect=RuntimeError("thread unavailable")))

    assert not game._start_meshtastic_update()

    assert game._meshtastic_update_running is False
    assert not game._meshtastic_transition_busy()
    service.require_current.assert_not_called()
    assert any(
        "Could not start update worker" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_aborts_before_install_when_client_will_not_stop(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock())
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.close.return_value = False

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.install_tag.assert_not_called()
    game._meshtastic.start.assert_not_called()
    assert any(
        "worker did not stop" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_does_not_restart_after_unconfirmed_install_rollback(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(side_effect=MeshtasticInstallError(
            "install and rollback failed", rollback_restored=False,
            backup=Path("/var/backups/meshtasticd-wdg/evidence"))),
    )
    game = _game(service)
    game._meshtastic.running = True

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    game._meshtastic.start.assert_not_called()
    assert any(
        "rollback was not confirmed" in call.args[0]
        and "evidence" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_reconnects_only_after_confirmed_install_rollback(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(side_effect=MeshtasticInstallError(
            "candidate rejected", rollback_restored=True, backup=None)),
    )
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.active_backend = "legacy_tcp"
    game._meshtastic.active_service_target.return_value = "stock"

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    game._meshtastic.start.assert_called_once_with()
    game._meshtastic.wait_connected.assert_called_once_with(
        timeout=15.0, backend="legacy_tcp")
    assert any(
        "previous backend was restored" in call.args[0]
        for call in game.msg.call_args_list)


def test_meshtastic_update_blocks_rollback_until_updated_client_stops(
        monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        rollback=Mock(),
    )
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.close.side_effect = (True, False)
    game._meshtastic.wait_connected.return_value = False

    assert game._start_meshtastic_update()
    _wait_result(game)
    game._poll_meshtastic_update()

    service.rollback.assert_not_called()
    assert any(
        "rollback is blocked" in call.args[0]
        and "transaction was retained" in call.args[0]
        for call in game.msg.call_args_list)


def test_auto_backend_rollback_reconnects_actual_legacy_backend(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.meshtastic_updates.meshtastic_releases",
        lambda: [{"tag_name": "v2.8.1-wdg.2"}])
    service = NS(
        require_current=Mock(),
        status=Mock(return_value=NS(package_version="2.8.1+wdg1")),
        install_tag=Mock(return_value={"package_version": "2.8.1+wdg2"}),
        rollback=Mock(),
    )
    game = _game(service)
    game._meshtastic.running = True
    game._meshtastic.backend_mode = "auto"
    game._meshtastic.active_backend = "legacy_tcp"
    game._meshtastic.active_service_target.return_value = "stock"
    game._meshtastic.wait_connected.side_effect = (False, True)

    def close():
        game._meshtastic.running = False
        game._meshtastic.connected = False
        return True

    game._meshtastic.close.side_effect = close

    assert game._start_meshtastic_update()
    _wait_result(game)

    service.rollback.assert_called_once_with()
    assert game._meshtastic.wait_connected.call_args_list[0].kwargs == {
        "timeout": 15.0, "backend": "fork_socket"}
    assert game._meshtastic.wait_connected.call_args_list[1].kwargs == {
        "timeout": 15.0, "backend": "legacy_tcp"}


def test_meshtastic_update_refuses_direct_radio_or_handoff():
    service = NS(require_current=Mock(), status=Mock(), install_tag=Mock())
    game = _game(service)
    game._lora.radio_owned = True

    assert not game._start_meshtastic_update()
    assert any("Stop MeshCore" in call.args[0]
               for call in game.msg.call_args_list)
    service.status.assert_not_called()

    game._lora.radio_owned = False
    game._lora_transition_active.return_value = True
    assert not game._start_meshtastic_update()
    assert any("radio handoff" in call.args[0]
               for call in game.msg.call_args_list)


def test_meshtastic_update_requires_setup_helper():
    game = _game(None)

    assert not game._start_meshtastic_update()
    assert game._meshtastic_update_running is False
    assert any(
        "Rerun setup.sh" in call.args[0]
        for call in game.msg.call_args_list)
