"""Context ID resolution and validation.

Pure functions for resolving context_id from action parameters and
validating context_id format. Replaces plugin methods:
  _validate_context_id, _resolve_context_id, _resolve_explicit,
  _resolve_plugin_root, _resolve_address_book
"""

from __future__ import annotations

import re
from typing import Any, Protocol


class ContextRegistry(Protocol):
    """Narrow protocol for context registry operations."""

    def get_context(self, context_id: str) -> dict[str, Any] | None: ...

    def get_or_create_plugin_root_context(
        self, plugin_namespace: str,
    ) -> str: ...


_CONTEXT_ID_RE = re.compile(r"^ctx-[a-z0-9]+$")


def validate_context_id(
    context_id: str,
    *,
    registry: ContextRegistry | None = None,
    verify_exists: bool = True,
) -> str:
    """Validate context_id format and optionally verify existence.

    Ensures context_id follows state-service format (ctx- prefix) and
    optionally verifies it exists in the registry. Prevents path traversal
    and creation of orphan context streams.

    Args:
        context_id: Context ID to validate.
        registry: Optional context registry for existence checks.
        verify_exists: If True, verify context exists in registry.

    Returns:
        Validated context_id (unchanged if valid).

    Raises:
        RuntimeError: If context_id is invalid, unsafe, or does not exist.
    """
    if not context_id:
        raise RuntimeError("context_id cannot be empty")

    if ".." in context_id or "/" in context_id or "\\" in context_id:
        raise RuntimeError(
            f"Invalid context_id '{context_id}': "
            "contains path traversal characters",
        )

    if not _CONTEXT_ID_RE.match(context_id):
        raise RuntimeError(
            f"Invalid context_id '{context_id}': must match ctx-<id> format",
        )

    if verify_exists and registry is not None:
        existing = registry.get_context(context_id)
        if not existing:
            raise RuntimeError(
                f"context_id '{context_id}' not found in registry",
            )

    return context_id


def _resolve_explicit(
    explicit_id: Any,
    *,
    registry: ContextRegistry | None,
) -> str:
    """Resolve context_id when source is EXPLICIT."""
    if not explicit_id:
        raise RuntimeError(
            "context_id_source=explicit requires context_id in params/state",
        )
    return validate_context_id(str(explicit_id), registry=registry)


def _resolve_plugin_root(
    explicit_id: Any,
    *,
    provider_name: str,
    registry: ContextRegistry,
) -> str:
    """Resolve context_id when source is PLUGIN_ROOT."""
    if explicit_id:
        return validate_context_id(str(explicit_id), registry=registry)
    return str(
        registry.get_or_create_plugin_root_context(
            plugin_namespace=provider_name,
        ),
    )


def _resolve_address_book(
    explicit_id: Any,
    address_key: str | None,
    *,
    registry: ContextRegistry | None,
) -> str:
    """Resolve context_id when source is ADDRESS_BOOK."""
    if explicit_id:
        return validate_context_id(str(explicit_id), registry=registry)
    if not address_key:
        raise RuntimeError(
            "context_id_source=address_book requires "
            "context.id_address_key config",
        )
    raise RuntimeError(
        f"ADDRESS_BOOK resolution not yet implemented (key: {address_key})",
    )


def resolve_context_id(
    action_params: dict[str, Any],
    state: dict[str, Any],
    context_id_source: str,
    *,
    provider_name: str,
    registry: ContextRegistry,
    address_key: str | None = None,
) -> str:
    """Resolve context_id based on configured source.

    Fail-fast: raises RuntimeError if context_id cannot be resolved for the
    configured source (no silent fallbacks).

    Args:
        action_params: Action parameters potentially containing context_id.
        state: Runtime state potentially containing context_id.
        context_id_source: The ContextIdSource value string.
        provider_name: Provider name for plugin_root resolution.
        registry: Context registry for lookup/creation.
        address_key: Optional key for address_book resolution.

    Returns:
        Validated context_id string.

    Raises:
        RuntimeError: If resolution fails for any reason.
    """
    from ananta.services.context_management.types import ContextIdSource

    explicit_id = action_params.get("context_id") or state.get("context_id")

    source = ContextIdSource(context_id_source)

    if source is ContextIdSource.EXPLICIT:
        return _resolve_explicit(explicit_id, registry=registry)

    if source is ContextIdSource.PLUGIN_ROOT:
        return _resolve_plugin_root(
            explicit_id,
            provider_name=provider_name,
            registry=registry,
        )

    if source is ContextIdSource.ADDRESS_BOOK:
        return _resolve_address_book(
            explicit_id, address_key, registry=registry,
        )

    raise RuntimeError(f"Unknown context_id_source: {context_id_source}")
