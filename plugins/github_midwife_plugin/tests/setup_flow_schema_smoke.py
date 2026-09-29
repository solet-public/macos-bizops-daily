"""Validate the decision-oriented setup-flow contract and macOS reference.

This smoke is intentionally more than a JSON parse check.  It proves that the
reference flow validates against the shipped Draft-07 schema, all typed
cross-references resolve, secret inputs cannot carry defaults, dynamic model
selection stays explicit, plugin authentication modes retain the reviewed
shape, and health alone cannot be reported as setup completion.

Run directly::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/setup_flow_schema_smoke.py
"""

from __future__ import annotations

import ast
import copy
import inspect
import json
import sys
import tempfile
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

from jsonschema import Draft7Validator

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT))
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "github_midwife_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "solet_cli" / "src"))

from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.installation_doctor import probe_handlers  # noqa: E402
from github_midwife_plugin.setup_adapter import _ALLOWED_PUBLIC_INPUTS  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_operations import _manual_connector, operation_handlers  # noqa: E402
from solet_manager.adapters import AdapterRegistry  # noqa: E402
from solet_manager.answer_validation import validate_normalized_answers  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.decision_discovery import _qualification_availability_error  # noqa: E402
from solet_manager.errors import ContractError  # noqa: E402
from solet_manager.operation_records import operation_request  # noqa: E402
from solet_manager.plan_builder import build_setup_plan  # noqa: E402
from solet_manager.probe_input_projection import probe_public_inputs  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

from bootstrap_adapter.routes import _ROUTES as _BOOTSTRAP_ROUTES  # noqa: E402
from bootstrap_adapter.routes import _dispatch as _bootstrap_dispatch  # noqa: E402

_KB_ROOT = Path(__file__).resolve().parents[1] / "knowledge_base"
_SCHEMA_PATH = _KB_ROOT / "setup_flow.schema.json"
_FLOW_PATH = _KB_ROOT / "macos_setup_flow.json"
_MANAGER_SOURCE_ROOT = _REPO_ROOT / "solet_cli" / "src"
_FIELD_KINDS_PATH = _KB_ROOT / "setup_flow_field_kinds.json"
_FIELD_KIND_ALLOWLIST_PATH = _REPO_ROOT / "quality_gates" / "setup_flow_field_kind_allowlist.json"
_HANDLER_FALLBACK_PUBLIC_INPUTS = {
    "setup::models.qualify_structured_actions": frozenset({"candidate_id"}),
    "setup::models.qualify_representative_inference": frozenset({"candidate_id"}),
}
_REQUIRED_OPERATION_PROJECTIONS = {
    **dict.fromkeys(
        (f"setup::lm_studio.{suffix}" for suffix in (
            "cli_available", "server_ready", "embedding_artifact_present", "embedding_model_served",
            "inference_artifact_present", "inference_model_served", "login_agent_valid", "jit_disabled",
        )),
        frozenset({"embeddings_implementation", "inference_implementation", "lm_studio_base_url"}),
    ),
    "genesis::solet.verify": frozenset({"autostart"}),
    "genesis::autostart.verify": frozenset({"autostart"}),
    "setup::models.qualify_embedding": frozenset({"candidate_id"}),
    "setup::models.qualify_structured_actions": frozenset({"candidate_id"}),
    "setup::models.qualify_representative_inference": frozenset({"candidate_id"}),
}

_SINGULAR_REF_REGISTRIES = {
    "component_ref": "components",
    "dependency_ref": "dependencies",
    "plugin_ref": "plugins",
    "input_ref": "inputs",
    "repository_ref_input": "inputs",
    "journal_location_ref": "inputs",
    "auth_flow_ref": "auth_flows",
    "consent_ref": "consents",
    "permission_ref": "permissions",
    "decision_ref": "decisions",
    "alternatives_decision_ref": "decisions",
    "resolution_stage_ref": "stages",
    "discovery_probe_ref": "probes",
    "validation_probe_ref": "probes",
    "grant_operation_ref": "operations",
    "rollback_operation_ref": "operations",
    "reminder_operation_ref": "operations",
    "remediation_operation_ref": "operations",
}
_PLURAL_REF_REGISTRIES = {
    "component_refs": "components",
    "dependency_refs": "dependencies",
    "plugin_refs": "plugins",
    "input_refs": "inputs",
    "secret_input_refs": "inputs",
    "public_input_refs": "inputs",
    "auth_flow_refs": "auth_flows",
    "consent_refs": "consents",
    "permission_refs": "permissions",
    "decision_refs": "decisions",
    "followup_decision_refs": "decisions",
    "operation_refs": "operations",
    "install_operation_refs": "operations",
    "configure_operation_refs": "operations",
    "setup_operation_refs": "operations",
    "start_operation_refs": "operations",
    "remediation_operation_refs": "operations",
    "probe_refs": "probes",
    "entry_probe_refs": "probes",
    "exit_probe_refs": "probes",
    "verification_probe_refs": "probes",
    "precondition_probe_refs": "probes",
    "postcondition_probe_refs": "probes",
    "qualification_probe_refs": "probes",
    "required_probe_refs": "probes",
    "stage_refs": "stages",
    "depends_on_stage_refs": "stages",
}

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on the first failed setup-contract check."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _load_json(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeFailureError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise SmokeFailureError(f"{path.name} did not parse to an object")
    return raw


def _load_list(path: Path) -> list[dict[str, str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SmokeFailureError(f"{path.name} is not valid JSON: {exc}") from exc
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise SmokeFailureError(f"{path.name} must contain an object list")
    return cast(list[dict[str, str]], raw)


def _walk(value: object, path: tuple[str | int, ...] = ()) -> Iterator[tuple[tuple[str | int, ...], object]]:
    yield path, value
    if isinstance(value, Mapping):
        for key, child in value.items():
            yield from _walk(child, (*path, str(key)))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _walk(child, (*path, index))


def _format_path(path: tuple[str | int, ...]) -> str:
    return "/".join(str(part) for part in path)


def _check_schema_validation(schema: dict[str, Any], flow: dict[str, Any]) -> Draft7Validator:
    Draft7Validator.check_schema(schema)
    validator = Draft7Validator(schema)
    errors = sorted(validator.iter_errors(flow), key=lambda error: list(error.absolute_path))
    rendered = [f"{_format_path(tuple(error.absolute_path))}: {error.message}" for error in errors]
    _check("macOS reference validates against Draft-07 schema", not errors, "; ".join(rendered[:10]))
    return validator


def _check_probe_expectation_advisory_contract(flow: dict[str, Any]) -> None:
    declared = [
        probe_id
        for probe_id, definition in flow["probes"].items()
        if "expectation" in definition
    ]
    _check(
        "Qwen index readback has a declared probe expectation",
        "lm_studio_inference_model_indexed" in declared,
        str(declared),
    )
    _check(
        "all 76 probe expectations, including Qwen index readback, are advisory documentation",
        len(declared) == 76
        and any(
            gap["id"] == "probe_expectations_advisory_only"
            for gap in flow["known_gaps"]
        ),
        str({"declared": len(declared), "known_gaps": flow["known_gaps"]}),
    )
    readers: list[str] = []
    for path in _MANAGER_SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        readers.extend(
            str(path.relative_to(_REPO_ROOT))
            for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and node.value == "expectation"
        )
    _check(
        "probe expectation has exactly one manager reader and it labels the value advisory",
        readers == ["solet_cli/src/solet_manager/completion_verifier.py"]
        and '"declared_expectation_advisory"' in (
            _MANAGER_SOURCE_ROOT / "solet_manager" / "completion_verifier.py"
        ).read_text(encoding="utf-8"),
        str(readers),
    )


def _check_static_option_conditions(validator: Draft7Validator, flow: dict[str, Any]) -> None:
    """Validate availability conditions as expressions over resolved decisions.

    Fact-dependent options would require probes before the selection that
    activates those probes. Compound expressions must keep the same
    decision-only leaf restriction recursively, including under negation.
    """

    leaf = {"decision_ref": "setup_profile", "operator": "equals", "value": "free"}
    fact = {"fact_ref": "host_memory_mb", "operator": "equals", "value": 24576}
    cases: list[tuple[object, bool]] = [
        (leaf, True),
        ({"all": [leaf, {"any": [leaf, {"not": leaf}]}]}, True),
        (None, False),
        ({}, False),
        ({"all": []}, False),
        ({**leaf, "operator": "matches"}, False),
        ({**leaf, "fallback": True}, False),
        ({"not": leaf, "any": [leaf]}, False),
        (fact, False),
        ({"all": [leaf, {"not": fact}]}, False),
    ]
    mutated = copy.deepcopy(flow)
    options = mutated["decisions"]["inference_implementation"]["option_source"]["options"]
    for condition, accepted in cases:
        options["none"]["available_when"] = condition
        _check(f"static option condition schema [{condition}]", validator.is_valid(mutated) == accepted, str(condition))


def _iter_refs(flow: dict[str, Any]) -> Iterator[tuple[str, str, str]]:
    """Yield ``(registry, reference, path)`` for every typed cross-reference.

    The leaf ``operation_ref`` inside an operation and ``probe_ref`` inside a
    probe are trusted resolver keys, not registry references.  Every other
    typed reference is checked against its registry here.
    """

    for path, value in _walk(flow):
        if not path or not isinstance(path[-1], str):
            continue
        key = path[-1]
        parent_registry = path[0] if path else ""
        if key == "operation_ref" and parent_registry == "operations":
            continue
        if key == "probe_ref" and parent_registry == "probes":
            continue
        yield from _refs_for_value(key, value, path)


def _refs_for_value(
    key: str,
    value: object,
    path: tuple[str | int, ...],
) -> Iterator[tuple[str, str, str]]:
    singular_registry = _SINGULAR_REF_REGISTRIES.get(key)
    if singular_registry is not None and isinstance(value, str):
        yield singular_registry, value, _format_path(path)
        return
    plural_registry = _PLURAL_REF_REGISTRIES.get(key)
    if plural_registry is None or not isinstance(value, list):
        return
    for index, reference in enumerate(value):
        if isinstance(reference, str):
            yield plural_registry, reference, f"{_format_path(path)}/{index}"


def _check_cross_references(flow: dict[str, Any]) -> None:
    missing: list[str] = []
    checked = 0
    for registry_name, reference, path in _iter_refs(flow):
        checked += 1
        registry = flow.get(registry_name)
        if not isinstance(registry, dict) or reference not in registry:
            missing.append(f"{path} -> {registry_name}/{reference}")
    _check(
        "all typed setup-flow references resolve",
        not missing,
        f"{len(missing)} missing of {checked}: {missing[:20]}",
    )

    wizard_missing: list[str] = []
    for page in flow["wizard"]["pages"]:
        for item in page["item_refs"]:
            if item["kind"] == "summary":
                continue
            registry_name = {
                "decision": "decisions",
                "input": "inputs",
                "consent": "consents",
                "permission": "permissions",
                "plugin": "plugins",
            }[item["kind"]]
            if item["ref"] not in flow[registry_name]:
                wizard_missing.append(f"{page['id']}:{item['kind']}:{item['ref']}")
    _check("all wizard item references resolve", not wizard_missing, str(wizard_missing))


def _check_decision_options(flow: dict[str, Any]) -> None:
    for decision_id, decision in flow["decisions"].items():
        source = decision["option_source"]
        static_options = source.get("options", {})
        for recommended in decision.get("recommended_option_refs", []):
            _check(
                f"decision recommendation resolves [{decision_id}/{recommended}]",
                recommended in static_options,
                f"available options: {sorted(static_options)}",
            )

    embedding_source = flow["decisions"]["embedding_model"]["option_source"]
    inference_source = flow["decisions"]["inference_model"]["option_source"]
    _check(
        "embedding model is dynamically discovered",
        embedding_source["mode"] == "discovered",
        str(embedding_source),
    )
    _check(
        "inference model is dynamically discovered",
        inference_source["mode"] == "discovered",
        str(inference_source),
    )
    _check(
        "embedding candidates require qualification",
        "embedding_model_qualification" in embedding_source["candidate_contract"]["qualification_probe_refs"],
        str(embedding_source["candidate_contract"]),
    )
    _check(
        "inference candidates are eligible when discovered as served",
        inference_source["candidate_contract"]["qualification_probe_refs"] == [],
        str(inference_source["candidate_contract"]),
    )

    embedding_options = flow["decisions"]["embeddings_implementation"]["option_source"]["options"]
    lm_plugins = set(embedding_options["lm_studio"]["activates"]["plugin_refs"])
    alternative_plugins = set(embedding_options["ollama_plugin"]["activates"]["plugin_refs"])
    _check(
        "implementation alternatives may activate different plugins",
        lm_plugins.isdisjoint(alternative_plugins),
        f"LM Studio={lm_plugins}, alternative={alternative_plugins}",
    )


def _check_candidate_wire_contract(validator: Draft7Validator, flow: dict[str, Any]) -> None:
    """Keep manager-side contracts aligned to the closed adapter candidate wire shape."""

    for decision_id in ("embedding_model", "inference_model"):
        contract = flow["decisions"][decision_id]["option_source"]["candidate_contract"]
        _check(
            f"candidate contract has no adapter-source field names [{decision_id}]",
            "label_field" not in contract and "value_field" not in contract,
            str(contract),
        )

    invalid = copy.deepcopy(flow)
    invalid["decisions"]["embedding_model"]["option_source"]["candidate_contract"][
        "label_field"
    ] = "display_name"
    _check(
        "schema rejects impossible adapter-source candidate field names",
        bool(list(validator.iter_errors(invalid))),
        "candidate contract unexpectedly accepted label_field",
    )
    derived = _qualification_availability_error("embedding_model", [], [], {})
    _check(
        "derived empty candidate behavior remains blocking",
        derived is not None and derived["error_kind"] == "candidate_set_empty",
        str(derived),
    )
    for decision_id in ("embedding_model", "inference_model"):
        source = flow["decisions"][decision_id]["option_source"]
        _check(
            f"empty candidate declaration matches derived block behavior [{decision_id}]",
            source["empty_result_behavior"] == "block",
            str(source),
        )


def _check_secret_contract(validator: Draft7Validator, flow: dict[str, Any]) -> None:
    secret_inputs = {input_id: value for input_id, value in flow["inputs"].items() if value["sensitive"] is True}
    _check("reference flow contains secret inputs", bool(secret_inputs), "none found")
    for input_id, secret in secret_inputs.items():
        handling = secret["secret_handling"]
        _check(f"secret has no default [{input_id}]", "default" not in secret, str(secret))
        _check(
            f"secret remains agent-blind [{input_id}]",
            handling["agent_visibility"] == "blind",
            str(handling),
        )
        _check(
            f"secret stays out of logs and journal [{input_id}]",
            {"no_logs", "no_journal"} <= set(handling["protections"]),
            str(handling["protections"]),
        )

    invalid = copy.deepcopy(flow)
    invalid["inputs"]["jira_api_token"]["default"] = "must-never-be-schema-valid"
    errors = list(validator.iter_errors(invalid))
    _check(
        "schema rejects a literal default on a secret input",
        bool(errors),
        "invalid secret-bearing contract unexpectedly validated",
    )


def _check_input_reader_contract(flow: dict[str, Any]) -> None:
    """Prove existing answer validation consumes type/pattern and secret seams."""

    class FixtureBundle:
        flow_id = "setup-flow-field-kind-fixture"
        inputs = flow["inputs"]
        decisions: dict[str, dict[str, Any]] = {}
        consents: dict[str, dict[str, Any]] = {}

    def invalid_public_input(value: Any) -> bool:
        answers: dict[str, Any] = {
            "schema_version": 1,
            "flow_id": FixtureBundle.flow_id,
            "flow_source_revision": None,
            "name": "fixture",
            "target": "/tmp/fixture",
            "public_inputs": {"solet_name": value},
            "decisions": {},
            "consents": {},
            "resolution_evidence": {},
        }
        try:
            validate_normalized_answers(FixtureBundle(), answers)
        except ContractError:
            return True
        return False

    _check(
        "declared public input pattern is enforced",
        invalid_public_input("Invalid Name!"),
        "solet_name invalid pattern was accepted",
    )
    _check(
        "declared public input value type is enforced",
        invalid_public_input(17),
        "solet_name non-string value was accepted",
    )

    for operation_id, operation in flow["operations"].items():
        if "secret_input_refs" not in operation:
            continue
        reference = operation["operation_ref"]
        declared_public = set(operation.get("input_refs", ())) - set(
            operation["secret_input_refs"]
        )
        _check(
            f"secret input declaration matches adapter public allowlist [{operation_id}]",
            _ALLOWED_PUBLIC_INPUTS.get(reference) == declared_public,
            f"{reference}: declared={sorted(declared_public)}, "
            f"adapter={sorted(_ALLOWED_PUBLIC_INPUTS.get(reference, ())) }",
        )


def _valid_field_kind_allowlist_entry(item: dict[str, str]) -> bool:
    required_keys = {"field", "reader_to_be", "ruling"}
    keys = set(item)
    if keys != required_keys and keys != required_keys | {"legacy"}:
        return False
    if not all(isinstance(item[key], str) and item[key] for key in required_keys):
        return False
    return "legacy" not in item or isinstance(item["legacy"], str) and bool(item["legacy"])


def _unwired_behavioural_fields(fields: dict[str, Any]) -> set[str]:
    return {
        field
        for field, metadata in fields.items()
        if isinstance(field, str)
        and isinstance(metadata, dict)
        and metadata.get("kind") == "behavioural"
        and "reader" not in metadata
    }


def _check_field_kind_metadata(field: str, metadata: dict[str, Any], unwired: set[str]) -> None:
    kind = metadata.get("kind")
    _check(
        f"field has a recognized kind [{field}]",
        kind in {"behavioural", "derived", "documentary"},
        str(metadata),
    )
    if kind == "behavioural" and field not in unwired:
        _check(
            f"implemented behavioural field names a reader [{field}]",
            isinstance(metadata.get("reader"), str) and bool(metadata["reader"]),
            str(metadata),
        )
    if kind == "derived":
        _check(
            f"derived field names its resolver [{field}]",
            isinstance(metadata.get("resolver"), str) and bool(metadata["resolver"]),
            str(metadata),
        )
    if kind == "documentary":
        _check(
            f"documentary field records its disposition [{field}]",
            isinstance(metadata.get("debt"), str) and bool(metadata["debt"]),
            str(metadata),
        )


def _check_field_kind_contract() -> None:
    """Enforce derived, behavioural, and documentary unread-field dispositions."""

    manifest = _load_json(_FIELD_KINDS_PATH)
    fields = manifest.get("fields")
    _check("field-kind manifest has the corrected unread census", isinstance(fields, dict), str(fields))
    if not isinstance(fields, dict):
        return
    allowlist = _load_list(_FIELD_KIND_ALLOWLIST_PATH)
    allowed = {item.get("field") for item in allowlist if _valid_field_kind_allowlist_entry(item)}
    _check("field-kind allowlist entries have exact tracked-debt shape", len(allowed) == len(allowlist), str(allowlist))
    behavioural_without_reader = _unwired_behavioural_fields(fields)
    _check(
        "every unimplemented behavioural field is exactly allowlisted debt",
        behavioural_without_reader == allowed,
        f"unwired={sorted(behavioural_without_reader)}, allowlisted={sorted(allowed)}",
    )
    for field, metadata in fields.items():
        if not isinstance(metadata, dict):
            raise SmokeFailureError(f"field-kind metadata for {field!r} is not an object")
        _check_field_kind_metadata(field, metadata, behavioural_without_reader)


def _decision_activation_field_names(schema: dict[str, Any]) -> set[str]:
    definitions = schema.get("definitions")
    activation = definitions.get("activation") if isinstance(definitions, dict) else None
    properties = activation.get("properties") if isinstance(activation, dict) else None
    if not isinstance(properties, dict) or not all(isinstance(key, str) for key in properties):
        raise SmokeFailureError("setup-flow schema activation properties are not an object")
    return {f"decisions.activates.{key}" for key in properties}


def _check_decision_activation_field_kind_coverage(
    schema: dict[str, Any],
    field_kinds: dict[str, Any],
) -> None:
    expected = _decision_activation_field_names(schema)
    missing = sorted(expected - set(field_kinds))
    _check(
        "every schema decision activation sub-key has a field-kind census entry",
        not missing,
        f"missing={missing}",
    )
    extended_schema = copy.deepcopy(schema)
    extended_schema["definitions"]["activation"]["properties"]["future_refs"] = {
        "$ref": "#/definitions/id_array"
    }
    missing_after_schema_extension = sorted(
        _decision_activation_field_names(extended_schema) - set(field_kinds)
    )
    _check(
        "an unclassified decision activation schema addition is detected",
        missing_after_schema_extension == ["decisions.activates.future_refs"],
        f"missing={missing_after_schema_extension}",
    )


def _check_lifecycle_contract(validator: Draft7Validator, flow: dict[str, Any]) -> None:
    start = flow["executor_contracts"]["start_command"]
    readiness = start["startup_readiness"]
    _check(
        "start declares canonical identity postconditions",
        start["operation_ref"] == "lifecycle::start" and start["postcondition_probe_refs"] == ["router_ready", "peer_identity_valid"],
        str(start),
    )
    _check(
        "start readiness authority covers exact target-local health",
        readiness
        == {
            "contract_version": 1,
            "budget_source": "executor_contracts.start_command.timeout_seconds",
            "budget_unit": "seconds",
            "semantic_scope": (
                "target_start_through_target_cli_health_status_healthy"
            ),
            "release_signal": "target_cli_health_top_level_status_healthy",
            "consumer_probe_purposes": ["stage_exit", "completion"],
            "consumer_probe_refs": ["embedding_request_succeeds"],
            "downstream_reservations": {
                "governed_process_call_seconds": 30
            },
        },
        str(readiness),
    )
    unknown = copy.deepcopy(flow)
    unknown["executor_contracts"]["start_command"]["command"] = "launchctl kickstart"
    _check(
        "lifecycle start declaration is closed",
        bool(list(validator.iter_errors(unknown))),
        "unknown start command unexpectedly validated",
    )
    missing_postcondition = copy.deepcopy(flow)
    missing_postcondition["executor_contracts"]["start_command"]["postcondition_probe_refs"] = []
    _check(
        "lifecycle start requires postconditions",
        bool(list(validator.iter_errors(missing_postcondition))),
        "start without identity postconditions unexpectedly validated",
    )
    missing_readiness = copy.deepcopy(flow)
    del missing_readiness["executor_contracts"]["start_command"]["startup_readiness"]
    _check(
        "lifecycle start requires readiness authority",
        bool(list(validator.iter_errors(missing_readiness))),
        "start without readiness authority unexpectedly validated",
    )
    ambiguous_unit = copy.deepcopy(flow)
    ambiguous_unit["executor_contracts"]["start_command"]["startup_readiness"][
        "budget_unit"
    ] = "milliseconds"
    _check(
        "readiness budget unit is unambiguous",
        bool(list(validator.iter_errors(ambiguous_unit))),
        "ambiguous readiness unit unexpectedly validated",
    )
    negative_reserve = copy.deepcopy(flow)
    negative_reserve["executor_contracts"]["start_command"]["startup_readiness"][
        "downstream_reservations"
    ]["governed_process_call_seconds"] = -1
    _check(
        "readiness downstream reservation is positive",
        bool(list(validator.iter_errors(negative_reserve))),
        "negative readiness reservation unexpectedly validated",
    )


def _check_auth_inventory(flow: dict[str, Any]) -> None:
    expected_modes = {
        "google_workspace_oauth": "oauth_browser",
        "jira_api_token": "api_token",
        "marketo_client_credentials": "oauth_client_credentials",
        "salesforce_cli_login": "cli_session",
        "schwab_oauth": "oauth_browser",
        "snowflake_rsa": "rsa_key_pair",
        "zuora_client_credentials": "oauth_client_credentials",
        "external_postgres_credentials": "database_credentials",
    }
    actual = {auth_id: flow["auth_flows"][auth_id]["mode"] for auth_id in expected_modes}
    _check("reviewed connector auth modes are pinned", actual == expected_modes, str(actual))

    jira = flow["plugins"]["jira_plugin"]
    snowflake = flow["plugins"]["snowflake_plugin"]
    _check(
        "Jira destructive capability is explicit",
        jira["access_posture"]["destructive_actions"] is True and "jira_destructive_access_consent" in jira["consent_refs"],
        str(jira["access_posture"]),
    )
    _check(
        "Snowflake role is the declared RBAC boundary",
        "role" in snowflake["access_posture"]["rbac_boundary"].lower(),
        snowflake["access_posture"]["rbac_boundary"],
    )
    external_postgres = flow["plugins"]["external_postgres_plugin"]
    _check(
        "external PostgreSQL supports zero or more on-demand connections",
        external_postgres["cardinality"] == "zero_or_more" and external_postgres["configuration_timing"] == "on_demand",
        str(external_postgres),
    )


def _check_execution_safety(flow: dict[str, Any]) -> None:
    for operation_id, operation in flow["operations"].items():
        _check(
            f"operation uses a trusted resolver key [{operation_id}]",
            "::" in operation["operation_ref"] and "\n" not in operation["operation_ref"],
            operation["operation_ref"],
        )
        if operation["risk"] in {"high", "destructive"}:
            _check(
                f"high-risk operation requires confirmation [{operation_id}]",
                operation["requires_confirmation"] is True,
                str(operation),
            )
    raw_command_fields = [_format_path(path) for path, _ in _walk(flow) if path and path[-1] in {"command", "shell"}]
    _check(
        "flow contains no arbitrary command or shell fields",
        not raw_command_fields,
        str(raw_command_fields),
    )
    _check(
        "user remains the sole authorization actor",
        flow["policies"]["authorization_actor"] == "user" and flow["policies"]["model_role"]["may_authorize"] is False,
        str(flow["policies"]),
    )
    _check(
        "local coding-agent history is preserved",
        flow["policies"]["history_policy"]["local_session_artifacts"] == "preserve",
        str(flow["policies"]["history_policy"]),
    )
    _check(
        "setup completion cannot be health-only",
        flow["completion"]["allow_health_only"] is False,
        str(flow["completion"]),
    )
    _check(
        "resume begins at the first unverified checkpoint",
        flow["state_policy"]["resume_mode"] == "from_first_unverified_checkpoint",
        str(flow["state_policy"]),
    )


def _check_derived_implementation_status(flow: dict[str, Any]) -> None:
    """Resolve adapter seams to prevent hand-authored implementation-status drift.

    This maps each flow definition's resolver key through the production
    operation/probe registries. It deliberately does not grep a field name:
    a same-named attribute on an unrelated contract cannot satisfy this check.
    Adapter-bound runners need a registered callable and are therefore
    ``adapter_required``. The explicit manual connector handler remains
    ``manual`` because it returns ``awaiting_user`` rather than applying work.
    """

    bootstrap_errors = _bootstrap_implementation_status_errors(flow)
    _check(
        "bootstrap resolver statuses are derived from the closed pre-venv registry",
        not bootstrap_errors,
        str(bootstrap_errors),
    )
    index_ids = ("ensure_index_lm_studio_inference", "lm_studio_inference_model_indexed")
    with patch("bootstrap_adapter.routes.lm_studio_route", return_value={"routed": "lm_studio"}) as routed:
        routed_results = [
            _bootstrap_dispatch(cast(Any, {"operation_id": item, "operation_ref": _BOOTSTRAP_ROUTES[item][0]}), cast(Any, object()))
            for item in index_ids
        ]
    _check(
        "Qwen index operation and probe dispatch through the pre-venv LM Studio bridge",
        routed_results == [{"routed": "lm_studio"}] * 2 and routed.call_count == 2,
        str({"results": routed_results, "calls": routed.call_count}),
    )
    understated = copy.deepcopy(flow)
    understated["operations"]["request_homebrew_install"][
        "implementation_status"
    ] = "adapter_required"
    _check(
        "understated bootstrap resolver status is rejected",
        bool(_bootstrap_implementation_status_errors(understated)),
        "request_homebrew_install marked adapter_required escaped reconciliation",
    )

    adapter_bound_runners = frozenset(
        {"external_cli", "hydration", "system", "system_settings"}
    )
    registries = (
        ("operations", "operation_ref", operation_handlers()),
        ("probes", "probe_ref", probe_handlers()),
    )
    for registry_name, reference_key, handlers in registries:
        definitions = flow[registry_name]
        for definition_id, definition in definitions.items():
            reference = definition[reference_key]
            handler = handlers.get(reference)
            status = definition["implementation_status"]
            if handler is _manual_connector:
                _check(
                    f"manual handler stays manual [{registry_name}/{definition_id}]",
                    status == "manual",
                    f"{reference} resolves to _manual_connector but declares {status}",
                )
                continue
            if definition["runner"] not in adapter_bound_runners:
                continue
            _check(
                f"adapter-bound resolver is registered [{registry_name}/{definition_id}]",
                handler is not None,
                f"{reference} has no handler in the {registry_name} registry",
            )
            _check(
                f"adapter-bound resolver status is derived [{registry_name}/{definition_id}]",
                status == "adapter_required",
                f"{reference} resolves to {handler!r} but declares {status}",
            )


def _bootstrap_implementation_status_errors(flow: dict[str, Any]) -> list[str]:
    """Return contract discrepancies for pre-venv routes implemented in bootstrap.py."""

    errors: list[str] = []
    registries = (
        ("operations", "operation_ref", "operation"),
        ("probes", "probe_ref", "probe"),
    )
    for registry_name, reference_key, route_kind in registries:
        for definition_id, definition in flow[registry_name].items():
            if definition["runner"] != "bootstrap":
                continue
            declared = _BOOTSTRAP_ROUTES.get(definition_id)
            expected = (definition[reference_key], route_kind)
            if declared != expected:
                errors.append(
                    f"{registry_name}/{definition_id}: expected route {expected!r}, got {declared!r}"
                )
            if definition["implementation_status"] != "implemented":
                errors.append(
                    f"{registry_name}/{definition_id}: expected implemented, got "
                    f"{definition['implementation_status']!r}"
                )
    return errors


def _check_stage_frontier_contract(flow: dict[str, Any]) -> None:
    """Check where decisions resolve and which runner owns each frontier probe.

    The three groups run in their original order and keep their check labels.
    """
    _check_decision_resolution_stages(flow)
    _check_decision_checkpoint_boundaries(flow)
    _check_model_probe_frontier(flow)


def _check_decision_resolution_stages(flow: dict[str, Any]) -> None:
    """Model choices resolve at models; every other decision resolves at review.

    Model discovery needs the selected implementation installed, so only the
    two model decisions may wait for the models stage.
    """
    resolution_stages = {
        decision_id: definition["resolution_stage_ref"]
        for decision_id, definition in flow["decisions"].items()
    }
    _check(
        "model choices resolve only at models",
        resolution_stages["embedding_model"] == "models"
        and resolution_stages["inference_model"] == "models",
        str(resolution_stages),
    )
    _check(
        "non-model choices resolve at decision review",
        all(
            stage_id == "decision_review"
            for decision_id, stage_id in resolution_stages.items()
            if decision_id not in {"embedding_model", "inference_model"}
        ),
        str(resolution_stages),
    )


def _check_decision_checkpoint_boundaries(flow: dict[str, Any]) -> None:
    """Pin the pre-venv runners and the reused decision checkpoint boundaries.

    The checkout probe runs before any target venv exists, and the decision
    checkpoint guards both the review exit and the models entry.
    """
    _check(
        "pre-venv checkout and manager decision runners are pinned",
        flow["probes"]["git_checkout_valid"]["runner"] == "bootstrap"
        and flow["probes"]["decisions_resolved"]["runner"] == "manager",
        str(
            {
                "git_checkout_valid": flow["probes"]["git_checkout_valid"],
                "decisions_resolved": flow["probes"]["decisions_resolved"],
            }
        ),
    )
    _check(
        "decision checkpoint is reused at the exact stage boundaries",
        flow["stages"]["decision_review"]["exit_probe_refs"]
        == ["decisions_resolved"]
        and flow["stages"]["models"]["entry_probe_refs"]
        == ["decisions_resolved", "apple_host_eligible"],
        str(
            {
                "decision_review": flow["stages"]["decision_review"],
                "models": flow["stages"]["models"],
            }
        ),
    )
    unrunnable = _pre_venv_boundary_probes_without_adapter(flow)
    _check("no boundary probe before the target venv needs a venv-bound runner", not unrunnable, str(unrunnable))


def _pre_venv_boundary_probes_without_adapter(flow: dict[str, Any]) -> list[str]:
    """Name boundary probes that run before build_instance_environment yet need its venv.

    The runner rule is read from the real adapter registry on a seeded target
    that has bootstrap.py but no .venv, so it cannot drift from the runtime.
    """
    stages = flow["stages"]
    venv_sequence = next(
        stage["sequence"] for stage in stages.values()
        if "build_instance_environment" in stage.get("operation_refs", [])
    )
    probe_ids = [
        probe_id
        for stage in stages.values()
        for key, bound in (("entry_probe_refs", venv_sequence), ("exit_probe_refs", venv_sequence - 1))
        if stage["sequence"] <= bound
        for probe_id in stage.get(key, [])
    ]
    with tempfile.TemporaryDirectory() as raw:
        target = Path(raw)
        (target / "bootstrap.py").touch()
        registry = AdapterRegistry(target=target, base_python=Path(sys.executable))
        return [
            probe_id for probe_id in probe_ids
            if flow["probes"][probe_id]["runner"] != "manager"
            and registry.command_for(flow["probes"][probe_id]["runner"]) is None
        ]


def _check_model_probe_frontier(flow: dict[str, Any]) -> None:
    """Keep model probes at the frontier that can actually answer them.

    The Core AI vector probe follows router readiness, raw qualification stays
    at hydration, and the configured embedding probe uses the bound service.
    """
    coreai_embedding_probe = flow["probes"]["coreai_embedding_request_succeeds"]
    models_exit_probes = flow["stages"]["models"]["exit_probe_refs"]
    _check(
        "CoreAI embedding request uses its selected handler after router readiness",
        coreai_embedding_probe["required_when"]
        == {
            "decision_ref": "embeddings_implementation",
            "operator": "equals",
            "value": "coreai",
        }
        and coreai_embedding_probe["probe_ref"]
        == "setup::apple.embedding_request_succeeds"
        and "router_ready" in models_exit_probes
        and "coreai_embedding_request_succeeds" in models_exit_probes
        and models_exit_probes.index("coreai_embedding_request_succeeds")
        > models_exit_probes.index("router_ready"),
        str(
            {
                "probe": coreai_embedding_probe,
                "models_exit_probe_refs": models_exit_probes,
            }
        ),
    )
    _check(
        "raw model qualification stays at hydration frontier",
        flow["probes"]["structured_action_qualification"]["runner"]
        == "hydration"
        and flow["probes"]["representative_inference_probe"]["runner"]
        == "hydration",
        "model qualification probes must not require platform readiness",
    )
    _check(
        "configured embedding request uses the bound service interface",
        flow["probes"]["embedding_request_succeeds"]["probe_ref"]
        == "service_interface::embedding_service.get_embedding_dimension",
        str(flow["probes"]["embedding_request_succeeds"]),
    )


def _check_bootstrap_dependency_order(flow: dict[str, Any]) -> None:
    operations = flow["stages"]["system_dependencies"]["operation_refs"]
    _check(
        "reviewed Homebrew stop and Python runtime selection precede downstream dependencies",
        operations[:3]
        == [
            "request_homebrew_install",
            "install_python_runtime",
            "build_instance_environment",
        ],
        str(operations),
    )
    python_runtime = flow["operations"]["install_python_runtime"]
    _check(
        "Python runtime contract declares the reviewed Homebrew supply policy",
        python_runtime["runner"] == "bootstrap"
        and "Homebrew's python@3.13" in python_runtime["description"],
        str(python_runtime),
    )


def _check_genesis_adapter_request_contract() -> None:
    """Drive the manager plan and request builder into the strict adapter gate."""

    revision = "a" * 40
    target = Path("/tmp/genesis-adapter-contract")
    bundle = ContractBundle.load(source_revision=revision, directory=_KB_ROOT)
    seed = SeedLock(
        repository="example/seed",
        release_tag="v1.0.0",
        commit=revision,
        tree_hash="b" * 40,
        archive_sha256="c" * 64,
        profile="macos-bizops",
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=CreateConfig(name="genesis-contract", target=target, autostart=True),
        seed=seed,
        journal_path=Path("/tmp/genesis-adapter-contract.json"),
        prospective_consents=True,
        operation_stage_ids={"genesis"},
    )
    genesis = next(
        operation
        for operation in plan.operations
        if operation.operation_ref == "genesis::solet.run"
    )
    transaction = Transaction.create(
        name="genesis-contract",
        target=target,
        input_fingerprint=canonical_sha256({"fixture": "genesis-adapter-contract"}),
        answers=plan.answers,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=(),
    )
    request = operation_request(
        transaction,
        bundle,
        genesis,
        phase="probe",
        probe_purpose="preview",
        approval=None,
        attempt=1,
    )
    adapter_request = AdapterRequest.from_dict(cast(dict[str, object], request.to_dict()))

    def accepted_handler(_request: AdapterRequest, _runtime: object) -> JsonObject:
        return {"accepted": True}

    with patch.object(
        setup_adapter,
        "operation_handlers",
        return_value={"genesis::solet.run": accepted_handler},
    ):
        response = setup_adapter.dispatch_request(adapter_request, object())
    _check(
        "manager genesis request satisfies the strict adapter public-input contract",
        response == {"accepted": True},
        str({"public_inputs": request.public_inputs, "response": response}),
    )


def _literal_string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _public_string_key(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Name) or call.func.id != "public_string":
        return None
    if len(call.args) < 2:
        return None
    return _literal_string(call.args[1])


def _public_input_get_key(call: ast.Call) -> str | None:
    if not isinstance(call.func, ast.Attribute) or call.func.attr != "get" or not call.args:
        return None
    receiver = call.func.value
    if not isinstance(receiver, ast.Attribute) or receiver.attr != "public_inputs":
        return None
    if not isinstance(receiver.value, ast.Name) or receiver.value.id != "request":
        return None
    return _literal_string(call.args[0])


def _handler_public_input_keys(handler: object) -> set[str]:
    """Extract literal public-input reads from one registered probe handler."""

    tree = ast.parse(inspect.getsource(handler))
    keys = {
        key
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        for key in (_public_string_key(node), _public_input_get_key(node))
        if key is not None
    }
    return keys


def _matches_exact_projection(expected: frozenset[str], projected: set[str]) -> bool:
    return projected == expected


def _projection_transaction(flow: dict[str, Any]) -> Transaction:
    revision = "f" * 40
    seed = SeedLock(
        repository="example/projection",
        release_tag="v1.0.0",
        commit=revision,
        tree_hash="e" * 40,
        archive_sha256="d" * 64,
        profile="macos-bizops",
    )
    return Transaction.create(
        name="projection-contract",
        target=Path("/tmp/projection-contract"),
        input_fingerprint=canonical_sha256({"fixture": "projection-contract"}),
        answers={
            "decisions": {
                "autostart": "enabled",
                "coding_agents": ["codex"],
                "embedding_model": "fixture-embedding-model",
                "inference_model": "fixture-inference-model",
                "embeddings_implementation": "lm_studio",
                "inference_implementation": "lm_studio",
            },
            "public_inputs": {"lm_studio_base_url": "http://127.0.0.1:1234/v1"},
        },
        seed=seed,
        flow_id=str(flow["flow_id"]),
        flow_source_revision=revision,
        flow_contract_digest="sha256:" + "c" * 64,
        stage_ids=tuple(str(stage_id) for stage_id in flow["stages"]),
        completion_probe_ids=(),
    )


def _check_operation_probe_projection_contract(flow: dict[str, Any]) -> None:
    """Keep declared operation probes aligned with their input-bearing handlers."""

    handlers = probe_handlers()
    transaction = _projection_transaction(flow)
    uncovered: list[str] = []
    missing_required_projection: list[str] = []
    seen_refs: set[str] = set()
    for operation_id, operation in flow["operations"].items():
        idempotency = operation["idempotency"]
        for phase in ("precondition_probe_refs", "postcondition_probe_refs"):
            for probe_id in idempotency[phase]:
                probe = flow["probes"][probe_id]
                probe_ref = probe["probe_ref"]
                handler = handlers.get(probe_ref)
                if handler is None:
                    continue
                projected = set(probe_public_inputs(transaction, probe_ref))
                required = _handler_public_input_keys(handler)
                fallbacks = _HANDLER_FALLBACK_PUBLIC_INPUTS.get(probe_ref, frozenset())
                missing = required - projected - fallbacks
                if missing:
                    uncovered.append(
                        f"{operation_id}/{phase}/{probe_id}: {sorted(missing)}"
                    )
                if probe_ref not in seen_refs:
                    expected = _REQUIRED_OPERATION_PROJECTIONS.get(probe_ref, frozenset())
                    absent = expected - projected
                    if absent:
                        missing_required_projection.append(
                            f"{probe_ref}: {sorted(absent)}"
                        )
                    seen_refs.add(probe_ref)
    _check(
        "every operation probe handler input is projected or has a documented fallback",
        not uncovered,
        str(uncovered),
    )
    _check(
        "genesis and inference operation probes retain their required projections",
        not missing_required_projection,
        str(missing_required_projection),
    )
    for probe_ref, expected in _REQUIRED_OPERATION_PROJECTIONS.items():
        projected = set(probe_public_inputs(transaction, probe_ref))
        _check(
            f"operation probe projects its exact reviewed key set [{probe_ref}]",
            _matches_exact_projection(expected, projected),
            f"expected={sorted(expected)}, projected={sorted(projected)}",
        )
        _check(
            f"empty projection mutation is detected [{probe_ref}]",
            not _matches_exact_projection(expected, set()),
            f"expected={sorted(expected)}, projected=[]",
        )
        _check(
            f"wrong-key projection mutation is detected [{probe_ref}]",
            not _matches_exact_projection(expected, {"wrong_projection_key"}),
            f"expected={sorted(expected)}, projected=['wrong_projection_key']",
        )


def main() -> int:
    try:
        schema = _load_json(_SCHEMA_PATH)
        flow = _load_json(_FLOW_PATH)
        validator = _check_schema_validation(schema, flow)
        _check_static_option_conditions(validator, flow)
        _check_probe_expectation_advisory_contract(flow)
        _check_cross_references(flow)
        _check_decision_options(flow)
        _check_candidate_wire_contract(validator, flow)
        _check_secret_contract(validator, flow)
        _check_input_reader_contract(flow)
        _check_field_kind_contract()
        field_kind_manifest = _load_json(_FIELD_KINDS_PATH)
        field_kinds = field_kind_manifest.get("fields")
        if not isinstance(field_kinds, dict):
            raise SmokeFailureError("field-kind manifest fields are not an object")
        _check_decision_activation_field_kind_coverage(schema, field_kinds)
        _check_lifecycle_contract(validator, flow)
        _check_auth_inventory(flow)
        _check_execution_safety(flow)
        _check_derived_implementation_status(flow)
        _check_stage_frontier_contract(flow)
        _check_bootstrap_dependency_order(flow)
        _check_genesis_adapter_request_contract()
        _check_operation_probe_projection_contract(flow)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"setup_flow_schema_smoke OK: {len(_CHECKS_RUN)} checks passed ({len(flow['decisions'])} decisions, {len(flow['plugins'])} plugins, {len(flow['auth_flows'])} auth flows, {len(flow['probes'])} probes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
