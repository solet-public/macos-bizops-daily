"""Vault Service Public API.

AI-discoverable secure credential storage operations with @service_interface_process decorators.
All methods in this interface are indexed for process discovery.

Discoverability Policy (Task #47, 2026-05-24):
- EVERY method declares ``is_discoverable=True`` explicitly. The base decorator
  default for ``@service_interface_process`` is ``is_discoverable=False`` (service
  methods are presumed internal); vault operations are agent-callable end-to-end
  (operator stores/rotates secrets, agent retrieves via ``resolve_with_secrets``,
  operator manages OAuth clients), so the per-method flag overrides the default.
- Adding a new method without ``is_discoverable=True`` will SILENTLY exclude it
  from ``process_search`` and the agent will not be able to find it. Always set
  the flag explicitly when authoring a new vault operation.
- The only way a vault operation should stay non-discoverable is if it is pure
  internal plumbing (no current cases — the four `_persist*`/`_validate*` helpers
  on the plugin are private methods, not decorated processes).

Security Model:
- Two-tier key management: passphrase -> KEK -> Master Key
- AES-256-GCM authenticated encryption
- PBKDF2 key derivation (1.2M iterations)
- Per-secret unique salt and nonce
- NEVER logs plaintext secrets or keys
"""

from __future__ import annotations

import builtins
from abc import ABC, abstractmethod

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext
from ananta.core.services.service_interface_decorator import service_interface_process


class VaultQualificationAPI(ABC):
    """Public qualification processes sharing the vault_service namespace."""

    @service_interface_process(
        name="qualify_keychain",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Configured Keychain availability and current-user accessibility",
            type=ParameterType.OBJECT,
            properties={
                "backend": ParameterMetadata(type=ParameterType.STRING, description="Vault substrate"),
                "available": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether a usable Keychain backend is configured"),
                "current_user_accessible": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether the current user can read the configured Keychain namespace"),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def qualify_keychain(self, *, call_context: CallContext | None = None) -> ActionResult:
        """Qualify the configured Keychain without exposing credential contents."""
        pass

    @service_interface_process(
        name="qualify_round_trip",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Private vault canary store, retrieve, compare, and deletion proof",
            type=ParameterType.OBJECT,
            properties={
                "stored": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether the private canary was stored"),
                "retrieved": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether the private canary was retrieved"),
                "matched": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether retrieved bytes matched the private canary"),
                "deleted": ParameterMetadata(type=ParameterType.BOOLEAN, description="Whether only the private canary was deleted before return"),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def qualify_round_trip(self, *, call_context: CallContext | None = None) -> ActionResult:
        """Qualify the vault with a private canary that is deleted before return."""
        pass


class VaultServiceAPI(ABC):
    """Public vault operations - AI-discoverable via process registry.

    This interface defines secure credential storage operations that can be
    discovered and invoked by the AI orchestration system and action templates.

    Access via: service_interface::vault_service::{method_name}
    """

    @service_interface_process(
        name="store",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the secret (e.g., 'api_keys/openai')",
                required=True,
                type=ParameterType.STRING,
            ),
            "value": ParameterMetadata(
                description="The secret value (will be encrypted)",
                required=True,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization (e.g., ['api', 'production'])",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted - do not put secrets here)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Store confirmation with key (never value)",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def store(
        self,
        key: str,
        value: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Store secret (encrypted). Returns key confirmation, never value."""
        pass

    @service_interface_process(
        name="retrieve",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="The secret identifier (e.g., 'api_keys/openai')",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Retrieved secret with decrypted value",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(type=ParameterType.STRING, description="The secret key"),
                "value": ParameterMetadata(
                    type=ParameterType.STRING, description="Decrypted secret value"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def retrieve(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Get decrypted secret value."""
        pass

    @service_interface_process(
        name="delete",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="The secret identifier to delete",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Delete confirmation",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The deleted secret key"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def delete(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Permanently delete secret."""
        pass

    @service_interface_process(
        name="list",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "tag": ParameterMetadata(
                description="Optional tag filter",
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="List of secrets (keys and metadata only)",
            type=ParameterType.OBJECT,
            properties={
                "secrets": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="List of secret records (key, tags, metadata, version)",
                ),
                "count": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Number of secrets"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def list(
        self,
        tag: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """List secret keys only (never values)."""
        pass

    @service_interface_process(
        name="exists",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="The secret identifier to check",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Existence check result",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(type=ParameterType.STRING, description="The secret key"),
                "exists": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether the secret exists"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def exists(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Check if secret exists without retrieving."""
        pass

    @service_interface_process(
        name="store_random",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the new secret",
                required=True,
                type=ParameterType.STRING,
            ),
            "byte_length": ParameterMetadata(
                description="Number of random bytes to mint (default 32 = 256 bits)",
                required=False,
                type=ParameterType.INTEGER,
                default=32,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Mint + store confirmation; plaintext is never returned",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "byte_length": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of random bytes minted",
                ),
                "plaintext_fingerprint": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="sha256(plaintext) truncated; one-way fingerprint",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def store_random(
        self,
        key: str,
        byte_length: int = 32,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Mint a fresh random secret inside the vault and store it.

        The plaintext never leaves the vault process — only ``key`` and a
        one-way ``plaintext_fingerprint`` are returned. Used by first-boot
        orchestrators that need to seed tokens into a solet without
        the bytes touching any caller-reachable surface.
        """
        pass

    @service_interface_process(
        name="rotate",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="The secret identifier", required=True, type=ParameterType.STRING
            ),
            "new_value": ParameterMetadata(
                description="New secret value", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Rotation confirmation with new version",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(type=ParameterType.STRING, description="The secret key"),
                "version": ParameterMetadata(
                    type=ParameterType.INTEGER, description="New version number"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def rotate(
        self,
        key: str,
        new_value: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Update secret value. Increments version."""
        pass

    @service_interface_process(
        name="rename",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "old_key": ParameterMetadata(
                description=(
                    "Current secret identifier. The value (and "
                    "version/tags/metadata where the substrate supports "
                    "them) is preserved across the rename."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "new_key": ParameterMetadata(
                description=(
                    "Target secret identifier. Must not already exist."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Rename confirmation. Never contains the secret value — "
                "the rename happens server-side so plaintext never "
                "crosses the process boundary."
            ),
            type=ParameterType.OBJECT,
            properties={
                "old_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The key that was renamed from",
                ),
                "new_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The key that was renamed to",
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Confirmation message",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def rename(
        self,
        old_key: str,
        new_key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rename a secret server-side; plaintext never leaves the vault."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # Credential Ingestion Operations
    #
    # The agent issues an MCP call naming a *source* (env var, file path, kv
    # field, keychain entry); the solet process reads the source, encrypts the
    # value, and stores it. The agent never sees the plaintext value.
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="store_from_env",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the secret (e.g., 'api_keys/openai')",
                required=True,
                type=ParameterType.STRING,
            ),
            "env_var": ParameterMetadata(
                description="Name of the environment variable to read the value from",
                required=True,
                type=ParameterType.STRING,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization (e.g., ['api', 'production'])",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted - do not put secrets here)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Store confirmation with key (never value)",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "version": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Stored version number"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def store_from_env(
        self,
        key: str,
        env_var: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Store secret by reading the value from an environment variable.

        The plaintext value is read from os.environ[env_var] inside the platform
        process; the caller never sees it.
        """
        pass

    @service_interface_process(
        name="store_from_file",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the secret",
                required=True,
                type=ParameterType.STRING,
            ),
            "file_path": ParameterMetadata(
                description="Absolute path to a file whose contents are the secret value",
                required=True,
                type=ParameterType.STRING,
            ),
            "strip": ParameterMetadata(
                description="Strip trailing whitespace/newlines from file contents",
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Store confirmation with key (never value)",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "version": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Stored version number"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
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
        """Store secret by reading the value from a file at an absolute path."""
        pass

    @service_interface_process(
        name="store_from_kv_file",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the secret",
                required=True,
                type=ParameterType.STRING,
            ),
            "file_path": ParameterMetadata(
                description="Absolute path to a key/value file (env, json, or yaml)",
                required=True,
                type=ParameterType.STRING,
            ),
            "field": ParameterMetadata(
                description="Name of the field within the file to extract the value from",
                required=True,
                type=ParameterType.STRING,
            ),
            "format": ParameterMetadata(
                description=(
                    "File format: 'env', 'json', or 'yaml'. "
                    "Null/omitted = auto-detect by extension."
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "strip": ParameterMetadata(
                description="Strip surrounding whitespace from the extracted value",
                required=False,
                type=ParameterType.BOOLEAN,
                default=True,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Store confirmation with key (never value)",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "version": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Stored version number"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
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
        """Store secret by extracting a named field from a structured key/value file."""
        pass

    @service_interface_process(
        name="store_from_keychain",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "key": ParameterMetadata(
                description="Unique identifier for the secret",
                required=True,
                type=ParameterType.STRING,
            ),
            "service": ParameterMetadata(
                description=(
                    "Keychain service name (the 'site/title' shown in Apple Passwords)"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "account": ParameterMetadata(
                description=(
                    "Keychain account name. If null, the current OS user's login "
                    "name (getpass.getuser()) is used."
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "keychain": ParameterMetadata(
                description=(
                    "RESERVED for future per-keychain selection. Must be null; "
                    "non-null values fail fast with keychain_selection_unsupported."
                ),
                required=False,
                type=ParameterType.STRING,
                default=None,
            ),
            "tags": ParameterMetadata(
                description="Optional tags for organization",
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "metadata": ParameterMetadata(
                description="Optional metadata (NOT encrypted)",
                required=False,
                type=ParameterType.OBJECT,
                default={},
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Store confirmation with key (never value)",
            type=ParameterType.OBJECT,
            properties={
                "key": ParameterMetadata(
                    type=ParameterType.STRING, description="The secret key that was stored"
                ),
                "version": ParameterMetadata(
                    type=ParameterType.INTEGER, description="Stored version number"
                ),
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Confirmation message"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
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
        """Store secret by reading from the OS keychain via the keyring library."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # Vault Management Operations
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="status",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Vault status information",
            type=ParameterType.OBJECT,
            properties={
                "initialized": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether vault has been initialized"
                ),
                "unlocked": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Whether vault is currently unlocked"
                ),
                "backend": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Storage backend (System Keychain or File Storage)",
                ),
                "recovery_key_exists": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Whether a recovery key has been created",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def status(self, *, call_context: CallContext | None = None) -> ActionResult:
        """Get vault status."""
        pass

    @service_interface_process(
        name="unlock",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "passphrase": ParameterMetadata(
                description="Vault passphrase", required=True, type=ParameterType.STRING
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Unlock result",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Status message"
                ),
                "unlocked": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="Lock state after operation"
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def unlock(
        self,
        passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Unlock vault with passphrase."""
        pass

    @service_interface_process(
        name="lock",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Lock result",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING, description="Status message"
                ),
                "unlocked": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="Lock state after operation (should be False)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def lock(self, *, call_context: CallContext | None = None) -> ActionResult:
        """Lock vault - clear master key from memory."""
        pass

    @service_interface_process(
        name="ensure_encryption_keypair",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Keypair bootstrap result",
            type=ParameterType.OBJECT,
            properties={
                "created": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True if a new keypair was generated, False if one was already present",
                ),
                "public_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Base64-encoded X25519 public key (32 bytes)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def ensure_encryption_keypair(
        self, *, call_context: CallContext | None = None,
    ) -> ActionResult:
        """Bootstrap the X25519 identity keypair if absent. Idempotent."""
        pass

    @service_interface_process(
        name="get_public_key",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="Local X25519 public key",
            type=ParameterType.OBJECT,
            properties={
                "public_key": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Base64-encoded X25519 public key (32 bytes)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def get_public_key(
        self, *, call_context: CallContext | None = None,
    ) -> ActionResult:
        """Return the local X25519 public key for sealed-box transfer. Lazy-inits if absent."""
        pass

    @service_interface_process(
        name="export_encrypted",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "secret_name": ParameterMetadata(
                description="The secret name to export from the local vault",
                required=True,
                type=ParameterType.STRING,
            ),
            "recipient_pubkey": ParameterMetadata(
                description="Base64-encoded X25519 public key of the recipient",
                required=True,
                type=ParameterType.STRING,
            ),
            "recipient_identifier": ParameterMetadata(
                description="Free-text peer label for the audit log (e.g. solet name)",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Sealed-box ciphertext + one-way plaintext fingerprint",
            type=ParameterType.OBJECT,
            properties={
                "ciphertext": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Base64-encoded sealed-box payload",
                ),
                "plaintext_fingerprint": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="sha256(plaintext) truncated; compare against import side",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def export_encrypted(
        self,
        secret_name: str,
        recipient_pubkey: str,
        recipient_identifier: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Export a vault secret sealed for a recipient's public key (libsodium sealed-box)."""
        pass

    @service_interface_process(
        name="import_encrypted",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "name": ParameterMetadata(
                description="Destination secret name in the local vault",
                required=True,
                type=ParameterType.STRING,
            ),
            "ciphertext": ParameterMetadata(
                description="Base64-encoded sealed-box payload from the sender's export",
                required=True,
                type=ParameterType.STRING,
            ),
            "sender_identifier": ParameterMetadata(
                description="Free-text peer label for the audit log (e.g. solet name)",
                required=False,
                type=ParameterType.STRING,
            ),
            "overwrite": ParameterMetadata(
                description="If True, replace an existing secret of the same name (default False)",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Import confirmation + one-way plaintext fingerprint",
            type=ParameterType.OBJECT,
            properties={
                "ok": ParameterMetadata(
                    type=ParameterType.BOOLEAN, description="True on success"
                ),
                "plaintext_fingerprint": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="sha256(plaintext) truncated; must match the export side",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def import_encrypted(
        self,
        name: str,
        ciphertext: str,
        sender_identifier: str | None = None,
        overwrite: bool = False,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Import a sealed-box ciphertext as a vault secret. Plaintext never crosses the boundary."""
        pass

    @service_interface_process(
        name="oauth_client_register",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "client_name": ParameterMetadata(
                description="Human label for the new OAuth client",
                required=True,
                type=ParameterType.STRING,
            ),
            "scopes": ParameterMetadata(
                description="OAuth scopes (default ['mcp:read', 'mcp:write'])",
                required=False,
                type=ParameterType.LIST,
                default=["mcp:read", "mcp:write"],
            ),
            "redirect_uris": ParameterMetadata(
                description=(
                    "Pre-registered redirect URIs for the "
                    "authorization_code grant.  Exact-matched at "
                    "/authorize.  Required (non-empty) for the "
                    "claude.ai connector flow; empty list is only "
                    "valid for clients whose grant_types is "
                    "['client_credentials']."
                ),
                required=False,
                type=ParameterType.LIST,
                default=[],
            ),
            "grant_types": ParameterMetadata(
                description=(
                    "OAuth grant types this client is authorized to "
                    "use. Allowlist: authorization_code, "
                    "client_credentials, refresh_token. Default is "
                    "['authorization_code', 'refresh_token'] — the "
                    "standard claude.ai connector shape. Include "
                    "'client_credentials' only for machine-to-machine "
                    "clients that do not go through the browser auth "
                    "flow. Any value not in the allowlist is rejected."
                ),
                required=False,
                type=ParameterType.LIST,
                default=["authorization_code", "refresh_token"],
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "OAuth client registration result; client_secret is returned "
                "ONCE and never recoverable afterwards. Every client minted "
                "via this process is automatically operator_approved=True — "
                "the MCP OAuth transport refuses to issue tokens for "
                "clients without that flag."
            ),
            type=ParameterType.OBJECT,
            properties={
                "client_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Public OAuth client identifier",
                ),
                "client_secret": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Cleartext client secret (returned once)",
                ),
                "client_name": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Human label echoed from the request",
                ),
                "scopes": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="OAuth scopes granted to this client",
                ),
                "redirect_uris": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Pre-registered redirect URIs for the client",
                ),
                "grant_types": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Grant types this client is authorized to use."
                    ),
                ),
                "operator_approved": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description=(
                        "Always True for clients minted via this "
                        "operator-only platform process."
                    ),
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def oauth_client_register(
        self,
        client_name: str,
        scopes: builtins.list[str] | None = None,
        redirect_uris: builtins.list[str] | None = None,
        grant_types: builtins.list[str] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Register a new operator-approved OAuth 2.1 client for the streamable HTTP MCP transport.

        Dynamic Client Registration is disabled at /register (Task #31);
        this platform process is the only path to introduce a usable
        OAuth client. Defaults to the claude.ai connector shape
        (authorization_code + refresh_token); pass
        ``grant_types=["client_credentials"]`` for machine clients.
        """
        pass

    @service_interface_process(
        name="oauth_client_revoke",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "client_id": ParameterMetadata(
                description="Public OAuth client identifier to revoke",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Revocation result",
            type=ParameterType.OBJECT,
            properties={
                "client_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The revoked client identifier",
                ),
                "removed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Number of registry rows removed (0 if absent)",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def oauth_client_revoke(
        self,
        client_id: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Revoke a previously-registered OAuth client; idempotent."""
        pass

    @service_interface_process(
        name="oauth_client_list",
        is_discoverable=True,
        provider="vault_service",
        parameters={},
        return_value_schema=ReturnValueSchema(
            description="List of registered OAuth clients (public metadata only)",
            type=ParameterType.OBJECT,
            properties={
                "clients": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "List of {client_id, client_name, scopes, "
                        "created_at} entries sorted by created_at"
                    ),
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def oauth_client_list(
        self, *, call_context: CallContext | None = None,
    ) -> ActionResult:
        """List registered OAuth clients (public metadata only; never the secret)."""
        pass

    @service_interface_process(
        name="oauth_client_add_redirect_uri",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "client_id": ParameterMetadata(
                description="Existing OAuth client identifier",
                required=True,
                type=ParameterType.STRING,
            ),
            "redirect_uri": ParameterMetadata(
                description=(
                    "Absolute redirect URI to register for the client; "
                    "exact-matched at /authorize"
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description=(
                "Confirmation; surfaces the full redirect_uris list "
                "after the add and an idempotency flag"
            ),
            type=ParameterType.OBJECT,
            properties={
                "client_id": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="The OAuth client identifier",
                ),
                "redirect_uris": ParameterMetadata(
                    type=ParameterType.LIST,
                    description="Full redirect_uris list after the add",
                ),
                "added": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                    description="True if the URI was newly added; False if already present",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(),
        requires_call_context=True,
    )
    @abstractmethod
    def oauth_client_add_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Append a redirect URI to an existing OAuth client (idempotent)."""
        pass

    # ─────────────────────────────────────────────────────────────────────────
    # Vault Admin Operations (operator-only)
    #
    # W-VAULT-CALLER-ENFORCE (P0 Tier 2 sub-2): promoted from
    # plugin-process scope to service-interface scope so the master plan
    # §1.3 invariant holds — `service_interface::vault_service::*` is the
    # ONLY entry surface for the vault. The concrete vault plugin enforces
    # operator-only access via `@requires_operator_principal`; the proxy's
    # `OPERATOR_ONLY_METHODS` constant maintains the lockstep list.
    # ─────────────────────────────────────────────────────────────────────────

    @service_interface_process(
        name="vault_init",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "passphrase": ParameterMetadata(
                description="Operator passphrase for wrapping the master key",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Initialization result",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status message",
                ),
                "wrapped_key_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Path to the persisted wrapped master key",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def vault_init(
        self,
        passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Initialize the vault by generating + wrapping the master key.

        Operator-only. One-time per vault. Required before any
        store / retrieve / unlock call.
        """
        pass

    @service_interface_process(
        name="vault_create_recovery",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "recovery_passphrase": ParameterMetadata(
                description=(
                    "Operator passphrase that wraps a second copy of "
                    "the master key. Record this independently from the "
                    "primary passphrase."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Recovery-key creation result",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Status message",
                ),
                "recovery_key_path": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Path to the persisted recovery-wrapped master key",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def vault_create_recovery(
        self,
        recovery_passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Wrap the master key under a second passphrase for recovery.

        Operator-only. Vault must be unlocked first.
        """
        pass

    @service_interface_process(
        name="vault_rotate_passphrase",
        is_discoverable=True,
        provider="vault_service",
        parameters={
            "old_passphrase": ParameterMetadata(
                description="Current vault passphrase",
                required=True,
                type=ParameterType.STRING,
            ),
            "new_passphrase": ParameterMetadata(
                description="New vault passphrase",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        return_value_schema=ReturnValueSchema(
            description="Rotation result",
            type=ParameterType.OBJECT,
            properties={
                "message": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="Confirmation message",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
        ),
        requires_call_context=True,
    )
    @abstractmethod
    def vault_rotate_passphrase(
        self,
        old_passphrase: str,
        new_passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rotate the vault passphrase without re-encrypting stored secrets.

        Operator-only. Re-derives the KEK from the new passphrase and
        re-wraps the unchanged master key.
        """
        pass
