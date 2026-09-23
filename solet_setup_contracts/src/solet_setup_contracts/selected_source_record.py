"""Read-only, stdlib-only projection of a setup transaction's selected sources."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import cast

type JsonValue = None | bool | int | float | str | list[JsonValue] | dict[str, JsonValue]

_REQUIRED_TRANSACTION_KEYS = frozenset(
    {
        "schema_version",
        "operation_id",
        "name",
        "target",
        "input_fingerprint",
        "answers",
        "answers_fingerprint",
        "approval_fingerprint",
        "approval_recorded_at",
        "seed_repository",
        "seed_tag",
        "seed_commit",
        "seed_tree_hash",
        "seed_archive_sha256",
        "profile",
        "flow_id",
        "flow_source_revision",
        "flow_contract_digest",
        "status",
        "stages",
        "stage_probe_statuses",
        "stage_probe_attempts",
        "operation_stages",
        "operation_statuses",
        "operation_attempts",
        "evidence",
        "completion",
        "result_kind",
        "created_at",
        "updated_at",
    }
)
_SUPPORTED_JOURNAL_VERSIONS = frozenset({1, 2, 3})
_ANSWER_KEYS = frozenset(
    {
        "schema_version",
        "flow_id",
        "flow_source_revision",
        "name",
        "target",
        "public_inputs",
        "decisions",
        "consents",
        "resolution_evidence",
    }
)
_CONTRACT_FILENAMES = (
    "macos_setup_flow.json",
    "setup_flow.schema.json",
    "setup_answers.schema.json",
    "setup_journal.schema.json",
    "setup_adapter_envelope.schema.json",
)


class SelectedSourceContractError(RuntimeError):
    """The read-only setup-transaction projection is not trustworthy."""


@dataclass(frozen=True, slots=True)
class SelectedSourceTransaction:
    """The exact journal fields needed to qualify session-source consent."""

    name: str
    target: str
    answers: dict[str, JsonValue]
    answers_fingerprint: str
    flow_source_revision: str
    flow_contract_digest: str


def canonical_sha256(value: JsonValue) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def resolve_transaction_path(name: str, *, environ: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    """Resolve the manager journal path without importing manager path classes."""

    if not name or "/" in name:
        raise SelectedSourceContractError("transaction name is invalid")
    env = os.environ if environ is None else environ
    user_home = (Path.home() if home is None else home).expanduser().resolve(strict=False)
    configured = env.get("SOLET_HOME", "").strip()
    if configured:
        root = Path(configured).expanduser()
        if not root.is_absolute() or root.resolve(strict=False) == Path("/"):
            raise SelectedSourceContractError("SOLET_HOME must be an absolute, non-root path")
        return root.resolve(strict=False) / "state" / "transactions" / f"{name}.json"
    state = env.get("XDG_STATE_HOME", "").strip()
    state_root = Path(state).expanduser() if state else user_home / ".local" / "state"
    if not state_root.is_absolute():
        raise SelectedSourceContractError("XDG_STATE_HOME must be an absolute path")
    return state_root.resolve(strict=False) / "solet" / "transactions" / f"{name}.json"


def load_selected_source_transaction(path: Path) -> SelectedSourceTransaction | None:
    """Load a closed supported private journal and retain consent-proof fields."""

    if not _is_private_regular_file(path):
        return None
    raw = _read_closed_transaction(path)
    transaction = _selected_source_transaction(raw)
    if transaction.answers_fingerprint != canonical_sha256(transaction.answers):
        raise SelectedSourceContractError("transaction answers_fingerprint does not match its answers")
    return transaction


def _is_private_regular_file(path: Path) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if not stat.S_ISREG(info.st_mode):
        raise SelectedSourceContractError("transaction state file is not a private regular 0600 file")
    if stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
        raise SelectedSourceContractError("transaction state file is not a private regular 0600 file")
    return True


def _read_closed_transaction(path: Path) -> dict[str, JsonValue]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SelectedSourceContractError("transaction state file is unreadable or corrupt") from exc
    if not isinstance(raw, dict):
        raise SelectedSourceContractError("transaction does not match a closed supported journal schema")
    if not _REQUIRED_TRANSACTION_KEYS <= frozenset(raw) or raw.get("schema_version") not in _SUPPORTED_JOURNAL_VERSIONS:
        raise SelectedSourceContractError("transaction does not match a closed supported journal schema")
    return cast(dict[str, JsonValue], raw)


def _selected_source_transaction(raw: dict[str, JsonValue]) -> SelectedSourceTransaction:
    answers = raw["answers"]
    if not isinstance(answers, dict):
        raise SelectedSourceContractError("transaction fields are invalid")
    values = _transaction_string_values(raw)
    if not all(value for value in values):
        raise SelectedSourceContractError("transaction fields are invalid")
    return SelectedSourceTransaction(
        name=values[0],
        target=values[1],
        answers=cast(dict[str, JsonValue], answers),
        answers_fingerprint=values[2],
        flow_source_revision=values[3],
        flow_contract_digest=values[4],
    )


def _transaction_string_values(raw: dict[str, JsonValue]) -> tuple[str, str, str, str, str]:
    keys = ("name", "target", "answers_fingerprint", "flow_source_revision", "flow_contract_digest")
    values = tuple(raw[key] for key in keys)
    if not all(isinstance(value, str) for value in values):
        raise SelectedSourceContractError("transaction fields are invalid")
    return cast(tuple[str, str, str, str, str], values)


def validate_selected_source_answers(transaction: SelectedSourceTransaction, *, target: Path) -> None:
    """Validate the exact answer-carrier shape used by source qualification."""

    answers = transaction.answers
    _validate_answer_keys(answers)
    _validate_answer_identity(answers, transaction, target)
    decisions, consents = _decision_and_consent_carriers(answers)
    _validate_session_sources(decisions)
    _validate_consent_values(consents)


def _validate_answer_keys(answers: dict[str, JsonValue]) -> None:
    if frozenset(answers) != _ANSWER_KEYS or answers.get("schema_version") != 1:
        raise SelectedSourceContractError("normalized answers fields differ from v1")


def _validate_answer_identity(
    answers: dict[str, JsonValue],
    transaction: SelectedSourceTransaction,
    target: Path,
) -> None:
    if answers.get("flow_source_revision") != transaction.flow_source_revision:
        raise SelectedSourceContractError("normalized answers differ from the pinned flow revision")
    if answers.get("name") != transaction.name or answers.get("target") != str(target):
        raise SelectedSourceContractError("normalized answers identity differs from the transaction")


def _decision_and_consent_carriers(
    answers: dict[str, JsonValue],
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    decisions = answers.get("decisions")
    consents = answers.get("consents")
    if not isinstance(decisions, dict) or not isinstance(consents, dict):
        raise SelectedSourceContractError("normalized answers decision and consent carriers must be objects")
    return decisions, consents


def _validate_session_sources(decisions: dict[str, JsonValue]) -> None:
    selected = decisions.get("session_sources")
    if selected is None:
        return
    if not isinstance(selected, list) or not all(isinstance(item, str) for item in selected):
        raise SelectedSourceContractError("session_sources must be a string array")


def _validate_consent_values(consents: dict[str, JsonValue]) -> None:
    if not all(isinstance(value, bool) for value in consents.values()):
        raise SelectedSourceContractError("consents must map string ids to booleans")


def validate_target_contract_identity(transaction: SelectedSourceTransaction, *, target: Path) -> None:
    """Check the target's immutable contract bytes against the journal pin."""

    root = target / "plugins" / "github_midwife_plugin" / "knowledge_base"
    digest = hashlib.sha256()
    for name in sorted(_CONTRACT_FILENAMES):
        try:
            content = (root / name).read_bytes()
        except OSError as exc:
            raise SelectedSourceContractError("target setup contracts are unavailable") from exc
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(content)
        digest.update(b"\0")
    observed = f"sha256:{digest.hexdigest()}"
    if observed != transaction.flow_contract_digest:
        raise SelectedSourceContractError("pinned setup contract identity mismatch")
