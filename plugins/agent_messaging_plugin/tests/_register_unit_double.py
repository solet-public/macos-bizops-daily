"""In-memory register double for smokes that exercise dispatch wiring, not the mint.

``dispatch_managed_work`` now mints (or verifies) a Project Solet Unit before
every spawn (design ``unt_57725090``), so any smoke that drives a managed
dispatch needs a register.  The mint contract itself -- exact ``psolet`` argv,
refusals, reconciliation, compensation -- is proven against a recording fake
CLI in ``managed_dispatch_unit_mint_smoke.py``; this double accepts only a
mint the real seam would accept -- the same preconditions that fake enforces --
and reports it back, so wiring smokes keep testing what they test without
passing a request production would refuse.  It never answers for a Unit it did
not mint.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_messaging_plugin.register_unit_client import (
    MintReceipt,
    MintRequest,
    RegisterActor,
    RegisterUnitError,
    UnitReadback,
)

FIXTURE_REPOSITORY_ID = "rep_fixture-solet"
REFERENCE_BASES = frozenset({"existing_pattern", "no_existing_pattern"})


def _refuse(detail: str) -> RegisterUnitError:
    return RegisterUnitError("unit_mint_refused", detail)


def _require_declared_runtime(request: MintRequest) -> None:
    if not (request.kind.strip() and request.model.strip() and request.effort.strip()):
        raise _refuse("kind, model and effort are required and must not be blank")
    if request.kind == "fix" and request.reference_basis not in REFERENCE_BASES:
        raise _refuse("fix units require reference_basis (existing_pattern or no_existing_pattern)")
    if request.kind != "fix" and request.reference_basis:
        raise _refuse("reference_basis applies only to fix units")


def _require_registered_brief(request: MintRequest) -> None:
    brief_repository = request.brief_repository_root or request.repository_root
    if not request.brief_path.resolve().is_relative_to(brief_repository.resolve()):
        raise _refuse("brief path is outside the brief repository")
    if not request.brief_path.is_file():
        raise _refuse("source brief exact bytes are not registered")
    if hashlib.sha256(request.brief_path.read_bytes()).hexdigest() != request.brief_sha256:
        raise _refuse("document SHA-256 changed or does not match --expected-sha256")


class RegisterUnitDouble:
    """``RegisterUnitClient`` that mints into memory under the pinned key."""

    def __init__(self, repository_id: str = FIXTURE_REPOSITORY_ID) -> None:
        self.repository_id = repository_id
        self.units: dict[str, UnitReadback] = {}
        self.mints: list[MintRequest] = []
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.retired: list[tuple[str, str]] = []

    def resolve_repository(self, root: Path) -> str:
        del root
        return self.repository_id

    def mint_unit(self, request: MintRequest) -> MintReceipt:
        # The ``psolet dispatched`` refusals the recording fake also enforces.
        _require_declared_runtime(request)
        _require_registered_brief(request)
        if any(unit.unit_key == request.unit_key for unit in self.units.values()):
            raise RegisterUnitError("unit_mint_refused", f"duplicate unit_key {request.unit_key}")
        self.mints.append(request)
        unit_id = f"unt_double-{len(self.mints)}"
        self.units[unit_id] = UnitReadback(
            unit_id=unit_id,
            unit_key=request.unit_key,
            kind=request.kind,
            repository_id=self.repository_id,
            dispatch_source_ref=request.dispatch_source_ref,
            state="dispatched",
            is_deleted=False,
        )
        receipt = {"unit_id": unit_id, "brief_target_repository_id": self.repository_id}
        return MintReceipt(unit_id=unit_id, brief_target_repository_id=self.repository_id, raw=receipt)

    def find_unit_by_key(self, unit_key: str) -> UnitReadback | None:
        # The real client scans ``db unit list --state dispatched`` only.
        return next(
            (unit for unit in self.units.values() if unit.unit_key == unit_key and unit.state == "dispatched"),
            None,
        )

    def show_unit(self, unit_id: str) -> UnitReadback:
        if unit_id not in self.units:
            raise RegisterUnitError("unit_not_found", f"register double never minted {unit_id}")
        return self.units[unit_id]

    def record_unit_event(self, unit_id: str, detail: Mapping[str, Any], actor: RegisterActor) -> None:
        del actor
        self.events.append((unit_id, dict(detail)))

    def retire_unit(self, unit_id: str, reason: str, actor: RegisterActor) -> None:
        del actor
        if unit_id not in self.units:
            raise RegisterUnitError("unit_annotation_refused", f"register double never minted {unit_id}")
        self.retired.append((unit_id, reason))
        self.units[unit_id] = replace(self.units[unit_id], state="cancelled")
