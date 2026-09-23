"""macOS Vault Plugin - Secure credential storage via Keychain (per-credential) + SQL dual-write fallback during W-VAULT-MIGRATE window."""

from .plugin import MacosVaultPlugin

__all__ = ["MacosVaultPlugin"]
