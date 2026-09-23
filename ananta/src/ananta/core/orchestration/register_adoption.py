"""Coordinator-owned register adoption before managed dispatch."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol


class RegisterClient(Protocol):
    """Coordinator-supplied credential holder for project-solet writes."""

    def mint_unit(self, *, lane_id: str, model: str, effort: str, ttl: str, brief_ref: str) -> None: ...

    def file_unminted_debt(self, *, lane_id: str, brief_ref: str) -> None: ...


class RegisterMintError(RuntimeError):
    """The coordinator's register client refused to mint the lane row."""


def spawn_with_register_adoption(
    state: Any,
    spec: Any,
    spawn_request: object,
    *,
    register_client: RegisterClient,
    no_mint: bool,
    dispatch: Callable[[Any, Any, object], dict[str, Any]],
) -> dict[str, Any]:
    """Mint first, or file explicit debt before unchanged managed dispatch."""
    if no_mint:
        register_client.file_unminted_debt(lane_id=spec.lane_id, brief_ref=spec.brief_ref)
        return {
            **dispatch(state, spec, spawn_request),
            "register_adoption": "bypassed",
            "warning": "--no-mint used; debt filed",
        }
    register_client.mint_unit(
        lane_id=spec.lane_id,
        model=spec.model,
        effort=spec.effort,
        ttl=spec.expires_at,
        brief_ref=spec.brief_ref,
    )
    return {**dispatch(state, spec, spawn_request), "register_adoption": "minted"}
