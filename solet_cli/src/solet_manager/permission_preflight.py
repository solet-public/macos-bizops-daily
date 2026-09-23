"""Resolved permission pre-flight rendering from the generated M-2 manifest."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import cast

from .condition_evaluator import condition_is_inactive, condition_refs
from .contracts import PERMISSIONS_MANIFEST_FILENAME, ContractBundle
from .errors import ContractError
from .models import JsonValue
from .plan_builder import SetupPlan

_SETTINGS_URLS = {
    "General > Login Items & Extensions": (
        "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"
    ),
    "Privacy & Security > Files and Folders": (
        "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    ),
}
_SESSION_ROOT_PERMISSION_IDS = frozenset(
    {"codex_session_files_permission", "claude_session_files_permission"}
)


def render_permission_preflight(
    bundle: ContractBundle,
    plan: SetupPlan,
    stage_observations: Mapping[str, JsonValue],
) -> dict[str, JsonValue]:
    """Return the one review surface for the resolved trust boundary.

    The generated manifest remains the presentation authority.  Existing plan
    answers select its entries; this function does not discover or alter a
    decision.  Probe observations only classify a remediation already needed.
    """

    entries = _load_manifest_entries(bundle.permission_manifest)
    decisions = _decisions(plan.answers)
    selected_auth_flows = _selected_auth_flow_ids(bundle, decisions)
    selected_consents = _selected_consent_ids(plan.answers)
    observations = _observations_by_probe(stage_observations)
    display_attached = display_session_attached()
    items: list[JsonValue] = []
    for entry in entries:
        if _entry_is_resolved(entry, bundle, decisions, selected_auth_flows, selected_consents):
            items.append(
                _render_entry(
                    entry,
                    bundle,
                    decisions,
                    selected_auth_flows,
                    selected_consents,
                    observations,
                    display_attached,
                    resolution_state=_entry_resolution_state(entry, bundle, decisions),
                )
            )
    return {
        "manifest": PERMISSIONS_MANIFEST_FILENAME,
        "display_session_attached": display_attached,
        "headless_guidance": (
            None
            if display_attached
            else (
                "No attached GUI display was detected. Before following a macOS Settings "
                "item, connect with Screen Sharing after `launchctl kickstart -k "
                "system/com.apple.screensharing` and use port 5900. Each Settings pane "
                "opens on that attached GUI session's primary display."
            )
        ),
        "items": items,
    }


def permission_preflight_fingerprint_content(
    preflight: Mapping[str, JsonValue],
) -> list[JsonValue]:
    """Return the stable semantic permission subset approved by the operator.

    The reviewer-facing rendering carries display URLs and prose that may vary
    with an attached GUI session.  Approval instead binds just each rendered
    item's identity, remediation cause class, and condition resolution state.
    This consumes the already-rendered list so the display and fingerprint
    cannot select different permission subsets.
    """

    raw_items = preflight.get("items")
    if not isinstance(raw_items, list):
        raise ContractError("permission pre-flight items are invalid for fingerprinting")
    content: list[JsonValue] = []
    for item in raw_items:
        if not isinstance(item, dict):
            raise ContractError("permission pre-flight item is invalid for fingerprinting")
        permission_id = item.get("id")
        remediation = item.get("remediation")
        resolution_state = item.get("resolution_state")
        cause_class = remediation.get("cause") if isinstance(remediation, dict) else None
        semantic_values = (permission_id, cause_class, resolution_state)
        if not all(isinstance(value, str) for value in semantic_values):
            raise ContractError("permission pre-flight semantic content is invalid")
        content.append(
            {
                "permission_id": str(permission_id),
                "cause_class": str(cause_class),
                "resolution_state": str(resolution_state),
            }
        )
    return sorted(
        content,
        key=lambda item: str(cast(dict[str, JsonValue], item)["permission_id"]),
    )


def display_session_attached() -> bool:
    """Detect the current user's macOS GUI launch domain without opening a pane."""

    launchctl = Path("/bin/launchctl")
    if not launchctl.is_file():
        return False
    completed = subprocess.run(
        (str(launchctl), "print", f"gui/{os.getuid()}"),
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=2,
    )
    return completed.returncode == 0


def _load_manifest_entries(raw: Mapping[str, JsonValue]) -> list[dict[str, JsonValue]]:
    if raw.get("manifest_schema_version") != 1:
        raise ContractError("permission manifest has an unsupported schema version")
    entries = raw.get("entries")
    if not isinstance(entries, list):
        raise ContractError("permission manifest entries must be an array")
    parsed: list[dict[str, JsonValue]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ContractError("permission manifest entry must be an object")
        entry_id = entry.get("id")
        entry_type = entry.get("entry_type")
        source = entry.get("source")
        if (
            not isinstance(entry_id, str)
            or entry_type not in {"permission", "consent", "auth_flow"}
            or not isinstance(source, dict)
        ):
            raise ContractError("permission manifest entry has an invalid identity or source")
        parsed.append(entry)
    return parsed


def _decisions(answers: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    raw = answers.get("decisions")
    if not isinstance(raw, dict):
        raise ContractError("normalized answers lack resolved decisions")
    return raw


def _selected_consent_ids(answers: Mapping[str, JsonValue]) -> set[str]:
    raw = answers.get("consents")
    if not isinstance(raw, dict):
        raise ContractError("normalized answers lack consent states")
    return set(raw)


def _selected_auth_flow_ids(
    bundle: ContractBundle,
    decisions: Mapping[str, JsonValue],
) -> set[str]:
    selected: set[str] = set()
    for decision_id, value in decisions.items():
        definition = bundle.decisions.get(decision_id)
        if definition is not None:
            selected.update(_option_auth_flow_refs(definition, value))
    return selected


def _option_auth_flow_refs(definition: Mapping[str, JsonValue], value: JsonValue) -> set[str]:
    source = definition.get("option_source")
    options = source.get("options") if isinstance(source, dict) else None
    if not isinstance(options, dict):
        return set()
    selected: set[str] = set()
    selected_values = value if isinstance(value, list) else [value]
    for option_id in selected_values:
        selected.update(_auth_flow_refs_for_option(options.get(str(option_id))))
    return selected


def _auth_flow_refs_for_option(option: JsonValue) -> set[str]:
    if not isinstance(option, dict):
        return set()
    activates = option.get("activates")
    if not isinstance(activates, dict):
        return set()
    refs = activates.get("auth_flow_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) for ref in refs):
        return set()
    return {str(ref) for ref in refs}


def _entry_is_resolved(
    entry: Mapping[str, JsonValue],
    bundle: ContractBundle,
    decisions: Mapping[str, JsonValue],
    selected_auth_flows: set[str],
    selected_consents: set[str],
) -> bool:
    entry_id = str(entry["id"])
    entry_type = str(entry["entry_type"])
    if entry_type == "auth_flow":
        return entry_id in selected_auth_flows
    if entry_type == "consent":
        return entry_id in selected_consents
    source = _source(entry)
    condition = source.get("required_when")
    if condition is not None:
        return not condition_is_inactive(condition, dict(decisions))
    probe_ref = source.get("probe_ref")
    if not isinstance(probe_ref, str):
        raise ContractError(f"permission manifest entry {entry_id!r} lacks probe_ref")
    probe = bundle.probes.get(probe_ref)
    if probe is None:
        raise ContractError(f"permission manifest entry {entry_id!r} has unknown probe")
    probe_condition = probe.get("required_when")
    return probe_condition is None or not condition_is_inactive(
        probe_condition, dict(decisions)
    )


def _entry_resolution_state(
    entry: Mapping[str, JsonValue],
    bundle: ContractBundle,
    decisions: Mapping[str, JsonValue],
) -> str:
    """Classify a rendered item without adding volatile display detail."""

    condition = _entry_condition(entry, bundle)
    if condition is None:
        return "active"
    if any(decision_id not in decisions for decision_id in condition_refs(condition)):
        return "awaiting_decision"
    return "active"


def _entry_condition(
    entry: Mapping[str, JsonValue],
    bundle: ContractBundle,
) -> JsonValue | None:
    if str(entry["entry_type"]) != "permission":
        return None
    source = _source(entry)
    condition = source.get("required_when")
    if condition is not None:
        return condition
    probe_ref = source.get("probe_ref")
    if not isinstance(probe_ref, str):
        raise ContractError(f"permission manifest entry {entry['id']!r} lacks probe_ref")
    probe = bundle.probes.get(probe_ref)
    if probe is None:
        raise ContractError(f"permission manifest entry {entry['id']!r} has unknown probe")
    return probe.get("required_when")


def _render_entry(
    entry: Mapping[str, JsonValue],
    bundle: ContractBundle,
    decisions: Mapping[str, JsonValue],
    selected_auth_flows: set[str],
    selected_consents: set[str],
    observations: Mapping[str, dict[str, JsonValue]],
    display_attached: bool,
    *,
    resolution_state: str,
) -> dict[str, JsonValue]:
    del bundle, decisions, selected_auth_flows, selected_consents
    entry_id = str(entry["id"])
    source = _source(entry)
    probe_ref = source.get("probe_ref")
    observation = observations.get(probe_ref) if isinstance(probe_ref, str) else None
    remediation = _remediation(entry_id, entry, observation, display_attached)
    return {
        "id": entry_id,
        "entry_type": entry["entry_type"],
        "what": entry["what"],
        "why": entry["why"],
        "denial_consequence": entry["denial_behavior"],
        "remediation": remediation,
        "resolution_state": resolution_state,
    }


def _source(entry: Mapping[str, JsonValue]) -> dict[str, JsonValue]:
    source = entry.get("source")
    if not isinstance(source, dict):
        raise ContractError("permission manifest entry lacks source")
    return source


def _observations_by_probe(
    stage_observations: Mapping[str, JsonValue],
) -> dict[str, dict[str, JsonValue]]:
    by_probe: dict[str, dict[str, JsonValue]] = {}
    for identity, observation in stage_observations.items():
        if not isinstance(observation, dict):
            continue
        _stage, _boundary, probe_id = identity.partition(":")
        if not probe_id or ":" not in identity:
            continue
        by_probe[probe_id.rsplit(":", 1)[-1]] = observation
    return by_probe


def _remediation(
    entry_id: str,
    entry: Mapping[str, JsonValue],
    observation: Mapping[str, JsonValue] | None,
    display_attached: bool,
) -> dict[str, JsonValue]:
    if _has_absent_root_evidence(observation):
        return {
            "cause": "posix_absent",
            "action": "Nothing to do; the selected agent CLI creates this root on first use.",
            "settings_url": None,
        }
    error_kind = None if observation is None else observation.get("error_kind")
    if entry_id in _SESSION_ROOT_PERMISSION_IDS and isinstance(error_kind, str) and error_kind.endswith(
        "_session_roots_unreadable"
    ):
        return _settings_remediation(entry, display_attached)
    repair = None if observation is None else observation.get("repair")
    if isinstance(repair, str) and repair:
        return {"cause": "subsystem", "action": repair, "settings_url": None}
    return {
        "cause": "not_currently_blocked",
        "action": "No remediation is currently required; the install checkpoint remains the enforcement point.",
        "settings_url": None,
    }


def _has_absent_root_evidence(observation: Mapping[str, JsonValue] | None) -> bool:
    if observation is None:
        return False
    evidence = observation.get("evidence")
    if not isinstance(evidence, list):
        return False
    return any(
        isinstance(item, dict)
        and isinstance((evidence_id := item.get("id")), str)
        and evidence_id.endswith("_session_roots_absent")
        for item in evidence
    )


def _settings_remediation(
    entry: Mapping[str, JsonValue], display_attached: bool
) -> dict[str, JsonValue]:
    path = entry.get("system_settings_pane")
    url = _SETTINGS_URLS.get(path) if isinstance(path, str) else None
    if url is None:
        raise ContractError("Settings-grant pre-flight entry has no declared deep-link mapping")
    return {
        "cause": "tcc_settings_grant",
        "action": (
            f"Open {path} on the current GUI display and grant the listed Files and Folders access."
            if display_attached
            else f"Attach a GUI display first, then open {path} on that display and grant access."
        ),
        "settings_url": url if display_attached else None,
    }
