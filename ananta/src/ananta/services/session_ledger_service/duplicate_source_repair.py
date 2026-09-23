"""Module-level helpers for ``SessionLedgerService.retire_duplicate_source``
and ``_register_source_internal``'s race-safe converge.

Schema-debt-external-id lane, 2b-S1 (2026-08-06). Split out of
``service.py`` (module-level functions, not methods) to keep
``SessionLedgerService`` under the god-class LOC threshold and the file's
maintainability-index score in band — the same discipline ``ingest.py``'s
``_upsert_event_or_dedup_known_collision`` split used, one level further:
a dedicated module rather than a same-file module-level function, since
this file's aggregate Halstead volume was already large enough that even a
same-file split didn't recover the MI band.

Full evidence, winner-selection rules, and the per-pair quiesce protocol
are in
``workbench/2026-08-06_schema_debt_external_id_findings_schema-debt-impl.md``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ananta.llm.session_ledger.repository import (
    LedgerRepositoryError,
    SessionLedgerRepository,
    SourceRow,
)
from ananta.llm.session_ledger.types import IngestSourceKind

RELOAD_SAFE = True

# The physical name ``resolve_index_name`` (postgres_backend/ddl_renderer.py)
# will assign to the composite ``IndexDefinition(unique=True,
# columns=["source_kind", "root_uri"])`` 2b-S2 adds to
# ``session_ledger__source`` — ``f"{namespace}__{table}__{idx_name}"``.
# Declared here BEFORE that index exists so ``_insert_source_or_absorb_race``
# already names the exact constraint it will start catching the moment S2
# lands; until then the branch is LATENT (no such constraint exists yet, so
# Postgres never raises it).
SOURCE_KIND_ROOT_URI_UNIQUE_CONSTRAINT = (
    "session_ledger__source__idx_source_kind_root_uri_unique"
)


def insert_source_or_absorb_race(
    repository: SessionLedgerRepository,
    *,
    kind: IngestSourceKind,
    canonical_root_uri: str,
    account_label: str | None,
    config_json: dict[str, Any] | None,
) -> dict[str, Any]:
    """``_register_source_internal``'s insert step, wrapped TRY-INSERT-ABSORB.

    Degrades the ONE named ``SOURCE_KIND_ROOT_URI_UNIQUE_CONSTRAINT``
    violation to a re-read + ``"existed"`` outcome (LATENT until 2b-S2 adds
    that constraint — see the module docstring); any other failure still
    raises loud. Same pattern as ``ingest.py``'s
    ``_STALE_EXTERNAL_ID_UNIQUE_CONSTRAINT``.
    """
    try:
        source_id = repository.insert_source(
            source_kind=kind,
            root_uri=canonical_root_uri,
            account_label=account_label,
            config=config_json,
        )
    except LedgerRepositoryError as exc:
        if SOURCE_KIND_ROOT_URI_UNIQUE_CONSTRAINT not in str(exc):
            raise
        raced_id = repository.find_source_id_by_kind_and_root_uri(
            source_kind=kind, root_uri=canonical_root_uri,
        )
        if raced_id is None:
            raise
        return {"source_id": raced_id, "outcome": "existed"}
    return {"source_id": source_id, "outcome": "registered"}


def resolve_duplicate_source_pair(
    repository: SessionLedgerRepository,
    winner_source_id: str,
    loser_source_id: str,
) -> tuple[SourceRow, SourceRow]:
    """Identity + shape validation shared by the dry-run and confirmed paths.

    Raises ``ValueError`` for: identical ids, either row missing/deleted, or
    a ``source_kind`` mismatch (never a legitimate duplicate pair).
    """
    if winner_source_id == loser_source_id:
        raise ValueError(
            "retire_duplicate_source refused: winner_source_id == "
            f"loser_source_id ({winner_source_id!r})",
        )
    winner = repository.get_source(winner_source_id)
    loser = repository.get_source(loser_source_id)
    if winner is None:
        raise ValueError(
            f"retire_duplicate_source: winner {winner_source_id!r} "
            "not found or deleted",
        )
    if loser is None:
        raise ValueError(
            f"retire_duplicate_source: loser {loser_source_id!r} "
            "not found or deleted",
        )
    if winner.source_kind is not loser.source_kind:
        raise ValueError(
            "retire_duplicate_source refused: source_kind mismatch "
            f"(winner={winner.source_kind.value!r}, "
            f"loser={loser.source_kind.value!r}) — never a legitimate "
            "duplicate pair",
        )
    return winner, loser


def check_duplicate_source_quiesced(
    repository: SessionLedgerRepository,
    loser: SourceRow,
    loser_source_id: str,
) -> None:
    """Quiesce-protocol preconditions for ``confirm=True``, enforced here
    (not merely trusted to an external caller) — see
    ``SessionLedgerService.retire_duplicate_source``'s docstring for why.
    Raises ``ValueError`` if the loser is still ``enabled`` or still shows
    an active polling lease.
    """
    if loser.enabled:
        raise ValueError(
            f"retire_duplicate_source refused: loser {loser_source_id!r} "
            "is still enabled — disable it first (quiesce protocol) so "
            "the live poller cannot race the re-point.",
        )
    lease_state = repository.get_source_lease_state(loser_source_id)
    lease_until = (lease_state or {}).get("polling_lease_until")
    if isinstance(lease_until, datetime):
        lease_deadline = lease_until if lease_until.tzinfo else lease_until.replace(tzinfo=UTC)
        if lease_deadline > datetime.now(UTC):
            raise ValueError(
                f"retire_duplicate_source refused: loser {loser_source_id!r} "
                f"has an active polling lease until {lease_until.isoformat()!r} "
                "— wait for it to clear before retrying.",
            )


__all__ = [
    "SOURCE_KIND_ROOT_URI_UNIQUE_CONSTRAINT",
    "check_duplicate_source_quiesced",
    "insert_source_or_absorb_race",
    "resolve_duplicate_source_pair",
]
