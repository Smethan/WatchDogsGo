import json

from watchdogs.wardrive_settings import DEFAULTS, load_settings, save_settings


def test_legacy_settings_keep_existing_values_and_enable_lte_by_default(tmp_path):
    (tmp_path / "wardrive_settings.json").write_text(
        json.dumps({"trail": True, "cell_tracking": False, "lte_modem": "no"}))

    settings = load_settings(tmp_path)

    assert settings["trail"] is True
    assert settings["cell_tracking"] is False
    assert settings["lte_modem"] is True
    assert set(settings) == set(DEFAULTS)


def test_settings_round_trip_lte_off_atomically(tmp_path):
    settings = dict(DEFAULTS, lte_modem=False, cell_tracking=True)
    save_settings(tmp_path, settings)

    assert not (tmp_path / "wardrive_settings.tmp").exists()
    assert load_settings(tmp_path)["lte_modem"] is False
    stored = json.loads((tmp_path / "wardrive_settings.json").read_text())
    assert stored["lte_modem"] is False
    assert set(stored) == set(DEFAULTS)
