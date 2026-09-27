"""Process-wide arbitration for temporary BlueZ pairing agents.

BlueZ permits several clients to register agents, but only one agent can be
the system default.  WatchDogsGo also has to ask the Meshtastic daemon to
temporarily withdraw its own default agent.  This coordinator turns those two
resources into one named, bounded lease shared by the watch and MeshCore
pairing flows.
"""

from __future__ import annotations

import threading
import time
from typing import Callable


class BluetoothPairingCoordinator:
    """Serialize WDG pairing agents and the Meshtastic daemon-agent lease."""

    def __init__(
        self,
        daemon_acquire: Callable[[int], bool] | None = None,
        daemon_release: Callable[[], bool] | None = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._daemon_acquire = daemon_acquire
        self._daemon_release = daemon_release
        self._clock = clock
        self._lock = threading.RLock()
        self._owner = ""
        self._deadline = 0.0
        self._daemon_lease_held = False
        self._release_uncertain = False

    @property
    def owner(self) -> str:
        with self._lock:
            return self._owner

    @property
    def active(self) -> bool:
        with self._lock:
            return bool(self._owner)

    @property
    def release_uncertain(self) -> bool:
        with self._lock:
            return self._release_uncertain

    @property
    def remaining(self) -> float:
        with self._lock:
            return max(0.0, self._deadline - self._clock())

    def acquire(self, owner: str, seconds: int = 120) -> bool:
        """Acquire or renew the named pairing-agent lease.

        Calls are intentionally serialized while the daemon coordination RPC
        runs.  Pairing is a rare, user-initiated operation and preventing a
        second owner from slipping in between the daemon yield and the local
        state commit is more important than parallelism here.
        """
        name = str(owner or "").strip()[:64]
        if not name:
            return False
        duration = max(1, min(120, int(seconds)))
        with self._lock:
            if self._release_uncertain:
                return False
            if self._owner and self._owner != name:
                return False
            acquire = self._daemon_acquire
            if acquire is not None:
                try:
                    granted = bool(acquire(duration))
                except Exception:
                    granted = False
                if not granted:
                    return False
                self._daemon_lease_held = True
            self._owner = name
            self._deadline = self._clock() + duration
            return True

    def release(self, owner: str) -> bool:
        """Release only the matching owner; retain uncertainty on failure."""
        name = str(owner or "").strip()[:64]
        with self._lock:
            if not self._owner:
                return not self._release_uncertain
            if self._owner != name:
                return False
            if self._daemon_lease_held and self._daemon_release is not None:
                try:
                    released = bool(self._daemon_release())
                except Exception:
                    released = False
                if not released:
                    self._release_uncertain = True
                    return False
            self._daemon_lease_held = False
            self._release_uncertain = False
            self._owner = ""
            self._deadline = 0.0
            return True
