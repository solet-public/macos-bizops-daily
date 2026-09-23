"""Session-bound view over the memory service's focus surface (JOS-02).

Focus is session-scoped: every focus-buffer read/write keys by the acting
session. Plan/WBS lifecycle helpers (``wbs_lifecycle``, the thinking
plugin's ``PlanStore``) operate on "the focused memories" as a unit of
work — this adapter binds that unit to ONE session at construction time,
so the helpers stay session-correct without threading ``session_id``
through every signature.

The binding is explicit and required: constructing a view without a
session is a fail-fast error. ``remember`` stamps the bound session onto
the memory row (JOS-02 R5 — plan memories carry their owning session).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from ananta.error_handling import FrameworkError


class SessionKeyedMemoryService(Protocol):
    """The session-scoped memory-service surface the view binds over."""

    def get_focused(self, *, session_id: str) -> dict[str, Any]: ...

    def focus(self, memory_id: str, *, session_id: str) -> dict[str, Any]: ...

    def unfocus(self, memory_id: str, *, session_id: str) -> dict[str, Any]: ...

    def forget(self, memory_id: str) -> Any: ...

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class SessionScopedMemory:
    """One session's view of the focus surface.

    Satisfies the no-arg focus protocols consumed by ``wbs_lifecycle`` and
    ``PlanStore`` (``get_focused()`` / ``focus(mid)`` / ``unfocus(mid)`` /
    ``forget(mid)`` / ``remember(...)``) while keying every operation by the
    bound session.
    """

    memory_service: SessionKeyedMemoryService
    session_id: str

    def __post_init__(self) -> None:
        if not self.session_id:
            raise FrameworkError(
                message=(
                    "SessionScopedMemory requires a non-empty session_id — "
                    "focus operations have no meaning without an acting session"
                ),
                error_code="memory.session_required",
            )

    def get_focused(self) -> dict[str, Any]:
        """The bound session's focus envelope {"memories": [...], "count": N}."""
        return self.memory_service.get_focused(session_id=self.session_id)

    def focus(self, memory_id: str) -> dict[str, Any]:
        return self.memory_service.focus(memory_id, session_id=self.session_id)

    def unfocus(self, memory_id: str) -> dict[str, Any]:
        return self.memory_service.unfocus(memory_id, session_id=self.session_id)

    def forget(self, memory_id: str) -> Any:
        return self.memory_service.forget(memory_id)

    def remember(
        self,
        content: str,
        tags: list[str] | None = None,
        source_file: str | None = None,
        session_id: str | None = None,
        embed: bool = True,
    ) -> dict[str, Any]:
        """Store a memory stamped with the BOUND session (JOS-02 R5).

        A caller-supplied ``session_id`` differing from the binding is a
        contract violation — the view exists precisely so writes cannot
        drift across sessions.
        """
        if session_id and session_id != self.session_id:
            raise FrameworkError(
                message=(
                    f"SessionScopedMemory bound to {self.session_id!r} cannot "
                    f"remember into foreign session {session_id!r}"
                ),
                error_code="memory.session_mismatch",
            )
        return self.memory_service.remember(
            content=content,
            tags=tags,
            source_file=source_file,
            session_id=self.session_id,
            embed=embed,
        )
