"""Registration for agent-authored-by-value joseki cards (Phase 3, Seam A).

The validation half of the seam lives in ``authored_validation``; this
module is the storage half for joseki cards. Per the plan of record's Q14
decision, storage is dual: the card markdown goes into the dedicated
``authored_joseki`` knowledge base (the KB **is** the joseki registry —
no second ingestion, no symbolic registry), and one lifecycle row lands
in ``thinking_authored_joseki`` at its INITIAL state (``draft``). State
transitions (draft → candidate → proven → superseded → archived) and run
evidence are Phase 6 — deliberately NOT built here.

Why a DEDICATED knowledge base (2026-07-02 live finding): the writer used
to target thinking_plans, whose runtime write path chunks + embeds
correctly — but that KB sits in the knowledge plugin's
``SEARCH_EXCLUDED_KB_NAMES`` (plan chunks pollute semantic search), so
registered cards could NEVER surface on the plain-English retrieval §4.3
requires, and thinking_plans' manifest globs don't cover a joseki dir
either. ``authored_joseki`` is semantically searchable by design; cards
land at its ROOT (``<joseki_key>.md``).

The unique-key guard runs BEFORE the knowledge-base write:
``knowledge_service.create_file`` silently overwrites on conflict (it
falls back to ``edit_file``), so uniqueness must be enforced from the
lifecycle table, never left to the writer.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Protocol

from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import (
    PROVENANCE_AUTHORED_BY_VALUE,
    ErrorCode,
)

if TYPE_CHECKING:
    from default_thinking_plugin.artifact_authoring import StateStore

_JOSEKI_TABLE = "thinking_authored_joseki"
_JOSEKI_INITIAL_STATE = "draft"


class JosekiCardWriter(Protocol):
    """Write a joseki card into the authored_joseki knowledge base.

    Narrower than ``KnowledgeWriter`` — registration only ever writes;
    reads go through knowledge retrieval.
    """

    def write(self, path: str, content: str) -> None:
        """Create or update the card at *path* (KB-relative)."""
        ...


class AuthoredJosekiRegistrar:
    """Stores a validated authored-by-value joseki card (KB + lifecycle row).

    The caller validates BEFORE registering (the register verb hard-fails
    on any validation error); this class only guards uniqueness and
    persists.
    """

    def __init__(
        self,
        *,
        knowledge_writer: JosekiCardWriter,
        state_store: StateStore,
        namespace: str,
    ) -> None:
        self._knowledge_writer = knowledge_writer
        self._state_store = state_store
        self._namespace = namespace

    def register(self, *, joseki_key: str, content: str) -> dict[str, Any]:
        """Persist the card and its lifecycle row at state ``draft``."""
        self._assert_key_unclaimed(joseki_key)

        kb_path = f"{joseki_key}.md"
        self._knowledge_writer.write(kb_path, content)
        self._state_store.write_state(
            namespace=self._namespace,
            data={
                "table": _JOSEKI_TABLE,
                "record": {
                    "joseki_key": joseki_key,
                    "state": _JOSEKI_INITIAL_STATE,
                    "provenance": PROVENANCE_AUTHORED_BY_VALUE,
                    "knowledge_base_path": kb_path,
                },
            },
        )
        return {
            "joseki_key": joseki_key,
            "status": "registered",
            "state": _JOSEKI_INITIAL_STATE,
            "knowledge_base_path": kb_path,
        }

    def _assert_key_unclaimed(self, joseki_key: str) -> None:
        """Refuse to overwrite: an existing lifecycle row is a hard error."""
        existing = self._state_store.read_state(
            namespace=self._namespace,
            query={
                "table": _JOSEKI_TABLE,
                "filters": {"joseki_key": joseki_key, "is_deleted": 0},
                "limit": 1,
            },
        )
        if existing.get("data", {}).get("records"):
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} is already registered — "
                    f"registration never overwrites; supersession is a "
                    f"Phase 6 lifecycle transition"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
