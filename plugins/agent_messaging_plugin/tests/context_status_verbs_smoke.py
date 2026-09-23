#!/usr/bin/env python3
"""Unit smoke for ``context_status_verbs.py`` — the context gauge's storage layer.

THIS FILE EXISTS BECAUSE THE VERBS HAD NO PLUGIN-SIDE TEST AT ALL. Before the
2026-08-16 cache-state widening, the entire coverage of `report_context_status`
/ `session_context_status` was two hook-side fakes that scripted the response
ENVELOPE (`{"status": "completed", "result": {"data": {}}}`) and never
exercised the verb, the store, or the schema. An envelope fake that asserts
nothing about contents cannot go red when the interface widens — it just keeps
passing. So widening the interface safely required creating coverage, not
updating it.

Proves, all against the real verbs over a recording state double:
  - a report round-trips through the store and reads back
  - the three cache fields are TRI-STATE: true / false / NOT REPORTED, and a
    reporter that omits them never reads back as "cache is warm"
  - `rotation_band` is DERIVED at read time from the live policy constants,
    so a policy change needs no backfill
  - the unresolved shape carries the SAME keys as the resolved one, so a
    caller cannot KeyError its way through a legitimate `resolved: false`
  - `resolved: false` still returns its honest `resolution_error` rather than
    an estimated number

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/context_status_verbs_smoke.py
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from agent_messaging_plugin import rotation_thresholds  # noqa: E402
from agent_messaging_plugin.context_status_verbs import (  # noqa: E402
    REPORTER_SURFACES,
    report_context_status,
    session_context_status,
)
from agent_messaging_plugin.session_lifecycle_verbs import VerbError  # noqa: E402

_HISTORY_RECORDED_AT = "recorded_at"

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


class _RecordingState:
    """A state double that stores what it is given and hands it back.

    Deliberately NOT an envelope fake. It records the record dict verbatim, so
    a field the verb forgets to pass simply is not there on read — which is
    exactly the failure an envelope fake cannot express.
    """

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.history: list[dict[str, Any]] = []

    def upsert_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        record = dict(payload["record"])
        self.rows[record["agent_instance_id"]] = record
        return {"action_status": "completed", "data": {}}

    def query_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        wanted = payload["filters"]["agent_instance_id"]
        row = self.rows.get(wanted)
        return {"action_status": "completed", "data": {"records": [row] if row else []}}

    # -- GAU-15 (2026-08-19): the gauge write now also APPENDS a history row.
    # These three exist because `upsert_session_context_status` gained a second
    # writer, and a double that implements only the old half would fail this
    # smoke on a missing attribute rather than on anything it means to test.
    # They keep this double's whole point intact -- records are stored VERBATIM,
    # so a field the store forgets to carry into the history row is simply
    # absent on read, which is exactly what an envelope fake cannot express.

    def write_state(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.history.append(dict(payload["record"]))
        return {"action_status": "completed", "data": {"result": {"id": "h"}}}

    def query_ordered(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        wanted = payload["filters"]["agent_instance_id"]
        rows = [r for r in self.history if r.get("agent_instance_id") == wanted]
        # Newest first on the same key the store orders by; the store's own
        # prune/read logic is what is under test, never this ordering.
        rows.sort(key=lambda r: str(r.get("recorded_at") or ""), reverse=True)
        limit = int(payload.get("limit", len(rows)))
        return {
            "action_status": "completed",
            "data": {"records": rows[:limit], "count": min(limit, len(rows))},
        }

    def delete_records(self, _namespace: str, payload: dict[str, Any]) -> dict[str, Any]:
        filters = payload["filters"]
        cutoff = str(filters[_HISTORY_RECORDED_AT]["value"])
        kept = [
            r for r in self.history
            if not (
                r.get("agent_instance_id") == filters["agent_instance_id"]
                and str(r.get("recorded_at") or "") < cutoff
            )
        ]
        deleted = len(self.history) - len(kept)
        self.history = kept
        return {
            "action_status": "completed",
            "data": {"result": {"deleted": deleted, "soft_delete": False}},
        }


def _report(state: _RecordingState, **overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "agent_instance_id": "agi-test",
        "runtime_session_id": "sess-1",
        "provider": "anthropic",
        "runtime": "claude_code",
        "model": "claude-opus-5",
        "effort": "high",
        "current_tokens": 250_000,
        "ceiling": 1_000_000,
        "measured_at": "2026-08-16T20:00:00+00:00",
    }
    kwargs.update(overrides)
    return report_context_status(state, **kwargs)


# ---------------------------------------------------------------------------


def test_report_round_trips() -> None:
    state = _RecordingState()
    _report(state)
    out = session_context_status(state, agent_instance_id="agi-test")
    _check(out["resolved"] is True, "a reported snapshot reads back as resolved")
    _check(out["current_tokens"] == 250_000, "current_tokens survives the round trip")
    _check(out["runtime_session_id"] == "sess-1", "runtime-neutral session identity round-trips")
    _check(out["runtime"] == "claude_code", "the reporter's runtime is explicit")


def test_cache_fields_are_tri_state_not_boolean() -> None:
    """THE WOULD-HAVE-BEEN-RED TEST. Under the pre-widening interface these
    keys do not exist; under a widening that coerces None to False they read
    back as a WARM cache that nobody measured. Both are caught here."""
    state = _RecordingState()
    _report(state)  # cache fields OMITTED, as an un-upgraded reporter would
    out = session_context_status(state, agent_instance_id="agi-test")
    _check("cache_cold" in out, "the read-back exposes cache_cold at all")
    _check(
        out["cache_cold"] is None,
        "an omitted cache_cold reads back as None (NOT REPORTED), never False",
    )
    _check(
        out["cache_overage_signature"] is None,
        "an omitted overage signature reads back as None, never False",
    )
    _check(
        out["cache_read_tokens"] is None,
        "an omitted cache_read_tokens reads back as None, never 0",
    )


def test_reported_cache_state_survives_including_the_falsy_values() -> None:
    """0 and False are REAL measurements here and must not be swallowed by a
    truthiness test anywhere in the chain -- cache_read_tokens=0 is the
    strongest cold signal there is."""
    state = _RecordingState()
    _report(
        state,
        cache_read_tokens=0,
        cache_write_tokens=0,
        cache_cold=True,
        cache_overage_signature=False,
    )
    out = session_context_status(state, agent_instance_id="agi-test")
    _check(out["cache_read_tokens"] == 0, "a reported ZERO cache read survives as 0, not None")
    _check(out["cache_write_tokens"] == 0, "a reported ZERO cache write survives as 0")
    _check(out["cache_cold"] is True, "a reported cold cache reads back True")
    _check(
        out["cache_overage_signature"] is False,
        "a reported FALSE overage signature stays False, distinct from None",
    )


def test_band_is_derived_at_read_time_not_stored() -> None:
    """A policy change must never require backfilling stored rows, which is
    the same rule `fraction`/`rotation_due` already follow."""
    state = _RecordingState()
    _report(state, current_tokens=250_000, cache_cold=False)
    warm = session_context_status(state, agent_instance_id="agi-test")
    _check(warm["rotation_band"] == "warm_safe_checkpoint",
           "250K warm derives the safe-checkpoint band")
    _check("rotation_band" not in state.rows["agi-test"],
           "the band is NOT stored on the row -- derived, not persisted")

    _report(state, current_tokens=250_000, cache_cold=True)
    cold = session_context_status(state, agent_instance_id="agi-test")
    _check(cold["rotation_band"] == "cold_above_h",
           "the SAME 250K cold derives a different band -- cache state changes the verdict")


def test_rotation_due_and_the_band_can_no_longer_contradict_each_other() -> None:
    """GAU-08 at the PRODUCER. Every reader of this verb inherited its answer.

    The verb published `rotation_due` from the fraction and `rotation_band`
    from the policy bands, independently. On a 1M ceiling the bands saturate
    at `warm_immediate` at 300,000 and the fraction does not reach 0.5 until
    500,000, so for 200,000 tokens this verb returned, on the SAME row, the
    most urgent band it has beside `rotation_due=False` -- and any operator or
    lane reading the field rather than the band inherited the quiet answer.
    Measured across the live fleet on 2026-08-18: the field read False on
    every session at every value any of them occupied that day.

    Both fields are asserted on the SAME response object. Reading them from
    two separate calls could pass while the verb still derived them from
    different inputs, which is the defect itself.
    """
    state = _RecordingState()
    _report(state, current_tokens=336_000, cache_cold=False)
    row = session_context_status(state, agent_instance_id="agi-test")
    _check(row["rotation_band"] == "warm_immediate",
           "300K warm is the most urgent band the policy has")
    _check(row["rotation_due"] is True,
           "...and rotation_due on the SAME row now agrees with it")
    _check(row["fraction"] < rotation_thresholds.ROTATION_THRESHOLD_FRACTION,
           "...while the fraction (0.300) is still BELOW the 0.5 hint -- so the "
           "True cannot be coming from the fraction term, which is what makes this "
           "a test of the union rather than of a moved threshold")


def test_the_verdict_is_decided_on_the_ceiling_the_row_publishes() -> None:
    """The decision and the printed denominator are the SAME number.

    `rotation_due` is computed against the row's STORED `ceiling` -- the one
    this response also publishes and that `fraction` is derived from -- not
    against `resolve_ceiling(model)`. If those two ever disagree (a ceiling
    table edited after a row was written, or a model string this build does
    not know), deciding on one while printing the other would attach a true
    number to the wrong noun: a reader could check the arithmetic, find it
    inconsistent, and be right.

    Forced apart deliberately here with a model name no ceiling table has, so
    `resolve_ceiling` would answer 100,000 while the row says 1,000,000.
    """
    state = _RecordingState()
    _report(state, current_tokens=60_000, ceiling=1_000_000,
            model="a-model-no-table-has", cache_cold=False)
    row = session_context_status(state, agent_instance_id="agi-test")
    _check(rotation_thresholds.resolve_ceiling("a-model-no-table-has")
           == rotation_thresholds.DEFAULT_CONSERVATIVE_CEILING,
           "the model is genuinely unknown, so the two ceilings really do differ")
    _check(row["ceiling"] == 1_000_000, "the row publishes its STORED ceiling")
    _check(row["fraction"] == 0.06, "and its fraction is derived from that ceiling")
    _check(row["rotation_due"] is False,
           "rotation_due follows the STORED ceiling too: 0.06 is under the hint and "
           "60,000 is warm_keep. Deciding on resolve_ceiling(model) instead would "
           "have called this row DUE at 0.6 of a ceiling the response never showed")


def _codex_report(state: _RecordingState) -> None:
    _report(
        state,
        runtime_session_id="019d-codex-thread",
        provider="openai",
        runtime="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        current_tokens=200_000,
        ceiling=258_400,
        cache_read_tokens=100_000,
        cache_write_tokens=0,
        cache_cold=False,
    )


def _trusted_codex_request() -> dict[str, Any]:
    return {
        "expected_calls_after": 10,
        "required_actions": ["compact", "clear"],
        "capability_profile_id": "openai-gpt-5.6-capabilities-v1",
        "capability_profile_version": "openai-gpt-5.6-capabilities-v1",
        "usage_economics_profile_id": "openai-metered-gpt-5.6-sol-2026-08-22",
        "usage_economics_profile_version": "openai-gpt-5.6-sol-prices-v1",
        "objective": "metered_api:minimize_expected_monetary_cost",
        "calibrations": [],
    }


def test_fabricated_calibration_and_profile_claims_are_rejected() -> None:
    state = _RecordingState()
    _codex_report(state)
    fabricated = _trusted_codex_request()
    fabricated["cache_read_multiplier"] = 0.1
    fabricated["calibrations"] = [
        {
            "action": "compact",
            "post_action_prefix_tokens": 25_844,
            "one_time_cost_units": 1,
            "calibration_profile_id": "fabricated",
            "calibration_profile_version": "fabricated",
        },
    ]
    try:
        session_context_status(
            state,
            agent_instance_id="agi-test",
            calculation_request=fabricated,
        )
    except VerbError as exc:
        _check(
            exc.code == "untrusted_calculation_request",
            "caller-supplied calibration values are rejected before calculation",
        )
    else:
        _check(False, "fabricated calibration values must never produce a verdict")

    wrong_version = _trusted_codex_request()
    wrong_version["usage_economics_profile_version"] = "caller-invented-v1"
    try:
        session_context_status(
            state,
            agent_instance_id="agi-test",
            calculation_request=wrong_version,
        )
    except VerbError as exc:
        _check(
            exc.code == "calculation_profile_mismatch",
            "caller-claimed profile versions must match the resolved catalog row",
        )
    else:
        _check(False, "a fabricated profile version must be rejected")


def test_current_codex_catalog_is_unpriced_and_strategy_explanation_is_exposed() -> None:
    state = _RecordingState()
    _codex_report(state)
    out = session_context_status(
        state,
        agent_instance_id="agi-test",
        calculation_request=_trusted_codex_request(),
    )
    calculated = out["calculated_verdict"]
    economics = calculated["economics_decision"]
    _check(calculated["resolved"] is False, "current unpriced Codex actions stay unresolved")
    _check(
        "not priced" in calculated["economic_cause"],
        "the unresolved cause names missing priced action evidence",
    )
    _check(calculated["runtime"] == "codex", "the trusted verdict carries runtime identity")
    _check(calculated["ceiling"] == 258_400, "the verdict uses active runtime capacity")
    _check(
        economics["profile_version"] == "openai-gpt-5.6-sol-prices-v1",
        "the selected economics profile version comes from the catalog",
    )
    _check(
        economics["explanation_version"] == "usage-economics-explanation-v1",
        "the strategy explanation contract is explicitly versioned",
    )
    _check(
        bool(economics["projected_constraints"]),
        "the external result exposes the projected constraint inputs",
    )
    _check(
        "binding_pool_id" in economics and "crossover_profile_id" in economics,
        "binding and crossover pool identities are always explicit",
    )


def test_public_projection_binds_only_supplied_dimensions() -> None:
    """Unknown projection inputs must not become favorable constants."""
    cases = (
        ("absent", {}, "None", "None"),
        ("quarter floor", {"quality_floor": 0.25}, "0.25", "None"),
        ("high floor", {"quality_floor": 0.9}, "0.9", "None"),
        (
            "supplied latency",
            {"quality_floor": 0.25, "keep_latency_seconds": 17.5},
            "0.25",
            "17.5",
        ),
    )
    for label, overrides, expected_floor, expected_latency in cases:
        state = _RecordingState()
        _codex_report(state)
        request = _trusted_codex_request()
        request.update(overrides)
        result = session_context_status(
            state,
            agent_instance_id="agi-test",
            calculation_request=request,
        )
        economics = result["calculated_verdict"]["economics_decision"]
        constraints = economics["projected_constraints"]
        _check(
            f"quality_floor={expected_floor}" in constraints,
            f"{label}: the supplied-or-absent quality floor is projected exactly; "
            f"got {constraints!r}",
        )
        keep = next(constraint for constraint in constraints if constraint.startswith("keep:"))
        _check(
            f"latency_seconds={expected_latency}" in keep,
            f"{label}: keep latency is the supplied value or stays absent; got {keep}",
        )
        _check(
            "quality_score=None" in keep,
            f"{label}: absent keep quality stays unresolved; got {keep}",
        )
        _check(
            economics["status"] == "projection_unresolved",
            f"{label}: incomplete constraints keep the public strategy unresolved",
        )
        if expected_floor == "None":
            _check(
                "quality_floor is missing" in economics["explanation"],
                "an absent floor is named as a projection gap; got "
                f"{economics['explanation']}",
            )


def test_invalid_reading_clocks_fail_before_first_or_new_runtime_writes() -> None:
    """The store validates a clock before every acceptance short-circuit."""
    for bad_clock in ("garbage-clock", "2026-08-22T20:00:04"):
        first = _RecordingState()
        try:
            _report(first, reading_at=bad_clock)
        except VerbError as exc:
            _check(
                exc.code == "stale_context_reading",
                f"first {bad_clock!r} reading fails with the clock error",
            )
        else:
            _check(False, f"first {bad_clock!r} reading must fail before storage")
        _check(not first.rows, f"first {bad_clock!r} reading writes no snapshot")

        new_runtime = _RecordingState()
        _report(
            new_runtime,
            runtime_session_id="prior-runtime",
            reading_at="2026-08-22T20:00:03+00:00",
        )
        try:
            _report(
                new_runtime,
                runtime_session_id="new-runtime",
                reading_at=bad_clock,
            )
        except VerbError as exc:
            _check(
                exc.code == "stale_context_reading",
                f"new-runtime {bad_clock!r} reading fails with the clock error",
            )
        else:
            _check(False, f"new-runtime {bad_clock!r} reading must fail before storage")
        _check(
            new_runtime.rows["agi-test"]["claude_session_id"] == "prior-runtime",
            f"new-runtime {bad_clock!r} reading cannot replace the earlier snapshot",
        )


def test_reading_identity_is_monotone_within_one_runtime_session() -> None:
    state = _RecordingState()
    first = _report(
        state,
        reading_at="2026-08-22T20:00:04+00:00",
        current_tokens=25_844,
    )
    repeated = _report(
        state,
        reading_at="2026-08-22T20:00:04+00:00",
        current_tokens=99_999,
    )
    _check(first["status"] == "recorded", "the first reading identity is recorded")
    _check(repeated["status"] == "unchanged", "an equal reading identity is an idempotent no-op")
    _check(len(state.history) == 1, "the replay writes no duplicate history row")
    _check(state.rows["agi-test"]["current_tokens"] == 25_844, "the replay cannot overwrite the snapshot")
    try:
        _report(
            state,
            reading_at="2026-08-22T20:00:03+00:00",
            current_tokens=10,
        )
    except VerbError as exc:
        _check(exc.code == "stale_context_reading", "an older reading identity fails loud")
    else:
        _check(False, "an older reading identity must not regress the snapshot")


def test_calculated_verdict_preserves_unknown_cache() -> None:
    state = _RecordingState()
    _report(
        state,
        runtime_session_id="019d-codex-thread",
        provider="openai",
        runtime="codex",
        model="gpt-5.6-sol",
        effort="xhigh",
        current_tokens=200_000,
        ceiling=258_400,
    )
    request = _trusted_codex_request()
    out = session_context_status(
        state,
        agent_instance_id="agi-test",
        calculation_request=request,
    )
    calculated = out["calculated_verdict"]
    _check(calculated["resolved"] is False, "unknown cache prevents a priced verdict")
    _check(calculated["cache_state"] == "unknown", "missing cache evidence stays explicit unknown")


def test_unresolved_shape_carries_the_same_keys() -> None:
    """`resolved: false` is a legitimate, expected answer, so a caller must be
    able to read the same keys without a KeyError. A shape that differs
    between the two branches makes the honest gap harder to handle than a
    fabricated number, which is precisely backwards."""
    state = _RecordingState()
    unresolved = session_context_status(state, agent_instance_id="agi-absent")
    _report(state)
    resolved = session_context_status(state, agent_instance_id="agi-test")
    _check(set(unresolved) == set(resolved),
           "resolved and unresolved responses carry identical key sets")
    _check(unresolved["resolved"] is False, "an absent report is not resolved")
    _check(
        "no session_context_status report on file" in (unresolved["resolution_error"] or ""),
        "the unresolved branch explains itself rather than returning a number",
    )


def test_policy_constants_are_the_live_ones() -> None:
    """Pins that the verb reads the policy module rather than carrying its own
    copy of the numbers -- two copies of a threshold is how they drift."""
    state = _RecordingState()
    _report(state, current_tokens=rotation_thresholds.POLICY_H_TOKENS + 1, cache_cold=True)
    out = session_context_status(state, agent_instance_id="agi-test")
    _check(out["rotation_band"] == "cold_above_h",
           "one token above the LIVE POLICY_H_TOKENS is above H")


def test_reporter_attribution_round_trips() -> None:
    state = _RecordingState()
    _report(state, reporter_surface="plugin_cache", reporter_generation=7)
    out = session_context_status(state, agent_instance_id="agi-test")
    _check(out["reporter_surface"] == "plugin_cache", "a reported surface reads back verbatim")
    _check(out["reporter_generation"] == 7, "a reported generation reads back as an int")


def test_absent_reporter_reads_back_null_never_defaulted() -> None:
    """The whole point of the columns: absence must stay absent.

    A reporter predating attribution sends neither field, and that IS the
    finding — it identifies a stale copy. Coercing to "" or 0 would erase it,
    and 0 is a real generation number, so the collapse would be invisible.
    """
    state = _RecordingState()
    _report(state)
    out = session_context_status(state, agent_instance_id="agi-test")
    _check(out["reporter_surface"] is None,
           "an unreported surface reads back None, not an empty string")
    _check(out["reporter_generation"] is None,
           "an unreported generation reads back None, not 0")


def test_unknown_reporter_surface_is_rejected() -> None:
    """An invented surface is worse than an admitted unknown — fail loud."""
    state = _RecordingState()
    try:
        _report(state, reporter_surface="checkout_copy")
    except VerbError as exc:
        _check(exc.code == "unknown_reporter_surface",
               "a surface outside the known classes raises unknown_reporter_surface")
    else:
        _check(False, "a surface outside the known classes must not be stored")
    _check("agi-test" not in state.rows,
           "the rejected report wrote NOTHING — it fails before the upsert")


def test_attribution_separates_stale_copy_from_undeployed_verbs() -> None:
    """The discriminating case, and the reason these columns exist.

    Two rows, IDENTICAL in every cache field (all absent). Before attribution
    they were indistinguishable, and a reader had no way to tell "the verbs
    are not deployed" from "a stale copy served this tick". The assertion is
    that the reporter fields, and ONLY the reporter fields, separate them —
    so it is checked against a value that a same-shaped-but-wrong
    implementation could not produce by accident.
    """
    undeployed = _RecordingState()
    _report(undeployed, agent_instance_id="agi-a",
            reporter_surface="checkout", reporter_generation=1)
    stale = _RecordingState()
    _report(stale, agent_instance_id="agi-b")

    a = session_context_status(undeployed, agent_instance_id="agi-a")
    b = session_context_status(stale, agent_instance_id="agi-b")

    cache_keys = ("cache_read_tokens", "cache_cold", "cache_overage_signature")
    _check(all(a[k] is None for k in cache_keys) and all(b[k] is None for k in cache_keys),
           "both rows are identical in cache state — all three fields absent")
    _check(a["reporter_generation"] == 1 and b["reporter_generation"] is None,
           "generation tells a current reporter from a pre-attribution one")
    _check(a["reporter_surface"] == "checkout" and b["reporter_surface"] is None,
           "surface tells which copy served the tick when cache state cannot")


def test_widened_reporter_surfaces_are_accepted() -> None:
    """'vendored' and 'release' (added 2026-08-17, phase 1) round-trip."""
    for surface in ("vendored", "release"):
        state = _RecordingState()
        _report(state, reporter_surface=surface, reporter_generation=2)
        out = session_context_status(state, agent_instance_id="agi-test")
        _check(out["reporter_surface"] == surface,
               f"the widened surface {surface!r} is accepted and reads back")


def test_widening_is_inert_for_the_original_surfaces() -> None:
    """The property that makes phase 1 safe to land and deploy ALONE.

    Widening an accepted set must reject nothing that previously passed. If
    this goes red, the two-phase plan is unsafe: deploying phase 1 would break
    reporters that are working today.
    """
    for surface in ("checkout", "plugin_cache", "unknown"):
        state = _RecordingState()
        _report(state, reporter_surface=surface)
        out = session_context_status(state, agent_instance_id="agi-test")
        _check(out["reporter_surface"] == surface,
               f"the pre-existing surface {surface!r} still passes unchanged")


def test_every_surface_the_hook_can_emit_is_accepted() -> None:
    """★ CROSS-PHASE DRIFT GUARD: the reporter and the verb must not diverge.

    The reporting hook classifies its own path and sends the result; the verb
    rejects an unrecognised surface BEFORE any write. So a hook taught a new
    class while the verb still refuses it writes NO ROW AT ALL — an outage,
    not a degradation, and precisely why this landed in two phases.

    Asserts against the REAL classifier's output rather than a hand-written
    list, because a list copied from the constant would only restate it.
    """
    repo_root = Path(__file__).resolve().parents[3]
    hook_path = repo_root / ".claude" / "hooks" / "rotation_due_watch.py"
    if not hook_path.is_file():
        print(f"  SKIP  hook not present at {hook_path} — drift guard NOT run")
        return
    spec = importlib.util.spec_from_file_location("_rdw_under_test", hook_path)
    if spec is None or spec.loader is None:
        _check(False, "the reporting hook could not be loaded for the drift guard")
        return
    hook = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hook)

    emitted = set()
    for candidate in (
        "/repo/.claude/hooks/rotation_due_watch.py",
        "/repo/.claude/hooks/nested/rotation_due_watch.py",
        "/home/u/.claude/plugins/cache/mkt/plug/1.0.0/hooks/rotation_due_watch.py",
        "/repo/plugins/p/claude_plugin/coordination-hooks/hooks/rotation_due_watch.py",
        "/home/u/.ananta/releases/x/rel-1/code/plugins/p/coordination-hooks/hooks/rotation_due_watch.py",
        "rotation_due_watch.py",
    ):
        hook.__file__ = candidate
        emitted.add(hook._reporter_arguments()["reporter_surface"])

    unaccepted = emitted - REPORTER_SURFACES
    _check(not unaccepted,
           f"every surface the hook can emit is accepted by the verb "
           f"(would be rejected: {sorted(unaccepted)})")


def main() -> int:
    print("context_status_verbs smoke\n")
    for test in (
        test_report_round_trips,
        test_cache_fields_are_tri_state_not_boolean,
        test_reported_cache_state_survives_including_the_falsy_values,
        test_band_is_derived_at_read_time_not_stored,
        test_rotation_due_and_the_band_can_no_longer_contradict_each_other,
        test_the_verdict_is_decided_on_the_ceiling_the_row_publishes,
        test_fabricated_calibration_and_profile_claims_are_rejected,
        test_current_codex_catalog_is_unpriced_and_strategy_explanation_is_exposed,
        test_public_projection_binds_only_supplied_dimensions,
        test_invalid_reading_clocks_fail_before_first_or_new_runtime_writes,
        test_reading_identity_is_monotone_within_one_runtime_session,
        test_calculated_verdict_preserves_unknown_cache,
        test_unresolved_shape_carries_the_same_keys,
        test_policy_constants_are_the_live_ones,
        test_reporter_attribution_round_trips,
        test_absent_reporter_reads_back_null_never_defaulted,
        test_unknown_reporter_surface_is_rejected,
        test_attribution_separates_stale_copy_from_undeployed_verbs,
        test_widened_reporter_surfaces_are_accepted,
        test_widening_is_inert_for_the_original_surfaces,
        test_every_surface_the_hook_can_emit_is_accepted,
    ):
        test()
    print()
    print(f"{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
