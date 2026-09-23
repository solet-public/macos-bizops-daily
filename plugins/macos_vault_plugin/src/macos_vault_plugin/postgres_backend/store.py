"""Postgres-backed :class:`Store` shim — re-export from the local state plugin.

Per W-STORE-POSTGRES-BACKEND-MOVE (Tier 1, 2026-06-07): the canonical
factory + `PostgresStore` class now live in
`postgres_state_management_plugin.postgres_backend.store_factory`. This
file is preserved as the existing import path
(`macos_vault_plugin.postgres_backend.store`) — vault `plugin.py` and
external smoke scripts continue to import this module without change.

Importing this module transitively imports the canonical
`store_factory` module, whose module-level
`register_backend("postgres", make_postgres_store)` registers the
backend with the global Store factory. Both this shim and the canonical
factory module expose the exact same `make_postgres_store` and
`PostgresStore` objects — Python's `sys.modules` cache returns the same
module on every import, so `register_backend` is idempotent on the same
function object across any number of import paths.

Per dispatch isolation discipline:
* canonical absolute import of the local state plugin's factory module
* no relative import / path alias / wrapper function / copied factory
* no module reload
* `__all__` re-exports the same function/class objects (preserves
  `is`-identity equality across consumers)
"""

from __future__ import annotations

from postgres_state_management_plugin.postgres_backend.store_factory import (
    PostgresStore,
    make_postgres_store,
)

__all__ = ["PostgresStore", "make_postgres_store"]
