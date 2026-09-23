"""Lock down direct Postgres access to substrate-providing plugins only.

Background
----------

Per ``[[state-service-is-the-only-postgres-path]]``, no plugin may import
or use a Postgres driver directly — every Postgres read/write must go
through ``state_service`` (canonical), with the exception of the small
set of substrate-providing plugins that ARE the Postgres path. This
module installs a runtime guard that monkey-patches ``psycopg``'s
connection-opening entry points so that any attempt to open a Postgres
connection from outside the allowlist raises a loud ``RuntimeError``
naming the unauthorized module.

Authorized substrate-providing plugins
--------------------------------------

* ``postgres_state_management_plugin`` — canonical state-service substrate
* ``rds_postgres_state_management_plugin`` — cloud sibling
* ``pgvector_service_plugin`` — canonical vector-service substrate
* ``rds_pgvector_service_plugin`` — cloud sibling

Platform code (``ananta.*``) is also allowed — the platform itself reaches
through state_service abstractions which are tested separately. Operator
tooling under ``plugins/<x>/tools/*`` is allowed because its scope is
explicitly excluded from the runtime gates by the KB "Peer Pre-Completion
Gate Procedure" (operator-managed quality surface).

Everything else — vault, midwife, knowledge, blob, sound effects, etc.,
including any future plugin — must route through ``state_service`` or
``vector_service`` and will raise on direct ``psycopg`` use.

Operator directive 2026-06-09 PT: *"We need to lock down Postgres fully.
If any plugin other than the authorized plugin attempts to access Postgres
directly, that operation needs to fail loudly. Let us please complete
this work."*
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

logger = logging.getLogger(__name__)

# Substrate-providing plugins. Top-level module names (the segment before
# the first ``.`` in any imported module name).
_AUTHORIZED_TOP_LEVEL_MODULES: frozenset[str] = frozenset({
    "postgres_state_management_plugin",
    "rds_postgres_state_management_plugin",
    "pgvector_service_plugin",
    "rds_pgvector_service_plugin",
})

# Internal traversal markers — frames in these top-levels are skipped when
# walking the stack looking for the caller (they're either psycopg's own
# machinery or platform-side wiring that legitimately reaches Postgres
# only through state_service abstractions).
_TRAVERSAL_SKIP_PREFIXES: tuple[str, ...] = (
    "psycopg",
    "psycopg_pool",
    "ananta",
)

_installed: bool = False


def install_postgres_authorization_guard() -> None:
    """Patch ``psycopg`` connection entry points to enforce caller authorization.

    Idempotent: subsequent calls after the first are no-ops. Safe to
    call once at platform startup before any plugin's
    ``prepare_for_readiness`` fires.
    """
    global _installed
    if _installed:
        return

    import psycopg
    import psycopg_pool

    original_connect = psycopg.connect
    original_pool_init = psycopg_pool.ConnectionPool.__init__

    def checked_connect(*args: Any, **kwargs: Any) -> Any:
        _enforce_caller_authorization("psycopg.connect")
        return original_connect(*args, **kwargs)

    def checked_pool_init(self: Any, *args: Any, **kwargs: Any) -> None:
        _enforce_caller_authorization("psycopg_pool.ConnectionPool.__init__")
        original_pool_init(self, *args, **kwargs)

    psycopg.connect = checked_connect  # type: ignore[assignment]
    psycopg_pool.ConnectionPool.__init__ = checked_pool_init  # type: ignore[method-assign,assignment]

    _installed = True
    logger.info(
        "postgres_authorization_guard: installed (allowlist=%s)",
        sorted(_AUTHORIZED_TOP_LEVEL_MODULES),
    )


def _enforce_caller_authorization(operation: str) -> None:
    """Walk the call stack; raise if the caller is not in the allowlist."""
    frame = inspect.currentframe()
    if frame is not None:
        # Skip our own frame.
        frame = frame.f_back

    while frame is not None:
        module = frame.f_globals.get("__name__", "")
        top = module.split(".", 1)[0]
        if top in _AUTHORIZED_TOP_LEVEL_MODULES:
            return
        if top.startswith(_TRAVERSAL_SKIP_PREFIXES) or not top:
            frame = frame.f_back
            continue
        raise RuntimeError(
            f"postgres_authorization_guard: unauthorized direct Postgres "
            f"{operation} from module {module!r}. Only substrate-providing "
            f"plugins {sorted(_AUTHORIZED_TOP_LEVEL_MODULES)} may call "
            "psycopg directly; everyone else must go through state_service "
            "or vector_service per [[state-service-is-the-only-postgres-path]].",
        )

    # No identifying frame found — the call is internal to psycopg or
    # below it. Allow.
