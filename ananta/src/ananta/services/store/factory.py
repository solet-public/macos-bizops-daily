"""Backend registry + :func:`open_store` factory for :class:`Store`.

Backends register themselves by name (``in_memory``, ``postgres``).
``open_store`` looks up the registered factory and delegates.  Splitting
registration from invocation keeps the core ``services/store`` package
free of plugin imports — the Postgres adapter registers itself when its
module is imported by the plugin manager (or directly by a smoke
script).

Backend-specific arguments (e.g., ``state_service`` for ``postgres``)
are forwarded as keyword arguments to the registered factory.  Each
backend's factory documents its own required kwargs.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from ananta.types.schema_types import TableSchema

from .in_memory import InMemoryStore
from .protocol import Store

BackendFactory = Callable[..., Store]

_BACKENDS: dict[str, BackendFactory] = {}


def register_backend(name: str, factory: BackendFactory) -> None:
    """Register ``factory`` as the implementation for ``backend=name``.

    Idempotent re-registration with the same factory is a no-op; a
    different factory under the same name raises :class:`ValueError`
    so two backends can't silently shadow each other.
    """
    existing = _BACKENDS.get(name)
    if existing is factory:
        return
    if existing is not None:
        raise ValueError(
            f"backend {name!r} already registered with a different factory; "
            "the second registration would shadow the first",
        )
    _BACKENDS[name] = factory


def list_backends() -> list[str]:
    """Return registered backend names (diagnostics)."""
    return sorted(_BACKENDS.keys())


def open_store(
    schema: TableSchema,
    namespace: str,
    *,
    backend: str = "postgres",
    **kwargs: Any,
) -> Store:
    """Open a :class:`Store` bound to ``(namespace, schema)``.

    ``backend`` selects the registered backend implementation; any
    additional keyword arguments forward to that backend's factory.

    The in-memory backend takes no extra kwargs.  The Postgres backend
    requires ``state_service``.
    """
    factory = _BACKENDS.get(backend)
    if factory is None:
        raise ValueError(
            f"unknown backend {backend!r}; registered: {list_backends()}",
        )
    return factory(schema, namespace, **kwargs)


def _make_in_memory_store(
    schema: TableSchema, namespace: str, **kwargs: Any,
) -> Store:
    if kwargs:
        raise TypeError(
            f"in_memory backend takes no extra kwargs; got {sorted(kwargs)}",
        )
    return InMemoryStore(schema, namespace)


register_backend("in_memory", _make_in_memory_store)


__all__ = [
    "BackendFactory",
    "list_backends",
    "open_store",
    "register_backend",
]
