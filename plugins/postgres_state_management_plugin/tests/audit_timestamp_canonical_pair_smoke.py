#!/usr/bin/env python3
"""Tier 1.A audit-timestamp canonical-pair restriction smoke.

Per the 2026-06-12 audit-timestamp remediation design (Option B): the
implicit ``NOW() AT TIME ZONE 'UTC'`` auto-default should fire ONLY for
the canonical audit pair (``created_at`` / ``updated_at``). Other ``_at``
columns must NOT get the default so callers can use ``IS NULL`` guards
on nullable-semantic columns (e.g. ``acknowledged_at``, ``closed_at``,
``restoration_at``). The explicit ``__CONTRACT:auto_timestamp_on_insert__``
placeholder opt-in path must still work as the canonical way for
non-pair columns to request the auto-stamp.

Smoke matrix (10 scenarios across both postgres + RDS sibling renderers):

1. ``created_at`` DATETIME with default=None → DEFAULT clause emitted.
2. ``updated_at`` DATETIME with default=None → DEFAULT clause emitted.
3. ``acknowledged_at`` DATETIME with default=None → NO default (the fix).
4. ``restoration_at`` DATETIME with default=None → NO default (the fix).
5. ``other_at`` arbitrary ``_at`` column → NO default (the fix).
6. ``foo`` (non-``_at``) DATETIME with default=None → NO default (always).
7. ``created_at`` with EXPLICIT default value → preserves explicit default.
8. Explicit ``__CONTRACT:auto_timestamp_on_insert__`` placeholder on
   ``acknowledged_at`` (non-pair column) → DEFAULT clause emitted via the
   opt-in path. Confirms non-pair columns can still request auto-stamp.
9. ``_render_default`` on canonical pair with no declared default →
   delegates through to ``_render_auto_timestamp`` and emits DEFAULT.
10. RDS sibling renderer mirrors postgres-renderer behavior on all of
    the above (smoke 1-9 run twice, once against each sibling).

Run:
    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/audit_timestamp_canonical_pair_smoke.py

Per [[sandbox-mutating-smokes]]: pure-function tests against the
renderer; no DB / filesystem side effects.
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
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src"),
)

from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import ColumnDefinition  # noqa: E402
from postgres_state_management_plugin.postgres_backend import (  # noqa: E402
    ddl_renderer as pg_renderer,
)
from rds_postgres_state_management_plugin.postgres_backend import (  # noqa: E402
    ddl_renderer as rds_renderer,
)


def _check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


_CANONICAL_PAIR = ("created_at", "updated_at")
_NON_PAIR_AT_COLUMNS = (
    "acknowledged_at",
    "closed_at",
    "restoration_at",
    "other_at",
)
_NON_AT_COLUMN = "foo"


def test_canonical_pair_gets_default(renderer: object, renderer_label: str) -> None:
    """``created_at`` / ``updated_at`` with default=None → DEFAULT clause."""
    for col_name in _CANONICAL_PAIR:
        col_def = ColumnDefinition(type=ColumnType.DATETIME)
        rendered = renderer._render_auto_timestamp(col_name, col_def)  # type: ignore[attr-defined]
        _check(
            rendered is not None,
            f"{renderer_label}: {col_name} must emit DEFAULT clause; got None",
        )
        sql_text = rendered.as_string(None) if rendered is not None else ""
        _check(
            "NOW() AT TIME ZONE 'UTC'" in sql_text,
            f"{renderer_label}: {col_name} default clause must be NOW(): {sql_text!r}",
        )


def test_non_pair_at_columns_get_no_default(
    renderer: object, renderer_label: str,
) -> None:
    """Other ``_at`` columns must NOT get the implicit auto-default."""
    for col_name in _NON_PAIR_AT_COLUMNS:
        col_def = ColumnDefinition(type=ColumnType.DATETIME)
        rendered = renderer._render_auto_timestamp(col_name, col_def)  # type: ignore[attr-defined]
        _check(
            rendered is None,
            f"{renderer_label}: {col_name} must NOT emit a default; got {rendered!r}",
        )


def test_non_at_datetime_column_gets_no_default(
    renderer: object, renderer_label: str,
) -> None:
    """A DATETIME column that isn't named ``*_at`` never auto-defaults."""
    col_def = ColumnDefinition(type=ColumnType.DATETIME)
    rendered = renderer._render_auto_timestamp(_NON_AT_COLUMN, col_def)  # type: ignore[attr-defined]
    _check(
        rendered is None,
        f"{renderer_label}: {_NON_AT_COLUMN} must NOT emit a default; got {rendered!r}",
    )


def test_canonical_pair_with_explicit_default(
    renderer: object, renderer_label: str,
) -> None:
    """Canonical-pair column with an explicit default does NOT get the auto-stamp."""
    col_def = ColumnDefinition(type=ColumnType.DATETIME, default="2026-01-01")
    rendered = renderer._render_auto_timestamp("created_at", col_def)  # type: ignore[attr-defined]
    _check(
        rendered is None,
        f"{renderer_label}: explicit default on created_at must skip auto-stamp; got {rendered!r}",
    )


def test_explicit_contract_opt_in_still_works(
    renderer: object, renderer_label: str,
) -> None:
    """Non-pair ``_at`` column with ``__CONTRACT:auto_timestamp_on_insert__`` opts in."""
    col_def = ColumnDefinition(
        type=ColumnType.DATETIME,
        default="__CONTRACT:auto_timestamp_on_insert__",
    )
    rendered = renderer._render_default("acknowledged_at", col_def)  # type: ignore[attr-defined]
    _check(
        rendered is not None,
        f"{renderer_label}: explicit contract opt-in must emit DEFAULT; got None",
    )
    sql_text = rendered.as_string(None) if rendered is not None else ""
    _check(
        "NOW() AT TIME ZONE 'UTC'" in sql_text,
        f"{renderer_label}: contract opt-in clause must be NOW(): {sql_text!r}",
    )


def test_render_default_dispatches_canonical_pair_through_auto_stamp(
    renderer: object, renderer_label: str,
) -> None:
    """End-to-end via _render_default: canonical pair lands the DEFAULT clause."""
    col_def = ColumnDefinition(type=ColumnType.DATETIME)
    rendered = renderer._render_default("updated_at", col_def)  # type: ignore[attr-defined]
    _check(
        rendered is not None,
        f"{renderer_label}: _render_default on updated_at must emit DEFAULT; got None",
    )
    sql_text = rendered.as_string(None) if rendered is not None else ""
    _check(
        "NOW() AT TIME ZONE 'UTC'" in sql_text,
        f"{renderer_label}: dispatcher clause must be NOW(): {sql_text!r}",
    )


def test_render_default_dispatches_non_pair_at_to_none(
    renderer: object, renderer_label: str,
) -> None:
    """End-to-end via _render_default: non-pair `_at` column emits no DEFAULT."""
    col_def = ColumnDefinition(type=ColumnType.DATETIME)
    rendered = renderer._render_default("restoration_at", col_def)  # type: ignore[attr-defined]
    _check(
        rendered is None,
        f"{renderer_label}: _render_default on restoration_at must NOT emit; got {rendered!r}",
    )


def run_suite_against(renderer: object, renderer_label: str) -> int:
    cases = (
        ("canonical_pair_gets_default", test_canonical_pair_gets_default),
        ("non_pair_at_columns_no_default", test_non_pair_at_columns_get_no_default),
        ("non_at_datetime_no_default", test_non_at_datetime_column_gets_no_default),
        ("canonical_pair_explicit_default_preserved", test_canonical_pair_with_explicit_default),
        ("explicit_contract_opt_in_works", test_explicit_contract_opt_in_still_works),
        (
            "render_default_dispatches_canonical_pair",
            test_render_default_dispatches_canonical_pair_through_auto_stamp,
        ),
        ("render_default_dispatches_non_pair_at", test_render_default_dispatches_non_pair_at_to_none),
    )
    failures = 0
    for name, fn in cases:
        try:
            fn(renderer, renderer_label)
        except AssertionError as exc:
            sys.stderr.write(f"FAIL {renderer_label}::{name}: {exc}\n")
            failures += 1
            continue
        sys.stdout.write(f"OK   {renderer_label}::{name}\n")
    return failures


def main() -> int:
    pg_failures = run_suite_against(pg_renderer, "postgres")
    rds_failures = run_suite_against(rds_renderer, "rds")
    total = pg_failures + rds_failures
    if total:
        sys.stderr.write(f"\n{total} smoke failure(s) across the canonical-pair restriction.\n")
        return 1
    sys.stdout.write("\nAll Tier 1.A audit-timestamp canonical-pair smokes passed (both renderers).\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
