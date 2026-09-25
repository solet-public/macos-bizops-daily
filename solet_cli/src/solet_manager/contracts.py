"""Runtime loading and validated access to shared setup contracts."""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast

from .answer_validation import validate_normalized_answers
from .contract_reconciliation_rules import (
    AnswerValueMigration,
    FirstUseInactiveProbeMigration,
    parse_answer_value_migrations,
    parse_first_use_inactive_probe_migrations,
)
from .contract_reconciliation_validation import (
    parse_operation_statuses_to_reset,
    reconciliation_entry,
)
from .contract_validation import (
    declares_knowledge_readiness,
    validate_contract_bundle,
    validate_knowledge_readiness,
)
from .decision_activation import active_decision_ids
from .errors import ContractError
from .models import JsonValue

PERMISSIONS_MANIFEST_FILENAME = "permissions_manifest.json"
_DIGESTED_CONTRACT_FILENAMES = (
    "macos_setup_flow.json",
    "setup_flow.schema.json",
    "setup_answers.schema.json",
    "setup_journal.schema.json",
    "setup_adapter_envelope.schema.json",
)
_CONTRACT_FILENAMES = (
    *_DIGESTED_CONTRACT_FILENAMES,
    PERMISSIONS_MANIFEST_FILENAME,
)
_TRANSITION_BUNDLE_FILENAMES = (
    "existing_install_flow.json",
    "existing_install_flow.schema.json",
    "setup_adapter_envelope.schema.json",
)
_RECONCILIATION_MANIFEST = "released_metadata/contract_reconciliation_manifest.json"
_RECONCILIATION_MANIFEST_KEYS = frozenset({"schema_version", "migrations"})
_RECONCILIATION_MAPPING_KEYS = frozenset({"source", "destination"})
_RECONCILIATION_STAGE_PROBE_KEYS = frozenset({"stage_id", "boundary", "probe_id"})
RECONCILIATION_DIGEST_PATTERN = r"^sha256:[0-9a-f]{64}$"
_LEGACY_RESUME_CONTRACT_DIGESTS = frozenset(
    {
        "sha256:67c903ea332287f2c73f036ff055b793f50072ed3af0784d68a505801c040540",
        "sha256:ab2a9d6df8098fee294c9886d9e3a39a9bb555b4d28efe6feaea731f7741105f",
    }
)

__all__ = [
    "ContractBundle",
    "ContractResolution",
    "PERMISSIONS_MANIFEST_FILENAME",
    "ContractReconciliation",
    "RECONCILIATION_DIGEST_PATTERN",
    "FirstUseInactiveProbeMigration",
    "StageProbeMapping",
    "StartupReadinessBudget",
    "active_decision_ids",
    "contract_digest",
    "contract_digest_from_bytes",
    "contract_digested_filenames",
    "contract_filenames",
    "transition_bundle_filenames",
    "discover_contract_directory",
    "load_reconciliation_destination_bundle",
    "startup_readiness_budget",
    "target_contract_directory",
    "load_contract_reconciliations",
    "validate_normalized_answers",
]


def contract_filenames() -> tuple[str, ...]:
    """Return the complete persisted setup-contract bundle file set."""

    return _CONTRACT_FILENAMES


def contract_digested_filenames() -> tuple[str, ...]:
    """Return the persisted setup-contract files that define release identity."""

    return _DIGESTED_CONTRACT_FILENAMES


@dataclass(frozen=True)
class StageProbeMapping:
    """One explicit historical stage-probe identity normalization."""

    source: tuple[str, str, str]
    destination: tuple[str, str, str]


@dataclass(frozen=True)
class ContractReconciliation:
    """One release-declared, source-pinned persisted-contract migration."""

    migration_id: str
    flow_id: str
    source_revision: str
    source_digest: str
    destination_digest: str
    stage_probe_mappings: tuple[StageProbeMapping, ...]
    operation_statuses_to_reset: tuple[str, ...] = ()
    first_use_inactive_probe_migrations: tuple[FirstUseInactiveProbeMigration, ...] = ()
    answer_value_migrations: tuple[AnswerValueMigration, ...] = ()


@dataclass(frozen=True, slots=True)
class ContractResolution:
    """The exact contract bytes selected for an operation."""

    path: Path
    origin: Literal["production", "explicit", "explicit_development"]
    flow_id: str
    digest: str

    def to_identity_dict(self) -> dict[str, JsonValue]:
        """Return the preview- and fingerprint-ready public resolution shape."""

        return {
            "path": str(self.path),
            "origin": self.origin,
            "flow_id": self.flow_id,
            "digest": self.digest,
        }


@dataclass(frozen=True, slots=True)
class _ContractDirectory:
    """A path resolution before its contract identity has been loaded."""

    path: Path
    origin: Literal["production", "explicit", "explicit_development"]


def load_contract_reconciliations(
    *,
    manifest_path: Path | None = None,
) -> tuple[ContractReconciliation, ...]:
    """Load the formula-authenticated closed reconciliation declaration."""

    base = Path(__file__).resolve().parent
    manifest = base / _RECONCILIATION_MANIFEST if manifest_path is None else manifest_path
    raw = _load_object(manifest)
    if frozenset(raw) != _RECONCILIATION_MANIFEST_KEYS or raw.get("schema_version") != 1:
        raise ContractError("contract reconciliation manifest does not match closed v1 schema")
    entries = raw.get("migrations")
    if not isinstance(entries, list):
        raise ContractError("contract reconciliation manifest migrations must be an array")
    parsed = tuple(_parse_reconciliation(item) for item in entries)
    if len({item.migration_id for item in parsed}) != len(parsed):
        raise ContractError("contract reconciliation manifest migration ids must be unique")
    return parsed


def _parse_reconciliation(value: JsonValue) -> ContractReconciliation:
    entry = reconciliation_entry(value)
    source = _manifest_identity(
        entry["source"],
        fields=("flow_id", "flow_source_revision", "flow_contract_digest"),
        label="source",
    )
    destination = _manifest_identity(
        entry["destination"],
        fields=("flow_id", "flow_contract_digest"),
        label="destination",
    )
    migration_id = entry["migration_id"]
    if not isinstance(migration_id, str) or not migration_id:
        raise ContractError("contract reconciliation migration_id must be a non-empty string")
    mappings = entry["stage_probe_mappings"]
    if not isinstance(mappings, list):
        raise ContractError("contract reconciliation stage_probe_mappings must be an array")
    parsed_mappings = tuple(_parse_stage_probe_mapping(item) for item in mappings)
    if len({item.source for item in parsed_mappings}) != len(parsed_mappings):
        raise ContractError("contract reconciliation source stage-probe mappings must be unique")
    if len({item.destination for item in parsed_mappings}) != len(parsed_mappings):
        raise ContractError(
            "contract reconciliation destination stage-probe mappings must be unique"
        )
    parsed_first_use_migrations = parse_first_use_inactive_probe_migrations(
        entry["first_use_inactive_probe_migrations"],
        migration_id=migration_id,
        source_digest=source[2],
        destination_digest=destination[1],
    )
    operation_statuses_to_reset = parse_operation_statuses_to_reset(
        entry["operation_statuses_to_reset"]
    )
    answer_value_migrations = parse_answer_value_migrations(entry["answer_value_migrations"])
    return ContractReconciliation(
        migration_id=migration_id,
        flow_id=source[0],
        source_revision=source[1],
        source_digest=source[2],
        destination_digest=destination[1],
        stage_probe_mappings=parsed_mappings,
        operation_statuses_to_reset=operation_statuses_to_reset,
        first_use_inactive_probe_migrations=parsed_first_use_migrations,
        answer_value_migrations=answer_value_migrations,
    )


def _manifest_identity(
    value: JsonValue,
    *,
    fields: tuple[str, ...],
    label: str,
) -> tuple[str, ...]:
    if not isinstance(value, dict) or frozenset(value) != frozenset(fields):
        raise ContractError(f"contract reconciliation {label} identity is invalid")
    values = tuple(value[field] for field in fields)
    if not all(isinstance(item, str) and item for item in values):
        raise ContractError(f"contract reconciliation {label} identity values are invalid")
    digest = values[-1]
    if not isinstance(digest, str) or re.fullmatch(RECONCILIATION_DIGEST_PATTERN, digest) is None:
        raise ContractError(
            f"contract reconciliation {label} flow_contract_digest must be sha256 followed by "
            "64 lowercase hexadecimal characters"
        )
    return cast(tuple[str, ...], values)


def _parse_stage_probe_mapping(value: JsonValue) -> StageProbeMapping:
    if not isinstance(value, dict) or frozenset(value) != _RECONCILIATION_MAPPING_KEYS:
        raise ContractError("contract reconciliation stage-probe mapping is invalid")
    return StageProbeMapping(
        source=_stage_probe_identity(value["source"]),
        destination=_stage_probe_identity(value["destination"]),
    )


def _stage_probe_identity(value: JsonValue) -> tuple[str, str, str]:
    if not isinstance(value, dict) or frozenset(value) != _RECONCILIATION_STAGE_PROBE_KEYS:
        raise ContractError("contract reconciliation stage-probe identity is invalid")
    stage_id, boundary, probe_id = (
        value["stage_id"],
        value["boundary"],
        value["probe_id"],
    )
    if (
        not isinstance(stage_id, str)
        or boundary not in {"entry", "exit"}
        or not isinstance(probe_id, str)
    ):
        raise ContractError("contract reconciliation stage-probe identity values are invalid")
    return stage_id, boundary, probe_id


@dataclass(frozen=True, slots=True)
class StartupReadinessBudget:
    """Validated transport lineage for the released startup-ready budget."""

    contract_version: int
    contract_digest: str
    source_artifact: str
    budget_source: str
    budget_unit: str
    semantic_scope: str
    release_signal: str
    consumer_probe_purposes: tuple[str, ...]
    consumer_probe_refs: tuple[str, ...]
    parent_budget_seconds: int
    governed_process_call_seconds: int

    @property
    def effective_wait_seconds(self) -> int:
        return self.parent_budget_seconds - self.governed_process_call_seconds

    def public_inputs(
        self,
        *,
        consumer_probe_purpose: str,
        consumer_probe_ref: str,
    ) -> dict[str, JsonValue]:
        if consumer_probe_purpose not in self.consumer_probe_purposes:
            raise ContractError(
                "executor_contracts.start_command.startup_readiness does not "
                f"govern {consumer_probe_purpose!r}"
            )
        if consumer_probe_ref not in self.consumer_probe_refs:
            raise ContractError(
                "executor_contracts.start_command.startup_readiness does not "
                f"govern probe {consumer_probe_ref!r}"
            )
        return {
            "startup_readiness_contract_version": self.contract_version,
            "startup_readiness_contract_digest": self.contract_digest,
            "startup_readiness_source_artifact": self.source_artifact,
            "startup_readiness_budget_source": self.budget_source,
            "startup_readiness_budget_unit": self.budget_unit,
            "startup_readiness_semantic_scope": self.semantic_scope,
            "startup_readiness_release_signal": self.release_signal,
            "startup_readiness_consumer_probe_purposes": list(self.consumer_probe_purposes),
            "startup_readiness_consumer_probe_purpose": consumer_probe_purpose,
            "startup_readiness_consumer_probe_refs": list(self.consumer_probe_refs),
            "startup_readiness_consumer_probe_ref": consumer_probe_ref,
            "startup_readiness_parent_budget_seconds": self.parent_budget_seconds,
            "startup_readiness_governed_process_call_seconds": (self.governed_process_call_seconds),
        }


@dataclass(frozen=True)
class ContractBundle:
    """Validated contract bytes used by one new transaction."""

    directory: Path
    flow: dict[str, JsonValue]
    flow_schema: dict[str, JsonValue]
    answers_schema: dict[str, JsonValue]
    journal_schema: dict[str, JsonValue]
    adapter_schema: dict[str, JsonValue]
    permission_manifest: dict[str, JsonValue]
    source_revision: str
    contract_digest: str
    resolution: ContractResolution

    @classmethod
    def load(
        cls,
        *,
        source_revision: str,
        directory: Path | None = None,
        expected_digest: str | None = None,
        resume_compatibility: bool = False,
    ) -> ContractBundle:
        resolved = _resolve_contract_directory(directory, explicit_origin="explicit")
        return cls._load_from_directory(
            source_revision=source_revision,
            resolved=resolved,
            expected_digest=expected_digest,
            resume_compatibility=resume_compatibility,
        )

    @classmethod
    def _load_from_directory(
        cls,
        *,
        source_revision: str,
        resolved: _ContractDirectory,
        expected_digest: str | None,
        resume_compatibility: bool,
    ) -> ContractBundle:
        root = resolved.path
        loaded = {name: _load_object(root / name) for name in _CONTRACT_FILENAMES}
        digest = contract_digest(root)
        if expected_digest is not None and digest != expected_digest:
            raise ContractError(
                "pinned setup contract identity mismatch: "
                f"recorded={expected_digest}, observed={digest}",
                repair=(
                    "Restore the managed target's recorded release bytes; do not "
                    "resume it under formula contracts from another release."
                ),
            )
        flow = loaded["macos_setup_flow.json"]
        if resume_compatibility:
            flow = _normalize_legacy_resume_flow_v1(digest, flow)
        bundle = cls(
            directory=root,
            flow=flow,
            flow_schema=loaded["setup_flow.schema.json"],
            answers_schema=loaded["setup_answers.schema.json"],
            journal_schema=loaded["setup_journal.schema.json"],
            adapter_schema=loaded["setup_adapter_envelope.schema.json"],
            permission_manifest=loaded[PERMISSIONS_MANIFEST_FILENAME],
            source_revision=source_revision,
            contract_digest=digest,
            resolution=ContractResolution(
                path=root,
                origin=resolved.origin,
                flow_id=str(flow["flow_id"]),
                digest=digest,
            ),
        )
        bundle.validate()
        return bundle

    def validate(self) -> None:
        validate_contract_bundle(self)

    @property
    def flow_id(self) -> str:
        return str(self.flow["flow_id"])

    @property
    def stages(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "stages")

    @property
    def operations(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "operations")

    @property
    def probes(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "probes")

    @property
    def decisions(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "decisions")

    @property
    def consents(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "consents")

    @property
    def inputs(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "inputs")

    @property
    def dependencies(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "dependencies")

    @property
    def requirements(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "requirements")

    @property
    def components(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "components")

    @property
    def plugins(self) -> dict[str, dict[str, JsonValue]]:
        return _registry(self.flow, "plugins")

    @property
    def completion_probe_ids(self) -> tuple[str, ...]:
        completion = _object(self.flow, "completion")
        return _string_tuple(
            completion.get("required_probe_refs"),
            "completion.required_probe_refs",
        )

    @property
    def start_command(self) -> dict[str, JsonValue]:
        executor_contracts = _object(self.flow, "executor_contracts")
        return _json_object(
            executor_contracts.get("start_command"),
            "executor_contracts.start_command",
        )



def knowledge_readiness_budget(bundle: ContractBundle) -> int:
    """Return the separately declared parent timeout for knowledge stage exit."""
    # Older pinned schemas use the ordinary boundary timeout. A schema that
    # declares the dedicated policy must provide it; malformed policy never
    # takes this historical execution path.
    executor = _object(bundle.flow, "executor_contracts")
    if not declares_knowledge_readiness(bundle.flow_schema) and "knowledge_readiness" not in executor:
        return 30
    validate_knowledge_readiness(bundle.flow)
    source = "executor_contracts.knowledge_readiness"
    readiness = _json_object(_object(bundle.flow, "executor_contracts").get("knowledge_readiness"), source)
    return _integer(readiness, "timeout_seconds", source)

def startup_readiness_budget(bundle: ContractBundle) -> StartupReadinessBudget:
    """Return validated startup-readiness authority and consumer lineage."""
    start = bundle.start_command
    source = "executor_contracts.start_command.startup_readiness"
    readiness = _json_object(start.get("startup_readiness"), source)
    reservations = _json_object(
        readiness.get("downstream_reservations"),
        f"{source}.downstream_reservations",
    )
    return StartupReadinessBudget(
        contract_version=_integer(readiness, "contract_version", source),
        contract_digest=bundle.contract_digest,
        source_artifact="macos_setup_flow.json",
        budget_source=_string(readiness, "budget_source", source),
        budget_unit=_string(readiness, "budget_unit", source),
        semantic_scope=_string(readiness, "semantic_scope", source),
        release_signal=_string(readiness, "release_signal", source),
        consumer_probe_purposes=_string_tuple(
            readiness.get("consumer_probe_purposes"),
            f"{source}.consumer_probe_purposes",
        ),
        consumer_probe_refs=_string_tuple(
            readiness.get("consumer_probe_refs"),
            f"{source}.consumer_probe_refs",
        ),
        parent_budget_seconds=_integer(
            start,
            "timeout_seconds",
            "executor_contracts.start_command",
        ),
        governed_process_call_seconds=_integer(
            reservations,
            "governed_process_call_seconds",
            f"{source}.downstream_reservations",
        ),
    )


def load_reconciliation_destination_bundle(
    *,
    source_revision: str,
    development_directory: Path | None,
) -> ContractBundle:
    """Load the production bundle or one explicit development bundle, never a fallback."""

    resolved = _resolve_contract_directory(
        development_directory,
        explicit_origin="explicit_development",
    )
    return ContractBundle._load_from_directory(
        source_revision=source_revision,
        resolved=resolved,
        expected_digest=None,
        resume_compatibility=False,
    )


def discover_contract_directory(explicit: Path | None) -> Path:
    """Return a hard-pinned production or explicit development contract directory."""

    return _resolve_contract_directory(
        explicit,
        explicit_origin="explicit_development",
    ).path


def _resolve_contract_directory(
    explicit: Path | None,
    *,
    explicit_origin: Literal["explicit", "explicit_development"],
) -> _ContractDirectory:
    if explicit is not None:
        candidate = explicit.expanduser().resolve(strict=False)
        _require_contract_files(candidate, origin=explicit_origin)
        return _ContractDirectory(path=candidate, origin=explicit_origin)
    candidate = Path(sys.prefix) / "share" / "solet" / "contracts"
    _require_contract_files(candidate, origin="production")
    return _ContractDirectory(path=candidate, origin="production")


def _require_contract_files(
    directory: Path,
    *,
    origin: Literal["production", "explicit", "explicit_development"],
) -> None:
    missing = tuple(name for name in _CONTRACT_FILENAMES if not (directory / name).is_file())
    if not missing:
        return
    rendered_missing = ", ".join(missing)
    repair = (
        "Reinstall the formula contracts."
        if origin == "production"
        else "Pass a reviewed development contract directory containing the complete bundle."
    )
    raise ContractError(
        f"{origin} setup contract directory is incomplete at {directory}; "
        f"missing: {rendered_missing}",
        repair=repair,
    )


def target_contract_directory(target: Path) -> Path:
    """Return the canonical contract directory inside one materialized seed."""

    return target / "plugins" / "github_midwife_plugin" / "knowledge_base"


def contract_digest(root: Path) -> str:
    """Digest the create-flow contract files materialised under ``root``."""
    files: dict[str, bytes] = {}
    for name in _DIGESTED_CONTRACT_FILENAMES:
        path = root / name
        try:
            files[name] = path.read_bytes()
        except OSError as exc:
            raise ContractError(f"contract {path} cannot be hashed: {exc}") from exc
    return contract_digest_from_bytes(files)


def contract_digest_from_bytes(files: Mapping[str, bytes]) -> str:
    """Digest a closed set of contract files by name and bytes.

    The encoding is ``name NUL bytes NUL`` in sorted filename order, exactly
    what :func:`contract_digest` has always produced for a directory, so a
    bundle read from Git blobs and a bundle read from a directory digest
    identically.  The caller decides which closed filename set is in play;
    this function never adds or drops a member.
    """
    if not files:
        raise ContractError("contract digest requires at least one file")
    digest = hashlib.sha256()
    for name in sorted(files):
        if not name or "/" in name:
            raise ContractError(f"contract filename is not a bare name: {name!r}")
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def transition_bundle_filenames() -> tuple[str, ...]:
    """Return the closed existing-install transition bundle file set.

    Distinct from the create bundle: it never includes ``macos_setup_flow.json``
    and shares only the wire-envelope schema with it.
    """

    return _TRANSITION_BUNDLE_FILENAMES


def _load_object(path: Path) -> dict[str, JsonValue]:
    try:
        raw = cast(JsonValue, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"contract {path} is unreadable or invalid: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"contract {path} must be one JSON object")
    return raw


def _normalize_legacy_resume_flow_v1(
    contract_digest: str,
    flow: dict[str, JsonValue],
) -> dict[str, JsonValue]:
    """Repair only the known v1 legacy shape while reading its pinned journal flow."""

    if contract_digest not in _LEGACY_RESUME_CONTRACT_DIGESTS:
        return flow
    if not _is_known_legacy_resume_shape(flow):
        return flow
    normalized = deepcopy(flow)
    normalized_operations = cast(
        dict[str, dict[str, dict[str, JsonValue]]], normalized["operations"]
    )
    normalized_decisions = cast(dict[str, dict[str, dict[str, JsonValue]]], normalized["decisions"])
    normalized_operations["install_python_runtime"]["idempotency"]["postcondition_probe_refs"] = [
        "python_version_valid"
    ]
    normalized_operations["install_postgresql"]["idempotency"]["postcondition_probe_refs"] = [
        "postgres_binary_version_valid"
    ]
    normalized_operations["install_postgresql"]["idempotency"]["precondition_probe_refs"] = [
        "postgres_binary_version_valid",
        "postgres_ready",
        "pgvector_ready",
    ]
    normalized_operations["configure_postgresql"]["idempotency"]["precondition_probe_refs"] = [
        "postgres_role_policy_valid",
        "postgres_ready",
        "pgvector_ready",
    ]
    normalized_operations["install_shell_integration"]["idempotency"]["precondition_probe_refs"] = [
        "fresh_shell_path_valid",
        "fresh_shell_python_valid",
    ]
    normalized_operations["configure_lm_studio_embeddings"]["idempotency"][
        "precondition_probe_refs"
    ] = ["embedding_request_succeeds"]
    normalized_operations["configure_lm_studio_inference"]["idempotency"][
        "precondition_probe_refs"
    ] = []
    normalized_operations["configure_lm_studio_inference"]["idempotency"][
        "postcondition_probe_refs"
    ] = []
    candidate_contract = cast(
        dict[str, JsonValue],
        normalized_decisions["inference_model"]["option_source"]["candidate_contract"],
    )
    candidate_contract["qualification_probe_refs"] = []
    normalized_operations["open_background_items_settings"]["idempotency"][
        "postcondition_probe_refs"
    ] = []
    return normalized


def _is_known_legacy_resume_shape(flow: dict[str, JsonValue]) -> bool:
    operations = flow.get("operations")
    probes = flow.get("probes")
    if not isinstance(operations, dict) or not isinstance(probes, dict):
        return False
    return all(
        _operation_has_postconditions(operations, operation_id, expected)
        for operation_id, expected in (
            ("install_python_runtime", ["python_version_valid", "fresh_shell_python_valid"]),
            ("install_postgresql", ["postgres_binary_version_valid", "pgvector_ready"]),
            (
                "configure_postgresql",
                ["postgres_ready", "postgres_role_policy_valid", "pgvector_ready"],
            ),
            ("install_shell_integration", ["fresh_shell_path_valid", "fresh_shell_python_valid"]),
            ("configure_lm_studio_embeddings", ["embedding_request_succeeds"]),
            ("configure_lm_studio_inference", ["structured_action_qualification"]),
            ("open_background_items_settings", ["launchagent_running"]),
        )
    ) and all(
        _probe_has_remediations(probes, probe_id, expected)
        for probe_id, expected in (
            (
                "fresh_shell_python_valid",
                ["install_python_runtime", "install_shell_integration"],
            ),
            ("pgvector_ready", ["install_postgresql", "configure_postgresql"]),
            ("launchagent_running", ["install_launchagent"]),
        )
    )


def _operation_has_postconditions(
    operations: dict[str, JsonValue],
    operation_id: str,
    expected: list[str],
) -> bool:
    operation = operations.get(operation_id)
    if not isinstance(operation, dict):
        return False
    idempotency = operation.get("idempotency")
    return isinstance(idempotency, dict) and idempotency.get("postcondition_probe_refs") == expected


def _probe_has_remediations(
    probes: dict[str, JsonValue],
    probe_id: str,
    expected: list[str],
) -> bool:
    probe = probes.get(probe_id)
    return isinstance(probe, dict) and probe.get("remediation_operation_refs") == expected


def _object(value: dict[str, JsonValue], key: str) -> dict[str, JsonValue]:
    return _json_object(value.get(key), key)


def _json_object(value: JsonValue, label: str) -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be an object")
    return value


def _registry(
    value: dict[str, JsonValue],
    key: str,
) -> dict[str, dict[str, JsonValue]]:
    raw = _object(value, key)
    parsed: dict[str, dict[str, JsonValue]] = {}
    for item_id, item in raw.items():
        if not isinstance(item, dict):
            raise ContractError(f"{key}.{item_id} must be an object")
        parsed[item_id] = item
    return parsed


def _string_tuple(value: JsonValue, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ContractError(f"{label} must be a string array")
    return tuple(item for item in value if isinstance(item, str))


def _string(value: dict[str, JsonValue], key: str, label: str) -> str:
    item = value.get(key)
    if not isinstance(item, str):
        raise ContractError(f"{label}.{key} must be a string")
    return item


def _integer(value: dict[str, JsonValue], key: str, label: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ContractError(f"{label}.{key} must be an integer")
    return item
