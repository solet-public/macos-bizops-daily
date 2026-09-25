"""Project Solet register client for the managed-dispatch Unit mint.

``dispatch_managed_work`` is the single authorized caller that mints (or
verifies) a Project Solet Unit before any worker exists (design
``unt_57725090`` section 4).  The platform never touches Project Solet's database: the
``psolet`` CLI is the contract, exactly as in
``fleet_maintenance_plugin.register_client``.

Every failure carries a stable ``unit_*`` code.  A timeout is *uncertain*
rather than refused, because the CLI may have committed before it was killed;
the caller reconciles by the pinned ``unit_key`` before minting again.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# Mirrors ``fleet_maintenance_plugin.constants`` (PSOLET_CLI_ENV,
# DEFAULT_PSOLET_CLI, REGISTER_TIMEOUT_SECONDS).  Duplicated rather than
# cross-imported: shared contract, not a plugin-to-plugin dependency (same
# rationale as ``headless_adapter._pid_alive``).
PSOLET_CLI_ENV = "PROJECT_SOLET_CLI"
DEFAULT_PSOLET_CLI = str(Path.home() / "Workspace" / "project-solet" / ".venv" / "bin" / "psolet")
REGISTER_TIMEOUT_SECONDS = 60.0
CONFIG_PSOLET_CLI = "psolet_cli"

UNIT_DISPATCHABLE_STATES = frozenset({"dispatched", "working"})
LINK_EVENT_SCHEMA = "managed-dispatch-link/v1"
# The actor agent id is derived from the runtime solet name, never a literal.
_ACTOR_AGENT_ID_SUFFIX = "_platform"
_ACTOR_SESSION_NAME = "dispatch_managed_work"


class RegisterUnitError(RuntimeError):
    """The register refused, was unreachable, or answered uncertainly."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


@dataclass(frozen=True, slots=True)
class RegisterActor:
    """The dispatching coordinator the register attributes writes to."""

    agent_instance_id: str
    agent_session_id: str
    role_name: str
    lane_id: str


@dataclass(frozen=True, slots=True)
class MintRequest:
    """One ``psolet dispatched`` call, pinned before the first attempt."""

    lane_id: str
    brief_path: Path
    brief_sha256: str
    repository_root: Path
    brief_repository_root: Path | None
    kind: str
    model: str
    effort: str
    unit_key: str
    dispatch_source_ref: str
    addresses: tuple[str, ...]
    reference_basis: str
    reference_basis_reason: str
    actor: RegisterActor


@dataclass(frozen=True, slots=True)
class MintReceipt:
    """The CLI's verbatim JSON receipt plus the two fields the platform checks."""

    unit_id: str
    brief_target_repository_id: str
    raw: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class UnitReadback:
    """The register's current view of one Unit."""

    unit_id: str
    unit_key: str
    kind: str
    repository_id: str
    dispatch_source_ref: str
    state: str
    is_deleted: bool


class RegisterUnitClient(Protocol):
    """The register reads and writes the managed-dispatch mint needs."""

    def resolve_repository(self, root: Path) -> str: ...

    def mint_unit(self, request: MintRequest) -> MintReceipt: ...

    def find_unit_by_key(self, unit_key: str) -> UnitReadback | None: ...

    def show_unit(self, unit_id: str) -> UnitReadback: ...

    def record_unit_event(
        self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor
    ) -> None: ...

    def retire_unit(self, unit_id: str, reason: str, actor: RegisterActor) -> None: ...


def resolve_psolet_cli(configured: object) -> str:
    """Config wins, then ``PROJECT_SOLET_CLI``, then the checkout default."""
    if isinstance(configured, str) and configured.strip():
        return configured.strip()
    return os.environ.get(PSOLET_CLI_ENV, "").strip() or DEFAULT_PSOLET_CLI


def _text(row: Mapping[str, Any], field: str) -> str:
    value = row.get(field)
    return value if isinstance(value, str) else ""


def _current_state(events: object) -> str:
    """Latest ``unit_state_event.to_state``, ordered as ``db unit list`` does."""
    if not isinstance(events, list):
        return ""
    rows = [event for event in events if isinstance(event, Mapping) and not event.get("is_deleted")]
    if not rows:
        return ""
    latest = max(rows, key=lambda event: (str(event.get("observed_at")), str(event.get("created_at"))))
    return _text(latest, "to_state")


def parse_unit_show(unit_id: str, payload: object) -> UnitReadback:
    """Turn ``psolet db unit show`` JSON into a typed readback."""
    units = payload.get("unit") if isinstance(payload, Mapping) else None
    if not isinstance(units, list) or len(units) != 1 or not isinstance(units[0], Mapping):
        raise RegisterUnitError("unit_not_found", f"register has no single unit row for {unit_id}")
    unit: Mapping[str, Any] = units[0]
    return UnitReadback(
        unit_id=_text(unit, "id"),
        unit_key=_text(unit, "unit_key"),
        kind=_text(unit, "kind"),
        repository_id=_text(unit, "repository_id"),
        dispatch_source_ref=_text(unit, "dispatch_source_ref"),
        state=_current_state(payload.get("unit_state_event") if isinstance(payload, Mapping) else None),
        is_deleted=bool(unit.get("is_deleted")),
    )


class PsoletRegisterUnitClient:
    """``RegisterUnitClient`` over the project-solet CLI."""

    def __init__(
        self,
        psolet: str,
        *,
        solet_name: str,
        timeout_seconds: float = REGISTER_TIMEOUT_SECONDS,
    ) -> None:
        self._psolet = psolet
        self._solet_name = solet_name
        self._timeout = timeout_seconds

    def _run(self, *arguments: str, refused_code: str = "unit_mint_refused") -> object:
        label = " ".join(arguments[:3])
        try:
            completed = subprocess.run(
                (self._psolet, *arguments),
                capture_output=True,
                text=True,
                check=False,
                timeout=self._timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RegisterUnitError("unit_mint_uncertain", f"psolet {label} timed out: {exc}") from exc
        except OSError as exc:
            raise RegisterUnitError("unit_register_unavailable", f"psolet {label} failed to run: {exc}") from exc
        if completed.returncode != 0:
            detail = completed.stderr.strip() or completed.stdout.strip()
            raise RegisterUnitError(refused_code, f"psolet {label} exited {completed.returncode}: {detail}")
        try:
            return json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise RegisterUnitError("unit_mint_uncertain", f"psolet {label} printed non-JSON") from exc

    def _actor_agent_id(self) -> str:
        """``<solet>_platform``; blank (so omitted) when the solet name is unknown."""
        return f"{self._solet_name}{_ACTOR_AGENT_ID_SUFFIX}" if self._solet_name else ""

    def _actor_arguments(self, actor: RegisterActor) -> tuple[str, ...]:
        """Attribute to the dispatching coordinator; an unknown value is omitted, never blank."""
        pairs = (
            ("--solet", self._solet_name),
            ("--agent-instance-id", actor.agent_instance_id),
            ("--agent-session-id", actor.agent_session_id),
            ("--role-name", actor.role_name),
            ("--agent-id", self._actor_agent_id()),
            ("--session-name", _ACTOR_SESSION_NAME),
            ("--lane-id", actor.lane_id),
        )
        return tuple(item for flag, value in pairs if value for item in (flag, value))

    def resolve_repository(self, root: Path) -> str:
        payload = self._run("db", "repository", "resolve", str(root), refused_code="unit_repository_unresolved")
        repository_id = _text(payload, "repository_id") if isinstance(payload, Mapping) else ""
        if not repository_id:
            raise RegisterUnitError("unit_repository_unresolved", f"psolet resolved no repository_id for {root}")
        return repository_id

    def mint_unit(self, request: MintRequest) -> MintReceipt:
        arguments = [
            "dispatched", request.lane_id,
            "--brief", str(request.brief_path),
            "--expected-sha256", request.brief_sha256,
            "--repo", str(request.repository_root),
            *(("--brief-repo", str(request.brief_repository_root)) if request.brief_repository_root else ()),
            "--kind", request.kind,
            "--model", request.model,
            "--effort", request.effort,
            "--unit-key", request.unit_key,
            "--dispatch-source-ref", request.dispatch_source_ref,
        ]
        for issue_id in request.addresses:
            arguments.extend(("--addresses", issue_id))
        if request.reference_basis:
            arguments.extend(("--reference-basis", request.reference_basis))
        if request.reference_basis_reason:
            arguments.extend(("--reference-basis-reason", request.reference_basis_reason))
        payload = self._run(*arguments, *self._actor_arguments(request.actor))
        if not isinstance(payload, Mapping) or not _text(payload, "unit_id"):
            raise RegisterUnitError("unit_mint_uncertain", "psolet dispatched printed no unit_id")
        return MintReceipt(
            unit_id=_text(payload, "unit_id"),
            brief_target_repository_id=_text(payload, "brief_target_repository_id"),
            raw=dict(payload),
        )

    def find_unit_by_key(self, unit_key: str) -> UnitReadback | None:
        # No key lookup verb exists yet (design 7.3 follow-up); ``db unit list``
        # is unpaginated, so a dispatched-state scan is complete.
        rows = self._run("db", "unit", "list", "--state", "dispatched", refused_code="unit_register_unavailable")
        if not isinstance(rows, list):
            raise RegisterUnitError("unit_register_unavailable", "psolet db unit list did not print a list")
        matches = [row for row in rows if isinstance(row, Mapping) and _text(row, "unit_key") == unit_key]
        if not matches:
            return None
        # ``list_units`` rows key the id as ``unit_id`` (project-solet unit_repository).
        return self.show_unit(_text(matches[0], "unit_id"))

    def show_unit(self, unit_id: str) -> UnitReadback:
        return parse_unit_show(unit_id, self._run("db", "unit", "show", unit_id, refused_code="unit_not_found"))

    def record_unit_event(self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor) -> None:
        self._run(
            "db", "unit", "record-event", unit_id,
            "--event-kind", "observation",
            "--detail", json.dumps({"schema": LINK_EVENT_SCHEMA, **detail}, sort_keys=True),
            *self._actor_arguments(actor),
            refused_code="unit_annotation_refused",
        )

    def retire_unit(self, unit_id: str, reason: str, actor: RegisterActor) -> None:
        self._run(
            "db", "unit", "retire", unit_id,
            "--state", "cancelled",
            "--reason", reason,
            *self._actor_arguments(actor),
            refused_code="unit_annotation_refused",
        )


__all__ = [
    "CONFIG_PSOLET_CLI",
    "DEFAULT_PSOLET_CLI",
    "LINK_EVENT_SCHEMA",
    "PSOLET_CLI_ENV",
    "UNIT_DISPATCHABLE_STATES",
    "MintReceipt",
    "MintRequest",
    "PsoletRegisterUnitClient",
    "RegisterActor",
    "RegisterUnitClient",
    "RegisterUnitError",
    "UnitReadback",
    "parse_unit_show",
    "resolve_psolet_cli",
]
