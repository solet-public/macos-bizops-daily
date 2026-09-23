"""Verb implementations — pure functions over an already-open connection.

Same shape as external_postgres_plugin's query_actions.py: take an
already-built connection and a ``params`` dict, return plain result dicts.
Export-path containment (``path_gate``) is injected. ``run_query``,
``export_query``, and (when its statement produces a result set)
``run_statement`` ALWAYS write their result to the caller-supplied
``output_tsv_path`` and return a handle only — never rows inline, at any size
(business-data limits + data-export migration, 2026-08-02; the former
inline-return/``INLINE_BYTE_CAP`` branch is deleted, not lowered). The
effective row ceiling is DEFAULT_ROW_LIMIT unless the caller supplies BOTH
``acknowledge_default_limit_override=true`` and an explicit ``row_limit`` (up
to the verb's hard cap) — see ``_resolve_effective_limit``. Invalid params
raise ``ValueError`` (-> snowflake.invalid_params); guard violations raise
``StatementGuardError``; driver errors propagate to the plugin's
``classify_snowflake_error``.

``run_statement`` is this module's WRITE verb (operator ruling 2026-08-09 +
Amendment 1, "vendor RBAC is the control plane"): it performs NO statement
classification of any kind — ``assert_read_statement`` (the read/write
ACCESS-CONTROL decision) never appears on this path, and unlike
external_postgres_plugin's ``run_statement`` there is no
``assert_single_statement`` SHAPE guard to reuse either: Snowflake's
``MULTI_STATEMENT_COUNT`` session parameter defaults to 1 and this connector
never calls ``execute_string``, so a two-statement string is refused natively
by the driver (verified live against the operator's own account, 2026-08-10 —
a harmless read-only ``"SELECT 1; SELECT 2"`` probe through ``run_query``
failed at the driver rather than silently running both). What the statement
is actually allowed to do is decided entirely by the server-side role grants
on the registered credential. Snowflake also has no session-level read-only
CONNECTION characteristic (connection.py) and — unlike psycopg3 — defaults
every session to ``AUTOCOMMIT=TRUE`` (each statement commits or rolls back on
its own the instant it finishes; verified against the installed
``snowflake-connector-python`` source and Snowflake's own transactions
documentation, 2026-08-10). ``run_statement`` explicitly disables autocommit
on its own connection before executing so a statement that turns out to
produce a result set (e.g. a RETURNING clause, where supported) but arrives
with no ``output_tsv_path`` can still be rolled back instead of silently
committing while discarding the returned rows — every other verb runs
unaffected under Snowflake's normal per-statement autocommit default.

This module is S2-exempt (whole-file): its ``SHOW``/information-schema-style
introspection and the caller's own ``run_query``/``export_query``/
``run_statement`` text target the operator-registered Snowflake account only
(containment invariant #1 — no account parameter on any verb). Introspection
queries bind identifiers via Snowflake's ``fully_qualified`` list results,
never string-interpolating caller-controlled filter values into a WHERE
clause.
"""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Callable
from typing import Any

from .constants import (
    DEFAULT_ROW_LIMIT,
    EXPORT_ROW_CAP,
    MAX_ROWS_HARD_CAP,
    PARAM_ACKNOWLEDGE_OVERRIDE,
    PARAM_ROW_LIMIT,
)
from .statement_guard import assert_read_statement

# path_gate(output_tsv_path) -> realpath-resolved path to write, or raises
# ExportPathRefusedError. Injected by the plugin, which binds the operator's
# export_allowed_roots config (export_containment.assert_export_path_allowed).
PathGate = Callable[[str], str]


def run_query(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Run one read-only statement; write the result to output_tsv_path, return a handle.

    Defaults to DEFAULT_ROW_LIMIT rows; an acknowledged override may request
    up to MAX_ROWS_HARD_CAP. For pulls beyond that ceiling, use export_query
    (up to EXPORT_ROW_CAP with the same override).
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=MAX_ROWS_HARD_CAP, verb="run_query",
    )
    sql = _require_str(params, "sql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    assert_read_statement(sql)
    return _execute_and_write_tsv(conn, sql, effective_limit, path_gate, output_tsv_path)


def run_statement(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Run ONE statement; server-side role grants decide what it can do.

    No statement-leader classification exists on this path, by design (module
    docstring's Amendment 1 note) — ``assert_read_statement`` is never called
    here. Snowflake defaults every session to ``AUTOCOMMIT=TRUE``, so this
    function explicitly disables it on its own connection FIRST, before the
    caller's statement ever executes: a statement with no result set (the
    common INSERT/UPDATE/DELETE/DDL case) then commits explicitly and returns
    ``rowcount`` inline. A statement that DOES produce a result set (e.g. a
    RETURNING clause, where the target object supports one) is routed through
    the SAME always-TSV export path as run_query — rows are never returned
    inline, at any size — so ``output_tsv_path`` is required in that case; its
    absence rolls the statement back rather than silently discarding the
    returned rows while still committing the write. Which branch fires is
    decided by the driver's own ``cur.description`` AFTER executing, never by
    inspecting the SQL text beforehand.
    """
    sql = _require_str(params, "sql")
    output_tsv_path = params.get("output_tsv_path")
    resolved_path: str | None = None
    if isinstance(output_tsv_path, str) and output_tsv_path.strip():
        resolved_path = path_gate(output_tsv_path)
        parent_dir = os.path.dirname(resolved_path)
        if not os.path.isdir(parent_dir):
            raise ValueError(
                f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
                "create it first — this verb writes one file, it does not create directories"
            )
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=MAX_ROWS_HARD_CAP, verb="run_statement",
    )
    conn.autocommit(False)
    with conn.cursor() as cur:
        cur.execute(sql)
        has_result_set = cur.description is not None
        if not has_result_set:
            rowcount = cur.rowcount
            conn.commit()
            return {"rowcount": rowcount, "has_result_set": False}
        if resolved_path is None:
            conn.rollback()
            raise ValueError(
                "the statement produced a result set (e.g. a RETURNING clause) but no "
                "output_tsv_path was supplied; rows are never returned inline — pass "
                "output_tsv_path to receive them as a TSV, the same always-export contract "
                "as run_query"
            )
        columns = [d[0] for d in cur.description]
        rows = cur.fetchmany(effective_limit)
        rowcount = cur.rowcount
    row_lists = [_row_to_list(r) for r in rows]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    conn.commit()
    return {
        "rowcount": rowcount,
        "has_result_set": True,
        "path": resolved_path,
        "row_count": len(row_lists),
        "columns": columns,
        "truncated": len(row_lists) >= effective_limit,
    }


def list_databases(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List databases visible to the current role."""
    with conn.cursor() as cur:
        cur.execute("SHOW DATABASES")
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    return {"databases": [{"name": _cell(r, name_idx)} for r in rows]}


def list_schemas(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List schemas in a database (identifier passed via Snowflake's own IN clause)."""
    database = _require_str(params, "database")
    with conn.cursor() as cur:
        cur.execute(f'SHOW SCHEMAS IN DATABASE "{_quote_ident(database)}"')
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    return {"schemas": [_cell(r, name_idx) for r in rows]}


def list_tables(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """List tables in a database.schema."""
    database = _require_str(params, "database")
    schema = _require_str(params, "schema")
    with conn.cursor() as cur:
        cur.execute(
            f'SHOW TABLES IN SCHEMA "{_quote_ident(database)}"."{_quote_ident(schema)}"'
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    kind_idx = _column_index(columns, "kind")
    return {
        "tables": [
            {"name": _cell(r, name_idx), "kind": _cell(r, kind_idx)} for r in rows
        ]
    }


def describe_table(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Describe a table's columns."""
    database = _require_str(params, "database")
    schema = _require_str(params, "schema")
    table = _require_str(params, "table")
    with conn.cursor() as cur:
        cur.execute(
            f'DESCRIBE TABLE "{_quote_ident(database)}"."{_quote_ident(schema)}".'
            f'"{_quote_ident(table)}"'
        )
        rows = cur.fetchall()
        columns = [d[0] for d in cur.description] if cur.description else []
    name_idx = _column_index(columns, "name")
    type_idx = _column_index(columns, "type")
    null_idx = _column_index(columns, "null?")
    default_idx = _column_index(columns, "default")
    return {
        "columns": [
            {
                "name": _cell(r, name_idx),
                "type": _cell(r, type_idx),
                "nullable": _cell(r, null_idx) == "Y",
                "default": _cell(r, default_idx) or None,
            }
            for r in rows
        ]
    }


def test_connection(conn: Any, params: dict[str, Any]) -> dict[str, Any]:
    """Confirm the connection: account, user, role, warehouse, server version."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT CURRENT_ACCOUNT(), CURRENT_USER(), CURRENT_ROLE(), "
            "CURRENT_WAREHOUSE(), CURRENT_VERSION()"
        )
        row = cur.fetchone()
    return {
        "ok": True,
        "account": _as_str(row[0]) if row else "",
        "user": _as_str(row[1]) if row else "",
        "role": _as_str(row[2]) if row else "",
        "warehouse": _as_str(row[3]) if row else "",
        "version": _as_str(row[4]) if row else "",
    }


def export_query(conn: Any, params: dict[str, Any], path_gate: PathGate) -> dict[str, Any]:
    """Export a read-only query's result as a workspace TSV — the N>>500 route.

    Same caller-supplied-path destination and override mechanism as
    run_query; only the override's hard cap differs (EXPORT_ROW_CAP, not
    MAX_ROWS_HARD_CAP). Absent the override this also defaults to
    DEFAULT_ROW_LIMIT — for the common small/default case, run_query has an
    identical interface with a lower ceiling. The gate runs BEFORE the
    statement executes, so a refused path never sends the query.
    """
    effective_limit = _resolve_effective_limit(
        params, default=DEFAULT_ROW_LIMIT, hard_cap=EXPORT_ROW_CAP, verb="export_query",
    )
    sql = _require_str(params, "sql")
    output_tsv_path = _require_str(params, "output_tsv_path")
    assert_read_statement(sql)
    return _execute_and_write_tsv(conn, sql, effective_limit, path_gate, output_tsv_path)


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _resolve_effective_limit(
    params: dict[str, Any], *, default: int, hard_cap: int, verb: str,
) -> int:
    """Resolve the effective fetch ceiling from the §5 override pair.

    Absent (or ``acknowledge_default_limit_override`` not exactly ``True``)
    with no ``row_limit``: returns ``default``. Both must be given together —
    the override flag alone, or ``row_limit`` alone, fails loud rather than
    silently honoring half. A ``row_limit`` above ``hard_cap`` is refused,
    never silently clamped back down.
    """
    override = params.get(PARAM_ACKNOWLEDGE_OVERRIDE)
    row_limit = params.get(PARAM_ROW_LIMIT)
    override_present = override is True
    row_limit_present = row_limit is not None
    if override_present != row_limit_present:
        raise ValueError(
            f"{verb}: '{PARAM_ACKNOWLEDGE_OVERRIDE}' and '{PARAM_ROW_LIMIT}' must be "
            f"given together — got {PARAM_ACKNOWLEDGE_OVERRIDE}={override!r}, "
            f"{PARAM_ROW_LIMIT}={row_limit!r}"
        )
    if not override_present:
        return default
    if not isinstance(row_limit, int) or isinstance(row_limit, bool) or row_limit < 1:
        raise ValueError(f"{verb}: '{PARAM_ROW_LIMIT}' must be a positive integer")
    if row_limit > hard_cap:
        raise ValueError(
            f"{verb}: '{PARAM_ROW_LIMIT}'={row_limit} exceeds the hard cap of "
            f"{hard_cap}; refusing rather than silently clamping"
        )
    return row_limit


def _execute_and_write_tsv(
    conn: Any, sql: str, cap: int, path_gate: PathGate, output_tsv_path: str,
) -> dict[str, Any]:
    """Admit the path, fetch up to ``cap`` rows, write a TSV, return a handle.

    The containment gate runs BEFORE the statement executes, so a refused
    path never sends the query. ``fetchmany(cap)`` bounds the fetch at the
    cursor/network level — never fetch-everything-then-truncate.
    """
    resolved_path = path_gate(output_tsv_path)
    parent_dir = os.path.dirname(resolved_path)
    if not os.path.isdir(parent_dir):
        raise ValueError(
            f"the parent directory of output_tsv_path does not exist ({parent_dir}); "
            "create it first — this verb writes one file, it does not create directories"
        )
    columns, rows = _execute_fetch(conn, sql, cap)
    row_lists = [_row_to_list(r) for r in rows]
    with open(resolved_path, "wb") as handle:
        handle.write(_to_tsv(columns, row_lists))
    return {
        "path": resolved_path,
        "row_count": len(row_lists),
        "columns": columns,
        "truncated": len(row_lists) >= cap,
    }


def _execute_fetch(conn: Any, sql: str, cap: int) -> tuple[list[str], list[tuple[Any, ...]]]:
    with conn.cursor() as cur:
        cur.execute(sql)
        columns = [d[0] for d in cur.description] if cur.description else []
        rows = cur.fetchmany(cap) if columns else []
    return columns, rows


def _row_to_list(row: tuple[Any, ...]) -> list[Any]:
    return [_jsonable(val) for val in row]


def _to_tsv(columns: list[str], row_lists: list[list[Any]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(row_lists)
    return buffer.getvalue().encode("utf-8")


def _jsonable(value: Any) -> Any:
    """Coerce a warehouse value to a JSON-safe form; non-primitives become their str repr."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _require_str(params: dict[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"'{key}' is required and must be a non-empty string")
    return value


def _as_str(value: Any) -> str:
    return value if isinstance(value, str) else "" if value is None else str(value)


def _column_index(columns: list[str], name: str) -> int:
    lowered = [c.lower() for c in columns]
    try:
        return lowered.index(name.lower())
    except ValueError:
        return -1


def _cell(row: tuple[Any, ...], idx: int | None) -> str:
    if idx is None or idx < 0 or idx >= len(row):
        return ""
    value = row[idx]
    return _as_str(value)


def _quote_ident(identifier: str) -> str:
    """Escape a double-quote inside a caller-supplied Snowflake identifier.

    Identifiers here come from typed params (database/schema/table), never
    from the free-text ``sql``/``run_query`` surface — this only prevents an
    identifier containing ``"`` from breaking out of its quoting.
    """
    return identifier.replace('"', '""')
