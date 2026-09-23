"""Per-product Google API client builders.

The token store is the single owner of refresh logic: :meth:`GoogleServiceFactory`
asks it for a fresh access token on every build and wraps it in a minimal
``Credentials``. No refresh_token is handed to the API client, so the client
never refreshes behind the token store's back — one refresh owner, no drift.

``cache_discovery=False`` avoids the legacy file-cache path (which reaches for
the deprecated ``oauth2client``); ``static_discovery=True`` uses the discovery
documents bundled inside ``google-api-python-client`` so no discovery HTTP call
is made at build time.
"""

from __future__ import annotations

from typing import Any

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from .token_store import TokenStore


class GoogleServiceFactory:
    """Builds Gmail / Drive / Sheets service clients from vaulted tokens."""

    def __init__(self, token_store: TokenStore) -> None:
        self._tokens = token_store

    def _credentials(self) -> Credentials:
        return Credentials(token=self._tokens.get_access_token())

    def _build(self, service_name: str, version: str) -> Any:
        return build(
            service_name,
            version,
            credentials=self._credentials(),
            cache_discovery=False,
            static_discovery=True,
        )

    def gmail(self) -> Any:
        return self._build("gmail", "v1")

    def drive(self) -> Any:
        return self._build("drive", "v3")

    def sheets(self) -> Any:
        return self._build("sheets", "v4")

    def docs(self) -> Any:
        return self._build("docs", "v1")

    def slides(self) -> Any:
        return self._build("slides", "v1")
