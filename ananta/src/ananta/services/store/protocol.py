"""Backend-agnostic CRUD :class:`Store` protocol.

A :class:`Store` is opened against one :class:`~ananta.types.schema_types.TableSchema`
declaration.  The same declaration works against any registered backend
(currently ``in_memory`` and ``postgres``).  Standard fields (``id``,
``created_at``, ``updated_at``, ``namespace``, ``is_deleted``, ...) are
auto-filled by the backend; consumers never set them by hand.

The seven methods are intentionally narrow — equality-match conditions,
no joins, no query language, no transactions across stores.  Consumers
that need anything richer drop down to the underlying state-management
service directly.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

Row = dict[str, Any]


@runtime_checkable
class Store(Protocol):
    """Backend-agnostic CRUD over one ``TableSchema`` shape."""

    def insert(self, data: Row) -> str:
        """Insert one row; return the auto-generated id.

        Auto-fills ``id`` (using ``schema.id_prefix``), ``namespace``,
        ``created_at``, ``updated_at``, and ``is_deleted=0``.  Raises
        :class:`~ananta.services.store.errors.UniqueViolationError` if
        the row collides with an existing ``unique=True`` column value
        (including soft-deleted rows, matching Postgres semantics).
        """
        ...

    def update(self, conditions: Row, updates: Row) -> int:
        """Update rows matching ``conditions``; bump ``updated_at``.

        Returns the number of rows changed.  Refuses an empty
        ``updates`` dict — call :meth:`touch` to bump activity-only.
        """
        ...

    def upsert(self, data: Row, conflict_columns: list[str]) -> str:
        """Insert or update on conflict; return the row id."""
        ...

    def delete(self, conditions: Row, soft_delete: bool = True) -> int:
        """Soft-delete (``is_deleted=1``) or hard-delete matching rows.

        Soft-delete also bumps ``updated_at``.  Returns the count.
        """
        ...

    def read(
        self,
        conditions: Row | None = None,
        *,
        include_deleted: bool = False,
    ) -> list[Row]:
        """Return matching rows.

        Excludes ``is_deleted=1`` rows by default; pass
        ``include_deleted=True`` to include them.  The state-management
        read path does NOT filter ``is_deleted`` automatically, so the
        adapter injects the filter explicitly.
        """
        ...

    def read_one(
        self,
        conditions: Row,
        *,
        include_deleted: bool = False,
    ) -> Row | None:
        """Return at most one matching row, ``None`` if none match."""
        ...

    def touch(self, conditions: Row) -> int:
        """Bump ``updated_at`` on matching rows without changing other fields.

        Returns the count.  Real backend primitive on both stores: the
        in-memory backend writes ``updated_at = now()`` directly; the
        Postgres backend issues ``UPDATE ... SET updated_at = updated_at``
        so the BEFORE UPDATE trigger fires.  Never implemented as
        ``update(conditions, {})`` — that produces an invalid empty SET
        clause on the Postgres provider.
        """
        ...


__all__ = ["Row", "Store"]
