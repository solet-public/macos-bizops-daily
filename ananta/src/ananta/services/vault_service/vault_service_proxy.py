"""VaultServiceProxy — caller-bound facade over the vault plugin.

W-VAULT-INTERFACE-EXTEND Phase C (P0 Tier 1, state-service consolidation
campaign): each plugin that consumed `get_service("vault_service")` now
receives a per-plugin proxy instance via lifecycle injection. The proxy
pre-binds the caller plugin name into a server-built `CallContext` at
construction time, then forwards every call to the underlying vault
service WITH that `call_context` applied.

The proxy is the BINDING mechanism. Enforcement (namespace ownership +
operator-only method gating) lives in the concrete vault service
implementation — see master plan §3.3.4 Tier 2 W-VAULT-CALLER-ENFORCE.
Tier 1 lands the binding seam, the CallContext wiring, and the
spoofing-resistance: caller-supplied `call_context` arguments are
dropped on the floor (the proxy always uses its bound context).

Per the dispatch isolation rules:

* Explicit per-method dispatch across THREE layers — Layer 1 (runtime
  `VaultServiceInterface`), Layer 2 (public `VaultServiceAPI`), Layer 3
  (transitional in-process OAuth helpers). NO raw ``__getattr__``
  passthrough for secret-bearing methods — the surface is enumerated
  by hand so the parity smoke catches any drift.
* The ONLY attribute passthrough is ``_oauth_registry`` (Layer 3 marker
  for `agent_messaging_plugin._maybe_get_vault_oauth_registry`). This
  is the migration exception with removal target W-OAUTH-EXTRACT
  (Tier 4); annotated below.
* Backward-compatible signatures: each proxy method matches the
  underlying interface signature 1:1 EXCEPT the proxy never accepts
  a `call_context` kwarg from the caller — the bound context is
  always used. ActionProcessor-injected `call_context` flows by a
  separate path (queued service-interface action; ActionProcessor
  builds + injects directly into the impl).

Reference:
- `ananta/src/ananta/core/services/call_context.py` (foundation).
- the state-service consolidation master plan §3.3 (dev-checkout
  workbench — not part of the shipped tree).
- the tier-1 dispatch plan for Codex signoff v2 §1.B, binding mechanism
  (dev-checkout workbench — not part of the shipped tree).
"""

from __future__ import annotations

import builtins
from typing import TYPE_CHECKING, Any

from ananta.core.domain.types import ActionResult
from ananta.core.services.call_context import CallContext

if TYPE_CHECKING:  # pragma: no cover — typing only
    from ananta.interfaces.vault_service_interface import VaultServiceInterface


# Methods that MUST be present on the underlying vault service for the
# proxy to be fully functional. Used by the parity smoke; the smoke fails
# if any of these surfaces is missing from the bound vault — drift
# protection. The Layer 3 helpers are concrete-impl-only (not in
# `VaultServiceInterface` or `VaultServiceAPI`), so they're flagged
# explicitly here for the smoke's per-layer enumeration.
_LAYER_1_METHODS: frozenset[str] = frozenset({
    "store", "retrieve", "delete", "list", "exists", "rotate", "rename",
    "store_random", "status", "unlock", "lock",
    "oauth_client_register", "oauth_client_revoke",
    "oauth_client_list", "oauth_client_add_redirect_uri",
})

_LAYER_2_ONLY_METHODS: frozenset[str] = frozenset({
    "store_from_env", "store_from_file", "store_from_kv_file",
    "store_from_keychain",
    "ensure_encryption_keypair", "get_public_key",
    "export_encrypted", "import_encrypted",
    # W-VAULT-CALLER-ENFORCE (sub-2) — admin verbs promoted from
    # plugin-process scope to service-interface scope.
    "vault_init", "vault_create_recovery", "vault_rotate_passphrase",
})

_LAYER_3_HELPERS: frozenset[str] = frozenset({
    "lookup_oauth_client", "verify_oauth_client_credentials",
    "issue_oauth_refresh_token", "consume_oauth_refresh_token",
})

# Methods that admin/operator principal kinds must be allowed to call;
# enforcement is Tier 2 (W-VAULT-CALLER-ENFORCE). Listed here so the
# parity smoke + spoofing-negative smoke know which surfaces to assert
# on. Maintain in lockstep with `OPERATOR_ONLY_METHODS` in the
# concrete vault plugins.
OPERATOR_ONLY_METHODS: frozenset[str] = frozenset({
    "unlock", "lock",
    "store_from_env", "store_from_file", "store_from_kv_file", "store_from_keychain",
    "ensure_encryption_keypair", "get_public_key",
    "export_encrypted", "import_encrypted",
    "oauth_client_register", "oauth_client_revoke", "oauth_client_list",
    "oauth_client_add_redirect_uri",
    "rename",
    # W-VAULT-CALLER-ENFORCE (sub-2) — admin-only verbs promoted from
    # plugin-process scope to service-interface scope (master plan §1.3:
    # `service_interface::vault_service::*` is the ONLY entry surface).
    "vault_init", "vault_create_recovery", "vault_rotate_passphrase",
})


class VaultServiceProxy:
    """Caller-bound proxy over a vault service implementation.

    Each consumer plugin gets its OWN proxy instance, constructed by the
    lifecycle injection seam (`_inject_vault_service` in
    `startup_sequence.py`) with `caller_plugin` baked in. The proxy:

    1. Stores a strong reference to the raw vault service plugin.
    2. Constructs the bound CallContext once at __init__ time via
       `CallContext.for_plugin(caller_plugin)`.
    3. Forwards each call to the underlying service with the bound
       context as `call_context` kwarg.
    4. Drops any caller-supplied `call_context` value — the binding is
       server-side authoritative.

    The transitional `_oauth_registry` attribute is the ONLY attribute
    passthrough; agent_messaging consumes it for OAuth refresh-token
    rotation. Removal target: W-OAUTH-EXTRACT (Tier 4).
    """

    def __init__(
        self,
        vault_service: VaultServiceInterface,
        caller_plugin: str,
    ) -> None:
        if not caller_plugin:
            raise ValueError(
                "VaultServiceProxy requires a non-empty caller_plugin name; "
                "the binding mechanism cannot be deferred to call time",
            )
        self._vault_service = vault_service
        self._caller_plugin = caller_plugin
        self._call_context = CallContext.for_plugin(caller_plugin)

    @property
    def caller_plugin(self) -> str:
        """The plugin name this proxy is bound to (read-only)."""
        return self._caller_plugin

    @property
    def call_context(self) -> CallContext:
        """The bound CallContext (read-only)."""
        return self._call_context

    # ─────────────────────────────────────────────────────────────────────
    # Layer 1 — VaultServiceInterface (runtime ABC)
    # ─────────────────────────────────────────────────────────────────────

    def store(
        self,
        key: str,
        value: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store(
            key, value, tags, metadata, call_context=self._call_context,
        )

    def retrieve(self, key: str) -> ActionResult:
        return self._vault_service.retrieve(key, call_context=self._call_context)

    def delete(self, key: str) -> ActionResult:
        return self._vault_service.delete(key, call_context=self._call_context)

    def list(self, tag: str | None = None) -> ActionResult:
        return self._vault_service.list(tag, call_context=self._call_context)

    def exists(self, key: str) -> ActionResult:
        return self._vault_service.exists(key, call_context=self._call_context)

    def rotate(self, key: str, new_value: str) -> ActionResult:
        return self._vault_service.rotate(
            key, new_value, call_context=self._call_context,
        )

    def rename(self, old_key: str, new_key: str) -> ActionResult:
        return self._vault_service.rename(
            old_key, new_key, call_context=self._call_context,
        )

    def store_random(
        self,
        key: str,
        byte_length: int = 32,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store_random(
            key, byte_length, tags, metadata, call_context=self._call_context,
        )

    def status(self) -> ActionResult:
        return self._vault_service.status(call_context=self._call_context)

    def unlock(self, passphrase: str) -> ActionResult:
        return self._vault_service.unlock(
            passphrase, call_context=self._call_context,
        )

    def lock(self) -> ActionResult:
        return self._vault_service.lock(call_context=self._call_context)

    def oauth_client_register(
        self,
        client_name: str,
        scopes: builtins.list[str] | None = None,
        redirect_uris: builtins.list[str] | None = None,
        grant_types: builtins.list[str] | None = None,
    ) -> ActionResult:
        return self._vault_service.oauth_client_register(
            client_name, scopes, redirect_uris, grant_types,
            call_context=self._call_context,
        )

    def oauth_client_revoke(self, client_id: str) -> ActionResult:
        return self._vault_service.oauth_client_revoke(
            client_id, call_context=self._call_context,
        )

    def oauth_client_list(self) -> ActionResult:
        return self._vault_service.oauth_client_list(
            call_context=self._call_context,
        )

    def oauth_client_add_redirect_uri(
        self, client_id: str, redirect_uri: str,
    ) -> ActionResult:
        return self._vault_service.oauth_client_add_redirect_uri(
            client_id, redirect_uri, call_context=self._call_context,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Layer 2 — VaultServiceAPI public surface
    # ─────────────────────────────────────────────────────────────────────

    def store_from_env(
        self,
        key: str,
        env_var: str,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store_from_env(
            key, env_var, tags, metadata, call_context=self._call_context,
        )

    def store_from_file(
        self,
        key: str,
        file_path: str,
        strip: bool = True,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store_from_file(
            key, file_path, strip, tags, metadata,
            call_context=self._call_context,
        )

    def store_from_kv_file(
        self,
        key: str,
        file_path: str,
        field: str,
        format: str | None = None,
        strip: bool = True,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store_from_kv_file(
            key, file_path, field, format, strip, tags, metadata,
            call_context=self._call_context,
        )

    def store_from_keychain(
        self,
        key: str,
        service: str,
        account: str | None = None,
        keychain: str | None = None,
        tags: builtins.list[str] | None = None,
        metadata: dict[str, str] | None = None,
    ) -> ActionResult:
        return self._vault_service.store_from_keychain(
            key, service, account, keychain, tags, metadata,
            call_context=self._call_context,
        )

    def ensure_encryption_keypair(self) -> ActionResult:
        return self._vault_service.ensure_encryption_keypair(
            call_context=self._call_context,
        )

    def get_public_key(self) -> ActionResult:
        return self._vault_service.get_public_key(
            call_context=self._call_context,
        )

    def export_encrypted(
        self,
        secret_name: str,
        recipient_pubkey: str,
        recipient_identifier: str | None = None,
    ) -> ActionResult:
        return self._vault_service.export_encrypted(
            secret_name, recipient_pubkey, recipient_identifier,
            call_context=self._call_context,
        )

    def import_encrypted(
        self,
        name: str,
        ciphertext: str,
        sender_identifier: str | None = None,
        overwrite: bool = False,
    ) -> ActionResult:
        return self._vault_service.import_encrypted(
            name, ciphertext, sender_identifier, overwrite,
            call_context=self._call_context,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Layer 2 — Vault admin (operator-only; promoted from plugin-process
    # scope by W-VAULT-CALLER-ENFORCE sub-2). The proxy still forwards the
    # bound caller context; the concrete impl rejects non-operator
    # principals via @requires_operator_principal.
    # ─────────────────────────────────────────────────────────────────────

    def vault_init(self, passphrase: str) -> ActionResult:
        return self._vault_service.vault_init(
            passphrase, call_context=self._call_context,
        )

    def vault_create_recovery(self, recovery_passphrase: str) -> ActionResult:
        return self._vault_service.vault_create_recovery(
            recovery_passphrase, call_context=self._call_context,
        )

    def vault_rotate_passphrase(
        self, old_passphrase: str, new_passphrase: str,
    ) -> ActionResult:
        return self._vault_service.vault_rotate_passphrase(
            old_passphrase, new_passphrase, call_context=self._call_context,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Layer 3 — Transitional in-process OAuth helpers
    #
    # Removal target: W-OAUTH-EXTRACT (Tier 4). Until then, agent_messaging
    # and the OAuth bridge code need these direct-call surfaces.
    # ─────────────────────────────────────────────────────────────────────

    def lookup_oauth_client(self, client_id: str) -> dict[str, Any] | None:
        return self._vault_service.lookup_oauth_client(
            client_id, call_context=self._call_context,
        )

    def verify_oauth_client_credentials(
        self, client_id: str, client_secret: str,
    ) -> dict[str, Any] | None:
        return self._vault_service.verify_oauth_client_credentials(
            client_id, client_secret, call_context=self._call_context,
        )

    def issue_oauth_refresh_token(
        self,
        *,
        client_id: str,
        scopes: builtins.list[str],
        audience: str,
        ttl_seconds: int,
    ) -> str:
        return self._vault_service.issue_oauth_refresh_token(
            client_id=client_id,
            scopes=scopes,
            audience=audience,
            ttl_seconds=ttl_seconds,
            call_context=self._call_context,
        )

    def consume_oauth_refresh_token(
        self, cleartext: str,
    ) -> dict[str, Any] | None:
        return self._vault_service.consume_oauth_refresh_token(
            cleartext, call_context=self._call_context,
        )

    # ─────────────────────────────────────────────────────────────────────
    # Transitional attribute passthrough — MIGRATION EXCEPTION
    #
    # `_oauth_registry` access is the ONLY attribute passthrough this proxy
    # allows. Used by:
    #   plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py:1934
    #   plugins/agent_messaging_plugin/src/agent_messaging_plugin/plugin.py:2297
    # for `_maybe_get_vault_oauth_registry` style direct-registry access.
    #
    # Removal target: W-OAUTH-EXTRACT (Tier 4) — extract the OAuth registry
    # into its own service interface; agent_messaging consumes the
    # service-interface methods rather than reaching into vault's private
    # state.
    # ─────────────────────────────────────────────────────────────────────

    @property
    def _oauth_registry(self) -> Any | None:
        """Transitional pass-through to the vault plugin's OAuth registry.

        See migration note at the top of this section. Returns None when
        the underlying vault service does not expose the registry (e.g.,
        a mock vault used in smoke tests).
        """
        return getattr(self._vault_service, "_oauth_registry", None)


__all__ = [
    "OPERATOR_ONLY_METHODS",
    "VaultServiceProxy",
    "_LAYER_1_METHODS",
    "_LAYER_2_ONLY_METHODS",
    "_LAYER_3_HELPERS",
]
