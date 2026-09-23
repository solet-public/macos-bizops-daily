"""Lifecycle transitions + run evidence for authored-by-value joseki (Phase 6, §4.3).

Phase 3 (``authored_registration``) registered joseki cards at state ``draft``
only; state transitions and run evidence were deferred here. This module is
that Phase 6 half: the lifecycle state machine

    draft → candidate → proven → superseded → archived

plus run evidence (``run_count`` / ``last_run_at``) and a row-maintenance
``reconcile`` that fixes the Phase-3 migration crumb (a stale
``knowledge_base_path``).

Storage discipline (◆R2): lifecycle state lives in the
``thinking_authored_joseki`` table (the Q14 run-evidence table, already
declared via ``SchemaDefinition``), accessed ONLY through
``StateManagementInterface`` primitives — ``read_state`` and the predicated
``update_state`` compare-and-set (``SET state=<to> WHERE joseki_key=X AND
state=<from>``; the reported rows-affected count IS the CAS guard). Never raw
SQL. The nearest raw-SQL exemplar (``session_ledger``) is grandfathered debt,
not a pattern.

Design decisions (settled against the plan of record + the supersession
discipline article):

* **``proven`` is EARNED, not declared.** ``§4.3`` defines proven as "≥1
  recorded successful run", so it is reachable only via
  ``record_run`` (which advances candidate→proven in the SAME CAS that
  increments ``run_count`` — proven therefore implies ``run_count ≥ 1`` by
  construction). ``transition`` rejects ``proven`` as a manual target.

* **``candidate`` RE-VALIDATES against the CURRENT registry.** Promotion reads
  the stored card back from the canonical ``authored_joseki`` KB location and
  re-runs the authored-joseki validator. A card that referenced a
  since-retired process key can no longer be promoted — this is the seam to
  Tier 2 (retire-process). The stored ``knowledge_base_path`` column is NOT
  trusted for the read: a card's canonical home is ``<joseki_key>.md`` by the
  registrar's invariant, so promotion is robust against the migration crumb.

* **Retirement stamps the card, but does not de-index it.** A transition to
  ``superseded``/``archived`` writes a visible ``> **LIFECYCLE: …**`` banner
  into the card so semantic discovery surfaces its retirement with a signal.
  The heavier retrieval-INDEX exclusion (moving the card to ``.archive/`` +
  the manifest exclusion glob) is per-type KB-lifecycle work deferred to §4.6
  (Tier 3) — a deliberate scope line, not an oversight.

* **Transitions are idempotent + self-healing.** Transitioning to the state a
  card already holds is a benign no-op (Phase-4 resume ethos), and the no-op
  path still re-asserts the retirement banner so a card whose first stamp
  failed heals on re-run.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, NoReturn, Protocol

from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import (
    JOSEKI_STATE_ARCHIVED,
    JOSEKI_STATE_CANDIDATE,
    JOSEKI_STATE_DRAFT,
    JOSEKI_STATE_PROVEN,
    JOSEKI_STATE_SUPERSEDED,
    ErrorCode,
)

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from default_thinking_plugin.authored_validation import AuthoredValidationReport

_JOSEKI_TABLE = "thinking_authored_joseki"

# Legal FROM-states per ``transition`` target. ``proven`` is intentionally
# absent — it is earned via ``record_run``, never a manual transition.
_LEGAL_FROM: dict[str, frozenset[str]] = {
    JOSEKI_STATE_CANDIDATE: frozenset({JOSEKI_STATE_DRAFT}),
    JOSEKI_STATE_SUPERSEDED: frozenset(
        {JOSEKI_STATE_DRAFT, JOSEKI_STATE_CANDIDATE, JOSEKI_STATE_PROVEN},
    ),
    JOSEKI_STATE_ARCHIVED: frozenset(
        {
            JOSEKI_STATE_DRAFT,
            JOSEKI_STATE_CANDIDATE,
            JOSEKI_STATE_PROVEN,
            JOSEKI_STATE_SUPERSEDED,
        },
    ),
}
_MANUAL_TARGETS = frozenset(_LEGAL_FROM)
# A run may only be recorded against a validated card (candidate) or one that
# is already proven — never a raw draft, a superseded, or an archived card.
_RUN_ELIGIBLE = frozenset({JOSEKI_STATE_CANDIDATE, JOSEKI_STATE_PROVEN})

# A retirement banner line, matched for idempotent re-stamping.
_BANNER_LINE_RE = re.compile(r"^> \*\*LIFECYCLE:.*$\n?", re.MULTILINE)


class LifecycleStateStore(Protocol):
    """State access for the joseki lifecycle — read + predicated CAS.

    ``update_state`` is the compare-and-set: it returns an envelope whose
    ``data.result.updated`` carries the rows-affected count (0 ⇒ the WHERE
    predicate matched nothing — the guard).
    """

    def read_state(
        self, namespace: str, query: dict[str, object],
    ) -> dict[str, Any]: ...

    def update_state(
        self,
        namespace: str,
        query: dict[str, object],
        updates: dict[str, object],
    ) -> dict[str, Any]: ...


class JosekiCardReader(Protocol):
    """Read a joseki card from the authored_joseki knowledge base."""

    def read(self, path: str) -> str:
        """Return the card markdown at *path*, or ``""`` when absent."""
        ...


class JosekiCardWriter(Protocol):
    """Write a joseki card into the authored_joseki knowledge base."""

    def write(self, path: str, content: str) -> None:
        """Create or update the card at *path* (KB-relative)."""
        ...


class AuthoredJosekiLifecycle:
    """Lifecycle state machine + run evidence over the authored-joseki row.

    Collaborators are injected so the whole engine is exercisable offline
    (``BootstrapDatabaseStorage`` / recording stubs); no service is resolved
    inside.
    """

    def __init__(
        self,
        *,
        state_store: LifecycleStateStore,
        card_reader: JosekiCardReader,
        card_writer: JosekiCardWriter,
        validate: Callable[[str, str | None], AuthoredValidationReport],
        now: Callable[[], datetime],
        namespace: str,
    ) -> None:
        self._state = state_store
        self._reader = card_reader
        self._writer = card_writer
        self._validate = validate
        self._now = now
        self._namespace = namespace

    # -- public verbs -------------------------------------------------------

    def transition(
        self,
        *,
        joseki_key: str,
        target_state: str,
        superseded_by: str | None = None,
    ) -> dict[str, Any]:
        """Advance a joseki to *target_state* (candidate/superseded/archived).

        ``proven`` is rejected — it is earned via :meth:`record_run`. Returns
        the previous + new state; ``status`` is ``"transitioned"`` or, when the
        card already held the target, ``"unchanged"``.
        """
        self._reject_non_manual_target(target_state)
        row = self._require_row(joseki_key)
        current = str(row["state"])

        if current == target_state:
            return self._idempotent_transition(joseki_key, row, superseded_by)

        if current not in _LEGAL_FROM[target_state]:
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} cannot transition {current!r} → "
                    f"{target_state!r} — legal source states for "
                    f"{target_state!r} are "
                    f"{sorted(_LEGAL_FROM[target_state])}"
                ),
                error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
            )

        if target_state == JOSEKI_STATE_CANDIDATE:
            self._assert_card_validates(joseki_key)
        if target_state == JOSEKI_STATE_SUPERSEDED:
            self._assert_supersession(joseki_key, superseded_by)

        updates: dict[str, object] = {"state": target_state}
        if target_state == JOSEKI_STATE_SUPERSEDED:
            updates["superseded_by"] = superseded_by
        self._cas_or_raise(joseki_key, current, updates)

        if target_state in (JOSEKI_STATE_SUPERSEDED, JOSEKI_STATE_ARCHIVED):
            self._ensure_banner(joseki_key, target_state, superseded_by)

        return {
            "joseki_key": joseki_key,
            "previous_state": current,
            "state": target_state,
            "superseded_by": superseded_by if target_state == JOSEKI_STATE_SUPERSEDED else None,
            "status": "transitioned",
        }

    def record_run(
        self,
        *,
        joseki_key: str,
        wbs_id: str | None = None,
    ) -> dict[str, Any]:
        """Record ONE successful run of a joseki (run evidence, §4.3 proven gate).

        Increments ``run_count`` and stamps ``last_run_at``; when the card is a
        ``candidate`` it advances to ``proven`` in the same predicated update,
        so ``proven`` always implies ``run_count ≥ 1``. Only ``candidate`` /
        ``proven`` cards accept a run — a run against a raw ``draft`` (never
        validated), a ``superseded`` or an ``archived`` card is a state error.
        """
        row = self._require_row(joseki_key)
        current = str(row["state"])
        if current not in _RUN_ELIGIBLE:
            raise FrameworkError(
                message=(
                    f"cannot record a run on joseki {joseki_key!r} in state "
                    f"{current!r} — only {sorted(_RUN_ELIGIBLE)} accept run "
                    f"evidence (validate to 'candidate' first)"
                ),
                error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
            )

        prior = row.get("run_count")
        prior_count = int(prior) if prior is not None else 0
        next_count = prior_count + 1

        updates: dict[str, object] = {
            "run_count": next_count,
            "last_run_at": self._now(),
        }
        new_state = current
        if current == JOSEKI_STATE_CANDIDATE:
            updates["state"] = JOSEKI_STATE_PROVEN
            new_state = JOSEKI_STATE_PROVEN

        # CAS on BOTH the state and the prior run_count so the increment is
        # atomic against a concurrent run (optimistic concurrency); a NULL
        # prior matches via the ``is_null`` filter op.
        filters: dict[str, object] = {
            "joseki_key": joseki_key,
            "state": current,
            "is_deleted": 0,
            "run_count": {"op": "is_null"} if prior is None else prior_count,
        }
        affected = self._update(filters, updates)
        if affected == 0:
            self._raise_raced(joseki_key)

        return {
            "joseki_key": joseki_key,
            "state": new_state,
            "run_count": next_count,
            "wbs_id": wbs_id,
            "status": "run_recorded",
        }

    def get(self, *, joseki_key: str) -> dict[str, Any]:
        """Read the lifecycle row (observability). ``found`` is False when absent."""
        row = self._read_row(joseki_key)
        if row is None:
            return {"found": False, "joseki_key": joseki_key}
        run_count = row.get("run_count")
        return {
            "found": True,
            "joseki_key": str(row.get("joseki_key", joseki_key)),
            "state": str(row.get("state", "")),
            "provenance": row.get("provenance"),
            "knowledge_base_path": row.get("knowledge_base_path"),
            "superseded_by": row.get("superseded_by"),
            "run_count": int(run_count) if run_count is not None else 0,
            "last_run_at": _as_text(row.get("last_run_at")),
        }

    def reconcile_row(self, *, joseki_key: str) -> dict[str, Any]:
        """Row maintenance: normalise ``knowledge_base_path`` to the canonical
        ``<joseki_key>.md``.

        Fixes the Phase-3 migration crumb (a card migrated into the dedicated
        ``authored_joseki`` KB whose row still carried a ``thinking_plans``-era
        path). Idempotent.
        """
        row = self._require_row(joseki_key)
        canonical = f"{joseki_key}.md"
        current_path = row.get("knowledge_base_path")
        if current_path == canonical:
            return {
                "joseki_key": joseki_key,
                "knowledge_base_path": canonical,
                "status": "unchanged",
            }
        affected = self._update(
            {
                "joseki_key": joseki_key,
                "knowledge_base_path": current_path,
                "is_deleted": 0,
            },
            {"knowledge_base_path": canonical},
        )
        if affected == 0:
            # The compare-and-set matched nothing though we read a non-canonical
            # path: the row's path changed underneath us. Re-read — a row that
            # is NOW canonical is a benign no-op; any other value is a genuine
            # race that must fail loud, not be silently swallowed as 'unchanged'
            # (Codex Tier-1 MAJOR-2; mirrors transition / record_run).
            fresh = self._require_row(joseki_key)
            if fresh.get("knowledge_base_path") == canonical:
                return {
                    "joseki_key": joseki_key,
                    "knowledge_base_path": canonical,
                    "status": "unchanged",
                }
            self._raise_raced(joseki_key)
        return {
            "joseki_key": joseki_key,
            "previous_knowledge_base_path": current_path,
            "knowledge_base_path": canonical,
            "status": "reconciled",
        }

    # -- transition helpers -------------------------------------------------

    @staticmethod
    def _reject_non_manual_target(target_state: str) -> None:
        """Only candidate/superseded/archived are manual transition targets."""
        if target_state in _MANUAL_TARGETS:
            return
        if target_state == JOSEKI_STATE_PROVEN:
            raise FrameworkError(
                message=(
                    "'proven' is not a manual transition — it is earned via "
                    "record_authored_joseki_run once a candidate has ≥1 "
                    "recorded successful run"
                ),
                error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
            )
        raise FrameworkError(
            message=(
                f"unknown target state {target_state!r} — manual transitions "
                f"target one of {sorted(_MANUAL_TARGETS)}"
            ),
            error_code=ErrorCode.PARAMETER_ERROR,
        )

    def _idempotent_transition(
        self,
        joseki_key: str,
        row: dict[str, Any],
        superseded_by: str | None,
    ) -> dict[str, Any]:
        """No-op when the card already holds the target; re-assert the banner.

        For ``superseded`` a differing ``superseded_by`` is a conflict, not a
        no-op — the replacement pointer is load-bearing.
        """
        current = str(row["state"])
        if current == JOSEKI_STATE_SUPERSEDED:
            existing = row.get("superseded_by")
            if superseded_by is not None and superseded_by != existing:
                raise FrameworkError(
                    message=(
                        f"joseki {joseki_key!r} is already superseded by "
                        f"{existing!r}; refusing to repoint it to "
                        f"{superseded_by!r}"
                    ),
                    error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
                )
            self._ensure_banner(joseki_key, current, existing)
        elif current == JOSEKI_STATE_ARCHIVED:
            self._ensure_banner(joseki_key, current, None)
        return {
            "joseki_key": joseki_key,
            "previous_state": current,
            "state": current,
            "superseded_by": row.get("superseded_by"),
            "status": "unchanged",
        }

    def _assert_card_validates(self, joseki_key: str) -> None:
        """Candidate gate: the stored card must validate against LIVE schemas.

        The row key is threaded into the validator as ``expected_joseki_key``
        so the card's own ``JOSEKI_KEY:`` header MUST match the lifecycle row
        it is being promoted for — a card sitting at ``<row_key>.md`` whose
        header names a *different* key is an identity mismatch and is rejected,
        never promoted (Codex Tier-1 BLOCKER-1).
        """
        content = self._reader.read(f"{joseki_key}.md")
        if not content:
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} cannot be promoted — its card was "
                    f"not found at the canonical authored_joseki path "
                    f"{joseki_key}.md"
                ),
                error_code=ErrorCode.JOSEKI_NOT_FOUND,
            )
        report = self._validate(content, joseki_key)
        if not report.valid:
            bullets = "\n".join(f"  - {e}" for e in report.errors)
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} cannot be promoted to 'candidate' "
                    f"— its card fails validation against the current process "
                    f"registry ({len(report.errors)} error(s)):\n{bullets}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )

    def _assert_supersession(
        self, joseki_key: str, superseded_by: str | None,
    ) -> None:
        """Supersession requires a real, non-self, non-archived replacement."""
        if not superseded_by:
            raise FrameworkError(
                message=(
                    "superseding a joseki requires 'superseded_by' naming the "
                    "replacement card"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        if superseded_by == joseki_key:
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} cannot supersede itself"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        replacement = self._read_row(superseded_by)
        if replacement is None:
            raise FrameworkError(
                message=(
                    f"replacement joseki {superseded_by!r} is not registered — "
                    f"register it before superseding {joseki_key!r} to it"
                ),
                error_code=ErrorCode.JOSEKI_NOT_FOUND,
            )
        if str(replacement.get("state")) == JOSEKI_STATE_ARCHIVED:
            raise FrameworkError(
                message=(
                    f"replacement joseki {superseded_by!r} is archived — a "
                    f"retired card cannot be the successor for {joseki_key!r}"
                ),
                error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
            )

    def _ensure_banner(
        self,
        joseki_key: str,
        state: str,
        superseded_by: str | None,
    ) -> None:
        """Idempotently stamp the retirement banner onto the stored card.

        Re-writes only when the banner changes, so re-running a transition (or
        the idempotent no-op path) heals a card whose first stamp failed and
        updates a superseded→archived banner in place.
        """
        content = self._reader.read(f"{joseki_key}.md")
        if not content:
            return
        stamped = _apply_banner(content, state, superseded_by)
        if stamped != content:
            self._writer.write(f"{joseki_key}.md", stamped)

    # -- state-store plumbing ----------------------------------------------

    def _read_row(self, joseki_key: str) -> dict[str, Any] | None:
        result = self._state.read_state(
            namespace=self._namespace,
            query={
                "table": _JOSEKI_TABLE,
                "filters": {"joseki_key": joseki_key, "is_deleted": 0},
                "limit": 1,
            },
        )
        records = result.get("data", {}).get("records") or []
        return records[0] if records else None

    def _require_row(self, joseki_key: str) -> dict[str, Any]:
        row = self._read_row(joseki_key)
        if row is None:
            raise FrameworkError(
                message=(
                    f"joseki {joseki_key!r} is not registered — no lifecycle "
                    f"row exists"
                ),
                error_code=ErrorCode.JOSEKI_NOT_FOUND,
            )
        return row

    def _cas_or_raise(
        self, joseki_key: str, from_state: str, updates: dict[str, object],
    ) -> None:
        affected = self._update(
            {"joseki_key": joseki_key, "state": from_state, "is_deleted": 0},
            updates,
        )
        if affected == 0:
            self._raise_raced(joseki_key)

    def _update(
        self, filters: dict[str, object], updates: dict[str, object],
    ) -> int:
        result = self._state.update_state(
            namespace=self._namespace,
            query={"table": _JOSEKI_TABLE, "filters": filters},
            updates=updates,
        )
        return _rows_affected(result)

    def _raise_raced(self, joseki_key: str) -> NoReturn:
        row = self._read_row(joseki_key)
        actual = str(row["state"]) if row else "<deleted>"
        raise FrameworkError(
            message=(
                f"joseki {joseki_key!r} changed underneath the transition — "
                f"it is now in state {actual!r}; re-read and retry"
            ),
            error_code=ErrorCode.JOSEKI_STATE_CONFLICT,
        )


def _apply_banner(content: str, state: str, superseded_by: str | None) -> str:
    """Return *content* with a single current LIFECYCLE banner at the top."""
    body = _BANNER_LINE_RE.sub("", content).lstrip("\n")
    if state == JOSEKI_STATE_SUPERSEDED:
        target = f" — replaced by `{superseded_by}`; use that card for new work." if superseded_by else "."
        banner = f"> **LIFECYCLE: SUPERSEDED**{target}"
    else:
        banner = (
            "> **LIFECYCLE: ARCHIVED** — this joseki is retired; do not use it "
            "for new work."
        )
    return f"{banner}\n\n{body}"


def _rows_affected(result: dict[str, Any]) -> int:
    """Extract the ``data.result.updated`` rows-affected count from an envelope."""
    data = result.get("data")
    if not isinstance(data, dict):
        return 0
    inner = data.get("result")
    if not isinstance(inner, dict):
        return 0
    updated = inner.get("updated", 0)
    return int(updated) if isinstance(updated, int) else 0


def _as_text(value: object) -> str | None:
    """Serialise a timestamp-ish value to text for the read envelope."""
    if value is None:
        return None
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        return str(isoformat())
    return str(value)
