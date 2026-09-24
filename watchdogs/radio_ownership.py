"""Process-level ownership for the AIO v2 SX1262 radio.

The SX1262 is accessed directly over SPI by WatchDogsGo, while Meshtastic can
access the same device from a separate daemon.  This advisory lock serializes
direct WatchDogsGo users.  Service handoff remains the caller's responsibility
because an unmodified daemon does not participate in this lock.
"""

from __future__ import annotations

import fcntl
import os
from pathlib import Path


AIO_SX1262_LOCK_PATH = Path("/run/lock/watchdogs/aio-sx1262.lock")


class RadioOwnershipBusy(RuntimeError):
    """Another process currently owns the direct SX1262 interface."""


class RadioOwnership:
    """Hold one nonblocking advisory file lock for a direct radio session."""

    def __init__(self, path: Path | str = AIO_SX1262_LOCK_PATH) -> None:
        self.path = Path(path)
        self._fd: int | None = None
        self.owner = ""

    @property
    def held(self) -> bool:
        return self._fd is not None

    def acquire(self, owner: str = "WatchDogsGo direct radio") -> None:
        """Acquire the radio lock without waiting.

        The file remains in place after release.  Unlinking a lock file can
        create two independently locked inodes and defeat mutual exclusion.
        """
        if self._fd is not None:
            return

        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
        flags = os.O_RDWR | os.O_CREAT
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.path, flags, 0o660)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            detail = ""
            try:
                detail = os.read(fd, 256).decode("utf-8", "replace").strip()
            except OSError:
                pass
            os.close(fd)
            suffix = f" ({detail})" if detail else ""
            raise RadioOwnershipBusy(
                f"AIO SX1262 is already owned by another process{suffix}"
            ) from exc
        except Exception:
            os.close(fd)
            raise

        try:
            os.ftruncate(fd, 0)
            metadata = f"pid={os.getpid()} owner={owner}\n".encode("utf-8")
            os.write(fd, metadata)
            os.fsync(fd)
        except Exception:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            raise

        self._fd = fd
        self.owner = owner

    def release(self) -> None:
        """Release this instance's lock, if held."""
        fd = self._fd
        if fd is None:
            return
        # Retain conservative in-memory ownership if either operation fails.
        # Callers must not start another hardware owner when release is
        # uncertain.  A successful close is the final ownership boundary.
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
        self._fd = None
        self.owner = ""

    def __enter__(self) -> "RadioOwnership":
        self.acquire()
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()
