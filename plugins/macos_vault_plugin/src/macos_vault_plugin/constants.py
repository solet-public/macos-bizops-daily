"""Vault plugin constants."""

import os

# Plugin identification
# Renamed from ``default_vault_plugin`` per master plan §3.2 + Tier 3
# W-VAULT-LOCAL-KEYCHAIN (2026-06-07). The SQL substrate namespace
# (``default_vault_plugin__secret`` etc.) is intentionally NOT renamed
# — table identity is preserved across the migration window; Tier 5
# W-VAULT-MIGRATE drops the SQL substrate entirely.
PLUGIN_NAME = "macos_vault_plugin"
PLUGIN_VERSION = "0.2.0"

# Crypto constants
PBKDF2_ITERATIONS = 1_200_000  # 2025 Django recommendation
SALT_SIZE = 32  # bytes
NONCE_SIZE = 12  # bytes (GCM standard)
KEY_SIZE = 32  # bytes (256 bits)
TAG_SIZE = 16  # bytes (GCM auth tag)

# Environment variables. Per-solet: each solet owns its own
# env vars (e.g. ``EXAMPLE_VAULT_PASSPHRASE`` for a solet named 'example'), so
# multiple solets can coexist in the same shell without ambiguity.
# Both helpers fast-fail when ``SOLET_NAME`` is unset — vault
# unlock without an owning solet would silently look at the wrong
# entry.


def passphrase_env_var() -> str:
    """Per-solet passphrase env var name (e.g. ``EXAMPLE_VAULT_PASSPHRASE``)."""
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "macos_vault_plugin.constants: SOLET_NAME env var is "
            "required to resolve the per-solet passphrase env var name.",
        )
    return f"{name.upper()}_VAULT_PASSPHRASE"


def master_key_env_var() -> str:
    """Per-solet legacy master-key env var name (e.g. ``EXAMPLE_VAULT_MASTER_KEY``)."""
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "macos_vault_plugin.constants: SOLET_NAME env var is "
            "required to resolve the per-solet master-key env var name.",
        )
    return f"{name.upper()}_VAULT_MASTER_KEY"


# Note: the cloud-profile ``<NAME>_VAULT_MASTER_KEY_SECRET_ARN`` env var
# was removed in Task #26 (2026-05-22). The cloud profile now binds to
# ``secrets_manager_vault_plugin`` instead of this plugin switching
# backends on an env-var flag (Interface -> Plugin architectural rule).

# Credential ingestion: keyring backend module markers (no real OS keychain)
KEYRING_FAIL_BACKEND_MODULE = "keyring.backends.fail"
KEYRING_NULL_BACKEND_MODULE = "keyring.backends.null"

# Credential ingestion: macOS keychain lookup
DARWIN_PLATFORM = "darwin"
KEYCHAIN_NOT_FOUND_DARWIN_DETAIL = (
    "Searched iCloud Keychain and login keychain. "
    "No entry matches service={service!r} account={account!r}."
)

# Sealed-box secret transfer (cross-solet).
# X25519 identity keypair is stored as two secrets via the same AES-256-GCM-at-rest path
# used for every other vault secret. Values are base64-encoded 32-byte X25519 keys.
#
# Keys are scoped per master plan §3.3.1: <solet>.<plugin>.<credential>.
# Built at module import time from SOLET_NAME; fast-fails if unset, matching
# the same discipline as passphrase_env_var()/master_key_env_var() above.
def _solet_or_fail() -> str:
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "macos_vault_plugin.constants: SOLET_NAME env var is "
            "required to resolve scoped vault keys for the encryption keypair.",
        )
    return name


_ENCRYPTION_KEYPAIR_SOLET = _solet_or_fail()
# Scoped per master plan §3.3.1: <solet>.<plugin>.<credential>.
# Renamed plugin segment from ``default_vault_plugin`` to ``macos_vault_plugin``
# per Tier 3 W-VAULT-LOCAL-KEYCHAIN brief §7 + §2. The renamed plugin's
# ``_migrate_legacy_keypair_if_present`` (called at readiness BEFORE
# ``_ensure_keypair_internal``) detects rows under the OLD scoped name
# and atomically renames them to the NEW scoped name so the solet's
# sealed-box identity is preserved across the rename.
ENCRYPTION_KEYPAIR_PRIVATE_KEY = (
    f"{_ENCRYPTION_KEYPAIR_SOLET}.macos_vault_plugin.identity__encryption_private_key"
)
ENCRYPTION_KEYPAIR_PUBLIC_KEY = (
    f"{_ENCRYPTION_KEYPAIR_SOLET}.macos_vault_plugin.identity__encryption_public_key"
)
# Legacy scoped form (used only by ``_migrate_legacy_keypair_if_present``
# at startup to detect pre-rename keypair rows).
_LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY = (
    f"{_ENCRYPTION_KEYPAIR_SOLET}.default_vault_plugin.identity__encryption_private_key"
)
_LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY = (
    f"{_ENCRYPTION_KEYPAIR_SOLET}.default_vault_plugin.identity__encryption_public_key"
)
X25519_KEY_LENGTH_BYTES = 32

# Plaintext-fingerprint format. sha256 of plaintext, truncated; safe to share.
PLAINTEXT_FINGERPRINT_PREFIX = "sha256:"
PLAINTEXT_FINGERPRINT_HEX_LEN = 16

# Public-key fingerprint format. sha256 of the 32-byte X25519 public key, truncated.
# Logged in audit records; the full pubkey is not.
PUBLIC_KEY_FINGERPRINT_PREFIX = "sha256:"
PUBLIC_KEY_FINGERPRINT_HEX_LEN = 16

class ErrorCode:
    """Error codes for vault operations."""

    KEY_EXISTS = "vault.key_exists"
    NOT_FOUND = "vault.not_found"
    DECRYPTION_FAILED = "vault.decryption_failed"
    MASTER_KEY_NOT_CONFIGURED = "vault.master_key_not_configured"
    INVALID_MASTER_KEY = "vault.invalid_master_key"
    ENCRYPTION_FAILED = "vault.encryption_failed"

    # Two-tier key management errors
    VAULT_NOT_INITIALIZED = "vault.not_initialized"
    VAULT_ALREADY_INITIALIZED = "vault.already_initialized"
    VAULT_LOCKED = "vault.locked"
    INVALID_PASSPHRASE = "vault.invalid_passphrase"
    PASSPHRASE_MISMATCH = "vault.passphrase_mismatch"

    # Credential ingestion: env-var source
    ENV_VAR_NOT_SET = "vault.env_var_not_set"
    ENV_VAR_EMPTY = "vault.env_var_empty"

    # Credential ingestion: file source
    FILE_NOT_FOUND = "vault.file_not_found"
    FILE_UNREADABLE = "vault.file_unreadable"
    FILE_EMPTY = "vault.file_empty"
    FILE_PATH_NOT_ABSOLUTE = "vault.file_path_not_absolute"

    # Credential ingestion: kv-file source
    KV_FILE_FORMAT_UNKNOWN = "vault.kv_file_format_unknown"
    KV_FILE_FIELD_NOT_FOUND = "vault.kv_file_field_not_found"
    KV_FILE_FIELD_NOT_SCALAR = "vault.kv_file_field_not_scalar"
    KV_FILE_FIELD_EMPTY = "vault.kv_file_field_empty"
    KV_FILE_PARSE_ERROR = "vault.kv_file_parse_error"

    # Credential ingestion: keychain source
    KEYCHAIN_ENTRY_NOT_FOUND = "vault.keychain_entry_not_found"
    KEYCHAIN_ENTRY_EMPTY = "vault.keychain_entry_empty"
    KEYCHAIN_UNAVAILABLE = "vault.keychain_unavailable"
    KEYCHAIN_SELECTION_UNSUPPORTED = "vault.keychain_selection_unsupported"

    # Cross-solet sealed-box secret transfer
    KEYPAIR_NOT_INITIALIZED = "vault.keypair_not_initialized"
    INVALID_PUBKEY = "vault.invalid_pubkey"
    INVALID_CIPHERTEXT = "vault.invalid_ciphertext"
    DECRYPT_FAILED = "vault.decrypt_failed"
    SECRET_ALREADY_EXISTS = "vault.secret_already_exists"
    SECRET_NOT_FOUND = "vault.secret_not_found"

    # Internal minting (store_random)
    INVALID_BYTE_LENGTH = "vault.invalid_byte_length"

    # OAuth 2.1 client_credentials registry
    OAUTH_CLIENT_NOT_FOUND = "vault.oauth_client_not_found"
    OAUTH_CLIENT_INVALID_NAME = "vault.oauth_client_invalid_name"
    OAUTH_CLIENT_STORE_UNAVAILABLE = "vault.oauth_client_store_unavailable"


# OAuth scrypt parameters.  N=2**14 = 16384 keeps verification under ~50ms on
# a modern x86 core which is fine for /oauth/token (called once per claude.ai
# token refresh, not per MCP request).
OAUTH_SCRYPT_N = 1 << 14
OAUTH_SCRYPT_R = 8
OAUTH_SCRYPT_P = 1
OAUTH_SCRYPT_DKLEN = 32
OAUTH_SCRYPT_SALT_BYTES = 16

# Client identifier format.  16 bytes hex = 32 chars; total = "client-" + 32.
OAUTH_CLIENT_ID_PREFIX = "client-"
OAUTH_CLIENT_ID_HEX_BYTES = 16

# Client secret format.  32 bytes urlsafe-b64 = 43 chars.  Mints fresh on
# every register call; never re-derivable from the stored hash.
OAUTH_CLIENT_SECRET_BYTES = 32
