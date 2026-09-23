"""Postgres backend machinery used by macos_vault_plugin.

Owned per-plugin (no platform-layer sharing). Vault plugins use the
store helper for KV-style storage backed by PostgresProvider.
"""
