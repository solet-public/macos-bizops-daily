"""Fail-closed model and vendor policy for every managed spawn.

The policy is deliberately data-backed: changing an assignment rule means a
reviewable JSON edit, while this module owns parsing and rejection semantics.
No policy is cached. A missing or malformed file therefore refuses the first
spawn after an edit instead of leaving a stale, silently permissive process.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final

_POLICY_PATH: Final[Path] = Path(__file__).resolve().parents[2] / "model_dispatch_policy.v1.json"
_PROFILE_ROOT: Final[Path] = Path(__file__).resolve().parents[2] / "model_profiles"
_DISPATCH_KINDS: Final[frozenset[str]] = frozenset(
    {"diagnose", "design", "review", "fix", "infrastructure"}
)
ModelPair = tuple[str, str]
# Capability floors the policy file MUST declare. A floor is a capability
# dimension independent of dispatch_kind: when a spawn's declared scope
# touches it, the (runtime, model) pair must be one of the floor's own pairs
# whatever dispatch provenance label the caller supplies. Requiring the
# key here (not merely honouring it when present) is what makes the floor
# impossible to disable by deleting it from the JSON: a policy missing a
# required floor refuses EVERY spawn, the same fail-closed shape as a policy
# missing the ``fix`` row (iss_63d91ca9 / iss_da9e5e67, 2026-09-20).
_REQUIRED_CAPABILITY_FLOORS: Final[frozenset[str]] = frozenset({"state_schema"})
_FLOOR_KEYS: Final[frozenset[str]] = frozenset({"issue_ids", "reason", "brief_markers", "floor_pairs"})
_FLOOR_SOURCE_DECLARED: Final[str] = "declared"
_FLOOR_SOURCE_BRIEF: Final[str] = "brief_marker"


class DispatchPolicyError(Exception):
    """A deterministic refusal emitted before a managed-session write."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CapabilityFloor:
    """A scope-keyed minimum-capability rule that outranks every dispatch_kind."""

    tag: str
    issue_ids: tuple[str, ...]
    reason: str
    brief_markers: tuple[str, ...]
    floor_pairs: tuple[ModelPair, ...]

    def marker_in(self, brief_text: str) -> str | None:
        """The first declared marker present in ``brief_text``, or None."""
        return next((marker for marker in self.brief_markers if marker in brief_text), None)


@dataclass(frozen=True, slots=True)
class AppliedFloor:
    """One capability floor that a spawn's declared scope triggered, and why."""

    tag: str
    source: str
    detail: str


@dataclass(frozen=True, slots=True)
class DispatchPolicy:
    policy_version: str
    orchestrator_role_names: frozenset[str]
    orchestrator_models: tuple[str, ...]
    capability_floors: dict[str, CapabilityFloor]


def _read_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model dispatch policy is missing: {path}",
        ) from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model dispatch policy is unreadable: {path}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", "model dispatch policy must be an object.")
    return raw


def _non_empty_strings(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or any(not isinstance(item, str) or not item for item in value):
        raise DispatchPolicyError("dispatch_policy_invalid", f"{field} must be a non-empty string list.")
    return tuple(value)


def _read_profile(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model profile is unreadable: {path}: {exc}",
        ) from exc
    if not isinstance(raw, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", f"model profile must be an object: {path}")
    return raw


def _catalog_profile_pairs(raw: dict[str, Any]) -> set[ModelPair]:
    runtime, models = raw.get("runtime"), raw.get("models")
    if not isinstance(runtime, str) or not isinstance(models, list):
        return set()
    return {
        (runtime, model_id)
        for row in models
        if isinstance(row, dict) and isinstance((model_id := row.get("canonical_model_id")), str)
    }


def _cost_profile_pairs(raw: dict[str, Any]) -> set[ModelPair]:
    profiles = raw.get("profiles")
    if not isinstance(profiles, list):
        return set()
    return {
        (runtime, model)
        for row in profiles
        if isinstance(row, dict)
        and isinstance((runtime := row.get("runtime")), str)
        and isinstance((model := row.get("model")), str)
    }


def _profile_pairs() -> set[ModelPair]:
    paths = sorted(_PROFILE_ROOT.glob("*.v1.json"))
    if not paths:
        raise DispatchPolicyError(
            "dispatch_policy_unavailable", f"model profile directory is empty: {_PROFILE_ROOT}",
        )
    pairs: set[ModelPair] = set()
    for path in paths:
        raw = _read_profile(path)
        pairs.update(_catalog_profile_pairs(raw))
        pairs.update(_cost_profile_pairs(raw))
    return pairs


def _policy_sections(raw: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], str]:
    if raw.get("schema_version") != 1 or not isinstance(raw.get("policy_version"), str):
        raise DispatchPolicyError("dispatch_policy_invalid", "policy schema_version=1 and policy_version are required.")
    kinds = raw.get("dispatch_kinds")
    orchestrator = raw.get("orchestrator")
    if not isinstance(kinds, dict) or set(kinds) != set(_DISPATCH_KINDS) or not isinstance(orchestrator, dict):
        raise DispatchPolicyError("dispatch_policy_invalid", "policy must declare exactly the supported dispatch kinds and orchestrator.")
    return kinds, orchestrator, str(raw["policy_version"])


def _parse_floor_pairs(tag: str, raw_pairs: object, profiles: set[ModelPair]) -> tuple[ModelPair, ...]:
    if not isinstance(raw_pairs, list) or not raw_pairs:
        raise DispatchPolicyError("dispatch_policy_invalid", f"capability_floors.{tag}.floor_pairs is required.")
    pairs: list[ModelPair] = []
    for entry in raw_pairs:
        if not isinstance(entry, dict) or set(entry) != {"agent_runtime", "model"}:
            raise DispatchPolicyError("dispatch_policy_invalid", f"capability_floors.{tag} has malformed floor_pairs.")
        runtime, model = entry.get("agent_runtime"), entry.get("model")
        if not isinstance(runtime, str) or not isinstance(model, str) or (runtime, model) not in profiles:
            raise DispatchPolicyError(
                "dispatch_policy_invalid",
                f"capability_floors.{tag} names unknown model pair ({runtime!r}, {model!r}); refresh model_profiles.",
            )
        pairs.append((runtime, model))
    if len(set(pairs)) != len(pairs):
        raise DispatchPolicyError("dispatch_policy_invalid", f"capability_floors.{tag} repeats a floor pair.")
    return tuple(pairs)


def _parse_capability_floor(tag: str, raw_floor: object, profiles: set[ModelPair]) -> CapabilityFloor:
    if not isinstance(raw_floor, dict) or set(raw_floor) != set(_FLOOR_KEYS):
        raise DispatchPolicyError(
            "dispatch_policy_invalid",
            f"capability_floors.{tag} must contain exactly {sorted(_FLOOR_KEYS)}.",
        )
    reason = raw_floor.get("reason")
    if not isinstance(reason, str) or not reason:
        raise DispatchPolicyError("dispatch_policy_invalid", f"capability_floors.{tag}.reason must be a non-empty string.")
    return CapabilityFloor(
        tag=tag,
        issue_ids=_non_empty_strings(raw_floor.get("issue_ids"), f"capability_floors.{tag}.issue_ids"),
        reason=reason,
        brief_markers=_non_empty_strings(raw_floor.get("brief_markers"), f"capability_floors.{tag}.brief_markers"),
        floor_pairs=_parse_floor_pairs(tag, raw_floor.get("floor_pairs"), profiles),
    )


def _parse_capability_floors(raw_floors: object, profiles: set[ModelPair]) -> dict[str, CapabilityFloor]:
    if not isinstance(raw_floors, dict) or not raw_floors:
        raise DispatchPolicyError("dispatch_policy_invalid", "policy must declare a non-empty capability_floors object.")
    missing = _REQUIRED_CAPABILITY_FLOORS - set(raw_floors)
    if missing:
        raise DispatchPolicyError(
            "dispatch_policy_invalid", f"capability_floors is missing required floor(s): {sorted(missing)}.",
        )
    floors: dict[str, CapabilityFloor] = {}
    for tag, raw_floor in raw_floors.items():
        if not isinstance(tag, str) or not tag:
            raise DispatchPolicyError("dispatch_policy_invalid", "capability_floors keys must be non-empty strings.")
        floors[tag] = _parse_capability_floor(tag, raw_floor, profiles)
    return floors


def _validate_dispatch_kinds(kinds: dict[str, Any]) -> None:
    if any(not isinstance(rule, dict) or rule for rule in kinds.values()):
        raise DispatchPolicyError(
            "dispatch_policy_invalid",
            "dispatch_kinds are provenance labels only and must be empty objects.",
        )


def _parse_orchestrator(orchestrator: dict[str, Any], profiles: set[ModelPair]) -> tuple[tuple[str, ...], tuple[str, ...]]:
    role_names = _non_empty_strings(orchestrator.get("role_names"), "orchestrator.role_names")
    models = _non_empty_strings(orchestrator.get("allowed_models"), "orchestrator.allowed_models")
    if any(("claude_code", model) not in profiles for model in models):
        raise DispatchPolicyError("dispatch_policy_invalid", "orchestrator names a model absent from claude_code profiles.")
    return role_names, models


def load_dispatch_policy() -> DispatchPolicy:
    """Load and validate the current policy and every named profile pair."""
    raw = _read_json(_POLICY_PATH)
    kinds, orchestrator, policy_version = _policy_sections(raw)
    profiles = _profile_pairs()
    _validate_dispatch_kinds(kinds)
    role_names, models = _parse_orchestrator(orchestrator, profiles)
    return DispatchPolicy(
        policy_version=policy_version,
        orchestrator_role_names=frozenset(role_names),
        orchestrator_models=models,
        capability_floors=_parse_capability_floors(raw.get("capability_floors"), profiles),
    )


def _validate_dispatch_kind(dispatch_kind: str) -> None:
    if not dispatch_kind:
        raise DispatchPolicyError("dispatch_kind_required", "spawn_session requires dispatch_kind.")
    if dispatch_kind not in _DISPATCH_KINDS:
        raise DispatchPolicyError("dispatch_policy_violation", f"unknown dispatch_kind {dispatch_kind!r}.")


def _validate_scope_tags(policy: DispatchPolicy, scope_tags: Iterable[str]) -> tuple[str, ...]:
    tags = tuple(scope_tags)
    if any(not tag for tag in tags):
        raise DispatchPolicyError("scope_tag_invalid", "scope_tags must be non-empty strings.")
    unknown = sorted(set(tags) - set(policy.capability_floors))
    if unknown:
        raise DispatchPolicyError(
            "scope_tag_unknown",
            f"scope_tags {unknown} are not capability floors declared by the policy "
            f"(known: {sorted(policy.capability_floors)}).",
        )
    return tags


def applied_capability_floors(
    policy: DispatchPolicy, *, scope_tags: Iterable[str], brief_text: str,
) -> tuple[AppliedFloor, ...]:
    """Every floor this spawn triggers: declared by the caller OR detected in its brief.

    Detection is the half the caller cannot opt out of: a brief that names a
    schema-touching identifier floors the spawn whether or not the dispatcher
    remembered (or chose) to declare the tag. Declared tags are honoured even
    when the brief is silent, so a truthful declaration never under-floors.
    """
    declared = set(_validate_scope_tags(policy, scope_tags))
    applied: list[AppliedFloor] = []
    for tag, floor in sorted(policy.capability_floors.items()):
        if tag in declared:
            applied.append(AppliedFloor(tag=tag, source=_FLOOR_SOURCE_DECLARED, detail="scope_tags"))
            continue
        marker = floor.marker_in(brief_text) if brief_text else None
        if marker is not None:
            applied.append(AppliedFloor(tag=tag, source=_FLOOR_SOURCE_BRIEF, detail=marker))
    return tuple(applied)


def _validate_capability_floors(
    policy: DispatchPolicy, applied: tuple[AppliedFloor, ...], dispatch_kind: str, agent_runtime: str, model: str,
) -> None:
    """Refuse a pair below any applied floor. Runs BEFORE, and independently of, the kind rule.

    This function never consults ``allow_any_kinds``: a floor is a property of
    the work's declared scope, not of the dispatch_kind a caller picked, so
    ``dispatch_kind=infrastructure`` cannot lower it (the exact loophole in
    iss_da9e5e67 / iev_1850901f recommendation 3).
    """
    for entry in applied:
        floor = policy.capability_floors[entry.tag]
        if (agent_runtime, model) in floor.floor_pairs:
            continue
        rendered = ", ".join(f"({runtime}, {floor_model})" for runtime, floor_model in floor.floor_pairs)
        raise DispatchPolicyError(
            "capability_floor_violation",
            f"capability floor {entry.tag!r} applies ({entry.source}: {entry.detail}) and outranks "
            f"dispatch_kind={dispatch_kind!r}; received ({agent_runtime}, {model}); floor pairs: {rendered}. "
            f"Issues: {', '.join(floor.issue_ids)}.",
        )


def validate_spawn_dispatch(
    *, dispatch_kind: str, agent_runtime: str, model: str,
    reviewed_report_vendor: str, pair_id: str,
    scope_tags: Iterable[str] = (), brief_text: str = "",
) -> tuple[AppliedFloor, ...]:
    """Raise a named refusal unless one spawn satisfies the policy; return the floors applied.

    Order is load-bearing: capability floors are checked first and never
    short-circuited by a dispatch kind. ``scope_tags`` is the
    caller's declared scope; ``brief_text`` is the workbench brief when the
    caller has one (the detection half -- see ``applied_capability_floors``).

    ``reviewed_report_vendor`` and ``pair_id`` remain optional provenance only;
    neither may constrain the cheapest capability-clearing answer.
    """
    del reviewed_report_vendor, pair_id
    _validate_dispatch_kind(dispatch_kind)
    policy = load_dispatch_policy()
    applied = applied_capability_floors(policy, scope_tags=scope_tags, brief_text=brief_text)
    _validate_capability_floors(policy, applied, dispatch_kind, agent_runtime, model)
    return applied


def orchestrator_model_verdict(*, role_label: str, model: str) -> tuple[bool, tuple[str, ...]]:
    """Return whether a policy-governed seat has an allowed transcript model."""
    policy = load_dispatch_policy()
    role_name = role_label.rsplit("-", maxsplit=1)[-1]
    if role_label not in policy.orchestrator_role_names and role_name not in policy.orchestrator_role_names:
        return True, policy.orchestrator_models
    return model in policy.orchestrator_models, policy.orchestrator_models


__all__ = [
    "AppliedFloor",
    "DispatchPolicyError",
    "CapabilityFloor",
    "DispatchPolicy",
    "applied_capability_floors",
    "load_dispatch_policy",
    "orchestrator_model_verdict",
    "validate_spawn_dispatch",
]
