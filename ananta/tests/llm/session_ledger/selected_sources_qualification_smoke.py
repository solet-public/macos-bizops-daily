#!/usr/bin/env python3
"""Hermetic contract smoke for selected-session-source qualification."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import cast

_REPO = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(_REPO / "ananta/src"))
sys.path.insert(0, str(_REPO / "solet_cli/src"))
sys.path.insert(0, str(_REPO / "solet_setup_contracts/src"))

from ananta.services.session_ledger_service.selected_sources import (  # noqa: E402
    SelectedSourceRecord,
    SelectedSourceRecordError,
    load_selected_source_record,
    qualify_selected_sources,
)
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402
from solet_manager.contracts import ContractBundle, target_contract_directory  # noqa: E402
from solet_manager.journal_migrations import CURRENT_JOURNAL_VERSION  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256, write_transaction  # noqa: E402
from solet_setup_contracts.selected_source_record import _SUPPORTED_JOURNAL_VERSIONS  # noqa: E402


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def _raises(error: type[BaseException], callback: object, message: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        return
    raise AssertionError(message)


def _record(*, target: Path) -> SelectedSourceRecord:
    answers = {
        "decisions": {"session_sources": ["codex_local"]},
        "consents": {
            "codex_session_ingestion_consent": True,
            "claude_session_ingestion_consent": False,
        },
    }
    return SelectedSourceRecord(
        record_source="manager_transaction",
        target=str(target),
        name="newborn",
        answers=answers,
        answers_fingerprint=canonical_sha256(answers),
    )


class _ClaudeCodeLocalLedger:
    """Minimal ledger double for the persisted selected-source round trip."""

    def __init__(self) -> None:
        self.queried_source_kinds: list[str] = []

    def list_sources(self) -> dict[str, object]:
        return {"sources": [{"source_id": "src_claude", "source_kind": "claude_code_local"}]}

    def census(self) -> dict[str, object]:
        return {"sources": [{"source_kind": "claude_code_local", "session_count": 1}]}

    def list_events_by_source_window(self, *, source_kind: str, limit: int) -> dict[str, object]:
        self.queried_source_kinds.append(source_kind)
        _check(limit == 1, "qualification uses one bounded registry retrieval")
        return {"events": [{"id": "evt_claude"}]}


def _manager_transaction_load_failure(
    *,
    target: Path,
    name: str,
    answers_fingerprint: str,
) -> str:
    """Assert public qualification retains a named loader-contract failure."""

    service = cast(SessionLedgerService, object.__new__(SessionLedgerService))
    outcome = service.qualify_selected_sources(
        target=str(target),
        name=name,
        answers_fingerprint=answers_fingerprint,
    )
    _check(outcome["record_source"] == "manager_transaction", "named failure keeps provenance")
    _check(outcome["target_identity_matched"] is False, "named failure withholds target proof")
    _check(outcome["answers_fingerprint_matched"] is False, "named failure withholds answer proof")
    _check(outcome["sources"] == [], "named failure withholds source claims")
    reason = outcome["qualification_reason"]
    _check(isinstance(reason, str), "named loader failure has a textual reason")
    return reason


def test_selected_and_declined_sources_are_separate() -> None:
    with tempfile.TemporaryDirectory(prefix="selected-sources-") as temporary:
        target = Path(temporary).resolve()
        answers = {
            "decisions": {"session_sources": ["codex_local"]},
            "consents": {
                "codex_session_ingestion_consent": True,
                "claude_session_ingestion_consent": False,
            },
        }
        fingerprint = canonical_sha256(answers)
        outcome = qualify_selected_sources(
            target=target,
            name="newborn",
            requested_answers_fingerprint=fingerprint,
            record=_record(target=target),
            registered_source_kinds={"codex_local"},
            backfill_counts={"codex_local": 3, "claude_code_local": 0},
            retrieval_by_kind={"codex_local": True, "claude_code_local": False},
        )
    _check(outcome["target_identity_matched"] is True, "target identity matches")
    _check(outcome["answers_fingerprint_matched"] is True, "fingerprint matches")
    _check(outcome["record_source"] == "manager_transaction", "honest provenance")
    rows = outcome["sources"]
    _check(
        rows
        == [
            {
                "source": "codex_local",
                "selected": True,
                "consented": True,
                "registered": True,
                "backfill_count": 3,
                "retrieval_ok": True,
            },
            {
                "source": "claude_code_local",
                "selected": False,
                "consented": False,
                "registered": False,
                "backfill_count": 0,
                "retrieval_ok": False,
            },
        ],
        f"unexpected source qualification rows: {rows!r}",
    )


def test_fingerprint_mismatch_withholds_selection_claims() -> None:
    with tempfile.TemporaryDirectory(prefix="selected-sources-") as temporary:
        target = Path(temporary).resolve()
        outcome = qualify_selected_sources(
            target=target,
            name="newborn",
            requested_answers_fingerprint="sha256:" + "0" * 64,
            record=_record(target=target),
            registered_source_kinds={"codex_local"},
            backfill_counts={"codex_local": 3},
            retrieval_by_kind={"codex_local": True},
        )
    _check(outcome["answers_fingerprint_matched"] is False, "mismatch is visible")
    _check(outcome["qualification_reason"] == "answers_fingerprint_mismatch", "typed mismatch")
    _check(outcome["sources"] == [], "untrusted answers produce no selection claims")


def _legacy_v1_transaction_document(transaction: Transaction) -> dict[str, object]:
    """Spell out the frozen v1 root shape without borrowing the current writer."""

    return {
        "schema_version": 1,
        "operation_id": "legacy-operation",
        "name": transaction.name,
        "target": transaction.target,
        "input_fingerprint": "sha256:" + "1" * 64,
        "answers": transaction.answers,
        "answers_fingerprint": transaction.answers_fingerprint,
        "approval_fingerprint": None,
        "approval_recorded_at": None,
        "seed_repository": "https://example.invalid/seed.git",
        "seed_tag": "release-fixture",
        "seed_commit": "a" * 40,
        "seed_tree_hash": "b" * 40,
        "seed_archive_sha256": "c" * 64,
        "profile": "fixture",
        "flow_id": transaction.flow_id,
        "flow_source_revision": transaction.flow_source_revision,
        "flow_contract_digest": transaction.flow_contract_digest,
        "status": "pending",
        "stages": {},
        "stage_probe_statuses": {},
        "stage_probe_attempts": [],
        "operation_stages": {},
        "operation_statuses": {},
        "operation_attempts": [],
        "evidence": [],
        "completion": {},
        "result_kind": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
    }


def test_manager_transaction_loader_is_schema_validated() -> None:
    _check(
        CURRENT_JOURNAL_VERSION in _SUPPORTED_JOURNAL_VERSIONS,
        "selected-source admission tracks the current manager journal version",
    )
    with tempfile.TemporaryDirectory(prefix="selected-sources-") as temporary:
        root = Path(temporary).resolve()
        target = root / "target"
        contracts = target_contract_directory(target)
        shutil.copytree(_REPO / "plugins/github_midwife_plugin/knowledge_base", contracts)
        bundle = ContractBundle.load(source_revision="a" * 40, directory=contracts)
        answers = {
            "schema_version": 1,
            "flow_id": "macos.repository_setup",
            "flow_source_revision": "a" * 40,
            "name": "newborn",
            "target": str(target),
            "public_inputs": {},
            "decisions": {
                "setup_profile": "macos-bizops",
                "session_sources": ["claude_code_local"],
            },
            "consents": {
                "codex_session_ingestion_consent": False,
                "claude_session_ingestion_consent": True,
            },
            "resolution_evidence": [],
        }
        transaction = Transaction.create(
            name="newborn",
            target=target,
            input_fingerprint="sha256:" + "2" * 64,
            answers=answers,
            seed=SeedLock(
                "https://example.invalid/seed.git",
                "release-fixture",
                "a" * 40,
                "b" * 40,
                "c" * 64,
                "fixture",
            ),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            completion_probe_ids=bundle.completion_probe_ids,
        )
        prior_home = os.environ.get("SOLET_HOME")
        os.environ["SOLET_HOME"] = str(root / "manager")
        try:
            transaction_path = ManagerPaths.resolve().transaction_path("newborn")
            write_transaction(transaction_path, transaction)
            loaded = load_selected_source_record(target=target, name="newborn")
            ledger = _ClaudeCodeLocalLedger()
            qualified = SessionLedgerService.qualify_selected_sources(
                cast(SessionLedgerService, ledger),
                target=str(target),
                name="newborn",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                ledger.queried_source_kinds == ["claude_code_local"],
                "persisted Claude decision probes the registered Claude Code source kind",
            )
            _check(
                qualified["sources"] == [
                    {
                        "source": "codex_local",
                        "selected": False,
                        "consented": False,
                        "registered": False,
                        "backfill_count": 0,
                        "retrieval_ok": False,
                    },
                    {
                        "source": "claude_code_local",
                        "selected": True,
                        "consented": True,
                        "registered": True,
                        "backfill_count": 1,
                        "retrieval_ok": True,
                    },
                ],
                "persisted Claude decision round-trips through the registered source lookup",
            )
            legacy = _legacy_v1_transaction_document(transaction)
            _check(
                "probe_activations" not in legacy,
                "literal v1 fixture excludes post-v1 root keys",
            )
            transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            transaction_path.chmod(0o600)
            legacy_loaded = load_selected_source_record(target=target, name="newborn")
            future = {**legacy, "schema_version": 4}
            transaction_path.write_text(json.dumps(future), encoding="utf-8")
            transaction_path.chmod(0o600)
            future_reason = _manager_transaction_load_failure(
                target=target,
                name="newborn",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                future_reason == "manager_transaction_load_error: "
                "transaction does not match a closed supported journal schema",
                "unsupported journals remain a named b15 qualification failure",
            )

            transaction_path.write_text("{", encoding="utf-8")
            transaction_path.chmod(0o600)
            corrupt_reason = _manager_transaction_load_failure(
                target=target,
                name="newborn",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                corrupt_reason
                == "manager_transaction_load_error: transaction state file is unreadable or corrupt",
                "corrupt state is distinct from an unsupported journal",
            )

            transaction_path.write_text(json.dumps(legacy), encoding="utf-8")
            transaction_path.chmod(0o644)
            private_file_reason = _manager_transaction_load_failure(
                target=target,
                name="newborn",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                private_file_reason == "manager_transaction_load_error: "
                "transaction state file is not a private regular 0600 file",
                "insecure state file is distinct from corrupt and schema failures",
            )

            tampered_fingerprint = {**legacy, "answers_fingerprint": "sha256:" + "0" * 64}
            transaction_path.write_text(json.dumps(tampered_fingerprint), encoding="utf-8")
            transaction_path.chmod(0o600)
            fingerprint_reason = _manager_transaction_load_failure(
                target=target,
                name="newborn",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                fingerprint_reason == "manager_transaction_load_error: "
                "transaction answers_fingerprint does not match its answers",
                "loader fingerprint failure remains distinct from record identity mismatch",
            )
            _check(
                fingerprint_reason != "manager_transaction_identity_mismatch",
                "loader fingerprint failure is never the record identity mismatch cause",
            )
            invalid_name_reason = _manager_transaction_load_failure(
                target=target,
                name="newborn/invalid",
                answers_fingerprint=transaction.answers_fingerprint,
            )
            _check(
                invalid_name_reason
                == "manager_transaction_load_error: transaction name is invalid",
                "path-resolution failures remain named qualification failures",
            )
            _raises(
                SelectedSourceRecordError,
                lambda: load_selected_source_record(target=target, name="newborn"),
                "selected-source loader translates named contract failures",
            )
        finally:
            if prior_home is None:
                del os.environ["SOLET_HOME"]
            else:
                os.environ["SOLET_HOME"] = prior_home
    _check(loaded.record_source == "manager_transaction", "manager record provenance")
    _check(loaded.answers_fingerprint == canonical_sha256(answers), "loader preserves fingerprint")
    _check(legacy_loaded.record_source == "manager_transaction", "loader accepts closed v1 journal")


def main() -> int:
    test_selected_and_declined_sources_are_separate()
    test_fingerprint_mismatch_withholds_selection_claims()
    test_manager_transaction_loader_is_schema_validated()
    print("selected_sources_qualification_smoke: passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
