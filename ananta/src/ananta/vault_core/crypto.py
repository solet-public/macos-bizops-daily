"""Pure crypto helpers shared by both vault plugins.

NO storage, NO process registration, NO master-key handling. Just the
primitives both plugins use:

* scrypt OAuth client-secret hashing (constant-time verify)
* libsodium sealed-box (X25519 anonymous-sender) encrypt/decrypt
* X25519 keypair generation
* sha256 fingerprint truncations for audit + UI display
"""

from __future__ import annotations

import hashlib
import hmac

from nacl.public import PrivateKey, PublicKey, SealedBox

from .records import (
    OAUTH_SCRYPT_DKLEN,
    OAUTH_SCRYPT_N,
    OAUTH_SCRYPT_P,
    OAUTH_SCRYPT_R,
    X25519_KEY_LENGTH_BYTES,
)

# Fingerprint format constants — sha256 of input, hex-truncated. Safe
# to log / display; never reversible to the plaintext.
PLAINTEXT_FINGERPRINT_PREFIX = "sha256:"
PLAINTEXT_FINGERPRINT_HEX_LEN = 16
PUBLIC_KEY_FINGERPRINT_PREFIX = "sha256:"
PUBLIC_KEY_FINGERPRINT_HEX_LEN = 16


# ─── OAuth client_secret scrypt hashing ─────────────────────────────────────


def scrypt_hash_secret(secret: str, salt: bytes) -> bytes:
    """Compute the scrypt hash of an OAuth client_secret.

    Returns the raw 32-byte hash. Callers serialize via base64 for
    storage. Matches the parameters used by macos_vault_plugin so
    records round-trip cleanly across the migration.
    """
    return hashlib.scrypt(
        secret.encode("utf-8"),
        salt=salt,
        n=OAUTH_SCRYPT_N,
        r=OAUTH_SCRYPT_R,
        p=OAUTH_SCRYPT_P,
        dklen=OAUTH_SCRYPT_DKLEN,
    )


def scrypt_verify_secret(candidate: str, salt: bytes, expected_hash: bytes) -> bool:
    """Constant-time compare an OAuth ``client_secret`` against its stored hash.

    Returns ``True`` iff the candidate hashes to ``expected_hash``
    under the platform's standard scrypt parameters + the stored salt.
    Uses :func:`hmac.compare_digest` so a timing oracle can't probe
    valid vs. invalid secrets.
    """
    candidate_hash = scrypt_hash_secret(candidate, salt)
    return hmac.compare_digest(candidate_hash, expected_hash)


# ─── X25519 keypair + sealed-box transfer ───────────────────────────────────


def generate_encryption_keypair() -> tuple[bytes, bytes]:
    """Generate a fresh X25519 identity keypair.

    Returns ``(private_bytes, public_bytes)`` each 32 bytes long.
    Callers persist the private half as a vault secret and expose the
    public half via ``get_public_key`` for cross-solet transfers.
    """
    private_key = PrivateKey.generate()
    private_bytes = bytes(private_key)
    public_bytes = bytes(private_key.public_key)
    if len(private_bytes) != X25519_KEY_LENGTH_BYTES:
        raise RuntimeError(
            f"vault_core: PrivateKey.generate produced {len(private_bytes)} bytes; "
            f"expected {X25519_KEY_LENGTH_BYTES}",
        )
    if len(public_bytes) != X25519_KEY_LENGTH_BYTES:
        raise RuntimeError(
            f"vault_core: PublicKey from PrivateKey.generate produced "
            f"{len(public_bytes)} bytes; expected {X25519_KEY_LENGTH_BYTES}",
        )
    return private_bytes, public_bytes


def encrypt_sealed_box(plaintext: bytes, recipient_public_key: bytes) -> bytes:
    """Encrypt ``plaintext`` to ``recipient_public_key`` via libsodium sealed-box.

    Anonymous sender: the resulting ciphertext does not reveal the
    sender's identity (no static-static DH). Only the recipient's
    private key can open it. Returns the full sealed-box ciphertext
    (ephemeral pubkey || box).
    """
    box = SealedBox(PublicKey(recipient_public_key))
    return box.encrypt(plaintext)


def decrypt_sealed_box(ciphertext: bytes, recipient_private_key: bytes) -> bytes:
    """Open a sealed-box ciphertext with ``recipient_private_key``.

    Raises :class:`nacl.exceptions.CryptoError` (subclass of
    ``Exception``) if the ciphertext is malformed, truncated, or not
    sealed to this recipient.
    """
    box = SealedBox(PrivateKey(recipient_private_key))
    return box.decrypt(ciphertext)


# ─── Fingerprints — safe-to-log truncated sha256s ───────────────────────────


def fingerprint_plaintext(plaintext: bytes | str) -> str:
    """Return a ``sha256:<hex16>`` fingerprint of plaintext content.

    Used in audit rows to record "what value was transferred" without
    leaking the value itself. Hex truncated to 16 chars (64 bits)
    which is enough entropy to detect mismatches in audits while
    keeping the field human-skimmable.
    """
    if isinstance(plaintext, str):
        plaintext = plaintext.encode("utf-8")
    digest = hashlib.sha256(plaintext).hexdigest()[:PLAINTEXT_FINGERPRINT_HEX_LEN]
    return f"{PLAINTEXT_FINGERPRINT_PREFIX}{digest}"


def fingerprint_public_key(public_key_bytes: bytes) -> str:
    """Return a ``sha256:<hex16>`` fingerprint of an X25519 pubkey.

    Same shape as :func:`fingerprint_plaintext` but with the
    ``PUBLIC_KEY_*`` constants so audit consumers can distinguish
    intent (the two values happen to share format today; keeping the
    constants separate leaves room to diverge).
    """
    digest = hashlib.sha256(public_key_bytes).hexdigest()[
        :PUBLIC_KEY_FINGERPRINT_HEX_LEN
    ]
    return f"{PUBLIC_KEY_FINGERPRINT_PREFIX}{digest}"
