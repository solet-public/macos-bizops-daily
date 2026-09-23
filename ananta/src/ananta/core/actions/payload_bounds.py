"""Action-payload size bound — the guard that must run BEFORE the parse.

The 2026-08-15 outage (``workbench/2026-08-14_action_queue_stall_incident/
INCIDENT.md``) was caused by an 82 MB ``deliver_result`` payload. A worker
thread spent **two hours inside CPython's C JSON scanner** parsing it while
holding the GIL ~95% of the time; every other Python thread in the process
starved behind ``take_gil``. The action poller and all ``ananta.core``
periodic tasks stopped for 3h20m.

The load-bearing design constraint, and the reason this module exists rather
than a check inside any single verb:

    **The parse IS the harm, so the bound must precede the parse.**

A size check placed after ``json.loads`` has already caused the outage it was
meant to prevent — by the time you can measure the parsed object, the GIL has
already been held for hours. Every guard in this module therefore measures a
**raw byte length of a string that already exists**, never a parsed object.

Two enforcement points, both required, neither redundant:

* **Enqueue** (:func:`check_action_parameters_size`, called from
  ``ActionEventRecorder._build_action_data``) — measures the string that
  ``json.dumps`` had to produce anyway in order to persist the row. The caller
  learns immediately and the oversized row never reaches the database. This is
  primary: it stops the payload at the boundary where a human can still act on
  the error.
* **Claim** (:func:`check_claimed_parameters_size`, called from
  ``ActionQueuePoller._build_queued_action``) — measures ``len()`` of the raw
  ``parameters`` string as read from Postgres, before any ``json.loads``.
  ``core__action_events.parameters`` is ``ColumnType.TEXT``, so psycopg returns
  a plain ``str`` and performs no driver-side parse; this length check is
  genuinely pre-parse. It exists because rows predating the enqueue guard are
  already in the table (60 orphaned ``processing`` rows, oldest 2026-05-29,
  including a 601 MB row from 2026-06-28), and because it is the narrowest
  waist upstream of every parse site on the dispatch path.

**Disclosed honestly:** the enqueue guard does not eliminate serialisation of
an oversized payload — it measures the one ``json.dumps`` that had to happen
to persist the row, so the check itself is free, but that single serialisation
still occurs. Eliminating even that requires bounding at the source, which is
what the ``read_state`` row cap does (see ``_MAX_READ_ROWS`` in the postgres
state-management plugin). One serialisation on the producing side is not what
caused this outage; the repeated parse on the consuming side was.

**A row cap and a byte cap are not redundant.** A row cap bounds ROWS, not
BYTES: ``SELECT *`` on June's single wedged action row returned 164 MB for ONE
row. The row cap stops 109k small rows; this byte cap stops one huge row.
Neither alone closes the incident.
"""

from __future__ import annotations

from ananta.error_handling import FrameworkError

# ---------------------------------------------------------------------------
# The bound, argued from the incident's measured distribution
# ---------------------------------------------------------------------------
#
# Legitimate traffic (measured, INCIDENT.md §1): 107 ``deliver_result`` actions
# on the day of the outage averaged **2.5 MB**; the small ones sampled during
# recovery ranged 253 B – 8 KB.
#
# Fatal traffic (measured, INCIDENT.md §1/§2b/D12/D13): the payloads that
# actually wedged the platform were **82, 165, 346, 601 and 790 MB**. The
# smallest observation that has ever caused an outage is 82 MB.
#
# So there is an empty gap spanning 2.5 MB → 82 MB, a factor of ~33. The bound
# is placed at the **geometric midpoint** of that gap, which maximises the
# multiplicative headroom on both sides simultaneously:
#
#     sqrt(2.5 MB * 82 MB) ~= 14.3 MB  ->  rounded to 16 MiB
#
# At 16 MiB the bound sits ~6.4x above the largest well-characterised
# legitimate population and ~5.1x below the smallest payload ever observed to
# wedge the platform. Neither margin is tight, which is the point: a bound that
# is tight on the legitimate side gets raised by the first person it
# inconveniences, and a bound that is tight on the fatal side does not prevent
# the outage.
#
# WHAT BREAKS AT THIS VALUE, stated plainly: any caller that legitimately needs
# to move more than 16 MiB through a single action's parameters now fails at
# enqueue with a loud, instructive error instead of succeeding. On the measured
# distribution no legitimate caller does. If one appears, the correct response
# is to move the payload by reference (a blob/job handle) rather than to raise
# this number — an action payload is a control-plane message, not a data
# channel, and 16 MiB is already three orders of magnitude above the median.
#
# NOT MEASURED, and why: the live max of the legitimate population was not
# re-measured for this bound. Establishing it requires reading the
# ``parameters`` column of every action row, and the state interface performs
# no column projection (``select`` is ``SELECT *``) — so measuring the
# distribution would mean pulling the very payloads that cause the harm through
# the very process that this guard protects. The bound is therefore argued from
# the incident's captured evidence, which is real measured data, rather than
# from a fresh scan of a platform that was recovered by hand three times.
MAX_ACTION_PARAMETERS_BYTES = 16 * 1024 * 1024  # 16 MiB


def _format_bytes(size: int) -> str:
    """Render a byte count as a human-comparable string.

    The refusal message is read by a human under time pressure during an
    incident, so it carries both the exact byte count (for a filter or a
    ticket) and a rounded human unit (for judging how far over the bound the
    payload is at a glance).
    """
    mib = size / (1024 * 1024)
    if mib >= 1.0:
        return f"{size} bytes ({mib:.1f} MiB)"
    kib = size / 1024
    if kib >= 1.0:
        return f"{size} bytes ({kib:.1f} KiB)"
    return f"{size} bytes"


def _refusal_message(
    *,
    size: int,
    bound: int,
    process_key: str,
    where: str,
    remedy: str,
) -> str:
    """Build a refusal that tells the caller what to DO, not just what failed.

    Fail-loud is only half of the lesson from this incident; the other half is
    that Lane AA hit this guard's ABSENCE while doing exactly what its brief
    told it to do. A refusal that names the size and the bound but leaves the
    caller without a next step just relocates the dead end, so every refusal
    carries an explicit remedy.
    """
    return (
        f"Action payload refused at {where}: parameters for {process_key!r} are "
        f"{_format_bytes(size)}, over the {_format_bytes(bound)} bound "
        f"(over by {_format_bytes(size - bound)}). "
        f"{remedy} "
        "This bound exists because an oversized action payload holds the GIL "
        "inside the C JSON parser and starves every thread in the process — "
        "see workbench/2026-08-14_action_queue_stall_incident/INCIDENT.md."
    )


def check_action_parameters_size(
    serialized_parameters: str,
    *,
    process_key: str,
    bound: int = MAX_ACTION_PARAMETERS_BYTES,
) -> None:
    """Refuse an oversized action payload at ENQUEUE, before it is persisted.

    ``serialized_parameters`` is the string ``json.dumps`` already produced in
    order to write the row, so measuring it costs nothing extra and — critically
    — no parse happens here at all.

    Raises:
        FrameworkError: when the payload exceeds ``bound``. Raising (rather
            than truncating or dropping) is deliberate: a silent truncation
            would corrupt every analysis built on the result, where a loud
            refusal merely stops one caller.
    """
    size = len(serialized_parameters.encode("utf-8"))
    if size <= bound:
        return
    raise FrameworkError(
        message=_refusal_message(
            size=size,
            bound=bound,
            process_key=process_key,
            where="enqueue",
            remedy=(
                "Reduce the payload at its SOURCE rather than raising the "
                "bound: bound the query that produced it (pass an explicit "
                "'limit' to read_state, or paginate with query_ordered), or "
                "pass the data by reference (a blob or job handle) instead of "
                "inline in the action parameters."
            ),
        ),
        error_code="action_recorder.parameters_too_large",
        details={
            "process_key": process_key,
            "parameters_bytes": size,
            "bound_bytes": bound,
            "over_by_bytes": size - bound,
        },
    )


class OversizedActionPayloadError(Exception):
    """Raised when a CLAIMED action's stored payload exceeds the bound.

    Distinct from the enqueue :class:`FrameworkError` because the poller
    handles it differently: the row is already persisted, so it must be failed
    in place with a legible reason rather than refused to a caller who is no
    longer waiting.
    """

    def __init__(self, message: str, *, action_id: str, size: int, bound: int) -> None:
        super().__init__(message)
        self.action_id = action_id
        self.size = size
        self.bound = bound


def check_claimed_parameters_size(
    raw_parameters: str,
    *,
    action_id: str,
    process_key: str,
    bound: int = MAX_ACTION_PARAMETERS_BYTES,
) -> None:
    """Refuse an oversized STORED payload at CLAIM time, before ``json.loads``.

    ``raw_parameters`` is the ``parameters`` column exactly as psycopg returned
    it — a ``str``, because the column is ``ColumnType.TEXT``. ``len()`` on it
    is O(1) and involves no parse, which is what makes this guard able to fire
    in time. Calling ``json.loads`` first and measuring the result would be the
    outage, not the guard against it.

    Raises:
        OversizedActionPayloadError: when the stored payload exceeds ``bound``.
    """
    size = len(raw_parameters.encode("utf-8"))
    if size <= bound:
        return
    raise OversizedActionPayloadError(
        _refusal_message(
            size=size,
            bound=bound,
            process_key=process_key,
            where=f"claim of action {action_id}",
            remedy=(
                "The row is already persisted, so it is being FAILED rather "
                "than dispatched; it will not be retried. Re-run the "
                "originating request with a bounded query."
            ),
        ),
        action_id=action_id,
        size=size,
        bound=bound,
    )


__all__ = [
    "MAX_ACTION_PARAMETERS_BYTES",
    "OversizedActionPayloadError",
    "check_action_parameters_size",
    "check_claimed_parameters_size",
]
