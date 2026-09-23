"""T1 S3 (budget-line rollup, ruling of record + seat's S3 design ruling
2026-08-05) -- ``budget_report`` aggregates token spend per ``budget_line``
(optionally scoped to one ``lane_id``/``budget_line``) by joining THIS
plugin's own ``managed_session``/``session_claude_mapping`` tables against
``session_ledger_service``'s ``session``/``event`` tables.

FIRST cross-plugin state read of its kind in this codebase (no existing
plugin reads session_ledger's tables directly) -- sanctioned by the seat for
v1 on the grounds that every ``StateManagementInterface`` method takes
``namespace`` as an explicit per-call argument (a handle is not bound to one
namespace), so this uses the SAME primitive the whole lane already uses
everywhere else, just pointed at ``session_ledger``'s namespace via its own
PUBLIC schema constants (``NAMESPACE``/``TABLE_SESSION``/``TABLE_EVENT`` +
the ``SourceVendor`` enum) -- zero raw SQL, zero reach into session_ledger's
internal Python classes or its live singleton. Named as a follow-up for the
Architect's T1-close review to decide whether this stays a direct read or
later moves behind a dedicated session_ledger read verb.

Sibling-row resolution (usage-capture-attribution lane, 2026-08-06): the v1
premise above -- "no duplicate-session class exists yet" -- was FALSIFIED by
measurement (workbench/2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md,
D2 addendum 2/3): a single ``external_session_id`` can back MULTIPLE
``session_ledger__session`` rows sharing one ``(vendor, external_session_id)``
today -- a thin overlay source (e.g. ``claude_code_history``) alongside the
rich per-message source (``claude_code_local``), and even multiple rows of
the SAME rich source_kind across duplicate/re-registered ``__source`` rows
(a distinct, separately-tracked defect -- source registration is not
restart-idempotent). ``_resolve_usage_events_for_claude_session`` below
resolves this: it considers every row sharing the external_session_id, and
if more than one carries usage-bearing events, selects exactly ONE (the
greatest ``event_count``, ties broken by ``last_event_at`` then ``id`` for
full order-independent determinism) rather than summing across rows -- a
naive sum would double-count overlapping/duplicate ingestion of the same
real turns. ``sessions_multi_row_resolved`` on the report's buckets
(rail: D4 declared-exclusions) surfaces exactly how many covered sessions
required this disambiguation, so it is never silent. M18's own
``canonical_external_session_id`` is deliberately NOT consulted as a
usage-authority signal -- it governs cross-source-kind grouping for M18's
own search/summary purposes; treating it as meaningful for usage was the
assumption that broke.

D4 exclusion-scoping (close-out, 2026-08-06 -- seat ratification on the
lane's D3 measurement, workbench
2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md): a
lane's ``sessions_uncovered`` used to conflate three different epistemic
states into one silent bucket. It no longer does. Every
``managed_session`` row now lands in exactly one of five buckets:

- ``sessions_excluded_operator_hosted`` -- ``host=operator``. The
  SessionStart hook is only ever wired by the tmux/headless adapters;
  operator-launched rows are permanently out of the hook's contract, by
  design, regardless of when they were created.
- ``sessions_excluded_pre_landing`` -- created before commit
  ``a61267399``'s landing instant (``_HOOK_LANDING_AT``, 2026-08-05
  16:36:06Z), the SessionStart hook + adapter wiring. These rows could
  never have had the hook wired into their pinned settings; measured (D1)
  and ratified (D3) as permanently unattributable -- a probabilistic
  time-correlation backfill was considered and explicitly REJECTED
  (dozens of concurrent claude_code sessions active in the same
  historical windows make any such match a guess, and a wrong guess
  silently misattributes one worker's real spend to another, which is
  worse than an honest gap).
- ``sessions_no_llm_turns`` -- in scope, every claude_session_id this
  worker ever mapped to resolved to a real ``session_ledger__session``
  (we hold its full ledger group), and NONE of them carry a single
  usage-bearing event. A TRUE MEASURED ZERO, not a gap -- distinct from
  both "covered" (usage found) and "uncovered" (attribution unresolved),
  because folding a confirmed zero into either misreads the numbers.
- ``sessions_uncovered`` -- in scope, but no mapping row exists at all, OR
  at least one mapped claude_session_id never resolved to any
  ``session_ledger__session`` row (session_ledger hasn't ingested it yet,
  or never will). The ONLY class that represents a genuine, still-open
  attribution gap.
- ``sessions_covered`` -- usage found and attributed (``sessions_multi_row_resolved``
  is this class's own sub-signal, from the sibling-row resolution above).

The exclusion checks run FIRST, in :func:`_apply_row`, and return before
touching the mapping table at all -- an excluded row is never queried for
its own sake, only counted. Every one of the five counts is a top-level
field on the report's buckets: the D4 bar ("attributable-scope coverage
should read `sessions_uncovered=0` when the pipeline is healthy") is now
actually reachable, because the historical debris that could never be
captured no longer inflates the denominator it can never clear.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.state_results import require_records
from ananta.llm.session_ledger.schema import NAMESPACE as SESSION_LEDGER_NAMESPACE
from ananta.llm.session_ledger.schema import TABLE_EVENT as SESSION_LEDGER_TABLE_EVENT
from ananta.llm.session_ledger.schema import TABLE_SESSION as SESSION_LEDGER_TABLE_SESSION
from ananta.llm.session_ledger.types import SourceVendor

from .schema import SESSION_HOST_OPERATOR
from .session_claude_mapping_store import list_session_claude_mappings
from .session_lifecycle_store import iter_managed_sessions_unbounded, list_managed_sessions

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

# bool is an int subclass in Python; a stray boolean field in a vendor's
# verbatim usage_json (there are none today, but usage_json carries no
# fixed schema by design -- S2a's own ruling) must never silently coerce
# into the numeric sum.
_NUMERIC_TYPES = (int, float)


def _sum_numeric_fields(target: dict[str, float], source: dict[str, Any]) -> None:
    """Adds every numeric field in ``source`` into ``target``, generically --
    ``usage_json`` carries no fixed schema (S2a's own verbatim-per-vendor
    design), so this never hardcodes a vendor's field names."""
    for key, value in source.items():
        if isinstance(value, _NUMERIC_TYPES) and not isinstance(value, bool):
            target[key] = target.get(key, 0) + value


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 ``event_at`` cell to an aware (UTC) datetime.

    Same private-copy convention as ``session_sweep.py`` and
    ``session_claude_mapping_ingest.py`` (state ``DATETIME`` columns read
    back offset-naive; coerce once at this boundary)."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


# D4 exclusion-scoping (usage-capture-attribution lane, 2026-08-06 close-out
# ruling): commit a61267399's landing instant (SessionStart hook + adapter
# wiring). A managed_session created before this could never have had the
# hook wired into its pinned settings, regardless of host -- see the module
# docstring's "attributable scope" note. Parsed once at import time; the
# string is fixed history, never expected to change.
_HOOK_LANDING_AT = _parse_iso("2026-08-05T16:36:06+00:00")


@dataclass
class _Bucket:
    """One (budget_line) or (budget_line, model) aggregate. Seat's S3 rails:
    ``sessions_uncovered`` (rail 3, the no-silent-caps disclosure) and
    ``as_of`` (rail 2, the staleness marker) are first-class, not
    afterthoughts. D4 close-out (2026-08-06): ``sessions_uncovered`` now
    means ONLY a genuine attribution gap within attributable scope --
    ``sessions_no_llm_turns`` and the two ``sessions_excluded_*`` counts are
    each their own declared class, never silently folded into "uncovered"
    (see module docstring)."""

    sessions_covered: int = 0
    sessions_uncovered: int = 0
    sessions_no_llm_turns: int = 0
    sessions_multi_row_resolved: int = 0
    sessions_excluded_pre_landing: int = 0
    sessions_excluded_operator_hosted: int = 0
    as_of: datetime | None = None
    usage: dict[str, float] = field(default_factory=dict)

    def add_usage(self, usage_json: dict[str, Any], event_at: datetime | None) -> None:
        _sum_numeric_fields(self.usage, usage_json)
        if event_at is not None and (self.as_of is None or event_at > self.as_of):
            self.as_of = event_at

    def to_dict(self) -> dict[str, Any]:
        return {
            "sessions_covered": self.sessions_covered,
            "sessions_uncovered": self.sessions_uncovered,
            "sessions_no_llm_turns": self.sessions_no_llm_turns,
            "sessions_multi_row_resolved": self.sessions_multi_row_resolved,
            "sessions_excluded_pre_landing": self.sessions_excluded_pre_landing,
            "sessions_excluded_operator_hosted": self.sessions_excluded_operator_hosted,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "usage": dict(self.usage),
        }


def _usage_events_for_ledger_session(
    state: StateManagementInterface, ledger_session_id: str,
) -> list[dict[str, Any]]:
    """Every ``session_ledger__event`` row for ``ledger_session_id`` that
    carries a populated ``usage_json`` (server-side filtered via
    ``{"op": "is_not_null"}`` -- never a bare ``None`` filter value, which
    the real provider compiles to an always-false ``col = NULL``)."""
    result = state.query_state(
        SESSION_LEDGER_NAMESPACE,
        {
            "table": SESSION_LEDGER_TABLE_EVENT,
            "filters": {
                "session_id": ledger_session_id,
                "usage_json": {"op": "is_not_null"},
                "is_deleted": 0,
            },
        },
    )
    return require_records(result)


def _row_selection_key(row: dict[str, Any]) -> tuple[int, float, str]:
    """Sort key for picking ONE row among several usage-bearing siblings that
    share an ``external_session_id`` (2026-08-06 sibling-row defect, see
    module docstring). Ascending sort on this key puts the desired winner
    FIRST: greatest ``event_count`` (negated -- larger count sorts earlier),
    then most-recent ``last_event_at`` (negated epoch seconds -- newer sorts
    earlier; unparsable/missing sorts last via epoch 0), then ``id``
    ascending as a final, fully order-independent tiebreak."""
    event_count = int(row.get("event_count") or 0)
    parsed = _parse_iso(row.get("last_event_at"))
    epoch = parsed.timestamp() if parsed is not None else 0.0
    return (-event_count, -epoch, str(row.get("id") or ""))


def _resolve_usage_events_for_claude_session(
    state: StateManagementInterface, claude_session_id: str,
) -> tuple[list[dict[str, Any]], bool, bool]:
    """``claude_session_id`` (from ``session_claude_mapping``) -> the usage-
    bearing events of the ONE ``session_ledger__session`` row that should be
    attributed for it, whether picking that row required disambiguating
    among multiple usage-bearing siblings, and whether ANY
    ``session_ledger__session`` row resolved at all for this
    ``claude_session_id`` (regardless of usage) -- the third value is what
    lets the caller distinguish a confirmed TRUE ZERO (rows exist, none
    carry usage -- ``sessions_no_llm_turns``) from an unresolved gap (no
    row at all -- session_ledger hasn't ingested it yet, or never will --
    ``sessions_uncovered``). D4 close-out, 2026-08-06.

    Restricted to the claude_code vendor (the only vendor with populated
    ``usage_json`` this wave -- S2b). A single ``external_session_id`` can
    back MULTIPLE rows today (module docstring) -- every candidate row's own
    events are checked; rows with no usage-bearing event are dropped
    entirely. If more than one candidate remains, exactly ONE is selected via
    :func:`_row_selection_key` -- usage is NEVER summed across rows, since
    that would double-count overlapping/duplicate ingestion of the same real
    turns.

    Named residual, chosen deliberately (coordinator-seat ratification, 2026-08-06):
    if a feed ever rotated MID-SESSION such that two sibling rows each carry
    a genuinely DISJOINT half of the real turns (rather than the measured
    overlapping-duplicate shape), this max-``event_count`` selection
    UNDER-COUNTS -- only the biggest row's usage is kept, the other row's
    usage is dropped. This is chosen deliberately over summing across rows,
    which would silently double-count the measured overlapping-duplicate
    failure mode with no comparable tell -- under-counting is bounded and
    surfaced (the caller's ``sessions_multi_row_resolved`` names exactly
    which sessions took this path; an audit can see it), over-counting from
    a naive sum would not be. The full fix for the disjoint-split case is
    event-identity dedup (compare individual events, not whole rows), which
    belongs with the source-registration/schema-debt follow-on named in the
    module docstring, not this resolver."""
    result = state.query_state(
        SESSION_LEDGER_NAMESPACE,
        {
            "table": SESSION_LEDGER_TABLE_SESSION,
            "filters": {
                "vendor": SourceVendor.CLAUDE_CODE.value,
                "external_session_id": claude_session_id,
                "is_deleted": 0,
            },
        },
    )
    rows = require_records(result)
    if not rows:
        return [], False, False
    candidates: list[tuple[dict[str, Any], list[dict[str, Any]]]] = []
    for row in rows:
        events = _usage_events_for_ledger_session(state, str(row["id"]))
        if events:
            candidates.append((row, events))
    if not candidates:
        return [], False, True
    if len(candidates) == 1:
        return candidates[0][1], False, True
    candidates.sort(key=lambda item: _row_selection_key(item[0]))
    return candidates[0][1], True, True


_STATUS_COVERED = "covered"
_STATUS_NO_LLM_TURNS = "no_llm_turns"
_STATUS_UNCOVERED = "uncovered"


@dataclass
class _RowClassification:
    """One ``managed_session`` row's D4 classification (2026-08-06
    close-out): ``status`` is exactly one of ``_STATUS_COVERED`` (real usage
    found), ``_STATUS_NO_LLM_TURNS`` (every claude_session_id this worker
    ever mapped to resolved to a real session_ledger__session -- we HOLD its
    full ledger group -- and NONE of them carry a usage-bearing event: a
    TRUE MEASURED ZERO, not a gap), or ``_STATUS_UNCOVERED`` (no mapping row
    at all, or at least one claude_session_id never resolved to any ledger
    session -- an open attribution gap, not a confirmed zero)."""

    usage: dict[str, float]
    as_of: datetime | None
    multi_row_resolved: bool
    status: str


def _accumulate_usage_events(
    events: list[dict[str, Any]], usage: dict[str, float], as_of: datetime | None,
) -> datetime | None:
    """Sums every event's ``usage_json`` into ``usage`` (mutated in place --
    the caller's running total across possibly-many claude_session_ids) and
    returns the later of ``as_of`` and these events' own latest
    ``event_at``. Split out of :func:`_collect_row_usage` to keep it under
    the radon cc threshold."""
    for event in events:
        usage_json = event.get("usage_json")
        if not isinstance(usage_json, dict):
            continue
        _sum_numeric_fields(usage, usage_json)
        event_at = _parse_iso(event.get("event_at"))
        if event_at is not None and (as_of is None or event_at > as_of):
            as_of = event_at
    return as_of


def _collect_row_usage(
    state: StateManagementInterface, agent_instance_id: str,
) -> _RowClassification:
    """Every usage figure attributable to one ``managed_session`` row, summed
    across EVERY distinct ``claude_session_id`` it has ever been observed
    under (the ONE-TO-MANY rotation history S1's mapping table exists to
    capture -- a worker's total spend is the sum over its whole lifetime,
    not just its current session), plus its D4 status classification (see
    :class:`_RowClassification`)."""
    usage: dict[str, float] = {}
    as_of: datetime | None = None
    multi_row_resolved = False
    mappings = list_session_claude_mappings(state, agent_instance_id)
    claude_session_ids = {
        str(m["claude_session_id"]) for m in mappings if m.get("claude_session_id")
    }
    if not claude_session_ids:
        return _RowClassification(
            usage={}, as_of=None, multi_row_resolved=False, status=_STATUS_UNCOVERED,
        )
    all_resolved = True
    for claude_session_id in claude_session_ids:
        events, was_multi_row, resolved_any = _resolve_usage_events_for_claude_session(
            state, claude_session_id,
        )
        multi_row_resolved = multi_row_resolved or was_multi_row
        all_resolved = all_resolved and resolved_any
        as_of = _accumulate_usage_events(events, usage, as_of)
    status = _status_for(usage=usage, all_resolved=all_resolved)
    return _RowClassification(
        usage=usage, as_of=as_of, multi_row_resolved=multi_row_resolved, status=status,
    )


def _status_for(*, usage: dict[str, float], all_resolved: bool) -> str:
    """The D4 status classification's own decision, isolated so
    :func:`_collect_row_usage` reads as one straight-line accumulation."""
    if usage:
        return _STATUS_COVERED
    if all_resolved:
        return _STATUS_NO_LLM_TURNS
    return _STATUS_UNCOVERED


_EXCLUDED_OPERATOR_HOSTED = "operator_hosted"
_EXCLUDED_PRE_LANDING = "pre_landing"


def _exclusion_class_for_row(row: dict[str, Any]) -> str | None:
    """D4 close-out (2026-08-06): ``None`` means in attributable scope.
    Otherwise one of the two exclusion class names -- checked in this
    order because ``host=operator`` excludes regardless of timing (the
    hook contract never applies to it at all), so it must win over the
    landing-date check rather than the two being evaluated independently."""
    if str(row.get("host") or "") == SESSION_HOST_OPERATOR:
        return _EXCLUDED_OPERATOR_HOSTED
    created_at = _parse_iso(row.get("created_at"))
    if (
        created_at is not None
        and _HOOK_LANDING_AT is not None
        and created_at < _HOOK_LANDING_AT
    ):
        return _EXCLUDED_PRE_LANDING
    return None


def _apply_classification(classification: _RowClassification, top: _Bucket, sub: _Bucket) -> None:
    """Folds one row's already-computed :class:`_RowClassification` into its
    two buckets. Split out of :func:`_apply_row` to keep it under the radon
    cc threshold."""
    if classification.status == _STATUS_COVERED:
        top.sessions_covered += 1
        sub.sessions_covered += 1
        top.add_usage(classification.usage, classification.as_of)
        sub.add_usage(classification.usage, classification.as_of)
        if classification.multi_row_resolved:
            top.sessions_multi_row_resolved += 1
            sub.sessions_multi_row_resolved += 1
    elif classification.status == _STATUS_NO_LLM_TURNS:
        top.sessions_no_llm_turns += 1
        sub.sessions_no_llm_turns += 1
    else:
        top.sessions_uncovered += 1
        sub.sessions_uncovered += 1


def _apply_row(
    state: StateManagementInterface,
    row: dict[str, Any],
    *,
    top_buckets: dict[str, _Bucket],
    model_buckets: dict[tuple[str, str], _Bucket],
) -> None:
    """One ``managed_session`` row's worth of :func:`build_budget_report` --
    split out to keep the outer loop under the radon cc threshold (mirrors
    ``session_sweep.py``'s ``_mark_one_overdue`` split).

    D4 close-out (2026-08-06): an excluded row (see
    :func:`_exclusion_class_for_row`) is declared into its own
    ``sessions_excluded_*`` count and returns before touching the
    mapping/ledger lookup at all -- it was never eligible for capture by
    design (ratified no-backfill scope; see the module docstring and the
    findings file's D3 section)."""
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if not agent_instance_id:
        return
    row_budget_line = str(row.get("budget_line") or "")
    row_model = str(row.get("model") or "")
    top = top_buckets.setdefault(row_budget_line, _Bucket())
    sub = model_buckets.setdefault((row_budget_line, row_model), _Bucket())

    exclusion = _exclusion_class_for_row(row)
    if exclusion == _EXCLUDED_OPERATOR_HOSTED:
        top.sessions_excluded_operator_hosted += 1
        sub.sessions_excluded_operator_hosted += 1
        return
    if exclusion == _EXCLUDED_PRE_LANDING:
        top.sessions_excluded_pre_landing += 1
        sub.sessions_excluded_pre_landing += 1
        return

    classification = _collect_row_usage(state, agent_instance_id)
    _apply_classification(classification, top, sub)


def build_budget_report(
    state: StateManagementInterface, *, lane_id: str = "", budget_line: str = "",
) -> dict[str, Any]:
    """§T1 S3 ``budget_report`` -- one entry per distinct ``budget_line``
    among ``managed_session`` rows matching the optional ``lane_id``/
    ``budget_line`` filters. Read-only; issues no writes."""
    filters: dict[str, Any] = {}
    if lane_id:
        filters["lane_id"] = lane_id
    if budget_line:
        filters["budget_line"] = budget_line
    managed_rows = (
        list_managed_sessions(state, filters)
        if filters
        else list(
            iter_managed_sessions_unbounded(
                state,
                reason=(
                    "an unscoped budget_report intentionally aggregates every managed session "
                    "into budget-line totals"
                ),
            )
        )
    )

    top_buckets: dict[str, _Bucket] = {}
    model_buckets: dict[tuple[str, str], _Bucket] = {}
    for row in managed_rows:
        _apply_row(state, row, top_buckets=top_buckets, model_buckets=model_buckets)

    budget_lines_out: list[dict[str, Any]] = []
    for bl, top in sorted(top_buckets.items()):
        entry = top.to_dict()
        entry["budget_line"] = bl
        entry["by_model"] = {
            model: sub.to_dict()
            for (owning_bl, model), sub in model_buckets.items() if owning_bl == bl
        }
        budget_lines_out.append(entry)

    return {"budget_lines": budget_lines_out}


__all__ = ["build_budget_report"]
