"""Shared vault-plugin helpers (storage-backend-agnostic).

Both :mod:`macos_vault_plugin` (filesystem / state-service) and
:mod:`secrets_manager_vault_plugin` (AWS Secrets Manager) consume these
helpers. The module deliberately contains NO ``@platform_process``
decorators, NO storage-backend code, NO master-key handling, NO
keychain abstraction. Process registration and storage are
plugin-owned concerns; this module supplies the pure-function building
blocks (crypto, record serializers, audit-row builders, KV-file parser)
that both plugin shapes need.

Exported helpers:

* :func:`scrypt_hash_secret` / :func:`scrypt_verify_secret` — OAuth
  client_secret hashing.
* :func:`encrypt_sealed_box` / :func:`decrypt_sealed_box` —
  cross-solet sealed-box transfer of secret values.
* :func:`generate_encryption_keypair` — fresh X25519 identity
  keypair material.
* :func:`fingerprint_public_key` / :func:`fingerprint_plaintext` —
  share-safe sha256 truncations used in audit + UI.
* :func:`build_audit_row` / :func:`get_audit_schema` — audit table
  row builder and shared schema definition.
* :func:`resolve_format` / :func:`extract_field` — KV-file parsing.
* :class:`OauthClientRecord` — TypedDict matching the bundle/state
  schema. New optional fields ``grant_types`` and ``last_used_at``
  default during migration; see Task #26 dispatch §4.4.7.
"""

from .audit import (
    AUDIT_DIRECTION_EXPORT,
    AUDIT_DIRECTION_IMPORT,
    AUDIT_ID_PREFIX,
    AUDIT_STATUS_ERROR,
    AUDIT_STATUS_SUCCESS,
    AUDIT_TABLE_NAME,
    build_audit_row,
    get_audit_schema,
)
from .crypto import (
    decrypt_sealed_box,
    encrypt_sealed_box,
    fingerprint_plaintext,
    fingerprint_public_key,
    generate_encryption_keypair,
    scrypt_hash_secret,
    scrypt_verify_secret,
)
from .kv_file_parser import (
    KVFileError,
    KVFileFieldNotFoundError,
    KVFileFieldNotScalarError,
    KVFileFormatUnknownError,
    KVFileParseError,
    extract_field,
    resolve_format,
)
from .oauth_clients import (
    DEFAULT_OAUTH_GRANT_TYPES,
    OauthGrantValidationError,
    normalize_oauth_grant_types,
    normalize_oauth_register_params,
    project_oauth_client_metadata,
)
from .oauth_registry import (
    OauthClientNotFoundError,
    OAuthClientStorage,
    RefreshTokenStorage,
    VaultOAuthRegistry,
)
from .records import (
    OAUTH_ALLOWED_GRANT_TYPES,
    OAUTH_CLIENT_ID_HEX_BYTES,
    OAUTH_CLIENT_ID_PREFIX,
    OAUTH_CLIENT_SECRET_BYTES,
    OAUTH_SCRYPT_DKLEN,
    OAUTH_SCRYPT_N,
    OAUTH_SCRYPT_P,
    OAUTH_SCRYPT_R,
    OAUTH_SCRYPT_SALT_BYTES,
    X25519_KEY_LENGTH_BYTES,
    OauthClientRecord,
    SecretRecord,
    mint_client_credentials,
    utc_now_iso,
)

__all__ = [
    "AUDIT_DIRECTION_EXPORT",
    "AUDIT_DIRECTION_IMPORT",
    "AUDIT_ID_PREFIX",
    "AUDIT_STATUS_ERROR",
    "AUDIT_STATUS_SUCCESS",
    "AUDIT_TABLE_NAME",
    "DEFAULT_OAUTH_GRANT_TYPES",
    "KVFileError",
    "KVFileFieldNotFoundError",
    "KVFileFieldNotScalarError",
    "KVFileFormatUnknownError",
    "KVFileParseError",
    "OAUTH_ALLOWED_GRANT_TYPES",
    "OAuthClientStorage",
    "OauthClientNotFoundError",
    "OAUTH_CLIENT_ID_HEX_BYTES",
    "OAUTH_CLIENT_ID_PREFIX",
    "OAUTH_CLIENT_SECRET_BYTES",
    "OAUTH_SCRYPT_DKLEN",
    "OAUTH_SCRYPT_N",
    "OAUTH_SCRYPT_P",
    "OAUTH_SCRYPT_R",
    "OAUTH_SCRYPT_SALT_BYTES",
    "X25519_KEY_LENGTH_BYTES",
    "OauthClientRecord",
    "OauthGrantValidationError",
    "RefreshTokenStorage",
    "SecretRecord",
    "VaultOAuthRegistry",
    "build_audit_row",
    "decrypt_sealed_box",
    "encrypt_sealed_box",
    "extract_field",
    "fingerprint_plaintext",
    "fingerprint_public_key",
    "generate_encryption_keypair",
    "get_audit_schema",
    "mint_client_credentials",
    "normalize_oauth_grant_types",
    "normalize_oauth_register_params",
    "project_oauth_client_metadata",
    "resolve_format",
    "scrypt_hash_secret",
    "scrypt_verify_secret",
    "utc_now_iso",
]
