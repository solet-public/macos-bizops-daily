"""Two-tier key management: passphrase -> KEK -> Master Key.

Security:
- Master Key is random 256-bit, never changes, provides maximum entropy
- User passphrase derives Key Encryption Key (KEK) via PBKDF2
- Master Key is wrapped (encrypted) with KEK, stored in OS keychain
- Passphrase rotation re-wraps Master Key without re-encrypting secrets
- Master Key only exists in memory while vault is unlocked

Storage:
- Primary: OS keychain (macOS Keychain, Linux secret-service)
- Fallback: File in $APP_HOME/config/plugins/macos_vault_plugin/
"""

import logging
import os
from typing import ClassVar

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .constants import (
    KEY_SIZE,
    NONCE_SIZE,
    PBKDF2_ITERATIONS,
    SALT_SIZE,
    passphrase_env_var,
)
from .crypto import VaultCrypto
from .errors import (
    InvalidPassphraseError,
    VaultAlreadyInitializedError,
    VaultLockedError,
    VaultNotInitializedError,
)
from .keychain import (
    MASTER_KEY_ACCOUNT,
    RECOVERY_KEY_ACCOUNT,
    get_backend_name,
    get_keychain,
)

logger = logging.getLogger(__name__)


class VaultKeyManager:
    """Manages two-tier key hierarchy: passphrase -> KEK -> Master Key.

    The Master Key is a random 256-bit key that never changes. It's wrapped
    (encrypted) with a Key Encryption Key (KEK) derived from the user's
    passphrase using PBKDF2.

    This allows:
    - Passphrase rotation without re-encrypting all secrets
    - Multiple unlock methods (primary passphrase, recovery passphrase)
    - Maximum entropy Master Key (not limited by human-memorable passphrase)

    Storage is handled by the OS keychain when available, with file fallback.
    """

    # Wrapped key format:
    # [salt: 32 bytes][nonce: 12 bytes][ciphertext+tag: 48 bytes]
    # Total: 92 bytes
    WRAPPED_KEY_SIZE: ClassVar[int] = SALT_SIZE + NONCE_SIZE + KEY_SIZE + 16

    def __init__(self) -> None:
        """Initialize key manager. Master key starts locked."""
        self._master_key: bytes | None = None

    def is_initialized(self) -> bool:
        """Check if vault has been initialized (wrapped key exists in keychain)."""
        keychain = get_keychain()
        return keychain.exists(MASTER_KEY_ACCOUNT)

    def is_unlocked(self) -> bool:
        """Check if vault is currently unlocked (master key in memory)."""
        return self._master_key is not None

    def initialize(self, passphrase: str) -> None:
        """First-time vault setup: generate Master Key, wrap with passphrase.

        Args:
            passphrase: User passphrase for wrapping the master key

        Raises:
            VaultAlreadyInitializedError: If vault is already initialized
            RuntimeError: When the active keychain is a direct-master-key
                store (e.g. AWS Secrets Manager). In that mode the
                provisioning layer owns secret creation; the plugin
                must not write.
        """
        keychain = get_keychain()
        if getattr(keychain, "direct_master_key", False):
            raise RuntimeError(
                f"{get_backend_name()} is a direct-master-key, read-only "
                "backend. Provisioning owns secret creation; the plugin must "
                "not call initialize() in this mode."
            )

        if self.is_initialized():
            raise VaultAlreadyInitializedError(
                f"Vault already initialized. Using {get_backend_name()}"
            )

        # Generate random 256-bit Master Key (maximum entropy)
        master_key = os.urandom(KEY_SIZE)

        # Wrap and store master key
        self._store_wrapped_key(MASTER_KEY_ACCOUNT, passphrase, master_key)

        # Keep master key in memory (vault is now unlocked)
        self._master_key = master_key

        logger.debug(f"Vault initialized using {get_backend_name()}")

    def unlock(self, passphrase: str | None = None) -> None:
        """Unlock vault: derive KEK from passphrase, unwrap Master Key.

        When the active keychain is a *direct-master-key* backend
        (Secrets Manager tenant-isolated path), the stored bytes ARE
        the 32-byte master key — no passphrase is required and the
        ``passphrase`` argument is ignored. Otherwise the legacy wrap
        path applies and the passphrase is resolved from arg / env /
        file as documented below.

        Args:
            passphrase: User passphrase. If None, reads from env var or
                passphrase file. Ignored entirely when the active
                keychain is direct-master-key.

        Passphrase resolution order (wrap-mode only):
            1. Explicit passphrase argument
            2. ``<SOLET_NAME>_VAULT_PASSPHRASE`` environment variable
               (e.g. ``EXAMPLE_VAULT_PASSPHRASE``)
            3. File: $APP_HOME/config/plugins/macos_vault_plugin/passphrase

        Raises:
            VaultNotInitializedError: If vault hasn't been initialized
            InvalidPassphraseError: If passphrase is wrong or not found
        """
        keychain = get_keychain()

        if getattr(keychain, "direct_master_key", False):
            # SecretsManagerKeychain: the stored bytes ARE the master
            # key. No KEK unwrap, no passphrase required.
            master_key = keychain.retrieve(MASTER_KEY_ACCOUNT)
            if master_key is None:
                raise VaultNotInitializedError(
                    f"Direct-master-key backend ({get_backend_name()}) returned "
                    "no master key — provisioning must pre-create the secret."
                )
            if len(master_key) != KEY_SIZE:
                raise VaultNotInitializedError(
                    f"Direct-master-key backend returned {len(master_key)}-byte "
                    f"payload; expected exactly {KEY_SIZE}."
                )
            self._master_key = master_key
            logger.debug("Vault unlocked (direct-master-key)")
            return

        if not self.is_initialized():
            raise VaultNotInitializedError("Vault not initialized. Run 'vaultctl init' first.")

        # Get passphrase from argument, environment, or file
        env_var = passphrase_env_var()
        if passphrase is None:
            passphrase = os.environ.get(env_var)

        if passphrase is None:
            # Try reading from passphrase file in plugin config directory
            passphrase = self._read_passphrase_file()

        if not passphrase:
            raise InvalidPassphraseError(
                f"No passphrase provided. Set {env_var} or create passphrase file."
            )

        # Read and unwrap master key
        self._master_key = self._retrieve_wrapped_key(MASTER_KEY_ACCOUNT, passphrase)

        logger.debug("Vault unlocked")

    def lock(self) -> None:
        """Lock vault: clear Master Key from memory."""
        self._master_key = None
        logger.debug("Vault locked")

    def rotate_passphrase(self, old_passphrase: str, new_passphrase: str) -> None:
        """Change passphrase without re-encrypting secrets.

        Args:
            old_passphrase: Current passphrase
            new_passphrase: New passphrase

        Raises:
            VaultNotInitializedError: If vault not initialized
            InvalidPassphraseError: If old passphrase is wrong
        """
        if not self.is_initialized():
            raise VaultNotInitializedError("Vault not initialized.")

        # Unwrap with old passphrase
        master_key = self._retrieve_wrapped_key(MASTER_KEY_ACCOUNT, old_passphrase)

        # Re-wrap with new passphrase (overwrites old)
        self._store_wrapped_key(MASTER_KEY_ACCOUNT, new_passphrase, master_key)

        # Update in-memory key if vault was unlocked
        self._master_key = master_key

        logger.debug("Passphrase rotated successfully")

    def create_recovery_key(self, recovery_passphrase: str) -> str:
        """Create a recovery key with a separate passphrase.

        The recovery key can be used to recover the vault if the primary
        passphrase is lost.

        Args:
            recovery_passphrase: Passphrase for the recovery key

        Returns:
            Description of where recovery key is stored

        Raises:
            VaultLockedError: If vault is locked
        """
        if not self._master_key:
            raise VaultLockedError("Vault must be unlocked to create recovery key.")

        self._store_wrapped_key(RECOVERY_KEY_ACCOUNT, recovery_passphrase, self._master_key)

        location = f"{get_backend_name()} ({RECOVERY_KEY_ACCOUNT})"
        logger.debug(f"Recovery key created: {location}")
        return location

    def recover_with_recovery_key(self, recovery_passphrase: str) -> None:
        """Recover vault using recovery passphrase.

        Args:
            recovery_passphrase: The recovery passphrase

        Raises:
            VaultNotInitializedError: If no recovery key exists
            InvalidPassphraseError: If recovery passphrase is wrong
        """
        keychain = get_keychain()
        if not keychain.exists(RECOVERY_KEY_ACCOUNT):
            raise VaultNotInitializedError("No recovery key found")

        self._master_key = self._retrieve_wrapped_key(RECOVERY_KEY_ACCOUNT, recovery_passphrase)
        logger.debug("Vault recovered using recovery key")

    def get_crypto(self) -> VaultCrypto:
        """Get VaultCrypto instance with unlocked Master Key.

        Returns:
            VaultCrypto configured with the Master Key

        Raises:
            VaultLockedError: If vault is locked
        """
        if not self._master_key:
            raise VaultLockedError("Vault is locked. Call unlock() with passphrase first.")
        return VaultCrypto(self._master_key)

    def get_status(self) -> dict[str, bool | str]:
        """Get vault status information.

        Returns:
            Dict with initialized, unlocked, backend, recovery_key_exists
        """
        keychain = get_keychain()
        return {
            "initialized": self.is_initialized(),
            "unlocked": self.is_unlocked(),
            "backend": get_backend_name(),
            "recovery_key_exists": keychain.exists(RECOVERY_KEY_ACCOUNT),
        }

    # ─────────────────────────────────────────────────────────────────────────
    # Private: Passphrase resolution
    # ─────────────────────────────────────────────────────────────────────────

    def _read_passphrase_file(self) -> str | None:
        """Read passphrase from file in plugin config directory.

        Location: $APP_HOME/config/plugins/macos_vault_plugin/passphrase

        Returns:
            Passphrase string if file exists, None otherwise
        """
        from pathlib import Path

        app_home = os.environ.get("APP_HOME")
        if not app_home:
            return None

        passphrase_file = (
            Path(app_home) / "config" / "plugins" / "macos_vault_plugin" / "passphrase"
        )
        if not passphrase_file.exists():
            return None

        try:
            passphrase = passphrase_file.read_text().strip()
            if passphrase:
                return passphrase
        except Exception as e:
            logger.error(f"Failed to read passphrase file: {e}")

        return None

    # ─────────────────────────────────────────────────────────────────────────
    # Private: Key wrapping/unwrapping
    # ─────────────────────────────────────────────────────────────────────────

    def _derive_kek(self, passphrase: str, salt: bytes) -> bytes:
        """Derive Key Encryption Key from passphrase using PBKDF2.

        Args:
            passphrase: User passphrase
            salt: Random salt (stored with wrapped key)

        Returns:
            256-bit Key Encryption Key
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=KEY_SIZE,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return kdf.derive(passphrase.encode("utf-8"))

    def _wrap_key(self, kek: bytes, nonce: bytes, master_key: bytes) -> bytes:
        """Wrap (encrypt) master key with KEK using AES-256-GCM.

        Args:
            kek: Key Encryption Key
            nonce: Random nonce (12 bytes)
            master_key: The master key to wrap

        Returns:
            Ciphertext with appended GCM tag (48 bytes total)
        """
        aesgcm = AESGCM(kek)
        return aesgcm.encrypt(nonce, master_key, None)

    def _unwrap_key(self, kek: bytes, nonce: bytes, ciphertext_tag: bytes) -> bytes:
        """Unwrap (decrypt) master key with KEK.

        Args:
            kek: Key Encryption Key
            nonce: Nonce used for encryption
            ciphertext_tag: Ciphertext with GCM tag

        Returns:
            Unwrapped master key

        Raises:
            InvalidPassphraseError: If decryption fails (wrong passphrase)
        """
        aesgcm = AESGCM(kek)
        try:
            return aesgcm.decrypt(nonce, ciphertext_tag, None)
        except Exception as e:
            raise InvalidPassphraseError("Invalid passphrase - cannot unwrap master key") from e

    def _store_wrapped_key(self, account: str, passphrase: str, master_key: bytes) -> None:
        """Wrap and store key in keychain.

        Data format: [salt: 32][nonce: 12][ciphertext+tag: 48]
        """
        salt = os.urandom(SALT_SIZE)
        nonce = os.urandom(NONCE_SIZE)

        kek = self._derive_kek(passphrase, salt)
        ciphertext_tag = self._wrap_key(kek, nonce, master_key)

        wrapped_data = salt + nonce + ciphertext_tag

        keychain = get_keychain()
        keychain.store(account, wrapped_data)

    def _retrieve_wrapped_key(self, account: str, passphrase: str) -> bytes:
        """Retrieve and unwrap key from keychain.

        Args:
            account: Keychain account name
            passphrase: Passphrase to unwrap with

        Returns:
            Unwrapped master key

        Raises:
            InvalidPassphraseError: If passphrase is wrong
            VaultNotInitializedError: If key not found
        """
        keychain = get_keychain()
        data = keychain.retrieve(account)

        if data is None:
            raise VaultNotInitializedError(f"No key found for account: {account}")

        salt = data[:SALT_SIZE]
        nonce = data[SALT_SIZE : SALT_SIZE + NONCE_SIZE]
        ciphertext_tag = data[SALT_SIZE + NONCE_SIZE :]

        kek = self._derive_kek(passphrase, salt)
        return self._unwrap_key(kek, nonce, ciphertext_tag)


# Global singleton for vault-wide key management
_key_manager: VaultKeyManager | None = None


def get_key_manager() -> VaultKeyManager:
    """Get the global VaultKeyManager singleton.

    Returns:
        The global VaultKeyManager instance
    """
    global _key_manager
    if _key_manager is None:
        _key_manager = VaultKeyManager()
    return _key_manager


def reset_key_manager() -> None:
    """Reset the global key manager (for testing)."""
    global _key_manager
    if _key_manager:
        _key_manager.lock()
    _key_manager = None
