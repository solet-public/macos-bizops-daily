"""Default Vault Plugin - Secure credential storage with AES-256-GCM encryption.

Security:
- Two-tier key management: passphrase -> KEK -> Master Key
- AES-256-GCM authenticated encryption
- PBKDF2 key derivation (1.2M iterations)
- Per-secret unique salt and nonce
- Passphrase rotation without re-encrypting secrets
- NEVER logs plaintext secrets or keys
"""

from __future__ import annotations

import base64
import builtins
import getpass
import hashlib
import logging
import os
import secrets
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import keyring
from ananta.core.config.config_provider import ConfigProvider
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_base import PluginBase
from ananta.core.plugins.plugin_contracts import ActionStatus
from ananta.core.services.call_context import (
    CallContext,
    VaultAccessDeniedError,  # noqa: F401  # pyright: ignore[reportUnusedImport]
    VaultKeyMalformedError,  # noqa: F401  # pyright: ignore[reportUnusedImport]
)
from ananta.interfaces.state_service_protocol import StateServiceProtocol
from ananta.interfaces.vault_qualification_interface import VaultQualificationInterface
from ananta.interfaces.vault_service_interface import VaultServiceInterface
from ananta.logging_setup import configure_plugin_logging
from ananta.services.store import Store, open_store
from ananta.services.vault_service.enforcement import (
    enforce_namespace,
    requires_operator_principal,
)
from ananta.services.vault_service.interfaces.public import VaultQualificationAPI, VaultServiceAPI
from ananta.types.schema_types import SchemaDefinition
from ananta.vault_core import (
    AUDIT_DIRECTION_EXPORT,
    AUDIT_DIRECTION_IMPORT,
    AUDIT_STATUS_ERROR,
    AUDIT_STATUS_SUCCESS,
    KVFileFieldNotFoundError,
    KVFileFieldNotScalarError,
    KVFileFormatUnknownError,
    KVFileParseError,
    OauthClientNotFoundError,
    OauthGrantValidationError,
    VaultOAuthRegistry,
    extract_field,
    get_audit_schema,
    resolve_format,
)
from nacl.exceptions import CryptoError
from nacl.public import PrivateKey, PublicKey, SealedBox

from . import keychain_interaction, macos_keychain
from .constants import (
    _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY,
    _LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY,
    DARWIN_PLATFORM,
    ENCRYPTION_KEYPAIR_PRIVATE_KEY,
    ENCRYPTION_KEYPAIR_PUBLIC_KEY,
    KEYCHAIN_NOT_FOUND_DARWIN_DETAIL,
    KEYRING_FAIL_BACKEND_MODULE,
    KEYRING_NULL_BACKEND_MODULE,
    PLAINTEXT_FINGERPRINT_HEX_LEN,
    PLAINTEXT_FINGERPRINT_PREFIX,
    PLUGIN_NAME,
    PUBLIC_KEY_FINGERPRINT_HEX_LEN,
    PUBLIC_KEY_FINGERPRINT_PREFIX,
    X25519_KEY_LENGTH_BYTES,
    ErrorCode,
    passphrase_env_var,
)
from .crypto import VaultCrypto
from .errors import (
    InvalidPassphraseError,
    MasterKeyNotConfiguredError,
    VaultAlreadyInitializedError,
    VaultKeypairMigrationError,
    VaultLockedError,
    VaultMasterKeyMigrationError,
    VaultNotInitializedError,
)
from .key_manager import VaultKeyManager, get_key_manager
from .keychain import PerCredentialKeychain, SystemKeychain
from .schema import NAMESPACE as VAULT_NAMESPACE
from .schema import (
    get_oauth_client_schema,
    get_oauth_refresh_token_schema,
)


class MacosVaultPlugin(
    PluginBase,
    VaultServiceInterface,
    VaultQualificationInterface,
    VaultServiceAPI,
    VaultQualificationAPI,
):
    """Secure credential storage with AES-256-GCM encryption.

    Implements both the runtime service interface (VaultServiceInterface, returning
    ActionResult dicts) and the AI-discoverable interface (VaultServiceAPI, used
    for process registry indexing). Both contracts have the same method names and
    are satisfied by a single concrete implementation that returns ActionResult
    (which is a dict[str, Any]-compatible TypedDict).
    """

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.config = config or {}
        self.logger: logging.Logger = logging.getLogger(self.name)

        self._crypto: VaultCrypto | None = None
        self.config_provider: ConfigProvider | None = None

        # Injected dependencies
        self.state_service: StateServiceProtocol | None = None

        # Postgres-backed Store over the secret_transfer_audit table; opened
        # in prepare_for_readiness once state_service is resolved. The
        # _audit_transfer helper writes one row per export/import call,
        # success or failure.
        self._audit_store: Store | None = None
        # Postgres-backed Store over the oauth_client table; opened in
        # prepare_for_readiness alongside the audit store.  Backs the
        # ``oauth_client_register / _revoke / _list`` processes and the
        # in-process ``verify_oauth_client_credentials`` helper that the
        # Streamable HTTP MCP transport's ``/oauth/token`` endpoint uses.
        self._oauth_clients_store: Store | None = None
        # Postgres-backed Store over the oauth_refresh_token table.
        # Single-use rotation: each consume deletes the row and the
        # caller (OAuth router) mints a fresh row alongside the new
        # access token, per OAuth 2.1 §4.3.1.
        self._oauth_refresh_tokens_store: Store | None = None

        # Per-credential OS Keychain substrate (W-VAULT-LOCAL-KEYCHAIN
        # Tier 3, 2026-06-07). Initialized HERE in ``__init__`` so the
        # substrate is reachable as soon as the plugin instance exists —
        # specifically, before any consumer plugin's launch-key gate
        # fires ``vault_service.exists(...)`` through the
        # ``VaultServiceProxy`` (the proxy holds the instance reference
        # from plugin discovery, not from readiness). This is B-fix-1
        # from the 2026-06-08 wholesale re-eval memo §6.6; ER-13's
        # B-fix-2 (substrate-provider startup-ordering mechanism) is
        # the eventual architectural fix. Tests inject a ``FakeKeychain``
        # by overwriting ``self._keychain`` after construction.
        self._keychain: PerCredentialKeychain | None = None
        self._init_per_credential_keychain()

    # ------------------------------------------------------------------
    # VaultKeysProvider — W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """Self-owned X25519 keypair is LAZY-CREATED, not required.

        ``ensure_encryption_keypair`` mints the identity keypair on
        first bootstrap (operator-initiated for the local profile, or
        spawned by the bridge HTTP server during cross-solet
        sealed-box transfer setup). The plugin must load with no
        keypair row present so the bootstrap helper can mint it.
        Returns empty list per W-CLASSIFY §A.2.3 + brief §3.6.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """Self-owned vault keys this plugin reads or writes.

        Tier 1 W-ADDRESS-BOOK-RENAME migrated the keypair constants to
        scoped form per W-CLASSIFY §A.2.3. Tier 3 W-VAULT-LOCAL-KEYCHAIN
        further migrated the plugin segment to ``macos_vault_plugin``
        (constants at ``constants.py:92-97``: ``<solet>.macos_vault_plugin.identity__encryption_(private|public)_key``).
        Legacy rows under ``<solet>.default_vault_plugin.identity__encryption_*``
        are renamed at startup by ``_migrate_legacy_keypair_if_present``
        BEFORE ``_ensure_keypair_internal`` runs, so the W-INT Cycle 2
        static gate sees only the new scoped names in
        ``_retrieve_impl(ENCRYPTION_KEYPAIR_*)`` call sites.
        """
        return [ENCRYPTION_KEYPAIR_PRIVATE_KEY, ENCRYPTION_KEYPAIR_PUBLIC_KEY]

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """Return schema definitions for the vault tables.

        Tables: default_vault_plugin__secret, default_vault_plugin__audit,
        default_vault_plugin__oauth_client, default_vault_plugin__oauth_refresh_token.
        The audit schema is shared with vault_core; this plugin owns its instance.
        """
        from .schema import get_vault_schema

        return [
            get_vault_schema(),
            SchemaDefinition(
                namespace=VAULT_NAMESPACE,
                tables={"audit": get_audit_schema()},
            ),
        ]

    def prepare_for_readiness(self) -> None:
        """Initialize plugin. Fail-fast if dependencies unavailable.

        Uses Service Registry pattern: plugin REQUESTS services from orchestrator.
        See: ananta_build/2025-12-06_service_binding_architecture.md
        """
        if not self.orchestrator_ref:
            raise RuntimeError(f"{self.name}: orchestrator_ref not injected")

        APP_HOME = getattr(self.orchestrator_ref, "APP_HOME", None)
        if not APP_HOME:
            raise RuntimeError(
                f"{self.name}: Application directory not configured - plugin cannot initialize"
            )

        self.config_provider = ConfigProvider(self.name, self.config or {})
        self.logger = configure_plugin_logging(APP_HOME, self.name, self.config_provider)
        self.logger.debug(f"Initializing {self.name}")

        # Request state_service - plugin decides it needs this
        self.state_service = cast(
            StateServiceProtocol | None, self.orchestrator_ref.get_service("state_service")
        )
        if self.state_service is None:
            raise RuntimeError(f"{self.name}: state_service not available")
        self.logger.debug("state_service acquired from orchestrator")

        # Open the Postgres-backed Store for the secret_transfer_audit table.
        # The Postgres backend self-registers on import; we touch the module
        # explicitly so the backend is registered no matter what plugin
        # load order the runtime picked.
        from macos_vault_plugin.postgres_backend import (  # noqa: PLC0415
            store as _pg_store_module,
        )
        _ = _pg_store_module
        self._audit_store = open_store(
            get_audit_schema(),
            namespace=VAULT_NAMESPACE,
            backend="postgres",
            state_service=self.state_service,
        )
        self._oauth_clients_store = open_store(
            get_oauth_client_schema(),
            namespace=VAULT_NAMESPACE,
            backend="postgres",
            state_service=self.state_service,
        )
        self._oauth_refresh_tokens_store = open_store(
            get_oauth_refresh_token_schema(),
            namespace=VAULT_NAMESPACE,
            backend="postgres",
            state_service=self.state_service,
        )
        # Compose the OAuth registry over the two stores so all
        # OAuth-domain logic lives in vault_core and the plugin
        # becomes thin platform-process glue (Task #31 Boy Scout).
        self._oauth_registry: VaultOAuthRegistry = VaultOAuthRegistry(
            client_storage=_StoreOAuthClientStorage(self._oauth_clients_store),
            refresh_store=_StoreRefreshTokenStorage(
                self._oauth_refresh_tokens_store,
            ),
            b64_encode=self._b64encode_bytes,
            b64_decode=self._b64decode_str,
            logger=self.logger,
        )

        # Per-credential Keychain substrate is initialized in ``__init__``
        # (B-fix-1, see __init__ comment) so it's reachable before any
        # consumer plugin's launch-key gate fires.

        # First-boot bootstrap. Initializes the vault if absent (needs a
        # passphrase from env or file), then materializes the X25519
        # identity keypair so every external caller can rely on
        # ``get_public_key`` returning a stable value from the first
        # request after startup. Fail-fast: no fallback paths.
        self._bootstrap_vault_and_keypair()

    def _init_per_credential_keychain(self) -> None:
        """Construct the per-credential :class:`SystemKeychain` for runtime use.

        Called from :meth:`__init__` so the substrate is reachable
        before any consumer's launch-key gate fires (B-fix-1, see
        ``__init__`` comment). Fail-soft on Keychain unavailability:
        ``self._keychain`` stays ``None`` and any runtime verb that
        reaches :meth:`_require_keychain_pair` raises with a clear
        ordering hint. Tests inject a ``FakeKeychain`` by overwriting
        ``self._keychain`` after construction.
        """
        candidate = SystemKeychain()
        if candidate.is_available():
            self._keychain = candidate
            self.logger.debug("Per-credential Keychain substrate ACTIVE")
        else:
            self.logger.warning(
                "Per-credential Keychain substrate INACTIVE — runtime vault "
                "verbs will raise. Local profile requires macOS Keychain "
                "per [[solet-locality]].",
            )

    # ─────────────────────────────────────────────────────────────────────
    # Startup-compat migrations.
    #
    # Only the keypair migrator survives: it scales to the
    # ``cutover_secret`` per-row atomic-cutover pattern (P0-A) and the
    # SQL substrate it operates on is the only one we still consult
    # during a single boot. The legacy default_vault_plugin → macos_vault_plugin
    # file rename and the file-bootstrap third-substrate were retired
    # under the ``NO backwards-compatibility code`` rule; their helpers
    # were replaced with fail-loud assertions at the call site so any
    # future drift surfaces immediately.
    # ─────────────────────────────────────────────────────────────────────

    @staticmethod
    def _assert_legacy_vault_substrates_absent() -> None:
        """Fail loud if any pre-Tier-3 vault scaffolding lingers on disk.

        Pre-Tier-3 vault scaffolding (``default_vault_plugin`` config
        dir, file-bootstrap ``secrets/*.enc``) is no longer read by any
        runtime path; a non-empty presence means an old solet's
        state leaked in. The vault refuses to boot rather than silently
        ignore the leak.
        """
        app_home = os.environ.get("APP_HOME", "").strip()
        if not app_home:
            return
        legacy_dir = Path(app_home) / "config" / "plugins" / "default_vault_plugin"
        for stray in ("master-key.enc", "passphrase"):
            stray_path = legacy_dir / stray
            if stray_path.exists():
                raise VaultMasterKeyMigrationError(
                    f"legacy vault scaffolding present at {stray_path}; "
                    "expected default_vault_plugin/ to be empty after Tier 3 rename. "
                    "Inspect and remove manually.",
                )
        legacy_secrets = legacy_dir / "secrets"
        if legacy_secrets.is_dir() and any(legacy_secrets.glob("*.enc")):
            raise VaultMasterKeyMigrationError(
                f"legacy file-bootstrap secrets present at {legacy_secrets}; "
                "expected default_vault_plugin/secrets/ to be empty (file-bootstrap "
                "substrate retired). Inspect and remove manually.",
            )

    def _migrate_legacy_keypair_if_present(self) -> None:
        """Rename pre-Tier-3 self-owned X25519 keypair rows to the new scoped form.

        Runs in ``_bootstrap_vault_and_keypair`` BEFORE
        ``_ensure_keypair_internal``. If legacy rows are present but the
        rename fails, raises so the existing ``_ensure_keypair_internal``
        never runs — that's how we refuse to silently mint a fresh keypair
        and rotate the solet's cross-solet sealed-box identity.
        """
        self._migrate_legacy_keypair_secret(
            _LEGACY_ENCRYPTION_KEYPAIR_PRIVATE_KEY,
            ENCRYPTION_KEYPAIR_PRIVATE_KEY,
            "private key",
        )
        self._migrate_legacy_keypair_secret(
            _LEGACY_ENCRYPTION_KEYPAIR_PUBLIC_KEY,
            ENCRYPTION_KEYPAIR_PUBLIC_KEY,
            "public key",
        )

    def _migrate_legacy_keypair_secret(
        self, old_key: str, new_key: str, label: str,
    ) -> None:
        """Atomic state-service rename of one keypair row from old to new scoped name."""
        if self._get_by_key(old_key) is None:
            return
        if self._get_by_key(new_key) is not None:
            raise VaultKeypairMigrationError(
                f"detected legacy {label} row {old_key!r} AND new-form row "
                f"{new_key!r}; ambiguous state, refusing to migrate. Operator "
                "must reconcile manually (likely one is stale from an aborted "
                "prior migration).",
            )
        state_error = self._state_rename_secret(old_key, new_key)
        if state_error is not None:
            raise VaultKeypairMigrationError(
                f"detected legacy {label} row at {old_key!r} but rename to "
                f"{new_key!r} failed; REFUSING to mint fresh keypair (would "
                "silently rotate sealed-box identity).",
            )
        self.logger.info(
            "Migrated legacy keypair %s: %s -> %s", label, old_key, new_key,
        )

    def _bootstrap_vault_and_keypair(self) -> None:
        """Initialize the vault on first boot, then ensure the X25519 keypair.

        Runs once from ``prepare_for_readiness`` — explicit start-time
        bootstrap, not a lazy ``ensure`` scattered through action methods.

        Two boot paths depending on the active keychain backend:

        1. **Direct master key** (Secrets Manager / tenant-isolated cloud).
           The active keychain backend reports ``direct_master_key=True``
           (e.g. ``secrets_manager_vault_plugin``); the 32-byte master
           key is pre-provisioned out of band. No passphrase, no init
           call, no first-boot creation path — the plugin only unlocks.

        2. **Passphrase-wrapped master key** (local docker-compose /
           dev box). First boot generates a master key, wraps it with a
           passphrase, and writes the envelope to the file/system
           keychain. Subsequent boots unlock with the same passphrase.

        W-VAULT-LOCAL-KEYCHAIN Tier 3 startup-compat migrations run FIRST,
        before any key_manager call: the master-key file moves from the
        old ``default_vault_plugin`` directory to ``macos_vault_plugin``,
        and (after crypto is unlocked) the self-owned keypair rows are
        renamed from the legacy scoped form to the new scoped form. Both
        refuse to mint fresh on failure — a silent fresh-mint would lose
        the solet's encrypted state (master-key) or sealed-box
        identity (keypair).
        """
        from .keychain import get_keychain  # noqa: PLC0415

        self._assert_legacy_vault_substrates_absent()

        key_manager = self._get_key_manager()
        keychain = get_keychain()

        if getattr(keychain, "direct_master_key", False):
            # Tenant-isolated SM path: no init, no passphrase.
            key_manager.unlock()
            self._crypto = None
            self.logger.info(
                "Vault unlocked via direct-master-key backend (%s)",
                keychain.__class__.__name__,
            )
        else:
            if not key_manager.is_initialized():
                passphrase = self._get_passphrase()
                if not passphrase:
                    raise RuntimeError(
                        f"{self.name}: vault not initialized and no passphrase "
                        f"available. Set {passphrase_env_var()} or create "
                        f"$APP_HOME/config/plugins/{self.name}/passphrase."
                    )
                key_manager.initialize(passphrase)
                self._crypto = None
                self.logger.info("Vault initialized (first boot)")

        try:
            self._get_crypto()
        except VaultLockedError as exc:
            raise RuntimeError(
                f"{self.name}: vault locked and cannot be unlocked at start; "
                f"check passphrase / Secrets Manager configuration: {exc}"
            ) from exc

        self._migrate_legacy_keypair_if_present()
        _, created = self._ensure_keypair_internal()
        self.logger.info(
            "Encryption keypair %s",
            "created (first boot)" if created else "already present",
        )

    # _ensure_schema retired 2026-06-09 (P0-A Round 5) — the no-op lazy
    # flag-setter was vestigial; schema tables are created during the
    # platform's startup sequence via ``get_schema_definitions()`` before
    # any actions execute, so callers in this file had no work to gate.

    # NOTE: set_state_service() REMOVED (2025-12-06)
    # Plugin now requests state_service in prepare_for_readiness() via orchestrator.get_service()
    # See: ananta_build/2025-12-06_service_binding_architecture.md

    @property
    def service_interfaces(self) -> tuple[type, ...]:
        """Declare that this plugin implements VaultServiceInterface.

        As a ServiceProvider, the vault is:
        - Registered under service_interface:: namespace instead of plugin::
        - Validated at load time to ensure it inherits from VaultServiceInterface
        - Accessed via service_interface::vault_service::* process keys
        """
        return (VaultServiceInterface, VaultQualificationInterface)

    @property
    def supported_interface_versions(self) -> dict[type, str]:
        return {
            VaultServiceInterface: VaultServiceInterface.INTERFACE_VERSION,
            VaultQualificationInterface: VaultQualificationInterface.INTERFACE_VERSION,
        }

    def get_config_schema(self) -> dict[str, object]:
        """Declare configuration schema for the vault plugin.

        Returns JSON Schema for setup flow to generate UI/prompts.
        """
        return {
            "$schema": "http://json-schema.org/draft-07/schema#",
            "title": "Default Vault Plugin",
            "description": "Secure credential storage with AES-256-GCM encryption and two-tier key management",
            "type": "object",
            "required": [],
            "properties": {
                "passphrase": {
                    "type": "string",
                    "title": "Vault Passphrase",
                    "description": f"Passphrase for unlocking the vault. Can also be set via the per-solet environment variable {passphrase_env_var()} or passphrase file at $APP_HOME/config/plugins/macos_vault_plugin/passphrase",
                    "x-secret": True,
                    "x-group": "security",
                    "x-order": 1,
                },
            },
        }

    def _get_key_manager(self) -> VaultKeyManager:
        """Get the global VaultKeyManager instance."""
        return get_key_manager()

    def _passphrase_file_path(self) -> Path | None:
        """Return the persistent local passphrase path when APP_HOME is configured."""
        app_home = os.environ.get("APP_HOME")
        if not app_home:
            return None
        return Path(app_home) / "config" / "plugins" / self.name / "passphrase"

    @staticmethod
    def _stage_rotated_passphrase(passphrase_file: Path, new_passphrase: str) -> Path:
        """Create the replacement passphrase file securely before rewrapping.

        ``O_EXCL`` and mode ``0600`` make the temporary file private at birth;
        the caller publishes it with :func:`os.replace` only after the Keychain
        envelope has been successfully rewrapped.
        """
        staged_file = passphrase_file.with_name(
            f".{passphrase_file.name}.rotate-{secrets.token_hex(16)}",
        )
        descriptor = os.open(
            staged_file,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                stream.write(new_passphrase)
                stream.flush()
                os.fsync(stream.fileno())
        except BaseException:
            staged_file.unlink(missing_ok=True)
            raise
        return staged_file

    def _get_passphrase(self) -> str | None:
        """Get vault passphrase from environment or file.

        Priority:
        1. Per-solet environment variable ``<SOLET_NAME>_VAULT_PASSPHRASE``
           (e.g. ``EXAMPLE_VAULT_PASSPHRASE``)
        2. Passphrase file at $APP_HOME/config/plugins/macos_vault_plugin/passphrase

        Returns:
            Passphrase string, or None if not available
        """
        # 1. Environment variable (for CI/automated deployments)
        passphrase = os.environ.get(passphrase_env_var())
        if passphrase:
            return passphrase

        # 2. Passphrase file (for local development)
        passphrase_file = self._passphrase_file_path()
        if passphrase_file is not None and passphrase_file.exists():
            return passphrase_file.read_text().strip()

        return None

    def _get_crypto(self) -> VaultCrypto:
        """Return cached :class:`VaultCrypto` after two-tier key-manager unlock."""
        if self._crypto is not None:
            return self._crypto

        key_manager = self._get_key_manager()
        if not key_manager.is_initialized():
            raise MasterKeyNotConfiguredError(
                f"Vault not initialized. Run 'vault init' action or set {passphrase_env_var()}."
            )

        if not key_manager.is_unlocked():
            # Direct-master-key backends (Secrets Manager) read the
            # master key from the backend directly — no passphrase.
            # Wrap-mode backends require passphrase from env/file.
            from .keychain import get_keychain  # noqa: PLC0415

            if getattr(get_keychain(), "direct_master_key", False):
                key_manager.unlock()
            else:
                passphrase = self._get_passphrase()
                if not passphrase:
                    raise VaultLockedError(
                        f"Vault is locked. Set {passphrase_env_var()} or provide passphrase file."
                    )
                key_manager.unlock(passphrase)

        self._crypto = key_manager.get_crypto()
        return self._crypto

    def _get_by_key(self, secret_key: str) -> dict[str, Any] | None:
        """Get secret record by key."""
        if not self.state_service:
            return None

        result = self.state_service.read_state(
            namespace=VAULT_NAMESPACE,
            query={"table": "secret", "filters": {"secret_key": secret_key}},
        )

        data = result.get("data", {})
        records = data.get("records", [])
        if not isinstance(records, list) or not records:
            return None
        first_row = records[0]
        if not isinstance(first_row, dict):
            return None
        return first_row

    def _now(self) -> str:
        """Get current timestamp as ISO string."""
        return datetime.now(UTC).isoformat()

    def _success(self, data: dict[str, Any]) -> ActionResult:
        """Build success response."""
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "timestamp": self._now(),
            "data": data,
            "actions": [],
            "error": None,
        }

    def _not_found(self, key: str) -> ActionResult:
        """Build not-found response (business result, not an error).

        NOT_FOUND is a valid business outcome - the action executed successfully,
        it just didn't find the requested key. This should NOT trigger ERROR logs.
        """
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "timestamp": self._now(),
            "data": {
                "found": False,
                "key": key,
                "message": f"Secret '{key}' not found",
            },
            "actions": [],
            "error": None,
        }

    def _auth_failed(self, reason: str) -> ActionResult:
        """Build authentication failure response (business result, not an error).

        Invalid passphrase is an expected business outcome - the action executed
        successfully, but authentication failed. This should NOT trigger ERROR logs.
        """
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "timestamp": self._now(),
            "data": {
                "authenticated": False,
                "reason": reason,
                "message": "Authentication failed",
            },
            "actions": [],
            "error": None,
        }

    def _error(self, code: str, message: str) -> ActionResult:
        """Build error response."""
        error_detail: ErrorDetail = {
            "type": "VaultError",
            "code": code,
            "message": message,
            "details": {"plugin_name": PLUGIN_NAME},
            "severity": "error",
            "timestamp": self._now(),
        }
        return {
            "action_status": ActionStatus.ERROR.value,
            "timestamp": self._now(),
            "data": {},
            "actions": [],
            "error": error_detail,
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Interface Methods (direct calls)
    # ─────────────────────────────────────────────────────────────────────────

    def store(
        self,
        key: str,
        value: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Store secret - interface method."""
        enforce_namespace(key, call_context)
        return self._store_impl(key, value, tags or [], metadata or {})

    def retrieve(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Retrieve secret - interface method."""
        enforce_namespace(key, call_context)
        return self._retrieve_impl(key)

    def delete(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Delete secret - interface method."""
        enforce_namespace(key, call_context)
        return self._delete_impl(key)

    def list(
        self,
        tag: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """List secrets - interface method."""
        return self._list_impl(tag)

    def exists(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Check if secret exists - interface method."""
        enforce_namespace(key, call_context)
        return self._exists_impl(key)

    def qualify_keychain(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Return bounded evidence that this user's Keychain namespace is readable."""
        del call_context
        if self._keychain is None:
            return self._success({
                "backend": "macos_keychain",
                "available": False,
                "current_user_accessible": False,
            })
        try:
            available = self._keychain.is_available()
            if not available:
                return self._success({
                    "backend": "macos_keychain",
                    "available": False,
                    "current_user_accessible": False,
                })
            self._keychain.list_credentials_under_solet()
        except Exception:
            return self._success({
                "backend": "macos_keychain",
                "available": True,
                "current_user_accessible": False,
            })
        return self._success({
            "backend": "macos_keychain",
            "available": True,
            "current_user_accessible": True,
        })

    def qualify_round_trip(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Prove the Keychain round trip using a private canary deleted before return."""
        del call_context
        solet = os.environ.get("SOLET_NAME", "").strip()
        if not solet:
            return self._success({"stored": False, "retrieved": False, "matched": False, "deleted": False})
        key = f"{solet}.{PLUGIN_NAME}.qualify_round_trip_{secrets.token_hex(16)}"
        value = secrets.token_urlsafe(32)
        stored = False
        retrieved = False
        matched = False
        deleted = False
        try:
            stored = self._store_impl(key, value, [], {}).get("action_status") == ActionStatus.COMPLETED.value
            if stored:
                result = self._retrieve_impl(key)
                retrieved = result.get("action_status") == ActionStatus.COMPLETED.value
                data = result.get("data", {})
                matched = retrieved and data.get("value") == value
        except Exception:
            pass
        finally:
            try:
                result = self._delete_impl(key)
                deleted = result.get("action_status") == ActionStatus.COMPLETED.value
            except Exception:
                deleted = False
        return self._success({
            "stored": stored,
            "retrieved": retrieved,
            "matched": matched,
            "deleted": deleted,
        })

    def rotate(
        self,
        key: str,
        new_value: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rotate secret - interface method."""
        enforce_namespace(key, call_context)
        return self._rotate_impl(key, new_value)

    @requires_operator_principal
    def rename(
        self,
        old_key: str,
        new_key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rename secret server-side - interface method.

        W-PLUGIN-LAUNCH-KEYS sub-1 (Q1 unblock): canonical no-leak path
        for flat-to-scoped vault-key migrations. Reads the encrypted
        row under old_key, rewrites the secret_key column to new_key
        atomically via state_service.update_state, preserving the
        ciphertext + version + tags + metadata. Plaintext never leaves
        the vault process. Operator-only enforcement activated in
        W-VAULT-CALLER-ENFORCE sub-2 via @requires_operator_principal.
        """
        enforce_namespace(old_key, call_context)
        enforce_namespace(new_key, call_context)
        return self._rename_impl(old_key, new_key)

    def store_random(
        self,
        key: str,
        byte_length: int = 32,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Mint a random secret and store it - interface method."""
        enforce_namespace(key, call_context)
        return self._store_random_impl(key, byte_length, tags or [], metadata or {})

    def status(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Get vault status - interface method."""
        key_manager = self._get_key_manager()
        status_data = key_manager.get_status()
        return self._success(status_data)

    @requires_operator_principal
    def unlock(
        self,
        passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Unlock vault - interface method."""
        try:
            key_manager = self._get_key_manager()
            key_manager.unlock(passphrase)
            self._crypto = None
            return self._success({"message": "Vault unlocked", "unlocked": True})
        except VaultNotInitializedError as e:
            return self._error(ErrorCode.VAULT_NOT_INITIALIZED, str(e))
        except InvalidPassphraseError:
            return self._auth_failed("Invalid passphrase")
        except Exception as e:
            self.logger.error(f"Vault unlock failed: {type(e).__name__}")
            return self._error(ErrorCode.DECRYPTION_FAILED, "Failed to unlock vault")

    @requires_operator_principal
    def lock(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Lock vault - interface method."""
        key_manager = self._get_key_manager()
        key_manager.lock()
        self._crypto = None
        return self._success({"message": "Vault locked", "unlocked": False})

    # ─────────────────────────────────────────────────────────────────────────
    # Vault admin (operator-only; promoted from plugin-process scope by
    # W-VAULT-CALLER-ENFORCE sub-2 — master plan §1.3:
    # service_interface::vault_service::* is the ONLY entry surface).
    # ─────────────────────────────────────────────────────────────────────────

    @requires_operator_principal
    def vault_init(
        self,
        passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Initialize vault with two-tier key management. Operator-only."""
        try:
            key_manager = self._get_key_manager()
            key_manager.initialize(passphrase)
            self._crypto = None
            status = key_manager.get_status()
            return self._success(
                {
                    "message": "Vault initialized successfully",
                    "wrapped_key_path": status["wrapped_key_path"],
                },
            )
        except VaultAlreadyInitializedError as e:
            return self._error(ErrorCode.VAULT_ALREADY_INITIALIZED, str(e))
        except Exception as e:
            self.logger.error(f"Vault initialization failed: {type(e).__name__}")
            return self._error(
                ErrorCode.ENCRYPTION_FAILED, "Vault initialization failed",
            )

    @requires_operator_principal
    def vault_create_recovery(
        self,
        recovery_passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Wrap the master key under a recovery passphrase. Operator-only."""
        try:
            key_manager = self._get_key_manager()
            recovery_path = key_manager.create_recovery_key(recovery_passphrase)
            return self._success(
                {
                    "message": (
                        "Recovery key created. Store the passphrase securely "
                        "offline!"
                    ),
                    "recovery_key_path": str(recovery_path),
                },
            )
        except VaultLockedError as e:
            return self._error(ErrorCode.VAULT_LOCKED, str(e))
        except Exception as e:
            self.logger.error(f"Recovery key creation failed: {type(e).__name__}")
            return self._error(
                ErrorCode.ENCRYPTION_FAILED, "Recovery key creation failed",
            )

    @requires_operator_principal
    def vault_rotate_passphrase(
        self,
        old_passphrase: str,
        new_passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rotate vault passphrase. Operator-only."""
        if os.environ.get(passphrase_env_var()):
            return self._error(
                ErrorCode.PASSPHRASE_MISMATCH,
                "Passphrase rotation is refused while the vault unlocks from "
                f"{passphrase_env_var()}; update the persistent environment "
                "configuration and restart instead.",
            )
        passphrase_file = self._passphrase_file_path()
        if passphrase_file is None or not passphrase_file.exists():
            return self._error(
                ErrorCode.PASSPHRASE_MISMATCH,
                "Passphrase rotation requires the persistent passphrase file at "
                "$APP_HOME/config/plugins/macos_vault_plugin/passphrase.",
            )
        try:
            staged_file = self._stage_rotated_passphrase(
                passphrase_file, new_passphrase,
            )
        except OSError:
            self.logger.error("Passphrase rotation could not stage passphrase file")
            return self._error(
                ErrorCode.ENCRYPTION_FAILED,
                "Passphrase rotation could not prepare its persistent file; "
                "no rotation was performed.",
            )
        try:
            key_manager = self._get_key_manager()
            key_manager.rotate_passphrase(old_passphrase, new_passphrase)
            try:
                os.replace(staged_file, passphrase_file)
            except OSError:
                key_manager.rotate_passphrase(new_passphrase, old_passphrase)
                raise
            self._crypto = None
            return self._success(
                {
                    "message": (
                        "Passphrase rotated successfully. No secrets were "
                        "re-encrypted."
                    ),
                },
            )
        except VaultNotInitializedError as e:
            return self._error(ErrorCode.VAULT_NOT_INITIALIZED, str(e))
        except InvalidPassphraseError:
            return self._auth_failed("Invalid passphrase")
        except Exception as e:
            self.logger.error(f"Passphrase rotation failed: {type(e).__name__}")
            return self._error(
                ErrorCode.ENCRYPTION_FAILED, "Passphrase rotation failed",
            )
        finally:
            staged_file.unlink(missing_ok=True)

    # ─────────────────────────────────────────────────────────────────────────
    # Sealed-box + OAuth interface methods — match VaultServiceAPI abstract
    # signatures. W-VAULT-CALLER-ENFORCE sub-2: bodies lifted from the
    # deleted @platform_process *_action wrappers; @requires_operator_principal
    # gates the operator-only verbs (lockstep with
    # vault_service_proxy.OPERATOR_ONLY_METHODS).
    # ─────────────────────────────────────────────────────────────────────────

    @requires_operator_principal
    def ensure_encryption_keypair(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Bootstrap the X25519 identity keypair if absent. Idempotent."""
        try:
            self._get_crypto()
        except VaultLockedError as exc:
            return self._error(ErrorCode.VAULT_LOCKED, str(exc))

        public_bytes, created = self._ensure_keypair_internal()
        return self._success(
            {
                "created": created,
                "public_key": self._b64encode_bytes(public_bytes),
            },
        )

    @requires_operator_principal
    def get_public_key(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Return the local X25519 public key; assumes readiness loaded the keypair."""
        try:
            self._get_crypto()
        except VaultLockedError as exc:
            return self._error(ErrorCode.VAULT_LOCKED, str(exc))

        public_bytes = self._get_stored_public_key_bytes()
        if public_bytes is None:
            raise RuntimeError(
                f"{self.name}: encryption keypair not loaded after prepare_for_readiness; "
                "call ensure_encryption_keypair explicitly or check startup_sequence ordering.",
            )
        return self._success({"public_key": self._b64encode_bytes(public_bytes)})

    @requires_operator_principal
    def export_encrypted(
        self,
        secret_name: str,
        recipient_pubkey: str,
        recipient_identifier: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Seal a local secret for a recipient's X25519 public key.

        The plaintext is fetched from the local vault, encrypted with the
        recipient's pubkey, and the plaintext goes out of scope at function
        exit. Returns ciphertext + plaintext fingerprint only.
        """

        try:
            recipient_pub_bytes = self._b64decode_str(recipient_pubkey)
        except ValueError:
            return self._error(
                ErrorCode.INVALID_PUBKEY, "recipient_pubkey is not valid base64",
            )
        if len(recipient_pub_bytes) != X25519_KEY_LENGTH_BYTES:
            return self._error(
                ErrorCode.INVALID_PUBKEY,
                f"recipient_pubkey must decode to {X25519_KEY_LENGTH_BYTES} bytes; "
                f"got {len(recipient_pub_bytes)}",
            )
        peer_fp = self._pubkey_fingerprint(recipient_pub_bytes)

        try:
            self._get_crypto()
        except VaultLockedError as exc:
            return self._error(ErrorCode.VAULT_LOCKED, str(exc))

        retrieve_result = self._retrieve_impl(secret_name)
        if retrieve_result.get("action_status") != ActionStatus.COMPLETED.value:
            return retrieve_result
        plaintext_value = retrieve_result.get("data", {}).get("value")
        if not isinstance(plaintext_value, str):
            self._audit_transfer(
                direction=AUDIT_DIRECTION_EXPORT,
                secret_name=secret_name,
                peer_identifier=recipient_identifier,
                peer_pubkey_fingerprint=peer_fp,
                plaintext_fingerprint="",
                status=AUDIT_STATUS_ERROR,
                error_message="secret_not_found",
            )
            return self._error(
                ErrorCode.SECRET_NOT_FOUND, f"no secret named {secret_name!r} in vault",
            )

        plaintext_bytes = plaintext_value.encode("utf-8")
        try:
            ciphertext_bytes = SealedBox(PublicKey(recipient_pub_bytes)).encrypt(
                plaintext_bytes,
            )
            plaintext_fp = self._plaintext_fingerprint(plaintext_bytes)
        finally:
            del plaintext_bytes  # belt-and-braces; the local frame drops it anyway

        self._audit_transfer(
            direction=AUDIT_DIRECTION_EXPORT,
            secret_name=secret_name,
            peer_identifier=recipient_identifier,
            peer_pubkey_fingerprint=peer_fp,
            plaintext_fingerprint=plaintext_fp,
            status=AUDIT_STATUS_SUCCESS,
            error_message=None,
        )
        return self._success(
            {
                "ciphertext": self._b64encode_bytes(ciphertext_bytes),
                "plaintext_fingerprint": plaintext_fp,
            },
        )

    @requires_operator_principal
    def import_encrypted(
        self,
        name: str,
        ciphertext: str,
        sender_identifier: str | None = None,
        overwrite: bool = False,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Decrypt a sealed-box payload and store the plaintext in the local vault.

        The plaintext is decrypted, stored via the existing AES-256-GCM-at-rest
        path, and goes out of scope at function exit. Returns `{ok, fingerprint}`
        only — the plaintext is never returned.
        """

        try:
            self._get_crypto()
        except VaultLockedError as exc:
            return self._error(ErrorCode.VAULT_LOCKED, str(exc))

        private_bytes = self._get_stored_private_key_bytes()
        if private_bytes is None:
            raise RuntimeError(
                f"{self.name}: encryption keypair not loaded after prepare_for_readiness; "
                "call ensure_encryption_keypair explicitly or check startup_sequence ordering.",
            )
        local_pub_fp = self._pubkey_fingerprint(
            self._get_stored_public_key_bytes() or b"",
        )

        try:
            ciphertext_bytes = self._b64decode_str(ciphertext)
        except ValueError:
            return self._error(
                ErrorCode.INVALID_CIPHERTEXT, "ciphertext is not valid base64",
            )

        # Reject the no-overwrite-against-existing case BEFORE decrypting — no
        # reason to bring plaintext into memory only to throw it away. The
        # audit record on this path omits plaintext_fingerprint because the
        # plaintext was never observed.
        existing = self._get_by_key(name)
        if existing and not overwrite:
            self._audit_transfer(
                direction=AUDIT_DIRECTION_IMPORT,
                secret_name=name,
                peer_identifier=sender_identifier,
                peer_pubkey_fingerprint=local_pub_fp,
                plaintext_fingerprint="",
                status=AUDIT_STATUS_ERROR,
                error_message="secret_already_exists",
            )
            del private_bytes
            return self._error(
                ErrorCode.SECRET_ALREADY_EXISTS,
                f"secret {name!r} already exists; pass overwrite=true to replace",
            )

        try:
            plaintext_bytes = SealedBox(PrivateKey(private_bytes)).decrypt(
                ciphertext_bytes,
            )
        except CryptoError:
            self._audit_transfer(
                direction=AUDIT_DIRECTION_IMPORT,
                secret_name=name,
                peer_identifier=sender_identifier,
                peer_pubkey_fingerprint=local_pub_fp,
                plaintext_fingerprint="",
                status=AUDIT_STATUS_ERROR,
                error_message="decrypt_failed",
            )
            return self._error(
                ErrorCode.DECRYPT_FAILED,
                "sealed-box decryption failed (wrong recipient or corrupted ciphertext)",
            )
        finally:
            del private_bytes

        try:
            plaintext_str = plaintext_bytes.decode("utf-8")
            store_result = (
                self._rotate_impl(name, plaintext_str)
                if existing
                else self._store_impl(name, plaintext_str, [], {})
            )
            plaintext_fp = self._plaintext_fingerprint(plaintext_bytes)
        finally:
            del plaintext_bytes

        if store_result.get("action_status") != ActionStatus.COMPLETED.value:
            self._audit_transfer(
                direction=AUDIT_DIRECTION_IMPORT,
                secret_name=name,
                peer_identifier=sender_identifier,
                peer_pubkey_fingerprint=local_pub_fp,
                plaintext_fingerprint=plaintext_fp,
                status=AUDIT_STATUS_ERROR,
                error_message="storage_failed",
            )
            return store_result

        self._audit_transfer(
            direction=AUDIT_DIRECTION_IMPORT,
            secret_name=name,
            peer_identifier=sender_identifier,
            peer_pubkey_fingerprint=local_pub_fp,
            plaintext_fingerprint=plaintext_fp,
            status=AUDIT_STATUS_SUCCESS,
            error_message=None,
        )
        return self._success({"ok": True, "plaintext_fingerprint": plaintext_fp})

    @requires_operator_principal
    def oauth_client_register(
        self,
        client_name: str,
        scopes: builtins.list[str] | None = None,
        redirect_uris: builtins.list[str] | None = None,
        grant_types: builtins.list[str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Register an operator-approved OAuth client.

        Defaults the per-client grant_types to
        ``["authorization_code", "refresh_token"]`` — the standard
        claude.ai connector shape. Pass an explicit list (e.g.
        ``["client_credentials"]``) for machine-to-machine clients.
        Values outside the allowlist {authorization_code,
        client_credentials, refresh_token} are rejected.

        Per Task #31, every minted client is operator_approved=True —
        the MCP transport refuses to issue tokens otherwise. The
        cleartext client_secret leaves the vault exactly once in this
        response.
        """
        if self._oauth_clients_store is None:
            return self._error(
                ErrorCode.OAUTH_CLIENT_STORE_UNAVAILABLE,
                "OAuth client store not opened; check vault initialization",
            )
        params: dict[str, Any] = {
            "client_name": client_name,
            "scopes": scopes or ["mcp:read", "mcp:write"],
            "redirect_uris": redirect_uris or [],
            "grant_types": (
                grant_types
                if grant_types is not None
                else ["authorization_code", "refresh_token"]
            ),
        }
        try:
            return self._success(self._oauth_registry.register_client(params))
        except OauthGrantValidationError as exc:
            return self._error(
                ErrorCode.OAUTH_CLIENT_INVALID_NAME, str(exc),
            )

    @requires_operator_principal
    def oauth_client_revoke(
        self,
        client_id: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Delete an OAuth client by id (registry delegation)."""
        if self._oauth_clients_store is None:
            return self._error(
                ErrorCode.OAUTH_CLIENT_STORE_UNAVAILABLE,
                "OAuth client store not opened; check vault initialization",
            )
        if not client_id:
            return self._error(
                ErrorCode.OAUTH_CLIENT_INVALID_NAME,
                "client_id must be a non-empty string",
            )
        removed = self._oauth_registry.revoke_client(client_id)
        return self._success(
            {"client_id": client_id, "removed": removed},
        )

    @requires_operator_principal
    def oauth_client_list(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """List OAuth clients with public metadata only (registry delegation)."""
        if self._oauth_clients_store is None:
            return self._error(
                ErrorCode.OAUTH_CLIENT_STORE_UNAVAILABLE,
                "OAuth client store not opened; check vault initialization",
            )
        return self._success({"clients": self._oauth_registry.list_clients()})

    @requires_operator_principal
    def oauth_client_add_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Idempotently append a redirect_uri (registry delegation)."""
        if self._oauth_clients_store is None:
            return self._error(
                ErrorCode.OAUTH_CLIENT_STORE_UNAVAILABLE,
                "OAuth client store not opened; check vault initialization",
            )
        if not client_id:
            return self._error(
                ErrorCode.OAUTH_CLIENT_INVALID_NAME,
                "client_id must be a non-empty string",
            )
        if not redirect_uri:
            return self._error(
                ErrorCode.OAUTH_CLIENT_INVALID_NAME,
                "redirect_uri must be a non-empty string",
            )
        try:
            return self._success(
                self._oauth_registry.add_redirect_uri(client_id, redirect_uri),
            )
        except OauthClientNotFoundError:
            return self._error(
                ErrorCode.OAUTH_CLIENT_NOT_FOUND,
                f"OAuth client {client_id!r} is not registered",
            )

    def issue_oauth_refresh_token(
        self,
        *,
        client_id: str,
        scopes: builtins.list[str],
        audience: str,
        ttl_seconds: int,
        call_context: CallContext | None = None,
    ) -> str:
        """Mint a fresh cleartext refresh token; thin registry delegation."""
        return self._oauth_registry.issue_refresh_token(
            client_id=client_id,
            scopes=scopes,
            audience=audience,
            ttl_seconds=ttl_seconds,
        )

    def consume_oauth_refresh_token(
        self,
        cleartext: str,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any] | None:
        """Single-use consume + rotation; thin registry delegation."""
        return self._oauth_registry.consume_refresh_token(cleartext)

    def lookup_oauth_client(
        self,
        client_id: str,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any] | None:
        """Public-metadata lookup by ``client_id``; thin registry delegation.

        Used by ``/authorize`` (RFC 6749 §4.1.1). Returns the projected
        public metadata or ``None`` if the id is unknown. Not a
        ``@platform_process``.
        """
        return self._oauth_registry.lookup_client(client_id)

    def record_oauth_client_token_use(
        self,
        client_id: str,
        *,
        client_ip: str = "",
        user_agent: str = "",
        transport: str = "",
        call_context: CallContext | None = None,
    ) -> bool:
        """Record last-use evidence after a successful token issuance.

        Thin registry delegation, called in-process by
        ``/oauth/token`` on every success (initial grant and silent
        refresh alike). Not a ``@platform_process``. Returns True iff
        a client row matched; a miss is logged by the registry and
        never raised, because the token is already issued by then.
        """
        return self._oauth_registry.record_token_use(
            client_id,
            client_ip=client_ip,
            user_agent=user_agent,
            transport=transport,
        )

    def verify_oauth_client_credentials(
        self,
        client_id: str,
        client_secret: str,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any] | None:
        """Constant-time secret verify; thin registry delegation.

        Returns the projected public metadata on a successful
        verification, ``None`` if the client_id is unknown OR the
        secret does not hash to the stored value. Not a
        ``@platform_process``; used in-process by ``/oauth/token``
        only.
        """
        return self._oauth_registry.verify_client_credentials(
            client_id, client_secret,
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Private implementation helpers (called from the structural interface
    # methods above; the @platform_process plugin-process surface was removed
    # in W-VAULT-CALLER-ENFORCE sub-2 — service_interface::vault_service::*
    # is the only entry point now).
    # ─────────────────────────────────────────────────────────────────────────

    # Dual-substrate helpers (W-VAULT-LOCAL-KEYCHAIN Tier 3, 2026-06-07).
    # The vault impl methods layer ``self._keychain`` (per-credential OS
    # Keychain) on top of the state-service substrate per Codex sign-off
    # correction #3: both substrates must succeed for a write to commit,
    # partial-failure paths roll back the side that succeeded so the
    # substrates never silently diverge. State service stays authoritative
    # for ``list`` (no portable Keychain enumeration — Codex correction
    # #5). All persistence routes through ``state_service``; no plugin-
    # local Postgres driver per [[state-service-is-the-only-postgres-path]].

    def _parse_scoped_key(self, key: str) -> tuple[str, str] | None:
        """Split a scoped vault key ``<solet>.<plugin>.<credential>`` into ``(plugin_name, credential)``.

        Returns ``None`` when the key isn't in three-segment scoped form.
        Pre-Tier-2-sub-2 callers may still hold flat keys; those take the
        state-service-only path because the per-credential Keychain
        service name needs the plugin segment to scope correctly.
        """
        parts = key.split(".", 2)
        if len(parts) != 3:
            return None
        _solet, plugin_name, credential = parts
        if not plugin_name or not credential:
            return None
        return plugin_name, credential

    # ─────────────────────────────────────────────────────────────────────
    # Runtime impls (Keychain-only post-P0-A).
    #
    # The substrate is per-credential macOS Keychain entries at
    # (service=`<solet>.<plugin>`, account=`<credential>`). The
    # dual-substrate code paths + every state-service write helper were
    # retired in P0-A Round 3 (2026-06-09). The keypair-migrator at
    # _migrate_legacy_keypair_if_present still calls _get_by_key + the
    # _state_rename_secret helper below — those two SQL paths are
    # preserved for the migrator's narrow use, not as a runtime fallback.
    # ─────────────────────────────────────────────────────────────────────

    def _require_keychain_pair(self, key: str) -> tuple[str, str]:
        """Resolve ``(plugin_name, credential)`` for a scoped key; raise loudly otherwise.

        Substitutes for the old dual-substrate ``_keychain_write_pair``: the
        Keychain is the only substrate now, so absence is an
        unrecoverable readiness failure, not a "fall through to SQL" hint.
        """
        if self._keychain is None:
            raise RuntimeError(
                f"{self.name}: per-credential Keychain substrate not initialized "
                "before vault verb dispatched. Check prepare_for_readiness ordering.",
            )
        pair = self._parse_scoped_key(key)
        if pair is None:
            raise ValueError(
                f"{self.name}: key {key!r} is not in scoped form "
                "'<solet>.<plugin>.<credential>'. The vault no longer "
                "accepts non-scoped keys.",
            )
        return pair

    def _state_rename_secret(self, old_key: str, new_key: str) -> ActionResult | None:
        """State-service-side atomic rename — keypair migrator's only client.

        Preserved (NOT a runtime fallback) because the startup
        keypair migration pattern at _migrate_legacy_keypair_secret needs
        to rename pre-Tier-3 legacy keypair rows from
        ``<solet>.default_vault_plugin.identity__encryption_*_key`` to
        ``<solet>.macos_vault_plugin.identity__encryption_*_key`` if the
        legacy rows still exist on any solet. On this solet
        the rename completed via Phase 1 of the P0-A data migration —
        this method is a no-op-in-practice but stays load-bearing for
        first-cold-boot of any future solet that's still on the
        legacy substrate.
        """
        if not self.state_service:
            return self._error(
                ErrorCode.ENCRYPTION_FAILED, "State service not available",
            )
        result = self.state_service.update_state(
            namespace=VAULT_NAMESPACE,
            query={"table": "secret", "filters": {"secret_key": old_key}},
            updates={"secret_key": new_key, "updated_at": self._now()},
        )
        if result.get("action_status") != "completed":
            return self._error(
                ErrorCode.ENCRYPTION_FAILED,
                "Failed to rename secret in state service",
            )
        return None

    def _store_impl(
        self, key: str, value: str, tags: builtins.list[str], metadata: dict[str, str]
    ) -> ActionResult:
        """Keychain-only store. ``tags`` / ``metadata`` are not yet persisted (Round 4 kSecAttrGeneric scope)."""
        del tags, metadata  # accepted for interface stability; persisted in Round 4
        pair = self._require_keychain_pair(key)
        if self._keychain is None:  # narrowed for type-checker; _require_keychain_pair already raised
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        if self._keychain.exists_credential(*pair):
            return self._error(
                ErrorCode.KEY_EXISTS,
                f"Secret '{key}' already exists. Use rotate to update.",
            )
        self._keychain.store_credential(pair[0], pair[1], value.encode("utf-8"))
        self.logger.debug("Secret stored: %s", key)
        return self._success({"key": key, "version": 1, "message": "Secret stored"})

    def _retrieve_impl(self, key: str) -> ActionResult:
        """Keychain-only retrieve. No SQL fallback, no file-bootstrap fallback."""
        pair = self._require_keychain_pair(key)
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        value_bytes = self._keychain.retrieve_credential(*pair)
        if value_bytes is None:
            return self._not_found(key)
        return self._success({"key": key, "value": value_bytes.decode("utf-8")})

    def _delete_impl(self, key: str) -> ActionResult:
        """Keychain-only delete."""
        pair = self._require_keychain_pair(key)
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        if not self._keychain.exists_credential(*pair):
            return self._not_found(key)
        self._keychain.delete_credential(*pair)
        self.logger.debug("Secret deleted: %s", key)
        return self._success({"key": key, "message": "Secret deleted"})

    def _exists_impl(self, key: str) -> ActionResult:
        """Keychain-only exists check."""
        pair = self._require_keychain_pair(key)
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        return self._success({
            "key": key,
            "exists": self._keychain.exists_credential(*pair),
        })

    def _list_impl(self, tag: str | None) -> ActionResult:
        """Keychain enumeration via SystemKeychain.list_credentials_under_solet().

        ``tag`` filter currently a no-op because tags+metadata are not
        persisted under the Keychain-only contract until Round 4 adds
        kSecAttrGeneric JSON. Callers passing a tag get an empty result
        until then; this is a known regression vs the SQL substrate's
        tag-indexed list and is documented in
        ``workbench/2026-06-09_p0a_overnight_progress.md``.
        """
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        solet = os.environ.get("SOLET_NAME", "").strip()
        if not solet:
            return self._error(
                ErrorCode.ENCRYPTION_FAILED,
                "SOLET_NAME env var is required for vault list",
            )
        pairs = self._keychain.list_credentials_under_solet()
        secrets: list[dict[str, Any]] = [
            {
                "key": f"{solet}.{plugin}.{credential}",
                "tags": [],
                "metadata": {},
                "created_at": None,
                "updated_at": None,
                "version": 1,
            }
            for plugin, credential in pairs
        ]
        if tag:
            secrets = [s for s in secrets if tag in s["tags"]]
        return self._success({"secrets": secrets, "count": len(secrets)})

    def _rename_impl(self, old_key: str, new_key: str) -> ActionResult:
        """Keychain-only rename: read-store-delete sequence on the per-credential surface."""
        if old_key == new_key:
            return self._success({
                "old_key": old_key,
                "new_key": new_key,
                "message": "Secret rename is a no-op (old_key == new_key)",
            })
        old_pair = self._require_keychain_pair(old_key)
        new_pair = self._require_keychain_pair(new_key)
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")

        if not self._keychain.exists_credential(*old_pair):
            return self._not_found(old_key)
        if self._keychain.exists_credential(*new_pair):
            return self._error(
                ErrorCode.KEY_EXISTS,
                f"Target key '{new_key}' already exists. Refusing to overwrite.",
            )

        value_bytes = self._keychain.retrieve_credential(*old_pair)
        if value_bytes is None:
            return self._not_found(old_key)
        self._keychain.store_credential(new_pair[0], new_pair[1], value_bytes)
        self._keychain.delete_credential(*old_pair)
        self.logger.debug("Secret renamed: %s -> %s", old_key, new_key)
        return self._success({
            "old_key": old_key,
            "new_key": new_key,
            "message": "Secret renamed",
        })

    def _rotate_impl(self, key: str, new_value: str) -> ActionResult:
        """Keychain-only rotate: overwrite in place. Version tracking awaits Round 4 kSecAttrGeneric."""
        pair = self._require_keychain_pair(key)
        if self._keychain is None:
            return self._error(ErrorCode.ENCRYPTION_FAILED, "Keychain unavailable")
        if not self._keychain.exists_credential(*pair):
            return self._not_found(key)
        self._keychain.store_credential(pair[0], pair[1], new_value.encode("utf-8"))
        self.logger.debug("Secret rotated: %s", key)
        return self._success({
            "key": key,
            "version": 1,
            "message": "Secret rotated",
        })

    # ─────────────────────────────────────────────────────────────────────────
    # Credential Ingestion (interface methods)
    #
    # Each method resolves a value from a local source the agent named, then
    # delegates to _store_impl. The plaintext never leaves the platform process.
    # ─────────────────────────────────────────────────────────────────────────

    @requires_operator_principal
    def store_from_env(
        self,
        key: str,
        env_var: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Read a value from an env var and store it - interface method."""
        enforce_namespace(key, call_context)
        return self._store_from_env_impl(key, env_var, tags or [], metadata or {})

    @requires_operator_principal
    def store_from_file(
        self,
        key: str,
        file_path: str,
        strip: bool = True,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Read a value from a file and store it - interface method."""
        enforce_namespace(key, call_context)
        return self._store_from_file_impl(key, file_path, strip, tags or [], metadata or {})

    @requires_operator_principal
    def store_from_kv_file(
        self,
        key: str,
        file_path: str,
        field: str,
        format: str | None = None,
        strip: bool = True,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Read a named field from a kv file and store it - interface method."""
        enforce_namespace(key, call_context)
        return self._store_from_kv_file_impl(
            key, file_path, field, format, strip, tags or [], metadata or {}
        )

    @requires_operator_principal
    def store_from_keychain(
        self,
        key: str,
        service: str,
        account: str | None = None,
        keychain: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Read a value from the OS keychain and store it - interface method."""
        enforce_namespace(key, call_context)
        return self._store_from_keychain_impl(
            key, service, account, keychain, tags or [], metadata or {}
        )

    def _store_from_env_impl(
        self,
        key: str,
        env_var: str,
        tags: builtins.list[str],
        metadata: dict[str, str],
    ) -> ActionResult:
        """Resolve value from env var, then store via the shared internal path."""
        raw = os.environ.get(env_var)
        if raw is None:
            return self._error(
                ErrorCode.ENV_VAR_NOT_SET,
                f"Environment variable '{env_var}' is not set.",
            )
        if len(raw) == 0:
            return self._error(
                ErrorCode.ENV_VAR_EMPTY,
                f"Environment variable '{env_var}' is set but empty.",
            )
        return self._store_impl(key, raw, tags, metadata)

    def _store_from_file_impl(
        self,
        key: str,
        file_path: str,
        strip: bool,
        tags: builtins.list[str],
        metadata: dict[str, str],
    ) -> ActionResult:
        """Resolve value from file path, then store via the shared internal path."""
        path_obj = Path(file_path)
        if not path_obj.is_absolute():
            return self._error(
                ErrorCode.FILE_PATH_NOT_ABSOLUTE,
                f"File path must be absolute: {file_path!r}",
            )
        if not path_obj.exists():
            return self._error(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: {file_path!r}",
            )
        try:
            raw = path_obj.read_text(encoding="utf-8")
        except OSError as exc:
            return self._error(
                ErrorCode.FILE_UNREADABLE,
                f"File unreadable: {type(exc).__name__}",
            )

        value = raw.rstrip() if strip else raw
        if len(value) == 0:
            return self._error(
                ErrorCode.FILE_EMPTY,
                f"File is empty (or whitespace-only after strip): {file_path!r}",
            )
        return self._store_impl(key, value, tags, metadata)

    def _store_from_kv_file_impl(
        self,
        key: str,
        file_path: str,
        field: str,
        fmt: str | None,
        strip: bool,
        tags: builtins.list[str],
        metadata: dict[str, str],
    ) -> ActionResult:
        """Resolve a value from a kv file's named field, then store it."""
        path_obj = Path(file_path)
        if not path_obj.is_absolute():
            return self._error(
                ErrorCode.FILE_PATH_NOT_ABSOLUTE,
                f"File path must be absolute: {file_path!r}",
            )
        if not path_obj.exists():
            return self._error(
                ErrorCode.FILE_NOT_FOUND,
                f"File not found: {file_path!r}",
            )
        try:
            text = path_obj.read_text(encoding="utf-8")
        except OSError as exc:
            return self._error(
                ErrorCode.FILE_UNREADABLE,
                f"File unreadable: {type(exc).__name__}",
            )

        kv_error = self._extract_kv_value(text, path_obj, field, fmt)
        if isinstance(kv_error, dict):
            return kv_error
        value = kv_error.rstrip() if strip else kv_error
        if len(value) == 0:
            return self._error(
                ErrorCode.KV_FILE_FIELD_EMPTY,
                f"Field {field!r} is empty (or whitespace-only after strip).",
            )
        return self._store_impl(key, value, tags, metadata)

    def _extract_kv_value(
        self,
        text: str,
        path_obj: Path,
        field: str,
        fmt: str | None,
    ) -> str | ActionResult:
        """Extract a named field from kv-file text. Returns value str or ActionResult error."""
        try:
            resolved_format = resolve_format(path_obj, fmt)
            return extract_field(text, resolved_format, field)
        except KVFileFormatUnknownError as exc:
            return self._error(ErrorCode.KV_FILE_FORMAT_UNKNOWN, str(exc))
        except KVFileFieldNotFoundError as exc:
            return self._error(ErrorCode.KV_FILE_FIELD_NOT_FOUND, str(exc))
        except KVFileFieldNotScalarError as exc:
            return self._error(ErrorCode.KV_FILE_FIELD_NOT_SCALAR, str(exc))
        except KVFileParseError as exc:
            return self._error(ErrorCode.KV_FILE_PARSE_ERROR, str(exc))

    def _store_from_keychain_impl(
        self,
        key: str,
        service: str,
        account: str | None,
        keychain: str | None,
        tags: builtins.list[str],
        metadata: dict[str, str],
    ) -> ActionResult:
        """Resolve a value from the OS keychain, then store it.

        On Darwin, searches both iCloud Keychain (Apple Passwords) and the legacy
        login keychain via the Security framework. On other platforms, uses the
        ``keyring`` library (libsecret on Linux, Credential Manager on Windows).
        """
        if keychain is not None:
            return self._error(
                ErrorCode.KEYCHAIN_SELECTION_UNSUPPORTED,
                "Per-keychain selection is reserved for future use; pass keychain=null.",
            )

        resolved_account = account if account is not None else getpass.getuser()
        raw_or_error = self._lookup_keychain_password(service, resolved_account)
        if isinstance(raw_or_error, dict):
            return raw_or_error
        if raw_or_error is None:
            return self._keychain_not_found_error(service, resolved_account)
        if len(raw_or_error) == 0:
            return self._error(
                ErrorCode.KEYCHAIN_ENTRY_EMPTY,
                f"Keychain entry for service={service!r} account={resolved_account!r} is empty.",
            )
        return self._store_impl(key, raw_or_error, tags, metadata)

    def _lookup_keychain_password(
        self, service: str, account: str
    ) -> str | None | ActionResult:
        """Look up a keychain password, dispatching by platform.

        Returns the password string (found), ``None`` (not found), or an
        ``ActionResult`` error (backend unavailable / framework load failure).
        """
        if sys.platform == DARWIN_PLATFORM:
            return self._lookup_darwin(service, account)
        return self._lookup_via_keyring(service, account)

    def _lookup_darwin(self, service: str, account: str) -> str | None | ActionResult:
        """Darwin path: query both iCloud and login keychains via Security framework.

        Runs under the no-prompt gate: this is a daemon read, so an item whose
        ACL does not authorize the running binary must fail loud rather than
        block on a SecurityAgent dialog nobody will answer.
        """
        try:
            with keychain_interaction.user_interaction_disallowed():
                return macos_keychain.get_password(service, account)
        except ImportError as exc:
            return self._error(
                ErrorCode.KEYCHAIN_UNAVAILABLE,
                f"macOS Security framework unavailable: {exc}.",
            )

    def _lookup_via_keyring(self, service: str, account: str) -> str | None | ActionResult:
        """Non-Darwin path: keyring (libsecret on Linux, Credential Manager on Windows)."""
        backend_module = type(keyring.get_keyring()).__module__
        if backend_module in (KEYRING_FAIL_BACKEND_MODULE, KEYRING_NULL_BACKEND_MODULE):
            return self._error(
                ErrorCode.KEYCHAIN_UNAVAILABLE,
                f"No real keychain backend available (active: {backend_module}).",
            )
        return keyring.get_password(service, account)

    def _keychain_not_found_error(self, service: str, account: str) -> ActionResult:
        """Build the keychain_entry_not_found error, with Darwin-specific phrasing."""
        if sys.platform == DARWIN_PLATFORM:
            message = KEYCHAIN_NOT_FOUND_DARWIN_DETAIL.format(service=service, account=account)
        else:
            message = f"No keychain entry for service={service!r} account={account!r}."
        return self._error(ErrorCode.KEYCHAIN_ENTRY_NOT_FOUND, message)

    # ─────────────────────────────────────────────────────────────────────────
    # Internal Minting
    #
    # Mints a cryptographically random secret directly inside the vault
    # process and stores it under the named key. The plaintext never
    # leaves the process; the caller only ever sees a fingerprint. Used
    # by first-boot orchestrators that need to seed tokens (admin user
    # token, transport bootstrap tokens, etc.) into a fresh solet
    # without those bytes touching any orchestrator memory or log.
    # ─────────────────────────────────────────────────────────────────────────

    def _store_random_impl(
        self,
        key: str,
        byte_length: int,
        tags: builtins.list[str],
        metadata: dict[str, str],
    ) -> ActionResult:
        """Mint ``byte_length`` random bytes, base64-encode, store under ``key``.

        Plaintext lives only on the local frame and inside the encrypted vault
        row at rest. Never returned. The agent / orchestrator that triggered
        this call sees only ``key`` and the one-way ``plaintext_fingerprint``.
        """
        if byte_length < 16:
            return self._error(
                ErrorCode.INVALID_BYTE_LENGTH,
                f"byte_length must be >= 16; got {byte_length}",
            )

        raw_bytes = os.urandom(byte_length)
        try:
            value = self._b64encode_bytes(raw_bytes)
            fp = self._plaintext_fingerprint(value.encode("utf-8"))
            store_result = self._store_impl(key, value, tags, metadata)
        finally:
            del raw_bytes

        if store_result.get("action_status") != ActionStatus.COMPLETED.value:
            return store_result
        return self._success(
            {
                "key": key,
                "byte_length": byte_length,
                "plaintext_fingerprint": fp,
                "message": "Random secret minted and stored",
            }
        )

    # ─────────────────────────────────────────────────────────────────────────
    # Cross-Solet Sealed-Box Secret Transfer
    #
    # libsodium anonymous-sender sealed boxes (X25519). Each solet owns
    # an X25519 identity keypair stored as two regular vault secrets, so the
    # private key never bypasses AES-256-GCM-at-rest. Transfer protocol:
    #
    #   1. recipient.get_public_key()
    #   2. sender.export_encrypted(secret_name, recipient_pubkey)
    #   3. recipient.import_encrypted(name, ciphertext)
    #
    # Returned `plaintext_fingerprint`s on (2) and (3) must match — that is
    # how the agent confirms intact transfer without ever seeing plaintext.
    # ─────────────────────────────────────────────────────────────────────────

    def _b64encode_bytes(self, data: bytes) -> str:
        return base64.b64encode(data).decode("ascii")

    def _b64decode_str(self, text: str) -> bytes:
        return base64.b64decode(text.encode("ascii"))

    def _plaintext_fingerprint(self, plaintext: bytes) -> str:
        digest = hashlib.sha256(plaintext).hexdigest()[:PLAINTEXT_FINGERPRINT_HEX_LEN]
        return f"{PLAINTEXT_FINGERPRINT_PREFIX}{digest}"

    def _pubkey_fingerprint(self, pub_bytes: bytes) -> str:
        digest = hashlib.sha256(pub_bytes).hexdigest()[:PUBLIC_KEY_FINGERPRINT_HEX_LEN]
        return f"{PUBLIC_KEY_FINGERPRINT_PREFIX}{digest}"

    def _audit_transfer(
        self,
        *,
        direction: str,
        secret_name: str,
        peer_identifier: str | None,
        peer_pubkey_fingerprint: str,
        plaintext_fingerprint: str,
        status: str,
        error_message: str | None,
    ) -> None:
        """Append one row to the secret_transfer_audit Store.

        Never writes plaintext or ciphertext — only the fingerprints,
        peer identifier (free-text label, not the pubkey), and outcome.
        Standard ``id``/``created_at``/``updated_at`` columns are
        auto-filled by the Store backend.

        The Store is opened in ``prepare_for_readiness``; reaching this
        method before then is a programming error and raises
        ``RuntimeError``.
        """
        if self._audit_store is None:
            raise RuntimeError(
                f"{self.name}: _audit_transfer called before "
                "prepare_for_readiness opened _audit_store"
            )
        self._audit_store.insert(
            {
                "direction": direction,
                "secret_name": secret_name,
                "peer_identifier": peer_identifier,
                "peer_pubkey_fingerprint": peer_pubkey_fingerprint,
                "plaintext_fingerprint": plaintext_fingerprint,
                "status": status,
                "error_message": error_message,
            }
        )

    def _get_stored_public_key_bytes(self) -> bytes | None:
        """Return the locally-stored X25519 public key, or None if absent."""
        result = self._retrieve_impl(ENCRYPTION_KEYPAIR_PUBLIC_KEY)
        value = result.get("data", {}).get("value")
        if not isinstance(value, str):
            return None
        return self._b64decode_str(value)

    def _get_stored_private_key_bytes(self) -> bytes | None:
        """Return the locally-stored X25519 private key, or None if absent.

        Caller MUST treat the returned bytes as a local variable only — never
        assign to ``self`` and never include in returned dicts or log fields.
        """
        result = self._retrieve_impl(ENCRYPTION_KEYPAIR_PRIVATE_KEY)
        value = result.get("data", {}).get("value")
        if not isinstance(value, str):
            return None
        return self._b64decode_str(value)

    def _generate_and_store_keypair(self) -> bytes:
        """Generate a fresh X25519 keypair, store both halves, return public bytes.

        Private and public both go through the existing AES-256-GCM-at-rest
        store path — the private key is never written to a plugin attribute
        and goes out of scope at function exit.

        When the secret rows already exist from a prior boot (e.g., the
        master-key wrap file was lost and ``_get_stored_*_key_bytes``
        couldn't decrypt the existing values, so the caller is
        regenerating), fall through to ``_rotate_impl`` to replace the
        rows in place.  Without this, the UNIQUE constraint on
        ``secret_key`` causes the INSERT to fail silently, the new
        keypair never lands in the database, and every subsequent
        ``get_public_key`` call regenerates another doomed in-memory
        keypair.
        """
        sk = PrivateKey.generate()
        private_bytes = bytes(sk)
        public_bytes = bytes(sk.public_key)

        if not self._store_or_rotate(
            ENCRYPTION_KEYPAIR_PRIVATE_KEY,
            self._b64encode_bytes(private_bytes),
        ):
            return public_bytes  # caller surfaces store failure; refuse to lose pub
        self._store_or_rotate(
            ENCRYPTION_KEYPAIR_PUBLIC_KEY,
            self._b64encode_bytes(public_bytes),
        )
        return public_bytes

    def _store_or_rotate(self, key: str, value: str) -> bool:
        """Persist ``key=value`` via INSERT, falling back to ROTATE on conflict.

        Returns True iff the secret is now stored under the current
        master key.  Used by :meth:`_generate_and_store_keypair` so a
        re-bootstrap after master-key rotation overwrites stale rows
        rather than silently failing on the UNIQUE constraint.
        """
        store_result = self._store_impl(key, value, [], {})
        if store_result.get("action_status") == ActionStatus.COMPLETED.value:
            return True
        # Only auto-rotate on the specific "already exists" path; other
        # failures (encryption error, state-service down) propagate as
        # store failures.
        error: Any = store_result.get("error") or {}
        if error.get("code") != ErrorCode.KEY_EXISTS:
            return False
        rotate_result = self._rotate_impl(key, value)
        return rotate_result.get(
            "action_status",
        ) == ActionStatus.COMPLETED.value

    def _ensure_keypair_internal(self) -> tuple[bytes, bool]:
        """Idempotent keypair bootstrap. Returns (public_bytes, created).

        Used by the public ``ensure_encryption_keypair`` action and as the
        lazy-init helper inside ``get_public_key`` / ``import_encrypted`` so
        callers do not have to remember to bootstrap first.
        """
        existing = self._get_stored_public_key_bytes()
        if existing is not None:
            return existing, False
        public_bytes = self._generate_and_store_keypair()
        return public_bytes, True


class _StoreOAuthClientStorage:
    """OAuthClientStorage adapter backed by a state-service Store.

    Used by MacosVaultPlugin (local Postgres). The cloud SM vault
    has a different adapter that mutates the SM bundle dict instead.
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def get_client(self, client_id: str) -> Mapping[str, Any] | None:
        return self._store.read_one({"client_id": client_id})

    def insert_client(self, record: dict[str, Any]) -> None:
        self._store.insert(record)

    def delete_client(self, client_id: str) -> int:
        return self._store.delete({"client_id": client_id}, soft_delete=False)

    def list_clients(self) -> list[Mapping[str, Any]]:
        return list(self._store.read())

    def update_client_redirect_uris(
        self, client_id: str, redirect_uris: list[str],
    ) -> bool:
        return bool(
            self._store.update(
                {"client_id": client_id},
                {"redirect_uris": redirect_uris},
            ),
        )

    def record_client_token_use(
        self, client_id: str, fields: Mapping[str, str],
    ) -> bool:
        return bool(
            self._store.update({"client_id": client_id}, dict(fields)),
        )


class _StoreRefreshTokenStorage:
    """RefreshTokenStorage adapter backed by a state-service Store.

    Shared between both vault plugins because both back the
    refresh-token registry with an identical Postgres table (the
    SM vault keeps refresh tokens local; only the bundle holds
    OAuth client records).
    """

    def __init__(self, store: Store) -> None:
        self._store = store

    def insert_token(self, row: dict[str, Any]) -> None:
        self._store.insert(row)

    def consume_token(self, token_hash: str) -> Mapping[str, Any] | None:
        row = self._store.read_one({"token_hash": token_hash})
        if row is None:
            return None
        # Single-use: delete regardless of expiry so an expired token
        # doesn't accumulate as dead state. The registry checks expiry
        # AFTER consume and returns None for expired claims.
        self._store.delete({"token_hash": token_hash}, soft_delete=False)
        return row
