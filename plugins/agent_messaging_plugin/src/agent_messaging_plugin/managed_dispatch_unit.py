"""Register Unit decisions for the managed-dispatch state machine.

Design ``unt_57725090`` section 4: a prepared dispatch mints (or verifies) its
Project Solet Unit before any attempt spawns.  This module owns the decisions
-- lane-root resolution, verify, mint, reconcile-by-key, link-event detail,
retirement -- and returns row updates; ``managed_dispatch`` owns every
dispatch-row write and append-only event, so the causal-version CAS stays in
one place.  Every refusal is a ``DispatchError`` whose code starts ``unit_``
(or is a lane-root refusal that fires before any register call).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_records

from .lane_worktrees import LaneWorktreeRepoRootError, resolve_lane_repo_root
from .managed_dispatch_errors import DispatchError
from .register_unit_client import (
    UNIT_DISPATCHABLE_STATES,
    MintRequest,
    RegisterActor,
    RegisterUnitClient,
    RegisterUnitError,
)
from .schema import TABLE_MANAGED_DISPATCH

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

NEXT_ENSURE_UNIT = "ensure_unit"
NEXT_SPAWN_CURRENT_ATTEMPT = "spawn_current_attempt"
EVENT_ENSURE_UNIT = "ensure_unit"
EVENT_REGISTER_ANNOTATION_FAILED = "register_annotation_failed"
UNIT_MINT_MINTED = "minted"
UNIT_MINT_ADOPTED = "adopted"
UNIT_MINT_VERIFIED = "verified"
MINT_INPUT_FIELDS = (
    "repository_id",
    "unit_key",
    "addresses",
    "reference_basis",
    "reference_basis_reason",
    "brief_repository_root",
)
# PSM ruling (unt_a9f1776c R3): a brief that is not in a registered checkout
# is unsupported; the refusal says where it must live instead.
BRIEF_LOCATION_RULING = (
    "Briefs outside a registered repository (for example a home-directory reports folder) are NOT supported: "
    "store the brief in a registered repository's workbench and declare that repository's root "
    "as brief_repository_root."
)


def unit_mint_columns(record: dict[str, Any]) -> dict[str, Any]:
    """Pop the spec's mint inputs from ``record`` and pin them once, at prepare."""
    inputs = {name: record.pop(name) for name in MINT_INPUT_FIELDS}
    unit_id = str(record.get("unit_id") or "")
    default_key = f"{record['lane_id']}-{record['dispatch_id']}"
    return {
        "unit_mint_state": "",
        "unit_mint_key": "" if unit_id else str(inputs["unit_key"] or default_key),
        "unit_repository_id": "",
        "unit_mint_receipt": {},
        "unit_mint_request": {
            "expected_repository_id": str(inputs["repository_id"] or ""),
            "addresses": [str(item) for item in inputs["addresses"]],
            "reference_basis": str(inputs["reference_basis"] or ""),
            "reference_basis_reason": str(inputs["reference_basis_reason"] or ""),
            "brief_repository_root": str(inputs["brief_repository_root"] or ""),
        },
    }


def fail_start_updates(row: Mapping[str, Any], error: DispatchError) -> dict[str, Any]:
    return {
        "state": "failed_start",
        "terminal_reason": f"{error.code}: {error.message}",
        "next_required_action": "decide_retry_or_cancel",
        "responsible_role": str(row["spawned_by_role"]),
    }


def dispatch_source_ref(row: Mapping[str, Any]) -> str:
    return f"managed-dispatch:{row['dispatch_id']}"


def lane_root(row: Mapping[str, Any]) -> Path:
    """The checkout worktree provisioning will use; never ``Path.cwd()``."""
    repository_root = str(row.get("repository_root") or "")
    app_home = os.environ.get("APP_HOME", "").strip()
    if not repository_root.strip() and not app_home:
        raise DispatchError(
            "lane_worktree_app_home_required",
            "lane worktree provisioning requires an explicit APP_HOME-derived checkout",
        )
    try:
        return resolve_lane_repo_root(repository_root, app_home)
    except LaneWorktreeRepoRootError as exc:
        raise DispatchError(exc.code, str(exc)) from exc


def _register_call[T](call: Callable[[], T]) -> T:
    try:
        return call()
    except RegisterUnitError as exc:
        raise DispatchError(exc.code, exc.detail) from exc


def _mint_inputs(row: Mapping[str, Any]) -> Mapping[str, Any]:
    inputs = row.get("unit_mint_request")
    return inputs if isinstance(inputs, Mapping) else {}


def ensure_unit_updates(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    register: RegisterUnitClient,
    actor: RegisterActor,
    *,
    reconcile_first: bool,
) -> dict[str, Any]:
    """Return the row updates that record an ensured Unit, or refuse."""
    if not str(row.get("dispatch_kind") or "").strip():
        # The register refuses a blank kind too; refuse first, under the
        # policy's own code, so no register call carries invalid input.
        raise DispatchError("dispatch_kind_required", "managed dispatch requires dispatch_kind before its Unit.")
    root = lane_root(row)
    repository_id = _register_call(lambda: register.resolve_repository(root))
    expected = str(_mint_inputs(row).get("expected_repository_id") or "")
    if expected and expected != repository_id:
        raise DispatchError(
            "unit_repository_mismatch",
            f"repository_id {expected} does not match the lane root's {repository_id}.",
        )
    if str(row.get("unit_id") or ""):
        updates = _verify_supplied_unit(state, row, register, repository_id)
    else:
        updates = _mint_or_adopt_unit(row, register, actor, root, repository_id, reconcile_first=reconcile_first)
    return {**updates, "unit_repository_id": repository_id, "next_required_action": NEXT_SPAWN_CURRENT_ATTEMPT}


def _verify_supplied_unit(
    state: StateManagementInterface,
    row: Mapping[str, Any],
    register: RegisterUnitClient,
    repository_id: str,
) -> dict[str, Any]:
    """Refuse a caller-supplied Unit the register cannot confirm (s4.6)."""
    unit_id = str(row["unit_id"])
    unit = _register_call(lambda: register.show_unit(unit_id))
    if unit.is_deleted or unit.unit_id != unit_id:
        raise DispatchError("unit_not_found", f"Register has no live Unit {unit_id}.")
    if unit.state not in UNIT_DISPATCHABLE_STATES:
        raise DispatchError("unit_not_dispatchable", f"Unit {unit_id} is {unit.state or 'stateless'}.")
    if unit.repository_id != repository_id:
        raise DispatchError(
            "unit_repository_mismatch",
            f"Unit {unit_id} belongs to {unit.repository_id}, not {repository_id}.",
        )
    if unit.kind != str(row.get("dispatch_kind") or ""):
        raise DispatchError("unit_kind_mismatch", f"Unit {unit_id} kind {unit.kind!r} differs from dispatch_kind.")
    _require_no_other_live_dispatch(state, row)
    return {"unit_mint_state": UNIT_MINT_VERIFIED, "unit_mint_key": unit.unit_key}


def _require_no_other_live_dispatch(state: StateManagementInterface, row: Mapping[str, Any]) -> None:
    # Call-local: ``managed_dispatch`` imports this module at load time.
    from .managed_dispatch import DISPATCH_TERMINAL_STATES  # noqa: PLC0415

    rows = require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MANAGED_DISPATCH, "filters": {"unit_id": str(row["unit_id"]), "is_deleted": 0}},
        ),
    )
    live = [
        other for other in rows
        if other.get("dispatch_id") != row["dispatch_id"] and other.get("state") not in DISPATCH_TERMINAL_STATES
    ]
    if live:
        raise DispatchError(
            "unit_already_dispatched",
            f"Unit {row['unit_id']} already has live dispatch {live[0].get('dispatch_id')}.",
        )


def _mint_or_adopt_unit(
    row: Mapping[str, Any],
    register: RegisterUnitClient,
    actor: RegisterActor,
    root: Path,
    repository_id: str,
    *,
    reconcile_first: bool,
) -> dict[str, Any]:
    """Mint under the pinned key; after any uncertainty, adopt before re-minting."""
    brief_root = brief_repository_root(row, register, root)
    adopted = _reconcile_unit_by_key(row, register, repository_id) if reconcile_first else None
    if adopted is not None:
        return adopted
    request = _mint_request(row, actor, root, brief_root)
    try:
        receipt = register.mint_unit(request)
    except RegisterUnitError as exc:
        if exc.code != "unit_mint_uncertain":
            raise DispatchError(exc.code, exc.detail) from exc
        adopted = _reconcile_unit_by_key(row, register, repository_id)
        if adopted is not None:
            return adopted
        receipt = _register_call(lambda: register.mint_unit(request))
    if receipt.brief_target_repository_id != repository_id:
        raise DispatchError(
            "unit_repository_mismatch",
            f"Minted Unit {receipt.unit_id} targets {receipt.brief_target_repository_id}, not {repository_id}.",
        )
    return {
        "unit_id": receipt.unit_id,
        "unit_mint_state": UNIT_MINT_MINTED,
        "unit_mint_receipt": dict(receipt.raw),
    }


def brief_repository_root(row: Mapping[str, Any], register: RegisterUnitClient, root: Path) -> Path | None:
    """The declared ``--brief-repo`` for this brief, or ``None`` when it sits in the lane root.

    Never inferred: an undeclared brief outside the lane root, a brief outside
    its declared root, and a declared root the register does not know all
    refuse before any mint.
    """
    brief = (root / str(row["brief_ref"])).resolve()
    declared = str(_mint_inputs(row).get("brief_repository_root") or "")
    if not declared:
        if not brief.is_relative_to(root):
            raise DispatchError(
                "unit_brief_outside_repository",
                f"brief {brief} is outside the lane root {root} and no brief_repository_root was declared. "
                f"{BRIEF_LOCATION_RULING}",
            )
        return None
    if not Path(declared).is_absolute():
        raise DispatchError(
            "unit_brief_repository_invalid",
            f"brief_repository_root must be an absolute checkout path, got {declared!r}.",
        )
    declared_root = Path(declared).resolve()
    if not brief.is_relative_to(declared_root):
        raise DispatchError(
            "unit_brief_outside_repository",
            f"brief {brief} is not under the declared brief_repository_root {declared_root}. "
            f"{BRIEF_LOCATION_RULING}",
        )
    try:
        register.resolve_repository(declared_root)
    except RegisterUnitError as exc:
        raise DispatchError(
            "unit_brief_repository_unregistered",
            f"brief_repository_root {declared_root} is not a registered repository ({exc.detail}). "
            f"{BRIEF_LOCATION_RULING}",
        ) from exc
    return declared_root


def _mint_request(
    row: Mapping[str, Any],
    actor: RegisterActor,
    root: Path,
    brief_root: Path | None,
) -> MintRequest:
    inputs = _mint_inputs(row)
    raw_addresses = inputs.get("addresses")
    return MintRequest(
        lane_id=str(row["lane_id"]),
        brief_path=root / str(row["brief_ref"]),
        brief_sha256=str(row["brief_sha256"]),
        repository_root=root,
        brief_repository_root=brief_root,
        kind=str(row.get("dispatch_kind") or ""),
        model=str(row["model"]),
        effort=str(row["effort"]),
        unit_key=str(row["unit_mint_key"]),
        dispatch_source_ref=dispatch_source_ref(row),
        addresses=tuple(str(item) for item in raw_addresses) if isinstance(raw_addresses, list) else (),
        reference_basis=str(inputs.get("reference_basis") or ""),
        reference_basis_reason=str(inputs.get("reference_basis_reason") or ""),
        actor=actor,
    )


def _reconcile_unit_by_key(
    row: Mapping[str, Any],
    register: RegisterUnitClient,
    repository_id: str,
) -> dict[str, Any] | None:
    """Adopt a Unit an uncertain mint committed; refuse one another dispatch owns."""
    key = str(row["unit_mint_key"])
    unit = _register_call(lambda: register.find_unit_by_key(key))
    if unit is None:
        return None
    owned = (
        unit.dispatch_source_ref == dispatch_source_ref(row)
        and unit.repository_id == repository_id
        and unit.kind == str(row.get("dispatch_kind") or "")
    )
    if not owned:
        raise DispatchError(
            "unit_mint_conflict",
            f"Unit {unit.unit_id} holds key {key} for {unit.dispatch_source_ref!r}, not this dispatch.",
        )
    return {
        "unit_id": unit.unit_id,
        "unit_mint_state": UNIT_MINT_ADOPTED,
        "unit_mint_receipt": {"adopted_unit_key": key, "unit_id": unit.unit_id, "unit_state": unit.state},
    }


def link_event_detail(row: Mapping[str, Any], event: str, extra: Mapping[str, Any] | None) -> dict[str, Any]:
    """The ``managed-dispatch-link/v1`` observation for one attempt decision."""
    return {
        "dispatch_id": str(row["dispatch_id"]),
        "attempt_number": int(row["attempt_number"]),
        "event": event,
        "agent_instance_id": str(row.get("current_agent_instance_id") or ""),
        "model": str(row["model"]),
        "effort": str(row["effort"]),
        "host": str(row["host"]),
        "observed_at": datetime.now(UTC).isoformat(),
        **(extra or {}),
    }


def _unbound() -> RegisterUnitError:
    return RegisterUnitError("unit_register_unavailable", "no register client is bound to this call")


def annotate_unit(
    register: RegisterUnitClient | None,
    row: Mapping[str, Any],
    actor: RegisterActor,
    event: str,
    extra: Mapping[str, Any] | None = None,
) -> RegisterUnitError | None:
    """Best-effort link event; the failure is returned for the caller to record."""
    try:
        if register is None:
            raise _unbound()
        register.record_unit_event(str(row["unit_id"]), link_event_detail(row, event, extra), actor)
    except RegisterUnitError as exc:
        return exc
    return None


def retire_unit(
    register: RegisterUnitClient | None,
    row: Mapping[str, Any],
    actor: RegisterActor,
    reason: str,
) -> RegisterUnitError | None:
    """Retire a self-minted Unit no attempt took up; the failure is returned."""
    try:
        if register is None:
            raise _unbound()
        register.retire_unit(str(row["unit_id"]), f"{dispatch_source_ref(row)} cancelled before uptake: {reason}", actor)
    except RegisterUnitError as exc:
        return exc
    return None


__all__ = [
    "BRIEF_LOCATION_RULING",
    "EVENT_ENSURE_UNIT",
    "EVENT_REGISTER_ANNOTATION_FAILED",
    "MINT_INPUT_FIELDS",
    "NEXT_ENSURE_UNIT",
    "NEXT_SPAWN_CURRENT_ATTEMPT",
    "UNIT_MINT_ADOPTED",
    "UNIT_MINT_MINTED",
    "UNIT_MINT_VERIFIED",
    "annotate_unit",
    "brief_repository_root",
    "dispatch_source_ref",
    "ensure_unit_updates",
    "fail_start_updates",
    "lane_root",
    "link_event_detail",
    "retire_unit",
    "unit_mint_columns",
]
