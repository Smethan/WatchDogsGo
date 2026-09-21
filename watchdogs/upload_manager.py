"""WPA-sec integration — upload handshake captures, download passwords."""

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

from .config import WPASEC_URL, WPASEC_DL_URL, WPASEC_KEY

log = logging.getLogger(__name__)

# Runtime token cache (set from secrets.conf OR via in-game input dialog)
_runtime_key: str = ""

_UPLOAD_LEDGER_NAME = ".wpasec_uploads.json"
_UPLOAD_LEDGER_VERSION = 1
_upload_lock = threading.Lock()

# These responses describe the capture itself, so retrying identical bytes can
# never make the upload succeed.  Keep the matching deliberately narrow: HTTP
# failures, authentication errors, rate limits, and timeouts must remain
# retryable.
_PERMANENT_CAPTURE_REJECTION_PATTERNS = (
    "unsupported format",
    "unsupported capture format",
    "unsupported file format",
    "invalid capture format",
    "not a valid capture",
    "invalid or empty pcap",
    "we support pcap and pcapng",
    "no valid handshake",
    "no valid pmkid",
    "no valid handshakes/pmkids",
    "no crackable handshake",
    "no usable handshake",
    "no usable pmkid",
    "no usable password",
    "no valid password",
    "no passwords",
    "contains no password",
)


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default) or ""


def get_wpasec_key() -> str:
    """Return the active WPA-sec key (runtime > env > config)."""
    if _runtime_key:
        return _runtime_key
    # Try new env var first, then legacy, then config
    return (os.environ.get("WDG_WPASEC_KEY")
            or os.environ.get("JANOS_WPASEC_KEY")
            or WPASEC_KEY
            or "")


def set_wpasec_key(key: str) -> None:
    """Set token at runtime (from user input dialog)."""
    global _runtime_key
    _runtime_key = key.strip()


def wpasec_configured() -> bool:
    return bool(get_wpasec_key())


def save_wpasec_key(app_dir: str, key: str) -> None:
    """Persist the WPA-sec key to secrets.conf as WDG_WPASEC_KEY.
    Migrates legacy JANOS_WPASEC_KEY entries on the fly."""
    conf = Path(app_dir) / "secrets.conf"
    lines: list[str] = []
    found = False
    if conf.is_file():
        for line in conf.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("WDG_WPASEC_KEY=") or stripped.startswith("JANOS_WPASEC_KEY="):
                if not found:
                    lines.append(f"WDG_WPASEC_KEY={key}")
                    found = True
                # Drop legacy line (replaced by new one above)
            else:
                lines.append(line)
    if not found:
        if not lines:
            lines.append("# Watch Dogs Go secrets (gitignored)")
        lines.append(f"WDG_WPASEC_KEY={key}")
    conf.write_text("\n".join(lines) + "\n", encoding="utf-8")
    set_wpasec_key(key)


# ── Upload ────────────────────────────────────────────────────────────────

def upload_wpasec(capture_path: Path) -> tuple[bool, str]:
    """Upload one PCAP/PCAPNG capture to WPA-sec."""
    key = get_wpasec_key()
    if not key:
        return False, "WPA-sec key not configured"
    if not capture_path.is_file():
        return False, f"File not found: {capture_path}"
    try:
        import requests
    except ImportError:
        return False, "requests library not installed"
    try:
        with open(capture_path, "rb") as fh:
            files = {"file": (capture_path.name, fh, "application/octet-stream")}
            resp = requests.post(
                WPASEC_URL,
                files=files,
                cookies={"key": key},
                timeout=60,
            )
        if resp.status_code == 200:
            body = resp.text.strip()
            if "already submitted" in body.lower():
                return True, "Already submitted"
            if body.lower().startswith("hcxpcapngtool"):
                return True, body[:200]
            return False, (
                "WPA-sec rejected capture: " + (body[:200] or "empty response"))
        return False, f"HTTP {resp.status_code}: {resp.text[:200]}"
    except Exception as exc:
        log.error("WPA-sec upload error: %s", exc)
        return False, str(exc)


def _capture_candidates(loot_dir: Path) -> list[Path]:
    """Prefer each capture's PCAPNG twin; retain legacy PCAP-only files."""
    candidates: dict[tuple[Path, str], Path] = {}
    for suffix in (".pcap", ".pcapng"):
        for path in loot_dir.rglob(f"handshakes/*{suffix}"):
            key = (path.parent, path.stem)
            current = candidates.get(key)
            if current is None or path.suffix.lower() == ".pcapng":
                candidates[key] = path
    return sorted(candidates.values())


_CAPTURE_MAGICS = {
    b"\x0a\x0d\x0d\x0a",  # PCAPNG
    b"\xa1\xb2\xc3\xd4",  # PCAP, big endian
    b"\xd4\xc3\xb2\xa1",  # PCAP, little endian
}


def _capture_uploadable(path: Path) -> tuple[bool, str]:
    """Match WPA-sec's capture checks before spending a network request."""
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            magic = stream.read(4)
    except OSError as exc:
        return False, f"unreadable capture: {exc}"
    if size <= 64 or magic not in _CAPTURE_MAGICS:
        return False, "invalid or empty PCAP/PCAPNG"

    # WDG generates this from a validated HCCAPX.  A nonempty companion newer
    # than the immutable capture proves hcxtools already extracted a hash.
    companion = path.with_suffix(".22000")
    try:
        if (companion.is_file() and companion.stat().st_size > 0
                and companion.stat().st_mtime_ns >= path.stat().st_mtime_ns):
            return True, ""
    except OSError:
        pass

    tool = shutil.which("hcxpcapngtool")
    if not tool:
        # Preserve legacy behavior on systems where the optional local
        # validator is unavailable; WPA-sec will perform its own validation.
        return True, ""
    try:
        with tempfile.TemporaryDirectory(prefix="wdg-wpasec-") as temp_dir:
            output = Path(temp_dir) / "capture.22000"
            probes = Path(temp_dir) / "probes.txt"
            result = subprocess.run(
                [
                    tool,
                    "--nonce-error-corrections=8",
                    "--eapoltimeout=30000",
                    "--max-essids=1",
                    "-o", str(output),
                    "-R", str(probes),
                    str(path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if output.is_file() and output.stat().st_size > 0:
                return True, ""
            if result.returncode:
                return False, f"hcxtools rejected capture (exit {result.returncode})"
            return False, "no crackable handshake or PMKID"
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"local hcxtools validation failed: {exc}"


def _capture_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _permanent_capture_rejection(message: str) -> str:
    """Return a stable rejection category, or empty for retryable failures."""
    normalized = " ".join(message.lower().split())
    if not any(
            pattern in normalized
            for pattern in _PERMANENT_CAPTURE_REJECTION_PATTERNS):
        return ""
    if any(token in normalized for token in (
            "format", "not a valid capture", "invalid or empty pcap",
            "we support pcap")):
        return "unsupported capture format"
    return "no usable handshake, PMKID, or password data"


def _empty_upload_ledger() -> dict:
    return {"version": _UPLOAD_LEDGER_VERSION, "accounts": {}}


def _load_upload_ledger(loot_dir: Path) -> tuple[dict, str]:
    """Load upload receipts, failing open so captures are never suppressed."""
    path = loot_dir / _UPLOAD_LEDGER_NAME
    if not path.exists():
        return _empty_upload_ledger(), ""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(data, dict)
                or data.get("version") != _UPLOAD_LEDGER_VERSION
                or not isinstance(data.get("accounts"), dict)):
            raise ValueError("unsupported receipt format")
        return data, ""
    except (OSError, UnicodeError, ValueError) as exc:
        log.warning("Cannot read WPA-sec upload receipts %s: %s", path, exc)
        return _empty_upload_ledger(), "upload receipt file unreadable; retried all captures"


def _save_upload_ledger(loot_dir: Path, ledger: dict) -> None:
    """Atomically persist successful-upload receipts after each upload."""
    loot_dir.mkdir(parents=True, exist_ok=True)
    path = loot_dir / _UPLOAD_LEDGER_NAME
    temporary = path.with_name(path.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(ledger, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        try:
            directory_fd = os.open(loot_dir, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _upload_account_id() -> str:
    """Scope receipts to one WPA-sec account without storing its API key."""
    return hashlib.sha256(get_wpasec_key().encode("utf-8")).hexdigest()


def _account_receipts(ledger: dict) -> dict:
    accounts = ledger["accounts"]
    account_id = _upload_account_id()
    account = accounts.setdefault(account_id, {"captures": {}})
    if not isinstance(account, dict):
        account = {"captures": {}}
        accounts[account_id] = account
    captures = account.get("captures")
    if not isinstance(captures, dict):
        captures = {}
        account["captures"] = captures
    return captures


def _store_rejection(
    receipts: dict, digest: str, path: Path, size: int,
    reason: str, detail: str,
) -> None:
    """Record a content-addressed permanent rejection without API secrets."""
    receipts[digest] = {
        "filename": path.name,
        "size": size,
        "status": "permanent_rejection",
        "reason": reason,
        "detail": detail[:300],
        "rejected_at": datetime.now(timezone.utc).isoformat(
            timespec="seconds"),
    }


def upload_wpasec_all(loot_dir: Path) -> tuple[int, int, str]:
    """Upload only captures without a durable success receipt."""
    loot_dir = Path(loot_dir)
    with _upload_lock:
        captures = _capture_candidates(loot_dir)
        if not captures:
            return 0, 0, "No PCAP/PCAPNG files found"

        ledger, ledger_warning = _load_upload_ledger(loot_dir)
        receipts = _account_receipts(ledger)
        pending: list[tuple[Path, str, int]] = []
        already_uploaded = 0
        previously_rejected = 0
        hash_errors = 0
        errors: list[str] = []
        for path in captures:
            try:
                digest = _capture_sha256(path)
                size = path.stat().st_size
            except OSError as exc:
                hash_errors += 1
                errors.append(f"{path.name}: cannot hash: {exc}")
                continue
            receipt = receipts.get(digest)
            if receipt is not None:
                status = (
                    receipt.get("status", "accepted")
                    if isinstance(receipt, dict) else "accepted"
                )
                if status == "permanent_rejection":
                    previously_rejected += 1
                else:
                    already_uploaded += 1
            else:
                pending.append((path, digest, size))

        uploaded = 0
        uploadable: list[tuple[Path, str, int]] = []
        local_skips: dict[str, int] = {}
        permanent_skips: dict[str, int] = {}
        local_rejections_changed = False
        for path, digest, size in pending:
            ready, reason = _capture_uploadable(path)
            if ready:
                uploadable.append((path, digest, size))
            else:
                permanent_reason = _permanent_capture_rejection(reason)
                if permanent_reason:
                    _store_rejection(
                        receipts, digest, path, size,
                        permanent_reason, reason,
                    )
                    permanent_skips[permanent_reason] = (
                        permanent_skips.get(permanent_reason, 0) + 1)
                    local_rejections_changed = True
                else:
                    local_skips[reason] = local_skips.get(reason, 0) + 1

        if local_rejections_changed:
            try:
                _save_upload_ledger(loot_dir, ledger)
            except OSError as exc:
                errors.append(
                    "permanent local rejections were not saved: " + str(exc))

        attempted = len(uploadable) + hash_errors
        for path, digest, size in uploadable:
            ok, message = upload_wpasec(path)
            if not ok:
                permanent_reason = _permanent_capture_rejection(message)
                if not permanent_reason:
                    errors.append(f"{path.name}: {message}")
                    continue
                _store_rejection(
                    receipts, digest, path, size,
                    permanent_reason, message,
                )
                permanent_skips[permanent_reason] = (
                    permanent_skips.get(permanent_reason, 0) + 1)
                try:
                    _save_upload_ledger(loot_dir, ledger)
                except OSError as exc:
                    errors.append(
                        f"{path.name}: permanent rejection was not saved: {exc}")
                continue
            uploaded += 1
            receipts[digest] = {
                "filename": path.name,
                "size": size,
                "status": "accepted",
                "uploaded_at": datetime.now(timezone.utc).isoformat(
                    timespec="seconds"),
            }
            try:
                _save_upload_ledger(loot_dir, ledger)
            except OSError as exc:
                errors.append(
                    f"{path.name}: uploaded but receipt was not saved: {exc}")

        if attempted == 0 and (already_uploaded or previously_rejected):
            summary = "No new captures"
            if already_uploaded:
                summary += f" | {already_uploaded} already uploaded"
            if previously_rejected:
                summary += f" | {previously_rejected} permanently rejected"
        elif attempted == 0:
            summary = "No eligible captures"
        else:
            summary = f"{uploaded}/{attempted} uploaded"
            if already_uploaded:
                summary += f" | {already_uploaded} already uploaded"
            if previously_rejected:
                summary += f" | {previously_rejected} permanently rejected"
        if permanent_skips:
            skipped = sum(permanent_skips.values())
            reasons = ", ".join(
                f"{count} {reason}"
                for reason, count in sorted(permanent_skips.items()))
            summary += f" | {skipped} newly rejected ({reasons})"
        if local_skips:
            skipped = sum(local_skips.values())
            reasons = ", ".join(
                f"{count} {reason}" for reason, count in sorted(local_skips.items()))
            summary += f" | {skipped} skipped locally ({reasons})"
        if ledger_warning:
            summary += f" | Warning: {ledger_warning}"
        if errors:
            summary += f" | Errors: {'; '.join(errors[:3])}"
        return uploaded, attempted, summary


# ── Download (potfile) ────────────────────────────────────────────────────

def download_wpasec_potfile(loot_dir: Path) -> tuple[bool, int, str]:
    """Download cracked passwords from WPA-sec.

    Returns (ok, count, message).
    Saves to loot_dir/passwords/wpasec_cracked.potfile
    """
    key = get_wpasec_key()
    if not key:
        return False, 0, "WPA-sec key not configured"
    try:
        import requests
    except ImportError:
        return False, 0, "requests library not installed"
    try:
        resp = requests.get(
            WPASEC_DL_URL,
            cookies={"key": key},
            timeout=30,
        )
        if resp.status_code != 200:
            return False, 0, f"HTTP {resp.status_code}: {resp.text[:200]}"
        body = resp.text.strip()
        if not body:
            return True, 0, "No cracked passwords yet"
        lines = [ln for ln in body.splitlines() if ln.strip()]
        pwd_dir = loot_dir / "passwords"
        pwd_dir.mkdir(parents=True, exist_ok=True)
        out = pwd_dir / "wpasec_cracked.potfile"
        out.write_text(body + "\n", encoding="utf-8")
        # Parse and save JSON cache
        parsed = parse_potfile(out)
        _save_potfile_json(loot_dir, parsed)
        return True, len(lines), f"{len(lines)} passwords saved"
    except Exception as exc:
        log.error("WPA-sec download error: %s", exc)
        return False, 0, str(exc)


# ── Potfile parsing ───────────────────────────────────────────────────────

def parse_potfile(potfile_path: Path) -> dict:
    """Parse WPA-sec potfile into structured dict.

    Format per line: AP_MAC:CLIENT_MAC:SSID:PASSWORD
    MACs are 17 chars each (xx:xx:xx:xx:xx:xx) with internal colons.

    Returns {"by_ssid": {"SSID": [{"ap_mac", "client_mac", "password"}]},
             "count": N}
    """
    result: dict = {"by_ssid": {}, "count": 0}
    if not potfile_path.is_file():
        return result
    try:
        text = potfile_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return result
    for line in text.splitlines():
        line = line.strip()
        if not line or len(line) < 38:
            continue
        # Char-level parsing: AP_MAC[0:17] : CLIENT_MAC[18:35] : SSID:PASSWORD
        ap_mac = line[:17]
        if line[17] != ":":
            continue
        client_mac = line[18:35]
        if line[35] != ":":
            continue
        rest = line[36:]  # SSID:PASSWORD
        if ":" not in rest:
            continue
        ssid, password = rest.rsplit(":", 1)
        if not ssid:
            continue
        result["count"] += 1
        entry = {"ap_mac": ap_mac, "client_mac": client_mac, "password": password}
        result["by_ssid"].setdefault(ssid, []).append(entry)
    return result


def _save_potfile_json(loot_dir: Path, data: dict) -> None:
    """Save parsed potfile data as JSON cache."""
    pwd_dir = loot_dir / "passwords"
    pwd_dir.mkdir(parents=True, exist_ok=True)
    out = pwd_dir / "wpasec_cracked.json"
    try:
        out.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def load_wpasec_passwords(loot_dir: Path) -> dict:
    """Load parsed WPA-sec passwords from JSON cache (or parse potfile).

    Returns {"by_ssid": {...}, "count": N} or empty dict.
    """
    json_path = loot_dir / "passwords" / "wpasec_cracked.json"
    if json_path.is_file():
        try:
            with open(json_path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and "by_ssid" in data:
                return data
        except (json.JSONDecodeError, OSError):
            pass
    # Fallback: parse potfile directly
    potfile = loot_dir / "passwords" / "wpasec_cracked.potfile"
    if potfile.is_file():
        data = parse_potfile(potfile)
        _save_potfile_json(loot_dir, data)
        return data
    return {"by_ssid": {}, "count": 0}
