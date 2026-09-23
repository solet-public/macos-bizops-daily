"""Typed, non-secret decision facts needed by target-local aggregate probes."""

from __future__ import annotations

from pathlib import Path
from typing import cast

from .answer_validation import validate_decision_selection
from .contracts import ContractBundle, active_decision_ids, target_contract_directory
from .errors import StateConflictError
from .models import JsonValue
from .transaction import Transaction

_AUTOSTART_PROBE_REFS = frozenset(
    {
        "genesis::solet.verify",
        "genesis::autostart.verify",
    }
)
_CODING_AGENT_PROBE_REFS = frozenset(
    {
        "setup::coding_agents.verify_plugins",
        "setup::coding_agents.verify_hooks",
        "plugin::agent_messaging_plugin.peer_identity",
    }
)
_CODING_AGENT_OPTIONS = frozenset({"codex", "claude_code"})
_EMBEDDING_MODEL_QUALIFICATION_PROBE = "setup::models.qualify_embedding"
_STRUCTURED_ACTION_QUALIFICATION_PROBE = "setup::models.qualify_structured_actions"
_REPRESENTATIVE_INFERENCE_PROBE = "setup::models.qualify_representative_inference"


def probe_public_inputs(
    transaction: Transaction,
    probe_ref: str,
) -> dict[str, JsonValue]:
    """Project only the resolved facts the declared probe is entitled to use."""

    if probe_ref in _AUTOSTART_PROBE_REFS:
        return {"autostart": _autostart_decision(transaction)}
    if probe_ref in _CODING_AGENT_PROBE_REFS:
        return {"selected_coding_agents": list(_coding_agent_selection(transaction))}
    if probe_ref == _EMBEDDING_MODEL_QUALIFICATION_PROBE:
        return {"candidate_id": _embedding_model_selection(transaction)}
    if probe_ref in {
        _STRUCTURED_ACTION_QUALIFICATION_PROBE,
        _REPRESENTATIVE_INFERENCE_PROBE,
    }:
        return {"candidate_id": _inference_model_selection(transaction)}
    if probe_ref in _LM_STUDIO_PROBES:
        return _lm_studio_inputs(transaction)
    return {}


_LM_STUDIO_PROBES = frozenset(
    f"setup::lm_studio.{suffix}" for suffix in (
        "cli_available", "server_ready", "embedding_artifact_present", "embedding_model_served",
        "inference_artifact_present", "inference_model_served", "login_agent_valid", "jit_disabled",
    )
)


def probe_recorded_input_refs(probe_ref: str) -> frozenset[str]:
    """Normalized public-input fields consumed by this declared probe route.

    Other routes project decisions, not retained public-input fields.
    Keep this aligned with the projections below and their closure smoke.
    """

    return frozenset({"lm_studio_base_url"}) if probe_ref in _LM_STUDIO_PROBES else frozenset()


def _lm_studio_inputs(transaction: Transaction) -> dict[str, JsonValue]:
    decisions = _decisions(transaction)
    inputs = transaction.answers.get("public_inputs")
    if not isinstance(inputs, dict):
        raise StateConflictError("LM Studio public inputs are unavailable")
    carriers: tuple[str, ...] = ("embeddings_implementation", "inference_implementation")
    missing = frozenset(carriers) - decisions.keys()
    if not missing:
        _validate_explicit_lm_studio_tuple(decisions, carriers)
    inactive = _omitted_inactive_carriers(transaction, decisions, missing)
    projected = {key: "none" if key in inactive else decisions.get(key) for key in carriers}
    projected["lm_studio_base_url"] = inputs.get("lm_studio_base_url")
    if any(not isinstance(value, str) or not value for value in projected.values()):
        raise StateConflictError("LM Studio implementation or loopback URL is unresolved")
    return projected


def _validate_explicit_lm_studio_tuple(
    decisions: dict[str, JsonValue],
    carriers: tuple[str, ...],
) -> None:
    """Reject a resolved vacancy unless its explicit profile selection is free."""

    if any(
        not isinstance(decisions[carrier], str) or not decisions[carrier]
        for carrier in carriers
    ):
        raise StateConflictError("LM Studio implementation is unresolved")
    if decisions["inference_implementation"] != "none":
        return
    profile = decisions.get("setup_profile")
    if not isinstance(profile, str) or not profile:
        raise StateConflictError("inference vacancy requires an explicit setup profile")
    if profile != "free":
        raise StateConflictError("nonfree inference implementation cannot be none")


def _omitted_inactive_carriers(
    transaction: Transaction,
    decisions: dict[str, JsonValue],
    missing: frozenset[str],
) -> frozenset[str]:
    """Prove absence is inactive from the pinned flow, never from a profile name."""
    if not missing:
        return frozenset()
    bundle = ContractBundle.load(
        source_revision=transaction.flow_source_revision,
        directory=target_contract_directory(Path(transaction.target)),
        expected_digest=transaction.flow_contract_digest,
        resume_compatibility=True,
    )
    validate_decision_selection(
        "setup_profile",
        decisions.get("setup_profile"),
        bundle.decisions["setup_profile"],
        decisions,
    )
    if missing & active_decision_ids(bundle, decisions):
        raise StateConflictError("LM Studio active implementation is unresolved")
    return missing


def _decisions(transaction: Transaction) -> dict[str, JsonValue]:
    decisions = transaction.answers.get("decisions")
    if not isinstance(decisions, dict):
        raise StateConflictError("transaction decisions are not a typed object")
    return decisions


def _autostart_decision(transaction: Transaction) -> str:
    value = _decisions(transaction).get("autostart")
    if value not in {"enabled", "disabled"}:
        raise StateConflictError("transaction autostart decision is unresolved or invalid")
    return str(value)


def _coding_agent_selection(transaction: Transaction) -> tuple[str, ...]:
    value = _decisions(transaction).get("coding_agents")
    if (
        not isinstance(value, list)
        or not value
        or not all(isinstance(item, str) and item in _CODING_AGENT_OPTIONS for item in value)
        or len(value) != len(set(value))
    ):
        raise StateConflictError("transaction coding-agent selection is unresolved or invalid")
    return tuple(cast(str, item) for item in value)


def _inference_model_selection(transaction: Transaction) -> str:
    value = _decisions(transaction).get("inference_model")
    if not isinstance(value, str) or not value:
        raise StateConflictError("transaction inference-model selection is unresolved or invalid")
    return value


def _embedding_model_selection(transaction: Transaction) -> str:
    value = _decisions(transaction).get("embedding_model")
    if not isinstance(value, str) or not value:
        raise StateConflictError("transaction embedding-model selection is unresolved or invalid")
    return value
