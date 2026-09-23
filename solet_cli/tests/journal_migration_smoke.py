"""Focused proof for versioned, structural setup-journal migration."""

from __future__ import annotations

import json
import sys
import tempfile
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import solet_manager.journal_migrations as migrations  # noqa: E402
from solet_manager.errors import StateError  # noqa: E402
from solet_manager.journal_migrations import (  # noqa: E402
    CURRENT_JOURNAL_VERSION,
    JOURNAL_BOUNDARY_KEYS,
    JOURNAL_MIGRATIONS,
    JournalMigration,
    journal_shape_fingerprint,
    migrate_journal,
    validate_journal_migration_registry,
)
from solet_manager.journal_validation import (
    JOURNAL_BOUNDARY_KEYS as PARSER_BOUNDARY_KEYS,  # noqa: E402
)
from solet_manager.stage_activation import (
    JOURNAL_BOUNDARY_KEYS as ACTIVATION_BOUNDARY_KEYS,  # noqa: E402
)
from solet_manager.transaction import Transaction, load_transaction, write_transaction  # noqa: E402

_CHECKS = 0
_FIXTURE_SHA256 = "6deadb024fa7721de08b54fe4f8648c5696af721013e5b7709a43ba76d205e53"
_EXPECTED_SHAPE_FINGERPRINT = "c16510c617593fd5a392345e770da4787f211b3656800655a88f22739d30e5f4"
_FIXTURE_PATH = Path(__file__).parent / "fixtures" / "journals" / "bizopsb15_v1_pre_r12.json"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _raises(error: type[BaseException], callback: object, label: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except error:
        _check(True, label)
    else:
        _check(False, label)


def _real_v1_journal() -> dict[str, object]:
    fixture_bytes = _FIXTURE_PATH.read_bytes()
    _check(
        sha256(fixture_bytes).hexdigest() == _FIXTURE_SHA256,
        "real captured v1 journal retains its recorded sha256",
    )
    raw = json.loads(fixture_bytes)
    if not isinstance(raw, dict):
        raise AssertionError("real journal fixture must be an object")
    return raw


def _synthetic_v1_current_attempt_journal(
    raw: dict[str, object],
) -> dict[str, object]:
    """Build the clearly labeled synthetic 16-key v1 control from real bytes."""

    synthetic = deepcopy(raw)
    attempts = synthetic["operation_attempts"]
    if not isinstance(attempts, list):
        raise AssertionError("real fixture operation_attempts must be a list")
    for attempt in attempts:
        if not isinstance(attempt, dict):
            raise AssertionError("real fixture operation attempt must be an object")
        attempt.update(
            {
                "exit_code": None,
                "timed_out": False,
                "duration_ms": 0,
                "reason": None,
            }
        )
    return synthetic


def _check_real_and_synthetic_bootstrap() -> dict[str, object]:
    raw = _real_v1_journal()
    raw_before = deepcopy(raw)
    migrated = migrate_journal(raw)
    _check(raw == raw_before, "migration does not mutate the real v1 journal")
    _check(migrated["schema_version"] == CURRENT_JOURNAL_VERSION, "v1 migrates through v2 to v3")
    attempts = migrated["operation_attempts"]
    _check(
        isinstance(attempts, list)
        and all(
            isinstance(attempt, dict)
            and attempt["exit_code"] is None
            and attempt["timed_out"] is False
            and attempt["duration_ms"] == 0
            and attempt["reason"] is None
            for attempt in attempts
        ),
        "real pre-r12 attempts receive only the established diagnostics",
    )
    synthetic = _synthetic_v1_current_attempt_journal(raw)
    synthetic_migrated = migrate_journal(synthetic)
    _check(
        synthetic_migrated["operation_attempts"] == synthetic["operation_attempts"],
        "synthetic v1 16-key attempts are preserved verbatim",
    )
    v2 = migrations._migrate_v1_to_v2(raw)  # noqa: SLF001 - direct prior-generation fixture
    v3 = migrate_journal(v2)
    _check(v3["schema_version"] == 3, "v2 journal reads cleanly through the v3 migration")
    _check(
        isinstance(v3.get("probe_activations"), dict)
        and all(
            status != "not_applicable"
            for boundaries in v3["stage_probe_statuses"].values()  # type: ignore[index,union-attr]
            for probes in boundaries.values()
            for status in probes.values()
        ),
        "v3 stores activation separately and never persists not_applicable",
    )
    return raw


def _check_refusals(raw: dict[str, object]) -> None:
    malformed = deepcopy(raw)
    attempts = malformed["operation_attempts"]
    if not isinstance(attempts, list) or not isinstance(attempts[0], dict):
        raise AssertionError("fixture lacks an operation attempt")
    del attempts[0]["request_id"]
    _raises(
        StateError,
        lambda: migrate_journal(malformed),
        "malformed same-version journal remains corrupt",
    )
    future = deepcopy(raw)
    future["schema_version"] = CURRENT_JOURNAL_VERSION + 1
    _raises(
        StateError,
        lambda: migrate_journal(future),
        "unknown future journal version is refused",
    )
    with patch.object(migrations, "JOURNAL_MIGRATIONS", ()):
        _raises(
            StateError,
            lambda: migrate_journal(raw),
            "missing registry edge is refused",
        )


def _check_multi_edge_ordering() -> None:
    order: list[str] = []

    def validate_version(version: int) -> object:
        def validate(value: dict[str, object]) -> None:
            if value.get("schema_version") != version:
                raise StateError(f"fixture expected v{version}")

        return validate

    def transform(version: int, label: str) -> object:
        def apply(value: dict[str, object]) -> dict[str, object]:
            order.append(label)
            return {**value, "schema_version": version}

        return apply

    first = JournalMigration(
        from_version=1,
        to_version=2,
        name="first",
        value_policy="value_preserving",
        validate_source=validate_version(1),  # type: ignore[arg-type]
        transform=transform(2, "first"),  # type: ignore[arg-type]
        validate_target=validate_version(2),  # type: ignore[arg-type]
    )
    second = JournalMigration(
        from_version=2,
        to_version=3,
        name="second",
        value_policy="value_changing",
        validate_source=validate_version(2),  # type: ignore[arg-type]
        transform=transform(3, "second"),  # type: ignore[arg-type]
        validate_target=validate_version(3),  # type: ignore[arg-type]
    )
    with (
        patch.object(migrations, "CURRENT_JOURNAL_VERSION", 3),
        patch.object(migrations, "JOURNAL_MIGRATIONS", (first, second)),
        patch.object(migrations, "_validate_v3", lambda value: None),
    ):
        result = migrate_journal({"schema_version": 1, "answers": {}})
    _check(order == ["first", "second"], "multi-edge migrations run in order")
    _check(result["schema_version"] == 3, "multi-edge migration reaches target")


def _check_reload_and_write_contract(raw: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "journal.json"
        original = json.dumps(raw, sort_keys=True, separators=(",", ":"))
        path.write_text(original, encoding="utf-8")
        path.chmod(0o600)
        loaded = load_transaction(path)
        _check(loaded is not None, "real v1 journal loads through the journal choke point")
        _check(
            path.read_text(encoding="utf-8") == original,
            "read-only load leaves the legacy journal bytes unchanged",
        )
        if loaded is None:
            raise AssertionError("checked non-null transaction unexpectedly absent")
        reloaded = Transaction.from_dict(loaded.to_dict())
        _check(
            reloaded.to_dict() == loaded.to_dict(),
            "current journal reload is idempotent",
        )
        write_transaction(path, loaded)
        persisted = json.loads(path.read_text(encoding="utf-8"))
        _check(
            persisted["schema_version"] == CURRENT_JOURNAL_VERSION,
            "first authorized write atomically persists v3",
        )
        before_refusal = path.read_bytes()
        _raises(
            StateError,
            lambda: write_transaction(path, replace(loaded, journal_version=1)),
            "pending migration transaction cannot be written",
        )
        _check(path.read_bytes() == before_refusal, "pending write refusal leaves bytes unchanged")


def _check_registry_and_shape_pin() -> None:
    validate_journal_migration_registry()
    _check(
        len(JOURNAL_MIGRATIONS) == 2
        and JOURNAL_MIGRATIONS[0].from_version == 1
        and JOURNAL_MIGRATIONS[0].to_version == 2
        and JOURNAL_MIGRATIONS[1].from_version == 2
        and JOURNAL_MIGRATIONS[1].to_version == CURRENT_JOURNAL_VERSION,
        "registry is contiguous from every supported version to current",
    )
    _check(
        JOURNAL_MIGRATIONS[0].value_policy == "value_preserving"
        and JOURNAL_MIGRATIONS[1].value_policy == "value_changing",
        "migration edges declare their derived-state value policies",
    )
    _check(
        journal_shape_fingerprint() == _EXPECTED_SHAPE_FINGERPRINT,
        "closed journal shape fingerprint is pinned",
    )
    _check(
        PARSER_BOUNDARY_KEYS is JOURNAL_BOUNDARY_KEYS
        and ACTIVATION_BOUNDARY_KEYS is JOURNAL_BOUNDARY_KEYS,
        "shape pin covers parser and stage-activation boundary enforcement",
    )


def main() -> int:
    raw = _check_real_and_synthetic_bootstrap()
    _check_refusals(raw)
    _check_multi_edge_ordering()
    _check_reload_and_write_contract(raw)
    _check_registry_and_shape_pin()
    print(f"journal_migration_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
