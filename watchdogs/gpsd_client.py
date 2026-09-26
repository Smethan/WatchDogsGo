"""Small non-blocking client for gpsd's newline-delimited JSON protocol."""

import json
import socket
import time
from collections.abc import Callable

WATCH_COMMAND = b'?WATCH={"enable":true,"json":true};\n'
MAX_BUFFER = 256 * 1024


class GpsdClient:
    """Read gpsd reports without adding a Python gpsd package dependency."""

    def __init__(self, host: str = "127.0.0.1", port: int = 2947,
                 connect_timeout: float = 0.5,
                 connector: Callable = socket.create_connection) -> None:
        self.host = host
        self.port = int(port)
        self.connect_timeout = float(connect_timeout)
        self._connector = connector
        self._sock: socket.socket | None = None
        self._buf = b""
        self.last_report_at = 0.0

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        self.close()
        conn = self._connector((self.host, self.port), self.connect_timeout)
        try:
            conn.sendall(WATCH_COMMAND)
            conn.setblocking(False)
        except Exception:
            conn.close()
            raise
        self._sock = conn
        self._buf = b""

    def read_reports(self) -> list[dict]:
        """Drain currently available gpsd JSON objects.

        An orderly socket close is raised to the caller so it can mark the
        provider unavailable and reconnect. Malformed/oversized individual
        records are discarded without poisoning later reports.
        """
        if self._sock is None:
            raise ConnectionError("gpsd is not connected")

        for _ in range(64):
            try:
                chunk = self._sock.recv(8192)
            except BlockingIOError:
                break
            except InterruptedError:
                continue
            except OSError as exc:
                raise ConnectionError(f"gpsd read failed: {exc}") from exc
            if not chunk:
                raise ConnectionError("gpsd closed the connection")
            self._buf += chunk
            if len(self._buf) > MAX_BUFFER:
                # Keep only bytes after the newest newline. A peer that sends
                # one unbounded record must not grow WDG's memory forever.
                _, separator, tail = self._buf.rpartition(b"\n")
                self._buf = tail if separator else b""

        reports: list[dict] = []
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            raw = raw.strip()
            if not raw:
                continue
            try:
                report = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(report, dict):
                reports.append(report)
        if reports:
            self.last_report_at = time.monotonic()
        return reports

    def close(self) -> None:
        conn, self._sock = self._sock, None
        if conn is not None:
            try:
                conn.close()
            except OSError:
                pass
        self._buf = b""
