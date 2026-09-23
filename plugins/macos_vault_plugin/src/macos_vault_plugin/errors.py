"""Vault plugin error types."""


class VaultError(Exception):
    """Base vault error - never includes secret values."""

    pass


class SecretNotFoundError(VaultError):
    """Secret key not found."""

    pass


class DecryptionError(VaultError):
    """Decryption failed - auth tag mismatch or corrupted data."""

    pass


class MasterKeyNotConfiguredError(VaultError):
    """Master key not available."""

    pass


class InvalidMasterKeyError(VaultError):
    """Master key is invalid (too short, wrong format, etc.)."""

    pass


class SecretExistsError(VaultError):
    """Secret with this key already exists."""

    pass


# Two-tier key management errors


class VaultNotInitializedError(VaultError):
    """Vault has not been initialized - no wrapped master key file."""

    pass


class VaultAlreadyInitializedError(VaultError):
    """Vault is already initialized - cannot reinitialize."""

    pass


class VaultLockedError(VaultError):
    """Vault is locked - call unlock() first."""

    pass


class InvalidPassphraseError(VaultError):
    """Passphrase is invalid - cannot unwrap master key."""

    pass


class PassphraseMismatchError(VaultError):
    """Passphrase confirmation does not match."""

    pass


# W-VAULT-LOCAL-KEYCHAIN Tier 3 startup-compat migrations (2026-06-07).
# Refusing fresh-mint on migration failure is load-bearing: a silent
# fresh-mint would rotate the solet's cross-solet sealed-box
# identity (keypair) or re-initialize the vault with a new master key
# (losing access to every encrypted secret in the state-service substrate).


class VaultKeypairMigrationError(VaultError):
    """Legacy keypair row(s) detected but migration to the new scoped name failed.

    Refusing to mint a fresh keypair so the operator can investigate
    without losing the solet's cross-solet identity.
    """

    pass


class VaultMasterKeyMigrationError(VaultError):
    """Legacy master-key file detected but move to the new plugin path failed.

    Refusing to re-initialize the vault so the operator can investigate
    without losing access to every encrypted secret.
    """

    pass
