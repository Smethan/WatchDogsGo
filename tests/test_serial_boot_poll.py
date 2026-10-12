"""USB discovery must keep making progress while the startup screen is shown."""
from unittest.mock import Mock

from watchdogs import app as appmod


def test_boot_screen_polls_serial_without_starting_normal_game_loop(monkeypatch):
    game = appmod.WatchDogsGame.__new__(appmod.WatchDogsGame)
    game._boot_phase = True
    game._boot_frame = 0
    game._boot_has_errors = False
    game._poll_serial = Mock()
    game._poll_gps = Mock()
    monkeypatch.setattr(appmod.pyxel, 'btnp', lambda _key: False)
    game.update()
    game._poll_serial.assert_called_once_with()
    game._poll_gps.assert_not_called()
    assert game._boot_frame == 1
