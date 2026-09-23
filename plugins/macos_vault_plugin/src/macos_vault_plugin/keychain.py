"""OS Keychain integration for vault key storage (local-solet only).

Single backend: :class:`SystemKeychain` — macOS Keychain via the
``keyring`` library. Stores the master key wrapped with a
passphrase-derived KEK at (service=``<solet>-vault``, account=
``master-key``) and per-plugin credentials directly at
(service=``<solet>.<plugin>``, account=``<credential>``) per the
:class:`PerCredentialKeychain` Protocol.

The previous file-substrate fallback (:class:`FileKeychain`) and the
AWS Secrets Manager backend were retired:

* AWS path moved to :mod:`secrets_manager_vault_plugin` (Task #26,
  2026-05-22) under the "Interface -> Plugin" rule.
* File-substrate retired in P0-A Round 4 (2026-06-09) under
  ``NO fallback code`` — non-macOS hosts use the cloud profile bindings;
  the local profile is macOS-only per ``[[solet-locality]]``.

The Keychain layer is paired with the vault plugin's state-service-
backed substrate; the plugin owns no Postgres driver per
``[[state-service-is-the-only-postgres-path]]``.
"""

import base64
import contextlib
import logging
import os
import urllib.parse
from typing import Protocol

from . import keychain_interaction

logger = logging.getLogger(__name__)

# Keychain identifiers. The service name is per-solet
# (``<solet_name>-vault``, e.g. ``example-vault``); resolved lazily on
# the ``SystemKeychain`` instance from ``SOLET_NAME`` and fast-fails
# if the env var is absent. The account names are platform-wide.
MASTER_KEY_ACCOUNT = "master-key"
RECOVERY_KEY_ACCOUNT = "recovery-key"

# Per-credential service-name template. The fully-resolved service name
# is ``<solet>.<plugin_name>`` (e.g. ``<solet>.macos_vault_plugin``),
# disjoint from the master-key path's ``<solet>-vault`` so the two
# surfaces never collide inside the same OS keychain. The dot separator
# matches the scoped vault-key shape ``<solet>.<plugin>.<credential>``
# from master plan §3.3.1.
PER_CREDENTIAL_SERVICE_SEPARATOR = "."

# RFC 2397 (data URI) value encoding for per-credential entries.
#
# store: a utf-8-safe value -> ``data:text/plain,<percent-encoded>`` (stays
#   human-readable in Keychain Access — no base64 on a text secret); a binary
#   value -> ``data:application/octet-stream;base64,<base64>``.
# retrieve: a ``data:``-prefixed value is parsed as RFC 2397; an UN-prefixed
#   value is LEGACY bare-base64 (the pre-RFC-2397 form the identity keypair +
#   pgvector still use) and decoded exactly as before — ZERO migration. A bare
#   base64 value can never start with ``data:`` (``:`` is not in the base64
#   alphabet), so the prefix is an unambiguous discriminator.
#
# Only ``%`` (the percent-escape), space, control and non-ASCII bytes are
# percent-encoded; common password punctuation stays literal for readability.
# The FIRST ``,`` is the RFC 2397 mediatype/data separator (retrieve splits on
# it), so any literal ``,`` inside the value is preserved.
_RFC2397_DATA_PREFIX = "data:"
_RFC2397_BASE64_SUFFIX = ";base64"
_RFC2397_TEXT_MEDIATYPE = "text/plain"
_RFC2397_BINARY_MEDIATYPE = "application/octet-stream"
_RFC2397_TEXT_SAFE = "!#$&'()*+,-./:;<=>?@[]^_`{|}~"


def _encode_credential_value(value: bytes) -> str:
    """Encode a credential value as an RFC 2397 data URI.

    utf-8-decodable values use the readable ``text/plain`` form; non-utf-8
    binary uses ``application/octet-stream;base64``. The inverse is
    :func:`_decode_credential_value`.
    """
    try:
        text = value.decode("utf-8")
    except UnicodeDecodeError:
        b64 = base64.b64encode(value).decode("ascii")
        return (
            f"{_RFC2397_DATA_PREFIX}{_RFC2397_BINARY_MEDIATYPE}"
            f"{_RFC2397_BASE64_SUFFIX},{b64}"
        )
    return (
        f"{_RFC2397_DATA_PREFIX}{_RFC2397_TEXT_MEDIATYPE},"
        f"{urllib.parse.quote(text, safe=_RFC2397_TEXT_SAFE)}"
    )


def _decode_credential_value(stored: str) -> bytes:
    """Decode a stored credential value.

    RFC 2397 data URIs (``data:`` prefix) are parsed by mediatype; an
    un-prefixed value is LEGACY bare-base64 and decoded as the pre-RFC-2397
    substrate did, so existing entries keep working with zero migration.
    """
    if not stored.startswith(_RFC2397_DATA_PREFIX):
        return base64.b64decode(stored)
    header, sep, data = stored.partition(",")
    if not sep:
        raise ValueError(
            f"malformed RFC 2397 vault value: {_RFC2397_DATA_PREFIX!r} prefix "
            f"without a ',' separator (header={header!r})",
        )
    mediatype = header[len(_RFC2397_DATA_PREFIX):]
    if mediatype.endswith(_RFC2397_BASE64_SUFFIX):
        return base64.b64decode(data)
    return urllib.parse.unquote_to_bytes(data)


class KeychainBackend(Protocol):
    """Protocol for keychain backends."""

    def is_available(self) -> bool:
        """Check if this backend is available."""
        ...

    def store(self, account: str, data: bytes) -> None:
        """Store binary data in keychain."""
        ...

    def retrieve(self, account: str) -> bytes | None:
        """Retrieve binary data from keychain. Returns None if not found."""
        ...

    def delete(self, account: str) -> bool:
        """Delete data from keychain. Returns True if deleted."""
        ...

    def exists(self, account: str) -> bool:
        """Check if account exists in keychain."""
        ...


class PerCredentialKeychain(Protocol):
    """Per-credential entry surface (vault dual-write substrate).

    Distinct from :class:`KeychainBackend`: the master-key path stores ONE
    wrapped key per (service=``<solet>-vault``, account=``master-key``)
    pair; the per-credential surface stores ONE plugin secret per
    (service=``<solet>.<plugin_name>``, account=``<credential>``) pair
    and is the Keychain side of the W-VAULT-LOCAL-KEYCHAIN dual-write
    contract.

    Implementations: :class:`SystemKeychain` (production) and the in-memory
    fake in ``tests/fake_keychain.py`` (CI). :class:`FileKeychain` does
    NOT implement this Protocol — headless / non-macOS deployments stay
    state-service-authoritative through Tier 5 (Codex sign-off correction
    #4).
    """

    def is_available(self) -> bool:
        """Return whether the configured Keychain backend is usable."""
        ...

    def store_credential(
        self, plugin_name: str, credential: str, value: bytes,
    ) -> None:
        """Store ``value`` at (service=``<solet>.<plugin_name>``, account=``<credential>``).

        Replaces any existing entry at the same (service, account) pair —
        the dual-write contract's atomicity guarantees come from the
        caller's wrapping logic, not from a Keychain-level CAS.
        """
        ...

    def retrieve_credential(
        self, plugin_name: str, credential: str,
    ) -> bytes | None:
        """Return the bytes at (service=``<solet>.<plugin_name>``, account=``<credential>``).

        Returns ``None`` if no entry exists. Does NOT fall through to
        the state-service substrate — the caller layers that fallback
        on top.
        """
        ...

    def delete_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        """Delete (service=``<solet>.<plugin_name>``, account=``<credential>``).

        Returns ``True`` when an entry was actually deleted, ``False`` when
        no entry was present (idempotent).
        """
        ...

    def exists_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        """Return whether an entry exists at (service=``<solet>.<plugin_name>``, account=``<credential>``)."""
        ...

    def list_credentials_under_solet(self) -> list[tuple[str, str]]:
        """Return ``[(plugin_name, credential), ...]`` for every entry the solet owns.

        Enumerates every ``kSecClassGenericPassword`` item whose service is
        ``<solet>.<plugin_name>`` (exactly two dot-separated segments).
        Excludes the master-key entry (``<solet>-vault``) and any
        legacy operator-written entries that don't match the per-credential
        scheme — those surface to the operator separately via punch-list
        review, not via the runtime ``vault::list`` verb.
        """
        ...


class SystemKeychain:
    """macOS Keychain backend via the keyring library. Single substrate post-P0-A."""

    def __init__(self, *, allow_user_interaction: bool = False) -> None:
        """Bind the per-solet service name and the credential-read prompt policy.

        Args:
            allow_user_interaction: whether this instance's credential READS may
                block on a macOS Keychain confirmation dialog. Defaults to
                ``False`` — the daemon-safe policy, under which an unauthorized
                read fails loud with ``errSecAuthFailed`` instead of hanging
                forever on a prompt nobody is watching (see
                :mod:`macos_vault_plugin.keychain_interaction`). Operator-run
                seed tooling passes ``True``: answering that prompt is how a
                human re-grants an ACL after the signing identity changes, so
                the repair path must stay interactive.
        """
        import keyring
        from keyring.backends import fail

        name = os.environ.get("SOLET_NAME", "").strip()
        if not name:
            raise RuntimeError(
                "macos_vault_plugin.keychain: SOLET_NAME env var "
                "is required to resolve the per-solet keychain "
                "service name.",
            )
        self._service_name: str = f"{name}-vault"
        self._allow_user_interaction: bool = allow_user_interaction
        self._available: bool = not isinstance(keyring.get_keyring(), fail.Keyring)

    @property
    def service_name(self) -> str:
        """Per-solet keychain service name (e.g. ``example-vault``).

        Resolved eagerly in ``__init__`` from ``SOLET_NAME``;
        fast-fails at construction time if the env var is absent — a
        keychain read/write without an owning solet would silently
        land in the wrong entry.
        """
        return self._service_name

    def is_available(self) -> bool:
        """Whether the host's ``keyring`` backend is a real one (not the fail-shim)."""
        return self._available

    def _read_gate(self) -> contextlib.AbstractContextManager[None]:
        """Prompt policy for this instance's credential reads.

        Reads are gated, writes are not: a write to a NEW item needs no ACL
        approval, and a write that DOES need one is only ever issued by operator
        tooling that can answer it. Only reads produced the indefinite hang.
        """
        return keychain_interaction.read_gate(self._allow_user_interaction)

    def store(self, account: str, data: bytes) -> None:
        """Store binary data in keychain (base64 encoded)."""
        import keyring

        # Keychain stores strings, so we base64 encode the binary data
        encoded = base64.b64encode(data).decode("ascii")
        keyring.set_password(self.service_name, account, encoded)

    def retrieve(self, account: str) -> bytes | None:
        """Retrieve binary data from keychain."""
        import keyring

        with self._read_gate():
            encoded = keyring.get_password(self.service_name, account)
        if encoded is None:
            return None

        data = base64.b64decode(encoded)
        return data

    def delete(self, account: str) -> bool:
        """Delete data from keychain."""
        import keyring

        try:
            keyring.delete_password(self.service_name, account)
            return True
        except keyring.errors.PasswordDeleteError:
            return False

    def exists(self, account: str) -> bool:
        """Check if account exists in keychain."""
        import keyring

        with self._read_gate():
            return keyring.get_password(self.service_name, account) is not None

    # ─────────────────────────────────────────────────────────────────────
    # PerCredentialKeychain implementation (W-VAULT-LOCAL-KEYCHAIN Tier 3).
    #
    # The per-credential surface scopes service=``<solet>.<plugin>``,
    # account=``<credential>`` — disjoint from the master-key path's
    # ``<solet>-vault`` service so the two coexist in one OS keychain.
    # ─────────────────────────────────────────────────────────────────────

    def _scoped_service_name(self, plugin_name: str) -> str:
        """Per-credential service name: ``<solet>.<plugin_name>``.

        Fast-fails when ``SOLET_NAME`` is unset — a per-credential
        write without an owning solet would silently land in the
        wrong tenant's keychain.
        """
        solet = os.environ.get("SOLET_NAME", "").strip()
        if not solet:
            raise RuntimeError(
                "macos_vault_plugin.keychain: SOLET_NAME env var is "
                "required to resolve the per-credential keychain service "
                "name.",
            )
        return f"{solet}{PER_CREDENTIAL_SERVICE_SEPARATOR}{plugin_name}"

    def store_credential(
        self, plugin_name: str, credential: str, value: bytes,
    ) -> None:
        """Direct Keychain storage as an RFC 2397 data URI. The OS Keychain provides AES-256-GCM at rest."""
        import keyring

        keyring.set_password(
            self._scoped_service_name(plugin_name),
            credential,
            _encode_credential_value(value),
        )

    def retrieve_credential(
        self, plugin_name: str, credential: str,
    ) -> bytes | None:
        import keyring

        with self._read_gate():
            stored = keyring.get_password(
                self._scoped_service_name(plugin_name), credential,
            )
        if stored is None:
            return None
        return _decode_credential_value(stored)

    def delete_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        import keyring

        try:
            keyring.delete_password(
                self._scoped_service_name(plugin_name), credential,
            )
            return True
        except keyring.errors.PasswordDeleteError:
            return False

    def exists_credential(
        self, plugin_name: str, credential: str,
    ) -> bool:
        import keyring

        with self._read_gate():
            return (
                keyring.get_password(
                    self._scoped_service_name(plugin_name), credential,
                )
                is not None
            )

    def list_credentials_under_solet(self) -> list[tuple[str, str]]:
        """Enumerate per-credential entries owned by this solet.

        macOS implementation: shells out to ``security dump-keychain`` and
        filters to entries whose service field is exactly
        ``<solet>.<plugin_name>`` (two dot-separated segments,
        non-empty plugin name). Anomalous entries (3+ dots, flat-account,
        bare-service) are excluded — they are operator-side review state
        per the punch list, not part of the runtime vault contract.

        Returns ``[(plugin_name, credential), ...]`` sorted for stable
        output. Excludes ``<solet>-vault`` (master-key path).
        """
        solet = os.environ.get("SOLET_NAME", "").strip()
        if not solet:
            raise RuntimeError(
                "macos_vault_plugin.keychain.list_credentials_under_solet: "
                "SOLET_NAME env var is required.",
            )
        expected_service_prefix = f"{solet}{PER_CREDENTIAL_SERVICE_SEPARATOR}"
        dump = _security_dump_keychain()
        seen: set[tuple[str, str]] = set()
        for record in _split_keychain_dump_records(dump):
            match = _extract_canonical_credential(record, expected_service_prefix)
            if match is not None:
                seen.add(match)
        return sorted(seen)


def _security_dump_keychain() -> str:
    """Run ``security dump-keychain`` and return its stdout, raising on non-zero exit."""
    import subprocess

    completed = subprocess.run(  # noqa: S603, S607
        ["security", "dump-keychain"],
        capture_output=True, text=True, check=False, timeout=15,
    )
    if completed.returncode != 0:
        raise RuntimeError(
            f"`security dump-keychain` exited {completed.returncode}: "
            f"{completed.stderr.strip()!r}",
        )
    return completed.stdout


def _split_keychain_dump_records(dump: str) -> list[str]:
    """Split ``security dump-keychain`` stdout into per-item record strings."""
    records: list[str] = []
    cur: list[str] = []
    for line in dump.splitlines(keepends=True):
        if line.startswith(("keychain:", "version:", "class:")) and cur:
            records.append("".join(cur))
            cur = []
        cur.append(line)
    if cur:
        records.append("".join(cur))
    return records


def _extract_canonical_credential(
    record: str, expected_service_prefix: str,
) -> tuple[str, str] | None:
    """Return ``(plugin_name, credential)`` iff record matches the canonical scheme."""
    import re

    svce_match = re.search(r'"svce"<blob>="([^"]*)"', record)
    acct_match = re.search(r'"acct"<blob>="([^"]*)"', record)
    if svce_match is None or acct_match is None:
        return None
    service = svce_match.group(1)
    account = acct_match.group(1)
    if not service.startswith(expected_service_prefix):
        return None
    plugin_segment = service[len(expected_service_prefix):]
    if not plugin_segment or PER_CREDENTIAL_SERVICE_SEPARATOR in plugin_segment:
        return None
    if not account or account == service:
        return None
    return plugin_segment, account


def get_keychain() -> KeychainBackend:
    """Return the macOS Keychain backend. Raises if the host has no real backend.

    Single substrate post-P0-A: the file-storage fallback and the AWS
    Secrets Manager branches were retired (see module docstring). Callers
    that need a non-macOS substrate use the cloud profile bindings.
    """
    keychain = SystemKeychain()
    if not keychain.is_available():
        raise RuntimeError(
            "macos_vault_plugin: SystemKeychain reports no real keyring backend "
            "available on this host. The local profile requires macOS Keychain "
            "per [[solet-locality]]; non-macOS hosts use the cloud profile "
            "bindings (secrets_manager_vault_plugin).",
        )
    return keychain


def get_backend_name() -> str:
    """Name of the active keychain backend, for diagnostics/logging."""
    import keyring

    return f"System Keychain ({type(keyring.get_keyring()).__name__})"
