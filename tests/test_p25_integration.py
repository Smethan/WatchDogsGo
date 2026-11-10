"""P25 Monitor menu and SDR event integration."""

from types import SimpleNamespace
from unittest.mock import Mock

from watchdogs.app import MENU_CATS, WatchDogsGame


def test_sniff_menu_opens_p25_monitor_without_starting_receiver():
    sniff = dict(MENU_CATS)["SNIFF"]
    assert any(item[1] == "P25 Monitor" and item[3] == "_p25_monitor"
               for item in sniff)
    game = WatchDogsGame.__new__(WatchDogsGame)
    screen = SimpleNamespace(show=Mock())
    game.wardrive = SimpleNamespace(p25_screen=screen)
    game._ota_reserved = False

    game._execute_item("_p25_monitor", "_p25_monitor", "P25 Monitor", [])

    screen.show.assert_called_once_with()


def test_p25_monitor_receives_metadata_and_error_without_pig(monkeypatch):
    import watchdogs.app as appmod

    payload = {"profile": "pig_p25", "tuned_frequency_hz": 856_212_500,
               "decoded_blocks": 42, "sysid": "1098"}
    screen = SimpleNamespace(ingest=Mock(), record_error=Mock())
    pig = SimpleNamespace(active=False, ingest_p25=Mock())
    game = WatchDogsGame.__new__(WatchDogsGame)
    game.wardrive = SimpleNamespace(
        pig=pig, p25_screen=screen, record_p25_error=Mock())
    game._sdr = SimpleNamespace(
        running=True, mode="pig_p25",
        poll_events=lambda: [("pig_p25", payload), ("error", "OP25 stopped")])
    game._sync_sdr_map_history = Mock()
    game.msg = Mock()
    game._term_add = Mock()
    monkeypatch.setattr(appmod, "pyxel", SimpleNamespace(frame_count=1))

    game._poll_sdr()

    screen.ingest.assert_called_once_with(payload)
    screen.record_error.assert_called_once_with("OP25 stopped")
    pig.ingest_p25.assert_not_called()
    game._term_add.assert_any_call("[SDR] OP25 stopped", raw=True)
