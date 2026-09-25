"""Capability-floor plumbing between the spawn verbs and the dispatch policy.

The policy module (``model_dispatch_policy``) decides; this module is the thin
adapter the spawn path calls so ``session_lifecycle_verbs`` and
``managed_dispatch`` stay small: it turns a policy refusal into the verb's own
``VerbError``, normalises the persisted ``scope_tags`` contract field (older
prepared-dispatch rows predate the column and carry ``None``), and renders the
applied floors into the ledger's JSON shape (iss_63d91ca9 / iss_da9e5e67).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict
from typing import Any, Protocol

from .model_dispatch_policy import AppliedFloor, DispatchPolicyError, validate_spawn_dispatch
from .workbench_brief_snapshot import BriefSnapshot, VerbError


class SpawnPolicyFields(Protocol):
    """The slice of a spawn request the dispatch policy reads."""

    @property
    def dispatch_kind(self) -> str: ...
    @property
    def agent_runtime(self) -> str: ...
    @property
    def model(self) -> str: ...
    @property
    def reviewed_report_vendor(self) -> str: ...
    @property
    def pair_id(self) -> str: ...
    @property
    def scope_tags(self) -> tuple[str, ...]: ...


def validate_spawn_policy(
    req: SpawnPolicyFields, brief_snapshot: BriefSnapshot | None,
) -> tuple[AppliedFloor, ...]:
    """The model-dispatch policy as a named verb refusal; returns the floors applied.

    Called twice per spawn: once before anything else (declared ``scope_tags``
    only, ``brief_snapshot=None``) and once more after the workbench brief is
    read. A configured floor may use the brief text; the retired state_schema
    floor does not. Both calls precede every host side effect and ledger write.
    """
    try:
        return validate_spawn_dispatch(
            dispatch_kind=req.dispatch_kind,
            agent_runtime=req.agent_runtime,
            model=req.model,
            reviewed_report_vendor=req.reviewed_report_vendor,
            pair_id=req.pair_id,
            scope_tags=req.scope_tags,
            brief_text=brief_snapshot.text if brief_snapshot is not None else "",
        )
    except DispatchPolicyError as exc:
        raise VerbError(exc.code, exc.message) from exc


def contract_list(row: Mapping[str, Any], field: str) -> tuple[str, ...] | None:
    """A list-valued contract field of a dispatch row, as the request's tuple shape.

    ``None`` reads as the empty declaration (rows prepared before a list column
    existed carry it); a value that is not a list reads as ``None`` so it can
    never equal a request tuple -- a contract mismatch, not a crash.
    """
    raw = row.get(field)
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)):
        return None
    return tuple(str(value) for value in raw)


def contract_scope_tags(row: Mapping[str, Any]) -> tuple[str, ...]:
    """The persisted ``scope_tags`` of a dispatch row, refusing a malformed column outright."""
    tags = contract_list(row, "scope_tags")
    if tags is None:
        raise VerbError("dispatch_contract_invalid", "scope_tags is not a list.")
    return tags


def recorded_floors(applied: tuple[AppliedFloor, ...]) -> list[dict[str, str]]:
    """The ledger's ``capability_floors`` JSON: every floor that governed this spawn's model."""
    return [asdict(floor) for floor in applied]


__all__ = ["SpawnPolicyFields", "contract_list", "contract_scope_tags", "recorded_floors", "validate_spawn_policy"]
