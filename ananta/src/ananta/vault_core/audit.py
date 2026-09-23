"""Audit helpers for vault plugin secret-transfer tracking.

Both vault plugins emit audit rows and own their own per-plugin audit table.
The schema function and row builder are shared here; each plugin instantiates
its table under its own namespace (e.g. ``default_vault_plugin__audit``,
``secrets_manager_vault_plugin__audit``).
"""

from __future__ import annotations

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    TableSchema,
)

from .records import utc_now_iso

# Audit directions and statuses — stable strings shared by both plugins.
AUDIT_DIRECTION_EXPORT = "export"
AUDIT_DIRECTION_IMPORT = "import"

AUDIT_STATUS_SUCCESS = "success"
AUDIT_STATUS_ERROR = "error"

# Table identity used by both vault plugins.
AUDIT_TABLE_NAME = "audit"
AUDIT_ID_PREFIX = "sta"


def get_audit_schema() -> TableSchema:
    """Append-only audit log for cross-solet sealed-box secret transfer.

    One row per ``export_encrypted`` / ``import_encrypted`` call, success or
    failure. Each vault plugin owns its own table instance under its own
    namespace; this function returns the shared schema definition.

    Standard fields (``id``, ``created_at``, ``updated_at``, ...) are
    auto-injected by :class:`SchemaStandardizer`.
    """
    return TableSchema(
        table_name=AUDIT_TABLE_NAME,
        description="Audit log of cross-solet sealed-box secret transfers",
        id_prefix=AUDIT_ID_PREFIX,
        data_sensitivity=0.3,
        columns={
            "direction": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Direction of the transfer: 'export' or 'import'.",
                data_sensitivity=0.1,
            ),
            "secret_name": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The local vault key involved in the transfer.",
                data_sensitivity=0.3,
            ),
            "peer_identifier": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Free-text peer identifier supplied by the caller (nullable).",
                data_sensitivity=0.3,
            ),
            "peer_pubkey_fingerprint": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="sha256 of the peer's X25519 public key, truncated.",
                data_sensitivity=0.2,
            ),
            "plaintext_fingerprint": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "sha256 of the plaintext, truncated. Empty string on "
                    "paths that rejected the request before decrypting."
                ),
                data_sensitivity=0.3,
            ),
            "status": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Outcome: 'success' or 'error'.",
                data_sensitivity=0.1,
            ),
            "error_message": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Short failure token on the error path; null on success.",
                data_sensitivity=0.3,
            ),
        },
        indexes=[
            IndexDefinition("idx_audit_direction", ["direction"]),
            IndexDefinition("idx_audit_secret_name", ["secret_name"]),
        ],
    )


def build_audit_row(
    direction: str,
    status: str,
    *,
    secret_name: str | None = None,
    peer_identifier: str | None = None,
    peer_public_key_fingerprint: str | None = None,
    plaintext_fingerprint: str | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
) -> dict[str, object]:
    """Build one audit row payload for insertion into a vault audit Store.

    Sensitive values (the secret value itself, peer's private material)
    are never included — only fingerprints and identifiers.
    """
    return {
        "direction": direction,
        "status": status,
        "secret_name": secret_name or "",
        "peer_identifier": peer_identifier or "",
        "peer_public_key_fingerprint": peer_public_key_fingerprint or "",
        "plaintext_fingerprint": plaintext_fingerprint or "",
        "error_code": error_code or "",
        "error_message": error_message or "",
        "occurred_at": utc_now_iso(),
    }
