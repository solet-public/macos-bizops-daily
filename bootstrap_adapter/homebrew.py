"""Fail-closed Homebrew package mutation boundary for bootstrap routes."""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import time
from dataclasses import dataclass

from .models import INSTALL_TIMEOUT_SECONDS, AdapterError, AdapterRuntime

_HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}
_HOMEBREW_INSTALL_MUTATION_ENV = {
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
}
_OUTPUT_LIMIT = 16_384
_VERSIONED_PACKAGE_ITEM = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9@._+/-]*)(?:\s+[0-9][A-Za-z0-9@._+/-]*)?$")
_PACKAGE_HEADER = re.compile(r"^Would install (?P<count>[1-9][0-9]*) (?P<kind>cask|casks|formula|formulas|formulae):$")
_DEPENDENCY_HEADER = re.compile(r"^Would install (?P<count>[1-9][0-9]*) dependenc(?:y|ies) for (?P<parent>.+):$")


@dataclass(frozen=True)
class CommandOutcome:
    """The bounded public result of one required host command."""

    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str


class CommandExecutionError(AdapterError):
    """A required command failure whose public diagnostics remain available."""

    def __init__(self, message: str, *, outcome: CommandOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.stdout = outcome.stdout
        self.stderr = outcome.stderr


class HomebrewInstallError(CommandExecutionError):
    """A Homebrew package failure that retains its command outcome."""


def _outcome(
    completed: subprocess.CompletedProcess[str] | None,
    *,
    started: float,
    timed_out: bool = False,
) -> CommandOutcome:
    """Bound command output once, before it can enter a result envelope."""

    return CommandOutcome(
        returncode=None if completed is None else completed.returncode,
        timed_out=timed_out,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        stdout="" if completed is None else completed.stdout[:_OUTPUT_LIMIT],
        stderr="" if completed is None else completed.stderr[:_OUTPUT_LIMIT],
    )


def _run_homebrew_command(
    runtime: AdapterRuntime,
    command: list[str],
    *,
    environment: dict[str, str],
) -> CommandOutcome:
    """Run one guarded Homebrew command and retain its bounded public receipt."""

    started = time.monotonic()
    try:
        completed = runtime.run(
            command,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            env=environment,
        )
    except subprocess.TimeoutExpired:
        return _outcome(None, started=started, timed_out=True)
    except OSError:
        return _outcome(None, started=started)
    return _outcome(completed, started=started)


def _require_homebrew_command_outcome(
    outcome: CommandOutcome,
    *,
    label: str,
    phase: str,
) -> None:
    """Turn an unavailable guarded command into a receipt-bearing failure."""

    if outcome.timed_out:
        raise HomebrewInstallError(f"{label} {phase} timed out", outcome=outcome)
    if outcome.returncode is None:
        raise HomebrewInstallError(f"{label} {phase} could not execute", outcome=outcome)


def homebrew_guard_environment() -> dict[str, str]:
    """Preserve Homebrew policy and bind the guard to the target account home."""

    environment = {key: value for key, value in os.environ.items() if key.startswith("HOMEBREW_")}
    environment.update(_HOMEBREW_GUARD_ENV)
    try:
        home = pwd.getpwuid(os.getuid()).pw_dir
    except KeyError as exc:
        raise AdapterError("Homebrew guard could not resolve the target account home") from exc
    if not home or not os.path.isabs(home):
        raise AdapterError("Homebrew guard resolved an invalid target account home")
    environment["HOME"] = home
    return environment


def _normalized_plan_lines(output: str) -> list[str]:
    """Remove Homebrew's informational prefix before interpreting plan headings."""

    return [line.strip().removeprefix("==> ").strip() for line in output.splitlines()]


def _plan_block_header(line: str, *, kind: str) -> tuple[list[str], int, str | None] | None:
    """Return a recognized package or dependency heading's empty item block."""

    package_header = _PACKAGE_HEADER.fullmatch(line)
    if package_header is not None:
        header_kind = package_header["kind"].rstrip("s")
        if header_kind == "formulae":
            header_kind = "formula"
        if header_kind != kind:
            return None
        return [], int(package_header["count"]), None
    dependency_header = _DEPENDENCY_HEADER.fullmatch(line)
    if dependency_header is None:
        return None
    return [], int(dependency_header["count"]), dependency_header["parent"]


def _homebrew_install_items(lines: list[str], *, kind: str) -> tuple[list[list[str]], list[tuple[str, list[str]]]] | None:
    """Extract requested-package blocks and their declared dependency blocks."""

    package_blocks: list[list[str]] = []
    dependency_blocks: list[tuple[str, list[str]]] = []
    block_counts: list[tuple[list[str], int]] = []
    current_items: list[str] | None = None
    for line in lines:
        if line.startswith("Would install"):
            block = _plan_block_header(line, kind=kind)
            if block is None:
                return None
            current_items, declared_count, parent = block
            block_counts.append((current_items, declared_count))
            if parent is None:
                package_blocks.append(current_items)
            else:
                dependency_blocks.append((parent, current_items))
            continue
        if current_items is not None:
            package_item = _VERSIONED_PACKAGE_ITEM.fullmatch(line)
            if package_item is not None:
                current_items.append(package_item["name"])
    if any(len(items) != declared_count for items, declared_count in block_counts):
        return None
    return package_blocks, dependency_blocks


def _dependency_closure_is_exact(package: str, dependency_blocks: list[tuple[str, list[str]]]) -> bool:
    """Require every declared dependency block to be reachable from the request."""

    approved = {package}
    pending = list(dependency_blocks)
    while pending:
        unresolved = [(parent, items) for parent, items in pending if parent not in approved]
        for parent, dependencies in pending:
            if parent in approved:
                approved.update(dependencies)
        if len(unresolved) == len(pending):
            return False
        pending = unresolved
    return True


def _homebrew_plan_is_exact(output: str, *, kind: str, package: str) -> bool:
    lines = _normalized_plan_lines(output)
    prohibited = ("Would upgrade", "Would reinstall", "Would remove", "Would unlink")
    if kind not in {"cask", "formula"} or any(line.startswith(prohibited) for line in lines):
        return False
    parsed = _homebrew_install_items(lines, kind=kind)
    if parsed is None:
        return False
    package_blocks, dependency_blocks = parsed
    if not package_blocks or any(items != [package] for items in package_blocks):
        return False
    return _dependency_closure_is_exact(package, dependency_blocks)


def _homebrew_package_is_installed(
    runtime: AdapterRuntime,
    brew: str,
    package_args: tuple[str, ...],
    label: str,
    environment: dict[str, str],
) -> bool:
    """Confirm that a failed install request already reached its desired state."""

    try:
        completed = runtime.run(
            [brew, "list", "--versions", *package_args],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise AdapterError(f"{label} installed-state check could not execute") from exc
    return completed.returncode == 0 and bool(completed.stdout.strip())


def run_homebrew_install_required(
    runtime: AdapterRuntime,
    brew: str,
    package: str,
    label: str,
    *,
    kind: str = "formula",
) -> CommandOutcome:
    """Fail before apply when Homebrew proposes collateral package mutation."""

    if kind not in {"cask", "formula"}:
        raise AdapterError(f"{label} has an unreviewed Homebrew package kind")
    package_args = ("--cask", package) if kind == "cask" else (package,)
    environment = homebrew_guard_environment()
    dry_run_outcome = _run_homebrew_command(
        runtime,
        [brew, "install", "--dry-run", *package_args],
        environment=environment,
    )
    _require_homebrew_command_outcome(dry_run_outcome, label=label, phase="dry-run")
    if dry_run_outcome.returncode != 0 or not _homebrew_plan_is_exact(f"{dry_run_outcome.stdout}\n{dry_run_outcome.stderr}", kind=kind, package=package):
        if _homebrew_package_is_installed(
            runtime,
            brew,
            package_args,
            label,
            environment,
        ):
            return dry_run_outcome
        raise HomebrewInstallError(
            f"{label} dry-run proposed an unapproved package mutation",
            outcome=dry_run_outcome,
        )
    mutation_environment = {**environment, **_HOMEBREW_INSTALL_MUTATION_ENV}
    outcome = _run_homebrew_command(
        runtime,
        [brew, "install", *package_args],
        environment=mutation_environment,
    )
    _require_homebrew_command_outcome(outcome, label=label, phase="install")
    if outcome.returncode != 0:
        if _homebrew_package_is_installed(
            runtime,
            brew,
            package_args,
            label,
            environment,
        ):
            return outcome
        raise HomebrewInstallError(
            f"{label} failed (exit {outcome.returncode})",
            outcome=outcome,
        )
    return outcome
