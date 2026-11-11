from types import SimpleNamespace

from watchdogs.aio_manager import AioManager, _pinctrl_get


def test_pinctrl_get_reads_high_and_low_levels(monkeypatch):
    outputs = iter(
        (
            "GPIO 7: level=1 func=OUTPUT pull=NONE",
            "GPIO 7: level=0 func=OUTPUT pull=NONE",
        )
    )
    monkeypatch.setattr(
        "watchdogs.aio_manager.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout=next(outputs)),
    )
    assert _pinctrl_get(7) is True
    assert _pinctrl_get(7) is False


def test_pinctrl_get_reports_unknown_for_failed_or_unrecognized_read(monkeypatch):
    monkeypatch.setattr(
        "watchdogs.aio_manager.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
    )
    assert _pinctrl_get(7) is None
    monkeypatch.setattr(
        "watchdogs.aio_manager.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="unexpected"),
    )
    assert _pinctrl_get(7) is None


def test_get_status_keeps_other_rail_values_when_sdr_is_unknown(monkeypatch):
    readings = iter((True, False, None, True))
    monkeypatch.setattr(
        "watchdogs.aio_manager.shutil.which", lambda _: "/usr/bin/pinctrl"
    )
    monkeypatch.setattr(
        "watchdogs.aio_manager._pinctrl_get", lambda pin: next(readings)
    )
    assert AioManager.get_status() == {
        "gps": True,
        "lora": False,
        "sdr": None,
        "usb": True,
    }


def test_get_status_can_sample_only_sdr(monkeypatch):
    seen = []
    monkeypatch.setattr("watchdogs.aio_manager.shutil.which", lambda _: "/usr/bin/pinctrl")
    monkeypatch.setattr("watchdogs.aio_manager._pinctrl_get",
                        lambda pin: seen.append(pin) or True)
    assert AioManager.get_status(("sdr",)) == {"sdr": True}
    assert seen == [7]
