#!/usr/bin/env python3
"""Focused fail-closed tests for the declarative model dispatch policy."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from agent_messaging_plugin import model_dispatch_policy as policy  # noqa: E402

_passed = 0
_failed: list[str] = []
REGISTER_MINTED_KINDS = (
    "DESIGN", "FIX", "build", "design", "diagnose", "docs", "fix", "implement",
    "implementation", "infrastructure", "integration", "repair", "review",
    "smoke-test", "test", "test-close",
)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _raises_code(callback: object) -> str:
    try:
        assert callable(callback)
        callback()
    except policy.DispatchPolicyError as exc:
        return exc.code
    return ""


def test_missing_and_malformed_policy_refuse() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "missing.json"
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_unavailable",
                "missing policy refuses at first use",
            )
            path.write_text("{not json", encoding="utf-8")
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_unavailable",
                "malformed policy refuses at first use",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_red_mutation_deleting_dispatch_kinds_section_refuses() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    del source["dispatch_kinds"]
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(
                    lambda: policy.validate_spawn_dispatch(
                        dispatch_kind="fix", agent_runtime="codex", model="gpt-5.6-terra",
                        reviewed_report_vendor="", pair_id="",
                    ),
                ) == "dispatch_policy_invalid",
                "red mutation deleting dispatch_kinds section refuses policy loading",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_policy_without_retired_orchestrator_section() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    source["schema_version"] = 2
    source.pop("orchestrator", None)
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "",
                "schema v2 policy without retired orchestrator section loads",
            )
            source["orchestrator"] = {"role_names": ["Main"], "allowed_models": ["claude-sonnet-5"]}
            path.write_text(json.dumps(source), encoding="utf-8")
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_invalid",
                "obsolete orchestrator allowlist is rejected rather than silently ignored",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_newer_profiled_model_is_not_seat_gated() -> None:
    _check(
        _validate("design", "claude-opus-5-5") == "",
        "profiled newer Claude model is not seat-gated by dispatch policy",
    )


def test_solo_dispatch_is_accepted_under_diagnose_design_and_review() -> None:
    """No dispatch_kind requires cross-vendor pairing any more (2026-09-19)."""
    _check(
        _raises_code(
            lambda: policy.validate_spawn_dispatch(
                dispatch_kind="diagnose", agent_runtime="claude_code", model="claude-opus-5",
                reviewed_report_vendor="", pair_id="",
            ),
        ) == "",
        "solo diagnose dispatch with no pair_id is accepted",
    )
    _check(
        _raises_code(
            lambda: policy.validate_spawn_dispatch(
                dispatch_kind="design", agent_runtime="claude_code", model="claude-opus-5",
                reviewed_report_vendor="", pair_id="",
            ),
        ) == "",
        "solo design dispatch with no pair_id is accepted",
    )
    _check(
        _raises_code(
            lambda: policy.validate_spawn_dispatch(
                dispatch_kind="review", agent_runtime="codex", model="gpt-5.6-terra",
                reviewed_report_vendor="codex", pair_id="",
            ),
        ) == "",
        "solo same-vendor review dispatch is accepted",
    )


def test_pair_allowlist_cannot_return_to_the_policy() -> None:
    """Dispatch kinds label work; they must never restore pair-selection authority."""
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    try:
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / "policy.json"
            malformed = json.loads(json.dumps(source))
            malformed["dispatch_kinds"]["fix"] = {
                "allowed_pairs": [{"agent_runtime": "codex", "model": "gpt-5.6-terra"}],
            }
            path.write_text(json.dumps(malformed), encoding="utf-8")
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_invalid",
                "red mutation restoring an allowed-pairs table refuses policy loading",
            )
            path.write_text(json.dumps(source), encoding="utf-8")
            _check(
                _raises_code(
                    lambda: policy.validate_spawn_dispatch(
                        dispatch_kind="fix", agent_runtime="codex", model="gpt-6-astra",
                        reviewed_report_vendor="", pair_id="",
                    ),
                ) == "",
                "a profiled cross-vendor pair is not blocked by dispatch_kind",
            )
    finally:
        policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def _validate(kind: str, model: str, **extra: object) -> str:
    return _raises_code(
        lambda: policy.validate_spawn_dispatch(
            dispatch_kind=kind, agent_runtime="claude_code", model=model,
            reviewed_report_vendor="", pair_id="", **extra,  # type: ignore[arg-type]
        ),
    )


def test_low_score_fix_ticket_reaches_a_cheap_pair() -> None:
    """iss_da9e5e67: a non-schema-touching low-score fix no longer forces opus/fable."""
    _check(_validate("fix", "claude-sonnet-5") == "", "plain fix dispatch accepts claude-sonnet-5")
    _check(
        policy.validate_spawn_dispatch(
            dispatch_kind="fix", agent_runtime="claude_code", model="claude-sonnet-5",
            reviewed_report_vendor="", pair_id="",
            brief_text="Tidy the fleet_status projection; no schema work.",
        ) == (),
        "a non-schema brief applies no capability floor",
    )


def test_register_minted_kinds_are_first_class() -> None:
    for kind in REGISTER_MINTED_KINDS:
        _check(_validate(kind, "claude-opus-5") == "", f"register kind {kind!r} is accepted")
    for kind in ("unknown-test-kind", "test ", "TEST"):
        _check(_validate(kind, "claude-opus-5") == "", f"open nonblank provenance {kind!r} is accepted")
    for kind in ("", " ", "\t"):
        _check(_validate(kind, "claude-opus-5") == "dispatch_kind_required", f"blank kind {kind!r} refuses")
    for kind in (123,):
        _check(
            _raises_code(lambda kind=kind: policy.validate_dispatch_kind(kind)) == "dispatch_policy_violation",
            f"non-text kind {kind!r} refuses",
        )
    _check(
        _raises_code(lambda: policy.validate_dispatch_kind(None)) == "dispatch_kind_required",
        "missing kind retains dispatch_kind_required refusal",
    )
    _check(
        _validate("test", "claude-sonnet-5", scope_tags=("state_schema",)) == "",
        "state_schema provenance does not restrict a profiled model",
    )
    _check(
        _raises_code(lambda: policy.validate_spawn_dispatch(
            dispatch_kind="test", agent_runtime="codex", model="gpt-6-astra",
            reviewed_report_vendor="", pair_id="", scope_tags=("state_schema",),
        )) == "",
        "Astra is accepted with state_schema provenance",
    )


def test_state_schema_tag_is_provenance() -> None:
    """rul_0c6ec7c7 retires model gating by state schema scope or brief marker."""
    for kind in ("fix", "infrastructure"):
        for label, extra in (
            ("declared scope_tags", {"scope_tags": ("state_schema",)}),
            ("brief marker", {"brief_text": "declare the table in get_schema_definitions()"}),
        ):
            _check(
                _validate(kind, "claude-sonnet-5", **extra) == "",
                f"{kind} + {label} accepts profiled claude-sonnet-5",
            )
            for floor_model in ("claude-opus-5", "claude-fable-5-1"):
                _check(
                    _validate(kind, floor_model, **extra) == "",
                    f"{kind} + {label} accepts {floor_model}",
                )
    applied = policy.validate_spawn_dispatch(
        dispatch_kind="infrastructure", agent_runtime="claude_code", model="claude-opus-5",
        reviewed_report_vendor="", pair_id="", scope_tags=("state_schema",),
    )
    _check(
        applied == (),
        "state_schema remains scope provenance without an applied floor",
    )
    _check(
        _validate("infrastructure", "claude-opus-5", scope_tags=("future_scope",)) == "",
        "new nonblank scope tags remain open provenance",
    )
    _check(
        _validate("infrastructure", "claude-opus-5", scope_tags=(" ",)) == "scope_tag_invalid",
        "blank scope tags refuse",
    )


def test_select_dispatch_tier_has_no_state_schema_floor() -> None:
    """The selector never consults a dispatch-kind pair table."""
    from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: PLC0415

    infra, version, floors = verbs._capability_floor_pairs("infrastructure", ("state_schema",))  # noqa: SLF001
    _check(infra is None and version is not None and floors == (), "infrastructure + state_schema has no model floor")
    fix, _, _ = verbs._capability_floor_pairs("fix", ("state_schema",))  # noqa: SLF001
    _check(fix is None, "fix + state_schema has no model floor")
    plain_fix, _, no_floors = verbs._capability_floor_pairs("fix", ())  # noqa: SLF001
    _check(
        plain_fix is None and no_floors == (),
        "fix without scope_tags applies no pair filter",
    )
    _check(verbs._capability_floor_pairs(None, ()) == (None, None, ()), "no kind and no tags consults nothing")  # noqa: SLF001


def test_red_mutation_deleting_capability_floors_refuses_every_spawn() -> None:
    """The floor cannot be disabled by editing it out of the JSON: fail closed, like the fix row."""
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            without = json.loads(json.dumps(source))
            del without["capability_floors"]
            path.write_text(json.dumps(without), encoding="utf-8")
            _check(
                _validate("infrastructure", "claude-opus-5") == "dispatch_policy_invalid",
                "red mutation deleting capability_floors refuses even an infrastructure spawn",
            )
            restored = json.loads(json.dumps(source))
            restored["capability_floors"] = {"state_schema": {
                "issue_ids": ["iss_fixture"], "reason": "fixture",
                "brief_markers": ["TableSchema"],
                "floor_pairs": [{"agent_runtime": "claude_code", "model": "claude-opus-5"}],
            }}
            path.write_text(json.dumps(restored), encoding="utf-8")
            _check(
                _validate("infrastructure", "claude-opus-5") == "dispatch_policy_invalid",
                "red mutation restoring the retired state_schema model floor refuses",
            )
            malformed = json.loads(json.dumps(source))
            malformed["capability_floors"] = {"future_scope": {"issue_ids": ["iss_fixture"], "reason": "fixture", "brief_markers": ["FutureMarker"], "floor_pairs": [{"agent_runtime": "claude_code", "model": "claude-unprofiled-fixture"}]}}
            path.write_text(json.dumps(malformed), encoding="utf-8")
            _check(
                _validate("infrastructure", "claude-opus-5") == "dispatch_policy_invalid",
                "a future floor pair absent from model_profiles refuses policy loading",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


if __name__ == "__main__":
    test_missing_and_malformed_policy_refuse()
    test_red_mutation_deleting_dispatch_kinds_section_refuses()
    test_policy_without_retired_orchestrator_section()
    test_newer_profiled_model_is_not_seat_gated()
    test_solo_dispatch_is_accepted_under_diagnose_design_and_review()
    test_pair_allowlist_cannot_return_to_the_policy()
    test_low_score_fix_ticket_reaches_a_cheap_pair()
    test_register_minted_kinds_are_first_class()
    test_state_schema_tag_is_provenance()
    test_select_dispatch_tier_has_no_state_schema_floor()
    test_red_mutation_deleting_capability_floors_refuses_every_spawn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
