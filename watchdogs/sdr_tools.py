"""Locate SDR tools consistently during setup, startup and radio use."""

import os
import shutil


DUMP1090_NAMES = ("dump1090", "dump1090-fa", "dump1090-mutability")
SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/usr/local/sbin", "/usr/sbin")


def find_dump1090() -> str | None:
    """Find source or packaged installs, including outside sudo's PATH."""
    search_path = os.pathsep.join((os.environ.get("PATH", ""), *SYSTEM_BIN_DIRS))
    for name in DUMP1090_NAMES:
        executable = shutil.which(name, path=search_path)
        if executable:
            return os.path.abspath(executable)
    return None


if __name__ == "__main__":
    executable = find_dump1090()
    if executable:
        print(executable)
    raise SystemExit(0 if executable else 1)
