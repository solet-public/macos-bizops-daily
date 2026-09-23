"""In-memory `PerCredentialKeychain` fake for the credential_seed smoke.

Own-copy of `macos_vault_plugin/tests/fake_keychain.py`'s shape (test-only
code, kept independently per the own-copy-per-plugin convention so this
plugin's gate-registered smoke never depends on another plugin's test
tree). Lives in `tests/` not `src/` so the production package never ships
it. No `keyring` import, no Security.framework call -- safe to instantiate
in any test process without touching the host Keychain.
"""

from __future__ import annotations


class FakeKeychain:
    """In-memory `PerCredentialKeychain`. Process-local, not thread-safe."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], bytes] = {}

    def _key(self, plugin_name: str, credential: str) -> tuple[str, str]:
        return (plugin_name, credential)

    def store_credential(self, plugin_name: str, credential: str, value: bytes) -> None:
        self._store[self._key(plugin_name, credential)] = value

    def retrieve_credential(self, plugin_name: str, credential: str) -> bytes | None:
        return self._store.get(self._key(plugin_name, credential))

    def delete_credential(self, plugin_name: str, credential: str) -> bool:
        return self._store.pop(self._key(plugin_name, credential), None) is not None

    def exists_credential(self, plugin_name: str, credential: str) -> bool:
        return self._key(plugin_name, credential) in self._store

    def list_credentials_under_solet(self) -> list[tuple[str, str]]:
        return sorted(self._store.keys())

    def snapshot(self) -> dict[tuple[str, str], bytes]:
        """Read-only copy of the in-memory store for assertions."""
        return dict(self._store)
