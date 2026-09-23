"""Server-derived caller provenance for application-adapter plugins."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ananta.core.services.call_context import CallContext

from .session_lifecycle_store import (
    SessionNotFoundError,
    format_directed_by,
    read_managed_session,
)
from .session_role_claim_store import read_session_role_claim

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9._:@/-]{1,256}$")


class CallerProvenanceError(RuntimeError):
    """A caller cannot be truthfully linked to a managed session."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CallerProvenance:
    """The bounded provenance envelope a client may attest downstream."""

    managed_session_id: str
    agent_session_id: str
    directed_by: str
    directed_by_encoding: str
    session_role_claim_id: str | None = None

    def as_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "managed_session_id": self.managed_session_id,
            "agent_session_id": self.agent_session_id,
            "directed_by": self.directed_by,
            "directed_by_encoding": self.directed_by_encoding,
        }
        if self.session_role_claim_id is not None:
            result["session_role_claim_id"] = self.session_role_claim_id
        return result


def resolve_caller_provenance(
    state_service: StateManagementInterface,
    state: dict[str, Any],
) -> CallerProvenance:
    """Resolve provenance solely from server-built EDGE state, or refuse."""
    call_context = state.get("call_context")
    if not isinstance(call_context, CallContext):
        raise CallerProvenanceError(
            "caller_provenance_unavailable",
            "Caller provenance requires server-built CallContext.",
        )
    agent_instance_id = _caller_instance_id(state)
    if agent_instance_id is None:
        raise CallerProvenanceError(
            "caller_provenance_unavailable",
            "Caller provenance has no server-derived registered or attributed instance.",
        )
    try:
        managed_session = read_managed_session(state_service, agent_instance_id)
    except SessionNotFoundError as exc:
        raise CallerProvenanceError(
            "caller_provenance_unavailable",
            "No managed-session ledger row exists for the server-derived caller instance.",
        ) from exc

    managed_session_id = _required_opaque(
        managed_session.get("id") or managed_session.get("agent_instance_id"),
        "managed-session ledger identity",
    )
    agent_session_id = _required_opaque(
        managed_session.get("agent_session_id"), "managed-session agent_session_id",
    )
    directed_by, directed_by_encoding = _directed_by(call_context)
    role_claim = read_session_role_claim(state_service, agent_session_id)
    role_claim_id = _optional_opaque(
        role_claim.get("id") if role_claim is not None else None,
        "session role-claim identity",
    )
    return CallerProvenance(
        managed_session_id=managed_session_id,
        agent_session_id=agent_session_id,
        directed_by=directed_by,
        directed_by_encoding=directed_by_encoding,
        session_role_claim_id=role_claim_id,
    )


def _caller_instance_id(state: dict[str, Any]) -> str | None:
    """Pick a registered caller first, then server-attributed one-shot caller."""
    for key in ("inference_vertex_session_id", "caller_attribution_instance_id"):
        value = state.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _directed_by(call_context: CallContext) -> tuple[str, str]:
    """Reuse the shared formatter and hash only when its value violates the grammar."""
    rendered = format_directed_by(call_context)
    if _OPAQUE_ID.fullmatch(rendered):
        return rendered, "format_directed_by"
    digest = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    return f"sha256:{digest}", "sha256(format_directed_by)"


def _required_opaque(value: object, field: str) -> str:
    result = _optional_opaque(value, field)
    if result is None:
        raise CallerProvenanceError(
            "caller_provenance_unavailable",
            f"Caller provenance has no grammar-valid {field}.",
        )
    return result


def _optional_opaque(value: object, field: str) -> str | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str) or _OPAQUE_ID.fullmatch(value) is None:
        raise CallerProvenanceError(
            "caller_provenance_unavailable",
            f"Caller provenance has invalid {field}.",
        )
    return value
