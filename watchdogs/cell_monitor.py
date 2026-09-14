"""Batch-aligned cellular observations for All Wardrive.

Serving-cell identity comes from ModemManager's cached Location interface.
Optional QMI neighbor measurements are deliberately isolated: they run at
most once per minute, never supply a WiGLE identity, and trip a session-local
circuit breaker after the first failure.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from queue import Empty, Full, Queue
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Optional

from .modem_location import ModemLocationBroker, ModemLocationSnapshot


UNKNOWN_CELL_DBM = -113.0
MAX_QMI_OUTPUT = 256 * 1024
MAX_SNAPSHOT_AGE = 15.0


@dataclass(frozen=True)
class CellObservation:
    technology: str
    operator_id: str
    area: int
    cell_id: int
    signal_dbm: float
    channel: Optional[int] = None
    provider: str = "modemmanager_location"
    signal_quality_percent: Optional[int] = None
    serving: bool = True
    frequency: Optional[int] = None

    @property
    def identity(self) -> str:
        return f"{self.operator_id}_{self.area}_{self.cell_id}"

    @property
    def mcc(self) -> str:
        return self.operator_id[:3]

    @property
    def mnc(self) -> str:
        return self.operator_id[3:]

    def record(self) -> dict:
        value = asdict(self)
        value.update(identity=self.identity, mcc=self.mcc, mnc=self.mnc)
        return value


@dataclass(frozen=True)
class CellNeighborCandidate:
    operator_id: str
    serving_area: int
    serving_cell_id: int
    technology: str
    channel: int
    pci: int
    latitude: float
    longitude: float
    observed_at: float
    signal_dbm: Optional[float] = None
    rsrp: Optional[float] = None
    rsrq: Optional[float] = None
    rssi: Optional[float] = None
    provisional: bool = True

    @property
    def key(self) -> str:
        return (f"{self.operator_id}:{self.serving_area}:{self.technology}:"
                f"{self.channel}:{self.pci}")

    def record(self) -> dict:
        value = asdict(self)
        value["key"] = self.key
        return value


@dataclass(frozen=True)
class NeighborProbeResult:
    candidates: tuple[CellNeighborCandidate, ...]
    serving_signal_dbm: Optional[float]
    serving_rsrp: Optional[float]
    serving_rsrq: Optional[float]


def _section(text: str, heading: str) -> str:
    match = re.search(rf"(?m)^{re.escape(heading)}\r?$", text)
    if not match:
        return ""
    tail = text[match.end():].lstrip("\r\n")
    lines = []
    for line in tail.splitlines():
        if line and not line[0].isspace():
            break
        lines.append(line)
    return "\n".join(lines)


def _blocks(section: str, prefix: str) -> list[str]:
    lines = section.expandtabs(4).splitlines()
    result = []
    for index, line in enumerate(lines):
        stripped = line.lstrip()
        if not re.fullmatch(rf"{re.escape(prefix)} \[\d+\]:", stripped):
            continue
        indent = len(line) - len(stripped)
        body = []
        for child in lines[index + 1:]:
            child_stripped = child.lstrip()
            if child_stripped and len(child) - len(child_stripped) <= indent:
                break
            body.append(child)
        result.append("\n".join(body))
    return result


def _value(text: str, label: str) -> Optional[str]:
    match = re.search(rf"(?m)^\s+{re.escape(label)}:\s*'([^']+)'", text)
    return match.group(1).strip() if match else None


def _number(text: str, label: str) -> Optional[float]:
    value = _value(text, label)
    match = re.search(r"-?\d+(?:\.\d+)?", value or "")
    return float(match.group()) if match else None


def _integer(text: str, label: str) -> Optional[int]:
    number = _number(text, label)
    return int(number) if number is not None else None


def parse_qmi_neighbors(text: str, snapshot: ModemLocationSnapshot,
                        fix: dict, observed_at: float) -> NeighborProbeResult:
    """Parse radio-only LTE neighbors without inventing global identities."""
    if not snapshot.identity or snapshot.area is None or snapshot.cell_id is None:
        return NeighborProbeResult((), None, None, None)
    candidates: dict[str, CellNeighborCandidate] = {}
    serving_signal = serving_rsrp = serving_rsrq = None

    def accept_cells(section: str, channel: Optional[int], serving_pci=None):
        nonlocal serving_signal, serving_rsrp, serving_rsrq
        if channel is None or channel <= 0:
            return
        for block in _blocks(section, "Cell"):
            pci = _integer(block, "Physical Cell ID")
            if pci is None or pci < 0:
                continue
            rsrp = _number(block, "RSRP")
            rsrq = _number(block, "RSRQ")
            rssi = _number(block, "RSSI")
            signal = rsrp if rsrp is not None else rssi
            if serving_pci is not None and pci == serving_pci:
                serving_signal, serving_rsrp, serving_rsrq = signal, rsrp, rsrq
                continue
            candidate = CellNeighborCandidate(
                operator_id=snapshot.operator_id or "",
                serving_area=snapshot.area,
                serving_cell_id=snapshot.cell_id,
                technology="LTE", channel=channel, pci=pci,
                latitude=float(fix["latitude"]), longitude=float(fix["longitude"]),
                observed_at=observed_at, signal_dbm=signal,
                rsrp=rsrp, rsrq=rsrq, rssi=rssi)
            candidates[candidate.key] = candidate

    intra = _section(text, "Intrafrequency LTE Info")
    if intra:
        qmi_area = _integer(intra, "Tracking Area Code")
        qmi_cell = _integer(intra, "Global Cell ID")
        channel = _integer(intra, "EUTRA Absolute RF Channel Number")
        serving_pci = _integer(intra, "Serving Cell ID")
        # QMI signal values may enrich a serving row only after its global
        # TAC/CI agree with ModemManager.  The QMI PLMN is intentionally ignored.
        if qmi_area == snapshot.area and qmi_cell == snapshot.cell_id:
            accept_cells(intra, channel, serving_pci)
        else:
            accept_cells(intra, channel, None)

    inter = _section(text, "Interfrequency LTE Info")
    for frequency in _blocks(inter, "Frequency") if inter else ():
        channel = _integer(frequency, "EUTRA Absolute RF Channel Number")
        accept_cells(frequency, channel, None)

    return NeighborProbeResult(tuple(candidates.values()), serving_signal,
                               serving_rsrp, serving_rsrq)


def find_unclean_cell_session(app_dir: str | Path) -> Optional[Path]:
    root = Path(app_dir) / "loot"
    try:
        sentinels = sorted(root.glob("*/active_cell_session.json"), reverse=True)
    except OSError:
        return None
    return sentinels[0] if sentinels else None


class HostCellScanner:
    """Batch-driven serving cells plus an isolated optional QMI worker."""

    def __init__(self, broker: Optional[ModemLocationBroker], *,
                 clock=time.monotonic, wall_clock=time.time,
                 qmi_interval: float = 60.0, qmi_timeout: float = 8.0,
                 popen_factory=subprocess.Popen):
        self.broker = broker
        self.clock = clock
        self.wall_clock = wall_clock
        self.qmi_interval = qmi_interval
        self.qmi_timeout = qmi_timeout
        self.popen_factory = popen_factory
        self.state = "idle"
        self.error = ""
        self.provider = "MM"
        self.session = ""
        self.experimental = False
        self.neighbors_paused = ""
        self.unique: set[str] = set()
        self.observations = 0
        self.latest: Optional[dict] = None
        self.candidates = 0
        self.drops = 0
        self._active = False
        self._owner = ""
        self._events: Queue = Queue(maxsize=64)
        self._tasks: Queue = Queue(maxsize=1)
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._process = None
        self._process_lock = threading.Lock()
        self._probe_pending = False
        self._next_probe = 0.0
        self._last_batch = None
        self._logged_modem_generation = 0
        self._signal_override: Optional[tuple[str, float, float]] = None
        self._loot_dir: Optional[Path] = None
        self._health_path: Optional[Path] = None
        self._sentinel: Optional[Path] = None
        self._last_health_state = ""

    @property
    def active(self) -> bool:
        return self._active

    def start(self, session: str, loot_dir: str | Path,
              experimental_neighbors: bool = False) -> bool:
        if self._active:
            return self.session == session
        self.session = session
        self.experimental = bool(experimental_neighbors)
        self.neighbors_paused = ""
        self.unique.clear()
        self.observations = self.candidates = self.drops = 0
        self.latest = None
        self.error = ""
        self._last_batch = None
        self._logged_modem_generation = 0
        self._signal_override = None
        self._loot_dir = Path(loot_dir)
        self._health_path = self._loot_dir / "cell_health.jsonl"
        self._sentinel = self._loot_dir / "active_cell_session.json"
        self._owner = "cell:" + session
        self._events = Queue(maxsize=64)
        self._tasks = Queue(maxsize=1)
        self._stop = threading.Event()
        self._probe_pending = False
        self._next_probe = self.clock()
        self.state = "preflight"
        self._active = True
        self._write_sentinel()
        self._health("start", experimental=self.experimental)
        if not self.broker:
            self._fail("GPS provider has no ModemManager broker")
            return False
        if not self.broker.acquire(self._owner, cell=True):
            self._fail(self.broker.error or "ModemManager location unavailable")
            return False
        if self.experimental:
            self._thread = threading.Thread(target=self._qmi_worker, daemon=True,
                                            name="wdg-cell-neighbors")
            self._thread.start()
        return True

    def _fail(self, message: str) -> None:
        self.state, self.error = "failed", message[:240]
        self._health("failed", error=self.error)
        self._put("status", {"state": self.state, "error": self.error})

    def observe_batch(self, fix: Optional[dict], batch: object,
                      observed_at: Optional[float] = None) -> None:
        if not self._active or batch == self._last_batch:
            return
        self._last_batch = batch
        if not fix or not fix.get("valid"):
            self._transition("waiting_gps", "No current GPS fix")
            return
        snapshot = self.broker.snapshot() if self.broker else None
        if not snapshot or not snapshot.identity or snapshot.area is None:
            reason = self.broker.error if self.broker else "No modem snapshot"
            self._transition("degraded", reason or "No identified serving cell")
            return
        age = self.clock() - snapshot.observed_monotonic
        if age < 0 or age > MAX_SNAPSHOT_AGE:
            self._transition("degraded", f"Serving-cell snapshot is stale ({max(0, age):.0f}s)")
            return
        signal = snapshot.signal_dbm
        if (signal is None and self._signal_override
                and self._signal_override[0] == snapshot.identity
                and self.clock() <= self._signal_override[2]):
            signal = self._signal_override[1]
        if snapshot.modem_generation != self._logged_modem_generation:
            self._logged_modem_generation = snapshot.modem_generation
            self._health("modem", generation=snapshot.modem_generation,
                         path=snapshot.modem_path, model=snapshot.model,
                         revision=snapshot.revision,
                         enabled_sources=getattr(self.broker, "enabled_sources", 0))
        cell = CellObservation(
            technology=snapshot.technology or "",
            operator_id=snapshot.operator_id or "", area=snapshot.area,
            cell_id=snapshot.cell_id or 0,
            signal_dbm=signal if signal is not None else UNKNOWN_CELL_DBM,
            signal_quality_percent=snapshot.signal_quality_percent)
        self._transition("running", "")
        measured = self.wall_clock() if observed_at is None else observed_at
        self._put("cell", (measured, cell, fix))
        if self.experimental and not self.neighbors_paused and self.clock() >= self._next_probe:
            if self._probe_pending:
                self._trip_neighbors("previous QMI neighbor probe still running")
                return
            if not snapshot.qmi_device:
                self._trip_neighbors("ModemManager reports no QMI control port")
                return
            self._probe_pending = True
            self._next_probe = self.clock() + self.qmi_interval
            try:
                self._tasks.put_nowait((self.session, snapshot, dict(fix), measured))
            except Full:
                self._trip_neighbors("QMI neighbor queue is busy")

    def poll(self) -> list[tuple[str, str, object]]:
        result = []
        for _ in range(64):
            try:
                result.append(self._events.get_nowait())
            except Empty:
                break
        return result

    def note_saved(self, cell: CellObservation) -> None:
        self.observations += 1
        self.unique.add(cell.identity)
        self.latest = cell.record()

    def stop(self, timeout: float = 5.0) -> None:
        if not self._active and not (self._thread and self._thread.is_alive()):
            return
        self._active = False
        self._stop.set()
        try:
            self._tasks.put_nowait(None)
        except Full:
            pass
        with self._process_lock:
            process = self._process
        if process and process.poll() is None:
            try:
                process.terminate()
            except OSError:
                pass
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        if thread and thread.is_alive():
            with self._process_lock:
                process = self._process
            if process and process.poll() is None:
                try:
                    process.kill()
                    process.wait(timeout=1)
                except Exception:
                    pass
            thread.join(1)
        if self.broker and self._owner:
            self.broker.release(self._owner)
        clean = not (thread and thread.is_alive())
        self._health("stop", clean=clean)
        if clean and self._sentinel:
            try:
                self._sentinel.unlink(missing_ok=True)
                self._sync_loot_directory()
            except OSError:
                pass
        self.state = "idle" if clean else "degraded"
        self._thread = None if clean else thread
        self._owner = ""

    def _put(self, kind: str, data: object) -> None:
        try:
            self._events.put_nowait((self.session, kind, data))
        except Full:
            self.drops += 1

    def _transition(self, state: str, error: str) -> None:
        changed = state != self.state or error != self.error
        self.state, self.error = state, error[:240]
        if changed:
            self._health("state", state=state, error=self.error)
            self._put("status", {"state": state, "error": self.error})

    def _trip_neighbors(self, reason: str) -> None:
        if self.neighbors_paused:
            return
        self.neighbors_paused = reason[:200]
        self._probe_pending = False
        self._health("neighbor_circuit_breaker", error=self.neighbors_paused)
        self._put("neighbor_error", self.neighbors_paused)

    def _qmi_worker(self) -> None:
        while not self._stop.is_set():
            try:
                task = self._tasks.get(timeout=.5)
            except Empty:
                continue
            if task is None:
                break
            if self._stop.is_set():
                break
            session, snapshot, fix, observed_at = task
            try:
                binary = shutil.which("qmicli")
                if not binary:
                    raise RuntimeError("qmicli is not installed (libqmi-utils)")
                command = [binary, "--device=" + str(snapshot.qmi_device),
                           "--device-open-proxy", "--nas-get-cell-location-info"]
                started = self.clock()
                process = self.popen_factory(command, stdout=subprocess.PIPE,
                                             stderr=subprocess.PIPE, text=True)
                with self._process_lock:
                    self._process = process
                try:
                    stdout, stderr = process.communicate(timeout=self.qmi_timeout)
                except subprocess.TimeoutExpired as exc:
                    process.terminate()
                    try:
                        process.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=1)
                    raise RuntimeError("QMI neighbor query timed out") from exc
                if len(stdout or "") + len(stderr or "") > MAX_QMI_OUTPUT:
                    raise RuntimeError("QMI neighbor response exceeded 256 KiB")
                if process.returncode:
                    detail = " ".join((stderr or stdout or "QMI query failed").split())[:180]
                    raise RuntimeError(detail)
                result = parse_qmi_neighbors(stdout or "", snapshot, fix, observed_at)
                duration = round(self.clock() - started, 3)
                self._health("neighbor_probe", duration=duration,
                             candidates=len(result.candidates))
                if not self._stop.is_set() and session == self.session:
                    if result.serving_signal_dbm is not None and snapshot.identity:
                        self._signal_override = (snapshot.identity,
                                                 result.serving_signal_dbm,
                                                 self.clock() + self.qmi_interval * 2)
                    self._put("neighbors", result)
            except Exception as exc:
                if not self._stop.is_set() and session == self.session:
                    self._trip_neighbors(str(exc) or type(exc).__name__)
            finally:
                with self._process_lock:
                    self._process = None
                self._probe_pending = False

    def _write_sentinel(self) -> None:
        if not self._sentinel:
            return
        try:
            value = {"session": self.session, "started_at": self.wall_clock(),
                     "experimental_neighbors": self.experimental}
            temp = self._sentinel.with_suffix(".tmp")
            with temp.open("w", encoding="utf-8") as stream:
                stream.write(json.dumps(value, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, self._sentinel)
            self._sync_loot_directory()
        except OSError:
            pass

    def _sync_loot_directory(self) -> None:
        if not self._loot_dir:
            return
        descriptor = None
        try:
            descriptor = os.open(self._loot_dir, os.O_RDONLY | os.O_DIRECTORY)
            os.fsync(descriptor)
        except (AttributeError, OSError):
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _health(self, event: str, **fields) -> None:
        if not self._health_path:
            return
        entry = {"time": self.wall_clock(), "session": self.session,
                 "event": event, **fields}
        try:
            with self._health_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(entry, separators=(",", ":")) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        except OSError:
            pass
