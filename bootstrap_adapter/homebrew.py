"""Fail-closed Homebrew package mutation boundary for bootstrap routes."""

from __future__ import annotations

import os
import pwd
import re
import subprocess
import time
from dataclasses import dataclass, field

from .models import INSTALL_TIMEOUT_SECONDS, AdapterError, AdapterRuntime

_HOMEBREW_GUARD_ENV = {
    "HOMEBREW_NO_AUTO_UPDATE": "1",
    "HOMEBREW_NO_COLOR": "1",
    "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
    "HOMEBREW_NO_INSTALL_UPGRADE": "1",
}
_HOMEBREW_INSTALL_MUTATION_ENV = {
    "HOMEBREW_NO_INSTALL_CLEANUP": "1",
}
_OUTPUT_LIMIT = 16_384
_VERSIONED_PACKAGE_ITEM = re.compile(r"^(?P<name>[a-z0-9][a-z0-9@._+/-]*)(?:\s+[0-9][A-Za-z0-9@._+/-]*)?$")
_PACKAGE_HEADER = re.compile(r"^Would install (?P<count>[1-9][0-9]*) (?P<kind>cask|casks|formula|formulas|formulae):$")
_DEPENDENCY_HEADER = re.compile(r"^Would install (?P<count>[1-9][0-9]*) dependenc(?:y|ies) for (?P<parent>.+):$")
_UPGRADE_DEPENDENCY_HEADER = re.compile(
    r"^Would upgrade (?P<count>[1-9][0-9]*) dependenc(?:y|ies) for (?P<parent>.+):$"
)
# New dependency names require review; a package-looking action line is not proof.
_REVIEWED_INSTALL_DEPENDENCIES: dict[tuple[str, str], frozenset[str]] = {
    ("formula", "postgresql@17"): frozenset({"krb5", "readline"}),
    # r53 macOS 26 guest receipt (iss_6130e3f2): llama.cpp 0.4.0 bottles depend on these.
    ("formula", "llama.cpp"): frozenset({"libomp", "ggml"}),
}
_KNOWN_PLAN_INFORMATION = frozenset(
    {
        "Warning: `$HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK` is set: not checking for outdated",
        "dependents or dependents with broken linkage!",
    }
)
_KNOWN_PLAN_INFORMATION_PATTERNS = (
    re.compile(r"^Downloading https?://\S+$"),
    re.compile(r"^Already downloaded: /.+$"),
    re.compile(r"^codex-cli [0-9]+(?:\.[0-9]+){1,3}$"),
    re.compile(r"^[0-9]+(?:\.[0-9]+){1,3} \(Claude Code\)$"),
)


@dataclass(frozen=True)
class CommandOutcome:
    """The bounded public result of one required host command."""

    returncode: int | None
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str
    output_complete: bool = True


class CommandExecutionError(AdapterError):
    """A required command failure whose public diagnostics remain available."""

    def __init__(self, message: str, *, outcome: CommandOutcome) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.stdout = outcome.stdout
        self.stderr = outcome.stderr


class HomebrewInstallError(CommandExecutionError):
    """A Homebrew package failure that retains its command outcome."""

    def __init__(
        self,
        message: str,
        *,
        outcome: CommandOutcome,
        blocked_upgrades: tuple[str, ...] = (),
        unrecognized_line: str | None = None,
    ) -> None:
        if unrecognized_line is not None:
            excerpt = unrecognized_line[:160]
            if len(unrecognized_line) > 160:
                excerpt += "…"
            message = f"{message}; unrecognized dry-run line {excerpt!r}"
        super().__init__(message, outcome=outcome)
        self.blocked_upgrades = blocked_upgrades
        self.unrecognized_line = unrecognized_line


class _UnrecognizedPlanLineError(ValueError):
    """One complete preview line is outside the reviewed Homebrew grammar."""

    def __init__(self, line: str) -> None:
        super().__init__(line)
        self.line = line


@dataclass(frozen=True)
class _PlanInspection:
    exact: bool
    unrecognized_line: str | None = None


@dataclass
class _PlanAccumulator:
    package_blocks: list[list[str]] = field(default_factory=list)
    dependency_blocks: list[tuple[str, list[str]]] = field(default_factory=list)
    block_counts: list[tuple[list[str], int]] = field(default_factory=list)
    current_items: list[str] | None = None
    current_parent: str | None = None

    def begin(self, line: str, *, kind: str, package: str, reviewed: frozenset[str]) -> None:
        block = _plan_block_header(line, kind=kind)
        if block is None:
            raise _UnrecognizedPlanLineError(line)
        items, declared_count, parent = block
        if parent is not None and parent not in {package, *reviewed}:
            raise _UnrecognizedPlanLineError(line)
        self.current_items = items
        self.current_parent = parent
        self.block_counts.append((items, declared_count))
        if parent is None:
            self.package_blocks.append(items)
        else:
            self.dependency_blocks.append((parent, items))

    def add_item(self, line: str, *, package: str, reviewed: frozenset[str]) -> None:
        if self.current_items is None:
            raise _UnrecognizedPlanLineError(line)
        name = _plan_item_name(line)
        allowed = {package} if self.current_parent is None else reviewed
        if name is None or name not in allowed:
            raise _UnrecognizedPlanLineError(line)
        self.current_items.append(name)


def _outcome(
    completed: subprocess.CompletedProcess[str] | None,
    *,
    started: float,
    timed_out: bool = False,
) -> CommandOutcome:
    """Bound public diagnostics while retaining whether the plan was complete."""

    return CommandOutcome(
        returncode=None if completed is None else completed.returncode,
        timed_out=timed_out,
        duration_ms=max(0, int((time.monotonic() - started) * 1000)),
        stdout="" if completed is None else completed.stdout[:_OUTPUT_LIMIT],
        stderr="" if completed is None else completed.stderr[:_OUTPUT_LIMIT],
        output_complete=completed is not None
        and len(completed.stdout) <= _OUTPUT_LIMIT
        and len(completed.stderr) <= _OUTPUT_LIMIT,
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
    environment.pop("HOMEBREW_COLOR", None)
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

    return [line.strip().removeprefix("==> ").strip() for line in output.split("\n")]


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


def _known_plan_information(line: str) -> bool:
    """Allow blanks and informational lines observed in reviewed dry-run receipts."""

    return not line or line in _KNOWN_PLAN_INFORMATION or any(
        pattern.fullmatch(line) is not None for pattern in _KNOWN_PLAN_INFORMATION_PATTERNS
    )


def _plan_item_name(line: str) -> str | None:
    """Parse the item shape; the requested package or review set supplies identity."""

    package_item = _VERSIONED_PACKAGE_ITEM.fullmatch(line)
    return None if package_item is None else package_item["name"]


def _homebrew_install_items(
    lines: list[str], *, kind: str, package: str
) -> tuple[list[list[str]], list[tuple[str, list[str]]]] | None:
    """Accept only requested or reviewed items; expose every unknown line."""

    state = _PlanAccumulator()
    reviewed_dependencies = _REVIEWED_INSTALL_DEPENDENCIES.get((kind, package), frozenset())
    for line in lines:
        if line.startswith("Would install"):
            state.begin(line, kind=kind, package=package, reviewed=reviewed_dependencies)
            continue
        if _known_plan_information(line):
            continue
        state.add_item(line, package=package, reviewed=reviewed_dependencies)
    if any(len(items) != declared_count for items, declared_count in state.block_counts):
        return None
    return state.package_blocks, state.dependency_blocks


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


def _inspect_homebrew_plan(output: str, *, kind: str, package: str) -> _PlanInspection:
    if kind not in {"cask", "formula"}:
        return _PlanInspection(False)
    unsafe_line = _unsafe_plan_line(output)
    if unsafe_line is not None:
        return _PlanInspection(False, unsafe_line)
    try:
        parsed = _homebrew_install_items(_normalized_plan_lines(output), kind=kind, package=package)
    except _UnrecognizedPlanLineError as exc:
        return _PlanInspection(False, exc.line)
    if parsed is None:
        return _PlanInspection(False)
    package_blocks, dependency_blocks = parsed
    if not package_blocks or any(items != [package] for items in package_blocks):
        return _PlanInspection(False)
    return _PlanInspection(_dependency_closure_is_exact(package, dependency_blocks))


def _homebrew_plan_is_exact(output: str, *, kind: str, package: str) -> bool:
    return _inspect_homebrew_plan(output, kind=kind, package=package).exact


def _has_plan_control_characters(output: str) -> bool:
    """Reject controls and line separators before parsing can erase them."""

    return any(
        (ord(char) < 32 and char != "\n")
        or 127 <= ord(char) <= 159
        or char in "\u2028\u2029"
        for char in output
    )


def _unsafe_plan_line(output: str) -> str | None:
    """Return the first unsafe heading or decorated line for bounded reporting."""

    if _has_plan_control_characters(output):
        return next(line for line in output.split("\n") if _has_plan_control_characters(line))
    for raw_line in output.split("\n"):
        line = raw_line.strip()
        if line.startswith("==> ") and not line.startswith(("==> Would install ", "==> Downloading ")):
            return line
        normalized = line.removeprefix("==> ")
        if re.search(r"\bwould\b", normalized, flags=re.IGNORECASE) and not normalized.startswith("Would install "):
            return line
    return None


def _upgrade_block_items(lines: list[str], start: int, declared_count: int) -> list[str] | None:
    items: list[str] = []
    for line in lines[start:]:
        if line.startswith("Would "):
            break
        item = _VERSIONED_PACKAGE_ITEM.fullmatch(line)
        if item is not None:
            items.append(item["name"])
    return items if len(items) == declared_count else None


def _blocked_dependency_upgrades(output: str, *, package: str) -> tuple[str, ...]:
    """Name only well-formed upgrade blocks attributed to this package."""

    if _has_plan_control_characters(output):
        return ()
    lines = _normalized_plan_lines(output)
    upgrades: list[str] = []
    for index, line in enumerate(lines):
        if not line.startswith("Would upgrade"):
            continue
        match = _UPGRADE_DEPENDENCY_HEADER.fullmatch(line)
        if match is None or match["parent"] != package:
            return ()
        items = _upgrade_block_items(lines, index + 1, int(match["count"]))
        if items is None:
            return ()
        upgrades.extend(items)
    return tuple(dict.fromkeys(upgrades))


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


def _dry_run_is_verified_noop(outcome: CommandOutcome, *, installed: bool) -> bool:
    """Accept truly empty successful streams only with fresh package-state proof."""

    return (
        outcome.returncode == 0
        and outcome.output_complete
        and outcome.stdout == ""
        and outcome.stderr == ""
        and installed
    )


def _inspect_dry_run(outcome: CommandOutcome, output: str, *, kind: str, package: str) -> _PlanInspection:
    """Only a successful complete receipt can authorize a package mutation."""

    if outcome.returncode != 0 or not outcome.output_complete:
        return _PlanInspection(False)
    if _homebrew_plan_is_exact(output, kind=kind, package=package):
        return _PlanInspection(True)
    return _inspect_homebrew_plan(output, kind=kind, package=package)


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
    plan_output = f"{dry_run_outcome.stdout}\n{dry_run_outcome.stderr}"
    inspection = _inspect_dry_run(dry_run_outcome, plan_output, kind=kind, package=package)
    if not inspection.exact:
        installed = _homebrew_package_is_installed(
            runtime,
            brew,
            package_args,
            label,
            environment,
        )
        if _dry_run_is_verified_noop(dry_run_outcome, installed=installed):
            return dry_run_outcome
        raise HomebrewInstallError(
            f"{label} dry-run proposed an unapproved package mutation",
            outcome=dry_run_outcome,
            blocked_upgrades=(
                _blocked_dependency_upgrades(plan_output, package=package)
                if dry_run_outcome.output_complete
                else ()
            ),
            unrecognized_line=inspection.unrecognized_line,
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
