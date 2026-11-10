from types import SimpleNamespace
import time

import pytest

from watchdogs.p25_catalog import BUILTIN_SITES
from watchdogs.p25_screen import P25Screen


class FakeSDR:
    def __init__(self):
        self.running = False
        self.mode = ""
        self.p25_quality = {}
        self.started = []
        self.stops = 0
        self.events = []
        self.p25_session_id = 0

    def start_pig_p25(self, *args, **kwargs):
        self.started.append((args, kwargs))
        self.running = True
        self.mode = "pig_p25"
        self.p25_session_id += 1
        return True

    def stop(self):
        self.stops += 1
        self.running = False
        self.mode = ""


class FakeOwner:
    def __init__(self, settings):
        self.settings = settings
        self.persisted = 0
        self.pig = SimpleNamespace(active=False, owns_sdr=False)

    def persist_settings(self):
        self.persisted += 1


def make_screen(site_index=0, *, loot=None):
    site = BUILTIN_SITES[site_index]
    settings = {
        "p25_custom_sites": [],
        "p25_selected_site_id": site["id"],
        "pig_p25_control_hz": site["control_hz"][0],
        "pig_p25_system": site["system"],
        "pig_gain_db": 0,
        "pig_ppm": 0,
    }
    sdr = FakeSDR()
    app = SimpleNamespace(
        _sdr=sdr,
        _sdr_enabled=True,
        loot=loot,
        messages=[],
        msg=lambda message, duration: app.messages.append(message),
    )
    return P25Screen(app, FakeOwner(settings)), app, site


@pytest.mark.parametrize("target", ["wdg"])
def test_hierarchy_and_scan_start_use_saved_site_controls_and_loot_dir(target):
    loot = SimpleNamespace(session_dir="/tmp/p25-session")
    screen, _, site = make_screen(loot=loot)
    screen._open_picker()
    assert screen._options()
    screen.browse_region = site["region"]
    screen.picker_level = "county"
    assert site["county"] in screen._options()
    screen.browse_county = site["county"]
    screen.picker_level = "site"
    assert any(row["id"] == site["id"] for row in screen._options())

    assert screen._start(scan=True)
    args, kwargs = screen.app._sdr.started[-1]
    assert args[0] == site["control_hz"][0]
    assert kwargs["control_channels"] == tuple(site["control_hz"])
    assert kwargs["loot_dir"] == "/tmp/p25-session"
    assert kwargs["gain"] is None
    assert screen.owns_sdr


@pytest.mark.parametrize("target", ["wdg"])
def test_stop_owned_stops_only_screen_session_and_not_pig_detector(target):
    screen, app, _ = make_screen()
    app._sdr.running = True
    app._sdr.mode = "pig_p25"
    screen.owns_sdr = False
    screen.stop_owned()
    assert app._sdr.stops == 0
    app._sdr.running = False
    app._sdr.mode = ""

    assert screen._start(scan=False)
    screen.stop_owned()
    assert app._sdr.stops == 1
    assert not screen.owns_sdr


def test_finished_screen_session_cannot_stop_new_pig_session():
    screen, app, _ = make_screen()
    assert screen._start(scan=False)
    old_session = screen.owned_session_id
    app._sdr.stop()
    assert app._sdr.start_pig_p25(856_212_500)
    screen.owner.pig.active = True
    screen.owner.pig.owns_sdr = True
    assert app._sdr.p25_session_id != old_session
    assert screen._owner_kind() == "pig"
    stops = app._sdr.stops
    screen.stop_owned()
    assert app._sdr.stops == stops
    app._sdr.stop()
    app._sdr.running = True
    app._sdr.mode = "433"
    screen.record_error("unrelated SDR profile error")
    assert "unrelated SDR profile error" not in "\n".join(screen.log)


def test_running_session_keeps_selected_site_and_frequency_unchanged():
    screen, app, original = make_screen()
    assert screen._start(scan=True)
    other = BUILTIN_SITES[1]
    assert screen._save_selection(other, other["control_hz"][0]) is False
    assert screen.owner.settings["p25_selected_site_id"] == original["id"]
    assert screen.owner.settings["pig_p25_control_hz"] == original["control_hz"][0]
    assert (
        screen.apply_input("add_site", ("Texas", "Denton", "New", "856.2125")) is False
    )
    assert not screen.owner.settings["p25_custom_sites"]
    assert app.messages


@pytest.mark.parametrize("target", ["wdg"])
def test_ingest_deduplicates_metadata_and_reports_system_identity_mismatch(target):
    screen, _, site = make_screen()
    screen.ingest(
        {
            "tuned_frequency_hz": site["control_hz"][0],
            "sysid": "1098",
            "wacn": "781824",
            "rfss": "1",
            "site": "6",
            "decoded_blocks": 28,
            "talkgroups": ["1234"],
        }
    )
    first_len = len(screen.log)
    screen.ingest(
        {
            "tuned_frequency_hz": site["control_hz"][0],
            "sysid": "1098",
            "wacn": "781824",
            "rfss": "1",
            "site": "6",
            "decoded_blocks": 28,
            "talkgroups": ["1234"],
        }
    )
    assert len(screen.log) == first_len
    joined = "\n".join(screen.log)
    assert "Frequency decoded, but system ID does not match" in joined
    assert "talkgroup 1234" in joined
    assert "audio" not in joined.lower()


@pytest.mark.parametrize("target", ["wdg"])
def test_error_filter_keeps_pending_start_failure_and_ignores_other_modes(target):
    screen, app, _ = make_screen()
    screen.record_error("unrelated 433 error")
    assert not screen.log

    screen.pending_start = True
    screen.pending_start_until = time.monotonic() + 10
    screen.record_error("OP25 failed to start")
    assert "OP25 failed to start" in screen.last_error
    assert not screen.pending_start

    screen.record_error("unrelated after failure")
    assert len(screen.log) == 1


@pytest.mark.parametrize("target", ["wdg"])
def test_identity_match_requires_observed_system_ids(target):
    screen, _, site = make_screen()
    screen.ingest(
        {"sysid": str(int(site["sysid"], 16)), "wacn": str(int(site["wacn"], 16))}
    )
    assert screen._identity_matches(site) is True
    unknown, _, unknown_site = make_screen()
    unknown.ingest({"site": str(unknown_site["site_number"])})
    assert unknown._identity_matches(unknown_site) is None


def test_partial_matching_identity_does_not_claim_a_mismatch():
    screen, _, site = make_screen()
    screen.ingest({"wacn": str(int(site["wacn"], 16)), "site": "0"})
    assert "does not match" not in "\n".join(screen.log)
    assert "matches selected" not in "\n".join(screen.log)


def test_single_conflicting_identifier_waits_for_complete_system_identity():
    screen, _, _ = make_screen()
    screen.ingest({"sysid": "1098", "site": "0"})
    assert "does not match" not in "\n".join(screen.log)
    screen.ingest({"sysid": "1098", "wacn": "781824", "site": "0"})
    assert "does not match" in "\n".join(screen.log)


def test_new_p25_run_logs_the_same_frequency_again():
    screen, app, site = make_screen()
    assert screen._start(scan=False)
    screen.ingest({"tuned_frequency_hz": site["control_hz"][0]})
    app._sdr.stop()
    screen.owns_sdr = False
    assert screen._start(scan=False)
    screen.ingest({"tuned_frequency_hz": site["control_hz"][0]})
    assert "\n".join(screen.log).count("Tuned ") == 2


def test_custom_input_uses_same_frequency_limit_as_saved_settings():
    screen, app, _ = make_screen()
    assert not screen.apply_input("add_site", ("Texas", "Denton", "Too high", "1750"))
    assert not screen.owner.settings["p25_custom_sites"]
    assert any("24-1740 MHz" in message for message in app.messages)
