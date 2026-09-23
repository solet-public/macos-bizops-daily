"""VaultKeysProvider — plugin capability for declaring vault keys.

W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, state-service consolidation campaign):
plugins that consume vault declare which scoped keys they require at
readiness AND which scoped keys they read or write at runtime. Two
enforcement layers consume the declarations:

- **Runtime readiness gate** (`startup_sequence._check_vault_keys_for_plugin`):
  iterates ``get_required_vault_keys()`` via the plugin's caller-bound
  ``VaultServiceProxy`` and calls ``exists()`` on each. Missing keys
  surface as ``MissingVaultKeyError``. The gate ships in WARN mode at
  sub-1 landing (logs each
  missing key but does not raise); the FAIL mode flip lands in sub-2
  (W-VAULT-CALLER-ENFORCE) alongside the namespace + operator-only
  enforcement activation.

- **Static gate** (W-INT Cycle 2 ``wint2_vault_key_declaration_check``):
  AST-walks plugin source for ``self._vault.<verb>(KEY, ...)`` and
  ``vault_service.<verb>(KEY, ...)`` call sites, resolves ``Final[str]``
  constants, and accepts any key matching a declared literal or a
  declared prefix pattern (terminated in ``*``). Per Codex correction #5
  the gate also accepts ``# vault-key: <declared-key-or-prefix>`` line
  annotations for genuinely dynamic call sites and a small allowlist for
  address-book-chain consumers.

Required vs declared:

- ``get_required_vault_keys()`` returns ONLY keys whose absence makes
  the plugin unloadable. Conservative interpretation: empty for plugins
  whose vault usage is gated per-action rather than at startup. Lazy-
  created keys (the plugin writes them on first use) MUST NOT be in
  this list — they don't exist at readiness time.

- ``get_declared_vault_keys()`` returns EVERY scoped key the plugin
  reads or writes, including required, lazy-created, and runtime-
  computed prefix patterns. Prefix patterns terminate in ``*`` (e.g.
  ``"<solet>.soundcloud_artist_studio_plugin.refresh_token__*"``). The
  static gate accepts any literal that matches a declared prefix.

The protocol is structurally typed (runtime-checkable Protocol) so
plugins opt in by simply defining the methods — no ABC subclassing
required. Per Codex judgment #1, the readiness-gate orchestrator uses
``hasattr``-style platform-loader fallback rather than relying on a
Protocol default body: if ``get_declared_vault_keys`` is absent, the
gate substitutes ``get_required_vault_keys()`` as the declared set.

"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class VaultKeysProvider(Protocol):
    """Plugin capability for declaring required + statically-used vault keys."""

    def get_required_vault_keys(self) -> list[str]:
        """Scoped keys whose EXISTENCE is checked at readiness.

        Missing key → ``MissingVaultKeyError`` → plugin doesn't load
        (FAIL mode) or warning is logged + plugin loads (WARN mode).

        Excludes lazy-create keys (plugin will create at first use) and
        runtime-computed per-tenant keys (their identity is not knowable
        at startup).

        Returned form: fully-scoped ``<solet>.<plugin>.<credential>``
        derived from the plugin's own runtime constants (NOT hardcoded
        ``"<solet>."``).
        """
        ...

    def get_declared_vault_keys(self) -> list[str]:
        """All scoped vault keys the plugin reads or writes.

        Includes readiness-required keys + lazy-created keys + runtime-
        computed prefix patterns. Prefix patterns terminate in ``*``
        (e.g. ``"<solet>.<plugin>.refresh_token__*"``); the static gate
        accepts any literal matching a declared prefix.

        Used by the W-INT Cycle 2 vault-key-declaration gate.

        Platform-loader fallback (per Codex judgment #1): if the plugin
        does not implement this method, the loader substitutes
        ``get_required_vault_keys()`` as the declared set.
        """
        ...


class MissingVaultKeyError(Exception):
    """Plugin's declared required key is absent in vault at readiness.

    Raised by the readiness gate when running in FAIL mode (sub-2
    onwards). In WARN mode (sub-1 landing) the gate logs each missing
    key instead of raising.
    """

    def __init__(self, plugin_name: str, missing: list[str]) -> None:
        self.plugin_name = plugin_name
        self.missing = list(missing)
        super().__init__(
            f"plugin {plugin_name!r} missing required vault keys: {missing}",
        )


class VaultServiceUnavailableError(Exception):
    """Vault subsystem itself failed (not a missing-key case).

    Raised by the readiness gate when ``vault_service.exists()`` raises
    any error other than the missing-key path. Operationally distinct
    from ``MissingVaultKeyError`` so operators can tell apart "the key
    isn't there" from "the vault is broken."
    """


class MalformedVaultKeyDeclarationError(Exception):
    """Plugin declared a key that violates the scoped naming rules.

    Per master plan §3.3.1, vault keys are ``<solet>.<plugin>.<credential>``
    with the plugin segment exactly matching the declaring plugin's
    ``name``. Mismatches (wrong plugin segment, fewer than three
    segments, etc.) raise this at readiness so declaration bugs surface
    early.
    """


__all__ = [
    "MalformedVaultKeyDeclarationError",
    "MissingVaultKeyError",
    "VaultKeysProvider",
    "VaultServiceUnavailableError",
]
