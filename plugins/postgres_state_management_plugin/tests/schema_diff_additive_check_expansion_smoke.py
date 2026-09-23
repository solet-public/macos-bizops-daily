#!/usr/bin/env python3
"""M21-RCA Fix 1 + Fix 2 smokes for schema_diff additive CHECK enum expansion.

Coverage (3 smokes per Coordinator-Dawn 2026-06-11 PT dispatch):

* ``additive_check_expansion`` — ColumnDefinition pair differing ONLY in
  ``check`` (live-set ⊆ declared-set on the same column) yields exactly
  one ``DROP CONSTRAINT`` op + one ``ADD CONSTRAINT … CHECK (…)`` op via
  the new ``_diff_or_refuse_column_changes`` dispatcher. No
  ``NotImplementedError`` raised.

* ``non_additive_check_refused`` — ColumnDefinition pair where the
  declared check DROPS a value from live (non-additive shrink) STILL
  raises ``NotImplementedError`` per the pre-Fix-1 discipline. Same
  refusal for any compound mutation (e.g. ``check`` + ``not_null``
  both differ).

* ``error_message_completeness`` — when the dispatcher raises, the
  message text NAMES every comparison field on ``ColumnDefinition``
  (``check`` / ``primary_key`` / ``type_params`` in addition to
  ``type`` / ``not_null`` / ``default`` / ``unique``). Regression
  guard for Fix 2: previously a ``check``-only delta showed as four
  identical-looking values + omitted the actual delta field, misleading
  any debugger.

Per [[sandbox-mutating-smokes]] all fixtures live in-memory; the
schema_diff machinery is pure-functional (no DB / filesystem side
effects); these smokes don't touch operator state.

Run:
    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/schema_diff_additive_check_expansion_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import (  # noqa: E402
    ColumnDefinition,
    SchemaDefinition,
    TableSchema,
)
from postgres_state_management_plugin.postgres_backend.schema_diff import (  # noqa: E402
    _check_is_additive_enum_expansion,
    _diff_or_refuse_column_changes,
    _parse_check_in_values,
    diff_schema,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ─── Fixture builders ───────────────────────────────────────────────────────


def _col_with_check(check: str | None) -> ColumnDefinition:
    """Source-kind TEXT NOT NULL column with the given CHECK body."""
    return ColumnDefinition(
        type=ColumnType.TEXT,
        not_null=True,
        check=check,
    )


def _table_with_col(check: str | None) -> TableSchema:
    return TableSchema(
        table_name="source",
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "source_kind": _col_with_check(check),
        },
    )


def _schema_with_check(check: str | None) -> SchemaDefinition:
    return SchemaDefinition(
        namespace="session_ledger",
        tables={"source": _table_with_col(check)},
    )


_CURRENT_CHECK = (
    "source_kind IN ('agent_messaging', 'codex_local', 'codex_pushed', "
    "'claude_code_local', 'claude_code_pushed', 'chatgpt_export')"
)
_DECLARED_CHECK = (
    "source_kind IN ('agent_messaging', 'codex_local', 'codex_pushed', "
    "'codex_state', 'codex_history', 'codex_goals', 'codex_memories', "
    "'codex_ambient', 'claude_code_local', 'claude_code_pushed', "
    "'claude_code_history', 'claude_code_tasks', 'chatgpt_export')"
)


# ─── (1) additive_check_expansion ──────────────────────────────────────────


def _verify_parser_helpers() -> None:
    """Parser + additive-predicate agree on the canonical expansion shape."""
    current_parsed = _parse_check_in_values(_CURRENT_CHECK)
    declared_parsed = _parse_check_in_values(_DECLARED_CHECK)
    _check(
        current_parsed is not None and declared_parsed is not None,
        "_parse_check_in_values handles canonical IN-with-quoted-csv shape",
    )
    if current_parsed and declared_parsed:
        _, current_values = current_parsed
        _, declared_values = declared_parsed
        _check(
            current_values < declared_values,
            f"current values STRICT subset of declared "
            f"({len(current_values)} ⊂ {len(declared_values)})",
        )
    _check(
        _check_is_additive_enum_expansion(_CURRENT_CHECK, _DECLARED_CHECK),
        "_check_is_additive_enum_expansion returns True for canonical expansion",
    )


def _verify_dispatcher_emits_drop_add_pair() -> None:
    """Dispatcher-level: check-only difference → DROP + ADD pair."""
    ops = _diff_or_refuse_column_changes(
        namespace="session_ledger",
        table_name="source",
        current_table=_table_with_col(_CURRENT_CHECK),
        declared_table=_table_with_col(_DECLARED_CHECK),
        schema_name="example",
    )
    _check(
        len(ops) == 2,
        f"exactly 2 ops emitted (DROP + ADD pair); got {len(ops)}",
    )
    if len(ops) != 2:
        return
    sql_strs = [op.as_string() for op in ops]
    _check(
        "DROP CONSTRAINT" in sql_strs[0]
        and "session_ledger__source_source_kind_check" in sql_strs[0],
        f"op 0 is DROP CONSTRAINT on Postgres-default name (got {sql_strs[0]!r})",
    )
    _check(
        "ADD CONSTRAINT" in sql_strs[1] and "CHECK" in sql_strs[1],
        f"op 1 is ADD CONSTRAINT … CHECK (…) (got {sql_strs[1]!r})",
    )
    _check(
        "codex_state" in sql_strs[1] and "claude_code_tasks" in sql_strs[1],
        "ADD CONSTRAINT body carries the EXPANDED enum values",
    )


def _verify_diff_schema_e2e() -> None:
    """End-to-end through diff_schema → DROP + ADD land in the op list."""
    e2e_ops = diff_schema(
        namespace="session_ledger",
        current=_schema_with_check(_CURRENT_CHECK),
        declared=_schema_with_check(_DECLARED_CHECK),
        mode="update",
        schema_name="example",
        current_index_physical_names={},
    )
    sql_strs_e2e = [op.as_string() for op in e2e_ops]
    _check(
        any("DROP CONSTRAINT" in s for s in sql_strs_e2e),
        f"diff_schema e2e includes DROP CONSTRAINT (got {len(e2e_ops)} ops)",
    )
    _check(
        any("ADD CONSTRAINT" in s and "CHECK" in s for s in sql_strs_e2e),
        "diff_schema e2e includes ADD CONSTRAINT … CHECK",
    )


def additive_check_expansion() -> None:
    print("additive_check_expansion:")
    _verify_parser_helpers()
    _verify_dispatcher_emits_drop_add_pair()
    _verify_diff_schema_e2e()


# ─── (2) non_additive_check_refused ────────────────────────────────────────


def non_additive_check_refused() -> None:
    print("non_additive_check_refused:")
    # Shrunk-set: declared drops a value. NOT safe — existing rows could
    # violate the new CHECK.
    declared_shrunk = (
        "source_kind IN ('agent_messaging', 'codex_local', 'codex_pushed')"
    )
    _check(
        not _check_is_additive_enum_expansion(_CURRENT_CHECK, declared_shrunk),
        "shrunk-set declared check is NOT classified as additive",
    )
    try:
        _diff_or_refuse_column_changes(
            namespace="session_ledger",
            table_name="source",
            current_table=_table_with_col(_CURRENT_CHECK),
            declared_table=_table_with_col(declared_shrunk),
            schema_name="example",
        )
    except NotImplementedError as exc:
        _check(True, f"shrunk-set raises NotImplementedError (got {type(exc).__name__})")
    else:
        _check(False, "shrunk-set should have raised NotImplementedError")

    # Compound mutation: check + not_null both differ. Compound changes are
    # NOT supported by the additive-check path; refuse.
    compound_current = ColumnDefinition(
        type=ColumnType.TEXT, not_null=True, check=_CURRENT_CHECK,
    )
    compound_declared = ColumnDefinition(
        type=ColumnType.TEXT, not_null=False, check=_DECLARED_CHECK,
    )
    compound_current_table = TableSchema(
        table_name="source",
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "source_kind": compound_current,
        },
    )
    compound_declared_table = TableSchema(
        table_name="source",
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "source_kind": compound_declared,
        },
    )
    try:
        _diff_or_refuse_column_changes(
            namespace="session_ledger",
            table_name="source",
            current_table=compound_current_table,
            declared_table=compound_declared_table,
            schema_name="example",
        )
    except NotImplementedError:
        _check(True, "compound mutation (check + not_null) raises NotImplementedError")
    else:
        _check(False, "compound mutation should have raised NotImplementedError")

    # Edge: malformed CHECK shape (no IN clause) cannot be classified.
    _check(
        not _check_is_additive_enum_expansion(
            "length(source_kind) > 0", "length(source_kind) > 0",
        ),
        "non-IN-shape CHECK is NOT classified as additive",
    )
    _check(
        not _check_is_additive_enum_expansion(None, _DECLARED_CHECK),
        "None current → declared transition is NOT additive expansion",
    )


# ─── (3) error_message_completeness ────────────────────────────────────────


def error_message_completeness() -> None:
    """Fix 2 regression guard: error message names every comparison field.

    Pre-Fix-2, only `type/not_null/default/unique` were named — a
    `check`-only delta produced identical-looking values for all four
    printed fields, misleading the debugger. Post-Fix-2, the message
    names `check`, `primary_key`, and `type_params` too.
    """
    print("error_message_completeness:")
    # Build a compound mutation so the path raises; we don't care about
    # specific values here — only that the message text names every field.
    current = ColumnDefinition(
        type=ColumnType.TEXT, not_null=True, check=_CURRENT_CHECK,
    )
    declared = ColumnDefinition(
        type=ColumnType.INTEGER, not_null=False, check=_DECLARED_CHECK,
    )
    current_table = TableSchema(
        table_name="source",
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "source_kind": current,
        },
    )
    declared_table = TableSchema(
        table_name="source",
        columns={
            "id": ColumnDefinition(type=ColumnType.TEXT, primary_key=True),
            "source_kind": declared,
        },
    )
    raised_message: str | None = None
    try:
        _diff_or_refuse_column_changes(
            namespace="session_ledger",
            table_name="source",
            current_table=current_table,
            declared_table=declared_table,
            schema_name="example",
        )
    except NotImplementedError as exc:
        raised_message = str(exc)
    _check(raised_message is not None, "compound mutation raises NotImplementedError")
    if raised_message:
        for field in (
            "type=", "primary_key=", "not_null=", "default=",
            "unique=", "check=", "type_params=",
        ):
            _check(
                field in raised_message,
                f"error message names field {field!r}",
            )


def main() -> int:
    additive_check_expansion()
    non_additive_check_refused()
    error_message_completeness()
    print()
    print(f"  passed: {_passed}")
    print(f"  failed: {len(_failed)}")
    for label in _failed:
        print(f"    - {label}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    sys.exit(main())
