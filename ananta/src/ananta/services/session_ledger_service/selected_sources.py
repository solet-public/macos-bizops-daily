"""Truthful, bounded selected-session-source qualification helpers."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from solet_setup_contracts.selected_source_record import SelectedSourceContractError

from solet_setup_contracts import (
    canonical_sha256,
    load_selected_source_transaction,
    resolve_transaction_path,
    validate_selected_source_answers,
    validate_target_contract_identity,
)


@dataclass(frozen=True, slots=True)
class SelectedSourceRecord:
    """Validated setup-answer projection from the manager transaction."""

    record_source: str
    target: str
    name: str
    answers: dict[str, Any]
    answers_fingerprint: str


class SelectedSourceRecordError(RuntimeError):
    """The manager-owned answer record could not be used as proof."""


_SOURCE_SPECS = (
    ("codex_local", "codex_session_ingestion_consent", "codex_local"),
    ("claude_code_local", "claude_session_ingestion_consent", "claude_code_local"),
)


def load_selected_source_record(*, target: Path, name: str) -> SelectedSourceRecord:
    """Load and schema-validate one manager transaction's recorded answers."""

    try:
        transaction = load_selected_source_transaction(resolve_transaction_path(name))
    except SelectedSourceContractError as exc:
        raise SelectedSourceRecordError(f"manager_transaction_load_error: {exc}") from exc
    if transaction is None:
        raise SelectedSourceRecordError("manager_transaction_missing")
    if transaction.name != name:
        raise SelectedSourceRecordError("manager_transaction_identity_mismatch")
    try:
        validate_target_contract_identity(transaction, target=target)
        validate_selected_source_answers(transaction, target=target)
    except Exception as exc:
        raise SelectedSourceRecordError("manager_transaction_invalid") from exc
    return SelectedSourceRecord(
        record_source="manager_transaction",
        target=transaction.target,
        name=transaction.name,
        answers=transaction.answers,
        answers_fingerprint=transaction.answers_fingerprint,
    )


def qualify_selected_sources(
    *,
    target: Path,
    name: str,
    requested_answers_fingerprint: str,
    record: SelectedSourceRecord,
    registered_source_kinds: set[str],
    backfill_counts: dict[str, int],
    retrieval_by_kind: dict[str, bool],
) -> dict[str, Any]:
    """Return source-local facts only after the answer record is authenticated."""

    target_identity_matched = record.name == name and _same_path(record.target, target)
    observed_fingerprint = canonical_sha256(record.answers)
    answers_fingerprint_matched = (
        observed_fingerprint == record.answers_fingerprint
        and observed_fingerprint == requested_answers_fingerprint
    )
    result: dict[str, Any] = {
        "record_source": record.record_source,
        "target_identity_matched": target_identity_matched,
        "answers_fingerprint_matched": answers_fingerprint_matched,
        "qualification_reason": None,
        "sources": [],
    }
    if not target_identity_matched:
        result["qualification_reason"] = "manager_transaction_identity_mismatch"
        return result
    if not answers_fingerprint_matched:
        result["qualification_reason"] = "answers_fingerprint_mismatch"
        return result

    decisions = record.answers.get("decisions")
    consents = record.answers.get("consents")
    selected = _selected_sources(decisions)
    consented = _consented_sources(consents)
    result["sources"] = [
        {
            "source": source,
            "selected": source in selected,
            "consented": consent_key in consented,
            "registered": source_kind in registered_source_kinds,
            "backfill_count": backfill_counts.get(source_kind, 0),
            "retrieval_ok": retrieval_by_kind.get(source_kind, False),
        }
        for source, consent_key, source_kind in _SOURCE_SPECS
    ]
    return result


def selected_and_consented_source_kinds(record: SelectedSourceRecord) -> tuple[str, ...]:
    """Return the measured source kinds allowed to receive a retrieval probe."""

    decisions = record.answers.get("decisions")
    consents = record.answers.get("consents")
    selected = _selected_sources(decisions)
    consented = _consented_sources(consents)
    return tuple(
        source_kind
        for source, consent_key, source_kind in _SOURCE_SPECS
        if source in selected and consent_key in consented
    )


def _same_path(recorded: str, requested: Path) -> bool:
    try:
        return Path(recorded).resolve(strict=False) == requested.resolve(strict=False)
    except OSError:
        return False


def _selected_sources(decisions: object) -> set[str]:
    if not isinstance(decisions, dict):
        return set()
    selected = decisions.get("session_sources")
    if not isinstance(selected, list) or not all(isinstance(value, str) for value in selected):
        return set()
    return set(selected)


def _consented_sources(consents: object) -> set[str]:
    if not isinstance(consents, dict):
        return set()
    return {key for key, value in consents.items() if isinstance(key, str) and value is True}
