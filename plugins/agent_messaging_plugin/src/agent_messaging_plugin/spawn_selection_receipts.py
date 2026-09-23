"""Receipt verification and persistence contracts for public session spawns."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .model_capability_verbs import CatalogError, verify_selection_receipt

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ananta.interfaces.state_management_interface import StateManagementInterface


class SelectionReceiptError(Exception):
    """A selector refusal normalized for the lifecycle verb boundary."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def receipt_contract_mismatches(
    row: Mapping[str, Any], *, enforce: bool, difficulty_score: float,
    selection_receipt: Mapping[str, Any] | None,
) -> set[str]:
    """Name immutable receipt fields that a managed retry attempted to alter."""
    if not enforce:
        return set()
    expected = {
        "difficulty_score": difficulty_score,
        "selection_receipt": selection_receipt,
    }
    return {field for field, value in expected.items() if row.get(field) != value}


def replay_selection_receipt(
    state: StateManagementInterface, *, synthetic_qualification: bool,
    enforce: bool, difficulty_score: float,
    selection_receipt: Mapping[str, Any] | None, dispatch_kind: str,
    scope_tags: tuple[str, ...], agent_runtime: str, model: str, effort: str,
) -> dict[str, Any]:
    """Replay a public receipt before spawn, or deliberately skip fixture-only calls."""
    if synthetic_qualification or not enforce:
        return {}
    try:
        return verify_selection_receipt(
            state,
            difficulty_score=difficulty_score,
            selection_receipt=selection_receipt,
            dispatch_kind=dispatch_kind,
            scope_tags=scope_tags,
            agent_runtime=agent_runtime,
            model=model,
            effort=effort,
        )
    except CatalogError as exc:
        raise SelectionReceiptError(exc.code, exc.message) from exc
