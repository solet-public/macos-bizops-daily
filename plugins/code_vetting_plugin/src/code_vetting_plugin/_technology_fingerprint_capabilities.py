"""Deterministic capability selection and exact reconciliation."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Final

from ._technology_fingerprint_probes import (
    _FRAMEWORK_COMPONENTS,
    _PROBE_ROSTER,
    _PROBE_STATUSES,
    _RUNTIME_PERFORMANCE_COMPONENTS,
)


def _fact_trigger(fact: Mapping[str, object], *, kind: str) -> list[dict[str, str]]:
    evidence = fact.get("evidence")
    if isinstance(evidence, list):
        return [
            dict(item)
            for item in evidence
            if isinstance(item, Mapping)
            and all(isinstance(item.get(key), str) for key in ("path", "pointer", "kind"))
        ]
    if isinstance(evidence, Mapping) and all(
        isinstance(evidence.get(key), str) for key in ("path", "pointer", "kind")
    ):
        return [dict(evidence)]
    raise RuntimeError(f"{kind} fact has no exact evidence pointer")


def _capability_row(
    *,
    capability_key: str,
    scope: str,
    status: str,
    trigger: str,
    evidence: Sequence[Mapping[str, str]],
    boundary: Mapping[str, str],
    coverage_authority: str,
) -> dict[str, object]:
    return {
        "capability_key": capability_key,
        "scope": scope,
        "status": status,
        "execution_status": "not_run_by_fingerprint",
        "trigger": trigger,
        "trigger_evidence": [dict(item) for item in evidence],
        "routing": {
            "status": "selected",
            "is_execution": False,
            "note": "Fingerprint routing records a need; it does not execute a capability.",
        },
        "execution_boundary": dict(boundary),
        "coverage_authority": coverage_authority,
    }


_NO_EXECUTION_BOUNDARY: Final[dict[str, str]] = {
    "target_code": "not_executed",
    "network": "not_used",
    "sandbox": "not_entered",
}
_TARGET_TOOL_BOUNDARY: Final[dict[str, str]] = {
    "target_code": "would_execute_target_controlled_code",
    "network": "deny_unless_separately_authorized",
    "sandbox": "required_for_foreign_target_execution",
}
_STATIC_REVIEW_BOUNDARY: Final[dict[str, str]] = {
    "target_code": "not_required_for_static_review",
    "network": "not_used_by_fingerprint",
    "sandbox": "not_entered",
}
# Performance BENCHMARKING is out of scope by ruling (2026-07-31): this is not a
# performance-analysis toolset. `adapter_missing` asserted an INTENT we do not have
# ("we should do this and lack the adapter"); this status asserts the truth instead.
# The row is KEPT rather than dropped so the coverage artifact can state what was not
# covered with the denominator visible — an out-of-scope row is the shape that carries.
# ⚠ The execution boundary deliberately STAYS `_TARGET_TOOL_BOUNDARY`: the status
# records our intent, the boundary records a PROPERTY of the capability (performing it
# would run target-controlled code), and that property is still true of the hypothetical.
_OUT_OF_SCOPE_STATUS: Final[str] = "out_of_scope"
_FINGERPRINT_COVERAGE_AUTHORITY: Final[str] = (
    "The fingerprint selects routing needs only and never reports execution, pass, or clean."
)
_SCANNER_LEDGER_AUTHORITY: Final[str] = (
    "Only the scanner coverage ledger records whether the target toolchain ran."
)


def _routing_eligible(item: Mapping[str, object]) -> bool:
    role = item.get("source_role")
    return isinstance(role, Mapping) and role.get("routing_eligible") is True


def _eligible_direct_components(
    components: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for fact in components:
        if fact.get("status") == "direct_declared" and _routing_eligible(fact):
            grouped.setdefault(str(fact["scope"]), []).append(fact)
    return grouped


def _eligible_tsconfigs(
    configs: Sequence[Mapping[str, object]],
) -> dict[str, list[Mapping[str, object]]]:
    grouped: dict[str, list[Mapping[str, object]]] = {}
    for config in configs:
        if config.get("config_key") == "tsconfig_json" and _routing_eligible(config):
            grouped.setdefault(str(config["scope"]), []).append(config)
    return grouped


def _direct_trigger_evidence(
    facts: Sequence[Mapping[str, object]],
) -> list[dict[str, str]]:
    return [
        item
        for fact in facts
        for item in _fact_trigger(fact, kind="component")
        if item["kind"] == "direct_declaration"
    ]


def _component_capability_rows(
    scope: str,
    scoped: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    specs = (
        (
            "expo_project_health",
            "adapter_missing",
            "eligible direct expo or expo-router declaration",
            frozenset({"expo", "expo_router"}),
            _TARGET_TOOL_BOUNDARY,
            _FINGERPRINT_COVERAGE_AUTHORITY,
        ),
        (
            "foreign_framework_architecture",
            "dynamic_guidance_required",
            "eligible direct foreign-framework declaration",
            _FRAMEWORK_COMPONENTS,
            _NO_EXECUTION_BOUNDARY,
            _FINGERPRINT_COVERAGE_AUTHORITY,
        ),
        (
            "runtime_performance",
            _OUT_OF_SCOPE_STATUS,
            "eligible direct client-runtime declaration",
            _RUNTIME_PERFORMANCE_COMPONENTS,
            _TARGET_TOOL_BOUNDARY,
            _FINGERPRINT_COVERAGE_AUTHORITY,
        ),
    )
    rows: list[dict[str, object]] = []
    for key, status, trigger, component_keys, boundary, authority in specs:
        selected = [fact for fact in scoped if fact.get("component_key") in component_keys]
        if selected:
            rows.append(
                _capability_row(
                    capability_key=key,
                    scope=scope,
                    status=status,
                    trigger=trigger,
                    evidence=_direct_trigger_evidence(selected),
                    boundary=boundary,
                    coverage_authority=authority,
                )
            )
    return rows


def _typescript_row(
    scope: str,
    scoped: Sequence[Mapping[str, object]],
    configs: Sequence[Mapping[str, object]],
) -> dict[str, object] | None:
    typescript = [fact for fact in scoped if fact.get("component_key") == "typescript"]
    trigger_evidence: list[dict[str, str]] = _direct_trigger_evidence(typescript)
    for config in configs:
        trigger_evidence.extend(_fact_trigger(config, kind="config"))
    if not trigger_evidence:
        return None
    return _capability_row(
        capability_key="typescript_target_toolchain",
        scope=scope,
        status="available_opt_in",
        trigger="eligible direct TypeScript declaration or tsconfig presence",
        evidence=trigger_evidence,
        boundary=_TARGET_TOOL_BOUNDARY,
        coverage_authority=_SCANNER_LEDGER_AUTHORITY,
    )


def _manifest_evidence(item: Mapping[str, object], key: str) -> list[dict[str, str]]:
    raw = item.get(key)
    if not isinstance(raw, list):
        raise RuntimeError(f"capability fact field {key!r} must be a list")
    return [
        dict(evidence_item)
        for evidence_item in raw
        if isinstance(evidence_item, Mapping)
        and all(isinstance(evidence_item.get(field), str) for field in ("path", "pointer", "kind"))
    ]


def _deno_capability_rows(
    deno_scopes: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    # Status is per-spec, not per-loop: `runtime_performance` is out of scope by ruling
    # while `deno_check_lint_test` genuinely lacks an adapter. A single hardcoded status
    # here would have silently re-asserted the false claim for the deno emission site.
    specs = (
        ("deno_check_lint_test", "adapter_missing", "eligible parsed deno runtime manifest"),
        ("runtime_performance", _OUT_OF_SCOPE_STATUS, "eligible parsed deno runtime manifest"),
    )
    rows: list[dict[str, object]] = []
    for runtime in deno_scopes:
        if runtime.get("status") != "runtime_declared" or not _routing_eligible(runtime):
            continue
        for capability_key, status, trigger in specs:
            rows.append(
                _capability_row(
                    capability_key=capability_key,
                    scope=str(runtime["scope"]),
                    status=status,
                    trigger=trigger,
                    evidence=_manifest_evidence(runtime, "manifest_evidence"),
                    boundary=_TARGET_TOOL_BOUNDARY,
                    coverage_authority=_FINGERPRINT_COVERAGE_AUTHORITY,
                )
            )
    return rows


def _supabase_capability_rows(
    topology: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    return [
        _capability_row(
            capability_key="supabase_db_rls_static_review",
            scope=str(item["scope"]),
            status="adapter_missing",
            trigger="eligible Supabase config, migration, or Edge Function topology",
            evidence=_manifest_evidence(item, "evidence"),
            boundary=_STATIC_REVIEW_BOUNDARY,
            coverage_authority=_FINGERPRINT_COVERAGE_AUTHORITY,
        )
        for item in topology
        if item.get("routing_eligible") is True
    ]


def _merge_trigger_evidence(existing: dict[str, object], incoming: Mapping[str, object]) -> None:
    existing_evidence = existing.get("trigger_evidence")
    new_evidence = incoming.get("trigger_evidence")
    if not isinstance(existing_evidence, list) or not isinstance(new_evidence, list):
        raise RuntimeError("capability trigger evidence must be a list")
    combined = {
        (str(item["path"]), str(item["pointer"]), str(item["kind"])): dict(item)
        for item in [*existing_evidence, *new_evidence]
        if isinstance(item, Mapping)
    }
    existing_evidence[:] = [combined[key] for key in sorted(combined)]


def _ordered_capabilities(
    rows: Sequence[dict[str, object]],
) -> list[dict[str, object]]:
    unique: dict[tuple[str, str], dict[str, object]] = {}
    for row in rows:
        key = (str(row["scope"]), str(row["capability_key"]))
        if key in unique:
            _merge_trigger_evidence(unique[key], row)
        else:
            unique[key] = row
    ordered = [unique[key] for key in sorted(unique)]
    for row in ordered:
        _validate_and_sort_evidence(row)
    return ordered


def _validate_and_sort_evidence(row: dict[str, object]) -> None:
    trigger_evidence = row.get("trigger_evidence")
    if not isinstance(trigger_evidence, list) or not trigger_evidence:
        raise RuntimeError(
            f"capability {row.get('capability_key')!r} has no exact trigger evidence"
        )
    trigger_evidence.sort(
        key=lambda item: (
            str(item["path"]),
            str(item["pointer"]),
            str(item["kind"]),
        )
    )


def capability_needs(
    components: Sequence[Mapping[str, object]],
    deno_scopes: Sequence[Mapping[str, object]],
    supabase: Sequence[Mapping[str, object]],
    configs: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    by_scope = _eligible_direct_components(components)
    tsconfig_by_scope = _eligible_tsconfigs(configs)
    rows: list[dict[str, object]] = []
    for scope in sorted(set(by_scope) | set(tsconfig_by_scope)):
        scoped = by_scope.get(scope, [])
        rows.extend(_component_capability_rows(scope, scoped))
        typescript = _typescript_row(scope, scoped, tsconfig_by_scope.get(scope, []))
        if typescript is not None:
            rows.append(typescript)
    rows.extend(_deno_capability_rows(deno_scopes))
    rows.extend(_supabase_capability_rows(supabase))
    return _ordered_capabilities(rows)


def unreadable_gaps(probes: Sequence[Mapping[str, object]]) -> list[dict[str, object]]:
    gaps: list[dict[str, object]] = []
    for probe in probes:
        observations = probe.get("observations")
        if not isinstance(observations, list):
            raise RuntimeError("probe observations must be a list")
        for observation in observations:
            if not isinstance(observation, Mapping):
                raise RuntimeError("probe observation must be an object")
            if observation.get("status") != "unreadable":
                continue
            gaps.append(
                {
                    "probe": probe["probe"],
                    **dict(observation),
                }
            )
    gaps.sort(
        key=lambda item: (
            str(item["probe"]),
            str(item.get("path", "")),
            str(item.get("reason", "")),
        )
    )
    return gaps


def _nonnegative_int(source: Mapping[str, object], key: str) -> int:
    value = source.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(f"technology fingerprint field {key!r} must be a non-negative int")
    return value


def _probe_reconciliation(
    probes: Sequence[Mapping[str, object]],
) -> tuple[dict[str, object], dict[str, object]]:
    status_counts = dict.fromkeys(_PROBE_STATUSES, 0)
    candidate_total = 0
    matched_candidates = 0
    unreadable_candidates = 0
    for probe in probes:
        status_value = probe.get("status")
        if status_value not in status_counts:
            raise RuntimeError(f"unknown probe status: {status_value!r}")
        status_counts[str(status_value)] += 1
        candidate_total += _nonnegative_int(probe, "candidate_count")
        matched_candidates += _nonnegative_int(probe, "matched_count")
        unreadable_candidates += _nonnegative_int(probe, "unreadable_count")
    roster_ok = len(probes) == len(_PROBE_ROSTER) and sum(status_counts.values()) == len(
        _PROBE_ROSTER
    )
    candidates_ok = candidate_total == matched_candidates + unreadable_candidates
    return (
        {
            "total": len(_PROBE_ROSTER),
            "by_status": status_counts,
            "reconciles": roster_ok,
        },
        {
            "total": candidate_total,
            "matched": matched_candidates,
            "unreadable": unreadable_candidates,
            "reconciles": candidates_ok,
        },
    )


def _component_reconciliation(
    components: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    direct = sum(1 for item in components if item.get("status") == "direct_declared")
    lock_only = sum(1 for item in components if item.get("status") == "lockfile_only")
    return {
        "total": len(components),
        "direct_declared": direct,
        "lockfile_only": lock_only,
        "reconciles": len(components) == direct + lock_only,
    }


def _capability_reconciliation(
    capabilities: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    not_run = sum(item.get("execution_status") == "not_run_by_fingerprint" for item in capabilities)
    return {
        "total": len(capabilities),
        "not_run_by_fingerprint": not_run,
        "reconciles": not_run == len(capabilities),
    }


def reconciliation(
    probes: Sequence[Mapping[str, object]],
    components: Sequence[Mapping[str, object]],
    capabilities: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    roster, candidates = _probe_reconciliation(probes)
    component_counts = _component_reconciliation(components)
    capability_counts = _capability_reconciliation(capabilities)
    sections = (roster, candidates, component_counts, capability_counts)
    if not all(section["reconciles"] is True for section in sections):
        raise RuntimeError("technology fingerprint failed exact reconciliation")
    return {
        "probe_roster": roster,
        "probe_candidates": candidates,
        "components": component_counts,
        "capability_needs": capability_counts,
        "reconciles": True,
    }
