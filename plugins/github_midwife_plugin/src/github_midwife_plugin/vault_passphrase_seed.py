"""Finding F10 (2026-07-11) — first-run vault passphrase file seed (Layer 1).

Verb-mode genesis structurally skipped the README ladder's vault-passphrase
provisioning step, so the newborn's `macos_vault_plugin` crash-looped at boot
with "vault not initialized and no passphrase available" (runbook §11 F10).

The vault's own unattended-first-boot path (`macos_vault_plugin.plugin`
around lines 434-444; `macos_vault_plugin.key_manager` line 136) resolves a
passphrase from, in order: an explicit argument, the
`<SOLET_NAME>_VAULT_PASSPHRASE` env var, or the FILE
`<app_home>/config/plugins/macos_vault_plugin/passphrase`. When the vault is
not yet initialized and a passphrase is resolvable, `key_manager.initialize`
runs unattended. Genesis therefore generates a high-entropy passphrase and
writes it to that file (resolution source #3) so the newborn boots with zero
passphrase intake.

This also moots the F10-rider: the env-var route
(`<SOLET_NAME>_VAULT_PASSPHRASE`) is unusable for any HYPHENATED newborn
name — `export FERN-FRESH-FORGE_VAULT_PASSPHRASE=...` fails ("not a valid
identifier"). The FILE route has no such constraint, so it is the only
universally-correct mechanism and the one genesis uses.

Hygiene bar (identical to `credential_seed`): the passphrase value is NEVER
printed, logged, embedded in an exception, or returned. `seed_vault_passphrase`
returns only a bool (created vs already-present).

IDEMPOTENT — write-if-absent, NEVER regenerate (Architect ruling, 2026-07-11):
the passphrase is the SOLE key to whatever the vault has already encrypted;
overwriting an existing passphrase file after the vault initialized would
orphan every secret sealed under it (unrecoverable, nothing can re-derive it).
If the file already exists this is a no-op — genesis never rewrites it.
"""

from __future__ import annotations

import os
from pathlib import Path
from secrets import token_urlsafe

_VAULT_PLUGIN_NAME = "macos_vault_plugin"
_PASSPHRASE_FILENAME = "passphrase"
_STALE_CHECK_PENDING_FILENAME = "passphrase.stale-check-pending"
_PASSPHRASE_TOKEN_BYTES = 32
_PASSPHRASE_FILE_MODE = 0o600


def vault_passphrase_path(target: Path) -> Path:
    """The resolution-source-#3 passphrase file the vault reads at first boot:
    `<target>/profile/config/plugins/macos_vault_plugin/passphrase`.
    """
    return (
        target / "profile" / "config" / "plugins"
        / _VAULT_PLUGIN_NAME / _PASSPHRASE_FILENAME
    )


def vault_passphrase_stale_check_pending_path(target: Path) -> Path:
    """Return the non-secret provenance marker for a Genesis-seeded passphrase.

    The marker exists only between creation of a passphrase by Genesis and a
    successful stale-Keychain master-key check.  It makes an interrupted or
    refused first attempt distinguishable from a passphrase that pre-dated
    Genesis, without recording the passphrase itself.
    """
    return vault_passphrase_path(target).with_name(_STALE_CHECK_PENDING_FILENAME)


def vault_passphrase_stale_check_is_pending(target: Path) -> bool:
    """Whether a Genesis-seeded passphrase still requires stale-key validation."""
    return vault_passphrase_stale_check_pending_path(target).is_file()


def clear_vault_passphrase_stale_check_pending(target: Path) -> None:
    """Clear the marker only after the stale-Keychain check succeeds."""
    try:
        vault_passphrase_stale_check_pending_path(target).unlink()
    except FileNotFoundError:
        return


def seed_vault_passphrase(target: Path) -> bool:
    """Write a fresh high-entropy vault passphrase to the newborn's passphrase
    file (mode 0600), IFF one does not already exist.

    Returns `True` if a passphrase file was created, `False` if one already
    existed (idempotent no-op — NEVER overwrites; see the module docstring's
    write-if-absent rationale). The generated value is never returned,
    printed, logged, or embedded in an exception.
    """
    path = vault_passphrase_path(target)
    if path.exists():
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    # Record provenance before creating the passphrase.  A crash between these
    # operations is conservative: the next attempt still performs the stale
    # Keychain check rather than silently accepting an unvalidated seed.
    pending_path = vault_passphrase_stale_check_pending_path(target)
    try:
        pending_fd = os.open(
            pending_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            _PASSPHRASE_FILE_MODE,
        )
    except FileExistsError:
        pass
    else:
        os.close(pending_fd)
    # Bound as a module-level name (not `secrets.token_urlsafe`) so a test can
    # patch it independently of credential_seed's own `secrets.token_urlsafe`
    # (both would otherwise resolve to the same shared secrets-module attr).
    passphrase = token_urlsafe(_PASSPHRASE_TOKEN_BYTES)
    # O_EXCL creates the file with mode 0o600 atomically and fails loud if it
    # appeared between the exists() probe and now (never clobber).
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, _PASSPHRASE_FILE_MODE)
    try:
        os.write(fd, passphrase.encode("utf-8"))
    finally:
        os.close(fd)
    # Defensive: enforce 0o600 regardless of the process umask.
    path.chmod(_PASSPHRASE_FILE_MODE)
    return True


__all__ = [
    "clear_vault_passphrase_stale_check_pending",
    "seed_vault_passphrase",
    "vault_passphrase_path",
    "vault_passphrase_stale_check_is_pending",
    "vault_passphrase_stale_check_pending_path",
]
