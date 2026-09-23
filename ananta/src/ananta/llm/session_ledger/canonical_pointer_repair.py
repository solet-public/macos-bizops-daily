"""Canonical-pointer-repair domain mixin for the session-ledger repository.

W5.O cycle 6 §3.5: 2 public methods + 1 private helper that resolve duplicate
canonical (vendor, external_session_id) groups before the M18 partial-unique
index can fire on green spawn.

SQL-lockdown #0, Slice 3a: migrated off raw ``transactional()`` SQL onto the
state-interface primitives. The dup-finder ``GROUP BY … HAVING count>1`` becomes
a Python ``collections.Counter`` over a ``query_state`` read; the per-group lift
drops the ``SELECT … FOR UPDATE`` row-lock in favour of a **deterministic
survivor + conditional compare-and-set**:

* survivor = the oldest canonical row in the group (``created_at`` then ``id``),
  a deterministic choice so concurrent repair passes elect the SAME survivor;
* each sibling is demoted with ``update_state`` filtered on
  ``canonical_external_session_id IS NULL`` — so a sibling a concurrent pass
  already demoted matches 0 rows (idempotent, no double-write);
* the repair is operator-gated + re-runs until ``count_canonical_duplicate_sessions``
  reaches 0, so a sibling inserted mid-pass is caught on the next pass.

This trades the FOR-UPDATE strict serialization for idempotent-CAS convergence,
which is sound for a one-shot pre-index repair (no hot-path concurrency) and
keeps the canonical-election correctness in the conditional ``WHERE`` rather than
a lock.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from ananta.llm.session_ledger.base import (
    LedgerRepositoryError,
    SessionLedgerRepositoryBase,
)
from ananta.llm.session_ledger.schema import (
    ID_PREFIX_SESSION_SOURCE_KIND,
    NAMESPACE,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
)
from ananta.llm.session_ledger.shared import _new_id

RELOAD_SAFE = True

logger = logging.getLogger(__name__)


class SessionLedgerCanonicalPointerRepairMixin(SessionLedgerRepositoryBase):
    """Canonical-pointer-repair domain mixin."""

    __slots__ = ()

    def _canonical_dup_group_counts(self) -> Counter[tuple[str, str]]:
        """Count canonical rows per ``(vendor, external_session_id)`` group.

        Reads every live canonical row (``canonical_external_session_id IS NULL``)
        and folds them in Python — the state interface has no ``GROUP BY``. A
        group with count > 1 is a duplicate-canonical anomaly.
        """
        rows = self._query(
            TABLE_SESSION,
            {
                "canonical_external_session_id": {"op": "is_null"},
                "is_deleted": 0,
            },
        )
        return Counter(
            (str(r.get("vendor", "")), str(r.get("external_session_id", "")))
            for r in rows
        )

    def count_canonical_duplicate_sessions(self) -> int:
        """Count duplicate ``(vendor, external_session_id)`` pairs where BOTH rows are canonical."""
        return sum(1 for n in self._canonical_dup_group_counts().values() if n > 1)

    def lift_canonical_pointer_for_duplicate_sessions(self) -> int:
        """Resolve duplicate canonical (vendor, external_session_id) groups in place.

        LOOPS — recount + re-lift until no duplicate-canonical group remains —
        because the dropped ``FOR UPDATE`` no longer serializes a canonical row
        inserted AFTER a group's non-locking read; such a row survives a single
        pass and is only caught on the next recount. The importer schedule is
        paused around the confirmed run, so with no live writers the loop
        converges in one or two passes. A pass that resolves NO group while
        duplicates remain is a data anomaly (a group with >1 canonical whose
        siblings could not be demoted) and fails fast rather than spinning.
        """
        total_demoted = 0
        while True:
            counts = self._canonical_dup_group_counts()
            # Deterministic order (matches the pre-migration ORDER BY vendor, ext_id).
            dup_groups = sorted(key for key, n in counts.items() if n > 1)
            if not dup_groups:
                return total_demoted
            pass_demoted = 0
            for vendor, external_session_id in dup_groups:
                pass_demoted += self._lift_one_duplicate_group(
                    vendor=vendor, external_session_id=external_session_id
                )
            if pass_demoted == 0:
                # A concurrent repair may have resolved the group(s) between our
                # count and this pass's reads/CAS, so ``_lift`` returns 0 on an
                # already-clean ledger. Confirm with a FRESH recount before
                # declaring an anomaly: a clean recount terminates cleanly; only a
                # recount that STILL reports duplicates means a group genuinely
                # could not be demoted (fail fast on the stale pre-pass counts
                # would false-raise on a benign concurrent resolution).
                if not any(
                    n > 1 for n in self._canonical_dup_group_counts().values()
                ):
                    return total_demoted
                raise LedgerRepositoryError(
                    f"canonical-pointer repair made no progress with "
                    f"{len(dup_groups)} duplicate group(s) still present after a "
                    "confirmation recount — a duplicate canonical group could not "
                    "be demoted (data anomaly)",
                )
            total_demoted += pass_demoted

    def _lift_one_duplicate_group(
        self, *, vendor: str, external_session_id: str
    ) -> int:
        """Lift sibling rows in one (vendor, external_session_id) duplicate group.

        Non-locking read + deterministic-survivor + conditional CAS (see the
        module docstring). Returns the number of siblings this pass actually
        demoted (a concurrent pass may have demoted some already — those return
        0 rows-affected and are not recounted).
        """
        now = self._clock()
        canonical_rows = self._query(
            TABLE_SESSION,
            {
                "vendor": vendor,
                "external_session_id": external_session_id,
                "canonical_external_session_id": {"op": "is_null"},
                "is_deleted": 0,
            },
        )
        if len(canonical_rows) < 2:
            return 0
        # Survivor = oldest by (created_at, id) — deterministic so concurrent
        # passes elect the same one (the survivor is therefore never demoted).
        ordered = sorted(
            canonical_rows,
            key=lambda r: (str(r.get("created_at", "")), str(r.get("id", ""))),
        )
        survivor_id = str(ordered[0]["id"])
        demoted = 0
        for sibling in ordered[1:]:
            sibling_id = str(sibling["id"])
            # Conditional CAS: only demote a sibling that is STILL canonical, so
            # a concurrent pass's demote is not double-applied (0 rows-affected).
            demoted += self._update(
                TABLE_SESSION,
                {
                    "id": sibling_id,
                    "canonical_external_session_id": {"op": "is_null"},
                },
                {
                    "canonical_external_session_id": external_session_id,
                    "updated_at": now,
                },
            )
            # SQL-lockdown list_sessions junction staleness reconciliation: AFTER
            # the re-election, the demoted ex-canonical's (sibling_id, kind)
            # junction rows are stale (it is no longer a group head). Merge its
            # kinds into the survivor + drop the stale rows. Runs unconditionally
            # (idempotent) so a prior pass that demoted-but-crashed-before-
            # reconcile is healed; ordering is load-bearing (survivor must be the
            # canonical before its junction rows are authoritative).
            self._reconcile_junction_for_demotion(
                survivor_id=survivor_id, demoted_id=sibling_id, now=now,
            )
        logger.info(
            "lift_canonical_pointer: group (vendor=%s, external_session_id=%s) "
            "survivor=%s demoted=%d",
            vendor, external_session_id, survivor_id, demoted,
        )
        return demoted

    def _reconcile_junction_for_demotion(
        self, *, survivor_id: str, demoted_id: str, now: datetime,
    ) -> None:
        """Move a demoted ex-canonical's ``session_source_kind`` rows to the survivor.

        SQL-lockdown list_sessions junction maintenance (recompute-after-re-
        election). When a duplicate-canonical group is re-elected, the demoted
        row's ``(demoted_id, kind)`` junction rows no longer describe a canonical
        group head. Each kind is re-asserted under the survivor via
        ``upsert_state`` DO-NOTHING (a kind the survivor already carries is a
        no-op — the UNIQUE ``(canonical_session_id, source_kind)`` absorbs it),
        then the stale ``(demoted_id, *)`` rows are HARD-deleted (operator
        soft-delete-opt-out: a derived-junction row has no recovery value).

        Idempotent + crash-safe: re-running merges nothing new and deletes
        already-gone rows. A stale junction row that briefly survives a crash is
        benign for the default read (the session read's ``canonical IS NULL``
        filter drops a non-canonical id); this keeps the forensic path + the
        table tidy.
        """
        stale = self._query(
            TABLE_SESSION_SOURCE_KIND,
            {"canonical_session_id": demoted_id, "is_deleted": 0},
        )
        if not stale:
            return
        for row in stale:
            self._upsert_do_nothing(
                TABLE_SESSION_SOURCE_KIND,
                {
                    "id": _new_id(ID_PREFIX_SESSION_SOURCE_KIND),
                    "namespace": NAMESPACE,
                    "canonical_session_id": survivor_id,
                    "source_kind": str(row["source_kind"]),
                    "created_at": now,
                    "updated_at": now,
                },
                conflict_columns=["canonical_session_id", "source_kind"],
                conflict_predicate=[],
            )
        self._delete(
            TABLE_SESSION_SOURCE_KIND,
            {"canonical_session_id": demoted_id},
            soft=False,
        )


__all__ = ["RELOAD_SAFE", "SessionLedgerCanonicalPointerRepairMixin"]
