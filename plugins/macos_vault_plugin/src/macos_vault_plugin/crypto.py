"""AES-256-GCM encryption with PBKDF2 key derivation.

Security:
- Uses AES-256-GCM (authenticated encryption)
- PBKDF2-HMAC-SHA256 with 1.2M iterations for key derivation
- Per-secret unique salt and nonce
- Never logs plaintext or keys
"""

import base64
import os

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from .constants import KEY_SIZE, NONCE_SIZE, PBKDF2_ITERATIONS, SALT_SIZE
from .errors import DecryptionError, InvalidMasterKeyError


class VaultCrypto:
    """AES-256-GCM encryption with PBKDF2 key derivation."""

    def __init__(self, master_key: bytes) -> None:
        """Initialize with master key.

        Args:
            master_key: Must be at least 32 bytes

        Raises:
            InvalidMasterKeyError: If master key is too short
        """
        if len(master_key) < KEY_SIZE:
            raise InvalidMasterKeyError(
                f"Master key must be at least {KEY_SIZE} bytes, got {len(master_key)}"
            )
        self._master_key = master_key

    def _derive_key(self, salt: bytes) -> bytes:
        """Derive AES-256 key from master key using PBKDF2.

        Args:
            salt: Random salt (should be SALT_SIZE bytes)

        Returns:
            Derived key (KEY_SIZE bytes)
        """
        kdf = PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=KEY_SIZE,
            salt=salt,
            iterations=PBKDF2_ITERATIONS,
        )
        return kdf.derive(self._master_key)

    def encrypt(self, plaintext: str) -> dict[str, str]:
        """Encrypt plaintext using AES-256-GCM.

        Args:
            plaintext: The string to encrypt

        Returns:
            Dict with base64-encoded: ciphertext, salt, nonce, tag
        """
        # Generate random salt and nonce
        salt = os.urandom(SALT_SIZE)
        nonce = os.urandom(NONCE_SIZE)

        # Derive key from master key + salt
        key = self._derive_key(salt)

        # Encrypt with AES-GCM
        aesgcm = AESGCM(key)
        ciphertext_with_tag = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)

        # GCM appends 16-byte tag to ciphertext
        ciphertext = ciphertext_with_tag[:-16]
        tag = ciphertext_with_tag[-16:]

        return {
            "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            "salt": base64.b64encode(salt).decode("ascii"),
            "nonce": base64.b64encode(nonce).decode("ascii"),
            "tag": base64.b64encode(tag).decode("ascii"),
        }

    def decrypt(self, ciphertext_b64: str, salt_b64: str, nonce_b64: str, tag_b64: str) -> str:
        """Decrypt ciphertext using AES-256-GCM.

        Args:
            ciphertext_b64: Base64-encoded ciphertext
            salt_b64: Base64-encoded salt
            nonce_b64: Base64-encoded nonce
            tag_b64: Base64-encoded authentication tag

        Returns:
            Decrypted plaintext string

        Raises:
            DecryptionError: If authentication fails or data is corrupted
        """
        try:
            ciphertext = base64.b64decode(ciphertext_b64)
            salt = base64.b64decode(salt_b64)
            nonce = base64.b64decode(nonce_b64)
            tag = base64.b64decode(tag_b64)

            # Derive key from master key + salt
            key = self._derive_key(salt)

            # Reconstruct ciphertext with tag for GCM
            aesgcm = AESGCM(key)
            ciphertext_with_tag = ciphertext + tag
            plaintext = aesgcm.decrypt(nonce, ciphertext_with_tag, None)

            return plaintext.decode("utf-8")

        except Exception as e:
            # Don't expose internal error details - could leak information
            raise DecryptionError("Failed to decrypt secret") from e
