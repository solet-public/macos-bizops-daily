"""In-memory ``PerCredentialKeychain`` fake for CI smokes.

Lives in ``tests/`` not ``src/`` so the production package never ships
the fake. Implements :class:`PerCredentialKeychain` with a single
``dict[tuple[str, str], bytes]`` keyed by (plugin_name, credential). No
``keyring`` import, no Security.framework call, no filesystem state, no
state-service dependency — safe to instantiate inside any test process
without touching the host keychain or the platform's state-service
substrate.

Codex sign-off correction #4: CI must not touch the real macOS keychain;
the dual-write smokes use this fake. The production ``SystemKeychain``
backend is exercised end-to-end only by the sacrificial-cutover smoke
(SC-17) against a freshly-birthed solet.
"""

from __future__ import annotations


class FakeKeychain:
    """In-memory ``PerCredentialKeychain``. Process-local, not thread-safe."""

    def __init__(self) -> None:
        self._store: dict[tuple[str, str], bytes] = {}

    def _key(self, plugin_name: str, credential: str) -> tuple[str, str]:
        return (plugin_name, credential)

    def is_available(self) -> bool:
        """Mirror the production backend availability probe for hermetic tests."""
        return True

    def store_credential(
        self, plugin_name: str, credential: str, value: bytes,
    ) -> None:
        self._store[self._key(plugin_name, credential)] = value

    def retrieve_credential(
        self, plugin_name: str, credential: str,
    ) -> bytes | None:
        return self._store.get(self._key(plugin_name, credential))

    def delete_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        return self._store.pop(self._key(plugin_name, credential), None) is not None

    def exists_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        return self._key(plugin_name, credential) in self._store

    def list_credentials_under_solet(self) -> list[tuple[str, str]]:
        """Return every (plugin_name, credential) in the fake — sorted."""
        return sorted(self._store.keys())

    # Test-only inspection helpers (used by SC-3, SC-4, SC-5, SC-6 to
    # assert dual-substrate state without going through the public
    # PerCredentialKeychain Protocol surface).

    def snapshot(self) -> dict[tuple[str, str], bytes]:
        """Read-only copy of the in-memory store for assertions."""
        return dict(self._store)

    def clear(self) -> None:
        """Reset the fake. Used between smokes that share a fixture."""
        self._store.clear()
