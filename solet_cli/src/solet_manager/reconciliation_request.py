"""Producer of the fixed ``macos.target_reconciliation`` wire envelope.

``existing::lifecycle.cutover`` is a Manager-side producer of the seed's
existing reconciliation contract (design section 7.2, review D4/D7).  The
seed's ``TargetReconciliationRequest._REQUEST_KEYS`` is an exact-membership
set, so the envelope carries precisely those nineteen keys and nothing else.
The ``CutoverTerms`` record the Manager fingerprints is a superset: its
``active_color``, ``adapter_module_sha256`` and ``adapter_module_replaced``
members back the approval and never travel on the wire.  The two tables are
kept separate here so a literal implementation cannot conflate them.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from .cutover_receipts import CutoverTerms
from .errors import AdapterProtocolError, StateConflictError
from .models import JsonValue

__all__ = [
    "RECONCILIATION_FLOW_ID",
    "RECONCILIATION_OPERATION_REF",
    "RECONCILIATION_PHASES",
    "RECONCILIATION_SCHEMA_VERSION",
    "RECONCILIATION_WIRE_KEYS",
    "TERMS_ONLY_MEMBERS",
    "ReconciliationOutcome",
    "build_reconciliation_envelope",
    "parse_reconciliation_response",
]

RECONCILIATION_FLOW_ID = "macos.target_reconciliation"
RECONCILIATION_OPERATION_REF = "reconcile::runtime.cutover"
RECONCILIATION_SCHEMA_VERSION = 1
RECONCILIATION_PHASES = frozenset({"probe", "apply", "recover"})
#: The exact seed wire table.  Membership is what the seed refuses on; a
#: smoke serialises a Manager envelope and proves equality against the seed's
#: own ``_REQUEST_KEYS`` byte-for-byte.
RECONCILIATION_WIRE_KEYS = frozenset(
    {
        "schema_version",
        "flow_id",
        "operation_ref",
        "phase",
        "name",
        "target_realpath",
        "reconciliation_id",
        "approved_fingerprint",
        "expected_source_surface_sha256",
        "expected_release_surface_sha256",
        "expected_manifest_etag",
        "expected_current_release_id",
        "expected_active_instance_id",
        "expected_active_start_token",
        "launch_topology",
        "expected_launchagent_label",
        "expected_launchagent_plist_sha256",
        "verification_modules",
        "timeout_seconds",
    }
)
#: ``CutoverTerms`` members that back ``approved_fingerprint`` but are not wire fields.
TERMS_ONLY_MEMBERS = frozenset({"active_color", "adapter_module_sha256", "adapter_module_replaced"})
_RECONCILIATION_ID = re.compile(r"^rec_[0-9a-f]{1,64}$")
_FINGERPRINT = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class ReconciliationOutcome:
    """The seed's closed response envelope, parsed."""

    status: str
    error_kind: str | None
    message: str | None
    phase: str | None
    observed_launch_topology: str | None
    observed_launchagent_plist_sha256: str | None
    approved_fingerprint: str | None
    reconciliation_id: str | None
    probed: bool
    mutated: bool | None
    cutover: dict[str, JsonValue] | None

    @property
    def refused(self) -> bool:
        return self.status != "ok"


def build_reconciliation_envelope(
    *,
    phase: str,
    name: str,
    target_realpath: str,
    reconciliation_id: str,
    approved_fingerprint: str,
    terms: CutoverTerms,
    timeout_seconds: int,
) -> dict[str, JsonValue]:
    """Assemble exactly the nineteen wire fields from constants, identity, and terms."""
    if phase not in RECONCILIATION_PHASES:
        raise StateConflictError(f"reconciliation phase is outside the closed set: {phase!r}")
    if not name or not Path(target_realpath).is_absolute():
        raise StateConflictError("reconciliation envelope identity is incomplete")
    if _RECONCILIATION_ID.fullmatch(reconciliation_id) is None:
        raise StateConflictError("reconciliation_id must match rec_<hex>")
    if _FINGERPRINT.fullmatch(approved_fingerprint) is None:
        raise StateConflictError("approved_fingerprint must be a sha256 digest")
    if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise StateConflictError("timeout_seconds must be a positive integer")
    envelope: dict[str, JsonValue] = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "flow_id": RECONCILIATION_FLOW_ID,
        "operation_ref": RECONCILIATION_OPERATION_REF,
        "phase": phase,
        "name": name,
        "target_realpath": target_realpath,
        "reconciliation_id": reconciliation_id,
        "approved_fingerprint": approved_fingerprint,
        "expected_source_surface_sha256": terms.source_surface_sha256,
        "expected_release_surface_sha256": terms.release_surface_sha256,
        "expected_manifest_etag": terms.manifest_etag,
        "expected_current_release_id": terms.current_release_id,
        "expected_active_instance_id": terms.active_instance_id,
        "expected_active_start_token": terms.active_start_token,
        "launch_topology": terms.launch_topology,
        "expected_launchagent_label": terms.launchagent_label,
        "expected_launchagent_plist_sha256": terms.launchagent_plist_sha256,
        "verification_modules": list(terms.verification_modules),
        "timeout_seconds": timeout_seconds,
    }
    if frozenset(envelope) != RECONCILIATION_WIRE_KEYS:
        raise StateConflictError("reconciliation envelope does not match the exact wire table")
    if TERMS_ONLY_MEMBERS & frozenset(envelope):
        raise StateConflictError("a CutoverTerms-only member leaked onto the wire")
    return envelope


def parse_reconciliation_response(raw_text: str) -> ReconciliationOutcome:
    """Parse the seed's envelope; a refusal is data, a malformed reply is a protocol error."""
    try:
        raw = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise AdapterProtocolError(f"reconciliation adapter stdout is not one JSON object: {exc}") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != RECONCILIATION_SCHEMA_VERSION or raw.get("flow_id") != RECONCILIATION_FLOW_ID:
        raise AdapterProtocolError("reconciliation adapter reply does not carry the fixed flow identity")
    value = cast(dict[str, JsonValue], raw)
    status = value.get("status")
    if status == "blocked":
        return _refusal(value)
    if status != "ok":
        raise AdapterProtocolError("reconciliation adapter reply status is outside the closed set")
    result = value.get("result")
    if not isinstance(result, dict):
        raise AdapterProtocolError("reconciliation adapter reply lacks its result object")
    cutover = result.get("cutover")
    mutated_raw = result.get("mutated")
    mutated: bool | None = mutated_raw if isinstance(mutated_raw, bool) else None
    return ReconciliationOutcome(
        "ok",
        None,
        None,
        _optional_text(result.get("phase")),
        _optional_text(result.get("observed_launch_topology")),
        _optional_text(result.get("observed_launchagent_plist_sha256")),
        _optional_text(result.get("approved_fingerprint")),
        _optional_text(result.get("reconciliation_id")),
        result.get("status") == "probed",
        mutated,
        cutover if isinstance(cutover, dict) else None,
    )


def _refusal(value: dict[str, JsonValue]) -> ReconciliationOutcome:
    error_kind, message = value.get("error_kind"), value.get("message")
    if not isinstance(error_kind, str) or not error_kind or not isinstance(message, str):
        raise AdapterProtocolError("reconciliation refusal lacks its machine-readable kind")
    return ReconciliationOutcome("blocked", error_kind, message, None, None, None, None, None, False, None, None)


def _optional_text(value: JsonValue) -> str | None:
    return value if isinstance(value, str) and value else None
