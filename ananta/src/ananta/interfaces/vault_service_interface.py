"""Vault Service Interface - Secure credential storage."""

import builtins
from abc import ABC, abstractmethod
from typing import ClassVar

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext


class VaultServiceInterface(ABC):
    """Secure credential storage. Encrypts at rest. Never exposes plaintext in logs/errors.

    Plugins implementing this interface should:
    1. Define service_interfaces property returning tuple containing VaultServiceInterface
    2. Define supported_interface_versions property with version mapping
    3. Encrypt all secrets at rest using strong encryption (AES-256-GCM recommended)
    4. NEVER log or expose plaintext secret values
    5. Return ActionResult TypedDict from all operations

    W-VAULT-INTERFACE-EXTEND (P0 Tier 1, 2026-06-07): every method accepts
    `call_context: CallContext | None = None` as a keyword-only parameter.
    Phase E lands the SIGNATURES + WIRING; enforcement (namespace ownership
    + operator-only gating) activates at Tier 2 W-VAULT-CALLER-ENFORCE.
    Caller-supplied `call_context` is dropped server-side (see
    VaultServiceProxy + ActionProcessor injection).
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

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
        """Store secret (encrypted). Returns key confirmation, never value.

        Args:
            key: Unique identifier for the secret
            value: The secret value (will be encrypted)
            tags: Optional tags for organization
            metadata: Optional metadata (NOT encrypted, do not put secrets here)
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with key confirmation
        """
        ...

    @abstractmethod
    def retrieve(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Get decrypted secret value.

        Args:
            key: The secret identifier
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with decrypted value in data field
        """
        ...

    @abstractmethod
    def delete(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Permanently delete secret.

        Args:
            key: The secret identifier
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult indicating success
        """
        ...

    @abstractmethod
    def list(
        self,
        tag: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """List secret keys only (never values).

        Args:
            tag: Optional tag filter
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with list of keys and metadata (no values)
        """
        ...

    @abstractmethod
    def exists(
        self,
        key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Check if secret exists without retrieving.

        Args:
            key: The secret identifier
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with exists: bool
        """
        ...

    @abstractmethod
    def rotate(
        self,
        key: str,
        new_value: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Update secret value. Increments version.

        Args:
            key: The secret identifier
            new_value: New secret value
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with rotation confirmation and new version
        """
        ...

    @abstractmethod
    def rename(
        self,
        old_key: str,
        new_key: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Rename a secret server-side without exposing plaintext.

        Reads the value (and version/tags/metadata where the substrate
        supports them) under ``old_key``, writes the same value under
        ``new_key``, then deletes ``old_key``. The plaintext never
        leaves the vault process — callers see only key identifiers in
        the return envelope. This is the canonical mechanism for the
        flat-to-scoped key migrations during the state-service
        consolidation campaign (W-VAULT-MIGRATE / sub-1 launch-key
        migrations) because ``retrieve``+``store``+``delete`` round
        trips would leak plaintext through the MCP / coding-agent
        transcript.

        Operator-only: listed in
        ``VaultServiceProxy.OPERATOR_ONLY_METHODS``; per-method
        enforcement (``@requires_operator_principal``) activates in
        W-VAULT-CALLER-ENFORCE (sub-2). Sub-1 ships the verb dormant —
        any caller (including bound-plugin proxies) may invoke it
        during this commit's window so the live row migrations succeed
        before sub-2 lands.

        Args:
            old_key: Current secret identifier.
            new_key: Target secret identifier. Must not already exist.
            call_context: Server-built caller context
                (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with ``old_key``, ``new_key``, and a
            confirmation message. Never contains the value.
        """
        ...

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
        """Mint cryptographic random bytes inside the vault and store them.

        The plaintext never crosses the process boundary. Used by first-boot
        orchestrators that need to seed tokens (admin user token, transport
        bootstrap tokens) without those bytes touching any caller's memory.

        Args:
            key: Unique identifier for the new secret
            byte_length: Number of random bytes to mint (default 32 = 256 bits)
            tags: Optional tags for organization
            metadata: Optional metadata (NOT encrypted)
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with key, byte_length, and a one-way
            plaintext_fingerprint (sha256 truncated). Never the value.
        """
        ...

    @abstractmethod
    def status(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Get vault status including initialized state, lock state, and backend info.

        Args:
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with vault status information
        """
        ...

    @abstractmethod
    def unlock(
        self,
        passphrase: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Unlock the vault with passphrase.

        Args:
            passphrase: Vault passphrase
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with unlock status
        """
        ...

    @abstractmethod
    def lock(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Lock the vault - clears master key from memory.

        Args:
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with lock status
        """
        ...

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
        """Register a new operator-approved OAuth 2.1 client.

        Mints ``(client_id, client_secret)``, stores an scrypt hash of
        the secret in the OAuth client registry, sets
        ``operator_approved=True`` on the row, and returns the
        cleartext ``client_secret`` ONCE in the response (never
        recoverable after registration).  Dynamic Client Registration
        is disabled at /register (Task #31); this is the only path to
        introduce a usable OAuth client.

        Args:
            client_name: Human label (e.g. ``"claude-ai"``).
            scopes: OAuth scopes granted to this client.  Defaults to
                ``["mcp:read", "mcp:write"]``.
            redirect_uris: Pre-registered redirect URIs for the
                authorization_code grant.  Each URI is exact-matched
                at /authorize before the redirect is honoured.
                Required for the claude.ai connector flow; defaults to
                ``[]`` (only valid for ``client_credentials``-only
                clients).
            grant_types: OAuth grant types this client is authorized
                to use. Allowlist:
                ``{authorization_code, client_credentials, refresh_token}``.
                Defaults to ``["authorization_code", "refresh_token"]``
                — the standard claude.ai connector shape. Include
                ``"client_credentials"`` only for machine-to-machine
                clients. Any value outside the allowlist is rejected.
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with data containing client_id, client_secret
            (cleartext, one-time), client_name, scopes, redirect_uris,
            grant_types, and operator_approved=True.
        """
        ...

    @abstractmethod
    def oauth_client_revoke(
        self,
        client_id: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Revoke a previously-registered OAuth client.

        Idempotent: ``removed=0`` when the client_id is already absent.

        Args:
            client_id: Public OAuth client identifier.
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with data containing client_id and removed count.
        """
        ...

    @abstractmethod
    def oauth_client_list(
        self,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """List registered OAuth clients (public metadata only).

        Never surfaces the secret or its hash — only ``client_id``,
        ``client_name``, ``scopes``, ``redirect_uris``, and
        ``created_at`` per row.

        Args:
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with data.clients = list of client metadata
            entries, sorted by created_at.
        """
        ...

    @abstractmethod
    def oauth_client_add_redirect_uri(
        self,
        client_id: str,
        redirect_uri: str,
        *,
        call_context: CallContext | None = None,
    ) -> ActionResult:
        """Append a redirect URI to an existing OAuth client.

        Idempotent: passing an already-registered URI returns the
        existing list with ``added=False``.  Used to pre-register
        callback URLs for the authorization_code grant.

        Args:
            client_id: Existing OAuth client identifier.
            redirect_uri: Absolute URI to add to the client's
                redirect-URI allow-list.
            call_context: Server-built caller context (see W-VAULT-INTERFACE-EXTEND).

        Returns:
            ActionResult with data containing client_id, the full
            redirect_uris list after the add, and an added boolean.
        """
        ...
