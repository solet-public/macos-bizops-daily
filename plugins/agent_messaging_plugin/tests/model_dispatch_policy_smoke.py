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


def test_red_mutation_deleting_fix_row_makes_fix_refuse() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    del source["dispatch_kinds"]["fix"]
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
                "red mutation deleting the fix row refuses fix dispatch",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_orchestrator_model_requires_profile_pair() -> None:
    original = policy._POLICY_PATH  # noqa: SLF001 -- red mutation fixture
    source = json.loads(original.read_text(encoding="utf-8"))
    source["orchestrator"]["allowed_models"].append("claude-unprofiled-fixture")
    with tempfile.TemporaryDirectory() as raw:
        path = Path(raw) / "policy.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        try:
            policy._POLICY_PATH = path  # type: ignore[misc]  # noqa: SLF001
            _check(
                _raises_code(lambda: policy.load_dispatch_policy()) == "dispatch_policy_invalid",
                "orchestrator model without a profile pair refuses policy loading",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


def test_fable_5_1_orchestrator_model_is_allowed() -> None:
    role_label = chr(65) + "da-Main"
    _check(
        policy.orchestrator_model_verdict(role_label=role_label, model="claude-fable-5-1") == (
            True,
            ("claude-sonnet-5", "claude-fable-5", "claude-fable-5-1"),
        ),
        "claude-fable-5-1 is allowed for the main role",
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


def test_state_schema_floor_outranks_fix_and_infrastructure() -> None:
    """iss_63d91ca9 / iev_1850901f recommendations 2 + 3: the floor is independent of
    dispatch_kind and of the ticket's raw score, and allow_any_pair cannot lower it."""
    for kind in ("fix", "infrastructure"):
        for label, extra in (
            ("declared scope_tags", {"scope_tags": ("state_schema",)}),
            ("brief marker", {"brief_text": "declare the table in get_schema_definitions()"}),
        ):
            _check(
                _validate(kind, "claude-sonnet-5", **extra) == "capability_floor_violation",
                f"{kind} + {label} refuses claude-sonnet-5",
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
        applied == (policy.AppliedFloor(tag="state_schema", source="declared", detail="scope_tags"),),
        "the applied floor names its source (declared)",
    )
    _check(
        _validate("infrastructure", "claude-opus-5", scope_tags=("not_a_floor",)) == "scope_tag_unknown",
        "an undeclared scope tag refuses rather than silently applying nothing",
    )


def test_select_dispatch_tier_uses_only_capability_floors() -> None:
    """The selector never consults a dispatch-kind pair table."""
    from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: PLC0415

    floor = frozenset({("claude_code", "claude-opus-5"), ("claude_code", "claude-fable-5-1"), ("codex", "gpt-5.6-sol")})
    infra, version, floors = verbs._capability_floor_pairs("infrastructure", ("state_schema",))  # noqa: SLF001
    _check(infra == floor and version is not None and len(floors) == 1, "infrastructure + state_schema narrows to the floor pairs")
    fix, _, _ = verbs._capability_floor_pairs("fix", ("state_schema",))  # noqa: SLF001
    _check(fix == floor, "fix + state_schema is the floor regardless of kind")
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
            renamed = json.loads(json.dumps(source))
            renamed["capability_floors"] = {"something_else": renamed["capability_floors"]["state_schema"]}
            path.write_text(json.dumps(renamed), encoding="utf-8")
            _check(
                _validate("infrastructure", "claude-opus-5") == "dispatch_policy_invalid",
                "red mutation renaming the required state_schema floor refuses",
            )
            cheap = json.loads(json.dumps(source))
            cheap["capability_floors"]["state_schema"]["floor_pairs"].append(
                {"agent_runtime": "claude_code", "model": "claude-unprofiled-fixture"},
            )
            path.write_text(json.dumps(cheap), encoding="utf-8")
            _check(
                _validate("infrastructure", "claude-opus-5") == "dispatch_policy_invalid",
                "a floor pair absent from model_profiles refuses policy loading",
            )
        finally:
            policy._POLICY_PATH = original  # type: ignore[misc]  # noqa: SLF001


if __name__ == "__main__":
    test_missing_and_malformed_policy_refuse()
    test_red_mutation_deleting_fix_row_makes_fix_refuse()
    test_orchestrator_model_requires_profile_pair()
    test_fable_5_1_orchestrator_model_is_allowed()
    test_solo_dispatch_is_accepted_under_diagnose_design_and_review()
    test_pair_allowlist_cannot_return_to_the_policy()
    test_low_score_fix_ticket_reaches_a_cheap_pair()
    test_state_schema_floor_outranks_fix_and_infrastructure()
    test_select_dispatch_tier_uses_only_capability_floors()
    test_red_mutation_deleting_capability_floors_refuses_every_spawn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    raise SystemExit(1 if _failed else 0)
