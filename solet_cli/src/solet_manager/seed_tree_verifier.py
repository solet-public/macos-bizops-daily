"""Read-only verification of a target checkout against its sealed seed tree.

This is intentionally the baseline half of the future managed-tree verifier.
``seed-tree-baseline-v1`` establishes whether a checkout still represents its
sealed tree and names tracked deviations; ``managed-tree-v1`` will later be
able to accept those deviations only when authenticated overlay receipts cover
them.  This module neither writes the target nor interprets receipts.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from .models import JsonValue

_GIT_TIMEOUT_SECONDS = 30
_FORMAT = "seed-tree-baseline-v1"


@dataclass(frozen=True)
class GitQueryResult:
    """The bounded result of one read-only Git query."""

    returncode: int
    stdout: str
    stderr: str


type GitQueryRunner = Callable[[Sequence[str], int], GitQueryResult]


@dataclass(frozen=True)
class SeedTreeDeviation:
    """One tracked index or worktree difference from the checked-out tree."""

    location: str
    status: str
    paths: tuple[str, ...]

    def to_dict(self) -> dict[str, JsonValue]:
        paths: list[JsonValue] = list(self.paths)
        return {
            "location": self.location,
            "status": self.status,
            "paths": paths,
        }


@dataclass(frozen=True)
class SeedTreeVerification:
    """Versioned baseline evidence consumable by ``managed-tree-v1`` later."""

    expected_tree_hash: str
    observed_head_tree_hash: str | None
    deviations: tuple[SeedTreeDeviation, ...]
    query_error: str | None = None

    @property
    def baseline_matches(self) -> bool:
        return (
            self.query_error is None
            and self.observed_head_tree_hash == self.expected_tree_hash
            and not self.deviations
        )

    @property
    def status(self) -> str:
        return "verified" if self.baseline_matches else "warn"

    @property
    def reason(self) -> str:
        if self.query_error is not None:
            return "seed_tree_unverifiable"
        if self.observed_head_tree_hash != self.expected_tree_hash:
            return "seed_tree_hash_mismatch"
        return "tracked_tree_deviation" if self.deviations else "seed_tree_matches"

    def to_dict(self) -> dict[str, JsonValue]:
        """Serialize the stable baseline seam without a mutable tree digest."""

        deviations: list[JsonValue] = [item.to_dict() for item in self.deviations]
        return {
            "format": _FORMAT,
            "status": self.status,
            "reason": self.reason,
            "expected_tree_hash": self.expected_tree_hash,
            "observed_head_tree_hash": self.observed_head_tree_hash,
            "baseline_matches": self.baseline_matches,
            "deviations": deviations,
            "query_error": self.query_error,
        }


def verify_seed_tree(
    target: Path,
    expected_tree_hash: str,
    *,
    runner: GitQueryRunner | None = None,
) -> SeedTreeVerification:
    """Measure the target's sealed baseline without mutating its Git state.

    ``HEAD^{tree}`` proves the checked-out commit tree and the two diff queries
    detect staged and unstaged tracked changes that a tree-object lookup alone
    cannot see.  Untracked runtime data is intentionally outside the seed-tree
    baseline and therefore excluded here.
    """

    query = _subprocess_git_query if runner is None else runner
    head_tree = _run_query(query, _git_command(target, "rev-parse", "HEAD^{tree}"))
    if isinstance(head_tree, str):
        return _unverifiable(expected_tree_hash, head_tree)
    worktree = _run_query(query, _git_command(target, "diff", "--name-status", "-z"))
    if isinstance(worktree, str):
        return _unverifiable(expected_tree_hash, worktree, head_tree.stdout.strip())
    index = _run_query(query, _git_command(target, "diff", "--cached", "--name-status", "-z"))
    if isinstance(index, str):
        return _unverifiable(expected_tree_hash, index, head_tree.stdout.strip())
    try:
        deviations = (
            *_parse_name_status(worktree.stdout, location="worktree"),
            *_parse_name_status(index.stdout, location="index"),
        )
    except ValueError as exc:
        return _unverifiable(expected_tree_hash, f"malformed Git name-status output: {exc}")
    return SeedTreeVerification(
        expected_tree_hash=expected_tree_hash,
        observed_head_tree_hash=head_tree.stdout.strip(),
        deviations=deviations,
    )


def _git_command(target: Path, *arguments: str) -> tuple[str, ...]:
    return ("git", "-C", str(target), "--no-optional-locks", *arguments)


def _subprocess_git_query(command: Sequence[str], timeout: int) -> GitQueryResult:
    """Run a fixed read-only Git vector without inheriting Git redirection."""

    environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    try:
        completed = subprocess.run(  # noqa: S603 - command vector is closed above
            list(command),
            capture_output=True,
            check=False,
            env=environment,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return GitQueryResult(1, "", str(exc))
    return GitQueryResult(completed.returncode, completed.stdout, completed.stderr)


def _run_query(runner: GitQueryRunner, command: tuple[str, ...]) -> GitQueryResult | str:
    result = runner(command, _GIT_TIMEOUT_SECONDS)
    if result.returncode == 0:
        return result
    detail = (result.stderr or result.stdout).strip().replace("\n", " ")[-500:]
    return f"{' '.join(command[:6])} failed with exit {result.returncode}: {detail}"


def _unverifiable(
    expected_tree_hash: str,
    query_error: str,
    observed_head_tree_hash: str | None = None,
) -> SeedTreeVerification:
    return SeedTreeVerification(
        expected_tree_hash=expected_tree_hash,
        observed_head_tree_hash=observed_head_tree_hash,
        deviations=(),
        query_error=query_error,
    )


def _parse_name_status(output: str, *, location: str) -> tuple[SeedTreeDeviation, ...]:
    values = iter(field for field in output.split("\0") if field)
    deviations: list[SeedTreeDeviation] = []
    for code in values:
        if code.startswith(("R", "C")):
            paths = _next_paths(values, 2, code)
        else:
            paths = _next_paths(values, 1, code)
        deviations.append(SeedTreeDeviation(location=location, status=code, paths=paths))
    return tuple(deviations)


def _next_paths(values: Iterator[str], count: int, code: str) -> tuple[str, ...]:
    try:
        return tuple(next(values) for _ in range(count))
    except StopIteration as exc:
        raise ValueError(f"status {code!r} lacked {count} path field(s)") from exc
