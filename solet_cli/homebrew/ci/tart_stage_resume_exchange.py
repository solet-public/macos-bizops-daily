#!/usr/bin/env python3
"""Closed request/receipt exchange for one real-tool stage-resume adapter run.

This is deliberately a host-side executor.  It prepares an immutable request
for a guest that already has custody, and validates the receipt the guest puts
on the writable receipts share.  It never starts Tart or mutates a guest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final, cast

from tart_stage_resume_contract import ContractError, sha256

_ADAPTER_PROBES: Final[dict[str, tuple[str, ...]]] = {
    "homebrew": ("homebrew_available",),
    "lm_studio": (
        "lm_studio_cli_available",
        "lm_studio_server_ready",
        "lm_studio_embedding_artifact_present",
        "lm_studio_embedding_model_served",
        "lm_studio_inference_artifact_present",
        "lm_studio_inference_model_served",
        "lm_studio_login_agent_valid",
        "lm_studio_jit_disabled",
    ),
    "postgresql": (
        "postgres_binary_version_valid",
        "postgres_ready",
        "pgvector_package_available",
        "postgres_role_policy_valid",
        "pgvector_ready",
    ),
    "launchd": ("launchagent_running",),
}
_TOOLS: Final[dict[str, str]] = {
    "homebrew": "brew",
    "lm_studio": "lms",
    "postgresql": "psql",
    "launchd": "launchctl",
}
_EXCHANGE_KEYS = frozenset(
    {"schema_version", "adapter", "flow_id", "flow_sha256", "tool", "probes", "operations"}
)
_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "adapter",
        "exchange_sha256",
        "flow_sha256",
        "tool",
        "tool_version",
        "probes",
    }
)
_PROBE_KEYS = frozenset({"id", "runner", "probe_ref"})
_OPERATION_KEYS = frozenset({"id", "runner", "operation_ref"})
_RECEIPT_PROBE_KEYS = frozenset({"id", "runner", "probe_ref", "checkpoint_status"})
_ACCEPTED_STATUSES = frozenset({"verified", "not_applicable"})


def adapter_exchange(flow_path: Path, adapter: str) -> dict[str, object]:
    """Build a guest request from one installed flow's declared entries."""

    if adapter not in _ADAPTER_PROBES:
        raise ContractError(f"unknown adapter: {adapter!r}")
    flow = _load_flow(flow_path)
    flow_id = flow.get("flow_id")
    probes = flow.get("probes")
    operations = flow.get("operations")
    if (
        not isinstance(flow_id, str)
        or not isinstance(probes, dict)
        or not isinstance(operations, dict)
    ):
        raise ContractError("installed flow lacks its executable probe/operation declarations")
    flow_probes = cast(dict[str, object], probes)
    flow_operations = cast(dict[str, object], operations)
    selected = _selected_probes(flow_probes, _ADAPTER_PROBES[adapter])
    required_operation_ids = {
        operation_id
        for probe in selected
        for operation_id in _remediation_ids(flow_probes[probe["id"]])
    }
    selected_operations = _selected_operations(flow_operations, required_operation_ids)
    if {item["id"] for item in selected_operations} != required_operation_ids:
        raise ContractError("selected adapter probe remediation is absent from the installed flow")
    return {
        "schema_version": 1,
        "adapter": adapter,
        "flow_id": flow_id,
        "flow_sha256": sha256(flow_path),
        "tool": _TOOLS[adapter],
        "probes": selected,
        "operations": selected_operations,
    }


def write_exchange(path: Path, exchange: dict[str, object]) -> None:
    """Persist only a closed guest exchange request."""

    _validate_exchange(exchange)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(exchange, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def read_exchange(path: Path) -> dict[str, object]:
    """Load a closed exchange before a guest acts on its declared probes."""

    exchange = _load_object(path, "exchange")
    _validate_exchange(exchange)
    return exchange


def validate_guest_receipt(exchange_path: Path, receipt_path: Path) -> dict[str, object]:
    """Validate and return a receipt bound to the exact staged exchange bytes."""

    exchange = read_exchange(exchange_path)
    receipt = _load_object(receipt_path, "receipt")
    _validate_receipt_header(exchange, receipt, exchange_path)
    _validate_tool_version(receipt)
    _validate_receipt_probes(exchange, receipt)
    return receipt


def _validate_receipt_header(
    exchange: dict[str, object], receipt: dict[str, object], exchange_path: Path
) -> None:
    if frozenset(receipt) != _RECEIPT_KEYS or receipt.get("schema_version") != 1:
        raise ContractError("guest receipt does not match the closed v1 shape")
    for field in ("adapter", "flow_sha256", "tool"):
        if receipt.get(field) != exchange[field]:
            raise ContractError(f"guest receipt {field} does not match its staged exchange")
    if receipt.get("exchange_sha256") != sha256(exchange_path):
        raise ContractError("guest receipt does not bind the staged exchange bytes")


def _validate_tool_version(receipt: dict[str, object]) -> None:
    tool_version = receipt.get("tool_version")
    if (
        not isinstance(tool_version, str)
        or not tool_version.strip()
        or len(tool_version) > 512
        or "\x00" in tool_version
    ):
        raise ContractError("guest receipt lacks a bounded real-tool version")


def _validate_receipt_probes(exchange: dict[str, object], receipt: dict[str, object]) -> None:
    expected = exchange["probes"]
    observed = receipt.get("probes")
    if not isinstance(expected, list) or not isinstance(observed, list):
        raise ContractError("guest receipt probe list does not match the staged exchange")
    expected_probes = cast(list[object], expected)
    observed_probes = cast(list[object], observed)
    if len(expected_probes) != len(observed_probes):
        raise ContractError("guest receipt probe list does not match the staged exchange")
    for expected_probe, observed_probe in zip(expected_probes, observed_probes, strict=True):
        _validate_receipt_probe(expected_probe, observed_probe)


def _validate_receipt_probe(expected_probe: object, observed_probe: object) -> None:
    if not isinstance(observed_probe, dict):
        raise ContractError("guest receipt probe has an unexpected shape")
    observed_fields = cast(dict[str, object], observed_probe)
    if frozenset(observed_fields) != _RECEIPT_PROBE_KEYS:
        raise ContractError("guest receipt probe has an unexpected shape")
    if not isinstance(expected_probe, dict):
        raise AssertionError("validated exchange probe is not an object")
    expected_fields = cast(dict[str, object], expected_probe)
    for field in ("id", "runner", "probe_ref"):
        if observed_fields.get(field) != expected_fields[field]:
            raise ContractError(
                f"guest receipt probe {field} differs from the staged flow declaration"
            )
    if observed_fields.get("checkpoint_status") not in _ACCEPTED_STATUSES:
        raise ContractError("guest receipt contains an unverified adapter probe")


def _load_flow(path: Path) -> dict[str, object]:
    return _load_object(path, "installed flow")


def _load_object(path: Path, label: str) -> dict[str, object]:
    try:
        value = cast(object, json.loads(path.read_text(encoding="utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ContractError(f"{label} cannot be read: {exc}") from exc
    if not isinstance(value, dict):
        raise ContractError(f"{label} must be a JSON object")
    return cast(dict[str, object], value)


def _selected_probes(
    probes: dict[str, object], identifiers: tuple[str, ...]
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for probe_id in identifiers:
        raw = probes.get(probe_id)
        if not isinstance(raw, dict):
            raise ContractError(f"installed flow lacks adapter probe {probe_id!r}")
        fields = cast(dict[str, object], raw)
        runner, probe_ref = fields.get("runner"), fields.get("probe_ref")
        if not isinstance(runner, str) or not isinstance(probe_ref, str):
            raise ContractError(f"installed flow probe {probe_id!r} is not executable")
        selected.append({"id": probe_id, "runner": runner, "probe_ref": probe_ref})
    return selected


def _remediation_ids(probe: object) -> tuple[str, ...]:
    if not isinstance(probe, dict):
        raise AssertionError("selected probe was not an object")
    values = cast(dict[str, object], probe).get("remediation_operation_refs")
    if not isinstance(values, list):
        raise ContractError("installed flow adapter probe has invalid remediation references")
    remediation_ids = cast(list[object], values)
    if not all(isinstance(value, str) for value in remediation_ids):
        raise ContractError("installed flow adapter probe has invalid remediation references")
    return tuple(cast(str, value) for value in remediation_ids)


def _selected_operations(
    operations: dict[str, object], required: set[str]
) -> list[dict[str, str]]:
    selected: list[dict[str, str]] = []
    for operation_id, raw in operations.items():
        if operation_id not in required:
            continue
        if not isinstance(raw, dict):
            raise ContractError("installed flow remediation operation is not an object")
        fields = cast(dict[str, object], raw)
        runner, operation_ref = fields.get("runner"), fields.get("operation_ref")
        if not isinstance(runner, str) or not isinstance(operation_ref, str):
            raise ContractError("installed flow remediation operation is not executable")
        selected.append({"id": operation_id, "runner": runner, "operation_ref": operation_ref})
    return selected


def _validate_exchange(exchange: dict[str, object]) -> None:
    adapter = _validate_exchange_header(exchange)
    probes, operations = _validate_exchange_declarations(exchange)
    _validate_exchange_probes(adapter, probes)
    _validate_exchange_operations(operations)


def _validate_exchange_header(exchange: dict[str, object]) -> str:
    if frozenset(exchange) != _EXCHANGE_KEYS or exchange.get("schema_version") != 1:
        raise ContractError("adapter exchange does not match the closed v1 shape")
    adapter = exchange.get("adapter")
    if not isinstance(adapter, str) or adapter not in _ADAPTER_PROBES:
        raise ContractError("adapter exchange has an invalid adapter/tool pair")
    if exchange.get("tool") != _TOOLS[adapter]:
        raise ContractError("adapter exchange has an invalid adapter/tool pair")
    if not isinstance(exchange.get("flow_id"), str) or not _sha256(exchange.get("flow_sha256")):
        raise ContractError("adapter exchange has an invalid flow identity")
    return adapter


def _validate_exchange_declarations(exchange: dict[str, object]) -> tuple[list[object], list[object]]:
    probes_value, operations_value = exchange.get("probes"), exchange.get("operations")
    if not isinstance(probes_value, list) or not isinstance(operations_value, list):
        raise ContractError("adapter exchange has invalid declarations")
    probes = cast(list[object], probes_value)
    operations = cast(list[object], operations_value)
    return probes, operations


def _validate_exchange_probes(adapter: str, probes: list[object]) -> None:
    probe_ids = [
        cast(dict[str, object], item).get("id")
        for item in probes
        if isinstance(item, dict)
    ]
    if probe_ids != list(_ADAPTER_PROBES[adapter]):
        raise ContractError("adapter exchange probe selection is not the closed adapter set")
    for item in probes:
        if not isinstance(item, dict):
            raise ContractError("adapter exchange probe is invalid")
        fields = cast(dict[str, object], item)
        if frozenset(fields) != _PROBE_KEYS or not all(
            isinstance(fields.get(key), str) for key in _PROBE_KEYS
        ):
            raise ContractError("adapter exchange probe is invalid")


def _validate_exchange_operations(operations: list[object]) -> None:
    for item in operations:
        if not isinstance(item, dict):
            raise ContractError("adapter exchange operation is invalid")
        fields = cast(dict[str, object], item)
        if frozenset(fields) != _OPERATION_KEYS or not all(
            isinstance(fields.get(key), str) for key in _OPERATION_KEYS
        ):
            raise ContractError("adapter exchange operation is invalid")


def _sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in value[7:])
    )
