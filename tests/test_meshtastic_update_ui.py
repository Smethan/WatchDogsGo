"""The app must never mutate the Meshtastic/broker package stack."""

from types import SimpleNamespace as NS
from unittest.mock import Mock

from watchdogs.app import C_DIM, WatchDogsGame


def test_in_app_update_directs_operator_to_transactional_setup():
    game = WatchDogsGame.__new__(WatchDogsGame)
    game._meshtastic_service = NS(install_tag=Mock())
    game.msg = Mock()
    game._term_add = Mock()

    assert game._start_meshtastic_update() is False

    game.msg.assert_called_once_with(
        "[MT] Run sudo bash setup.sh to update the radio stack", C_DIM)
    game._term_add.assert_called_once_with(
        "[MT] In-app Meshtastic installation/adoption was removed; "
        "setup.sh owns the transactional stack update",
        raw=True,
    )
    game._meshtastic_service.install_tag.assert_not_called()
