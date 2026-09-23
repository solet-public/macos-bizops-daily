"""Backend-agnostic :class:`Store` abstraction.

Public surface:

* :class:`Store` — Protocol every backend satisfies
* :func:`open_store` — factory that resolves ``backend=`` to a registered implementation
* :func:`register_backend` — used by Postgres adapter to self-register
* error types — :class:`StoreError`, :class:`UniqueViolationError`,
  :class:`NotNullViolationError`, :class:`EmptyUpdateError`

The :class:`InMemoryStore` is also exported for callers that want to
construct one directly (skipping the factory) — useful for tests and
for consumers that prefer constructor injection over a factory lookup.
"""

from __future__ import annotations

from .errors import (
    EmptyUpdateError,
    NotNullViolationError,
    StoreError,
    UniqueViolationError,
)
from .factory import (
    BackendFactory,
    list_backends,
    open_store,
    register_backend,
)
from .in_memory import InMemoryStore
from .protocol import Row, Store

__all__ = [
    "BackendFactory",
    "EmptyUpdateError",
    "InMemoryStore",
    "NotNullViolationError",
    "Row",
    "Store",
    "StoreError",
    "UniqueViolationError",
    "list_backends",
    "open_store",
    "register_backend",
]
