"""Locate SDR tools consistently during setup, startup and radio use."""

import os
import shutil
import subprocess
from pathlib import Path


DUMP1090_NAMES = ("dump1090", "dump1090-fa", "dump1090-mutability")
SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/usr/local/sbin", "/usr/sbin")
OP25_PIN = "71abcd0ead32f86f51615ea6cc8a6a4dba4c949a"
OP25_SOURCE_ROOTS = ("/opt/op25", "/usr/local/src/op25", "/usr/src/op25")


def find_dump1090() -> str | None:
    """Find source or packaged installs, including outside sudo's PATH."""
    search_path = os.pathsep.join((os.environ.get("PATH", ""), *SYSTEM_BIN_DIRS))
    for name in DUMP1090_NAMES:
        executable = shutil.which(name, path=search_path)
        if executable:
            return os.path.abspath(executable)
    return None


def find_optional_tool(names: tuple[str, ...], extra_paths=()) -> str | None:
    """Find an optional executable in PATH and known local install paths."""
    search_path = os.pathsep.join(
        (os.environ.get("PATH", ""), *SYSTEM_BIN_DIRS, *extra_paths))
    for name in names:
        executable = shutil.which(name, path=search_path)
        if executable and os.path.isfile(executable) and os.access(executable, os.X_OK):
            return os.path.abspath(executable)
    return None


def find_rtl_power() -> str | None:
    return find_optional_tool(("rtl_power",))


def find_op25_rx() -> str | None:
    """Accept only a source installation at the reviewed boatbod OP25 commit."""
    roots = (os.environ.get("WDG_OP25_ROOT"), *OP25_SOURCE_ROOTS)
    for root in roots:
        if not root:
            continue
        source = Path(root)
        executable = source / "op25/gr-op25_repeater/apps/rx.py"
        if not executable.is_file() or not os.access(executable, os.X_OK):
            continue
        try:
            head = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "HEAD"],
                capture_output=True, text=True, timeout=2, check=True).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if head == OP25_PIN:
            return str(executable.resolve())
    return None


if __name__ == "__main__":
    executable = find_dump1090()
    if executable:
        print(executable)
    raise SystemExit(0 if executable else 1)
