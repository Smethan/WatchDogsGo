"""Interactive, metadata-only P25 control-channel monitor."""

from collections import deque
from datetime import datetime
import time
import uuid

from .p25_catalog import all_sites, find_site
from .wardrive_settings import normalize_settings


class P25Screen:
    """Own P25 starts initiated here; observe, but never steal, other owners."""

    LOG_LIMIT = 64
    LOG_VISIBLE = 10

    def __init__(self, app, owner):
        self.app = app
        self.owner = owner
        self.open = False
        self.picker = False
        self.picker_level = "region"
        self.picker_selection = 0
        self.browse_region = ""
        self.browse_county = ""
        self.browse_site_id = ""
        self.owns_sdr = False
        self.owned_session_id = None
        self.pending_start = False
        self.pending_start_until = 0.0
        self.scan_mode = False
        self.last_error = ""
        self.log = deque(maxlen=self.LOG_LIMIT)
        self._log_keys = deque(maxlen=128)
        self._log_key_set = set()
        self._last_blocks = None
        self._logged_talkgroups = set()
        self._last_block_milestone = 0
        self._observed_sysid = ""
        self._observed_wacn = ""
        self._observed_rfss = ""
        self._observed_site = ""

    def show(self):
        self.open = True
        self.picker = False
        selected = self._selected_site()
        if selected:
            self.browse_region = selected["region"]
            self.browse_county = selected["county"]
            self.browse_site_id = selected["id"]

    def _sites(self):
        return list(all_sites(self.owner.settings))

    def _selected_site(self):
        return find_site(
            self.owner.settings,
            self.owner.settings.get("p25_selected_site_id", ""),
        )

    def _selected_frequency(self):
        try:
            return int(self.owner.settings.get("pig_p25_control_hz", 0))
        except (TypeError, ValueError):
            return 0

    def _site_for_frequency(self, frequency_hz):
        for site in self._sites():
            if frequency_hz in site.get("control_hz", ()):
                return site
        return None

    def _running_mode(self):
        sdr = getattr(self.app, "_sdr", None)
        if sdr is not None and getattr(sdr, "running", False):
            return str(getattr(sdr, "mode", "") or "SDR")
        return ""

    def _owner_kind(self):
        if not self._running_mode():
            return ""
        pig = getattr(self.owner, "pig", None)
        if (
            pig is not None
            and getattr(pig, "active", False)
            and getattr(pig, "owns_sdr", False)
            and getattr(self.app._sdr, "mode", "") == "pig_p25"
        ):
            return "pig"
        if self._owns_current_session():
            return "screen"
        return "other"

    def _owns_current_session(self):
        sdr = getattr(self.app, "_sdr", None)
        return bool(
            self.owns_sdr
            and sdr is not None
            and self.owned_session_id is not None
            and getattr(sdr, "p25_session_id", None) == self.owned_session_id
            and getattr(sdr, "mode", "") == "pig_p25"
        )

    def _current_path(self):
        site = self._selected_site()
        if not site:
            return "Custom / saved frequency"
        return f"{site['region']} / {site['county']} / {site['site']}"

    @staticmethod
    def _mhz(frequency_hz):
        if not frequency_hz:
            return "--"
        return f"{frequency_hz / 1_000_000:.5f}".rstrip("0").rstrip(".")

    def _append_log(self, text, *, key=None):
        value = str(text).replace("\n", " ").strip()[:112]
        if not value:
            return
        if key is not None:
            if key in self._log_key_set:
                return
            if len(self._log_keys) == self._log_keys.maxlen:
                self._log_key_set.discard(self._log_keys[0])
            self._log_keys.append(key)
            self._log_key_set.add(key)
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log.append(f"{stamp}  {value}")

    def record_error(self, message):
        if self.pending_start and time.monotonic() > self.pending_start_until:
            self.pending_start = False
        owns_ended_session = (
            self.owns_sdr
            and self.owned_session_id is not None
            and getattr(getattr(self.app, "_sdr", None), "p25_session_id", None)
            == self.owned_session_id
            and not self._running_mode()
        )
        if not (
            self.pending_start
            or self._running_mode() == "pig_p25"
            or owns_ended_session
        ):
            return
        self.last_error = str(message).replace("\n", " ")[:160]
        self._append_log("ERROR: " + self.last_error, key=("error", self.last_error))
        self.pending_start = False
        if owns_ended_session:
            self.owns_sdr = False
            self.owned_session_id = None

    @staticmethod
    def _format_hex(value):
        if value in (None, "", 0, "0", "-1"):
            return ""
        try:
            number = int(str(value), 0)
        except ValueError:
            try:
                number = int(str(value), 10)
            except ValueError:
                return ""
        return f"0x{number:X}"

    def _log_identity_result(self):
        site = self._selected_site()
        if not site:
            return
        expected_sysid = self._format_hex(site.get("sysid"))
        expected_wacn = self._format_hex(site.get("wacn"))
        if not expected_sysid and not expected_wacn:
            return
        observed = (
            self._observed_sysid,
            self._observed_wacn,
            self._observed_rfss,
            self._observed_site,
        )
        expected = (
            expected_sysid,
            expected_wacn,
            str(site.get("rfss") or ""),
            str(site.get("site_number") or ""),
        )
        if not any(observed):
            return
        comparable = [
            (got, want) for got, want in zip(observed, expected) if got and want
        ]
        matches = all(got == want for got, want in comparable)
        has_expected_system_ids = bool(expected_sysid and expected_wacn)
        has_observed_system_ids = bool(self._observed_sysid and self._observed_wacn)
        if matches and has_expected_system_ids and has_observed_system_ids:
            label = "Decoded frequency matches selected system IDs"
            key = ("identity-match", site["id"], expected)
        else:
            label = (
                "Frequency decoded, but system ID does not match selected site "
                f"(observed SYS {observed[0] or '?'} / WACN {observed[1] or '?'} / "
                f"RFSS {observed[2] or '?'} / site {observed[3] or '?'})"
            )
            key = ("identity-mismatch", site["id"], observed)
        self._append_log(label, key=key)

    def ingest(self, data):
        """Add meaningful metadata changes, deduplicating periodic status polls."""
        if not isinstance(data, dict):
            return
        frequency = data.get("tuned_frequency_hz") or data.get("frequency_hz")
        try:
            frequency = int(frequency) if frequency is not None else 0
        except (TypeError, ValueError):
            frequency = 0
        site = str(data.get("site") or "")
        rfss = str(data.get("rfss") or "")
        system = str(data.get("sysid") or data.get("system_id") or "")
        wacn = str(data.get("wacn") or "")
        self._observed_sysid = self._format_hex(system)
        self._observed_wacn = self._format_hex(wacn)
        self._observed_rfss = rfss
        self._observed_site = site
        blocks = data.get("decoded_blocks")
        try:
            blocks = int(blocks) if blocks is not None else None
        except (TypeError, ValueError):
            blocks = None

        if frequency:
            tune_key = ("tune", frequency)
            self._append_log(f"Tuned {self._mhz(frequency)} MHz", key=tune_key)
        if system or site:
            site_key = ("site", system, rfss, site)
            if system or site not in ("0", "-1"):
                label = " / ".join(
                    part
                    for part in (
                        f"SYS {self._observed_sysid}" if self._observed_sysid else "",
                        f"WACN {self._observed_wacn}" if self._observed_wacn else "",
                        f"RFSS {rfss}" if rfss not in ("-1", "0") else "",
                        f"Site {site}" if site not in ("-1", "0") else "",
                    )
                    if part
                )
                if label:
                    self._append_log("Control data: " + label, key=site_key)
                    self._log_identity_result()

        if blocks is not None:
            if self._last_blocks is None or blocks < self._last_blocks:
                if self._last_blocks is not None and blocks < self._last_blocks:
                    self._append_log(
                        f"Control block count reset at {self._mhz(frequency)} MHz; searching",
                        key=("reset", frequency, blocks, int(time.monotonic() // 10)),
                    )
                self._last_blocks = blocks
                self._last_block_milestone = (blocks // 25) * 25
            elif blocks > self._last_blocks:
                milestone = (blocks // 25) * 25
                if milestone > self._last_block_milestone:
                    self._last_block_milestone = milestone
                    self._append_log(
                        f"Decoded control blocks: {blocks} at {self._mhz(frequency)} MHz",
                        key=("blocks", frequency, milestone),
                    )
                self._last_blocks = blocks

        talkgroups = data.get("talkgroups", ())
        if not isinstance(talkgroups, (list, tuple, set)):
            talkgroups = (talkgroups,)
        for talkgroup in talkgroups:
            value = str(talkgroup).strip()
            if value and value not in self._logged_talkgroups:
                self._logged_talkgroups.add(value)
                if len(self._logged_talkgroups) > 512:
                    self._logged_talkgroups.clear()
                    self._logged_talkgroups.add(value)
                self._append_log(
                    f"Control data listed talkgroup {value}", key=("tg", value)
                )

    def _begin_input(self, action, fields):
        self.app.input_mode = True
        self.app.input_fields = [{"label": label, "value": ""} for label in fields]
        self.app.input_field_idx = 0
        self.app._input_pending_cat = -16
        self.app._input_pending_item = action

    def apply_input(self, action, values):
        """Persist a custom site or add a frequency to a custom site."""
        if self._running_mode():
            self.app.msg("[P25] Stop the SDR before changing control channels", 8)
            return False
        settings = dict(self.owner.settings)
        custom = [dict(site) for site in settings.get("p25_custom_sites", ())]
        try:
            if action == "add_site":
                if len(values) < 4:
                    raise ValueError("Enter region, county, site, and MHz")
                region, county, site_name = (str(value).strip() for value in values[:3])
                frequency = int(round(float(values[3]) * 1_000_000))
                if not region or not county or not site_name:
                    raise ValueError("Region, county, and site are required")
                if not 24_000_000 <= frequency <= 1_740_000_000:
                    raise ValueError("Control frequency must be 24-1740 MHz")
                identity = "custom:" + uuid.uuid4().hex[:16]
                custom.append(
                    {
                        "id": identity,
                        "region": region,
                        "county": county,
                        "site": site_name,
                        "system": site_name,
                        "control_hz": [frequency],
                    }
                )
                settings["p25_selected_site_id"] = identity
                settings["pig_p25_control_hz"] = frequency
                settings["pig_p25_system"] = site_name
            elif action == "add_frequency":
                selected = self._selected_site()
                if not selected or not str(selected.get("id", "")).startswith(
                    "custom:"
                ):
                    raise ValueError("Select a custom site before adding a frequency")
                if not values:
                    raise ValueError("Enter a frequency in MHz")
                frequency = int(round(float(values[0]) * 1_000_000))
                if not 24_000_000 <= frequency <= 1_740_000_000:
                    raise ValueError("Control frequency must be 24-1740 MHz")
                target = next(
                    (row for row in custom if row.get("id") == selected.get("id")), None
                )
                if target is None:
                    raise ValueError("The custom site is no longer saved")
                channels = list(target.get("control_hz", ()))
                if frequency in channels:
                    raise ValueError("That frequency is already saved")
                channels.append(frequency)
                target["control_hz"] = channels[:16]
            else:
                return False
        except (TypeError, ValueError, IndexError) as exc:
            self.app.msg("[P25] " + str(exc)[:100], 8)
            return False

        settings["p25_custom_sites"] = custom
        normalized = normalize_settings(settings)
        if normalized.get("p25_custom_sites") != custom:
            self.app.msg("[P25] Custom site was rejected by settings validation", 8)
            return False
        self.owner.settings.update(normalized)
        self.owner.persist_settings()
        self._append_log("Saved custom P25 control channel")
        return True

    def _delete_selected_custom_site(self, site_id=None):
        selected = (
            find_site(self.owner.settings, site_id)
            if site_id
            else self._selected_site()
        )
        if not selected or not str(selected.get("id", "")).startswith("custom:"):
            self.app.msg("[P25] Only saved custom sites can be deleted", 8)
            return
        if self._owner_kind():
            self.app.msg("[P25] Stop listening before deleting a site", 8)
            return
        identity = selected["id"]
        self.owner.settings["p25_custom_sites"] = [
            site
            for site in self.owner.settings.get("p25_custom_sites", ())
            if site.get("id") != identity
        ]
        self.owner.settings["p25_selected_site_id"] = ""
        self.owner.persist_settings()
        self.app.msg("[P25] Custom site deleted", 11)
        self._append_log("Deleted saved custom site")

    def _save_selection(self, site, frequency_hz):
        if self._running_mode():
            self.app.msg("[P25] Stop the SDR before changing control channels", 8)
            return False
        settings = dict(self.owner.settings)
        settings["p25_selected_site_id"] = site["id"]
        settings["pig_p25_control_hz"] = int(frequency_hz)
        settings["pig_p25_system"] = str(site.get("system") or site["site"])[:32]
        normalized = normalize_settings(settings)
        self.owner.settings.update(normalized)
        self.owner.persist_settings()
        self._append_log(f"Selected {site['site']} / {self._mhz(frequency_hz)} MHz")
        return True

    def _options(self):
        sites = self._sites()
        if self.picker_level == "region":
            return sorted({site["region"] for site in sites}, key=str.casefold)
        if self.picker_level == "county":
            return sorted(
                {
                    site["county"]
                    for site in sites
                    if site["region"] == self.browse_region
                },
                key=str.casefold,
            )
        if self.picker_level == "site":
            return [
                site
                for site in sites
                if site["region"] == self.browse_region
                and site["county"] == self.browse_county
            ]
        selected = find_site(self.owner.settings, self.browse_site_id)
        return list(selected.get("control_hz", ())) if selected else []

    def _open_picker(self):
        self.picker = True
        self.picker_level = "region"
        self.picker_selection = 0

    def _picker_back(self):
        if self.picker_level == "frequency":
            self.picker_level = "site"
            self.picker_selection = 0
        elif self.picker_level == "site":
            self.picker_level = "county"
            self.picker_selection = 0
        elif self.picker_level == "county":
            self.picker_level = "region"
            self.picker_selection = 0
        else:
            self.picker = False

    def _picker_enter(self):
        options = self._options()
        if not options:
            return
        selected = options[min(self.picker_selection, len(options) - 1)]
        if self.picker_level == "region":
            self.browse_region = selected
            self.picker_level = "county"
        elif self.picker_level == "county":
            self.browse_county = selected
            self.picker_level = "site"
        elif self.picker_level == "site":
            self.browse_site_id = selected["id"]
            self.picker_level = "frequency"
        else:
            site = find_site(self.owner.settings, self.browse_site_id)
            if site:
                if self._save_selection(site, selected):
                    self.picker = False
        self.picker_selection = 0

    def update(self):
        import pyxel as px

        if self.picker:
            options = self._options()
            if px.btnp(px.KEY_ESCAPE):
                self._picker_back()
                return
            if px.btnp(px.KEY_TAB):
                self._picker_back()
                return
            if px.btnp(px.KEY_UP):
                self.picker_selection = max(0, self.picker_selection - 1)
            if px.btnp(px.KEY_DOWN):
                self.picker_selection = min(
                    max(0, len(options) - 1), self.picker_selection + 1
                )
            if px.btnp(px.KEY_RETURN):
                self._picker_enter()
            if px.btnp(px.KEY_A):
                selected = find_site(self.owner.settings, self.browse_site_id)
                if (
                    self.picker_level == "frequency"
                    and selected
                    and str(selected.get("id", "")).startswith("custom:")
                ):
                    self._begin_input("add_frequency", ("Control MHz",))
                elif self.picker_level in ("region", "county"):
                    self._begin_input(
                        "add_site", ("Region", "County", "Site", "Control MHz")
                    )
                return
            if px.btnp(px.KEY_D):
                site = find_site(self.owner.settings, self.browse_site_id)
                if self.picker_level in ("site", "frequency") and site:
                    self._delete_selected_custom_site(site["id"])
            return

        if px.btnp(px.KEY_ESCAPE) or px.btnp(px.KEY_TAB):
            self.stop_owned()
            self.open = False
            return
        if px.btnp(px.KEY_F):
            self._open_picker()
            return
        if px.btnp(px.KEY_A):
            self._begin_input("add_site", ("Region", "County", "Site", "Control MHz"))
            return
        if px.btnp(px.KEY_D):
            self._delete_selected_custom_site()
            return
        if px.btnp(px.KEY_S):
            if self.owns_sdr:
                self.stop_owned()
            elif self._owner_kind():
                self.app.msg("[P25] Another mode owns the tuner", 8)
            return
        if px.btnp(px.KEY_RETURN):
            self._start(scan=False)
            return
        if px.btnp(px.KEY_H):
            self._start(scan=True)

    def _start(self, *, scan):
        if self._owner_kind():
            self.app.msg(
                "[P25] The SDR is already owned; stop its current mode first", 8
            )
            return False
        if not getattr(self.app, "_sdr_enabled", False):
            self.app.msg("[P25] SDR power is off (SYSTEM > SDR)", 8)
            return False
        site = self._selected_site()
        frequency = self._selected_frequency()
        if site is None or frequency not in site.get("control_hz", ()):
            site = self._site_for_frequency(frequency)
        if not frequency or site is None:
            self.app.msg("[P25] Select a saved control frequency first", 8)
            self._open_picker()
            return False
        sdr = getattr(self.app, "_sdr", None)
        if sdr is None:
            self.app.msg("[P25] SDR manager is unavailable", 8)
            return False
        channels = tuple(site.get("control_hz", ())) if scan else None
        loot = getattr(self.app, "loot", None)
        loot_dir = getattr(loot, "session_dir", "") if loot is not None else ""
        started = sdr.start_pig_p25(
            frequency,
            str(site.get("system") or site["site"]),
            gain=self.owner.settings.get("pig_gain_db") or None,
            ppm=self.owner.settings.get("pig_ppm", 0),
            loot_dir=str(loot_dir or ""),
            control_channels=channels,
        )
        if started:
            self.owns_sdr = True
            self.owned_session_id = getattr(sdr, "p25_session_id", None)
            self.pending_start = False
            self.pending_start_until = 0.0
            self.scan_mode = bool(scan)
            self.last_error = ""
            self._last_blocks = None
            self._logged_talkgroups.clear()
            self._append_log(
                ("Scanning site " if scan else "Listening on ")
                + f"{site['site']} / {self._mhz(frequency)} MHz"
            )
            return True
        self.pending_start = True
        self.pending_start_until = time.monotonic() + 10.0
        self.app.msg("[P25] Could not start the control-channel monitor", 8)
        return False

    def _identity_matches(self, site):
        if not site:
            return None
        expected = (
            self._format_hex(site.get("sysid")),
            self._format_hex(site.get("wacn")),
            str(site.get("rfss") or ""),
            str(site.get("site_number") or ""),
        )
        observed = (
            self._observed_sysid,
            self._observed_wacn,
            self._observed_rfss,
            self._observed_site,
        )
        comparable = [
            (got, want) for got, want in zip(observed, expected) if got and want
        ]
        if not (observed[0] and observed[1] and expected[0] and expected[1]):
            return None
        return all(got == want for got, want in comparable)

    def stop_owned(self):
        """Stop only a P25 session this screen successfully started."""
        sdr = getattr(self.app, "_sdr", None)
        if self._owns_current_session() and getattr(sdr, "running", False):
            sdr.stop()
            self._append_log("Stopped P25 monitoring")
        self.owns_sdr = False
        self.owned_session_id = None
        self.pending_start = False
        self.pending_start_until = 0.0
        self.scan_mode = False

    def _status(self):
        sdr = getattr(self.app, "_sdr", None)
        if not getattr(self.app, "_sdr_enabled", False):
            return "SDR POWER OFF", None, {}
        mode = self._running_mode()
        if mode == "pig_p25":
            quality = getattr(sdr, "p25_quality", {}) or {}
            tuned = quality.get("tuned_frequency_hz")
            if self._owner_kind() == "pig":
                prefix = "LISTENING / PIG DETECTOR"
            elif self.scan_mode or quality.get("scanning"):
                prefix = "SEARCHING SITE"
            else:
                prefix = "LISTENING"
            return prefix, tuned, quality
        if mode:
            return f"SDR BUSY: {mode}", None, {}
        if self.last_error:
            return "ERROR", None, {}
        return "STOPPED", None, {}

    def draw(self):
        import pyxel as px

        if self.picker:
            self._draw_picker(px)
            return
        state, tuned, quality = self._status()
        site = self._selected_site()
        frequency = self._selected_frequency()
        px.cls(0)
        px.rect(0, 0, 640, 18, 9)
        px.text(8, 5, "P25 RADIO / CONTROL CHANNEL MONITOR", 0)
        px.text(
            8,
            25,
            "Status: " + state,
            11 if state.startswith(("LISTENING", "SEARCHING")) else 8,
        )
        px.text(8, 41, "Selected: " + self._current_path()[:80], 7)
        px.text(
            8,
            56,
            f"Control: {self._mhz(frequency)} MHz  "
            f"Tuned: {self._mhz(tuned)} MHz  "
            f"Blocks: {quality.get('decoded_blocks', 0)}",
            13,
        )
        if self.scan_mode and site:
            px.text(
                8,
                71,
                "Site scan: "
                + ", ".join(self._mhz(value) for value in site.get("control_hz", ()))[
                    :95
                ],
                13,
            )
        decoded_hz = quality.get("recent_decoded_frequency_hz")
        if decoded_hz:
            decoded_site = self._site_for_frequency(decoded_hz)
            matches = self._identity_matches(decoded_site)
            note = "decoded on frequency"
            if decoded_site and matches is False:
                note += " - system identity mismatch"
            elif decoded_site and matches is True:
                note += " - site identity matches"
            activity_y = 82 if self.scan_mode else 77
            px.text(
                8,
                activity_y,
                f"Activity: {self._mhz(decoded_hz)} MHz ({note})"[:100],
                8 if matches is False else 11,
            )
        px.line(8, 87, 631, 87, 5)
        px.text(8, 93, "RECENT CONTROL DATA / METADATA ONLY", 3)
        rows = list(self.log)[-self.LOG_VISIBLE :]
        for index, line in enumerate(rows):
            px.text(8, 111 + index * 17, line[:104], 7)
        if not rows:
            px.text(8, 114, "No control-channel events received yet.", 13)
        if self.last_error:
            px.text(8, 290, ("Last error: " + self.last_error)[:100], 8)
        px.text(
            8,
            309,
            "Control channel metadata only. No audio or transcript is produced.",
            13,
        )
        px.line(8, 323, 631, 323, 5)
        px.text(
            8,
            331,
            "[ENTER] Listen  [H] Scan site  [F] Browse  [A] Add  [D] Delete  [S] Stop",
            3,
        )
        px.text(8, 345, "[ESC/TAB] Back (stops this screen's receiver)", 3)

    def _draw_picker(self, px):
        options = self._options()
        levels = {
            "region": "REGION",
            "county": "COUNTY",
            "site": "SITE",
            "frequency": "CONTROL FREQUENCY",
        }
        path = " / ".join(
            part
            for part in (
                self.browse_region,
                self.browse_county
                if self.picker_level in ("site", "frequency")
                else "",
                (find_site(self.owner.settings, self.browse_site_id) or {}).get(
                    "site", ""
                )
                if self.picker_level == "frequency"
                else "",
            )
            if part
        )
        px.cls(0)
        px.rect(0, 0, 640, 18, 9)
        px.text(8, 5, "P25 CONTROL CHANNEL / BROWSE", 0)
        px.text(8, 25, "Folder: " + (path or "All regions")[:90], 7)
        px.text(8, 41, "Choose " + levels[self.picker_level], 11)
        offset = max(0, self.picker_selection - 11)
        for index, item in enumerate(options[offset : offset + 16]):
            actual = index + offset
            y = 63 + index * 14
            selected = actual == self.picker_selection
            if selected:
                px.rect(5, y - 2, 630, 13, 1)
            if self.picker_level == "site":
                label = item["site"]
                suffix = str(len(item.get("control_hz", ()))) + " control freqs"
            elif self.picker_level == "frequency":
                label = self._mhz(item) + " MHz"
                suffix = "Ctrl"
            else:
                label = str(item)
                suffix = ""
            px.text(12, y, label[:46], 11 if selected else 7)
            if suffix:
                px.text(390, y, suffix, 13)
        if not options:
            px.text(14, 72, "No saved entries in this folder.", 13)
        px.text(8, 331, "[UP/DOWN] Move  [ENTER] Open/select  [A] Add  [D] Delete", 3)
        px.text(8, 345, "[ESC/TAB] Parent folder / return", 3)
