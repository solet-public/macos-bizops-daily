"""Immutable seed acquisition with commit and tree verification."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

from solet_setup_contracts.provenance_v1 import (
    ProvenanceV1Error,
    parse_provenance_v1,
    verify_seal_trailers,
)

from .errors import SourceError, SourceIdentityError
from .release_lock import SeedLock
from .state_io import ensure_private_directory
from .target_parent import ParentIdentity, ensure_target_parent

_GIT_TIMEOUT_SECONDS = 120
_REPOSITORY_PROBE_TIMEOUT_SECONDS = 15
_GITHUB_REPOSITORY = re.compile(
    r"^https://github\.com/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repository>[A-Za-z0-9_.-]+)\.git$"
)
_HTTP_STATUS = re.compile(r"^HTTP/\S+ (?P<status>[0-9]{3})(?: |$)")
_GIT_CURL_RESPONSE_HEADER = "<= Recv header:"
_ACCESS_ELIGIBLE_HTTP_STATUSES = frozenset({401, 403, 404})


@dataclass(frozen=True)
class RunResult:
    returncode: int
    stdout: str
    stderr: str
    final_http_status: int | None = None


type CommandRunner = Callable[[Sequence[str], Path | None, int], RunResult]
type ReplaceRunner = Callable[[Path, Path, ParentIdentity], None]


class RepositoryAccessReason(StrEnum):
    """Normalized GitHub repository facts available after anonymous fetch failure."""

    PRIVATE = "seed_repository_private"
    MISSING = "seed_repository_missing"
    PRIVATE_OR_MISSING = "seed_repository_private_or_missing"


class SeedRepositoryAccessError(SourceError):
    """Actionable fetch refusal with a stable reason beneath source_error."""

    def __init__(self, message: str, *, reason: RepositoryAccessReason, repair: str) -> None:
        super().__init__(message, repair=repair)
        self.reason = reason.value


def subprocess_runner(command: Sequence[str], cwd: Path | None, timeout: int) -> RunResult:
    """Run one bounded, argument-vector-only command."""

    if tuple(command[:2]) != ("git", "fetch"):
        completed = subprocess.run(  # noqa: S603 - closed argv is the contract
            list(command),
            cwd=cwd,
            capture_output=True,
            check=False,
            text=True,
            timeout=timeout,
        )
        return RunResult(completed.returncode, completed.stdout, completed.stderr)

    with tempfile.TemporaryDirectory(prefix="solet-git-transport-") as temporary:
        trace_path = Path(temporary) / "curl.trace"
        environment = os.environ.copy()
        environment["GIT_TRACE_CURL"] = str(trace_path)
        environment["GIT_TRACE_CURL_NO_DATA"] = "1"
        completed = subprocess.run(  # noqa: S603 - closed argv is the contract
            list(command),
            cwd=cwd,
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=timeout,
        )
        return RunResult(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            _final_git_http_status(trace_path),
        )


def materialize_locked_seed(
    seed: SeedLock,
    target: Path,
    *,
    cache_dir: Path,
    runner: CommandRunner = subprocess_runner,
    replace_runner: ReplaceRunner | None = None,
    repository_probe_runner: CommandRunner = subprocess_runner,
) -> Path:
    """Fetch one immutable seed identity, verify it, and retain origin/main.

    The staging checkout is deliberately retained on failure for diagnosis; no
    implicit cleanup or target deletion is part of acquisition.
    """

    staging, parent_identity = _prepare_staging(target, cache_dir)
    _initialize_staging(seed, staging, runner)
    _fetch_and_verify_identity(seed, staging, runner, repository_probe_runner)
    _checkout_verified_seed(seed, staging, runner)
    _verify_provenance_identity(staging, seed.profile, runner)
    _verify_origin_and_main(seed, staging, runner)
    _finalize_staging(staging, target, parent_identity, replace_runner)
    return target


def _prepare_staging(target: Path, cache_dir: Path) -> tuple[Path, ParentIdentity]:
    if target.exists():
        raise SourceError(f"target already exists and will not be reused or overwritten: {target}")
    ensure_private_directory(cache_dir)
    parent_identity = ensure_target_parent(target.parent)
    staging = target.parent / f".{target.name}.solet-acquire-{uuid.uuid4().hex}"
    if staging.exists():
        raise SourceError(f"unexpected acquisition staging collision: {staging}")
    staging.mkdir(mode=0o700)
    return staging, parent_identity


def _initialize_staging(seed: SeedLock, staging: Path, runner: CommandRunner) -> None:
    _run_checked(runner, ("git", "init", "--initial-branch=main", str(staging)), None)
    _run_checked(runner, ("git", "remote", "add", "origin", seed.repository), staging)


def _fetch_and_verify_identity(
    seed: SeedLock,
    staging: Path,
    runner: CommandRunner,
    repository_probe_runner: CommandRunner,
) -> None:
    commit = _fetch_locked_commit(seed, staging, runner, repository_probe_runner)
    _verify_fetched_commit(seed, commit)
    tree = _run_checked(runner, ("git", "rev-parse", f"{seed.commit}^{{tree}}"), staging).strip()
    if tree != seed.tree_hash:
        raise SourceIdentityError(
            f"commit tree {tree!r} differs from locked tree {seed.tree_hash!r}"
        )


def _fetch_locked_commit(
    seed: SeedLock,
    staging: Path,
    runner: CommandRunner,
    repository_probe_runner: CommandRunner,
) -> str:
    if seed.release_tag is None:
        _fetch_locked_seed(
            runner,
            ("git", "fetch", "--no-tags", "origin", seed.commit),
            staging,
            seed,
            repository_probe_runner,
        )
        return _run_checked(runner, ("git", "rev-parse", "FETCH_HEAD^{commit}"), staging).strip()
    tag_ref = f"refs/tags/{seed.release_tag}"
    _fetch_locked_seed(
        runner,
        ("git", "fetch", "--no-tags", "origin", f"{tag_ref}:{tag_ref}"),
        staging,
        seed,
        repository_probe_runner,
    )
    return _run_checked(runner, ("git", "rev-parse", f"{tag_ref}^{{commit}}"), staging).strip()


def _verify_fetched_commit(seed: SeedLock, commit: str) -> None:
    if commit != seed.commit:
        if seed.release_tag is None:
            raise SourceIdentityError(
                f"fetched commit {commit!r} differs from locked commit {seed.commit!r}"
            )
        raise SourceIdentityError(
            f"tag {seed.release_tag!r} peeled to {commit!r}, expected locked commit {seed.commit!r}"
        )


def _checkout_verified_seed(seed: SeedLock, staging: Path, runner: CommandRunner) -> None:
    _run_checked(runner, ("git", "checkout", "--detach", seed.commit), staging)
    _run_checked(runner, ("git", "branch", "--force", "main", seed.commit), staging)
    _run_checked(runner, ("git", "checkout", "main"), staging)


def _finalize_staging(
    staging: Path,
    target: Path,
    parent_identity: ParentIdentity,
    replace_runner: ReplaceRunner | None,
) -> None:
    if ensure_target_parent(target.parent) != parent_identity:
        raise SourceError("target parent identity changed during acquisition")
    if not staging.exists() or staging.parent != target.parent:
        raise SourceError("private acquisition staging moved before finalization")
    try:
        (replace_runner or _atomic_sibling_replace)(staging, target, parent_identity)
    except OSError as exc:
        raise SourceError(
            f"verified staging checkout could not be atomically installed at {target}: {exc}"
        ) from exc


def _atomic_sibling_replace(
    source: Path,
    destination: Path,
    expected_parent: ParentIdentity,
) -> None:
    if source.parent != destination.parent:
        raise SourceError("atomic seed finalization requires sibling paths")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(source.parent, flags)
    try:
        info = os.fstat(descriptor)
        if (info.st_dev, info.st_ino) != expected_parent:
            raise SourceError("target parent identity changed before atomic replacement")
        os.replace(
            source.name,
            destination.name,
            src_dir_fd=descriptor,
            dst_dir_fd=descriptor,
        )
    finally:
        os.close(descriptor)


def _verify_origin_and_main(seed: SeedLock, checkout: Path, runner: CommandRunner) -> None:
    origin = _run_checked(runner, ("git", "remote", "get-url", "origin"), checkout).strip()
    main = _run_checked(runner, ("git", "rev-parse", "main^{commit}"), checkout).strip()
    if origin != seed.repository or main != seed.commit:
        raise SourceIdentityError(
            f"materialized checkout identity drifted: origin={origin!r}, main={main!r}"
        )


def _verify_provenance_identity(
    checkout: Path, expected_profile: str, runner: CommandRunner
) -> None:
    """Bind checked-out bytes and HEAD trailers through the shared strict verifier."""
    try:
        committed = _run_checked(runner, ("git", "show", "HEAD:PROVENANCE.json"), checkout).encode(
            "utf-8"
        )
        working = (checkout / "PROVENANCE.json").read_bytes()
    except (OSError, SourceError) as exc:
        raise SourceIdentityError(f"seed provenance is missing from committed HEAD: {exc}") from exc
    if working != committed:
        raise SourceIdentityError(
            "working PROVENANCE.json differs byte-for-byte from committed HEAD"
        )
    try:
        stamp = parse_provenance_v1(committed)
    except ProvenanceV1Error as exc:
        raise SourceIdentityError(f"seed provenance is malformed: {exc}") from exc
    if stamp.bundle_name != expected_profile:
        raise SourceIdentityError(
            "seed provenance bundle/profile identity mismatch: "
            f"bundle={stamp.bundle_name!r}, locked_profile={expected_profile!r}"
        )
    message = _run_checked(runner, ("git", "show", "-s", "--format=%B", "HEAD"), checkout)
    try:
        verify_seal_trailers(stamp, _trailer_values(message))
    except ProvenanceV1Error as exc:
        raise SourceIdentityError(f"seed seal trailers are malformed: {exc}") from exc


def _trailer_values(message: str) -> dict[str, list[str]]:
    values: dict[str, list[str]] = {
        "Subject": [message.splitlines()[0]] if message.splitlines() else []
    }
    for line in message.splitlines()[1:]:
        if ": " not in line:
            continue
        key, value = line.split(": ", 1)
        if key in {
            "Seed-Id",
            "Origin-Id",
            "Manifest-SHA256",
            "Assembled-Ref",
            "License-Policy",
            "Minted-At",
            "Lineage-Parent",
        }:
            values.setdefault(key, []).append(value)
    return values


def _run_checked(
    runner: CommandRunner,
    command: Sequence[str],
    cwd: Path | None,
) -> str:
    try:
        result = runner(command, cwd, _GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"could not run {command[0]} {command[1]}: {exc}") from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()[-500:]
        raise SourceError(
            f"{command[0]} {command[1]} failed with exit {result.returncode}: {detail}"
        )
    return result.stdout


def _fetch_locked_seed(
    runner: CommandRunner,
    command: Sequence[str],
    cwd: Path,
    seed: SeedLock,
    repository_probe_runner: CommandRunner,
) -> str:
    try:
        result = runner(command, cwd, _GIT_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SourceError(f"could not run {command[0]} {command[1]}: {exc}") from exc
    if result.returncode == 0:
        return result.stdout
    detail = (result.stderr or result.stdout).strip()[-500:]
    if result.final_http_status in _ACCESS_ELIGIBLE_HTTP_STATUSES:
        reason = _repository_access_reason(seed.repository, repository_probe_runner)
        if reason is not None:
            raise _repository_access_error(seed, reason)
    raise SourceError(f"{command[0]} {command[1]} failed with exit {result.returncode}: {detail}")


def _final_git_http_status(trace_path: Path) -> int | None:
    try:
        lines = trace_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    statuses: list[int] = []
    for line in lines:
        _, marker, header = line.partition(_GIT_CURL_RESPONSE_HEADER)
        if marker == "":
            continue
        status = _response_status(header.strip())
        if status is not None:
            statuses.append(status)
    return statuses[-1] if statuses else None


def _repository_access_reason(
    repository: str,
    runner: CommandRunner,
) -> RepositoryAccessReason | None:
    identity = _github_repository_identity(repository)
    if identity is None:
        return None
    owner, name = identity
    conclusive, reason = _authenticated_repository_reason(owner, name, runner)
    if conclusive:
        return reason
    return _anonymous_repository_reason(owner, name, runner)


def _authenticated_repository_reason(
    owner: str,
    name: str,
    runner: CommandRunner,
) -> tuple[bool, RepositoryAccessReason | None]:
    authenticated = _probe(
        runner,
        ("gh", "auth", "status", "--hostname", "github.com", "--active"),
    )
    if authenticated is None or authenticated.returncode != 0:
        return False, None
    metadata = _probe(runner, ("gh", "api", "--include", f"repos/{owner}/{name}"))
    if metadata is None:
        return False, None
    return _authenticated_metadata_reason(owner, metadata, runner)


def _authenticated_metadata_reason(
    owner: str,
    metadata: RunResult,
    runner: CommandRunner,
) -> tuple[bool, RepositoryAccessReason | None]:
    status = _response_status(metadata.stdout)
    if status == 200:
        visibility = _response_visibility(metadata.stdout)
        if visibility == "private":
            return True, RepositoryAccessReason.PRIVATE
        if visibility == "public":
            return True, None
        return False, None
    if status != 404:
        return False, None
    viewer = _probe(runner, ("gh", "api", "user", "--jq", ".login"))
    if (
        viewer is not None
        and viewer.returncode == 0
        and viewer.stdout.strip().casefold() == owner.casefold()
    ):
        return True, RepositoryAccessReason.MISSING
    return True, RepositoryAccessReason.PRIVATE_OR_MISSING


def _anonymous_repository_reason(
    owner: str,
    name: str,
    runner: CommandRunner,
) -> RepositoryAccessReason | None:
    anonymous = _probe(
        runner,
        (
            "curl",
            "--silent",
            "--show-error",
            "--location",
            "--max-time",
            str(_REPOSITORY_PROBE_TIMEOUT_SECONDS),
            "--output",
            "/dev/null",
            "--write-out",
            "%{http_code}",
            "--header",
            "Accept: application/vnd.github+json",
            "--header",
            "X-GitHub-Api-Version: 2022-11-28",
            f"https://api.github.com/repos/{owner}/{name}",
        ),
    )
    if anonymous is None or anonymous.returncode != 0:
        return None
    status_text = anonymous.stdout.strip()
    if status_text == "200":
        return None
    if status_text == "404":
        return RepositoryAccessReason.PRIVATE_OR_MISSING
    return None


def _probe(runner: CommandRunner, command: Sequence[str]) -> RunResult | None:
    try:
        return runner(command, None, _REPOSITORY_PROBE_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired):
        return None


def _github_repository_identity(repository: str) -> tuple[str, str] | None:
    match = _GITHUB_REPOSITORY.fullmatch(repository)
    if match is None:
        return None
    return match.group("owner"), match.group("repository")


def _response_status(response: str) -> int | None:
    first_line = response.splitlines()[0] if response else ""
    match = _HTTP_STATUS.match(first_line)
    return int(match.group("status")) if match is not None else None


def _response_visibility(response: str) -> str | None:
    parts = re.split(r"\r?\n\r?\n", response, maxsplit=1)
    if len(parts) != 2:
        return None
    try:
        payload: object = json.loads(parts[1])
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, dict):
        return None
    visibility = cast(dict[str, object], payload).get("visibility")
    return visibility if isinstance(visibility, str) else None


def _repository_access_error(
    seed: SeedLock,
    reason: RepositoryAccessReason,
) -> SeedRepositoryAccessError:
    prefix = (
        f"source acquisition fetch refused [reason={reason.value}]: locked seed "
        f"{seed.profile!r} at {_seed_identity_label(seed)!r}"
    )
    if reason is RepositoryAccessReason.PRIVATE:
        return SeedRepositoryAccessError(
            f"{prefix} uses an existing private repository that is not anonymously "
            f"readable: {seed.repository}",
            reason=reason,
            repair=(
                "Obtain authorized Git access through the operator's intended Git "
                "mechanism, then rerun the same create command. Alternatively, rerun "
                "with `--seed <name>` or `--seed-lock <path>` selecting a reviewed "
                "public seed; the manager will not change repository visibility."
            ),
        )
    if reason is RepositoryAccessReason.MISSING:
        return SeedRepositoryAccessError(
            f"{prefix} names a repository that is missing or has not been published "
            f"and therefore cannot be read anonymously: {seed.repository}",
            reason=reason,
            repair=(
                "Ask the publisher to publish the exact locked seed or correct the "
                "repository reference, then rerun. Alternatively, use `--seed <name>` "
                "or `--seed-lock <path>` to select a reviewed public seed. Intentional "
                "private operation requires a valid existing private repository and "
                "authorized Git access."
            ),
        )
    return SeedRepositoryAccessError(
        f"{prefix} is not anonymously visible; without authorized repository metadata, "
        f"GitHub cannot distinguish an existing private repository from one that is "
        f"missing or not published: {seed.repository}",
        reason=reason,
        repair=(
            "For a public seed, ask the publisher to publish it or correct the "
            "repository reference, then retry; or select a reviewed public seed with "
            "`--seed <name>` or `--seed-lock <path>`. For an intentionally private "
            "seed, first verify that the repository exists and obtain authorized Git "
            "access, then rerun the same create command."
        ),
    )


def _seed_identity_label(seed: SeedLock) -> str:
    return seed.release_tag if seed.release_tag is not None else f"commit:{seed.commit}"
