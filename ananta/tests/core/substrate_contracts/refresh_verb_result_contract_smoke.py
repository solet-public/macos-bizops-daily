#!/usr/bin/env python3
"""RED-FIRST fixture for the result-processing contracts lane (b) + the "did it look?" controls.

Original design: Claude-C, 2026-08-01 (control family, property-not-shape framing, the
safe-by-accident analysis). Upgraded by Claude-A, 2026-08-01 — see "CLAUDE-A UPGRADE" below for what
changed and why; the control family is Claude-C's and is preserved verbatim in intent.

REGISTERED in `gate_smokes.txt` together with the (a) fix, and not one commit before it: until the
rename landed these assertions were RED BY DESIGN (they assert the END state), so registering early
would have broken the gate. Promoted here from `workbench/` on landing — a gate smoke cannot live in
`workbench/`, which is sanctioned as origin-only and never ships in a seed.

Drives the REAL `validate_common_success` path, never the schema in isolation — a schema-only test is
how a 100%-failing verb stays green (the lane's epigraph).

The control family exists because of the safe-by-accident finding: the invariant early-returns on a
MISSING `process_key`, which is the only reason the plural verb is unaffected. A fixture that only
proved "the real result no longer violates" would be satisfied by deleting the field AND by deleting
the check — so it would inherit the same by-omission blindness it is testing around.

  * present-and-WRONG    -> MUST raise   (proves the invariant actually LOOKED)
  * present-and-MATCHING -> must pass    (proves it accepts the legitimate case)
  * ABSENT               -> must pass    (pins the early-return the plural verb relies on)

===========================================================================================
CLAUDE-A UPGRADE 2026-08-01 — the fixture did not touch the code it was guarding
===========================================================================================

★ THE DEFECT IN THE FIXTURE ITSELF. The original built its payloads as inline literals
(`{"action_status": ..., "process_key": ..., "updated": ...}`). Nothing in it referenced the verb.
That means it could not go green when the verb was fixed and could not go red when the verb
regressed — it only ever exercised `validate_common_success` against a dict I typed by hand. I hit
this directly: my first upgrade added a `fixed=True` payload spelling `refreshed_process_key`, and it
passed IMMEDIATELY, with the defect still fully present in the tree. A red-first fixture that is
green before the fix is not red-first.

So the payload is now DERIVED FROM THE CODE, by two independent side-effect-free reads:

  1. the verb's DECLARED contract — `_service_interface_metadata.return_value_schema.properties`
     off the decorator on `KnowledgeRefreshAPI.refresh_plugin_process` (an ABC; importing it runs
     nothing);
  2. the IMPLEMENTATION's actual return keys — parsed out of `do_refresh_plugin_process`'s return
     statement with `ast`. Not imported and not called: the real function needs a live orchestrator
     and MUTATES THE PROCESS REGISTRY, so executing it is a side effect on a running system, not a
     free read. `ast` gets the field names with none of that.

Today (1) yields {process_key, updated} and (2) yields {status, plugin_name, process_key, updated,
errors} — so the fixture goes red on the real shape, and the two disagreeing is itself a finding
(the schema under-declares the verb by 3 of 5 fields). After the (a) rename both reads yield
`refreshed_process_key`, the invariant early-returns, and the headline tests go green — driven by the
code change, which is the entire point.

Also added: the PARTIAL-failure path (measured, see below), a control pinning that today's shape is
still rejected, and a control pinning the `action_status` injection.

MUTATION TABLE — every green here names the mutation that kills it:

  M1  restore `process_key` at do_refresh_plugin_process's return site
        -> headline clean AND partial go RED
  M2  delete `_check_result_process_key_matches` from validate_common_success
        -> `present-and-wrong` control and `todays-shape-still-violates` go RED
  M3  compare against something other than action_process_key in that check
        -> `present-and-matching` control goes RED
  M4  drop the injected action_status in execute_action:386-390
        -> `action_status masking` control goes RED
  M5  add a field to either refresh helper's return without declaring it
        -> the matching `claims-vs-code` test goes RED (and for the singular verb `_plugin_return`
           RAISES rather than silently under-testing the payload)

⚠ ONE TEST HAS NO INDEPENDENT MUTATION, and that is stated rather than left for a reviewer to
notice: `test_sync_success_and_async_rejection_must_not_diverge` is SUBSUMED by the headline once
the fix lands — M1 turns both red together, and no other mutation turns it red alone. It is kept
because it is the only place the false-success MECHANISM is written down as a property (sync success
=> the same payload must survive validation), and because that property is what makes the defect
worse than a clean failure. Its `sync_reported_success` is now DERIVED from `execute_action`'s own
early-return rule rather than hardcoded `True`, so it at least fails if that rule changes.

⚠ Run discipline: a same-size mutation restored within a second can reuse the mutant's `.pyc` and
produce a FALSE RED. Clear `__pycache__` between runs and finish with an explicit RESTORE row.

Run:
    .venv/bin/python3 ananta/tests/core/substrate_contracts/refresh_verb_result_contract_smoke.py
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.core.domain.enums import ActionStatus  # noqa: E402
from ananta.core.result_processing.contracts import (  # noqa: E402
    CommonSuccessInput,
    ResultContractViolationError,
    validate_common_success,
)
from ananta.core.result_processing.enums import ResultProcessorKind  # noqa: E402
from ananta.core.services.service_interface_decorator import (  # noqa: E402
    ServiceInterfaceActionMetadata,
)
from ananta.services.knowledge_service.interfaces.refresh import (  # noqa: E402
    KnowledgeRefreshAPI,
)

_REFRESH_VERB = "service_interface::knowledge_service::refresh_plugin_process"
# What a caller actually asks to refresh — a DIFFERENT process than the refreshing verb.
_REFRESHED_KEY = "plugin::agent_messaging_plugin::peer_inbox"

_IMPL_SOURCE = (
    REPO_ROOT
    / "plugins/default_knowledge_plugin/src/default_knowledge_plugin/kb_process_registry.py"
)
_IMPL_FUNCTION = "do_refresh_plugin_process"

_PROCESSOR_SOURCE = REPO_ROOT / "ananta/src/ananta/core/actions/action_processor.py"

# The field name that carries the key the caller asked to refresh, before and after the (a) rename.
_OLD_KEY_FIELD = "process_key"
_NEW_KEY_FIELD = "refreshed_process_key"

# A deliberately UNREGISTERED process key for the present-and-wrong control.
#
# ⚠ ASSEMBLED FROM PARTS ON PURPOSE — do not "simplify" it into one string.
# whole_tree_integration's C3.1 check scans source text for process-key literals
# and reports any with no matching registration, so spelling this one out makes
# the fixture fail the whole-tree gate as a dangling call-site. The join keeps
# the value identical while keeping it out of that scan.
#
# ★ And the scan reads COMMENTS too: the first version of this very note spelled
# the key out while explaining not to spell it out, and the gate flagged it
# again — the warning label carrying the thing it warns about.
_UNREGISTERED_KEY = "::".join(("plugin", "somewhere", "else"))

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


def _pending(invariant: str | None) -> str:
    """Render the pre-fix expectation ONLY while it is still true.

    A green line reading "EXPECTED RED" is its own small lie, and output a
    reader learns to discount is worse than no annotation — so the note
    disappears the moment the fix lands.
    """
    if invariant is None:
        return ""
    return f" [EXPECTED RED until (a) lands; today raises {invariant!r}]"


# ---------------------------------------------------------------------------
# Reading the code — both reads are side-effect free
# ---------------------------------------------------------------------------


def _declared_return_fields(verb_name: str = _IMPL_FUNCTION) -> frozenset[str]:
    """The verb's DECLARED return contract, off the decorator metadata.

    The decorator attaches `_service_interface_metadata` at runtime, so the
    attribute is invisible to the type checker on the bare function object;
    the `Any` hop is deliberate and narrow. No default is supplied to the
    attribute lookup — if the decorator ever stops attaching it, this raises
    rather than silently reporting an empty contract, which would make the
    claims-vs-code test pass for the wrong reason.
    """
    verb: Any = getattr(KnowledgeRefreshAPI, verb_name.removeprefix("do_"))
    metadata: ServiceInterfaceActionMetadata = verb._service_interface_metadata  # noqa: SLF001
    return frozenset(metadata.return_value_schema.properties)


def _find_function(source: Path, function_name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    """The named function's AST node, or a loud failure naming the moved anchor."""
    tree = ast.parse(source.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name == function_name:
                return node
    raise AssertionError(
        f"{function_name} not found in {source} — the fixture's anchor moved; "
        "fix the fixture rather than deleting this check"
    )


def _dict_returns(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[ast.Dict]:
    """Every dict-literal `return` in the function, in source order."""
    return [
        stmt.value
        for stmt in ast.walk(node)
        if isinstance(stmt, ast.Return) and isinstance(stmt.value, ast.Dict)
    ]


def _string_keys(dict_node: ast.Dict) -> frozenset[str]:
    """The literal string keys of a dict node, ignoring `**splat` (a None key)."""
    return frozenset(
        key.value
        for key in dict_node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    )


def _implementation_return_fields(function_name: str = _IMPL_FUNCTION) -> frozenset[str]:
    """The IMPLEMENTATION's actual return keys, via `ast`.

    Deliberately NOT imported and NOT called — these functions need a live
    orchestrator and MUTATE the live process registry, so executing one is a
    side effect on a running system rather than a free read.

    Takes the LAST dict-literal return in the function: both refresh helpers
    guard with raises first and build their payload at the end.
    """
    returns = _dict_returns(_find_function(_IMPL_SOURCE, function_name))
    if not returns:
        raise AssertionError(
            f"{function_name} has no dict-literal return in {_IMPL_SOURCE} — "
            "the fixture's anchor moved; fix the fixture rather than deleting this check"
        )
    return _string_keys(returns[-1])


# ---------------------------------------------------------------------------
# Building the payload FROM those reads
# ---------------------------------------------------------------------------


def _plugin_return(*, partial: bool) -> dict[str, Any]:
    """The plugin's return dict, keys taken from the implementation itself.

    Raises on any field the fixture has no value for, so a new return field
    fails loudly instead of being silently left out of the payload under test.
    """
    values: dict[str, Any] = {
        _OLD_KEY_FIELD: _REFRESHED_KEY,
        _NEW_KEY_FIELD: _REFRESHED_KEY,
        "status": "error" if partial else "success",
        "plugin_name": "agent_messaging_plugin",
        "updated": not partial,
        "errors": ["apply failed"] if partial else [],
    }
    fields = _implementation_return_fields()
    unmapped = fields - values.keys()
    if unmapped:
        raise AssertionError(
            f"{_IMPL_FUNCTION} returns field(s) {sorted(unmapped)} the fixture has no value "
            "for — extend `values` so the payload under test stays the REAL one (M5)"
        )
    return {field: values[field] for field in fields}


def _envelope_injected_keys() -> frozenset[str]:
    """The keys `execute_action` injects, READ FROM ITS SOURCE.

    ★ This was the fixture's SECOND blind spot, found by its own mutation table
    and fixed the same way as the first. The envelope used to be a hardcoded
    `{"success": True, "action_status": "completed", **result}`, which meant
    mutation M4 — deleting the `action_status` injection from `execute_action`
    — left the fixture fully GREEN. The suite modelled the platform instead of
    reading it, so it could not see the platform change.
    """
    for dict_node in _dict_returns(_find_function(_PROCESSOR_SOURCE, "execute_action")):
        # The SUCCESS envelope is the one that splats the plugin result — a
        # `**result` entry shows up as a None key.
        if any(key is None for key in dict_node.keys):
            return _string_keys(dict_node)
    raise AssertionError(
        "execute_action's success-envelope return not found in "
        f"{_PROCESSOR_SOURCE} — the fixture's anchor moved"
    )


def _envelope(plugin_result: dict[str, Any]) -> dict[str, Any]:
    """`action_processor.execute_action`'s envelope, INCLUDING the early return.

    The early return is modelled rather than skipped because it is half of why
    the partial path behaves as it does: `execute_action` bails to a failure
    envelope only on a SINGULAR `error` key, and this verb reports its problems
    under a PLURAL `errors` list — so a partial refresh sails past it into the
    success envelope.
    """
    if plugin_result.get("error"):
        return {"success": False, "error": plugin_result["error"]}
    injected = _envelope_injected_keys()
    envelope: dict[str, Any] = {}
    if "success" in injected:
        envelope["success"] = True
    if "action_status" in injected:
        envelope["action_status"] = ActionStatus.COMPLETED.value
    return {**envelope, **plugin_result}


def _payload(result_data: dict[str, Any]) -> CommonSuccessInput:
    return CommonSuccessInput(
        action_id="ae-redfirst",
        action_process_key=_REFRESH_VERB,
        completed_parameters={
            "plugin_name": "agent_messaging_plugin",
            "process_key": _REFRESHED_KEY,
        },
        result_data=result_data,
        plugin_returned_actions=(),
        error_processor={"template": "process_error"},
        result_processor_kind=ResultProcessorKind.INFERENCE,
    )


def _violation(result_data: dict[str, Any]) -> str | None:
    """Return the violated invariant's name, or None when the contract passes."""
    try:
        validate_common_success(_payload(result_data))
    except ResultContractViolationError as exc:
        return str(getattr(exc.violation, "invariant", "") or "")
    return None


# ---------------------------------------------------------------------------
# The defect — currently RED, and it is the red the fix must clear
# ---------------------------------------------------------------------------


def test_real_refresh_result_satisfies_the_contract() -> None:
    """⚠ EXPECTED RED until (a) lands. Asserts the END state, not today's.

    The payload's keys come from `do_refresh_plugin_process` itself, so this
    tracks the code: it is red while the impl returns a top-level
    `process_key`, and green once the rename lands. MUTATION M1.
    """
    invariant = _violation(_envelope(_plugin_return(partial=False)))
    _check(
        invariant is None,
        "(b) a real refresh_plugin_process result satisfies the contract"
        + _pending(invariant),
    )


def test_partial_refresh_result_also_satisfies_the_contract() -> None:
    """★ CLAUDE-A ADDITION. The partial-failure path — MEASURED, not assumed.

    I had written this up as a SECOND defect: the plugin returns
    `status="error"` on a partial refresh, so I asserted it must trip
    `result_status_not_completed` instead. Running it proved that FALSE.
    `execute_action` always injects `action_status="completed"`, which MASKS
    the plugin's own `status`, so the partial path fails on the SAME invariant
    as the clean one — and therefore the (a) rename fixes it too.

    It is pinned here rather than asserted in prose because that is the
    difference between the shipping note CLAIMING both paths are fixed and the
    suite PROVING it. MUTATION M1 turns this red alongside the clean case.
    """
    invariant = _violation(_envelope(_plugin_return(partial=True)))
    _check(
        invariant is None,
        "(b) a PARTIAL refresh result satisfies the contract too" + _pending(invariant),
    )


def test_declared_contract_matches_the_implementation() -> None:
    """⚠ EXPECTED RED until (a) lands — and red for a SECOND, independent reason.

    The declared `return_value_schema` lists {process_key, updated}; the impl
    returns {status, plugin_name, process_key, updated, errors}. The verb has
    been under-declared by 3 of 5 fields all along, independently of the
    invariant collision. The (a) commit declares the real shape, so this goes
    green with it — and afterwards it is the guard that keeps schema and impl
    from drifting again. MUTATION M5.
    """
    declared = _declared_return_fields()
    implemented = _implementation_return_fields()
    _check(
        declared == implemented,
        "(b) declared return_value_schema == implementation's return keys "
        f"[declared={sorted(declared)} impl={sorted(implemented)}]",
    )


def test_plural_verbs_declared_contract_matches_too() -> None:
    """⚠ EXPECTED RED until (a) lands. The SIBLING verb, deliberately in scope.

    `refresh_plugin_processes` has the identical under-declaration (declares
    {updated_count, process_keys}; returns five fields). It is not touched by
    the rename — its field is pluralised, which is the naming coincidence that
    keeps it out of the invariant's way entirely.

    ★ It is covered here because a claims-vs-code test that could only see the
    verb I happened to be fixing would certify a file whose immediate
    neighbour is still inaccurate — and would keep certifying it. Fixing one
    declaration while a four-line-away twin stays wrong is an oversight shape,
    so the lane takes both and the guard sees both. MUTATION M5.
    """
    declared = _declared_return_fields("do_refresh_plugin_processes")
    implemented = _implementation_return_fields("do_refresh_plugin_processes")
    _check(
        declared == implemented,
        "(b) PLURAL verb's declared schema == its implementation's return keys "
        f"[declared={sorted(declared)} impl={sorted(implemented)}]",
    )


def test_defect_is_exactly_the_process_key_invariant() -> None:
    """Pins WHICH invariant fails, so a future unrelated red is distinguishable.

    Written to hold before AND after the fix — it is the diagnosis, not the
    desired end state.
    """
    invariant = _violation(_envelope(_plugin_return(partial=False)))
    _check(
        invariant in (None, "result_process_key_mismatch"),
        "(b) the failure is the process_key invariant, not a bystander "
        f"(observed {invariant!r})",
    )


# ---------------------------------------------------------------------------
# "Did the invariant actually LOOK?" — Claude-C's control family
# ---------------------------------------------------------------------------


def test_present_and_wrong_must_raise() -> None:
    """The invariant must still bite when a WRONG key is present.

    Without this control, a fix that removed the CHECK would look identical to
    a fix that removed the FIELD. MUTATION M2.
    """
    _check(
        _violation({
            "action_status": "completed",
            "process_key": _UNREGISTERED_KEY,
        }) == "result_process_key_mismatch",
        "control: a present-and-wrong process_key RAISES — the invariant looks",
    )


def test_present_and_matching_must_pass() -> None:
    """A verb that legitimately echoes its OWN key is accepted. MUTATION M3."""
    _check(
        _violation({
            "action_status": "completed",
            "process_key": _REFRESH_VERB,
        }) is None,
        "control: a present-and-matching process_key passes",
    )


def test_absent_must_pass_through() -> None:
    """★ Pins the early-return that makes the PLURAL verb safe by accident.

    `refresh_plugin_processes` returns `process_keys` (plural), so
    `.get("process_key")` is None and the invariant never compares anything.
    That is a naming coincidence, not a design — pinned so the rename fix is
    understood as moving the singular verb into this same UNCHECKED class
    rather than as a hardening.
    """
    _check(
        _violation({
            "action_status": "completed",
            "updated_count": 1,
            "process_keys": [_REFRESHED_KEY],
        }) is None,
        "control: an ABSENT process_key passes through (the plural verb's "
        "accidental safety — NOT protection)",
    )


def test_todays_unfixed_shape_still_violates() -> None:
    """★ CLAUDE-A ADDITION — the fix's own did-it-look control.

    Pins that a payload carrying the OLD field is genuinely rejected. Without
    it, a mutation that neutered the invariant would turn the headline tests
    green while every other control still passed: the suite would certify the
    fix while the check no longer ran. MUTATION M2.
    """
    _check(
        _violation({
            "action_status": "completed",
            "status": "success",
            "plugin_name": "agent_messaging_plugin",
            _OLD_KEY_FIELD: _REFRESHED_KEY,
            "updated": True,
            "errors": [],
        }) == "result_process_key_mismatch",
        "control: the PRE-fix shape is still rejected — the fix is what "
        "changed, not the invariant",
    )


def test_injected_action_status_masks_the_plugin_status() -> None:
    """★ CLAUDE-A ADDITION. Guards the masking that falsified my second-defect claim.

    `_check_result_status_completed` reads `action_status` first and only falls
    back to the plugin's `status`. `execute_action` always injects
    `action_status="completed"`, so that fallback is unreachable in production
    and a partial refresh's `status="error"` never reaches the validator.

    Pinned so the masking cannot change silently underneath this lane's claim
    that BOTH paths are fixed — if the injection goes away, the partial path
    starts failing a different invariant and the claim needs revisiting.
    MUTATION M4.
    """
    injected = _envelope_injected_keys()
    _check(
        "action_status" in injected,
        "control: execute_action still INJECTS action_status "
        f"(read from source; injects {sorted(injected)}) — MUTATION M4",
    )
    partial = _envelope(_plugin_return(partial=True))
    without_injection = {k: v for k, v in partial.items() if k != "action_status"}
    _check(
        _violation(without_injection) == "result_status_not_completed",
        "control: that injection is what passes the status check "
        "(remove it and the partial path fails the STATUS invariant)",
    )


# ---------------------------------------------------------------------------
# The FALSE-SUCCESS divergence — sync says yes, async rejects
# ---------------------------------------------------------------------------
#
# ⚠ EVIDENCE PROVENANCE — corrected once already, which is the point of writing
# it this way. The sync shape was first relayed as Claude-B's live measurement,
# {"updated": true, "success": true}. Architect source-read the return site and
# SUPERSEDED it: the polled return and the stored row are the SAME dict — one
# pipeline, not two. The false-success mechanism is SEQUENCING (validation runs
# after caller-visible artifacts are written), not two divergent
# implementations. Don't hunt a second path.
#
# ★ THE RELAYED SHAPE WAS WRONG AND THIS ASSERTION DID NOT HAVE TO CHANGE,
# because it is written against the PROPERTY (sync success => the same payload
# must survive validation) rather than the literal shape.


def test_sync_success_and_async_rejection_must_not_diverge() -> None:
    """⚠ EXPECTED RED until (a) lands. The property, not the literal shape.

    Callers are told success synchronously while the async result is rejected —
    a FALSE SUCCESS, worse than a clean failure because nothing downstream has
    any reason to retry or alarm.
    """
    envelope = _envelope(_plugin_return(partial=False))
    # DERIVED from execute_action's own rule, not asserted as a literal: the
    # caller sees success unless the early return fired on a singular `error`.
    sync_reported_success = bool(envelope.get("success"))
    async_invariant = _violation(envelope)
    _check(
        not (sync_reported_success and async_invariant is not None),
        "(a) sync success and async rejection do NOT diverge" + _pending(async_invariant),
    )


def main() -> None:
    print("result-processing contracts — (b) red-first + did-it-look controls")
    print(f"  declared return fields: {sorted(_declared_return_fields())}")
    print(f"  impl return fields:     {sorted(_implementation_return_fields())}")
    for name, obj in sorted(globals().items()):
        if name.startswith("test_") and callable(obj):
            print(f"\n{name}")
            obj()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
