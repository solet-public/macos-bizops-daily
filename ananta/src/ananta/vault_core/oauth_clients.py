"""Shared OAuth-client policy used by both vault plugins.

Extracted in Task #31 so the new ``grant_types`` allowlist + the
projection that adds ``operator_approved`` + ``grant_types`` to the
public client metadata are defined once and consumed twice. Without
this module, both ``macos_vault_plugin`` and
``secrets_manager_vault_plugin`` (which are already 3000+ line god
classes) would each carry their own duplicate of the same policy —
making them worse, not better.

Plugin actions translate :class:`OauthGrantValidationError` into
their local ``ActionResult`` error envelope; the helpers themselves
are pure Python (no platform plumbing, no I/O, no @platform_process).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .records import OAUTH_ALLOWED_GRANT_TYPES

# Default per-client grant set when the caller does not specify one.
# Matches the claude.ai connector shape: browser OAuth flow + silent
# token renewal. Operator-created machine clients pass
# ``["client_credentials"]`` explicitly.
DEFAULT_OAUTH_GRANT_TYPES: list[str] = ["authorization_code", "refresh_token"]


class OauthGrantValidationError(ValueError):
    """Raised when oauth_client_register's grant_types input is invalid.

    The plugin action catches this and surfaces the message in its
    local ``ActionResult`` error envelope. Subclasses ValueError so
    callers that prefer duck-typed handling still work.
    """


def normalize_oauth_grant_types(raw: object | None) -> list[str]:
    """Validate + normalize a ``grant_types`` value from the action params.

    Accepts None (caller omitted the field) and returns the default.
    Accepts a list of strings whose every element is in
    :data:`OAUTH_ALLOWED_GRANT_TYPES`. Rejects anything else with
    :class:`OauthGrantValidationError`.

    The validation policy is intentionally fail-closed:
        - non-list input -> error
        - empty list -> error (a client with no usable grants is
          unreachable and likely an operator mistake)
        - any value outside the allowlist -> error (no silent
          accept of typos like "code" or "password")
    """
    if raw is None:
        return list(DEFAULT_OAUTH_GRANT_TYPES)
    if not isinstance(raw, list):
        raise OauthGrantValidationError(
            "grant_types must be a list of strings",
        )
    grants = [str(g) for g in raw]
    invalid = [g for g in grants if g not in OAUTH_ALLOWED_GRANT_TYPES]
    if invalid:
        raise OauthGrantValidationError(
            f"grant_types contains values outside the allowlist: "
            f"{invalid}. Allowed: {sorted(OAUTH_ALLOWED_GRANT_TYPES)}",
        )
    if not grants:
        raise OauthGrantValidationError(
            "grant_types must contain at least one allowed value",
        )
    return grants


def _list_of_strings_or_empty(raw: object) -> list[str]:
    """Coerce ``raw`` to ``list[str]``; empty list on any non-list input.

    Defensive helper for projecting JSON-column fields whose backing
    type may degrade if the row was written by an older schema
    version: a missing column or a malformed payload becomes ``[]``,
    never a TypeError.
    """
    if isinstance(raw, list):
        return [str(item) for item in raw]
    return []


def project_oauth_client_metadata(
    client_id: str, row: Mapping[str, object],
) -> dict[str, Any]:
    """Project a stored OAuth-client row to the public-metadata shape.

    Shared between ``lookup_oauth_client`` and
    ``verify_oauth_client_credentials`` on BOTH vault plugins so the
    field set is identical regardless of the storage backend.

    Security-critical projection rules (Task #31):
        ``operator_approved`` defaults to ``False`` when the underlying
        field is missing OR is anything other than the bool ``True``
        (strict identity check via ``is True``). Never infer approval
        from a side channel like timestamps. Non-bool truthy values
        (``1``, ``"true"``, ...) DO NOT pass.

        ``grant_types`` defaults to ``[]`` when the field is missing
        or malformed — every grant is rejected at /oauth/token until
        the operator explicitly re-registers the client.

    Never includes secret material; the cleartext + hash + salt
    columns are intentionally not projected.
    """
    return {
        "client_id": client_id,
        "client_name": str(row.get("client_name") or ""),
        "scopes": _list_of_strings_or_empty(row.get("scopes")),
        "redirect_uris": _list_of_strings_or_empty(row.get("redirect_uris")),
        "operator_approved": row.get("operator_approved") is True,
        "operator_equivalent": row.get("operator_equivalent") is True,
        "machine_grant_enabled": row.get("machine_grant_enabled") is True,
        "grant_types": _list_of_strings_or_empty(row.get("grant_types")),
    }


def normalize_oauth_register_params(
    params: Mapping[str, object],
) -> tuple[str, list[str], list[str], list[str]]:
    """Coerce + validate the four oauth_client_register input fields.

    Returns ``(client_name, scopes, redirect_uris, grant_types)``.
    Raises :class:`OauthGrantValidationError` on a non-string /
    whitespace-only ``client_name`` (caller's contract: missing name
    is invalid) or on any ``grant_types`` value outside the allowlist.
    Scopes default to ``["mcp:read", "mcp:write"]``; redirect_uris
    default to ``[]``; grant_types defaults to
    :data:`DEFAULT_OAUTH_GRANT_TYPES`.

    Shared by both vault plugins' ``oauth_client_register_action`` so
    the parsing rules cannot drift between local and cloud.
    """
    client_name_raw = params.get("client_name")
    if not isinstance(client_name_raw, str) or not client_name_raw.strip():
        raise OauthGrantValidationError(
            "client_name must be a non-empty string",
        )
    client_name = client_name_raw.strip()
    scopes_raw = params.get("scopes") or ["mcp:read", "mcp:write"]
    scopes = _list_of_strings_or_empty(scopes_raw) or ["mcp:read", "mcp:write"]
    redirect_uris = _list_of_strings_or_empty(params.get("redirect_uris"))
    grant_types = normalize_oauth_grant_types(params.get("grant_types"))
    return client_name, scopes, redirect_uris, grant_types


__all__ = [
    "DEFAULT_OAUTH_GRANT_TYPES",
    "OauthGrantValidationError",
    "normalize_oauth_grant_types",
    "normalize_oauth_register_params",
    "project_oauth_client_metadata",
]
