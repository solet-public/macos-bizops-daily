#!/usr/bin/env python3
"""Unit smoke for ``budget_report.py`` (T1 S3, seat's design ruling
2026-08-05) -- the per-``budget_line`` token-usage rollup joining
``managed_session``/``session_claude_mapping`` (agent_messaging_plugin)
against ``session_ledger__session``/``session_ledger__event``
(session_ledger_service) -- the first cross-plugin state read of its kind
in this codebase.

Proves: a covered row sums its usage_json fields and sets as_of correctly;
the three DISTINCT uncovered shapes (no mapping row at all, a mapping row
that resolves to no session_ledger session, and a resolved session with no
usage-bearing events) all count as sessions_uncovered, never
sessions_covered; a worker's usage sums across EVERY distinct
claude_session_id it has ever been observed under (the ONE-TO-MANY rotation
history); lane_id/budget_line filters narrow the managed_session scan;
by_model buckets (including the empty-string bucket for an unset model) are
correct and independent from the top-level totals; a non-numeric or boolean
field in usage_json is never summed; and the real
``AgentMessagingPlugin.budget_report`` verb handler genuinely reaches
``build_budget_report`` with correctly-extracted params.

Run:
    .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/budget_report_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.session_ledger.schema import NAMESPACE as LEDGER_NAMESPACE  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_EVENT as LEDGER_TABLE_EVENT  # noqa: E402
from ananta.llm.session_ledger.schema import TABLE_SESSION as LEDGER_TABLE_SESSION  # noqa: E402
from ananta.llm.session_ledger.types import SourceVendor  # noqa: E402

from agent_messaging_plugin.budget_report import build_budget_report  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    SESSION_HOST_HEADLESS,
    SESSION_HOST_OPERATOR,
    WORK_CLASS_READ_ONLY,
)
from agent_messaging_plugin.session_claude_mapping_store import (  # noqa: E402
    upsert_session_claude_mapping,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    transition_lifecycle_state,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _spawn(
    state: StateManagementInterface,
    *,
    agent_instance_id: str,
    lane_id: str = "lane-x",
    budget_line: str = "budget-x",
    model: str = "",
    host: str = SESSION_HOST_HEADLESS,
) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=agent_instance_id, lane_id=lane_id, brief_ref="",
            work_class=WORK_CLASS_READ_ONLY, budget_line=budget_line, host=host, model=model,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )


def _seed_ledger_session(
    state: StateManagementInterface,
    *,
    claude_session_id: str,
    source_id: str = "src-1",
    event_count: int = 1,
    last_event_at: str = "2026-08-05T18:05:00",
) -> str:
    """Inserts a ``session_ledger__session`` row and returns its generated id.
    ``source_id`` is the conflict key alongside ``external_session_id`` --
    calling this twice with the SAME ``claude_session_id`` but DIFFERENT
    ``source_id`` values creates two sibling rows sharing one
    ``external_session_id``, the exact measured shape of the 2026-08-06
    sibling-row defect (see budget_report.py's module docstring)."""
    state.upsert_state(
        LEDGER_NAMESPACE,
        {
            "table": LEDGER_TABLE_SESSION,
            "record": {
                "source_id": source_id,
                "external_session_id": claude_session_id,
                "vendor": SourceVendor.CLAUDE_CODE.value,
                "first_event_at": "2026-08-05T18:00:00",
                "last_event_at": last_event_at,
                "event_count": event_count,
            },
            "conflict_columns": ["source_id", "external_session_id"],
        },
    )
    result = state.query_state(
        LEDGER_NAMESPACE,
        {
            "table": LEDGER_TABLE_SESSION,
            "filters": {"external_session_id": claude_session_id, "source_id": source_id},
        },
    )
    records = cast("dict[str, Any]", result)["data"]["records"]
    assert len(records) == 1, (
        f"expected exactly one row for (external_session_id={claude_session_id!r}, "
        f"source_id={source_id!r}), got {records!r}"
    )
    return str(records[0]["id"])


def _seed_ledger_event(
    state: StateManagementInterface,
    *,
    ledger_session_id: str,
    event_at: str,
    usage_json: dict[str, Any] | None,
) -> None:
    state.write_state(
        LEDGER_NAMESPACE,
        {
            "table": LEDGER_TABLE_EVENT,
            "record": {
                "session_id": ledger_session_id,
                "event_type": "message",
                "event_at": event_at,
                "imported_at": event_at,
                "batch_id": "batch-1",
                "usage_json": usage_json,
            },
        },
    )


def _one_budget_line(report: dict[str, Any], budget_line: str) -> dict[str, Any]:
    matches = [entry for entry in report["budget_lines"] if entry["budget_line"] == budget_line]
    assert len(matches) == 1, f"expected exactly one {budget_line!r} entry, got {matches!r}"
    return matches[0]


def test_no_managed_sessions_returns_empty_list() -> None:
    state = _state()
    report = build_budget_report(state, budget_line="nothing-here")
    _check(
        report == {"budget_lines": []},
        "no matching managed_session rows -> an empty list, not an error",
    )


def test_covered_row_sums_usage_and_sets_as_of() -> None:
    state = _state()
    _spawn(state, agent_instance_id="agi-covered-1", budget_line="bl-covered", model="sonnet")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-covered-1", claude_session_id="cs-covered-1",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    ledger_id = _seed_ledger_session(state, claude_session_id="cs-covered-1")
    _seed_ledger_event(
        state, ledger_session_id=ledger_id, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 100, "output_tokens": 50},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-covered"), "bl-covered")
    _check(entry["sessions_covered"] == 1, "the covered row counts toward sessions_covered")
    _check(entry["sessions_uncovered"] == 0, "and NOT toward sessions_uncovered")
    _check(
        entry["usage"] == {"input_tokens": 100, "output_tokens": 50},
        f"usage sums the event's own fields exactly (got {entry['usage']!r})",
    )
    _check(entry["as_of"] == "2026-08-05T18:03:00+00:00", "as_of is the event's own event_at")


def test_uncovered_no_mapping_row_at_all() -> None:
    """Rail 3, shape 1: a managed_session row with NO session_claude_mapping
    row ever observed for it (e.g. the hook never fired -- S2c's own
    population)."""
    state = _state()
    _spawn(state, agent_instance_id="agi-uncovered-1", budget_line="bl-uncovered-1")
    report = build_budget_report(state, budget_line="bl-uncovered-1")
    entry = _one_budget_line(report, "bl-uncovered-1")
    _check(entry["sessions_covered"] == 0, "no mapping row -> not covered")
    _check(entry["sessions_uncovered"] == 1, "no mapping row -> uncovered")
    _check(entry["usage"] == {}, "no usage figures")
    _check(entry["as_of"] is None, "as_of is null when sessions_covered is 0")


def test_uncovered_mapping_resolves_to_no_ledger_session() -> None:
    """Rail 3, shape 2: a mapping row exists (the hook DID fire) but
    session_ledger never ingested that claude_session_id (e.g. the
    filesystem source hasn't polled it yet)."""
    state = _state()
    _spawn(state, agent_instance_id="agi-uncovered-2", budget_line="bl-uncovered-2")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-uncovered-2", claude_session_id="cs-never-ingested",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    report = build_budget_report(state, budget_line="bl-uncovered-2")
    entry = _one_budget_line(report, "bl-uncovered-2")
    _check(entry["sessions_covered"] == 0, "an unresolvable claude_session_id -> not covered")
    _check(entry["sessions_uncovered"] == 1, "-> uncovered")


def test_no_llm_turns_ledger_session_with_no_usage_events() -> None:
    """D4 close-out (2026-08-06): session_ledger DID ingest the session (we
    HOLD its full ledger group), but none of its events carry usage_json --
    a TRUE MEASURED ZERO, its own declared class (sessions_no_llm_turns),
    distinct from sessions_uncovered (an unresolved gap). This is the same
    fixture shape as the former "Rail 3, shape 3" test; only the expected
    classification changed, per the D4 close-out ruling."""
    state = _state()
    _spawn(state, agent_instance_id="agi-no-turns-1", budget_line="bl-no-turns-1")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-no-turns-1", claude_session_id="cs-no-usage",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    ledger_id = _seed_ledger_session(state, claude_session_id="cs-no-usage")
    _seed_ledger_event(
        state, ledger_session_id=ledger_id, event_at="2026-08-05T18:03:00+00:00", usage_json=None,
    )
    report = build_budget_report(state, budget_line="bl-no-turns-1")
    entry = _one_budget_line(report, "bl-no-turns-1")
    _check(entry["sessions_covered"] == 0, "events exist but none carry usage_json -> not covered")
    _check(
        entry["sessions_no_llm_turns"] == 1,
        "a resolved ledger group with zero usage-bearing events -> no_llm_turns",
    )
    _check(entry["sessions_uncovered"] == 0, "a true measured zero is NOT an unresolved gap")


# -- D4 exclusion-scoping (usage-capture-attribution D4 close-out, 2026-08-06),
# five legs, each naming its failing mutation -- the ratified fix for the
# lane's D3 measurement (workbench
# 2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md):
# host=operator and pre-landing rows must never inflate sessions_uncovered,
# each declared in its own sessions_excluded_* count instead.


def test_operator_hosted_excluded_never_touches_mapping_table() -> None:
    """FAILING MUTATION KILLED: counting a host=operator row as
    sessions_uncovered (or worse, querying the mapping table for it at all,
    which the state fake would reject via a KeyError/empty-lookup path
    since no mapping row could ever exist for it). No mapping row is
    seeded here on purpose -- if _apply_row's exclusion check were removed
    or misordered, this would fall through to sessions_uncovered instead of
    sessions_excluded_operator_hosted."""
    state = _state()
    _spawn(
        state, agent_instance_id="agi-operator-1", budget_line="bl-operator-1",
        host=SESSION_HOST_OPERATOR,
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-operator-1"), "bl-operator-1")
    _check(
        entry["sessions_excluded_operator_hosted"] == 1,
        "host=operator -> its own declared exclusion, never uncovered",
    )
    _check(entry["sessions_uncovered"] == 0, "excluded, not counted as an open gap")
    _check(entry["sessions_covered"] == 0, "excluded, not counted as covered either")
    _check(entry["sessions_no_llm_turns"] == 0, "excluded rows never reach the no_llm_turns check")


def test_pre_landing_excluded_regardless_of_mapping_state() -> None:
    """FAILING MUTATION KILLED: counting a pre-2026-08-05T16:36:06Z row as
    sessions_uncovered. Seeds a REAL usage-bearing mapping+ledger row to
    prove the exclusion check runs FIRST and wins regardless of what the
    mapping table holds -- a pre-landing row is excluded on timing alone,
    never on whether capture happened to work for it anyway."""
    state = _state()
    state.now_iso = lambda: "2026-08-01T00:00:00+00:00"
    _spawn(state, agent_instance_id="agi-pre-landing-1", budget_line="bl-pre-landing-1")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-pre-landing-1", claude_session_id="cs-pre-landing-1",
        captured_at="2026-08-01T00:01:00+00:00", capture_source="hook:startup",
    )
    ledger_id = _seed_ledger_session(state, claude_session_id="cs-pre-landing-1")
    _seed_ledger_event(
        state, ledger_session_id=ledger_id, event_at="2026-08-01T00:02:00+00:00",
        usage_json={"input_tokens": 999},
    )
    entry = _one_budget_line(
        build_budget_report(state, budget_line="bl-pre-landing-1"), "bl-pre-landing-1",
    )
    _check(
        entry["sessions_excluded_pre_landing"] == 1,
        "created before the hook-landing instant -> its own declared exclusion",
    )
    _check(entry["sessions_uncovered"] == 0, "excluded, not counted as an open gap")
    _check(
        entry["sessions_covered"] == 0,
        "excluded even though real usage exists -- timing wins, not capture luck",
    )
    _check(entry["usage"] == {}, "an excluded row's usage never leaks into the bucket's totals")


def test_post_landing_row_not_excluded() -> None:
    """Regression guard: a row created strictly AFTER the hook-landing
    instant must NOT be excluded -- proves the boundary is exclusive on the
    correct side, not an off-by-one that swallows legitimate post-landing
    sessions."""
    state = _state()
    state.now_iso = lambda: "2026-08-05T16:36:07+00:00"
    _spawn(state, agent_instance_id="agi-post-landing-1", budget_line="bl-post-landing-1")
    entry = _one_budget_line(
        build_budget_report(state, budget_line="bl-post-landing-1"), "bl-post-landing-1",
    )
    _check(entry["sessions_excluded_pre_landing"] == 0, "one second after landing -> NOT excluded")
    _check(entry["sessions_uncovered"] == 1, "in scope, no mapping row -> a genuine open gap")


def test_unresolved_gap_distinct_from_no_llm_turns() -> None:
    """Regression guard, the other half of the no_llm_turns split: a mapping
    row whose claude_session_id never resolves to ANY session_ledger__session
    row (ledger hasn't ingested it yet) must stay sessions_uncovered, never
    sessions_no_llm_turns -- that class is reserved for a CONFIRMED zero
    (rows exist, checked, empty), not an unresolved one (no row to check)."""
    state = _state()
    _spawn(state, agent_instance_id="agi-gap-1", budget_line="bl-gap-1")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-gap-1", claude_session_id="cs-never-ingested-2",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-gap-1"), "bl-gap-1")
    _check(entry["sessions_uncovered"] == 1, "no ledger row resolves at all -> an unresolved gap")
    _check(entry["sessions_no_llm_turns"] == 0, "never confused with a confirmed true-zero")


# -- 2026-08-06 sibling-row defect (usage-capture-attribution D2), six legs,
# each naming its failing mutation -- reproduces the row-level-proven flip
# (workbench/2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md,
# D2 addendum 2/3): a single external_session_id backed by MULTIPLE
# session_ledger__session rows (a thin overlay source alongside a rich one,
# or two rows of the same rich source_kind from a duplicate registration).


def test_sibling_canonical_zero_usage_sibling_has_usage_resolves_to_usage() -> None:
    """FAILING MUTATION KILLED: the pre-fix ``rows[0]``-order pick, when
    ``rows[0]`` happens to be the thin (zero-usage) row -- reports uncovered
    even though a sibling row carries real usage. Exact measured shape:
    canonical (thin) row inserted FIRST, usage-bearing sibling inserted
    second."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-1", budget_line="bl-sib-1")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-1", claude_session_id="cs-sib-1",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    thin_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-1", source_id="src-history", event_count=17,
    )
    _seed_ledger_event(
        state, ledger_session_id=thin_id, event_at="2026-08-05T18:01:00+00:00", usage_json=None,
    )
    rich_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-1", source_id="src-local", event_count=395,
    )
    _seed_ledger_event(
        state, ledger_session_id=rich_id, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 500},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-1"), "bl-sib-1")
    _check(entry["sessions_covered"] == 1, "the usage-bearing sibling is found -> covered")
    _check(
        entry["usage"] == {"input_tokens": 500},
        f"usage comes from the rich sibling, not zero (got {entry['usage']!r})",
    )
    _check(
        entry["sessions_multi_row_resolved"] == 0,
        "only ONE row is usage-bearing here (the thin row has zero usage-bearing "
        "events, so it is never a candidate) -- nothing to disambiguate, unlike "
        "the two-usage-bearing-siblings leg below",
    )


def test_sibling_resolution_is_order_independent() -> None:
    """FAILING MUTATION KILLED: any resolver relying on insertion/return
    ORDER (the pre-fix ``rows[0]`` pick) -- this is the direct regression
    test for the root-caused sessions_covered flip. Identical fixture to the
    leg above, but the usage-bearing sibling is inserted FIRST and the thin
    zero-usage row SECOND -- the result must be byte-identical."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-2", budget_line="bl-sib-2")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-2", claude_session_id="cs-sib-2",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    rich_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-2", source_id="src-local", event_count=395,
    )
    _seed_ledger_event(
        state, ledger_session_id=rich_id, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 500},
    )
    thin_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-2", source_id="src-history", event_count=17,
    )
    _seed_ledger_event(
        state, ledger_session_id=thin_id, event_at="2026-08-05T18:01:00+00:00", usage_json=None,
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-2"), "bl-sib-2")
    _check(entry["sessions_covered"] == 1, "still covered regardless of insertion order")
    _check(
        entry["usage"] == {"input_tokens": 500},
        f"same usage regardless of which row was inserted first (got {entry['usage']!r})",
    )


def test_two_usage_bearing_siblings_never_summed() -> None:
    """FAILING MUTATION KILLED: a naive 'sum every usage-bearing sibling'
    resolver -- the measured duplicate-registration shape (two
    claude_code_local-kind rows from two distinct __source registrations,
    both carrying usage). The higher-event_count row's usage is used
    EXCLUSIVELY; the result must NOT equal the sum of both rows."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-3", budget_line="bl-sib-3")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-3", claude_session_id="cs-sib-3",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    big_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-3", source_id="src-local-a", event_count=3567,
    )
    _seed_ledger_event(
        state, ledger_session_id=big_id, event_at="2026-08-05T18:05:00+00:00",
        usage_json={"input_tokens": 3000},
    )
    small_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-3", source_id="src-local-b", event_count=20,
    )
    _seed_ledger_event(
        state, ledger_session_id=small_id, event_at="2026-08-05T18:02:00+00:00",
        usage_json={"input_tokens": 200},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-3"), "bl-sib-3")
    _check(
        entry["usage"] == {"input_tokens": 3000},
        f"only the higher-event_count row's usage is used (got {entry['usage']!r})",
    )
    _check(
        entry["usage"] != {"input_tokens": 3200},
        "NOT the sum of both siblings -- that would double-count",
    )
    _check(entry["sessions_multi_row_resolved"] == 1, "two usage-bearing siblings -> flagged")


def test_sibling_tie_break_prefers_more_recent_last_event_at() -> None:
    """Equal event_count on both usage-bearing siblings -- the tiebreak
    (last_event_at descending) must pick the more recently active row
    deterministically, not whichever the state layer happens to return
    first."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-4", budget_line="bl-sib-4")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-4", claude_session_id="cs-sib-4",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    older_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-4", source_id="src-older",
        event_count=50, last_event_at="2026-08-05T18:10:00",
    )
    _seed_ledger_event(
        state, ledger_session_id=older_id, event_at="2026-08-05T18:10:00+00:00",
        usage_json={"input_tokens": 10},
    )
    newer_id = _seed_ledger_session(
        state, claude_session_id="cs-sib-4", source_id="src-newer",
        event_count=50, last_event_at="2026-08-05T18:20:00",
    )
    _seed_ledger_event(
        state, ledger_session_id=newer_id, event_at="2026-08-05T18:20:00+00:00",
        usage_json={"input_tokens": 20},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-4"), "bl-sib-4")
    _check(
        entry["usage"] == {"input_tokens": 20},
        f"tie on event_count -> the more-recent last_event_at wins (got {entry['usage']!r})",
    )


def test_sibling_group_with_no_usage_anywhere_is_no_llm_turns() -> None:
    """Regression guard: a multi-row group where NEITHER sibling carries
    usage_json must never fabricate usage from an empty candidate set. D4
    close-out (2026-08-06): both siblings DID resolve (rows exist), so this
    is a confirmed true-zero -- sessions_no_llm_turns, not sessions_uncovered
    (that class is reserved for an unresolved gap, e.g. no ledger row at
    all -- see test_uncovered_mapping_resolves_to_no_ledger_session)."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-5", budget_line="bl-sib-5")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-5", claude_session_id="cs-sib-5",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    id_a = _seed_ledger_session(state, claude_session_id="cs-sib-5", source_id="src-a")
    _seed_ledger_event(
        state, ledger_session_id=id_a, event_at="2026-08-05T18:01:00+00:00", usage_json=None,
    )
    id_b = _seed_ledger_session(state, claude_session_id="cs-sib-5", source_id="src-b")
    _seed_ledger_event(
        state, ledger_session_id=id_b, event_at="2026-08-05T18:02:00+00:00", usage_json=None,
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-5"), "bl-sib-5")
    _check(entry["sessions_covered"] == 0, "no usage anywhere in the group -> not covered")
    _check(entry["sessions_no_llm_turns"] == 1, "both siblings resolved, zero usage -> no_llm_turns")
    _check(entry["sessions_uncovered"] == 0, "a confirmed true-zero is NOT an unresolved gap")
    _check(
        entry["sessions_multi_row_resolved"] == 0,
        "nothing to disambiguate when no candidate carries usage",
    )


def test_single_row_group_unaffected_by_sibling_resolution() -> None:
    """Regression guard: the overwhelming common case (no siblings at all)
    must behave exactly as before the sibling-row fix."""
    state = _state()
    _spawn(state, agent_instance_id="agi-sib-6", budget_line="bl-sib-6")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-sib-6", claude_session_id="cs-sib-6",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    ledger_id = _seed_ledger_session(state, claude_session_id="cs-sib-6", event_count=5)
    _seed_ledger_event(
        state, ledger_session_id=ledger_id, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 42},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-sib-6"), "bl-sib-6")
    _check(entry["sessions_covered"] == 1, "single-row group -> covered, unchanged")
    _check(entry["usage"] == {"input_tokens": 42}, "usage unchanged for the common case")
    _check(
        entry["sessions_multi_row_resolved"] == 0,
        "no siblings -> never flagged as multi-row-resolved",
    )


def test_usage_sums_across_every_claude_session_in_the_workers_lifetime() -> None:
    """The ONE-TO-MANY join: a worker that rotated through TWO distinct
    claude_session_id values (e.g. across a /clear) has its usage summed
    across BOTH, and as_of reflects the LATER of the two events."""
    state = _state()
    _spawn(state, agent_instance_id="agi-multi-cs", budget_line="bl-multi-cs")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-multi-cs", claude_session_id="cs-multi-a",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-multi-cs", claude_session_id="cs-multi-b",
        captured_at="2026-08-05T19:00:00+00:00", capture_source="hook:clear",
    )
    ledger_a = _seed_ledger_session(state, claude_session_id="cs-multi-a")
    ledger_b = _seed_ledger_session(state, claude_session_id="cs-multi-b")
    _seed_ledger_event(
        state, ledger_session_id=ledger_a, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 100},
    )
    _seed_ledger_event(
        state, ledger_session_id=ledger_b, event_at="2026-08-05T19:03:00+00:00",
        usage_json={"input_tokens": 200},
    )
    report = build_budget_report(state, budget_line="bl-multi-cs")
    entry = _one_budget_line(report, "bl-multi-cs")
    _check(entry["sessions_covered"] == 1, "still ONE covered managed_session row, not two")
    _check(
        entry["usage"] == {"input_tokens": 300},
        f"summed across both claude sessions (got {entry['usage']!r})",
    )
    _check(entry["as_of"] == "2026-08-05T19:03:00+00:00", "as_of is the LATER of the two events")


def test_budget_line_filter_narrows_scope() -> None:
    state = _state()
    _spawn(state, agent_instance_id="agi-filter-1", budget_line="bl-keep")
    _spawn(state, agent_instance_id="agi-filter-2", budget_line="bl-drop")
    report = build_budget_report(state, budget_line="bl-keep")
    lines = [entry["budget_line"] for entry in report["budget_lines"]]
    _check(
        lines == ["bl-keep"], f"budget_line filter returns only the matching line (got {lines!r})",
    )


def test_lane_id_filter_narrows_scope() -> None:
    state = _state()
    _spawn(state, agent_instance_id="agi-lane-1", lane_id="lane-keep", budget_line="bl-lane-1")
    _spawn(state, agent_instance_id="agi-lane-2", lane_id="lane-drop", budget_line="bl-lane-2")
    report = build_budget_report(state, lane_id="lane-keep")
    lines = [entry["budget_line"] for entry in report["budget_lines"]]
    _check(
        lines == ["bl-lane-1"],
        f"lane_id filter returns only that lane's budget_line (got {lines!r})",
    )


def test_by_model_breakdown_including_empty_string_bucket() -> None:
    state = _state()
    _spawn(state, agent_instance_id="agi-model-a", budget_line="bl-model", model="sonnet")
    _spawn(state, agent_instance_id="agi-model-b", budget_line="bl-model", model="")
    for agent_instance_id, cs_id in (("agi-model-a", "cs-model-a"), ("agi-model-b", "cs-model-b")):
        upsert_session_claude_mapping(
            state, agent_instance_id=agent_instance_id, claude_session_id=cs_id,
            captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
        )
        ledger_id = _seed_ledger_session(state, claude_session_id=cs_id, source_id=f"src-{cs_id}")
        _seed_ledger_event(
            state, ledger_session_id=ledger_id, event_at="2026-08-05T18:03:00+00:00",
            usage_json={"input_tokens": 10},
        )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-model"), "bl-model")
    _check(entry["usage"] == {"input_tokens": 20}, "top-level totals sum across both models")
    _check(
        set(entry["by_model"]) == {"sonnet", ""},
        f"by_model has both the named + empty-string bucket (got {set(entry['by_model'])!r})",
    )
    _check(
        entry["by_model"]["sonnet"]["usage"] == {"input_tokens": 10},
        "sonnet bucket isolated correctly",
    )
    _check(
        entry["by_model"][""]["usage"] == {"input_tokens": 10},
        "empty-model bucket isolated correctly",
    )


def test_non_numeric_and_boolean_fields_never_summed() -> None:
    """RED-FIRST: a naive ``isinstance(value, (int, float))`` check would
    wrongly sum a boolean (bool is an int subclass in Python) -- this proves
    the guard actually excludes it, not just that a plain string is
    excluded."""
    state = _state()
    _spawn(state, agent_instance_id="agi-nonnum", budget_line="bl-nonnum")
    upsert_session_claude_mapping(
        state, agent_instance_id="agi-nonnum", claude_session_id="cs-nonnum",
        captured_at="2026-08-05T18:00:00+00:00", capture_source="hook:startup",
    )
    ledger_id = _seed_ledger_session(state, claude_session_id="cs-nonnum")
    _seed_ledger_event(
        state, ledger_session_id=ledger_id, event_at="2026-08-05T18:03:00+00:00",
        usage_json={"input_tokens": 10, "cache_hit": True, "model_label": "claude-sonnet"},
    )
    entry = _one_budget_line(build_budget_report(state, budget_line="bl-nonnum"), "bl-nonnum")
    _check(
        entry["usage"] == {"input_tokens": 10},
        f"only the numeric field is summed, bool and str fields excluded (got {entry['usage']!r})",
    )


def test_verb_handler_extracts_params_and_calls_through() -> None:
    """Drives the REAL ``AgentMessagingPlugin.budget_report`` method (the
    @platform_process-decorated handler, not the standalone
    build_budget_report function) to prove params extraction and the
    state_service wiring are genuinely correct, mirroring this plugin's own
    wired-consumer convention."""
    state = _state()
    _spawn(state, agent_instance_id="agi-verb-1", budget_line="bl-verb")
    fake_self = cast(
        "Any",
        type("FakeSelf", (), {"_get_state_service": lambda self: state})(),
    )
    result = AgentMessagingPlugin.budget_report(
        fake_self, {"parameters": {"budget_line": "bl-verb"}}, {},
    )
    _check(result["action_status"] == "completed", f"the verb handler succeeds (got {result!r})")
    lines = [entry["budget_line"] for entry in result["data"]["budget_lines"]]
    _check(
        lines == ["bl-verb"],
        "the handler's budget_line param genuinely reaches build_budget_report's filter",
    )


def test_verb_handler_reports_state_service_unavailable() -> None:
    fake_self = cast("Any", type("FakeSelf", (), {"_get_state_service": lambda self: None})())
    result = AgentMessagingPlugin.budget_report(fake_self, {}, {})
    _check(result["action_status"] == "failed", "no state_service -> a failed result, not a crash")
    _check(
        result["error"]["code"] == "state_service_unavailable",
        f"the failure names the right code (got {result['error']!r})",
    )


def main() -> int:
    test_no_managed_sessions_returns_empty_list()
    test_covered_row_sums_usage_and_sets_as_of()
    test_uncovered_no_mapping_row_at_all()
    test_uncovered_mapping_resolves_to_no_ledger_session()
    test_no_llm_turns_ledger_session_with_no_usage_events()
    test_operator_hosted_excluded_never_touches_mapping_table()
    test_pre_landing_excluded_regardless_of_mapping_state()
    test_post_landing_row_not_excluded()
    test_unresolved_gap_distinct_from_no_llm_turns()
    test_sibling_canonical_zero_usage_sibling_has_usage_resolves_to_usage()
    test_sibling_resolution_is_order_independent()
    test_two_usage_bearing_siblings_never_summed()
    test_sibling_tie_break_prefers_more_recent_last_event_at()
    test_sibling_group_with_no_usage_anywhere_is_no_llm_turns()
    test_single_row_group_unaffected_by_sibling_resolution()
    test_usage_sums_across_every_claude_session_in_the_workers_lifetime()
    test_budget_line_filter_narrows_scope()
    test_lane_id_filter_narrows_scope()
    test_by_model_breakdown_including_empty_string_bucket()
    test_non_numeric_and_boolean_fields_never_summed()
    test_verb_handler_extracts_params_and_calls_through()
    test_verb_handler_reports_state_service_unavailable()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
