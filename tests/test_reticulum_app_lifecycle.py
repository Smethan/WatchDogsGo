from queue import Queue
from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.app import WatchDogsGame
from watchdogs.reticulum_config import ReticulumProfile


def direct_manager(mode, events):
    manager = NS(
        running=True, worker_active=False, radio_owned=True, mode=mode,
        queue=Queue())

    def stop():
        events.append("stop:" + mode)
        manager.running = False
        manager.radio_owned = False
        return True

    manager.stop = Mock(side_effect=stop)
    manager.reuse_retained_service_snapshot_once = Mock(
        side_effect=lambda: events.append("reuse:" + mode))
    manager.set_mc_channels = Mock(
        side_effect=lambda _channels: events.append("channels:" + mode))

    def start_meshcore(_region):
        events.append("start:" + mode)
        manager.running = True
        manager.radio_owned = True
        return True

    manager.start_meshcore = Mock(side_effect=start_meshcore)
    return manager


def base_game():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._lora_transition_queue = Queue()
    game._lora_start_pending = ""
    game._reticulum_start_pending = None
    game._direct_protocol_rollback = ""
    game._mc_channels_list = []
    game._mc_region = "us_ca_narrow"
    game._reticulum_profile = ReticulumProfile(confirmed=True)
    game._restore_lora_power_owner = Mock(return_value=True)
    game._term_add = Mock()
    return game


def test_meshcore_to_reticulum_stops_then_reuses_exact_snapshot():
    events = []
    game = base_game()
    game._lora = direct_manager("meshcore", events)
    reticulum = NS(
        running=False, worker_active=False, radio_owned=False,
        reuse_retained_service_snapshot_once=Mock(
            side_effect=lambda: events.append("reuse:reticulum")))

    def start(_profile, action):
        events.append("start:reticulum:" + action)
        reticulum.running = True
        return True

    reticulum.start = Mock(side_effect=start)
    game._reticulum = reticulum

    game._run_direct_handoff("reticulum", "meshcore", "handoff", 4)

    assert events == [
        "stop:meshcore", "reuse:reticulum", "start:reticulum:handoff"]
    assert game._reticulum_start_pending == ("handoff", 4)
    assert game._direct_protocol_rollback == "meshcore"


def test_reticulum_to_meshcore_stops_then_reuses_exact_snapshot():
    events = []
    game = base_game()
    reticulum = direct_manager("reticulum", events)
    reticulum.start = Mock()
    game._reticulum = reticulum
    lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        set_mc_channels=Mock(
            side_effect=lambda _channels: events.append("channels:meshcore")),
        reuse_retained_service_snapshot_once=Mock(
            side_effect=lambda: events.append("reuse:meshcore")))

    def start_meshcore(_region):
        events.append("start:meshcore")
        lora.running = True
        lora.mode = "meshcore"
        return True

    lora.start_meshcore = Mock(side_effect=start_meshcore)
    game._lora = lora

    game._run_direct_handoff("meshcore", "reticulum", "handoff", 7)

    assert events == [
        "stop:reticulum", "reuse:meshcore", "channels:meshcore",
        "start:meshcore"]
    assert game._lora_start_pending == ("handoff", 7)
    assert game._direct_protocol_rollback == "reticulum"


def test_failed_new_direct_backend_restores_previous_owner():
    events = []
    game = base_game()
    game._lora = direct_manager("meshcore", events)
    game._reticulum = NS(
        running=False, worker_active=False, radio_owned=False,
        reuse_retained_service_snapshot_once=Mock(),
        start=Mock(return_value=False))

    game._run_direct_handoff("reticulum", "meshcore", "handoff", 3)

    assert events == [
        "stop:meshcore", "reuse:meshcore", "channels:meshcore",
        "start:meshcore"]
    game._restore_lora_power_owner.assert_not_called()
    ok, detail, action, epoch = game._lora_transition_queue.get_nowait()
    assert not ok
    assert "previous direct radio restored" in detail
    assert (action, epoch) == ("direct_handoff", 3)


def test_failed_meshcore_candidate_restarts_reticulum_with_exact_profile():
    events = []
    game = base_game()
    previous = ReticulumProfile(
        confirmed=True, display_name="exact", frequency_hz=915_500_000)
    reticulum = direct_manager("reticulum", events)
    reticulum.profile = previous

    def start_reticulum(profile, action):
        events.append(("start:reticulum", profile, action))
        reticulum.running = True
        reticulum.radio_owned = True
        return True

    reticulum.start = Mock(side_effect=start_reticulum)
    game._reticulum = reticulum
    game._lora = NS(
        running=False, worker_active=False, radio_owned=False, mode="",
        set_mc_channels=Mock(),
        reuse_retained_service_snapshot_once=Mock(),
        start_meshcore=Mock(return_value=False),
    )

    game._run_direct_handoff("meshcore", "reticulum", "handoff", 9)

    reticulum.reuse_retained_service_snapshot_once.assert_called_once_with()
    reticulum.start.assert_called_once_with(
        previous, action="direct_rollback")
    assert ("stop:reticulum" in events
            and any(item[0] == "start:reticulum"
                    for item in events if isinstance(item, tuple)))


def test_reticulum_preference_with_auto_disabled_does_not_spawn_sidecar():
    game = base_game()
    game.msg = Mock()
    game._term_add = Mock()
    game._meshtastic_update_running = False
    game._lora_power_ownership_uncertain = False
    game._lora = NS(running=False, mode="", stop=Mock())
    game._reticulum = NS(
        running=False, worker_active=False, radio_owned=False,
        start=Mock(), stop=Mock())
    game._meshtastic = NS(close=Mock(return_value=True))

    assert game._switch_lora_protocol_locked(
        "reticulum", start_if_enabled=False)
    game._reticulum.start.assert_not_called()
    game._reticulum.stop.assert_not_called()
    game._lora.stop.assert_not_called()
