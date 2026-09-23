#!/usr/bin/env python3
"""Live-Postgres regression smoke for the 2026-06-12 IndexDefinition fix.

Pins three end-to-end CREATE INDEX paths so the IndexDefinition contract
is honored at every layer the schema lifecycle traverses:

1. **GIN-with-opclass path** (the M21 trigram unblocking case, ddl_renderer
   layer) — an IndexDefinition with ``using='gin'`` +
   ``column_operator_classes={'content_text': 'gin_trgm_ops'}`` rendered
   directly through ``emit_create_index_op`` produces ``CREATE INDEX ...
   USING gin (content_text gin_trgm_ops)`` and succeeds against a table
   whose content_text column carries a row wider than the 8191-byte
   btree row limit (sized to 310 KB to match the operator's
   empirically-observed max).
2. **Plain-btree regression path** — an IndexDefinition with no
   ``using`` / no ``column_operator_classes`` renders to ``CREATE INDEX
   ... (col)`` identically pre- and post-fix; the renderer correctly
   defaults when the dataclass fields are unset.
3. **End-to-end roundtrip path** (the serialization-layer fix, added
   2026-06-12) — the IndexDefinition is serialized via
   ``ananta.services.plugin_schema_service.serialization.to_json`` and
   re-hydrated via ``from_json`` BEFORE being rendered. This mirrors
   the live schema lifecycle's ``_hydrate_and_standardize`` path. The
   serialization layer MUST preserve ``using`` + ``column_operator_classes``
   across the JSON roundtrip, otherwise the renderer (which is correct
   in isolation, per case 1) silently falls back to plain btree and
   crashes on wide-row content_text.

   This is the case that would have caught the 2026-06-12 cycle's
   serialization bug. Cases 1 + 2 verify the renderer in isolation; only
   case 3 exercises the FULL lifecycle path declaration → JSON
   roundtrip → schema_diff op → cursor.execute. **Discipline lesson**:
   when a new field is added to a dataclass that participates in
   schema lifecycle, dataclass + serializer + renderer must move in
   lock-step. End-to-end coverage like case 3 is what catches any
   layer that gets out of sync.

Sandboxed via a temporary schema ``example_test_ddl_renderer_<random>`` in
a live local Postgres DB. The cleanup ``DROP SCHEMA CASCADE`` runs in
a ``finally`` block so a smoke crash never leaves the schema lying
around. Env-gated behind ``DDL_RENDERER_FIX_SMOKE=1``.

Run::

    DDL_RENDERER_FIX_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/create_index_using_opclass_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from pathlib import Path
from typing import Any, LiteralString, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)

from ananta.services.plugin_schema_service.serialization import (  # noqa: E402
    _index_from_json,
    _index_to_json,
)
from ananta.types.schema_types import IndexDefinition  # noqa: E402
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.ddl_renderer import (  # noqa: E402
    emit_create_index_op,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
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


_PROFILE_PG_CONFIG = (
    _REPO_ROOT
    / "profile"
    / "config"
    / "plugins"
    / "postgres_state_management_plugin.json"
)


def _load_pg_config() -> PostgresConfig:
    raw = json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))
    return PostgresConfig(**raw)


# 310 KB of repeating text — matches the operator's empirically-observed max
# content_text length in session_ledger__event (310,693 bytes 2026-06-12).
# Comfortably above the 8191-byte btree row limit; well within Postgres TEXT.
_WIDE_CONTENT_TEXT = "abc def ghi jkl mno pqr stu vwx yz0 123 456 789\n" * 6800


_SESSION_LEDGER_NAMESPACE = "session_ledger"
_EVENT_LIKE_TABLE = "event_smoke"


def _gin_trgm_index() -> IndexDefinition:
    return IndexDefinition(
        name="idx_event_content_text_trgm",
        columns=["content_text"],
        using="gin",
        column_operator_classes={"content_text": "gin_trgm_ops"},
    )


def _plain_btree_index() -> IndexDefinition:
    return IndexDefinition(
        name="idx_event_event_at_status",
        columns=["event_at_status"],
    )


def _create_test_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'CREATE SCHEMA IF NOT EXISTS "{schema_name}"')
        )


def _drop_test_schema(provider: PostgresProvider, schema_name: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema_name}" CASCADE')
        )


def _create_event_like_table(
    provider: PostgresProvider, schema_name: str
) -> None:
    """Create a minimal `__event`-shaped table under the smoke schema.

    Uses raw DDL (not the full lifecycle) so we exercise ONLY the
    renderer's CREATE INDEX path — not the broader table-creation
    machinery — keeping the smoke focused.
    """
    full_table = f"{_SESSION_LEDGER_NAMESPACE}__{_EVENT_LIKE_TABLE}"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{schema_name}"."{full_table}" ('
                "id text PRIMARY KEY DEFAULT gen_random_uuid()::text, "
                "content_text text NOT NULL, "
                "event_at_status text NOT NULL DEFAULT 'ACTIVE'"
                ")",
            )
        )


def _seed_wide_row(provider: PostgresProvider, schema_name: str) -> int:
    """Insert one row whose content_text is wider than the 8191-byte btree limit."""
    full_table = f"{_SESSION_LEDGER_NAMESPACE}__{_EVENT_LIKE_TABLE}"
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'INSERT INTO "{schema_name}"."{full_table}" '
                "(content_text) VALUES (%s) RETURNING length(content_text)",
            ),
            (_WIDE_CONTENT_TEXT,),
        )
        row = cur.fetchone()
    assert row is not None
    row_d = dict(row) if not isinstance(row, dict) else row
    return int(cast(int, row_d.get("length", 0)))


def _apply_index_op(provider: PostgresProvider, op: object) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(Any, op))


def _fetch_indexdef(
    provider: PostgresProvider, schema_name: str, indexname: str
) -> str | None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                "SELECT indexdef FROM pg_indexes "
                "WHERE schemaname = %s AND indexname = %s",
            ),
            (schema_name, indexname),
        )
        row = cur.fetchone()
    if row is None:
        return None
    row_d = dict(row) if not isinstance(row, dict) else row
    return str(row_d.get("indexdef", ""))


def case_gin_trgm_creates_on_wide_text(
    provider: PostgresProvider, schema_name: str
) -> None:
    """GIN-with-opclass path: M21-shape IndexDefinition succeeds on a 310 KB row."""
    seeded_len = _seed_wide_row(provider, schema_name)
    _check(
        seeded_len > 8191,
        f"seeded content_text length ({seeded_len}) exceeds 8191-byte btree limit",
    )
    op = emit_create_index_op(
        _SESSION_LEDGER_NAMESPACE,
        _EVENT_LIKE_TABLE,
        _gin_trgm_index(),
        schema_name,
    )
    # No exception expected; pre-fix the renderer emitted btree here and
    # this call raised ProgramLimitExceeded matching the operator's
    # 2026-06-12 cutover error.
    _apply_index_op(provider, op)
    # Physical name follows the {namespace}__{table}__{idx_name} convention.
    physical_name = (
        f"{_SESSION_LEDGER_NAMESPACE}__{_EVENT_LIKE_TABLE}__"
        "idx_event_content_text_trgm"
    )
    indexdef = _fetch_indexdef(provider, schema_name, physical_name)
    _check(
        indexdef is not None,
        f"GIN trigram index lands under physical name {physical_name!r}",
    )
    if indexdef:
        _check(
            "USING gin" in indexdef,
            f"indexdef contains 'USING gin' (got: {indexdef!r})",
        )
        _check(
            "gin_trgm_ops" in indexdef,
            f"indexdef contains 'gin_trgm_ops' opclass (got: {indexdef!r})",
        )


def case_roundtrip_preserves_using_and_opclass(
    provider: PostgresProvider, schema_name: str
) -> None:
    """End-to-end: IndexDefinition → to_json → from_json → render → apply.

    Mirrors the live schema lifecycle's ``_hydrate_and_standardize`` path.
    Pre-fix this case would have rendered plain btree (because
    ``_index_to_json`` dropped ``using`` + ``column_operator_classes``)
    and crashed on the wide content_text row. Post-fix the JSON shape
    carries both fields and the rendered CREATE INDEX is GIN-with-opclass.
    """
    original = _gin_trgm_index()
    json_shape = _index_to_json(original)
    _check(
        json_shape.get("using") == "gin",
        f"to_json preserves using='gin' (got {json_shape.get('using')!r})",
    )
    _check(
        json_shape.get("column_operator_classes")
        == {"content_text": "gin_trgm_ops"},
        f"to_json preserves column_operator_classes "
        f"(got {json_shape.get('column_operator_classes')!r})",
    )
    rehydrated = _index_from_json(json_shape)
    _check(
        rehydrated.using == "gin",
        f"from_json restores using='gin' (got {rehydrated.using!r})",
    )
    _check(
        rehydrated.column_operator_classes == {"content_text": "gin_trgm_ops"},
        f"from_json restores column_operator_classes "
        f"(got {rehydrated.column_operator_classes!r})",
    )
    # Distinct name so this case's index doesn't collide with case 1.
    rehydrated = IndexDefinition(
        name="idx_trgm_rt",
        columns=rehydrated.columns,
        unique=rehydrated.unique,
        where=rehydrated.where,
        using=rehydrated.using,
        column_operator_classes=rehydrated.column_operator_classes,
    )
    op = emit_create_index_op(
        _SESSION_LEDGER_NAMESPACE,
        _EVENT_LIKE_TABLE,
        rehydrated,
        schema_name,
    )
    _apply_index_op(provider, op)
    physical_name = (
        f"{_SESSION_LEDGER_NAMESPACE}__{_EVENT_LIKE_TABLE}__"
        "idx_trgm_rt"
    )
    indexdef = _fetch_indexdef(provider, schema_name, physical_name)
    _check(
        indexdef is not None,
        f"roundtripped GIN trigram index lands under {physical_name!r}",
    )
    if indexdef:
        _check(
            "USING gin" in indexdef,
            "end-to-end roundtrip preserves 'USING gin' through to live DDL "
            f"(got: {indexdef!r})",
        )
        _check(
            "gin_trgm_ops" in indexdef,
            "end-to-end roundtrip preserves 'gin_trgm_ops' opclass "
            f"(got: {indexdef!r})",
        )


def case_plain_btree_unchanged(
    provider: PostgresProvider, schema_name: str
) -> None:
    """Regression: IndexDefinition without using/opclass still renders btree."""
    op = emit_create_index_op(
        _SESSION_LEDGER_NAMESPACE,
        _EVENT_LIKE_TABLE,
        _plain_btree_index(),
        schema_name,
    )
    _apply_index_op(provider, op)
    physical_name = (
        f"{_SESSION_LEDGER_NAMESPACE}__{_EVENT_LIKE_TABLE}__"
        "idx_event_event_at_status"
    )
    indexdef = _fetch_indexdef(provider, schema_name, physical_name)
    _check(
        indexdef is not None,
        f"plain btree index lands under physical name {physical_name!r}",
    )
    if indexdef:
        _check(
            "USING btree" in indexdef,
            "plain IndexDefinition (no using=) still renders btree "
            f"(got: {indexdef!r})",
        )
        _check(
            "gin_trgm_ops" not in indexdef,
            "plain IndexDefinition does NOT pick up a stray opclass "
            f"(got: {indexdef!r})",
        )


def main() -> int:
    if os.environ.get("DDL_RENDERER_FIX_SMOKE") != "1":
        print(
            "  SKIP  DDL_RENDERER_FIX_SMOKE != 1; "
            "this smoke creates and drops a sandbox schema in the live DB.",
        )
        return 0
    config = _load_pg_config()
    provider = PostgresProvider(config)
    provider.initialize()
    schema_name = f"example_test_ddl_renderer_{secrets.token_hex(4)}"
    try:
        _create_test_schema(provider, schema_name)
        _create_event_like_table(provider, schema_name)
        case_gin_trgm_creates_on_wide_text(provider, schema_name)
        case_roundtrip_preserves_using_and_opclass(provider, schema_name)
        case_plain_btree_unchanged(provider, schema_name)
    finally:
        _drop_test_schema(provider, schema_name)
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 0 if not _failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
