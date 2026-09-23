#!/usr/bin/env python3
"""Live dual-twin smoke for the 4th capability primitive — ``acquire_lease``.

The expiry-fenced lease-acquire CAS: the disjunctive
``(lease_column IS NULL OR lease_column < :now)`` predicate the flat
equality / ``= ANY`` / ``is_null`` filter grammar cannot express, compiled
to ONE atomic ``UPDATE ... RETURNING id`` so the row lock, the free-or-expired
check, and the write are atomic (no read-then-write TOCTOU). Consumer shape:
``polling_driver.try_acquire_polling_lease`` on ``session_ledger__source``.

Runs the REAL local-postgres AND RDS provider classes against the LOCAL
Postgres (the RDS provider connects via plain conninfo for localhost — cloud
IAM is NOT required); a one-sided twin fix fails loudly because every
behavioral case is parametrized over both provider classes.

Coverage:

* **Builder composition (pure).** ``build_acquire_lease_returning`` on BOTH
  twins emits the parenthesized disjunct ``(... IS NULL OR ... < %s)``, a
  ``RETURNING id``, param order ``[set..., filters..., now]``, and serializes
  a tz-aware ``now`` to NAIVE UTC (the F1 seam) — never binds it raw. Twin SQL
  is byte-identical.
* **Free / expired → acquired.** A NULL or strictly-past lease is claimed;
  the ``set`` columns (new expiry + fresh token) are written.
* **Held → rejected (the disjunct is load-bearing).** A live (future) lease is
  NOT claimed; ``acquired=False`` and the row is left UNCHANGED (a stale would-be
  acquirer cannot steal a held lease).
* **Strict ``<`` boundary.** ``lease_until == now`` is NOT acquirable (the
  predicate is ``< :now``, not ``<=``).
* **``updated_at`` trigger.** ``set`` omits ``updated_at``; a successful acquire
  still advances it — VERIFIES the BEFORE-UPDATE trigger maintains it on a
  lease-shaped table (so the consumer need not write it).
* **tz-aware threshold vs stored naive-UTC.** A tz-aware ``now`` compares
  correctly against stored naive-UTC expiries (the F1 datetime-CAS guard): a
  tz-aware threshold past the stored expiry acquires; before it, rejects.
* **Facade validation (both twins).** A missing/invalid ``now`` returns a clean
  error ``ActionResult`` (``acquire_lease.invalid_now``), never an uncaught raise.
* **REAL ``session_ledger__source`` trigger coverage.** Introspects
  ``information_schema.triggers`` for the live ``session_ledger__source`` table
  and asserts its ``updated_at`` BEFORE-UPDATE trigger exists — verified, not
  inferred from a sibling table.

Sandboxed via temporary schemas (one per provider); cleanup drops them in a
``finally``. Env-gated behind ``ACQUIRE_LEASE_SMOKE=1``.

Run::

    ACQUIRE_LEASE_SMOKE=1 \\
      .venv/bin/python3 \\
      plugins/postgres_state_management_plugin/tests/acquire_lease_smoke.py
"""

from __future__ import annotations

import json
import os
import secrets
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, LiteralString, cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"),
)
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src"),
)

from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from postgres_state_management_plugin.plugin import (  # noqa: E402
    PostgresStatePlugin,
)
from postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider,
)
from postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    serialize_value_for_txn as pg_serialize_txn,
)
from rds_postgres_state_management_plugin.plugin import (  # noqa: E402
    RdsPostgresStateManagementPlugin,
)
from rds_postgres_state_management_plugin.postgres_backend.config import (  # noqa: E402
    PostgresConfig as RdsConfig,
)
from rds_postgres_state_management_plugin.postgres_backend.provider import (  # noqa: E402
    PostgresProvider as RdsProvider,
)
from rds_postgres_state_management_plugin.rds_crud import (  # noqa: E402
    state_acquire_lease as rds_state_acquire_lease,
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
    _REPO_ROOT / "profile" / "config" / "plugins"
    / "postgres_state_management_plugin.json"
)

_NS = "leaseprobe"
_TABLE = "lease"
_PHYSICAL = f"{_NS}__{_TABLE}"
_LEASE_COL = "polling_lease_until"
_TOKEN_COL = "polling_lease_token"

# A fixed naive-UTC reference instant; cases derive past/future from it so the
# stored values mirror production's naive-UTC lease columns (F1 TZ seam).
_T0 = datetime(2026, 6, 20, 12, 0, 0)


def _raw_config() -> dict[str, Any]:
    return json.loads(_PROFILE_PG_CONFIG.read_text(encoding="utf-8"))


def _create_probe_table(provider: PostgresProvider | RdsProvider) -> None:
    """Create the lease-shaped probe table WITH the platform updated_at trigger."""
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'CREATE TABLE "{provider.config.schema_name}"."{_PHYSICAL}" ('
                "id text PRIMARY KEY, "
                f"{_LEASE_COL} timestamp, "
                f"{_TOKEN_COL} text, "
                "updated_at timestamp NOT NULL DEFAULT now(), "
                "is_deleted integer NOT NULL DEFAULT 0)",
            )
        )
        # Attach the real BEFORE-UPDATE updated_at trigger (function created by
        # provider.initialize()), so the trigger-coverage case is behavioral.
        provider._create_updated_at_trigger(cur, _PHYSICAL)  # noqa: SLF001


def _drop_schema(provider: PostgresProvider | RdsProvider, schema: str) -> None:
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(cast(LiteralString, f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def _seed(
    provider: PostgresProvider | RdsProvider,
    row_id: str,
    until: datetime | None,
    token: str | None,
) -> None:
    provider.insert(
        _NS,
        _TABLE,
        {"id": row_id, _LEASE_COL: until, _TOKEN_COL: token, "is_deleted": 0},
    )


def _row(provider: PostgresProvider | RdsProvider, row_id: str) -> dict[str, Any]:
    # Raw cursor: returns NATIVE datetimes (provider.select ISO-stringifies via
    # _serialize_for_json, which would break datetime equality assertions).
    with provider.get_transactional_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                f'SELECT * FROM "{provider.config.schema_name}"."{_PHYSICAL}" '
                "WHERE id = %s",
            ),
            (row_id,),
        )
        row = cur.fetchone()
    if row is None:
        raise RuntimeError(f"probe row {row_id!r} not found")
    return dict(cast("dict[str, Any]", row))


def _acquire(
    provider: PostgresProvider | RdsProvider,
    row_id: str,
    now: datetime,
    new_until: datetime,
    token: str,
) -> bool:
    return provider.acquire_lease(
        namespace=_NS,
        table=_TABLE,
        filters={"id": row_id, "is_deleted": 0},
        lease_column=_LEASE_COL,
        now=now,
        set_values={_LEASE_COL: new_until, _TOKEN_COL: token},
    )


def _acquire_raises(
    provider: PostgresProvider | RdsProvider, filters: dict[str, Any]
) -> bool:
    """True iff ``provider.acquire_lease(filters)`` raises ValueError — the
    single-row PK contract rejecting a non-scalar-``id`` (broad/empty/list/op)
    filter before any UPDATE."""
    try:
        provider.acquire_lease(
            namespace=_NS,
            table=_TABLE,
            filters=filters,
            lease_column=_LEASE_COL,
            now=_T0,
            set_values={_LEASE_COL: _T0 + timedelta(minutes=10), _TOKEN_COL: "x"},
        )
        return False
    except ValueError:
        return True


# --- pure composition (no DB) -----------------------------------------------


def case_builder_composition(label: str, provider: PostgresProvider | RdsProvider) -> str:
    aware = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone(timedelta(hours=5)))
    composed, params = provider.build_acquire_lease_returning(
        _NS,
        _TABLE,
        {"id": "r1", "is_deleted": 0},
        _LEASE_COL,
        aware,
        {_LEASE_COL: aware, _TOKEN_COL: "tok"},
    )
    with provider.get_connection() as conn:
        rendered = composed.as_string(conn)
    naive = pg_serialize_txn(aware)  # the real F1 seam: tz-aware -> naive UTC
    _check(
        f'"{_LEASE_COL}" IS NULL OR "{_LEASE_COL}" <' in rendered
        and "RETURNING id" in rendered,
        f"[{label}] builder emits the disjunctive ({_LEASE_COL} IS NULL OR "
        f"{_LEASE_COL} < %s) predicate + RETURNING id",
    )
    # param order: [set... (until, token), filters... (id, is_deleted), now]
    _check(
        params[-1] == naive and aware not in params,
        f"[{label}] tz-aware now serialized to NAIVE UTC as the LAST param "
        f"(F1 seam; not bound raw) (params={params!r})",
    )
    _check(
        params[0] == naive and params[1] == "tok" and params[2] == "r1" and params[3] == 0,
        f"[{label}] param order is [set..., filters..., now] (params={params!r})",
    )
    return rendered


# --- behavioral (both providers) --------------------------------------------


def case_acquire_free(label: str, provider: PostgresProvider | RdsProvider) -> None:
    _seed(provider, "free", None, None)
    acquired = _acquire(provider, "free", _T0, _T0 + timedelta(minutes=10), "tok-free")
    row = _row(provider, "free")
    _check(
        acquired
        and row[_TOKEN_COL] == "tok-free"
        and row[_LEASE_COL] == _T0 + timedelta(minutes=10),
        f"[{label}] free (NULL) lease acquired; new until+token written",
    )


def case_acquire_expired(label: str, provider: PostgresProvider | RdsProvider) -> None:
    _seed(provider, "expired", _T0 - timedelta(minutes=5), "stale")
    acquired = _acquire(
        provider, "expired", _T0, _T0 + timedelta(minutes=10), "tok-new"
    )
    row = _row(provider, "expired")
    _check(
        acquired and row[_TOKEN_COL] == "tok-new",
        f"[{label}] expired lease (until < now) acquired; token rotated",
    )


def case_held_rejected(label: str, provider: PostgresProvider | RdsProvider) -> None:
    held_until = _T0 + timedelta(minutes=10)
    _seed(provider, "held", held_until, "owner")
    acquired = _acquire(
        provider, "held", _T0, _T0 + timedelta(minutes=99), "thief"
    )
    row = _row(provider, "held")
    _check(
        not acquired
        and row[_TOKEN_COL] == "owner"
        and row[_LEASE_COL] == held_until,
        f"[{label}] HELD (future) lease NOT acquired; row UNCHANGED "
        "(disjunct is load-bearing)",
    )


def case_strict_boundary(label: str, provider: PostgresProvider | RdsProvider) -> None:
    _seed(provider, "boundary", _T0, "owner")
    acquired = _acquire(
        provider, "boundary", _T0, _T0 + timedelta(minutes=10), "thief"
    )
    row = _row(provider, "boundary")
    _check(
        not acquired and row[_TOKEN_COL] == "owner",
        f"[{label}] until == now is NOT acquirable (strict <, not <=)",
    )


def case_updated_at_trigger(label: str, provider: PostgresProvider | RdsProvider) -> None:
    _seed(provider, "trig", None, None)
    before = _row(provider, "trig")["updated_at"]
    acquired = _acquire(
        provider, "trig", _T0, _T0 + timedelta(minutes=10), "tok-trig"
    )
    after = _row(provider, "trig")["updated_at"]
    _check(
        acquired and after > before,
        f"[{label}] acquire advances updated_at though 'set' omits it "
        f"(BEFORE-UPDATE trigger covers the probe table) (before={before}, after={after})",
    )


def case_tz_aware_threshold(label: str, provider: PostgresProvider | RdsProvider) -> None:
    # Stored expiries are naive-UTC (as production writes them); thresholds come
    # in tz-aware (the injected clock). The CAS must compare correctly.
    _seed(provider, "tz-past", _T0 - timedelta(hours=1), "stale")
    _seed(provider, "tz-future", _T0 + timedelta(hours=1), "owner")
    now_aware = _T0.replace(tzinfo=UTC)
    got_past = _acquire(
        provider, "tz-past", now_aware, _T0 + timedelta(minutes=10), "tok-tz"
    )
    got_future = _acquire(
        provider, "tz-future", now_aware, _T0 + timedelta(minutes=10), "thief"
    )
    _check(
        got_past and not got_future,
        f"[{label}] tz-aware now vs stored naive-UTC: past acquires, future "
        "rejects (F1 datetime-CAS, no skew)",
    )


def case_single_row_contract(
    label: str, provider: PostgresProvider | RdsProvider
) -> None:
    """A lease is identity-targeted (Codex BLOCKER 2026-06-20): a filter that
    does not pin a SINGLE row by a scalar PK ``id`` is REJECTED before any
    UPDATE — a broad / empty / list-valued-id filter must NOT silently acquire
    leases on every matched row — and a scalar-id acquire never touches a
    sibling row."""
    _seed(provider, "sr-a", None, None)
    _seed(provider, "sr-b", None, None)
    bad_cases: tuple[tuple[str, dict[str, Any]], ...] = (
        ("broad/no-id", {"is_deleted": 0}),
        ("empty", {}),
        ("list-id(=ANY)", {"id": ["sr-a", "sr-b"], "is_deleted": 0}),
        # operator-form id (e.g. `id IS NOT NULL`) would match EVERY row — the
        # most dangerous broad shape; the scalar-PK guard must reject it too.
        ("op-id(is_not_null)", {"id": {"op": "is_not_null"}, "is_deleted": 0}),
    )
    for bad_label, bad_filters in bad_cases:
        raised = _acquire_raises(provider, bad_filters)
        a = _row(provider, "sr-a")
        b = _row(provider, "sr-b")
        _check(
            raised and a[_TOKEN_COL] is None and b[_TOKEN_COL] is None,
            f"[{label}] {bad_label} filter REJECTED (ValueError) + acquires NEITHER "
            f"row (raised={raised}, a={a[_TOKEN_COL]!r}, b={b[_TOKEN_COL]!r})",
        )
    _seed(provider, "sr-keep", None, None)
    _seed(provider, "sr-sib", None, None)
    got = _acquire(provider, "sr-keep", _T0, _T0 + timedelta(minutes=10), "kept")
    keep = _row(provider, "sr-keep")
    sib = _row(provider, "sr-sib")
    _check(
        got and keep[_TOKEN_COL] == "kept" and sib[_TOKEN_COL] is None,
        f"[{label}] scalar-id acquire claims exactly ONE row; sibling untouched "
        f"(got={got}, keep={keep[_TOKEN_COL]!r}, sib={sib[_TOKEN_COL]!r})",
    )


def case_facade_validation(
    label: str, provider: PostgresProvider | RdsProvider, acquire_fn: Any
) -> None:
    """The facade rejects a bad 'now' cleanly and wraps success in an ActionResult.

    ``ActionResult`` is a TypedDict: an error result carries a non-None
    ``error`` key; a success result carries ``error=None`` + the data envelope.
    """
    bad = acquire_fn(
        {
            "table": _TABLE,
            "filters": {"id": "x"},
            "lease_column": _LEASE_COL,
            "now": "2026-06-20T12:00:00",  # ISO string, not a datetime
            "set": {_LEASE_COL: _T0},
        }
    )
    _check(
        isinstance(bad, dict) and bad.get("error") is not None,
        f"[{label}] facade rejects a non-datetime 'now' with an error ActionResult "
        f"(result={bad!r})",
    )
    _seed(provider, "facade-ok", None, None)
    ok = acquire_fn(
        {
            "table": _TABLE,
            "filters": {"id": "facade-ok", "is_deleted": 0},
            "lease_column": _LEASE_COL,
            "now": _T0,
            "set": {_LEASE_COL: _T0 + timedelta(minutes=10), _TOKEN_COL: "ftok"},
        }
    )
    acquired = (
        ok.get("data", {}).get("result", {}).get("acquired")
        if isinstance(ok, dict)
        else None
    )
    _check(
        isinstance(ok, dict) and ok.get("error") is None and acquired is True,
        f"[{label}] facade success returns a clean acquired=True ActionResult "
        f"(result={ok!r})",
    )
    broad = acquire_fn(
        {
            "table": _TABLE,
            "filters": {"is_deleted": 0},  # no scalar id -> single-row violation
            "lease_column": _LEASE_COL,
            "now": _T0,
            "set": {_LEASE_COL: _T0 + timedelta(minutes=10), _TOKEN_COL: "broad"},
        }
    )
    _check(
        isinstance(broad, dict) and broad.get("error") is not None,
        f"[{label}] facade rejects a non-single-row (broad, no scalar id) filter "
        f"with an error ActionResult (result={broad!r})",
    )


# --- real session_ledger__source trigger coverage ---------------------------


def case_real_source_schema(provider: PostgresProvider | RdsProvider) -> None:
    """Verify (not infer) the REAL session_ledger__source schema the primitive
    presupposes: the updated_at BEFORE-UPDATE trigger, and a
    ``timestamp without time zone`` lease column (the F1 naive-UTC seam on which
    the serializer + the ``< :now`` comparison depend — if it were ``timestamptz``
    the probe is unfaithful and the design premise shifts)."""
    with provider.get_connection() as conn, conn.cursor() as cur:
        cur.execute(
            cast(
                LiteralString,
                "SELECT trigger_name FROM information_schema.triggers "
                "WHERE event_object_table = %s "
                "AND action_timing = 'BEFORE' AND event_manipulation = 'UPDATE'",
            ),
            ("session_ledger__source",),
        )
        triggers = [str(dict(r).get("trigger_name", "")) for r in cur.fetchall()]
        cur.execute(
            cast(
                LiteralString,
                "SELECT data_type FROM information_schema.columns "
                "WHERE table_name = %s AND column_name = %s",
            ),
            ("session_ledger__source", _LEASE_COL),
        )
        col_rows = [dict(r) for r in cur.fetchall()]
    _check(
        any("update_updated_at" in t for t in triggers),
        "REAL session_ledger__source carries its updated_at BEFORE-UPDATE trigger "
        f"(consumer may omit updated_at from 'set') (triggers={triggers!r})",
    )
    data_type = col_rows[0].get("data_type") if col_rows else None
    _check(
        data_type == "timestamp without time zone",
        f"REAL session_ledger__source.{_LEASE_COL} is 'timestamp without time zone' "
        "(the naive-UTC F1 seam the serializer + < :now comparison presuppose; my "
        f"probe matches) (data_type={data_type!r})",
    )


def case_implementers_concrete() -> None:
    """The new @abstractmethod is satisfied by BOTH plugin classes (empty
    ``__abstractmethods__`` => concrete => load-safe), and ``acquire_lease`` IS a
    required abstractmethod — so the third implementer (the inline
    ``StateServiceAdapter`` instantiated at platform startup) must define it too,
    which it now does (StateService forwards to the bound plugin)."""
    _check(
        "acquire_lease" in StateManagementInterface.__abstractmethods__,
        "acquire_lease is a required @abstractmethod on StateManagementInterface "
        "(every implementer must define it or fail at instantiation)",
    )
    _check(
        not PostgresStatePlugin.__abstractmethods__,
        "PostgresStatePlugin is concrete — no unimplemented abstractmethods "
        f"(remaining={set(PostgresStatePlugin.__abstractmethods__)})",
    )
    _check(
        not RdsPostgresStateManagementPlugin.__abstractmethods__,
        "RdsPostgresStateManagementPlugin is concrete — no unimplemented abstractmethods "
        f"(remaining={set(RdsPostgresStateManagementPlugin.__abstractmethods__)})",
    )


def _make_local(schema: str) -> PostgresProvider:
    cfg = PostgresConfig(**_raw_config())
    cfg.pg_schema = schema
    p = PostgresProvider(cfg)
    p.initialize()
    return p


def _make_rds(schema: str) -> RdsProvider:
    cfg = RdsConfig(**_raw_config())
    cfg.pg_schema = schema
    p = RdsProvider(cfg)
    p.initialize()
    return p


def _run_behavioral(label: str, provider: PostgresProvider | RdsProvider) -> None:
    case_acquire_free(label, provider)
    case_acquire_expired(label, provider)
    case_held_rejected(label, provider)
    case_strict_boundary(label, provider)
    case_updated_at_trigger(label, provider)
    case_tz_aware_threshold(label, provider)
    case_single_row_contract(label, provider)


def main() -> int:
    if os.environ.get("ACQUIRE_LEASE_SMOKE") != "1":
        print(
            "  SKIP  ACQUIRE_LEASE_SMOKE != 1; creates/drops sandbox schemas in "
            "the live DB.",
        )
        return 0

    local_schema = f"example_test_lease_local_{secrets.token_hex(4)}"
    rds_schema = f"example_test_lease_rds_{secrets.token_hex(4)}"
    local = _make_local(local_schema)
    rds = _make_rds(rds_schema)
    try:
        # Pure composition + twin SQL parity.
        local_sql = case_builder_composition("local", local)
        rds_sql = case_builder_composition("rds", rds)
        _check(
            local_sql.replace(local.config.schema_name, "<schema>")
            == rds_sql.replace(rds.config.schema_name, "<schema>"),
            "twin build_acquire_lease_returning SQL byte-identical (schema-normalized)",
        )

        _create_probe_table(local)
        _create_probe_table(rds)

        _run_behavioral("local", local)
        _run_behavioral("rds", rds)

        # Facade validation: local inline facade + rds_crud function.
        local_plugin = PostgresStatePlugin()
        local_plugin._provider = local  # noqa: SLF001
        case_facade_validation(
            "local-facade", local, lambda d: local_plugin.acquire_lease(_NS, d)
        )
        case_facade_validation(
            "rds-facade", rds, lambda d: rds_state_acquire_lease(rds, _NS, d)
        )

        # ABC blast-radius: both plugin classes concrete (abstractmethod satisfied).
        case_implementers_concrete()
        # Real ledger __source schema: updated_at trigger + lease-column type.
        case_real_source_schema(local)
    finally:
        _drop_schema(local, local_schema)
        _drop_schema(rds, rds_schema)

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
