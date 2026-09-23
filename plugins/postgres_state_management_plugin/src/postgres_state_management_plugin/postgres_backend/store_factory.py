"""Postgres-backed :class:`Store` factory — local profile.

Owned by `postgres_state_management_plugin` per W-STORE-POSTGRES-BACKEND-MOVE
(Tier 1, 2026-06-07). The factory used to live in
`macos_vault_plugin/postgres_backend/store.py`; the vault plugin now
imports + re-exports from this module via a thin shim so the factory
function object identity is preserved across import paths.

The cloud profile has its own parallel home at
`rds_postgres_state_management_plugin/postgres_backend/store_factory.py`.
The two factories are profile-specific siblings — each binds its own
sibling `PostgresProvider` from `.provider`. The cloud factory imports
ONLY RDS sibling code, never the local state plugin.

Concretely the adapter:

* runs the schema through :class:`SchemaStandardizer` at construction —
  same standardization that ``install_plugin_schema`` would apply, so
  the in-memory backend's standardized columns line up
* ``insert`` auto-generates ``id`` from ``schema.id_prefix`` in the
  adapter (the provider's ``insert()`` does NOT auto-id; only
  ``upsert()`` does)
* ``read`` injects ``is_deleted = 0`` into conditions unless the caller
  passes ``include_deleted=True`` — the provider's ``select()`` does
  not add that filter automatically
* ``update`` refuses an empty ``updates`` dict at the adapter layer so
  the provider never sees an invalid empty ``SET`` clause
* ``touch`` issues ``update_state(query, {"updated_at": now})`` — the
  table's BEFORE UPDATE trigger overrides the supplied value with
  ``NOW()``, producing the same observable "bump updated_at" semantics
  as the in-memory backend without coupling the adapter to raw SQL or
  forcing a new provider method
* ``psycopg.errors.UniqueViolation`` is caught and translated to
  :class:`~ananta.services.store.errors.UniqueViolationError` so
  callers don't branch by backend

The adapter is registered with the global factory at module import via
``register_backend("postgres", make_postgres_store)``. Smoke scripts
that need the Postgres backend ``import
postgres_state_management_plugin.postgres_backend.store_factory`` (or
go through one of the vault shims that re-exports this module) to
trigger registration. The platform's `register_backend` is idempotent
on the same function object, so multiple import paths reaching this
module are safe — see `ananta/src/ananta/services/store/factory.py:30`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import psycopg
from ananta.services.store import (
    NotNullViolationError,
    Store,
    UniqueViolationError,
    register_backend,
)
from ananta.services.store.errors import EmptyUpdateError
from ananta.services.store.protocol import Row
from ananta.types.schema_standardizer import SchemaStandardizer
from ananta.types.schema_types import SchemaDefinition, TableSchema

from .provider import PostgresProvider


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class PostgresStore:
    """Postgres-backed :class:`Store` over one :class:`TableSchema`."""

    def __init__(
        self,
        schema: TableSchema,
        namespace: str,
        *,
        provider: PostgresProvider,
    ) -> None:
        if not namespace:
            raise ValueError("namespace must be non-empty")
        standardized = SchemaStandardizer().standardize_schema(
            SchemaDefinition(
                namespace=namespace,
                tables={schema.table_name: schema},
            ),
        )
        self._schema: TableSchema = standardized.tables[schema.table_name]
        self._namespace = namespace
        self._provider = provider
        self._id_prefix: str = schema.id_prefix or "row"

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def insert(self, data: Row) -> str:
        record = self._fill_standard_fields(data)
        try:
            return self._provider.insert(
                namespace=self._namespace,
                table=self._schema.table_name,
                data=record,
            )
        except psycopg.errors.UniqueViolation as exc:
            raise self._unique_violation_from(exc, record) from exc
        except psycopg.errors.NotNullViolation as exc:
            raise self._not_null_violation_from(exc) from exc

    def update(self, conditions: Row, updates: Row) -> int:
        if not updates:
            raise EmptyUpdateError(
                "update() requires a non-empty updates dict; "
                "use touch() to bump updated_at only",
            )
        try:
            return self._provider.update(
                namespace=self._namespace,
                table=self._schema.table_name,
                conditions=conditions,
                updates=updates,
            )
        except psycopg.errors.UniqueViolation as exc:
            raise self._unique_violation_from(exc, dict(updates)) from exc

    def upsert(self, data: Row, conflict_columns: list[str]) -> str:
        """Insert if no row matches the conflict columns, else update.

        Implemented as read-then-route rather than ``INSERT ... ON
        CONFLICT DO UPDATE`` so the existing row's ``id`` is preserved
        on the update path.  The provider-level upsert path would
        overwrite the ``id`` column from EXCLUDED, which produces a
        new id on every update — the in-memory backend preserves the
        existing id, so the Postgres backend must match.

        This pattern also sidesteps the provider's ``_table_id_prefixes``
        dependency: plugin schemas installed via the DDL renderer (not
        via ``create_table``) never populate that map, so the provider's
        own upsert would raise on missing prefix.  Doing id generation
        in the adapter keeps the abstraction working regardless.
        """
        if not conflict_columns:
            raise ValueError("upsert() requires non-empty conflict_columns")
        conflict_conditions = {
            col: data[col] for col in conflict_columns if col in data
        }
        if len(conflict_conditions) != len(conflict_columns):
            raise ValueError(
                "upsert data missing values for conflict_columns: "
                f"need {conflict_columns}, got {sorted(conflict_conditions)}",
            )
        existing = self._provider.select(
            namespace=self._namespace,
            table=self._schema.table_name,
            conditions=conflict_conditions,
            limit=1,
        )
        if existing:
            existing_id = str(existing[0]["id"])
            updates = {k: v for k, v in data.items() if k != "id"}
            if updates:
                self.update({"id": existing_id}, updates)
            return existing_id
        return self.insert(data)

    def delete(self, conditions: Row, soft_delete: bool = True) -> int:
        if soft_delete:
            # update_state path: the BEFORE UPDATE trigger bumps
            # updated_at along with is_deleted, matching the in-memory
            # backend's "soft-delete also bumps updated_at" behavior.
            return self.update(conditions, {"is_deleted": 1})
        return self._provider.delete(
            namespace=self._namespace,
            table=self._schema.table_name,
            conditions=conditions,
            soft_delete=False,
        )

    def touch(self, conditions: Row) -> int:
        # The Postgres trigger overwrites NEW.updated_at = NOW() in any
        # UPDATE; passing a value-bearing updates dict is the cheapest
        # way to fire the trigger without raw SQL or a new provider
        # method.  The supplied value is discarded by the trigger.
        return self._provider.update(
            namespace=self._namespace,
            table=self._schema.table_name,
            conditions=conditions,
            updates={"updated_at": _now_iso()},
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def read(
        self,
        conditions: Row | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[Row]:
        effective = dict(conditions) if conditions else {}
        if not include_deleted and "is_deleted" not in effective:
            effective["is_deleted"] = 0
        return self._provider.select(
            namespace=self._namespace,
            table=self._schema.table_name,
            conditions=effective,
        )

    def read_one(
        self,
        conditions: Row,
        *,
        include_deleted: bool = False,
    ) -> Row | None:
        rows = self.read(conditions, include_deleted=include_deleted)
        if not rows:
            return None
        if len(rows) > 1:
            raise LookupError(
                f"read_one matched {len(rows)} rows for conditions={conditions!r}",
            )
        return rows[0]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _fill_standard_fields(self, data: Row) -> Row:
        """Auto-fill ``id``, ``namespace``, ``created_at``, ``updated_at``, ``is_deleted``.

        The provider's ``insert()`` does NOT auto-generate ``id`` (only
        its ``upsert()`` does, and only when ``_table_id_prefixes`` is
        populated for the table).  Filling them here keeps the adapter
        independent of whichever path the provider took to learn the
        table.
        """
        record = dict(data)
        if "id" not in record:
            record["id"] = f"{self._id_prefix}_{uuid4()}"
        now = _now_iso()
        record.setdefault("namespace", self._namespace)
        record.setdefault("created_at", now)
        record.setdefault("updated_at", now)
        record.setdefault("is_deleted", 0)
        return record

    def _unique_violation_from(
        self, exc: psycopg.errors.UniqueViolation, record: Row,
    ) -> UniqueViolationError:
        """Translate ``psycopg.UniqueViolation`` to the abstraction's error.

        Extracts the offending column from the diagnostic when
        psycopg surfaces one; falls back to scanning the record for any
        ``unique=True`` column.  Either way the resulting error is the
        same type the in-memory backend raises.
        """
        diag = getattr(exc, "diag", None)
        column_name = (
            getattr(diag, "column_name", None) if diag is not None else None
        )
        # psycopg often surfaces the constraint name (e.g.,
        # "idx_secret_key") rather than the column; walk the unique
        # columns and match by column value present in the record.
        column = column_name or self._infer_unique_column(record)
        value = record.get(column) if column else None
        return UniqueViolationError(
            column=column or "unknown",
            value=value,
            table=self._schema.table_name,
        )

    def _not_null_violation_from(
        self, exc: psycopg.errors.NotNullViolation,
    ) -> NotNullViolationError:
        diag = getattr(exc, "diag", None)
        column = (
            getattr(diag, "column_name", None) if diag is not None else None
        ) or "unknown"
        return NotNullViolationError(
            column=column, table=self._schema.table_name,
        )

    def _infer_unique_column(self, record: Row) -> str | None:
        for col_name, col_def in self._schema.columns.items():
            if col_def.unique and col_name in record:
                return col_name
        return None


def make_postgres_store(
    schema: TableSchema,
    namespace: str,
    **kwargs: Any,
) -> Store:
    """Backend factory called from :func:`ananta.services.store.open_store`.

    Accepts either a ``provider`` (raw :class:`PostgresProvider`) or a
    ``state_service`` (the high-level :class:`StateManagementInterface`
    implemented by :class:`PostgresStateManagementPlugin`); resolves to
    the underlying provider in both cases.  Smoke scripts typically
    pass a freshly-constructed ``provider``; in-process callers pass
    the live ``state_service`` resolved from the orchestrator.
    """
    provider = kwargs.pop("provider", None)
    state_service = kwargs.pop("state_service", None)
    if kwargs:
        raise TypeError(
            f"postgres backend got unexpected kwargs: {sorted(kwargs)}",
        )
    if provider is None:
        if state_service is None:
            raise ValueError(
                "postgres backend requires either 'provider' or 'state_service'",
            )
        provider = _resolve_provider(state_service)
    return PostgresStore(schema, namespace, provider=provider)


def _resolve_provider(state_service: object) -> PostgresProvider:
    """Extract a :class:`PostgresProvider` from a state-service handle.

    What ``orchestrator.get_service("state_service")`` returns is
    ``ananta.services.state_service.StateService`` — a wrapper that
    holds the actual state-management plugin under ``_state_plugin``.
    Unwrap one level before looking for the provider hook.

    The plugin exposes its provider via the private ``_get_provider()``
    method; falls back to a ``provider`` attribute for any future plugin
    that surfaces it publicly.

    Structural type-check via ``hasattr(provider, "get_connection")``
    rather than ``isinstance(provider, PostgresProvider)``: the latter
    would reject providers from the sibling RDS state plugin or any
    future state plugin that satisfies the contract without sharing
    PostgresProvider's class identity. The contract is the methods
    PostgresStore actually calls — not the class hierarchy.
    """
    inner = getattr(state_service, "_state_plugin", state_service) or state_service
    getter = getattr(inner, "_get_provider", None)
    provider = getter() if callable(getter) else getattr(inner, "provider", None)
    if provider is None or not hasattr(provider, "get_connection"):
        raise TypeError(
            "state_service does not expose a PostgresProvider-shaped object; "
            f"got {type(provider).__name__}",
        )
    return provider  # type: ignore[no-any-return]


register_backend("postgres", make_postgres_store)


__all__ = ["PostgresStore", "make_postgres_store"]
