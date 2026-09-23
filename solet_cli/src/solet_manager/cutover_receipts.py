"""Target-local durable receipt primitives for cutover reconciliation.

The manager owns approval and byte replacement, but recovery evidence belongs
to the target: a manager can die while a target-local adapter is still
executing.  This module deliberately has no transport or deployment logic.  It
only makes the state machine durable and validates the fixed manager/target
wire facts supplied by the caller.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from .errors import StateConflictError, StateError
from .models import JsonValue
from .state_io import atomic_remove, atomic_write_json

__all__ = [
    "CutoverJournal",
    "CutoverReceiptStore",
    "CutoverRuntimeObservation",
    "CutoverTerms",
    "OperationCutoverReceipt",
    "SnapshotReceipt",
    "load_operation_cutover_receipt",
    "new_reconciliation_id",
    "terminal_receipt_digest",
]

type CutoverStage = Literal[
    "prepared",
    "bytes_applied",
    "bootstrap_restarted",
    "cutover_requested",
    "router_cutover_observed",
    "supervisor_restarted",
    "runtime_verified",
]
type TerminalStatus = Literal[
    "reconciled",
    "already_reconciled",
    "compensated_prior_verified",
    "failed_prior_serving",
    "needs_intervention",
    "approval_stale",
    "unsupported_cutover_vintage",
]

_STAGE_ORDER: dict[CutoverStage, int] = {
    "prepared": 0,
    "bytes_applied": 1,
    "bootstrap_restarted": 2,
    "cutover_requested": 3,
    "router_cutover_observed": 4,
    "supervisor_restarted": 5,
    "runtime_verified": 6,
}
_TERMINAL_STATUSES = frozenset(
    {
        "reconciled",
        "already_reconciled",
        "compensated_prior_verified",
        "failed_prior_serving",
        "needs_intervention",
        "approval_stale",
        "unsupported_cutover_vintage",
    }
)
_SHA256 = re.compile(r"sha256:[0-9a-f]{64}\Z")
_SNAPSHOT_RECEIPT_KEYS = frozenset(
    {"schema_version", "name", "target", "seed", "preview_fingerprint", "state", "files"}
)
_SNAPSHOT_STATES = frozenset({"prepared", "applied", "recovered"})


@dataclass(frozen=True)
class CutoverTerms:
    """Approval-bound facts for the one target-local cutover act."""

    current_release_id: str
    active_color: str
    active_instance_id: str
    active_start_token: str
    manifest_etag: str
    launch_topology: str
    launchagent_label: str
    launchagent_plist_sha256: str
    adapter_module_sha256: str
    adapter_module_replaced: bool
    source_surface_sha256: str
    release_surface_sha256: str
    verification_modules: tuple[str, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.current_release_id, "current_release_id"),
            (self.active_color, "active_color"),
            (self.active_instance_id, "active_instance_id"),
            (self.active_start_token, "active_start_token"),
            (self.manifest_etag, "manifest_etag"),
            (self.launchagent_label, "launchagent_label"),
            (self.adapter_module_sha256, "adapter_module_sha256"),
        ):
            if not value:
                raise StateConflictError(f"cutover {label} is empty")
        if self.launch_topology not in {"legacy_direct", "materialized_supervisor"}:
            raise StateConflictError("cutover launch_topology is unsupported")
        for value, label in (
            (self.launchagent_plist_sha256, "launchagent_plist_sha256"),
            (self.adapter_module_sha256, "adapter_module_sha256"),
            (self.source_surface_sha256, "source_surface_sha256"),
            (self.release_surface_sha256, "release_surface_sha256"),
        ):
            if _SHA256.fullmatch(value) is None:
                raise StateConflictError(f"cutover {label} is not a sha256 identity")
        if (
            not self.verification_modules
            or tuple(sorted(set(self.verification_modules))) != self.verification_modules
        ):
            raise StateConflictError(
                "cutover verification_modules must be non-empty, unique, and sorted"
            )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "mode": "cutover",
            "current_release_id": self.current_release_id,
            "active_color": self.active_color,
            "active_instance_id": self.active_instance_id,
            "active_start_token": self.active_start_token,
            "manifest_etag": self.manifest_etag,
            "launch_topology": self.launch_topology,
            "launchagent_label": self.launchagent_label,
            "launchagent_plist_sha256": self.launchagent_plist_sha256,
            "adapter_module_sha256": self.adapter_module_sha256,
            "adapter_module_replaced": self.adapter_module_replaced,
            "source_surface_sha256": self.source_surface_sha256,
            "release_surface_sha256": self.release_surface_sha256,
            "verification_modules": list(self.verification_modules),
        }


@dataclass(frozen=True)
class CutoverRuntimeObservation:
    """The bounded read-only evidence used to resolve an orphaned request."""

    reachable: bool
    release_id: str | None
    active_instance_id: str | None
    source_surface_sha256: str | None
    release_surface_sha256: str | None

    def proves_desired(self, terms: CutoverTerms) -> bool:
        return self.reachable and (
            self.active_instance_id is not None
            and self.source_surface_sha256 == terms.source_surface_sha256
            and self.release_surface_sha256 == terms.release_surface_sha256
            and self.release_id is not None
            and self.release_id != terms.current_release_id
        )

    def proves_prior_serving(self, terms: CutoverTerms) -> bool:
        return self.reachable and (
            self.release_id == terms.current_release_id
            and self.active_instance_id == terms.active_instance_id
        )


@dataclass(frozen=True)
class CutoverJournal:
    """One active, forward-only target-local cutover journal."""

    reconciliation_id: str
    name: str
    target: str
    fingerprint: str
    terms: CutoverTerms
    stage: CutoverStage
    events: tuple[dict[str, JsonValue], ...]
    files: tuple[dict[str, JsonValue], ...]

    def __post_init__(self) -> None:
        if not self.reconciliation_id.startswith("rec_"):
            raise StateError("cutover reconciliation_id is invalid")
        if not self.name or not Path(self.target).is_absolute():
            raise StateError("cutover journal identity is invalid")
        if _SHA256.fullmatch(self.fingerprint) is None:
            raise StateError("cutover journal fingerprint is invalid")
        if not self.events or self.events[-1].get("stage") != self.stage:
            raise StateError("cutover journal must end with its current stage")

    @classmethod
    def prepared(
        cls,
        *,
        reconciliation_id: str,
        name: str,
        target: Path,
        fingerprint: str,
        terms: CutoverTerms,
        files: tuple[dict[str, JsonValue], ...],
    ) -> CutoverJournal:
        return cls(
            reconciliation_id=reconciliation_id,
            name=name,
            target=str(target),
            fingerprint=fingerprint,
            terms=terms,
            stage="prepared",
            events=(_event("prepared"),),
            files=files,
        )

    def advance(
        self,
        stage: CutoverStage,
        *,
        evidence: dict[str, JsonValue] | None = None,
    ) -> CutoverJournal:
        if _STAGE_ORDER[stage] <= _STAGE_ORDER[self.stage]:
            raise StateConflictError(
                f"cutover journal cannot move from {self.stage!r} to {stage!r}"
            )
        return CutoverJournal(
            reconciliation_id=self.reconciliation_id,
            name=self.name,
            target=self.target,
            fingerprint=self.fingerprint,
            terms=self.terms,
            stage=stage,
            events=(*self.events, _event(stage, evidence=evidence)),
            files=self.files,
        )

    def to_dict(self) -> dict[str, JsonValue]:
        return {
            "schema_version": 2,
            "kind": "cutover_active_journal",
            "reconciliation_id": self.reconciliation_id,
            "name": self.name,
            "target": self.target,
            "fingerprint": self.fingerprint,
            "terms": self.terms.to_dict(),
            "stage": self.stage,
            "events": list(self.events),
            "files": list(self.files),
        }


@dataclass(frozen=True)
class SnapshotReceipt:
    """A readable v1 receipt that remains on the snapshot-recovery path.

    The v2 cutover journal never converts or overwrites this manager-private
    receipt. Authenticated byte recovery remains owned by adapter_reconciliation.
    """

    path: Path
    name: str
    target: str
    state: Literal["prepared", "applied", "recovered"]


type ActiveReceipt = CutoverJournal | SnapshotReceipt


class CutoverReceiptStore:
    """Write one fsync'd active journal and immutable per-id terminal receipt."""

    def __init__(self, target: Path, *, legacy_snapshot_path: Path | None = None) -> None:
        if not target.is_absolute() or target.is_symlink() or not target.is_dir():
            raise StateConflictError(f"cutover target is not a real directory: {target}")
        if legacy_snapshot_path is not None and not legacy_snapshot_path.is_absolute():
            raise StateConflictError("legacy snapshot receipt path must be absolute")
        self._target = target
        self._root = target / "profile" / "data" / "reconciliations" / "adapter"
        self._active = self._root / "active.json"
        self._receipts = self._root / "receipts"
        self._legacy_snapshot_path = legacy_snapshot_path

    @property
    def active_path(self) -> Path:
        return self._active

    def receipt_path(self, reconciliation_id: str) -> Path:
        if not reconciliation_id.startswith("rec_"):
            raise StateError("cutover reconciliation_id is invalid")
        return self._receipts / f"{reconciliation_id}.json"

    def write_active(self, journal: CutoverJournal) -> None:
        existing = self.load_active()
        if isinstance(existing, SnapshotReceipt):
            raise StateConflictError("v1 snapshot receipt requires snapshot-mode recovery")
        if existing is not None and existing.reconciliation_id != journal.reconciliation_id:
            raise StateConflictError("a different cutover reconciliation journal is already active")
        atomic_write_json(self._active, journal.to_dict())

    def load_active(self) -> ActiveReceipt | None:
        if self._active.exists():
            raw = _load_json(self._active)
            if raw.get("schema_version") == 2 and raw.get("kind") == "cutover_active_journal":
                return _journal_from_dict(raw)
            if raw.get("schema_version") == 1:
                return _snapshot_from_dict(raw, self._active, self._target)
            raise StateError("cutover active journal does not match schema v2")
        if self._legacy_snapshot_path is None or not self._legacy_snapshot_path.exists():
            return None
        return _snapshot_from_dict(
            _load_json(self._legacy_snapshot_path), self._legacy_snapshot_path, self._target
        )

    def finalize(self, journal: CutoverJournal, status: TerminalStatus) -> Path:
        if status not in _TERMINAL_STATUSES:
            raise StateError("cutover terminal status is invalid")
        path = self.receipt_path(journal.reconciliation_id)
        if path.exists():
            raise StateConflictError("cutover terminal receipt already exists")
        receipt = journal.to_dict()
        receipt["kind"] = "cutover_terminal_receipt"
        receipt["terminal"] = {"status": status, "at": _now(), "receipt_sha256": None}
        digest = _canonical_sha256(receipt)
        terminal = cast(dict[str, JsonValue], receipt["terminal"])
        terminal["receipt_sha256"] = digest
        atomic_write_json(path, receipt)
        atomic_remove(self._active)
        return path

    def recover_requested(
        self,
        observe: Callable[[CutoverJournal], CutoverRuntimeObservation],
    ) -> tuple[CutoverJournal, TerminalStatus | None]:
        journal = self.load_active()
        if not isinstance(journal, CutoverJournal) or journal.stage != "cutover_requested":
            raise StateConflictError("no cutover_requested active journal exists")
        observed = observe(journal)
        if observed.proves_desired(journal.terms):
            verified = journal.advance(
                "runtime_verified", evidence={"recovered_by_observation": True}
            )
            self.write_active(verified)
            return verified, "reconciled"
        if observed.proves_prior_serving(journal.terms):
            return journal, "failed_prior_serving"
        return journal, "needs_intervention"


def new_reconciliation_id() -> str:
    """Mint an opaque per-attempt identifier without deriving it from a target name."""

    return f"rec_{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class OperationCutoverReceipt:
    """One terminal cutover receipt keyed by the update operation that produced it (design section 7.2)."""

    operation_id: str
    reconciliation_id: str
    receipt_path: str
    receipt_sha256: str
    terminal_status: str


def terminal_receipt_digest(target: Path, reconciliation_id: str) -> str | None:
    """Digest of the seed-written terminal receipt for ``reconciliation_id``, or ``None`` when absent."""
    path = target / "profile" / "data" / "reconciliations" / "adapter" / "receipts" / f"{reconciliation_id}.json"
    if not reconciliation_id.startswith("rec_") or not path.is_file():
        return None
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def load_operation_cutover_receipt(target: Path, operation_id: str, reconciliation_id: str) -> OperationCutoverReceipt:
    """Read one terminal receipt and bind it to its update operation; refuses a malformed receipt."""
    path = target / "profile" / "data" / "reconciliations" / "adapter" / "receipts" / f"{reconciliation_id}.json"
    raw = _load_json(path)
    terminal = raw.get("terminal")
    if raw.get("kind") != "cutover_terminal_receipt" or raw.get("reconciliation_id") != reconciliation_id or not isinstance(terminal, dict):
        raise StateError(f"cutover receipt {path} does not match the terminal receipt shape")
    status = terminal.get("status")
    if not isinstance(status, str) or status not in _TERMINAL_STATUSES:
        raise StateError(f"cutover receipt {path} carries an unknown terminal status")
    digest = terminal_receipt_digest(target, reconciliation_id)
    if digest is None:
        raise StateError(f"cutover receipt {path} vanished during read")
    return OperationCutoverReceipt(operation_id, reconciliation_id, str(path), digest, status)


def _event(
    stage: CutoverStage,
    *,
    evidence: dict[str, JsonValue] | None = None,
) -> dict[str, JsonValue]:
    return {"stage": stage, "at": _now(), "evidence": {} if evidence is None else evidence}


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _load_json(path: Path) -> dict[str, JsonValue]:
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StateError(f"cutover receipt is unreadable: {exc}") from exc
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise StateError("cutover receipt must be a JSON object")
    return cast(dict[str, JsonValue], raw)


def _journal_from_dict(raw: dict[str, JsonValue]) -> CutoverJournal:
    try:
        terms_raw = cast(dict[str, JsonValue], raw["terms"])
        modules_raw = cast(list[str], terms_raw["verification_modules"])
        terms = CutoverTerms(
            current_release_id=cast(str, terms_raw["current_release_id"]),
            active_color=cast(str, terms_raw["active_color"]),
            active_instance_id=cast(str, terms_raw["active_instance_id"]),
            active_start_token=cast(str, terms_raw["active_start_token"]),
            manifest_etag=cast(str, terms_raw["manifest_etag"]),
            launch_topology=cast(str, terms_raw["launch_topology"]),
            launchagent_label=cast(str, terms_raw["launchagent_label"]),
            launchagent_plist_sha256=cast(str, terms_raw["launchagent_plist_sha256"]),
            adapter_module_sha256=cast(str, terms_raw["adapter_module_sha256"]),
            adapter_module_replaced=cast(bool, terms_raw["adapter_module_replaced"]),
            source_surface_sha256=cast(str, terms_raw["source_surface_sha256"]),
            release_surface_sha256=cast(str, terms_raw["release_surface_sha256"]),
            verification_modules=tuple(modules_raw),
        )
        stage = cast(CutoverStage, raw["stage"])
        if stage not in _STAGE_ORDER:
            raise ValueError("unknown stage")
        events = tuple(cast(list[dict[str, JsonValue]], raw["events"]))
        files = tuple(cast(list[dict[str, JsonValue]], raw["files"]))
        return CutoverJournal(
            reconciliation_id=cast(str, raw["reconciliation_id"]),
            name=cast(str, raw["name"]),
            target=cast(str, raw["target"]),
            fingerprint=cast(str, raw["fingerprint"]),
            terms=terms,
            stage=stage,
            events=events,
            files=files,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateError("cutover active journal does not match schema v2") from exc


def _snapshot_from_dict(raw: dict[str, JsonValue], path: Path, target: Path) -> SnapshotReceipt:
    if frozenset(raw) != _SNAPSHOT_RECEIPT_KEYS:
        raise StateError("v1 snapshot receipt does not match the closed schema")
    name, receipt_target, state = _snapshot_identity(raw, target)
    if not isinstance(raw.get("seed"), dict) or not isinstance(raw.get("files"), list):
        raise StateError("v1 snapshot receipt identity is invalid")
    fingerprint = raw.get("preview_fingerprint")
    if not isinstance(fingerprint, str) or _SHA256.fullmatch(fingerprint) is None:
        raise StateError("v1 snapshot receipt identity is invalid")
    return SnapshotReceipt(
        path=path,
        name=name,
        target=receipt_target,
        state=state,
    )


def _snapshot_identity(
    raw: dict[str, JsonValue], target: Path
) -> tuple[str, str, Literal["prepared", "applied", "recovered"]]:
    name = raw.get("name")
    receipt_target = raw.get("target")
    state = raw.get("state")
    if not isinstance(name, str) or not name:
        raise StateError("v1 snapshot receipt identity is invalid")
    if not isinstance(receipt_target, str) or not Path(receipt_target).is_absolute():
        raise StateError("v1 snapshot receipt identity is invalid")
    if Path(receipt_target).resolve(strict=False) != target.resolve(strict=True):
        raise StateError("v1 snapshot receipt identity is invalid")
    if state not in _SNAPSHOT_STATES:
        raise StateError("v1 snapshot receipt identity is invalid")
    return name, receipt_target, cast(Literal["prepared", "applied", "recovered"], state)


def _canonical_sha256(value: dict[str, JsonValue]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"
