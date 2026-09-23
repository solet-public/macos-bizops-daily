"""Per-connection DSN resolution from the address book + JDBC-URL parsing.

Every connection is one ``external_pg::<name>`` address-book entry: literal
host/port/dbname/user/sslmode fields plus a ``vault::`` password reference the
resolver swaps in via ``resolve_with_secrets`` (chain-consumed — see
:func:`constants.vault_key_for_password`). ``resolve`` returns a frozen
:class:`ExternalDsn`; ``list_connection_names`` enumerates the registered names
(client-side prefix filter over the substring-style address-book search).

Registration convenience (§8.2, R7-ii): :func:`parse_jdbc_url` decomposes a
``jdbc:postgresql://…`` / ``postgresql://…`` URL into discrete fields for the
operator's registration runbook. The password is extracted to a SEPARATE field
and NEVER appears in any log/echo/error — both :class:`ExternalDsn` and
:class:`ParsedRegistration` redact it from ``repr``, and parse failures raise
with a :func:`scrub_password_from_url`-scrubbed URL (rev-F R-D4).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlsplit, urlunsplit

from .constants import (
    CONNECTION_ADDRESS_TYPE,
    CONNECTION_ENTRY_PREFIX,
    DEFAULT_PORT,
    DEFAULT_SSLMODE,
    ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
    ERROR_CONNECTION_UNKNOWN,
    FIELD_DBNAME,
    FIELD_HOST,
    FIELD_PASSWORD,
    FIELD_PORT,
    FIELD_SSLMODE,
    FIELD_USER,
    LIST_CONNECTIONS_SCAN_LIMIT,
)

_PASSWORD_REDACTION = "***"
_ALLOWED_SCHEMES = frozenset({"postgresql", "postgres"})


class ExternalPgConfigError(RuntimeError):
    """Raised when a connection cannot be resolved from the address book."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, repr=False)
class ExternalDsn:
    """A resolved foreign-Postgres connection target.

    ``repr`` deliberately redacts the password so logging the object never
    leaks the secret (rev-F R-D4).
    """

    name: str
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str

    def __repr__(self) -> str:
        return (
            f"ExternalDsn(name={self.name!r}, host={self.host!r}, port={self.port!r}, "
            f"dbname={self.dbname!r}, user={self.user!r}, "
            f"password={_PASSWORD_REDACTION!r}, sslmode={self.sslmode!r})"
        )


@dataclass(frozen=True, repr=False)
class ParsedRegistration:
    """Discrete fields decomposed from a JDBC/libpq URL for registration.

    The password is a separate field the runbook routes agent-blind to vault; it
    NEVER lands in the address-book entry or a log. ``repr`` redacts it.
    """

    host: str
    port: int
    dbname: str
    user: str
    sslmode: str
    password: str

    def __repr__(self) -> str:
        return (
            f"ParsedRegistration(host={self.host!r}, port={self.port!r}, "
            f"dbname={self.dbname!r}, user={self.user!r}, sslmode={self.sslmode!r}, "
            f"password={_PASSWORD_REDACTION!r})"
        )


class AppConfigLoader:
    """Resolve foreign-PG connections from ``service_interface::address_book_service``.

    The service is duck-typed so this plugin never imports from
    ``default_address_book_plugin``.
    """

    def __init__(self, address_book_service: Any) -> None:
        if address_book_service is None:
            raise ExternalPgConfigError(
                ERROR_ADDRESS_BOOK_NOT_AVAILABLE,
                "address_book_service is required for AppConfigLoader",
            )
        self._address_book = address_book_service

    def resolve(self, name: str) -> ExternalDsn:
        """Return the resolved DSN for connection ``name`` (vault password swapped)."""
        if not name:
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN, "a non-empty connection_name is required"
            )
        entry_name = f"{CONNECTION_ENTRY_PREFIX}{name}"
        result = self._address_book.resolve_with_secrets(name=entry_name)
        entries = self._extract_entries(result, name)
        return self._build_dsn(name, entries)

    def list_connection_names(self) -> tuple[list[str], bool]:
        """Enumerate registered connection names (bare, prefix-stripped).

        The address-book search is SUBSTRING-style, not a true prefix API, so a
        substring hit like ``foo_external_pg::bar`` is dropped client-side; only
        names that actually start with ``external_pg::`` survive. Returns
        ``(names, truncated)`` — ``truncated`` is True when the scan hit the
        limit (the caller logs it; no silent caps).
        """
        result = self._address_book.search(
            query=CONNECTION_ENTRY_PREFIX,
            address_type=CONNECTION_ADDRESS_TYPE,
            limit=LIST_CONNECTIONS_SCAN_LIMIT,
        )
        addresses = self._extract_addresses(result)
        names: list[str] = []
        for row in addresses:
            raw = row.get("name")
            if isinstance(raw, str) and raw.startswith(CONNECTION_ENTRY_PREFIX):
                names.append(raw[len(CONNECTION_ENTRY_PREFIX):])
        truncated = len(addresses) >= LIST_CONNECTIONS_SCAN_LIMIT
        return sorted(names), truncated

    def _extract_entries(self, result: Any, name: str) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN,
                f"no connection named '{name}' is registered "
                f"(expected address-book entry '{CONNECTION_ENTRY_PREFIX}{name}')",
            )
        data = result.get("data") or {}
        entries = data.get("entries", []) if isinstance(data, dict) else []
        if not isinstance(entries, list):
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN, f"connection '{name}' entry has no fields"
            )
        return [entry for entry in entries if isinstance(entry, dict)]

    @staticmethod
    def _extract_addresses(result: Any) -> list[dict[str, Any]]:
        if not isinstance(result, dict) or result.get("action_status") != "completed":
            return []
        data = result.get("data") or {}
        addresses = data.get("addresses", []) if isinstance(data, dict) else []
        if not isinstance(addresses, list):
            return []
        return [row for row in addresses if isinstance(row, dict)]

    def _build_dsn(self, name: str, entries: list[dict[str, Any]]) -> ExternalDsn:
        host = _first_value(entries, FIELD_HOST)
        dbname = _first_value(entries, FIELD_DBNAME)
        user = _first_value(entries, FIELD_USER)
        password = _first_value(entries, FIELD_PASSWORD)
        sslmode = _first_value(entries, FIELD_SSLMODE) or DEFAULT_SSLMODE
        port = _coerce_port(_first_value(entries, FIELD_PORT))
        missing = [
            label
            for label, value in (
                (FIELD_HOST, host),
                (FIELD_DBNAME, dbname),
                (FIELD_USER, user),
                (FIELD_PASSWORD, password),
            )
            if not value
        ]
        if missing:
            raise ExternalPgConfigError(
                ERROR_CONNECTION_UNKNOWN,
                f"connection '{name}' entry is incomplete: missing {missing}. "
                "Register host, port, dbname, user, sslmode, and a "
                "vault:: password reference.",
            )
        return ExternalDsn(
            name=name,
            host=host,
            port=port,
            dbname=dbname,
            user=user,
            password=password,
            sslmode=sslmode,
        )


def parse_jdbc_url(url: str) -> ParsedRegistration:
    """Decompose a JDBC/libpq Postgres URL into discrete registration fields.

    Accepts ``jdbc:postgresql://…`` (the ``jdbc:`` prefix is stripped),
    ``postgresql://…``, and ``postgres://…``. User/password may live in the
    netloc (``user:pass@host``) or as ``user=``/``password=`` query params. The
    password is extracted to its own field and scrubbed from any error message
    (rev-F R-D4). Raises ``ValueError`` (with a scrubbed URL) on a malformed or
    non-Postgres URL.
    """
    if not url.strip():
        raise ValueError("a non-empty JDBC/libpq URL is required")
    candidate = url.strip()
    if candidate.lower().startswith("jdbc:"):
        candidate = candidate[len("jdbc:"):]
    parts = urlsplit(candidate)
    if parts.scheme.lower() not in _ALLOWED_SCHEMES:
        raise ValueError(
            f"unsupported URL scheme '{parts.scheme}' in "
            f"{scrub_password_from_url(url)}; expected postgresql:// or postgres://"
        )
    query = parse_qs(parts.query)
    host = parts.hostname or ""
    dbname = parts.path.lstrip("/")
    user = parts.username or _first_query(query, "user")
    password = parts.password or _first_query(query, "password")
    sslmode = _first_query(query, "sslmode") or DEFAULT_SSLMODE
    port = parts.port if parts.port is not None else DEFAULT_PORT
    if not dbname:
        raise ValueError(
            f"URL {scrub_password_from_url(url)} is missing the database name "
            "(the /dbname path component)"
        )
    return ParsedRegistration(
        host=host,
        port=port,
        dbname=dbname,
        user=user,
        sslmode=sslmode,
        password=password,
    )


def scrub_password_from_url(url: str) -> str:
    """Return ``url`` with any embedded password replaced by ``***``.

    Fail-safe: if the URL cannot be parsed, return a fully-redacted placeholder
    rather than echoing a possibly-password-bearing raw string.
    """
    try:
        candidate = url.strip()
        prefix = ""
        if candidate.lower().startswith("jdbc:"):
            prefix, candidate = "jdbc:", candidate[len("jdbc:"):]
        parts = urlsplit(candidate)
        netloc = parts.netloc
        if parts.password:
            userinfo, _, hostinfo = netloc.rpartition("@")
            user = parts.username or ""
            netloc = f"{user}:{_PASSWORD_REDACTION}@{hostinfo}" if userinfo else hostinfo
        query = _scrub_query_password(parts.query)
        scrubbed = urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
        return prefix + scrubbed
    except (ValueError, TypeError):
        return "<url redacted>"


def _scrub_query_password(query: str) -> str:
    if not query:
        return query
    out: list[str] = []
    for pair in query.split("&"):
        key, sep, _ = pair.partition("=")
        if key.lower() == "password" and sep:
            out.append(f"{key}={_PASSWORD_REDACTION}")
        else:
            out.append(pair)
    return "&".join(out)


def _first_query(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key)
    return values[0] if values else ""


def _first_value(entries: list[dict[str, Any]], field_type: str) -> str:
    for entry in entries:
        if entry.get("field_type") == field_type:
            value = entry.get("value", "")
            return value if isinstance(value, str) else ""
    return ""


def _coerce_port(raw: str) -> int:
    """Blank/absent port defaults to libpq's 5432 (advisor blind-spot #1).

    A blank port would otherwise crash ``int("")`` in the containment guard or
    silently miss the platform-port compare — both are bypasses.
    """
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError as exc:
        raise ExternalPgConfigError(
            ERROR_CONNECTION_UNKNOWN, f"port '{raw}' is not an integer"
        ) from exc
