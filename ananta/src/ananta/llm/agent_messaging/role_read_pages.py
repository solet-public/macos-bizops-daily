"""Identity and validation helpers for server-issued role-read pages."""

from __future__ import annotations

import hashlib
import secrets


def page_token() -> str:
    """Return an opaque 256-bit bearer token; only its digest is persisted."""
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def page_external_id(token_hash: str) -> str:
    return f"page:{token_hash}"


def page_item_external_id(page_external_id_value: str, role_row_id: str) -> str:
    material = f"{page_external_id_value}\x00{role_row_id}".encode()
    return f"item:{hashlib.sha256(material).hexdigest()}"


def held_role_scope_hash(roles: tuple[str, ...]) -> str:
    return hashlib.sha256("\n".join(sorted(roles)).encode("utf-8")).hexdigest()


def page_item_digest(items: list[tuple[str, str, str, str]]) -> str:
    """Digest the immutable page-item identity/content in a stable order."""
    material = "\n".join(
        "\x00".join(item) for item in sorted(items)
    )
    return hashlib.sha256(material.encode()).hexdigest()
