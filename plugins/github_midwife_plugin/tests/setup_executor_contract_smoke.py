"""Validate the frozen answer, journal, and adapter contracts.

Run directly::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/setup_executor_contract_smoke.py
"""

from __future__ import annotations

import copy
import json
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft7Validator
from referencing import Registry, Resource

_ROOT = Path(__file__).resolve().parents[1] / "knowledge_base"
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _load(name: str) -> dict[str, Any]:
    value = json.loads((_ROOT / name).read_text(encoding="utf-8"))
    _check(isinstance(value, dict), f"{name}: object")
    return value


def _valid_answers() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "name": "bizops",
        "target": "/Users/example/Solets/bizops",
        "public_inputs": {
            "repository_ref": "release-2026-08-20",
            "solet_name": "bizops",
        },
        "decisions": {"setup_profile": "macos-bizops", "autostart": "enabled"},
        "consents": {"system_change_consent": True},
        "resolution_evidence": [{"id": "setup_profile", "source": "seed_lock", "summary": "macos-bizops"}],
    }


def main() -> int:
    flow = _load("macos_setup_flow.json")
    answer_schema = _load("setup_answers.schema.json")
    journal_schema = _load("setup_journal.schema.json")
    adapter_schema = _load("setup_adapter_envelope.schema.json")
    for name, schema in (
        ("answers", answer_schema),
        ("journal", journal_schema),
        ("adapter", adapter_schema),
    ):
        Draft7Validator.check_schema(schema)
        _check(schema["additionalProperties"] is False, f"{name}: root is closed")

    links = flow["executor_contracts"]
    _check(links["answers_schema"] == "setup_answers.schema.json", "flow links answers")
    _check(links["journal_schema"] == "setup_journal.schema.json", "flow links journal")
    _check(
        links["adapter_envelope_schema"] == "setup_adapter_envelope.schema.json",
        "flow links adapter",
    )
    _check(links["stop_command"] == "deferred", "stop is explicitly deferred")
    _check(
        links["start_command"]
        == {
            "operation_ref": "lifecycle::start",
            "runner": "hydration",
            "timeout_seconds": 120,
            "startup_readiness": {
                "contract_version": 1,
                "budget_source": (
                    "executor_contracts.start_command.timeout_seconds"
                ),
                "budget_unit": "seconds",
                "semantic_scope": (
                    "target_start_through_target_cli_health_status_healthy"
                ),
                "release_signal": (
                    "target_cli_health_top_level_status_healthy"
                ),
                "consumer_probe_purposes": ["stage_exit", "completion"],
                "consumer_probe_refs": ["embedding_request_succeeds"],
                "downstream_reservations": {
                    "governed_process_call_seconds": 30
                },
            },
            "postcondition_probe_refs": ["router_ready", "peer_identity_valid"],
        },
        "flow declares one closed lifecycle start and identity postconditions",
    )
    statuses = flow["state_policy"]["statuses"]
    _check(len(statuses) == 10 and len(set(statuses)) == 10, "full status vocabulary")

    answer_validator = Draft7Validator(answer_schema)
    answers = _valid_answers()
    _check(not list(answer_validator.iter_errors(answers)), "valid normalized answers")
    secret = copy.deepcopy(answers)
    secret["public_inputs"]["jira_api_token"] = "forbidden"
    _check(bool(list(answer_validator.iter_errors(secret))), "secret-like public key rejected")
    unknown = copy.deepcopy(answers)
    unknown["unknown"] = True
    _check(bool(list(answer_validator.iter_errors(unknown))), "unknown answer field rejected")

    registry = Registry().with_resource(answer_schema["$id"], Resource.from_contents(answer_schema)).with_resource(adapter_schema["$id"], Resource.from_contents(adapter_schema))
    journal_validator = Draft7Validator(journal_schema, registry=registry)
    journal = {
        "schema_version": 1,
        "operation_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        "name": "bizops",
        "target": "/Users/example/Solets/bizops",
        "input_fingerprint": "sha256:" + "1" * 64,
        "answers": answers,
        "answers_fingerprint": "sha256:" + "2" * 64,
        "approval_fingerprint": None,
        "approval_recorded_at": None,
        "seed_repository": "https://github.com/solet-public/macos-bizops.git",
        "seed_tag": "release-2026-08-20",
        "seed_commit": "a" * 40,
        "seed_tree_hash": "b" * 40,
        "seed_archive_sha256": "c" * 64,
        "profile": "macos-bizops",
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "flow_contract_digest": "sha256:" + "d" * 64,
        "status": "pending",
        "stages": dict.fromkeys(flow["stages"], "pending"),
        "stage_probe_statuses": {
            stage_id: {
                "entry": dict.fromkeys(stage.get("entry_probe_refs", []), "pending"),
                "exit": dict.fromkeys(stage["exit_probe_refs"], "pending"),
            }
            for stage_id, stage in flow["stages"].items()
        },
        "stage_probe_attempts": [],
        "operation_stages": {"install_postgresql": "system_dependencies"},
        "operation_statuses": {"install_postgresql": "pending"},
        "operation_attempts": [],
        "evidence": [],
        "completion": dict.fromkeys(flow["completion"]["required_probe_refs"], "pending"),
        "result_kind": None,
        "created_at": "2026-08-21T03:30:00Z",
        "updated_at": "2026-08-21T03:30:00Z",
    }
    _check(not list(journal_validator.iter_errors(journal)), "valid transaction journal")
    bad_status = copy.deepcopy(journal)
    bad_status["stages"]["preflight"] = "success"
    _check(bool(list(journal_validator.iter_errors(bad_status))), "noncanonical status rejected")
    missing_attempt_state = copy.deepcopy(journal)
    del missing_attempt_state["operation_statuses"]
    _check(
        bool(list(journal_validator.iter_errors(missing_attempt_state))),
        "journal operation state is required",
    )
    unknown_attempt = copy.deepcopy(journal)
    unknown_attempt["operation_attempts"] = [
        {
            "operation_id": "install_postgresql",
            "stage_id": "system_dependencies",
            "phase": "pre_probe",
            "attempt": 1,
            "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
            "checkpoint_status": "pending",
            "error_kind": None,
            "retry_safe": True,
            "exit_code": 0,
            "timed_out": False,
            "duration_ms": 0,
            "planned_actions": [],
            "evidence": [],
            "reason": None,
            "repair": None,
            "recorded_at": "2026-08-21T03:30:00Z",
            "unknown": True,
        }
    ]
    _check(bool(list(journal_validator.iter_errors(unknown_attempt))), "journal attempt is closed")

    adapter_validator = Draft7Validator(adapter_schema)
    request = {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": "8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        "operation_id": "install_postgresql",
        "operation_ref": "setup::postgresql.install",
        "phase": "probe",
        "probe_purpose": "preview",
        "attempt": 1,
        "name": "bizops",
        "target": "/Users/example/Solets/bizops",
        "flow_id": "macos.repository_setup",
        "flow_source_revision": "a" * 40,
        "answers_fingerprint": "sha256:" + "2" * 64,
        "approval_fingerprint": None,
        "dry_run": True,
        "timeout_seconds": 30,
        "public_inputs": {"solet_name": "bizops"},
    }
    _check(not list(adapter_validator.iter_errors(request)), "valid adapter request")
    result = {
        "protocol_version": 1,
        "kind": "operation_result",
        "request_id": request["request_id"],
        "operation_id": request["operation_id"],
        "phase": "probe",
        "probe_purpose": "preview",
        "checkpoint_status": "verified",
        "error_kind": None,
        "retry_safe": True,
        "exit_code": 0,
        "timed_out": False,
        "duration_ms": 3,
        "stdout": "",
        "stderr": "",
        "planned_actions": [
            {
                "id": "postgres.start_service",
                "title": "Start PostgreSQL service",
                "mutation_kind": "service_start",
                "target": "homebrew:postgresql",
                "requires_confirmation": True,
                "condition_or_evidence_ref": "postgres_service_not_running",
            }
        ],
        "discovered_candidates": [],
        "evidence": [],
        "reason": None,
        "repair": None,
    }
    _check(not list(adapter_validator.iter_errors(result)), "valid adapter result")
    request_secret = copy.deepcopy(request)
    request_secret["public_inputs"]["password"] = "forbidden"
    _check(bool(list(adapter_validator.iter_errors(request_secret))), "adapter secret key rejected")
    request_unknown = copy.deepcopy(request)
    request_unknown["command"] = "curl | sh"
    _check(bool(list(adapter_validator.iter_errors(request_unknown))), "adapter command field rejected")
    bad_action_id = copy.deepcopy(result)
    bad_action_id["planned_actions"][0]["id"] = "Invalid ID"
    _check(
        bool(list(adapter_validator.iter_errors(bad_action_id))),
        "planned action id grammar enforced",
    )
    unknown_action = copy.deepcopy(result)
    unknown_action["planned_actions"][0]["unknown"] = True
    _check(bool(list(adapter_validator.iter_errors(unknown_action))), "planned action is closed")
    bad_phase_pair = copy.deepcopy(request)
    bad_phase_pair["phase"] = "apply"
    _check(bool(list(adapter_validator.iter_errors(bad_phase_pair))), "apply purpose must be null")
    apply_request = copy.deepcopy(request)
    apply_request.update(
        {
            "phase": "apply",
            "probe_purpose": None,
            "approval_fingerprint": "sha256:" + "4" * 64,
            "dry_run": False,
        }
    )
    _check(not list(adapter_validator.iter_errors(apply_request)), "valid apply request")
    apply_result = copy.deepcopy(result)
    apply_result.update(
        {
            "phase": "apply",
            "probe_purpose": None,
            "checkpoint_status": "applied",
            "planned_actions": [],
        }
    )
    _check(not list(adapter_validator.iter_errors(apply_result)), "valid non-verifying apply result")
    apply_owns_verification = copy.deepcopy(apply_result)
    apply_owns_verification["checkpoint_status"] = "verified"
    _check(
        bool(list(adapter_validator.iter_errors(apply_owns_verification))),
        "apply result cannot own verification",
    )
    apply_with_action = copy.deepcopy(apply_result)
    apply_with_action["planned_actions"] = copy.deepcopy(result["planned_actions"])
    _check(
        bool(list(adapter_validator.iter_errors(apply_with_action))),
        "apply result planned_actions must be empty",
    )
    post_result = copy.deepcopy(result)
    post_result["probe_purpose"] = "post_apply"
    post_result["planned_actions"] = []
    _check(not list(adapter_validator.iter_errors(post_result)), "valid post-probe result")
    post_with_action = copy.deepcopy(post_result)
    post_with_action["planned_actions"] = copy.deepcopy(result["planned_actions"])
    _check(
        bool(list(adapter_validator.iter_errors(post_with_action))),
        "post-probe planned_actions must be empty",
    )
    for purpose in ("stage_entry", "stage_exit"):
        stage_result = copy.deepcopy(post_result)
        stage_result["probe_purpose"] = purpose
        _check(
            not list(adapter_validator.iter_errors(stage_result)),
            f"valid {purpose} result",
        )
        stage_with_action = copy.deepcopy(stage_result)
        stage_with_action["planned_actions"] = copy.deepcopy(result["planned_actions"])
        _check(
            bool(list(adapter_validator.iter_errors(stage_with_action))),
            f"{purpose} planned_actions must be empty",
        )
        stage_with_candidate = copy.deepcopy(stage_result)
        stage_with_candidate["discovered_candidates"] = [
            {
                "decision_id": "embedding_model",
                "value": "model.recommended",
                "label": "Recommended model",
                "recommendation_rank": 0,
                "metadata": {},
            }
        ]
        _check(
            bool(list(adapter_validator.iter_errors(stage_with_candidate))),
            f"{purpose} candidates must be empty",
        )
    discovery = copy.deepcopy(result)
    discovery["probe_purpose"] = "decision_discovery"
    discovery["planned_actions"] = []
    discovery["discovered_candidates"] = [
        {
            "decision_id": "embedding_model",
            "value": "model.recommended",
            "label": "Recommended model",
            "recommendation_rank": 0,
            "metadata": {"provider": "lm_studio", "dimensions": 768},
        }
    ]
    _check(not list(adapter_validator.iter_errors(discovery)), "valid discovered candidate")
    bad_rank = copy.deepcopy(discovery)
    bad_rank["discovered_candidates"][0]["recommendation_rank"] = -1
    _check(bool(list(adapter_validator.iter_errors(bad_rank))), "candidate rank must be nonnegative")
    print(f"setup_executor_contract_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
