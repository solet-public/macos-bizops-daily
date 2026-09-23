#!/usr/bin/env python3
"""Smoke: joseki string constants mirror their ``schema.py`` CHECK constraints.

``constants.py`` declares two families of string constants that are documented
as the CANONICAL mirror of two Postgres ``CHECK`` constraints in ``schema.py``:

* ``JOSEKI_STATE_*`` <-> ``thinking_authored_joseki.state`` (iss_aad77d89)
* ``JOSEKI_RUN_STATUS_*`` <-> ``thinking_joseki_run.status`` (iss_6f0be043)

Nothing enforced the mirror. ``authored_lifecycle_smoke.py`` drives the
lifecycle over an in-memory row double that never evaluates the CHECK, so a
constant added or renamed on one side only failed at the first real database
write. This smoke imports BOTH modules and asserts set-equality between the
constants (enumerated by name prefix off the module, so a new constant on that
side is seen too) and the allowed-value list PARSED out of the CHECK expression
the schema actually declares -- never a third hand-copied list to drift.

Coverage:

1. Each CHECK expression parses as ``<column> IN ('a', 'b', ...)`` with a
   non-empty value list naming the expected column.
2. ``JOSEKI_STATE_*`` values == the ``thinking_authored_joseki.state`` CHECK
   value set; ``JOSEKI_STATES`` (the aggregate frozenset) equals it too.
3. ``JOSEKI_RUN_STATUS_*`` values == the ``thinking_joseki_run.status`` CHECK
   value set.
4. Each family is non-empty and each constant value is distinct (a copy-paste
   duplicate would otherwise shrink the set and hide a missing member).

Offline: no live solet, no LM Studio, no Postgres -- both modules are pure
declarations in the same package.

Run directly:
    .venv/bin/python3 \\
      plugins/default_thinking_plugin/tests/joseki_constant_schema_drift_smoke.py
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
for _src in (
    _REPO_ROOT / "ananta" / "src",
    _REPO_ROOT / "plugins" / "default_thinking_plugin" / "src",
):
    if str(_src) not in sys.path:
        sys.path.insert(0, str(_src))

from default_thinking_plugin import constants as _constants  # noqa: E402
from default_thinking_plugin.schema import get_thinking_schema  # noqa: E402

_CHECKS_RUN: list[str] = []

# ``<column> IN ('v1', 'v2', ...)`` -- the only CHECK shape this schema uses
# for enumerated columns. Anything else fails loud rather than parsing to an
# empty set that would compare equal to nothing.
_IN_LIST_RE = re.compile(
    r"^\s*(?P<column>[A-Za-z_][A-Za-z0-9_]*)\s+IN\s*\((?P<values>[^)]*)\)\s*$",
)
_QUOTED_VALUE_RE = re.compile(r"'([^']*)'")

# (table, column, constants-name prefix, aggregate frozenset name or None)
_MIRRORS: tuple[tuple[str, str, str, str | None], ...] = (
    ("thinking_authored_joseki", "state", "JOSEKI_STATE_", "JOSEKI_STATES"),
    ("thinking_joseki_run", "status", "JOSEKI_RUN_STATUS_", None),
)


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_values(table: str, column: str) -> frozenset[str]:
    """Parse the allowed-value set out of the column's CHECK expression."""
    tables = get_thinking_schema().tables
    _check(
        f"{table} declared",
        table in tables,
        f"table not in schema (have: {sorted(tables)})",
    )
    columns = tables[table].columns
    _check(
        f"{table}.{column} declared",
        column in columns,
        f"column not in table (have: {sorted(columns)})",
    )
    check = columns[column].check
    _check(
        f"{table}.{column} has a CHECK",
        check is not None,
        "column declares no CHECK constraint",
    )
    matched = _IN_LIST_RE.match(check or "")
    _check(
        f"{table}.{column} CHECK is an IN-list",
        matched is not None,
        f"CHECK does not parse as '<column> IN (...)': {check!r}",
    )
    if matched is None:  # narrowed for the type checker; _check already raised
        raise SmokeFailureError("unreachable")
    _check(
        f"{table}.{column} CHECK names its own column",
        matched.group("column") == column,
        f"CHECK constrains {matched.group('column')!r}, not {column!r}",
    )
    values = _QUOTED_VALUE_RE.findall(matched.group("values"))
    _check(
        f"{table}.{column} CHECK list is non-empty",
        len(values) > 0,
        f"no quoted values in {check!r}",
    )
    _check(
        f"{table}.{column} CHECK list has no duplicates",
        len(values) == len(set(values)),
        f"duplicate values in {check!r}",
    )
    return frozenset(values)


def _constant_values(prefix: str) -> frozenset[str]:
    """Every ``<prefix>*`` string constant on the constants module, by value."""
    named = {
        name: value
        for name, value in vars(_constants).items()
        if name.startswith(prefix) and isinstance(value, str)
    }
    _check(
        f"{prefix}* constants exist",
        len(named) > 0,
        "no string constants with that prefix on constants.py",
    )
    _check(
        f"{prefix}* constant values are distinct",
        len(set(named.values())) == len(named),
        f"duplicate values across {sorted(named)}",
    )
    return frozenset(named.values())


def _run_mirror(
    table: str, column: str, prefix: str, aggregate_name: str | None,
) -> None:
    schema_values = _check_values(table, column)
    constant_values = _constant_values(prefix)
    _check(
        f"{prefix}* == {table}.{column} CHECK",
        constant_values == schema_values,
        f"constants-only={sorted(constant_values - schema_values)} "
        f"schema-only={sorted(schema_values - constant_values)}",
    )
    if aggregate_name is None:
        return
    aggregate = getattr(_constants, aggregate_name, None)
    _check(
        f"{aggregate_name} is a frozenset",
        isinstance(aggregate, frozenset),
        f"got {type(aggregate).__name__}",
    )
    _check(
        f"{aggregate_name} == {prefix}* values",
        aggregate == constant_values,
        f"aggregate={sorted(aggregate or ())} constants={sorted(constant_values)}",
    )


def main() -> int:
    try:
        for table, column, prefix, aggregate_name in _MIRRORS:
            _run_mirror(table, column, prefix, aggregate_name)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(
        f"OK: joseki constant/schema drift guard -- {len(_CHECKS_RUN)} checks, "
        f"{len(_MIRRORS)} constant families mirror their CHECK constraints",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
