"""Host-software checks: ``missing`` versus ``unknown``, shared by the doctor and the source preview (Step 7 section 7).

Governing section 5.A says missing software is ``missing``, not ``failed``, and
section 12 says the diagnosis must distinguish missing from unknown.  Before
Step 7 the existing-install path had no Manager-owned host probe: an absent
host Python and an absent instance Python both surfaced as ``dependency_closure:
unknown (adapter_missing)`` -- the probe could not run -- never as ``missing``.

The six checks here read the host only through ``RuntimeSeams``
(``resolve_base_python``, ``which``): a probe that returns ``None`` after asking
every candidate is ``missing``; one that raises is ``unknown``; nothing here
executes a found binary (the resolver runs ``python3.13 --version`` and nothing
else).  ``host_python_313`` is the one Manager requirement no runtime stage can
repair, so the source preview refuses on it before the forward-only boundary
(section 7.2); ``instance_python`` and ``instance_bridge_cli`` are repaired by
the dependencies stage and are disclosed, and the three advisory rows exist
because the operator guide's restart/fleet features depend on them.

The update preview adds one more row the doctor does not carry,
``claude_cli_present`` (iss_646b54b6): the runtime stage's plugin-cache refresh
needs the Claude Code CLI on every existing install and no stage installs it, so
the source preview refuses ``claude_cli_missing`` before the forward-only boundary
instead of after it.  It resolves ``claude`` exactly as the seed adapter does:
``PATH`` first, then the two Homebrew bin directories, then ``$HOME/.local/bin`` where Claude Code's native installer puts it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, replace
from pathlib import Path

from .existing_solet_diagnostics import DiagnosticCheck, DiagnosticStatus
from .models import JsonValue
from .update_runtime_plan import RuntimeSeams

__all__ = ["HOST_REQUIREMENT_CHECKS", "UPDATE_PREREQUISITE_CHECKS", "HostCheck", "base_python_or_none", "claude_search_directories", "host_checks", "host_requirement_reason", "host_section", "update_prerequisite_checks"]

_HOMEBREW_CANDIDATES = (Path("/opt/homebrew/bin/brew"), Path("/usr/local/bin/brew"))
_PSQL_GLOB = "/opt/homebrew/opt/postgresql@*/bin/psql"
#: The checks the source preview refuses on when they are not ``verified`` (section 7.2, D3).
HOST_REQUIREMENT_CHECKS = ("host_python_313",)
#: Update-only prerequisites the source preview refuses on by their own reason (iss_646b54b6); the doctor omits them.
UPDATE_PREREQUISITE_CHECKS = ("claude_cli_present",)
#: The seed adapter's ``resolve_executable`` fixed directories (``setup_adapter_runtime.executable_fallback_directories``):
#: the two Homebrew bins, then Claude Code's native-install directory under the invoking user's home.
_HOMEBREW_BIN_DIRECTORIES = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))
_NATIVE_BIN_RELATIVE = Path(".local") / "bin"


@dataclass(frozen=True, slots=True)
class HostCheck:
    """One host-software row: closed status, reason, source and what was observed."""

    check_id: str
    status: DiagnosticStatus
    reason: str | None
    source: str
    observed: JsonValue
    summary: str

    def to_dict(self) -> dict[str, JsonValue]:
        return {"check_id": self.check_id, "status": self.status.value, "reason": self.reason, "source": self.source, "observed": self.observed}

    def to_check(self) -> DiagnosticCheck:
        if self.status is DiagnosticStatus.VERIFIED:
            return DiagnosticCheck(self.check_id, self.status, self.summary, None, None, self.observed, None, self.source)
        repair = "install_host_software" if self.status is DiagnosticStatus.MISSING else "operator_review"
        return DiagnosticCheck(self.check_id, self.status, self.summary, self.reason or self.status.value, repair, self.observed, None, self.source)


def base_python_or_none(seams: RuntimeSeams) -> Path | None:
    """The resolved host Python for an adapter registry; a probe that raises is ``None`` here and ``unknown`` in the host rows."""
    try:
        return seams.resolve_base_python()
    except (OSError, TimeoutError):
        return None


def host_checks(seams: RuntimeSeams, target: Path) -> tuple[HostCheck, ...]:
    """The six section-7.1 rows in table order."""
    return (
        _host_python(seams),
        _instance_python(target),
        _instance_bridge_cli(target),
        _which("homebrew_present", seams, "brew", _HOMEBREW_CANDIDATES, "Homebrew is not a Manager requirement on the existing-install path."),
        _which("tmux_present", seams, "tmux", (), "tmux is not a Manager requirement; fleet features depend on it."),
        _which("postgresql_client_present", seams, "psql", tuple(sorted(Path("/").glob(_PSQL_GLOB.lstrip("/")))), "psql is not a Manager requirement; presence is not database policy."),
    )


def claude_search_directories(seams: RuntimeSeams) -> tuple[Path, ...]:
    """The fixed directories ``claude`` is looked for after PATH, in order: both Homebrew bins, then ``$HOME/.local/bin``."""
    return (*_HOMEBREW_BIN_DIRECTORIES, seams.home / _NATIVE_BIN_RELATIVE)


def update_prerequisite_checks(seams: RuntimeSeams) -> tuple[HostCheck, ...]:
    """The update-only rows (iss_646b54b6), appended to the preview's host group after the six section-7.1 rows."""
    row = _which("claude_cli_present", seams, "claude", tuple(directory / "claude" for directory in claude_search_directories(seams)), "The runtime stage's plugin-cache refresh needs the Claude Code CLI; the update never installs it.")
    if row.status is DiagnosticStatus.MISSING:
        row = replace(row, reason="claude_cli_missing")
    return (row,)


def host_section(seams: RuntimeSeams, target: Path) -> dict[str, JsonValue]:
    """The preview's ``host`` group (section 7.2): every row with its status, plus the refusal it implies."""
    checks = (*host_checks(seams, target), *update_prerequisite_checks(seams))
    reason = host_requirement_reason(checks)
    return {"checks": [check.to_dict() for check in checks], "requirement": reason}


def host_requirement_reason(checks: tuple[HostCheck, ...]) -> str | None:
    """The refusal the first unverified requirement row implies, else ``None``.

    ``host_requirement_missing`` / ``host_requirement_unknown`` for the host Python; ``claude_cli_missing`` /
    ``host_requirement_unknown`` for an update prerequisite row, when the rows include it.
    """
    by_id = {check.check_id: check for check in checks}
    for check_id in HOST_REQUIREMENT_CHECKS + tuple(item for item in UPDATE_PREREQUISITE_CHECKS if item in by_id):
        check = by_id[check_id]
        if check.status is DiagnosticStatus.MISSING:
            return "host_requirement_missing" if check_id in HOST_REQUIREMENT_CHECKS else check.reason
        if check.status is DiagnosticStatus.UNKNOWN:
            return "host_requirement_unknown"
    return None


def _host_python(seams: RuntimeSeams) -> HostCheck:
    source = "manager_host:resolve_base_python"
    try:
        resolved = seams.resolve_base_python()
    except (OSError, TimeoutError) as exc:
        return HostCheck("host_python_313", DiagnosticStatus.UNKNOWN, "host_probe_unqueryable", source, str(exc), "The host Python 3.13 probe could not run.")
    if resolved is None:
        return HostCheck("host_python_313", DiagnosticStatus.MISSING, "host_python_313_absent", source, None, "No Python 3.13 outside the Manager keg answered; `brew install python@3.13` is an operator action outside the Manager.")
    return HostCheck("host_python_313", DiagnosticStatus.VERIFIED, None, source, str(resolved), "A Python 3.13 outside the Manager keg resolved.")


def _instance_python(target: Path) -> HostCheck:
    path = target / ".venv" / "bin" / "python3"
    source = "manager_static"
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return HostCheck("instance_python", DiagnosticStatus.MISSING, "instance_interpreter_absent", source, {"path": str(path), "dangling": False}, "The instance interpreter is absent; the dependencies stage rebuilds the venv.")
    except OSError as exc:
        return HostCheck("instance_python", DiagnosticStatus.UNKNOWN, "instance_interpreter_unqueryable", source, str(exc), "The instance interpreter could not be stat'ed.")
    if stat.S_ISLNK(info.st_mode):
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return HostCheck("instance_python", DiagnosticStatus.MISSING, "instance_interpreter_absent", source, {"path": str(path), "dangling": True}, "The instance interpreter symlink dangles (the post-`brew upgrade python@3.13` shape); the dependencies stage rebuilds the venv.")
        if not resolved.is_file():
            return HostCheck("instance_python", DiagnosticStatus.MISSING, "instance_interpreter_absent", source, {"path": str(path), "dangling": True}, "The instance interpreter symlink does not resolve to a regular file.")
    elif not stat.S_ISREG(info.st_mode):
        return HostCheck("instance_python", DiagnosticStatus.MISSING, "instance_interpreter_absent", source, {"path": str(path), "dangling": False}, "The instance interpreter is not a regular file.")
    if not (target / ".venv" / "pyvenv.cfg").is_file():
        return HostCheck("instance_python", DiagnosticStatus.MISSING, "instance_interpreter_absent", source, {"path": str(path), "dangling": False, "pyvenv_cfg": False}, "The instance venv carries no pyvenv.cfg.")
    return HostCheck("instance_python", DiagnosticStatus.VERIFIED, None, source, {"path": str(path), "dangling": False}, "The instance interpreter is present.")


def _instance_bridge_cli(target: Path) -> HostCheck:
    path = target / ".venv" / "bin" / "solet-bridge"
    source = "manager_static"
    try:
        info = os.stat(path)
    except FileNotFoundError:
        return HostCheck("instance_bridge_cli", DiagnosticStatus.MISSING, "instance_bridge_cli_absent", source, str(path), "The instance bridge CLI is absent; the dependencies stage reinstalls it.")
    except OSError as exc:
        return HostCheck("instance_bridge_cli", DiagnosticStatus.UNKNOWN, "instance_bridge_cli_unqueryable", source, str(exc), "The instance bridge CLI could not be stat'ed.")
    if not stat.S_ISREG(info.st_mode) or not info.st_mode & stat.S_IXUSR:
        return HostCheck("instance_bridge_cli", DiagnosticStatus.MISSING, "instance_bridge_cli_absent", source, str(path), "The instance bridge CLI is not a regular executable file.")
    return HostCheck("instance_bridge_cli", DiagnosticStatus.VERIFIED, None, source, str(path), "The instance bridge CLI is present.")


def _which(check_id: str, seams: RuntimeSeams, binary: str, candidates: tuple[Path, ...], summary: str) -> HostCheck:
    source = f"manager_host:which {binary}"
    try:
        found = seams.which(binary)
        if found is None:
            # The fixed Homebrew/PostgreSQL paths go through the same seam (``shutil.which`` accepts a path with a
            # directory component), so a fixture can measure "absent" on a host that has them.
            found = next((item for item in (seams.which(str(candidate)) for candidate in candidates) if item is not None), None)
    except (OSError, TimeoutError) as exc:
        return HostCheck(check_id, DiagnosticStatus.UNKNOWN, "host_probe_unqueryable", source, str(exc), summary)
    if found is None:
        return HostCheck(check_id, DiagnosticStatus.MISSING, f"{binary}_absent", source, None, summary)
    return HostCheck(check_id, DiagnosticStatus.VERIFIED, None, source, found, summary)
