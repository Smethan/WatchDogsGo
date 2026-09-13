"""ESP32-C5 flashing helpers. No port is opened by discovery or planning."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time

from .config import FLASH_BOARDS
from .serial_manager import _ESP32_VID_PIDS

MIN_ESPTOOL = (5, 4, 0)
ESPTOOL_PACKAGE = "esptool>=5.4,<6"
FLASH_MODES = ("Automatic", "Manual BOOT")


def ports_now():
    from serial.tools.list_ports import comports
    return list(comports())


@dataclass(frozen=True)
class FlashTarget:
    device: str
    vid: int | None
    pid: int | None
    serial_number: str | None
    location: str | None

    @classmethod
    def from_port(cls, port):
        return cls(port.device, port.vid, port.pid, port.serial_number, port.location)

    def resolve(self, ports=None):
        """Follow this USB device across tty renumbering; never pick another ESP."""
        ports = ports_now() if ports is None else ports
        candidates = [p for p in ports if (p.vid, p.pid) == (self.vid, self.pid)]
        if self.serial_number:
            candidates = [p for p in candidates if p.serial_number == self.serial_number]
        elif self.location:
            candidates = [p for p in candidates if p.location == self.location]
        else:
            candidates = [p for p in candidates if os.path.realpath(p.device) == os.path.realpath(self.device)]
        if len(candidates) != 1:
            raise RuntimeError("Selected ESP32 is missing or ambiguous; reconnect that board and retry")
        return candidates[0].device


def select_target(preferred=None, ports=None):
    """Honor the application's live port; otherwise require one known ESP port."""
    ports = ports_now() if ports is None else ports
    if preferred:
        exact = [p for p in ports if os.path.realpath(p.device) == os.path.realpath(preferred)]
        if len(exact) == 1:
            return FlashTarget.from_port(exact[0])
    candidates = [p for p in ports if (p.vid, p.pid) in _ESP32_VID_PIDS]
    if len(candidates) != 1:
        raise RuntimeError("Connect one ESP32, or reconnect WDG to the intended board first")
    return FlashTarget.from_port(candidates[0])


def wait_for_target(target, timeout=10):
    end = time.monotonic() + timeout
    while True:
        try:
            if target is None:
                target = select_target()
            return target, target.resolve()
        except RuntimeError:
            if time.monotonic() >= end:
                raise
            time.sleep(0.2)


def esptool_version(python):
    try:
        result = subprocess.run([str(python), "-c", "import esptool; print(esptool.__version__)"],
                                capture_output=True, text=True, timeout=20)
        version = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"\d+\.\d+\.\d+", version):
            numbers = tuple(map(int, version.split(".")))
            if MIN_ESPTOOL <= numbers < (6, 0, 0):
                return version
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def ensure_esptool(cache_dir, report):
    """Like flash_board.py, install missing/newer tools in a private venv only."""
    version = esptool_version(sys.executable)
    if version:
        return sys.executable, version
    environment = Path(cache_dir) / "esptool-venv"
    python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    version = esptool_version(python)
    if version:
        return str(python), version
    report("Preparing a private esptool environment (first use only)...")
    for command in ([sys.executable, "-m", "venv", str(environment)],
                    [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--upgrade", ESPTOOL_PACKAGE]):
        result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, timeout=300)
        for line in result.stdout.splitlines():
            report(line)
        if result.returncode:
            raise RuntimeError("Flasher setup failed; check the flash log (Python venv/pip and internet are required)")
    version = esptool_version(python)
    if not version:
        raise RuntimeError("Flasher setup did not provide esptool 5.4 or newer (below 6)")
    return str(python), version


def flash_command(python, port, board, directory, manual=False):
    profile = FLASH_BOARDS[board]
    # default-reset auto-detects native USB-JTAG too. Manual BOOT must preserve
    # the already-running ROM/stub, as in the successful desktop flash log.
    command = [str(python), "-u", "-m", "esptool", "--chip", "esp32c5",
               "--port", port, "--baud", "115200" if manual else str(profile["baud"]),
               "--before", "no-reset" if manual else "default-reset",
               "--after", "watchdog-reset", "write-flash",
               "--flash-mode", "dio", "--flash-freq", "80m", "--flash-size", "detect"]
    for name, offset in profile["offsets"].items():
        command.extend([offset, str(Path(directory) / name)])
    return command


def run_flash(command, report):
    report("Command: " + shlex.join(command))
    # A subprocess matches the standalone flasher and keeps esptool's state
    # separate from the game's serial/GUI libraries. Never retry a write blindly.
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               text=True, bufsize=1, env=dict(os.environ, NO_COLOR="1"))
    try:
        for line in process.stdout:
            if line.strip():
                report(line.strip())
        code = process.wait()
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    if code:
        raise RuntimeError(f"esptool failed (exit {code}); see the saved log. Try Manual BOOT for reset/transfer failures")
