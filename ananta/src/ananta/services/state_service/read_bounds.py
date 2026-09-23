"""Row bound for the unordered ``read_state`` primitive — the source-side cap.

This is the ``read_state`` half of the 2026-08-15 oversized-payload fix
(``workbench/2026-08-14_action_queue_stall_incident/INCIDENT.md``, defect D1).
An unbounded ``read_state`` over a 109,393-row table returned every row as
records; the result became an 82 MB ``deliver_result`` payload whose parse held
the GIL for two hours and froze the platform for 3h20m.

**This module deliberately mirrors the policy already ruled in
``ordered_query.py`` ("Gap-C") rather than inventing a second one.** That
module caps ``query_ordered``, refuses a request over the cap, and requires an
explicit ``unbounded=True`` to opt into a larger page — and its docstring
records that the older *silent clamp-to-cap* was REMOVED precisely because a
caller could lose the overflow off the end without consenting. Two policies for
one question is how they drift apart; one policy applied in a second place is
how it holds.

Why a ROW cap at the query, and not a result-SIZE refusal: measuring the size
of a result requires materialising the result. A row cap is compiled into the
SQL, so an over-large read never leaves Postgres — it is the only bound in this
fix that is genuinely upstream of both the serialise and the parse.

**A row cap is not sufficient on its own, and must not be recorded as if it
were.** It bounds ROWS, not BYTES: ``SELECT *`` on the single wedged action row
from 2026-06-28 returned 164 MB for ONE row. The complementary byte cap on
action payloads lives in ``ananta.core.actions.payload_bounds``. This module
stops 109k small rows; that one stops one huge row. Neither alone closes the
incident.

## Never silently truncate

A silent truncation here would be worse than the outage it prevents: it would
corrupt every analysis built on the result, where a loud refusal merely stops
one caller. So when no explicit ``limit`` is given, the provider fetches
``cap + 1`` rows and REFUSES if that many come back — proving more rows exist
without ever materialising them all. A result at or under the cap is returned
complete and exact; a result over it is an error, never a prefix.
"""

from __future__ import annotations

# **100 — the same number as ``_MAX_ORDERED_LIMIT`` in ``ordered_query.py``, so
# the platform has ONE row-bound policy rather than two.** Operator ruling,
# 2026-08-15: "soft cap of 100 works well." *Soft* means OVERRIDABLE PER CALL
# (an explicit ``limit``, or ``unbounded=True`` for a declared bound above the
# default) — it does NOT mean "silently return the first 100". The never-silently-
# truncate contract in this module's docstring is unchanged and still governs.
#
# ## Why the previous value was wrong, recorded so it is not re-derived
#
# This constant was 10,000, justified on the claim that "no current caller is
# known to legitimately exceed 10,000". **That premise was never measured, and it
# was false.** Measured 2026-08-15:
#
#   embeddings 880,762 · memory 249,525 · blob metadata 193,390 ·
#   action_results 110,431 · action_events 109,666 · sessions 27,155 ·
#   flows 24,352 · agent_message 14,268
#
# Eight tables over the old cap, and they are the platform's BUSIEST ones —
# ``action_events`` / ``action_results`` gain rows on every action executed, so
# they grow with USE, not with data an operator chose to import. An adopter with
# a smaller workload does not avoid this; they arrive later. Four reads broke
# outright: two fatal at boot, one silently disabling auto-summarisation.
#
# The generalisable error: a bound justified by an unmeasured claim about its own
# callers. The call sites were audited only after the cap shipped, and the audit
# found 9 genuine unfiltered whole-table reads plus an interface
# (``query_state``) whose four implementations disagreed about whether this bound
# applied at all. All are repaired (2026-08-15, commit a609f7949); this drop is
# the last step of that programme deliberately, because lowering the default
# before the call sites were fixed would have converted latent defects into a
# fleet-wide outage.
#
# ## Two consequences a future editor will otherwise rediscover the hard way
#
# * **``unbounded=True`` is misnamed for its main legitimate use.** It does not
#   mean "no bound" — it means "my DECLARED bound exceeds the default".
#   ``resolve_read_limit`` refuses ANY explicit limit above ``cap`` without it, so
#   every ceiling above 100 raises at resolve time, before touching the database.
# * **Chunk sizes defined as this constant auto-follow it, and their round-trip
#   count scales inversely.** ``_EXTERNAL_ID_CHUNK`` is one: the pgvector orphan
#   reconcile passing 42,500 ids goes from 5 reads to 425. Correct, and 85x the
#   round trips.
#
# WHAT BREAKS AT THIS VALUE: a caller performing an unbounded ``read_state`` that
# returns more than 100 rows gets a loud, instructive error instead of a result.
# That is intended. The remedy is in the error message: pass an explicit
# ``limit``, paginate with ``query_ordered``, or pass ``unbounded=True`` to
# consent to a declared bound above the default.
MAX_READ_ROWS = 100


class ReadBoundError(ValueError):
    """Raised when a ``read_state`` request violates the row-bound contract."""


def resolve_read_limit(
    limit: object,
    *,
    unbounded: object = False,
    table: str,
    cap: int = MAX_READ_ROWS,
) -> tuple[int, bool]:
    """Validate a ``read_state`` limit and resolve the row bound to apply.

    Mirrors :func:`ananta.services.state_service.ordered_query.parse_ordered_query`
    Gap-C semantics: at/under the cap is used as-is, over the cap is refused
    unless the caller consciously opts in with ``unbounded=True``.

    Args:
        limit: The caller's requested ``limit`` — ``None`` when absent.
        unbounded: Explicit opt-in to a scan larger than ``cap``.
        table: Table name, for the error message only.
        cap: The row cap to enforce.

    Returns:
        A ``(fetch_limit, overflow_is_error)`` pair. When the caller gave an
        explicit limit, ``fetch_limit`` is that limit and ``overflow_is_error``
        is False — the caller asked for exactly N rows and N rows is a complete
        answer to that question, not a truncation. When no limit was given,
        ``fetch_limit`` is ``cap + 1`` and ``overflow_is_error`` is True, so the
        provider can detect "more rows exist" and refuse rather than truncate.

    Raises:
        ReadBoundError: on a non-int limit, a non-positive limit, or a limit
            over ``cap`` without ``unbounded=True``.
    """
    if limit is None:
        if bool(unbounded):
            # Explicit, conscious opt-in to an unbounded scan. Honoured because
            # refusing it outright would leave a legitimate bulk reader with no
            # sanctioned path at all, which is how guards get removed wholesale.
            return (0, False)
        return (cap + 1, True)

    if not isinstance(limit, int) or isinstance(limit, bool):
        raise ReadBoundError(
            f"read_state: 'limit' must be an int (got {type(limit).__name__})",
        )
    if limit < 1:
        raise ReadBoundError(f"read_state: 'limit' must be >= 1 (got {limit})")
    if limit > cap and not bool(unbounded):
        raise ReadBoundError(
            f"read_state: limit {limit} on table {table!r} exceeds the cap "
            f"{cap}; pass unbounded=True to opt into a larger scan (silent "
            "truncation is refused — the caller must consent to the larger "
            "read). An unbounded scan of a large table is what caused the "
            "2026-08-15 action-queue freeze.",
        )
    return (limit, False)


def overflow_message(*, table: str, cap: int = MAX_READ_ROWS) -> str:
    """Build the refusal for an unbounded read that would exceed ``cap``.

    Fail-loud is only half the lesson; the other half is that the lane which
    triggered this outage was doing exactly what its brief told it to do, and a
    good error would have redirected it in seconds. So the message names the
    bound AND every sanctioned way forward.
    """
    return (
        f"read_state on table {table!r} returned more than the {cap}-row cap "
        "for a query with no explicit 'limit'. Refused rather than truncated: "
        "returning a silent prefix would corrupt any analysis built on it. "
        "Choose one — pass an explicit 'limit' (<= "
        f"{cap}) if you only need the first N rows; paginate with "
        "'query_ordered' if you need every row; or pass 'unbounded': true to "
        "consent to the full scan. An unbounded read of a 109,393-row table is "
        "what froze the platform for 3h20m on 2026-08-15 — see "
        "workbench/2026-08-14_action_queue_stall_incident/INCIDENT.md."
    )


__all__ = [
    "MAX_READ_ROWS",
    "ReadBoundError",
    "overflow_message",
    "resolve_read_limit",
]
