#!/usr/bin/env python3
"""M6.5 Bug 3 cross-host shipper verification SQL shape smoke.

Run:

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/cross_host_subagent_push_smoke.py

Per 2026-06-11 M6.5 Bug 3 cross-host shipper section (Coordinator-Dawn
dispatch §5): after Bug 3 lands locally, the cross-host shipper is
redeployed to ``luna-lucid-librarian`` by the operator via
``apply_manifest``. **Implementer scope is VERIFY-DON'T-TOUCH** on the
shipper — this smoke does NOT exercise the live shipper or attempt the
luna-side ``apply_manifest`` (that's operator scope). It does verify
that the corrected verification SQL JOIN form (per Claude-B v2 brief
review patch P1) yields the expected row-shape:

    SELECT COUNT(*) FROM session_ledger__event e
    JOIN session_ledger__session s ON e.session_id = s.id
    WHERE s.source_kind = 'claude_code_local'
      AND s.external_session_id = '<planted-canary-uuid>'

Note: ``vendor_session_label`` lives on ``__session`` (not ``__event``).
The earlier draft used a non-existent ``__event.vendor_session_label``
column path; the corrected form joins through ``__session``. This smoke
confirms (via the existing schema definitions) that the JOIN form's
column references resolve.

Verifications:

1. ``session_ledger__event`` table schema declares ``session_id``
   (the JOIN key) and ``id`` (PK).
2. ``session_ledger__session`` table schema declares ``id``,
   ``source_kind`` (via the ``__source`` join not directly on the
   row), and ``external_session_id``.
3. The actual filter column ``source_kind`` lives on
   ``__source`` (one level of indirection beyond what the brief
   wrote); the canonical cross-host verification SQL JOIN must
   include the additional join from ``__session`` to ``__source``.
   This smoke surfaces that indirection so the operator's actual
   verification SQL gets the right shape.
4. ``external_session_id`` IS on ``__session`` (filterable directly
   per the brief).

Note: the smoke does NOT plant rows or execute SQL against the live
DB; it inspects the schema declarations to confirm column residency.
The live-DB JOIN execution would be operator-side after the luna
``apply_manifest``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))


def _expect(condition: bool, message: str) -> None:
    if not condition:
        print(f"FAIL: {message}", file=sys.stderr)
        sys.exit(1)
    print(f"PASS: {message}")


def test_event_table_has_session_id_and_id_columns() -> None:
    from ananta.llm.session_ledger.schema import (
        TABLE_EVENT,
        get_session_ledger_schema,
    )

    schema = get_session_ledger_schema()
    event_table = schema.tables[TABLE_EVENT]
    cols = set(event_table.columns.keys())
    # ``id`` is platform-injected from ``id_prefix='evt'`` (TableSchema
    # convention) — it is NOT in the explicit ``columns`` dict but is
    # always available as the PK on every persisted row.
    _expect(
        "session_id" in cols,
        f"__event has session_id (JOIN key); got {sorted(cols)[:8]}…",
    )
    _expect(
        event_table.id_prefix == "evt",
        f"__event PK is platform-injected via id_prefix='evt'; got "
        f"id_prefix={event_table.id_prefix!r}",
    )


def test_session_table_has_id_external_session_id_source_id() -> None:
    from ananta.llm.session_ledger.schema import (
        TABLE_SESSION,
        get_session_ledger_schema,
    )

    schema = get_session_ledger_schema()
    sess_table = schema.tables[TABLE_SESSION]
    cols = set(sess_table.columns.keys())
    # ``id`` is platform-injected from ``id_prefix='les'`` (TableSchema
    # convention) — same shape as TABLE_EVENT above.
    _expect(
        "external_session_id" in cols and "source_id" in cols,
        f"__session has external_session_id + source_id; got {sorted(cols)[:10]}…",
    )
    _expect(
        sess_table.id_prefix == "les",
        f"__session PK is platform-injected via id_prefix='les'; got "
        f"id_prefix={sess_table.id_prefix!r}",
    )


def test_source_kind_lives_on_source_not_session() -> None:
    """Surface the source_kind column residency to the operator.

    The dispatch brief's verification SQL filters on
    ``s.source_kind = 'claude_code_local'``, but ``source_kind`` is a
    column on ``__source`` — NOT directly on ``__session``. The
    operator's actual verification SQL must therefore add a second
    JOIN from ``__session`` to ``__source`` to filter by source_kind.

    Corrected canonical shape:

    SELECT COUNT(*)
    FROM session_ledger__event e
    JOIN session_ledger__session s ON e.session_id = s.id
    JOIN session_ledger__source src ON s.source_id = src.id
    WHERE src.source_kind = 'claude_code_local'
      AND s.external_session_id = '<planted-canary-uuid>'
    """
    from ananta.llm.session_ledger.schema import (
        TABLE_SESSION,
        TABLE_SOURCE,
        get_session_ledger_schema,
    )

    schema = get_session_ledger_schema()
    sess_table = schema.tables[TABLE_SESSION]
    src_table = schema.tables[TABLE_SOURCE]
    sess_cols = set(sess_table.columns.keys())
    src_cols = set(src_table.columns.keys())
    _expect(
        "source_kind" not in sess_cols,
        "source_kind NOT on __session (operator's verification SQL needs extra JOIN)",
    )
    _expect(
        "source_kind" in src_cols,
        f"source_kind IS on __source; got source cols {sorted(src_cols)[:8]}…",
    )


def main() -> None:
    print("M6.5 Bug 3 — cross-host shipper verification SQL JOIN-shape smoke")
    print("=" * 60)
    test_event_table_has_session_id_and_id_columns()
    test_session_table_has_id_external_session_id_source_id()
    test_source_kind_lives_on_source_not_session()
    print("=" * 60)
    print("ALL PASS")
    print()
    print("NOTE FOR OPERATOR + COORDINATOR-DAWN:")
    print("  The canonical verification SQL the brief sketched needs ONE")
    print("  more JOIN to filter on source_kind, because source_kind lives")
    print("  on __source (not __session):")
    print()
    print("    SELECT COUNT(*) FROM session_ledger__event e")
    print("    JOIN session_ledger__session s ON e.session_id = s.id")
    print("    JOIN session_ledger__source src ON s.source_id = src.id")
    print("    WHERE src.source_kind = 'claude_code_local'")
    print("      AND s.external_session_id = '<planted-canary-uuid>'")
    print()
    print("  Operator's luna-side verification run should use this shape.")


if __name__ == "__main__":
    main()
