"""SDR Manager — ADS-B aircraft tracking + 433 MHz sensor decoding.

Uses RTL-SDR (RTL2832U) on AIO v2 board via dump1090 and rtl_433.
Subprocess + thread + queue pattern (same as serial_manager).
"""

import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import urllib.request
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from queue import Queue
from typing import Callable, Optional

from .sdr_tools import find_dump1090, find_op25_rx, find_rtl_power

log = logging.getLogger(__name__)

# --- ADS-B Constants ---
# dump1090-fa/mutability: --net enables SBS on 30003 by default
# --quiet suppresses interactive output
DUMP1090_ARGS = ["--net", "--quiet"]
# Fallback without --quiet (some builds don't support it)
DUMP1090_ARGS_FALLBACK = ["--net"]

# --- rtl_433 Constants ---
RTL433_CMD = [
    "rtl_433",
    "-f", "433920000",            # 433.92 MHz
    "-F", "json",                 # JSON output to stdout
    "-M", "time:utc",            # UTC timestamps
    "-M", "level",               # include signal level
]

ADSB_TIMEOUT = 60  # remove aircraft not seen for 60s
SENSOR_TIMEOUT = 300  # remove sensors not seen for 5min
PIG_EVENT_LIMIT = 256
PIG_RF_INTERVAL_SECONDS = 1.0
PIG_RF_MIN_HZ = 24_000_000
PIG_RF_MAX_HZ = 1_766_000_000
PIG_RF_MAX_CHANNELS = 64
PIG_RF_THRESHOLD_DB = 8.0
PIG_LOG_LIMIT_BYTES = 4 * 1024 * 1024


@dataclass
class Aircraft:
    """Tracked aircraft from ADS-B."""
    icao: str            # ICAO 24-bit hex address
    callsign: str = ""
    lat: float = 0.0
    lon: float = 0.0
    altitude: int = 0    # feet
    speed: int = 0       # knots
    heading: int = 0     # degrees
    squawk: str = ""
    last_seen: float = 0.0
    rssi: float = 0.0

    @property
    def has_position(self) -> bool:
        return self.lat != 0.0 or self.lon != 0.0


@dataclass
class Sensor433:
    """Decoded 433 MHz sensor from rtl_433."""
    model: str
    sid: str              # unique sensor ID (model + id combo)
    data: dict = field(default_factory=dict)
    lat: float = 0.0     # GPS position when first seen
    lon: float = 0.0
    last_seen: float = 0.0
    count: int = 0        # times seen


class SDRManager:
    """Manage mutually exclusive RTL-SDR collection profiles."""

    def __init__(self):
        self.running = False
        self.mode: str = ""  # "adsb" or "433"; one AIO tuner, never both

        # ADS-B state
        self.aircraft: dict[str, Aircraft] = {}  # icao -> Aircraft
        self.total_aircraft_seen: int = 0

        # 433 MHz state
        self.sensors: dict[str, Sensor433] = {}  # sid -> Sensor433
        self.total_sensors_seen: int = 0

        # Subprocess handles
        self._dump1090_proc: Optional[subprocess.Popen] = None
        self._rtl433_proc: Optional[subprocess.Popen] = None
        self._pig_rf_proc: Optional[subprocess.Popen] = None
        self._pig_p25_proc: Optional[subprocess.Popen] = None
        self._pig_p25_tmpdir: str = ""
        self._pig_p25_url: str = ""
        self._pig_p25_stderr_tail = deque(maxlen=12)
        self._pig_p25_stderr_lock = threading.Lock()
        self._pig_p25_stderr_owner = None
        self._pig_p25_stderr_thread = None
        self._readsb_paused = False
        self._readsb_lease_lock = threading.RLock()
        self._readsb_restore_thread = None
        self._readsb_restore_cancel = threading.Event()
        self._adsb_source = ""
        self._adsb_reader_stop = None
        self._adsb_reader_thread = None

        # Event queue for app.py
        self._events: Queue = Queue()
        self._pig_events = deque(maxlen=PIG_EVENT_LIMIT)
        self._pig_events_lock = threading.Lock()
        self._threads: list[threading.Thread] = []
        self._stop_event = threading.Event()
        self._pig_stop_event = threading.Event()

        # Callbacks
        self.on_new_aircraft: Optional[Callable] = None
        self.on_new_sensor: Optional[Callable] = None

        # Loot file handles
        self._adsb_log: Optional[object] = None
        self._sensor_log: Optional[object] = None
        self._pig_log: Optional[object] = None
        self._pig_baselines: dict[str, float] = {}
        self._pig_channels: list[dict] = []
        self._pig_gain: Optional[float] = None
        self._pig_ppm = 0
        self._pig_channel_state: dict[str, dict] = {}
        self.p25_quality: dict = {}
        # Monotonic identity lets UI owners distinguish successive OP25 runs.
        self.p25_session_id = 0
        self._pig_p25_control_hz = 0
        self._pig_p25_control_channels: tuple[int, ...] = ()
        self._pig_p25_system_label = ""
        self._pig_p25_last_blocks = None
        self._pig_p25_last_block_at = None
        self._pig_p25_last_tuned_hz = None
        self._pig_p25_last_decoded_hz = None
        self._pig_p25_last_decoded_at = None

    @staticmethod
    def has_dump1090() -> bool:
        return find_dump1090() is not None

    @staticmethod
    def has_rtl433() -> bool:
        return shutil.which("rtl_433") is not None

    @staticmethod
    def has_rtl_power() -> bool:
        return find_rtl_power() is not None

    @staticmethod
    def has_op25() -> bool:
        return find_op25_rx() is not None

    def _pig_busy(self) -> bool:
        return bool(self._pig_rf_proc or self._pig_p25_proc)

    def _reject_pig_busy(self, requested: str) -> bool:
        if not self._pig_busy():
            return False
        active = self.mode or "Pig Detector"
        self._events.put(("error", f"{active} is active; stop it before starting {requested}"))
        return True

    def _readsb_state(self) -> str:
        """Return readsb's systemd state, or unsupported/missing when absent."""
        return self._readsb_status()[0]

    def _readsb_status(self) -> tuple[str, str]:
        """Return readsb ActiveState and pending systemd Job description."""
        # Keep callers that replace the legacy state probe in tests able to
        # exercise lease behavior without emulating systemd's full show output.
        state_override = self.__dict__.get("_readsb_state")
        if state_override is not None:
            return state_override(), ""
        if not shutil.which("systemctl"):
            return "unsupported", ""
        result = subprocess.run(
            [
                "systemctl",
                "show",
                "readsb.service",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=Job",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                "cannot query readsb.service" + (": " + detail[:100] if detail else "")
            )
        properties = {}
        for line in (result.stdout or "").splitlines():
            key, separator, value = line.partition("=")
            if separator:
                properties[key.strip()] = value.strip().lower()
        if properties.get("LoadState") == "not-found":
            return "missing", ""
        state = properties.get("ActiveState", "")
        if state not in ("active", "inactive", "failed", "activating", "deactivating"):
            raise RuntimeError(
                "readsb.service state is unknown" + (f" ({state})" if state else "")
            )
        return state, properties.get("Job", "").strip()

    @staticmethod
    def _readsb_has_pending_job(job: str) -> bool:
        value = (job or "").strip().lower()
        if value in ("", "-", "[]"):
            return False
        # systemd's D-Bus Job property is commonly rendered as
        # "(uint32 0, object path /)" when no job exists.
        return not (
            re.search(r"uint32\s+0\b", value)
            and re.search(r"(?:objectpath|object path)\s+['\"]?/['\"]?\s*\)?$", value)
        )

    def _acquire_readsb_lease(self) -> bool:
        """Stop readsb only after ruling out pending transitions and restore races."""
        with self._readsb_lease_lock:
            try:
                state, job = self._readsb_status()
            except Exception as exc:
                self._events.put(("error", f"SDR handoff failed: {str(exc)[:120]}"))
                return False

            pending_job = self._readsb_has_pending_job(job)
            if state in ("activating", "deactivating") or pending_job:
                self._events.put(
                    ("error", "SDR handoff failed: readsb has a pending systemd transition")
                )
                return False
            if state not in ("unsupported", "missing", "inactive", "failed", "active"):
                self._events.put(("error", "SDR handoff failed: readsb state is unknown"))
                return False

            # Prevent a waiting restore worker from starting readsb after the
            # state check. The lease lock serializes this with its systemctl call.
            self._readsb_restore_cancel.set()
            if state in ("unsupported", "missing"):
                self._readsb_paused = False
                return True
            if state in ("inactive", "failed"):
                # A stale lease flag is valid only while systemd confirms the
                # service is fully stopped with no pending job.
                return True

            # A stale paused flag cannot authorize tuner ownership when readsb
            # has come back. Reacquire by stopping the now-active service.
            self._readsb_paused = False
            self._events.put(
                ("status", "Pausing readsb ADS-B feed while WatchDogsGo uses the SDR")
            )
            try:
                result = subprocess.run(
                    ["systemctl", "stop", "readsb.service"],
                    capture_output=True, text=True, timeout=4)
            except subprocess.TimeoutExpired:
                try:
                    state, job = self._readsb_status()
                except Exception:
                    state, job = "unknown", "unknown"
                if state in ("inactive", "failed") and not self._readsb_has_pending_job(job):
                    self._readsb_paused = True
                    self._release_readsb_lease()
                elif state == "deactivating" or self._readsb_has_pending_job(job):
                    self._readsb_paused = True
                    self._release_readsb_lease()
                self._events.put(
                    ("error", "SDR handoff failed: readsb stop timed out; recovery deferred"))
                return False
            except Exception as exc:
                self._events.put(
                    ("error", f"SDR handoff failed stopping readsb: {str(exc)[:100]}"))
                return False
            if result.returncode:
                self._events.put(
                    ("error", f"SDR handoff failed: readsb stop exited {result.returncode}"))
                return False

            self._readsb_paused = True
            try:
                state, job = self._readsb_status()
            except Exception as exc:
                self._events.put(
                    ("error", f"SDR handoff failed verifying readsb: {str(exc)[:100]}"))
                self._release_readsb_lease()
                return False
            if state not in ("inactive", "failed") or self._readsb_has_pending_job(job):
                self._events.put(
                    ("error", "SDR handoff failed: readsb is still transitioning"))
                self._release_readsb_lease()
                return False
            self._events.put(("status", "readsb ADS-B feed paused for SDR use"))
            return True

    def _release_readsb_lease(self) -> bool:
        """Restore readsb only if this manager stopped it for an SDR profile."""
        with self._readsb_lease_lock:
            if not self._readsb_paused:
                return True
            try:
                state, job = self._readsb_status()
            except Exception as exc:
                self._events.put(
                    ("error", f"Could not restore readsb ADS-B feed: {str(exc)[:100]}"))
                self._defer_readsb_restore()
                return True
            if state == "active" and not self._readsb_has_pending_job(job):
                self._readsb_paused = False
                self._events.put(("status", "readsb ADS-B feed restored"))
                return True
            if state in ("activating", "deactivating") or self._readsb_has_pending_job(job):
                self._defer_readsb_restore()
                return True
            if state not in ("inactive", "failed"):
                self._events.put(("error", "Could not restore readsb ADS-B feed: state is unknown"))
                return False
            self._events.put(("status", "Restoring readsb ADS-B feed"))
            try:
                result = subprocess.run(
                    ["systemctl", "start", "readsb.service"],
                    capture_output=True, text=True, timeout=4)
                state, job = self._readsb_status()
            except subprocess.TimeoutExpired:
                self._defer_readsb_restore()
                return True
            except Exception as exc:
                self._events.put(
                    ("error", f"Could not restore readsb ADS-B feed: {str(exc)[:100]}"))
                self._defer_readsb_restore()
                return True
            if result.returncode or state != "active" or self._readsb_has_pending_job(job):
                self._defer_readsb_restore()
                return True
            self._readsb_paused = False
            self._events.put(("status", "readsb ADS-B feed restored"))
            return True

    def _defer_readsb_restore(self):
        """Retry restore off-frame; non-daemon worker holds shutdown up to 95s."""
        thread = self._readsb_restore_thread
        cancel = self._readsb_restore_cancel
        if thread and thread.is_alive() and not cancel.is_set():
            return
        # Each worker owns its own token: scheduling a replacement must not
        # clear the cancellation signal observed by an older live worker.
        cancel = threading.Event()
        self._readsb_restore_cancel = cancel
        self._events.put(("status", "readsb restore pending; waiting for systemd job"))

        def restore():
            deadline = time.monotonic() + 95
            while time.monotonic() < deadline and not cancel.is_set():
                with self._readsb_lease_lock:
                    if cancel.is_set() or not self._readsb_paused:
                        return
                    try:
                        state, job = self._readsb_status()
                        if state == "active" and not self._readsb_has_pending_job(job):
                            self._readsb_paused = False
                            self._events.put(("status", "readsb ADS-B feed restored"))
                            return
                        if (state in ("inactive", "failed")
                                and not self._readsb_has_pending_job(job)):
                            result = subprocess.run(
                                ["systemctl", "start", "readsb.service"],
                                capture_output=True, text=True, timeout=4)
                            if result.returncode != 0:
                                self._events.put(
                                    ("error", "Could not restore readsb ADS-B feed: systemctl start failed"))
                    except Exception:
                        pass
                cancel.wait(1)
            if not cancel.is_set():
                self._events.put(
                    ("error", "Could not restore readsb ADS-B feed after systemd transition"))

        self._readsb_restore_thread = threading.Thread(
            target=restore, daemon=False, name="readsb-restore")
        self._readsb_restore_thread.start()

    def _put_pig_event(self, name: str, payload: dict):
        with self._pig_events_lock:
            self._pig_events.append((name, payload))

    def _open_pig_log(self, loot_dir: str, filename: str):
        if not loot_dir:
            return None
        try:
            os.makedirs(loot_dir, exist_ok=True)
            return open(os.path.join(loot_dir, filename), "a", encoding="utf-8")
        except OSError as exc:
            self._events.put(("error", f"Pig Detector log unavailable: {exc}"))
            return None

    def _write_pig_log(self, event: dict):
        if self._pig_log is None:
            return
        try:
            line = json.dumps(event, separators=(",", ":")) + "\n"
            if self._pig_log.tell() + len(line.encode("utf-8")) <= PIG_LOG_LIMIT_BYTES:
                self._pig_log.write(line)
                self._pig_log.flush()
        except (OSError, ValueError, TypeError):
            pass

    @staticmethod
    def _validated_channels(channels) -> list[dict]:
        """Normalize bounded, user-configured center-frequency watch entries."""
        if not isinstance(channels, (list, tuple)):
            raise ValueError("RF watchlist must be a list of channels")
        if not channels or len(channels) > PIG_RF_MAX_CHANNELS:
            raise ValueError(f"RF watchlist must contain 1-{PIG_RF_MAX_CHANNELS} channels")
        normalized = []
        for index, item in enumerate(channels):
            if not isinstance(item, dict):
                raise ValueError("Each RF watchlist entry must be an object")
            try:
                frequency = int(item.get("frequency_hz", item.get("frequency")))
                bandwidth = int(item.get("bandwidth_hz", item.get("bandwidth", 25000)))
            except (TypeError, ValueError):
                raise ValueError("RF channels require integer frequency_hz and bandwidth_hz") from None
            if not PIG_RF_MIN_HZ <= frequency <= PIG_RF_MAX_HZ:
                raise ValueError(f"RF channel {index + 1} is outside supported RTL-SDR range")
            if not 1_000 <= bandwidth <= 2_400_000:
                raise ValueError(f"RF channel {index + 1} bandwidth must be 1 kHz to 2.4 MHz")
            label = str(item.get("label") or f"{frequency / 1e6:.4f} MHz")[:48]
            normalized.append({
                "frequency_hz": frequency,
                "bandwidth_hz": bandwidth,
                "label": label,
                "key": f"{frequency}:{bandwidth}:{index}",
            })
        return normalized

    def start_pig_watchlist(self, channels, gain=None, ppm=0, loot_dir="") -> bool:
        """Start a bounded rtl_power sweep of configured channels only."""
        try:
            normalized = self._validated_channels(channels)
            correction = int(ppm)
            if not -200 <= correction <= 200:
                raise ValueError("PPM correction must be within -200 to 200")
            configured_gain = None if gain is None else float(gain)
            if configured_gain is not None and not -1 <= configured_gain <= 100:
                raise ValueError("SDR gain must be -1 (automatic) or 0-100 dB")
        except (TypeError, ValueError) as exc:
            self._events.put(("error", f"Invalid RF watchlist: {exc}"))
            return False
        if self.mode or self.running or self._dump1090_proc or self._rtl433_proc:
            active = self.mode or "another SDR profile"
            self._events.put(("error", f"{active} is active; stop it before starting Pig RF watchlist"))
            return False
        executable = find_rtl_power()
        if not executable:
            self._events.put(("error", "rtl_power not installed"))
            return False
        if not self._acquire_readsb_lease():
            return False

        self._stop_event.clear()
        self._pig_stop_event.clear()
        self._pig_channels = normalized
        self._pig_gain = configured_gain
        self._pig_ppm = correction
        self._pig_baselines = {}
        self._pig_channel_state = {
            ch["key"]: {"last_visit": None, "last_visit_epoch": None,
                        "noise_floor_db": None, "threshold_db": None,
                        "power_db": None}
            for ch in normalized
        }
        self._pig_log = self._open_pig_log(loot_dir, "pig_rf.jsonl")
        thread = threading.Thread(
            target=self._pig_rf_worker, args=(executable,), daemon=True,
            name="pig-rf-watchlist")
        self._threads.append(thread)
        self.running = True
        self.mode = "pig_rf"
        thread.start()
        self._events.put(("status", "Pig RF watchlist started (rtl_power)"))
        return True

    def _pig_rf_worker(self, executable: str):
        index = 0
        failures = 0
        while not self._pig_stop_event.is_set():
            channel = self._pig_channels[index % len(self._pig_channels)]
            index += 1
            center = channel["frequency_hz"]
            bandwidth = channel["bandwidth_hz"]
            low = max(PIG_RF_MIN_HZ, center - bandwidth // 2)
            high = min(PIG_RF_MAX_HZ, center + bandwidth // 2)
            step = max(1000, min(100_000, bandwidth // 8))
            command = [executable, "-f", f"{low}:{high}:{step}",
                       "-i", str(PIG_RF_INTERVAL_SECONDS), "-e", "1s",
                       "-p", str(self._pig_ppm)]
            if self._pig_gain is not None and self._pig_gain >= 0:
                command.extend(("-g", str(self._pig_gain)))
            command.append("-")
            proc = None
            try:
                proc = subprocess.Popen(
                    command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, encoding="utf-8", errors="replace",
                    preexec_fn=os.setsid)
                self._pig_rf_proc = proc
                try:
                    output, _ = proc.communicate(timeout=6.0)
                except subprocess.TimeoutExpired:
                    self._terminate_proc(proc)
                    output, _ = proc.communicate(timeout=1.0)
                if proc.returncode not in (0, None):
                    if failures == 0:
                        self._events.put(("error", f"rtl_power exited with status {proc.returncode}; retrying"))
                    failures += 1
                else:
                    failures = 0
                for line in (output or "").splitlines():
                    parsed = self._parse_rtl_power_line(line)
                    if parsed is not None:
                        peak, noise_sample, sample_count = parsed
                        self._record_pig_power(channel, peak, sample_count,
                                               noise_sample=noise_sample)
            except Exception as exc:
                if not self._pig_stop_event.is_set():
                    if failures == 0:
                        self._events.put(("error", f"rtl_power failed: {str(exc)[:160]}; retrying"))
                    failures += 1
            finally:
                if self._pig_rf_proc is proc:
                    self._pig_rf_proc = None
                if proc and proc.poll() is None:
                    self._terminate_proc(proc)
            if failures >= 3:
                self._events.put((
                    "error", "rtl_power failed three times; stopping SDR profile"))
                self.stop()
                return
            if self._pig_stop_event.wait(2.0 if failures else 0.05):
                break

    @staticmethod
    def _parse_rtl_power_line(line: str) -> Optional[tuple[float, float, int]]:
        """Return peak bin, median noise sample, and count from rtl_power CSV."""
        try:
            fields = [part.strip() for part in line.split(",")]
            if len(fields) < 8 or fields[0].startswith("#"):
                return None
            samples = int(fields[5])
            bins = [float(value) for value in fields[6:] if value]
            bins = [value for value in bins if -200.0 < value < 20.0]
            if not bins or samples < 1:
                return None
            bins.sort()
            middle = len(bins) // 2
            noise_sample = (bins[middle] if len(bins) % 2 else
                            (bins[middle - 1] + bins[middle]) / 2)
            return bins[-1], noise_sample, samples
        except (TypeError, ValueError, IndexError):
            return None

    def _record_pig_power(self, channel: dict, power: float, samples: int,
                          *, noise_sample: Optional[float] = None):
        now = time.time()
        key = channel["key"]
        if noise_sample is None:
            noise_sample = power
        previous = self._pig_baselines.get(key)
        threshold = (previous if previous is not None else noise_sample) + PIG_RF_THRESHOLD_DB
        above = power >= threshold
        if previous is None:
            noise = noise_sample
        elif above:
            noise = previous
        else:
            noise = previous * 0.8 + noise_sample * 0.2
        self._pig_baselines[key] = noise
        state = self._pig_channel_state[key]
        previous_visit = state.get("last_visit_epoch")
        revisit = round(now-previous_visit, 1) if previous_visit else None
        state.update({
            "last_visit": datetime.fromtimestamp(now, timezone.utc).isoformat(),
            "last_visit_epoch": now,
            "revisit_seconds": revisit,
            "noise_floor_db": round(noise, 1),
            "threshold_db": round(noise + PIG_RF_THRESHOLD_DB, 1),
            "power_db": round(power, 1),
        })
        event = {
            "source": "rtl_power",
            "profile": "pig_rf",
            "frequency_hz": channel["frequency_hz"],
            "bandwidth_hz": channel["bandwidth_hz"],
            "label": channel["label"],
            "power_db": round(power, 1),
            "noise_floor_db": round(noise, 1),
            "threshold_db": round(noise + PIG_RF_THRESHOLD_DB, 1),
            "above_threshold": bool(above),
            "sample_count": int(samples),
            "last_visit": state["last_visit"],
            "last_visit_epoch": now,
            "revisit_seconds": revisit,
            "evidence": "rf_activity" if above else "noise_measurement",
        }
        self._put_pig_event("pig_rf", event)
        self._write_pig_log(event)

    def _start_pig_p25(
        self,
        control_hz: int,
        control_channels: tuple[int, ...],
        system_label: str,
        gain: Optional[float],
        ppm: int,
        loot_dir: str,
    ) -> bool:
        executable = find_op25_rx()
        if not executable:
            self._events.put(
                (
                    "error",
                    "Pinned OP25 source checkout unavailable; P25 metadata is unavailable",
                )
            )
            return False
        if not control_channels or control_hz not in control_channels:
            self._events.put(("error", "P25 control channel list is empty or invalid"))
            return False
        if system_label and len(system_label) > 48:
            self._events.put(
                ("error", "P25 system label must contain at most 48 characters")
            )
            return False
        try:
            correction = int(ppm)
            if not -200 <= correction <= 200:
                raise ValueError
            configured_gain = None if gain is None else float(gain)
            if configured_gain is not None and not -1 <= configured_gain <= 100:
                raise ValueError
        except (TypeError, ValueError):
            self._events.put(("error", "Invalid P25 gain or PPM correction"))
            return False
        if self.mode or self.running or self._dump1090_proc or self._rtl433_proc:
            active = self.mode or "another SDR profile"
            self._events.put(
                ("error", f"{active} is active; stop it before starting Pig P25")
            )
            return False
        import tempfile

        self._stop_event.clear()
        self._pig_stop_event.clear()
        self._pig_p25_tmpdir = tempfile.mkdtemp(prefix="wdg-pig-p25-")
        trunk_path = os.path.join(self._pig_p25_tmpdir, "trunk.tsv")
        blacklist_path = os.path.join(self._pig_p25_tmpdir, "all-talkgroups.tsv")
        system = re.sub(r"[^A-Za-z0-9 _.-]", "", system_label)[:48]
        system = system or f"P25 {control_hz / 1e6:.4f} MHz"
        # Current OP25 reads the blacklist column as a file path and accepts
        # inclusive ranges there.  Include every valid 16-bit TGID so ordinary
        # grants cannot make this metadata receiver follow a voice channel.
        with open(blacklist_path, "w", encoding="utf-8") as out:
            out.write("0\t65535\n")
        # OP25 accepts a comma-separated control-channel list and hunts through
        # it after repeated decode timeouts. Keep the selected channel first.
        control_list = ",".join(
            f"{frequency / 1e6:.6f}" for frequency in control_channels
        )
        # OP25's TSV parser maps columns by a required header row.
        with open(trunk_path, "w", encoding="utf-8") as out:
            out.write(
                "\t".join(
                    (
                        "Sysname",
                        "Control Channel List",
                        "Offset",
                        "NAC",
                        "Modulation",
                        "TGID Tags File",
                        "Whitelist",
                        "Blacklist",
                        "Center Frequency",
                    )
                )
                + "\n"
            )
            out.write(
                "\t".join(
                    (
                        system,
                        control_list,
                        "0",
                        "0x0",
                        "cqpsk",
                        "",
                        "",
                        blacklist_path,
                        f"{control_hz / 1e6:.6f}",
                    )
                )
                + "\n"
            )
        try:
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
        except OSError:
            port = 18080
        command = [
            executable,
            "--args",
            "rtl",
            "--frequency",
            str(control_hz),
            "--sample-rate",
            "1000000",
            "--trunk-conf-file",
            trunk_path,
            "--terminal-type",
            f"http:127.0.0.1:{port}",
            "--freq-corr",
            str(correction),
        ]
        if configured_gain is not None and configured_gain >= 0:
            command.extend(("--gains", f"LNA:{int(configured_gain)}"))
        # Deliberately omit OP25's --vocoder, --udp-player, audio, and record options.
        if not self._acquire_readsb_lease():
            self._remove_p25_tmpdir()
            return False
        try:
            with self._pig_p25_stderr_lock:
                self._pig_p25_stderr_tail.clear()
                self._pig_p25_stderr_owner = None
                self._pig_p25_stderr_thread = None
            self._pig_p25_proc = subprocess.Popen(
                command,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                preexec_fn=os.setsid,
                cwd=os.path.dirname(executable),
            )
        except Exception as exc:
            self._remove_p25_tmpdir()
            self._events.put(("error", f"OP25 failed to start: {str(exc)[:120]}"))
            self._release_readsb_lease()
            return False
        stderr_stream = getattr(self._pig_p25_proc, "stderr", None)
        if stderr_stream is not None:
            self._pig_p25_stderr_owner = self._pig_p25_proc
            stderr_thread = threading.Thread(
                target=self._drain_op25_stderr,
                args=(self._pig_p25_proc, stderr_stream),
                daemon=True,
                name="pig-p25-stderr",
            )
            self._pig_p25_stderr_thread = stderr_thread
            self._threads.append(stderr_thread)
            stderr_thread.start()
        self._pig_p25_url = f"http://127.0.0.1:{port}/"
        self._pig_p25_control_hz = control_hz
        self._pig_p25_control_channels = control_channels
        self._pig_p25_system_label = system
        self.p25_quality = {}
        self._pig_p25_last_blocks = None
        self._pig_p25_last_block_at = None
        self._pig_p25_last_tuned_hz = None
        self._pig_p25_last_decoded_hz = None
        self._pig_p25_last_decoded_at = None
        self._pig_log = self._open_pig_log(loot_dir, "pig_p25.jsonl")
        thread = threading.Thread(
            target=self._pig_p25_worker, daemon=True, name="pig-p25-metadata"
        )
        self._threads.append(thread)
        self.running = True
        self.mode = "pig_p25"
        self.p25_session_id += 1
        thread.start()
        self._events.put(("status", "Pig P25 metadata started (audio disabled)"))
        return True

    def start_pig_p25(
        self,
        control_hz,
        system_label="",
        gain=None,
        ppm=0,
        loot_dir="",
        *,
        control_channels=None,
    ) -> bool:
        try:
            frequency = int(control_hz)
        except (TypeError, ValueError):
            self._events.put(("error", "P25 control frequency must be in Hz"))
            return False
        channels = [frequency]
        if control_channels is not None:
            if not isinstance(control_channels, (list, tuple)):
                self._events.put(
                    ("error", "P25 control channel list must be a sequence")
                )
                return False
            if len(control_channels) > 16:
                self._events.put(
                    ("error", "P25 control channel list exceeds 16 frequencies")
                )
                return False
            for candidate in control_channels:
                if type(candidate) is not int:
                    self._events.put(
                        ("error", "P25 control frequencies must be integer Hz")
                    )
                    return False
                if not PIG_RF_MIN_HZ <= candidate <= PIG_RF_MAX_HZ:
                    self._events.put(
                        (
                            "error",
                            "P25 control frequency is outside supported RTL-SDR range",
                        )
                    )
                    return False
                if candidate not in channels:
                    channels.append(candidate)
            if len(channels) > 16:
                self._events.put(
                    ("error", "P25 control channel list exceeds 16 frequencies")
                )
                return False
        if not PIG_RF_MIN_HZ <= frequency <= PIG_RF_MAX_HZ:
            self._events.put(
                ("error", "P25 control frequency is outside supported RTL-SDR range")
            )
            return False
        return self._start_pig_p25(
            frequency, tuple(channels), system_label, gain, ppm, loot_dir
        )

    def _drain_op25_stderr(self, proc, stream=None):
        """Drain child output, retaining only useful lines for this OP25 run."""
        if stream is None:
            stream = proc
            proc = None
        try:
            for raw_line in iter(stream.readline, ""):
                line = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", raw_line).strip()
                if not line or "added talkgroup " in line.lower():
                    continue
                line = "".join(char for char in line if char.isprintable())[:240]
                if line:
                    with self._pig_p25_stderr_lock:
                        if proc is None or self._pig_p25_stderr_owner is proc:
                            self._pig_p25_stderr_tail.append(line)
        except Exception:
            pass

    def _await_op25_stderr(self, proc, timeout=0.5):
        with self._pig_p25_stderr_lock:
            thread = (self._pig_p25_stderr_thread
                      if self._pig_p25_stderr_owner is proc else None)
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=max(0.0, min(float(timeout), 1.0)))

    def _op25_failure_detail(self, proc):
        with self._pig_p25_stderr_lock:
            lines = (list(self._pig_p25_stderr_tail)
                     if self._pig_p25_stderr_owner in (None, proc) else [])
        preferred = ("usb_claim_interface", "osmosdr source_c creation failure",
                     "no module named", "error", "failed", "exception")
        detail = ""
        for marker in preferred:
            detail = next((line for line in lines if marker in line.lower()), "")
            if detail:
                break
        if not detail and lines:
            detail = lines[-1]
        code = proc.poll()
        if code is None:
            prefix = "OP25 failure"
        else:
            prefix = f"OP25 stopped (exit {code})"
        if detail:
            return f"{prefix}: {detail[:110]}"
        return prefix

    def _pig_p25_worker(self):
        last_verified = time.monotonic()
        verified_once = False
        last_status_error = ""
        while not self._pig_stop_event.wait(1.0):
            proc = self._pig_p25_proc
            if not proc:
                return
            if proc.poll() is not None:
                self._await_op25_stderr(proc)
                self._events.put(("error", self._op25_failure_detail(proc)))
                if self.mode == "pig_p25" and self._pig_p25_proc is proc:
                    self.stop()
                return
            if time.monotonic() - last_verified > (10 if verified_once else 30):
                detail = "OP25 tuning status unavailable; stopping P25 profile"
                if last_status_error:
                    detail += f" ({last_status_error[:80]})"
                self._events.put(("error", detail[:180]))
                if self.mode == "pig_p25" and self._pig_p25_proc is proc:
                    self.stop()
                return
            try:
                body = json.dumps(
                    [{"command": "update", "arg1": 0, "arg2": 0}]
                ).encode()
                request = urllib.request.Request(
                    self._pig_p25_url,
                    data=body,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=0.6) as response:
                    data = response.read(65536).decode("utf-8", errors="replace")
                parsed = self._parse_op25_status(data)
                if parsed:
                    observed_hz = parsed.get("tuned_frequency_hz")
                    if observed_hz is None:
                        continue
                    last_verified = time.monotonic()
                    verified_once = True
                    last_status_error = ""
                    tuned_hz = min(
                        self._pig_p25_control_channels or (self._pig_p25_control_hz,),
                        key=lambda candidate: abs(observed_hz - candidate),
                    )
                    if abs(observed_hz - tuned_hz) > 1000:
                        self._events.put(
                            (
                                "error",
                                "OP25 left the configured control channel/list; stopping P25 profile",
                            )
                        )
                        if self.mode == "pig_p25" and self._pig_p25_proc is proc:
                            self.stop()
                        return
                    # Attribute decoder activity to a frequency only when the
                    # block counter rises across two consecutive samples on that
                    # same tuned channel. A change/reset at a hop is not a hit.
                    blocks = parsed.get("decoded_blocks")
                    rate = None
                    sample_at = time.monotonic()
                    same_frequency = self._pig_p25_last_tuned_hz == tuned_hz
                    if type(blocks) is int:
                        if (
                            same_frequency
                            and self._pig_p25_last_blocks is not None
                            and blocks > self._pig_p25_last_blocks
                            and self._pig_p25_last_block_at is not None
                        ):
                            elapsed = max(0.1, sample_at - self._pig_p25_last_block_at)
                            rate = round(
                                (blocks - self._pig_p25_last_blocks) * 60 / elapsed, 1
                            )
                            self._pig_p25_last_decoded_hz = tuned_hz
                            self._pig_p25_last_decoded_at = sample_at
                        elif (
                            same_frequency
                            and self._pig_p25_last_blocks is not None
                            and blocks >= self._pig_p25_last_blocks
                            and self._pig_p25_last_block_at is not None
                        ):
                            elapsed = max(0.1, sample_at - self._pig_p25_last_block_at)
                            rate = round(
                                (blocks - self._pig_p25_last_blocks) * 60 / elapsed, 1
                            )
                        self._pig_p25_last_blocks = blocks
                        self._pig_p25_last_block_at = sample_at
                        self._pig_p25_last_tuned_hz = tuned_hz
                        recent_hz = (
                            self._pig_p25_last_decoded_hz
                            if (
                                self._pig_p25_last_decoded_at is not None
                                and sample_at - self._pig_p25_last_decoded_at <= 30.0
                            )
                            else None
                        )
                        self.p25_quality = {
                            "decoded_blocks": blocks,
                            "blocks_per_min": rate,
                            "tuned_frequency_hz": tuned_hz,
                            "last_decoded_frequency_hz": self._pig_p25_last_decoded_hz,
                            "recent_decoded_frequency_hz": recent_hz,
                            "control_channels_hz": self._pig_p25_control_channels,
                        }
                        parsed["decode_blocks_per_min"] = rate
                        parsed["decoded_frequency_hz"] = recent_hz
                    else:
                        self._pig_p25_last_blocks = None
                        self._pig_p25_last_block_at = None
                        self._pig_p25_last_tuned_hz = tuned_hz
                        self.p25_quality["tuned_frequency_hz"] = tuned_hz
                    parsed.update(
                        {
                            "source": "op25",
                            "profile": "pig_p25",
                            "control_frequency_hz": tuned_hz,
                            "configured_control_frequency_hz": self._pig_p25_control_hz,
                            "control_frequencies_hz": self._pig_p25_control_channels,
                            "system_label": self._pig_p25_system_label,
                            "evidence": "configured_network_activity",
                        }
                    )
                    self._put_pig_event("pig_p25", parsed)
                    self._write_pig_log(parsed)
            except Exception as exc:
                # OP25's loopback dashboard may take a few seconds to start.
                last_status_error = f"{type(exc).__name__}: {str(exc)[:80]}"
                continue

    @staticmethod
    def _parse_op25_status(body: str) -> Optional[dict]:
        """Accept OP25 JSON status payloads and conservative key/value text fixtures."""
        text = body[:65536]
        objects = []
        try:
            decoded = json.loads(text)
            objects.append(decoded)
        except (json.JSONDecodeError, TypeError):
            for candidate in re.findall(r"\{[^{}]{1,4096}\}", text):
                try:
                    objects.append(json.loads(candidate))
                except json.JSONDecodeError:
                    pass
        result = {}
        tuned_frequency_hz = None
        for obj in objects:
            if isinstance(obj, list):
                objects.extend(item for item in obj if isinstance(item, dict))
            if not isinstance(obj, dict):
                continue
            flat = {}
            for key, value in obj.items():
                if isinstance(value, dict):
                    flat.update(value)
                else:
                    flat[key] = value
            fragment = {}
            aliases = {
                "sysid": ("sysid", "sys_id", "system_id"),
                "wacn": ("wacn",), "rfss": ("rfss", "rfss_id"),
                "site": ("site", "site_id", "stid"),
                "talkgroup": ("talkgroup", "tgid", "talkgroup_id"),
                "frequency_hz": ("frequency_hz", "freq_hz", "frequency", "freq"),
                "system": ("system", "sysname"),
                "decoded_blocks": ("tsbks",),
            }
            for dest, keys in aliases.items():
                for key in keys:
                    if key in flat and flat[key] not in (None, ""):
                        try:
                            value = flat[key]
                            if dest == "frequency_hz":
                                number = float(value)
                                fragment[dest] = int(number * 1e6 if number < 1e7 else number)
                            elif dest == "decoded_blocks":
                                count = int(value)
                                if 0 <= count <= 2**32-1:
                                    fragment[dest] = count
                            else:
                                fragment[dest] = str(value)[:48]
                        except (TypeError, ValueError):
                            pass
                        break
            frequency_data = flat.get("frequency_data")
            talkgroups = set()
            if isinstance(frequency_data, dict):
                for entry in frequency_data.values():
                    if not isinstance(entry, dict):
                        continue
                    values = entry.get("tgids", ())
                    if not isinstance(values, (list, tuple, set)):
                        values = (values,)
                    for value in values:
                        try:
                            tgid = int(value)
                        except (TypeError, ValueError):
                            continue
                        if 0 < tgid <= 65535:
                            talkgroups.add(tgid)
            if talkgroups:
                fragment["talkgroups"] = sorted(talkgroups)
                fragment.setdefault("talkgroup", str(max(talkgroups)))
            if "site" not in fragment:
                top_line = flat.get("top_line")
                if top_line:
                    match = re.search(r"\bRFSS\s*(\d+)\s*[./-]\s*Site\s*(\d+)", str(top_line), re.I)
                    if match:
                        fragment["rfss"], fragment["site"] = match.groups()
            if obj.get("json_type") == "change_freq" and "frequency_hz" in fragment:
                tuned_frequency_hz = fragment["frequency_hz"]
            result.update(fragment)
        if tuned_frequency_hz is not None:
            result["tuned_frequency_hz"] = tuned_frequency_hz
        if result and any(k in result for k in
                          ("sysid", "site", "talkgroup", "talkgroups",
                           "system", "frequency_hz")):
            return result
        # Some OP25 builds expose textual status; accept only explicit labels.
        patterns = {
            "sysid": r"\bSys(?:tem)?\s*ID\s*[:=]\s*(0x[0-9a-f]+|[0-9]+)",
            "wacn": r"\bWACN\s*[:=]\s*(0x[0-9a-f]+|[0-9]+)",
            "rfss": r"\bRFSS\s*[:=]\s*([0-9]+)",
            "site": r"\bSite\s*[:=]\s*([0-9]+)",
            "talkgroup": r"\bTGID\s*[:=]\s*([0-9]+)",
            "frequency_hz": r"\b(?:Frequency|Freq)\s*[:=]\s*([0-9.]+)\s*(MHz|Hz)?",
        }
        result = {}
        for key, pattern in patterns.items():
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                value = match.group(1)
                if key == "frequency_hz":
                    unit = (match.group(2) or "MHz").lower()
                    result[key] = int(float(value) * (1e6 if unit == "mhz" else 1))
                else:
                    result[key] = value
        return result or None

    @staticmethod
    def _terminate_proc(proc):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

    def _remove_p25_tmpdir(self):
        if self._pig_p25_tmpdir:
            shutil.rmtree(self._pig_p25_tmpdir, ignore_errors=True)
            self._pig_p25_tmpdir = ""

    def start_adsb(self, loot_dir: str = "") -> bool:
        """Track ADS-B from readsb when available, otherwise run dump1090 privately."""
        if self._reject_pig_busy("ADS-B"):
            return False
        if self._rtl433_proc:
            self._events.put(
                ("error", "433 MHz is active; stop it before starting ADS-B")
            )
            return False
        if self.running and self.mode == "adsb":
            return True

        source = ""
        try:
            readsb_state = self._readsb_state()
        except Exception as exc:
            self._events.put(
                ("error", f"Cannot check readsb ADS-B feed: {str(exc)[:100]}")
            )
            readsb_state = "unknown"
        if readsb_state == "active" and self._sbs_feed_reachable():
            source = "readsb"
        else:
            executable = find_dump1090()
            if not executable:
                self._events.put(
                    ("error", "readsb SBS feed unavailable and dump1090 not installed")
                )
                return False
            if not self._acquire_readsb_lease():
                return False
            self._stop_event.clear()
            try:
                subprocess.run(
                    ["systemctl", "stop", "dump1090-mutability", "dump1090-fa"],
                    capture_output=True,
                    timeout=3,
                )
            except Exception:
                pass
            try:
                self._dump1090_proc = subprocess.Popen(
                    [executable, *DUMP1090_ARGS],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    preexec_fn=os.setsid,
                )
                time.sleep(0.2)
                if self._dump1090_proc.poll() is not None:
                    first = self._dump1090_proc
                    self._dump1090_proc = subprocess.Popen(
                        [executable, *DUMP1090_ARGS_FALLBACK],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        preexec_fn=os.setsid,
                    )
                    time.sleep(0.2)
                    if self._dump1090_proc.poll() is not None:
                        code = self._dump1090_proc.poll()
                        self._dump1090_proc = None
                        raise RuntimeError(
                            f"both launch attempts exited ({first.poll()}, {code})"
                        )
            except Exception as exc:
                self._dump1090_proc = None
                self._events.put(("error", f"dump1090 failed: {exc}"))
                self._release_readsb_lease()
                return False
            source = "dump1090"

        if loot_dir:
            try:
                os.makedirs(loot_dir, exist_ok=True)
                self._adsb_log = open(os.path.join(loot_dir, "adsb_aircraft.csv"), "a")
                if self._adsb_log.tell() == 0:
                    self._adsb_log.write(
                        "timestamp,icao,callsign,lat,lon,alt_ft,speed_kt,"
                        "heading,squawk\n"
                    )
            except Exception:
                pass

        self._stop_event.clear()
        self.running = True
        self.mode = "adsb"
        self._adsb_source = source
        run_stop = threading.Event()
        self._adsb_reader_stop = run_stop
        t = threading.Thread(
            target=self._sbs_reader,
            args=(run_stop,),
            daemon=True,
            name="adsb-sbs-reader",
        )
        self._adsb_reader_thread = t
        self._threads.append(t)
        t.start()
        self._events.put(("status", f"ADS-B started ({source}); waiting for aircraft"))
        return True

    @staticmethod
    def _sbs_feed_reachable() -> bool:
        try:
            with socket.create_connection(("127.0.0.1", 30003), timeout=0.35):
                return True
        except OSError:
            return False

    def start_433(self, loot_dir: str = "", gps_lat: float = 0.0,
                  gps_lon: float = 0.0) -> bool:
        """Start 433 MHz sensor decoding via rtl_433."""
        if self._reject_pig_busy("433 MHz"):
            return False
        if self._dump1090_proc or (self.running and self.mode == "adsb"):
            self._events.put((
                "error", "ADS-B is active; stop it before starting 433 MHz"))
            return False
        if not self.has_rtl433():
            self._events.put(("error", "rtl_433 not installed"))
            return False
        if self._rtl433_proc:
            return True
        if not self._acquire_readsb_lease():
            return False

        self._stop_event.clear()
        self._gps_lat = gps_lat
        self._gps_lon = gps_lon

        try:
            self._rtl433_proc = subprocess.Popen(
                RTL433_CMD,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                preexec_fn=os.setsid,
            )
        except Exception as e:
            self._events.put(("error", f"rtl_433 failed: {e}"))
            self._release_readsb_lease()
            return False

        # Open loot file
        if loot_dir:
            try:
                self._sensor_log = open(
                    os.path.join(loot_dir, "sdr_sensors.csv"), "a")
                if self._sensor_log.tell() == 0:
                    self._sensor_log.write(
                        "timestamp,model,id,data,lat,lon\n")
            except Exception:
                pass

        self.running = True
        self.mode = "433"
        t = threading.Thread(target=self._rtl433_reader, daemon=True,
                             name="rtl433-reader")
        self._threads.append(t)
        t.start()
        self._events.put(("status", "433 MHz scanner started (rtl_433)"))
        return True

    def stop(self):
        """Stop all SDR subprocesses."""
        self._stop_event.set()
        self._pig_stop_event.set()
        adsb_stop = self._adsb_reader_stop
        if adsb_stop is not None:
            adsb_stop.set()

        for proc, name in [
            (self._dump1090_proc, "dump1090"),
            (self._rtl433_proc, "rtl_433"),
            (self._pig_rf_proc, "rtl_power"),
            (self._pig_p25_proc, "OP25"),
        ]:
            if proc:
                self._terminate_proc(proc)

        current = threading.current_thread()
        for thread in tuple(self._threads):
            if thread is not current and thread.name in (
                "pig-rf-watchlist",
                "pig-p25-metadata",
                "pig-p25-stderr",
            ):
                thread.join(timeout=1.5)
        adsb_thread = self._adsb_reader_thread
        if adsb_thread and adsb_thread is not threading.current_thread():
            adsb_thread.join(timeout=1.5)
        self._threads = [thread for thread in self._threads if thread.is_alive()]

        self._dump1090_proc = None
        self._adsb_source = ""
        self._adsb_reader_stop = None
        self._adsb_reader_thread = None
        self._rtl433_proc = None
        self._pig_rf_proc = None
        self._pig_p25_proc = None

        for f in (self._adsb_log, self._sensor_log, self._pig_log):
            if f:
                try:
                    f.close()
                except Exception:
                    pass
        self._adsb_log = None
        self._sensor_log = None
        self._pig_log = None
        self._remove_p25_tmpdir()
        self._release_readsb_lease()
        self._pig_p25_url = ""
        self._pig_channels = []
        self._pig_channel_state = {}
        self._pig_baselines = {}
        self.p25_quality = {}
        self._pig_p25_control_channels = ()
        self._pig_p25_last_blocks = None
        self._pig_p25_last_block_at = None
        self._pig_p25_last_tuned_hz = None
        self._pig_p25_last_decoded_hz = None
        self._pig_p25_last_decoded_at = None
        with self._pig_p25_stderr_lock:
            self._pig_p25_stderr_tail.clear()
            self._pig_p25_stderr_owner = None
            self._pig_p25_stderr_thread = None

        self.running = False
        self.mode = ""

    def poll_events(self) -> list:
        """Drain event queue. Returns list of (type, data) tuples."""
        events = []
        while not self._events.empty():
            try:
                events.append(self._events.get_nowait())
            except Exception:
                break
        with self._pig_events_lock:
            events.extend(self._pig_events)
            self._pig_events.clear()
        return events

    def prune_stale(self):
        """Remove aircraft/sensors not seen recently."""
        now = time.time()
        stale_ac = [k for k, v in self.aircraft.items()
                    if now - v.last_seen > ADSB_TIMEOUT]
        for k in stale_ac:
            del self.aircraft[k]

        stale_s = [k for k, v in self.sensors.items()
                   if now - v.last_seen > SENSOR_TIMEOUT]
        for k in stale_s:
            del self.sensors[k]

    def update_gps(self, lat: float, lon: float):
        """Update GPS position for tagging new sensors."""
        self._gps_lat = lat
        self._gps_lon = lon

    # ------------------------------------------------------------------
    # SBS BaseStation format parser (ADS-B via dump1090 port 30003)
    # ------------------------------------------------------------------
    def _stop_on_owned_child_exit(self, proc, attr_name, label):
        """Stop the profile and release its tuner lease after child termination."""
        if self._stop_event.is_set() or getattr(self, attr_name, None) is not proc:
            return False
        try:
            returncode = proc.poll()
        except Exception:
            return False
        if returncode is None:
            return False
        self._events.put((
            "error", f"{label} exited unexpectedly (status {returncode}); stopping SDR"))
        self.stop()
        return True

    def _sbs_reader(self, run_stop=None):
        """Read SBS data, reconnecting transiently and stopping on sustained loss."""
        stop_event = run_stop if run_stop is not None else self._stop_event
        source = self._adsb_source or "dump1090"
        disconnected_at = None
        announced_wait = False
        while not stop_event.is_set():
            proc = self._dump1090_proc if source == "dump1090" else None
            if proc and self._stop_on_owned_child_exit(
                proc, "_dump1090_proc", "dump1090"
            ):
                return
            sock = None
            try:
                sock = socket.create_connection(("127.0.0.1", 30003), timeout=1)
                sock.settimeout(1)
                disconnected_at = None
                announced_wait = False
                buf = ""
                self._events.put(("status", f"ADS-B SBS feed connected ({source})"))
                while not stop_event.is_set():
                    try:
                        chunk = sock.recv(4096).decode("ascii", errors="ignore")
                        if not chunk:
                            break
                        buf += chunk
                        while "\n" in buf:
                            line, buf = buf.split("\n", 1)
                            self._parse_sbs_line(line.strip())
                    except socket.timeout:
                        if proc and self._stop_on_owned_child_exit(
                            proc, "_dump1090_proc", "dump1090"
                        ):
                            return
                        continue
                if stop_event.is_set():
                    return
                self._events.put(
                    ("status", "ADS-B SBS feed disconnected; reconnecting")
                )
                disconnected_at = disconnected_at or time.monotonic()
            except OSError:
                if stop_event.is_set():
                    return
                if proc and self._stop_on_owned_child_exit(
                    proc, "_dump1090_proc", "dump1090"
                ):
                    return
                if not announced_wait:
                    self._events.put(("status", "ADS-B waiting for SBS feed; retrying"))
                    announced_wait = True
                disconnected_at = disconnected_at or time.monotonic()
            finally:
                if sock is not None:
                    try:
                        sock.close()
                    except OSError:
                        pass
            if disconnected_at is not None and time.monotonic() - disconnected_at >= 60:
                self._events.put(
                    ("error", "ADS-B SBS feed unavailable for 60 seconds; stopping")
                )
                if not stop_event.is_set():
                    self.stop()
                return
            stop_event.wait(1)

    def _parse_sbs_line(self, line: str):
        """Parse SBS BaseStation format line.

        Format: MSG,type,ses,id,icao,id,date,time,date,time,
                callsign,alt,speed,heading,lat,lon,vrate,squawk,...
        """
        parts = line.split(",")
        if len(parts) < 11 or parts[0] != "MSG":
            return

        msg_type = parts[1].strip()
        icao = parts[4].strip().upper()
        if not icao:
            return

        now = time.time()
        is_new = icao not in self.aircraft
        ac = self.aircraft.setdefault(icao, Aircraft(icao=icao))
        ac.last_seen = now

        try:
            if msg_type == "1" and len(parts) > 10:
                cs = parts[10].strip()
                if cs:
                    ac.callsign = cs
            elif msg_type == "3" and len(parts) > 15:
                alt = parts[11].strip()
                spd = parts[12].strip()
                hdg = parts[13].strip()
                lat = parts[14].strip()
                lon = parts[15].strip()
                if alt:
                    ac.altitude = int(float(alt))
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
                if lat and lon:
                    ac.lat = float(lat)
                    ac.lon = float(lon)
            elif msg_type == "4" and len(parts) > 13:
                spd = parts[12].strip()
                hdg = parts[13].strip()
                if spd:
                    ac.speed = int(float(spd))
                if hdg:
                    ac.heading = int(float(hdg))
            elif msg_type == "6" and len(parts) > 17:
                sq = parts[17].strip()
                if sq:
                    ac.squawk = sq
        except (ValueError, IndexError):
            pass

        had_pos_before = not is_new and ac.has_position and msg_type != "3"

        if is_new:
            self.total_aircraft_seen += 1
            label = ac.callsign or icao
            self._events.put(("aircraft_new", ac))
            pos = f"pos:{ac.lat:.3f},{ac.lon:.3f}" if ac.has_position else "waiting for position..."
            self._events.put(("log",
                              f"[ADS-B] New: {label} "
                              f"alt:{ac.altitude}ft {pos}"))
            ac._logged_pos = ac.has_position
        elif msg_type == "3" and ac.has_position and not getattr(ac, '_logged_pos', False):
            # First position fix for this aircraft
            label = ac.callsign or icao
            self._events.put(("log",
                              f"[ADS-B] {label} located: "
                              f"{ac.lat:.3f},{ac.lon:.3f} "
                              f"alt:{ac.altitude}ft spd:{ac.speed}kt"))
            ac._logged_pos = True

        # Log to CSV
        if ac.has_position and self._adsb_log:
            try:
                observed = datetime.fromtimestamp(
                    now, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                self._adsb_log.write(
                    f"{observed},{icao},{ac.callsign},{ac.lat:.6f},"
                    f"{ac.lon:.6f},{ac.altitude},{ac.speed},"
                    f"{ac.heading},{ac.squawk}\n")
                self._adsb_log.flush()
            except Exception:
                pass

    # ------------------------------------------------------------------
    # rtl_433 JSON parser (433 MHz ISM sensors)
    # ------------------------------------------------------------------
    def _rtl433_reader(self):
        """Read rtl_433 JSON output line by line."""
        proc = self._rtl433_proc
        if not proc or not proc.stdout:
            return

        while not self._stop_event.is_set():
            try:
                line = proc.stdout.readline()
                if not line:
                    if self._stop_on_owned_child_exit(
                            proc, "_rtl433_proc", "rtl_433"):
                        return
                    continue
                self._parse_rtl433_line(
                    line.decode("utf-8", errors="replace").strip())
            except Exception:
                if self._stop_event.is_set():
                    return
                continue
        self._stop_on_owned_child_exit(proc, "_rtl433_proc", "rtl_433")

    def _parse_rtl433_line(self, line: str):
        """Parse a JSON line from rtl_433."""
        if not line.startswith("{"):
            return
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            return

        model = data.get("model", "Unknown")
        sid_num = data.get("id", data.get("channel", ""))
        sid = f"{model}_{sid_num}"
        now = time.time()

        is_new = sid not in self.sensors
        sensor = self.sensors.setdefault(sid, Sensor433(
            model=model, sid=sid,
            lat=getattr(self, "_gps_lat", 0.0),
            lon=getattr(self, "_gps_lon", 0.0),
        ))
        sensor.data = data
        sensor.last_seen = now
        sensor.count += 1

        if is_new:
            self.total_sensors_seen += 1
            self._events.put(("sensor_new", sensor))

        # Build display string
        display_parts = [f"[433] {model}"]
        for key in ("temperature_C", "humidity", "pressure_hPa",
                     "battery_ok", "wind_avg_km_h", "rain_mm"):
            if key in data:
                short = key.split("_")[0]
                display_parts.append(f"{short}:{data[key]}")
        rssi = data.get("rssi")
        if rssi is not None:
            display_parts.append(f"RSSI:{rssi}")
        self._events.put(("log", " ".join(display_parts)))

        # Log to CSV
        if self._sensor_log:
            try:
                safe_data = json.dumps(data, ensure_ascii=True)
                self._sensor_log.write(
                    f"{int(now)},{model},{sid_num},"
                    f"\"{safe_data}\","
                    f"{sensor.lat:.6f},{sensor.lon:.6f}\n")
                self._sensor_log.flush()
            except Exception:
                pass
