#!/usr/bin/python3
"""Safely orchestrate Meshtastic WDG release installation and adoption.

The privileged trust boundary remains ``watchdogs-meshtastic``.  This command
only coordinates its fixed operations, GitHub's authenticated CLI, and the
one-time copy of an immutable Actions artifact into the helper's root-owned
first-install inbox.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPOSITORY = "Smethan/meshtastic-firmware"
WORKFLOW = "wdg-native.yml"
WORKFLOW_PATH = ".github/workflows/wdg-native.yml"
HELPER = Path("/usr/local/libexec/watchdogs-meshtastic")
CACHE_ROOT = Path("/var/cache/watchdogs/meshtasticd-wdg")
FIRST_INSTALL_INBOX = CACHE_ROOT / "first-install-inbox"
SUDO = Path("/usr/bin/sudo")
INSTALL = Path("/usr/bin/install")
APT_GET = Path("/usr/bin/apt-get")
TAG_RE = re.compile(
    r"^v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\."
    r"(0|[1-9][0-9]*)-wdg\.(0|[1-9][0-9]*)$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
TARGETS = ("wdg", "stock")
Run = Callable[..., subprocess.CompletedProcess[str]]


class ReleaseToolError(RuntimeError):
    """A release operation failed without crossing the protected boundary."""


@dataclass(frozen=True)
class ServiceState:
    target: str
    load_state: str
    active_state: str
    unit_file_state: str
    package_version: str | None

    @property
    def active(self) -> bool:
        return self.active_state == "active"


def package_version_for_tag(tag: str) -> str:
    if not TAG_RE.fullmatch(tag):
        raise ReleaseToolError("Expected Meshtastic tag vX.Y.Z-wdg.N")
    return tag[1:].replace("-wdg.", "+wdg")


def package_name_for_tag(tag: str) -> str:
    return f"meshtasticd-wdg_{package_version_for_tag(tag)}_arm64.deb"


def expected_candidate_names(tag: str) -> frozenset[str]:
    return frozenset({
        "compatibility.json",
        "SHA256SUMS",
        "SOURCE.txt",
        "copyright",
        package_name_for_tag(tag),
    })


def validate_workflow_run(
    value: Any, *, tag: str, run_id: int, attempt: int,
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReleaseToolError("GitHub returned a malformed workflow run")
    expected = {
        "id": run_id,
        "run_attempt": attempt,
        "event": "push",
        "head_branch": tag,
        "status": "completed",
        "conclusion": "success",
        "path": WORKFLOW_PATH,
    }
    for field, wanted in expected.items():
        if value.get(field) != wanted:
            raise ReleaseToolError(
                f"Workflow run {field} must be {wanted!r}, found "
                f"{value.get(field)!r}")
    repository = value.get("repository")
    if not isinstance(repository, dict) or repository.get("full_name") != REPOSITORY:
        raise ReleaseToolError("Workflow run belongs to the wrong repository")
    if not isinstance(value.get("head_sha"), str) or not SHA_RE.fullmatch(
            value["head_sha"]):
        raise ReleaseToolError("Workflow run has an invalid source commit")
    return value


def validate_artifact_listing(
    value: Any, *, run_id: int, attempt: int,
) -> dict[str, Any]:
    if not isinstance(value, dict) or not isinstance(value.get("artifacts"), list):
        raise ReleaseToolError("GitHub returned a malformed artifact listing")
    expected_name = f"meshtasticd-wdg-arm64-{run_id}-{attempt}"
    matches = [
        item for item in value["artifacts"]
        if isinstance(item, dict) and item.get("name") == expected_name
    ]
    if len(matches) != 1:
        raise ReleaseToolError(
            f"Expected exactly one immutable artifact named {expected_name}")
    artifact = matches[0]
    if artifact.get("expired") is not False:
        raise ReleaseToolError("The tested Actions artifact has expired")
    if not isinstance(artifact.get("id"), int) or artifact["id"] <= 0:
        raise ReleaseToolError("Actions artifact has an invalid identifier")
    workflow_run = artifact.get("workflow_run")
    if (workflow_run is not None
            and (not isinstance(workflow_run, dict)
                 or workflow_run.get("id") != run_id)):
        raise ReleaseToolError("Actions artifact belongs to the wrong run")
    return artifact


def validate_candidate_directory(directory: Path, tag: str) -> list[Path]:
    if directory.is_symlink() or not directory.is_dir():
        raise ReleaseToolError("Downloaded candidate is not a real directory")
    expected = expected_candidate_names(tag)
    children = list(directory.iterdir())
    names = {child.name for child in children}
    if names != expected or len(children) != len(expected):
        missing = sorted(expected - names)
        extra = sorted(names - expected)
        raise ReleaseToolError(
            f"Candidate file set is not exact; missing={missing}, extra={extra}")
    for child in children:
        if child.is_symlink() or not child.is_file():
            raise ReleaseToolError(
                f"Candidate entry is not a regular file: {child.name}")
    return [directory / name for name in sorted(expected)]


class ReleaseManager:
    def __init__(
        self,
        *,
        runner: Run = subprocess.run,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        gh: str | None = None,
    ) -> None:
        self._runner = runner
        self._sleep = sleeper
        self._monotonic = monotonic
        self._gh = gh or shutil.which("gh")

    def _run(
        self,
        command: Sequence[str | os.PathLike[str]],
        *,
        timeout: int = 60,
        check: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        rendered = [os.fspath(part) for part in command]
        try:
            result = self._runner(
                rendered, capture_output=True, text=True, timeout=timeout,
                check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise ReleaseToolError(
                f"Could not run {Path(rendered[0]).name}: {exc}") from exc
        if check and result.returncode:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise ReleaseToolError(
                f"{Path(rendered[0]).name} failed: {detail[:1000]}")
        return result

    def _json_command(
        self, command: Sequence[str | os.PathLike[str]], *, timeout: int = 60,
    ) -> dict[str, Any]:
        result = self._run(command, timeout=timeout)
        try:
            value = json.loads(result.stdout)
        except (json.JSONDecodeError, TypeError) as exc:
            raise ReleaseToolError("Command returned malformed JSON") from exc
        if not isinstance(value, dict):
            raise ReleaseToolError("Command returned a non-object JSON reply")
        return value

    def _require_gh(self) -> str:
        if not self._gh:
            raise ReleaseToolError(
                "GitHub CLI is required; install gh and run gh auth login")
        return self._gh

    def _helper(
        self, *arguments: str, timeout: int = 120,
    ) -> dict[str, Any]:
        reply = self._json_command(
            [SUDO, "-n", HELPER, *arguments], timeout=timeout)
        if reply.get("ok") is not True:
            raise ReleaseToolError("Protected helper rejected the operation")
        return reply

    def service_status(self, target: str) -> ServiceState:
        if target not in TARGETS:
            raise ReleaseToolError("Unknown Meshtastic service target")
        reply = self._helper("status", target)
        required = {
            "target", "load_state", "active_state", "unit_file_state",
            "package_version",
        }
        if not required.issubset(reply) or reply.get("target") != target:
            raise ReleaseToolError("Protected helper returned malformed service state")
        if reply["load_state"] not in {"loaded", "not-found"}:
            raise ReleaseToolError(
                f"{target} service load state is unstable: {reply['load_state']}")
        if reply["active_state"] not in {"active", "inactive"}:
            raise ReleaseToolError(
                f"{target} service is transitioning: {reply['active_state']}")
        package_version = reply["package_version"]
        if package_version is not None and not isinstance(package_version, str):
            raise ReleaseToolError("Protected helper returned an invalid package version")
        return ServiceState(
            target=target,
            load_state=reply["load_state"],
            active_state=reply["active_state"],
            unit_file_state=str(reply["unit_file_state"]),
            package_version=package_version,
        )

    def snapshot_services(self) -> dict[str, ServiceState]:
        states = {target: self.service_status(target) for target in TARGETS}
        active = [target for target, state in states.items() if state.active]
        if len(active) > 1:
            raise ReleaseToolError(
                "Both Meshtastic daemons are active; resolve radio ownership first")
        return states

    def _stop_active(self, states: dict[str, ServiceState]) -> None:
        stopped: list[str] = []
        try:
            for target in TARGETS:
                if states[target].active:
                    self._helper("stop", target)
                    stopped.append(target)
        except Exception:
            for target in stopped:
                try:
                    self._helper("start", target)
                except ReleaseToolError:
                    pass
            raise

    def _restore_active(self, states: dict[str, ServiceState]) -> None:
        failures: list[str] = []
        for target in TARGETS:
            if not states[target].active:
                continue
            try:
                self._helper("start", target)
            except ReleaseToolError as exc:
                failures.append(f"{target}: {exc}")
        if failures:
            raise ReleaseToolError(
                "Could not restore the previously active service: "
                + "; ".join(failures))

    def status(self) -> dict[str, Any]:
        states = self.snapshot_services()
        return {
            "ok": True,
            "services": {
                target: {
                    "load_state": state.load_state,
                    "active_state": state.active_state,
                    "unit_file_state": state.unit_file_state,
                    "package_version": state.package_version,
                }
                for target, state in states.items()
            },
        }

    def adopt(self, tag: str) -> dict[str, Any]:
        expected_version = package_version_for_tag(tag)
        states = self.snapshot_services()
        installed = states["wdg"].package_version
        if installed != expected_version:
            raise ReleaseToolError(
                f"Installed package is {installed or 'absent'}, not {expected_version}")
        self._stop_active(states)
        failure: Exception | None = None
        reply: dict[str, Any] | None = None
        try:
            reply = self._helper("adopt-installed", tag, timeout=900)
        except Exception as exc:  # noqa: BLE001 - restore owner before propagating
            failure = exc
        try:
            self._restore_active(states)
        except Exception as restore_error:
            if failure is not None:
                raise ReleaseToolError(
                    f"Adoption failed ({failure}); service restoration also "
                    f"failed ({restore_error})") from failure
            raise
        if failure is not None:
            raise failure
        assert reply is not None
        return reply

    def update(self, tag: str) -> dict[str, Any]:
        package_version_for_tag(tag)
        return self._helper("install-tag", tag, timeout=900)

    def _github_json(self, endpoint: str) -> dict[str, Any]:
        gh = self._require_gh()
        return self._json_command([gh, "api", endpoint], timeout=60)

    def validate_provenance(
        self, *, tag: str, run_id: int, attempt: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        package_version_for_tag(tag)
        if run_id <= 0 or attempt <= 0:
            raise ReleaseToolError("Run ID and attempt must be positive integers")
        run = self._github_json(
            f"repos/{REPOSITORY}/actions/runs/{run_id}/attempts/{attempt}")
        validate_workflow_run(
            run, tag=tag, run_id=run_id, attempt=attempt)
        artifacts = self._github_json(
            f"repos/{REPOSITORY}/actions/runs/{run_id}/artifacts?per_page=100")
        artifact = validate_artifact_listing(
            artifacts, run_id=run_id, attempt=attempt)
        return run, artifact

    def _download_candidate(
        self, *, tag: str, run_id: int, attempt: int, destination: Path,
    ) -> list[Path]:
        gh = self._require_gh()
        artifact_name = f"meshtasticd-wdg-arm64-{run_id}-{attempt}"
        self._run([
            gh, "run", "download", str(run_id), "--repo", REPOSITORY,
            "--name", artifact_name, "--dir", destination,
        ], timeout=300)
        return validate_candidate_directory(destination, tag)

    def _stage_candidate(self, tag: str, files: list[Path]) -> None:
        inbox = FIRST_INSTALL_INBOX / tag
        self._run([
            SUDO, INSTALL, "-d", "-o", "root", "-g", "root", "-m", "0700",
            inbox,
        ])
        for source in files:
            self._run([
                SUDO, INSTALL, "-o", "root", "-g", "root", "-m", "0600",
                source, inbox / source.name,
            ])

    def _prepared_package(self, tag: str) -> Path:
        reply = self._helper("prepare-first-tag", tag, timeout=900)
        expected = CACHE_ROOT / tag / package_name_for_tag(tag)
        if reply.get("package_path") != str(expected):
            raise ReleaseToolError(
                "Protected helper returned an unexpected package path")
        if reply.get("package_version") != package_version_for_tag(tag):
            raise ReleaseToolError(
                "Protected helper returned an unexpected package version")
        return expected

    def _install_first_package(
        self, *, tag: str, package: Path, action: str,
    ) -> dict[str, Any]:
        states = self.snapshot_services()
        if states["wdg"].package_version is not None:
            raise ReleaseToolError(
                "meshtasticd-wdg is already installed; use adopt or update")
        self._stop_active(states)
        failure: Exception | None = None
        try:
            self._run([
                SUDO, APT_GET, "-y", "--no-install-recommends", "install",
                package,
            ], timeout=600)
            selected = self._helper("select-service", "wdg", timeout=120)
            return {
                "ok": True,
                "action": action,
                "tag": tag,
                "package_version": package_version_for_tag(tag),
                "adopted": False,
                "service": selected,
            }
        except Exception as exc:  # noqa: BLE001 - restore owner before propagating
            failure = exc
        try:
            self._restore_active(states)
        except Exception as restore_error:  # noqa: BLE001 - combine both failures
            raise ReleaseToolError(
                f"First install failed ({failure}); service restoration also "
                f"failed ({restore_error})") from failure
        assert failure is not None
        raise failure

    def install_public(self, tag: str) -> dict[str, Any]:
        package = self._prepared_package(tag)
        result = self._install_first_package(
            tag=tag, package=package, action="install-public")
        result["next_step"] = (
            "After hardware testing, run adopt with the same tag")
        return result

    def install_draft(
        self, *, tag: str, run_id: int, attempt: int,
    ) -> dict[str, Any]:
        states = self.snapshot_services()
        if states["wdg"].package_version is not None:
            raise ReleaseToolError(
                "Draft installation is only for the first fork package; "
                "use update for an adopted installation")
        run, artifact = self.validate_provenance(
            tag=tag, run_id=run_id, attempt=attempt)
        with tempfile.TemporaryDirectory(
                prefix="watchdogs-meshtastic-candidate-") as temporary:
            directory = Path(temporary)
            os.chmod(directory, 0o700)
            files = self._download_candidate(
                tag=tag, run_id=run_id, attempt=attempt,
                destination=directory)
            self._stage_candidate(tag, files)
        package = self._prepared_package(tag)
        result = self._install_first_package(
            tag=tag, package=package, action="install-draft")
        result["source_commit"] = run["head_sha"]
        result["artifact_id"] = artifact["id"]
        result["next_step"] = (
            "After hardware testing, run publish-adopt-draft with the same "
            "tag, run ID, and attempt")
        return result

    def _release_state(self, tag: str) -> dict[str, Any]:
        gh = self._require_gh()
        return self._json_command([
            gh, "release", "view", tag, "--repo", REPOSITORY,
            "--json", "tagName,isDraft,isPrerelease,publishedAt,url",
        ], timeout=60)

    @staticmethod
    def _release_is_public(value: dict[str, Any], tag: str) -> bool:
        return (
            value.get("tagName") == tag
            and value.get("isDraft") is False
            and value.get("isPrerelease") is False
            and isinstance(value.get("publishedAt"), str)
            and bool(value["publishedAt"])
        )

    def publish_adopt_draft(
        self,
        *,
        tag: str,
        run_id: int,
        attempt: int,
        confirmed: bool,
        timeout: int = 900,
    ) -> dict[str, Any]:
        if not confirmed:
            raise ReleaseToolError(
                "Publishing is external and permanent; repeat with --yes")
        states = self.snapshot_services()
        if states["wdg"].package_version != package_version_for_tag(tag):
            raise ReleaseToolError(
                "The exact tested draft package is not installed")
        run, artifact = self.validate_provenance(
            tag=tag, run_id=run_id, attempt=attempt)
        release = self._release_state(tag)
        if not self._release_is_public(release, tag):
            if (release.get("tagName") != tag
                    or release.get("isDraft") is not True
                    or release.get("isPrerelease") is not False):
                raise ReleaseToolError(
                    "Expected an ordinary draft release for the tested tag")
            gh = self._require_gh()
            self._run([
                gh, "workflow", "run", WORKFLOW, "--repo", REPOSITORY,
                "-f", "operation=publish_draft",
                "-f", f"package_tag={tag}",
                "-f", f"tested_run_id={run_id}",
                "-f", f"tested_artifact_attempt={attempt}",
            ], timeout=60)
            deadline = self._monotonic() + timeout
            while True:
                if self._monotonic() >= deadline:
                    raise ReleaseToolError(
                        "Timed out waiting for the draft release to publish")
                self._sleep(10)
                release = self._release_state(tag)
                if self._release_is_public(release, tag):
                    break
        adoption = self.adopt(tag)
        return {
            "ok": True,
            "action": "publish-adopt-draft",
            "tag": tag,
            "source_commit": run["head_sha"],
            "artifact_id": artifact["id"],
            "release_url": release.get("url"),
            "adoption": adoption,
        }


def positive_integer(value: str) -> int:
    try:
        parsed = int(value, 10)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected a positive integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("expected a positive integer")
    return parsed


def tag_argument(value: str) -> str:
    if not TAG_RE.fullmatch(value):
        raise argparse.ArgumentTypeError("expected vX.Y.Z-wdg.N")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Install, publish, adopt, and update Meshtastic WDG releases")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="show both daemon and package states")

    adopt = commands.add_parser(
        "adopt", help="adopt an installed, published release")
    adopt.add_argument("tag", type=tag_argument)

    install_public = commands.add_parser(
        "install-public",
        help="install the first public fork package for hardware testing")
    install_public.add_argument("tag", type=tag_argument)

    install_draft = commands.add_parser(
        "install-draft",
        help="install the first fork package from an immutable Actions artifact")
    install_draft.add_argument("tag", type=tag_argument)
    install_draft.add_argument("--run-id", required=True, type=positive_integer)
    install_draft.add_argument("--attempt", required=True, type=positive_integer)

    publish = commands.add_parser(
        "publish-adopt-draft",
        help="publish the tested draft and adopt that exact installed package")
    publish.add_argument("tag", type=tag_argument)
    publish.add_argument("--run-id", required=True, type=positive_integer)
    publish.add_argument("--attempt", required=True, type=positive_integer)
    publish.add_argument("--yes", action="store_true")
    publish.add_argument("--timeout", type=positive_integer, default=900)

    update = commands.add_parser(
        "update", help="transactionally update an adopted installation")
    update.add_argument("tag", type=tag_argument)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    manager = ReleaseManager()
    try:
        if args.command == "status":
            reply = manager.status()
        elif args.command == "adopt":
            reply = manager.adopt(args.tag)
        elif args.command == "install-public":
            reply = manager.install_public(args.tag)
        elif args.command == "install-draft":
            reply = manager.install_draft(
                tag=args.tag, run_id=args.run_id, attempt=args.attempt)
        elif args.command == "publish-adopt-draft":
            reply = manager.publish_adopt_draft(
                tag=args.tag, run_id=args.run_id, attempt=args.attempt,
                confirmed=args.yes, timeout=args.timeout)
        elif args.command == "update":
            reply = manager.update(args.tag)
        else:  # pragma: no cover - argparse enforces the closed command set
            parser.error("unsupported command")
        print(json.dumps(reply, indent=2, sort_keys=True))
        return 0
    except ReleaseToolError as exc:
        print(f"Meshtastic release operation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
