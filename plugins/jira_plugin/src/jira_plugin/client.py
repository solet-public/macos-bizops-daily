"""Jira client factory + token-expiry awareness.

Builds a pycontribs ``jira.JIRA`` client from the resolved :class:`JiraAppConfig`
using HTTP basic-auth (service-account email + API token) against a pinned REST
API version. The client is built lazily and cached: the first verb call triggers
construction inside the plugin's ``_run`` try, so a connection/auth fault
surfaces as a typed error rather than crashing readiness.

Token-expiry awareness (umbrella design §7.3): Atlassian API tokens expire
(default 1yr, 1-365d). ``check_token_expiry`` is a PURE function — given the
recorded ``expires_at``, the current time, and a warn window, it returns a typed
``ExpiryWarning`` (code ``jira.token_expiring``) when the token is at/within the
window (or already lapsed), else ``None``. The factory logs that warning loudly
at client-build so an impending lapse is visible up-front, never a mystery 401.
The warning message deliberately carries NO site host (topology hygiene, §2.4) —
only the days-remaining and the operator rotation pointer.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import UTC, datetime

from jira import JIRA

from .app_config import AppConfigLoader, JiraAppConfig
from .constants import (
    ERROR_TOKEN_EXPIRING,
    JIRA_OPTION_REST_API_VERSION,
    JIRA_REST_API_VERSION,
)

_SECONDS_PER_DAY: int = 86_400


@dataclass(frozen=True)
class ExpiryWarning:
    """A loud, typed token-expiry warning (no site host — topology-safe)."""

    code: str
    days_remaining: int
    message: str


def check_token_expiry(
    expires_at: datetime,
    now: datetime,
    warn_days: int,
) -> ExpiryWarning | None:
    """Return a typed warning when the token is at/within the warn window.

    ``days_remaining`` is the signed whole-day count (negative once expired).
    Returns ``None`` when the token has more than ``warn_days`` left. Pure — the
    caller injects ``now`` — so the expiry-awareness smoke tests it directly,
    red-first, with no clock dependency.
    """
    days_remaining = math.floor((expires_at - now).total_seconds() / _SECONDS_PER_DAY)
    if days_remaining > warn_days:
        return None
    if days_remaining < 0:
        message = (
            f"Jira API token has EXPIRED (~{abs(days_remaining)}d ago). Rotate now: "
            "mint a fresh token in the Atlassian console with the same scope, "
            "agent-blind vault re-store of jira_api_token, update expires_at in the "
            "jira_site entry."
        )
    else:
        message = (
            f"Jira API token expires in ~{days_remaining}d (within the {warn_days}d "
            "warn window). Rotate soon: mint a fresh token with the same scope, "
            "agent-blind vault re-store of jira_api_token, update expires_at in the "
            "jira_site entry."
        )
    return ExpiryWarning(code=ERROR_TOKEN_EXPIRING, days_remaining=days_remaining, message=message)


class JiraClientFactory:
    """Lazily build + cache the ``jira.JIRA`` client from the resolved config."""

    def __init__(
        self,
        app_config_loader: AppConfigLoader,
        logger: logging.Logger,
        *,
        warn_days: int,
        request_timeout: float,
    ) -> None:
        self._loader = app_config_loader
        self._logger = logger
        self._warn_days = warn_days
        self._request_timeout = request_timeout
        self._client: JIRA | None = None

    def client(self) -> JIRA:
        """Return the cached Jira client, building it on first use.

        ``AppConfigLoader.load`` raises ``AppConfigError`` (e.g. entry missing)
        which the plugin's ``_run`` maps to ``jira.not_configured``.
        """
        if self._client is None:
            config = self._loader.load()
            self._warn_if_expiring(config)
            self._client = self._build(config)
        return self._client

    def _build(self, config: JiraAppConfig) -> JIRA:
        # get_server_info=True is LOAD-BEARING: it populates deploymentType,
        # which gates the library's Cloud-only methods (enhanced_search_issues,
        # approximate_issue_count). Without it the gate SILENTLY returns None
        # instead of searching. One /serverInfo round-trip per cached build.
        return JIRA(
            server=config.base_url,
            basic_auth=(config.email, config.api_token),
            options={JIRA_OPTION_REST_API_VERSION: JIRA_REST_API_VERSION},
            get_server_info=True,
            timeout=self._request_timeout,
            logging=False,
        )

    def _warn_if_expiring(self, config: JiraAppConfig) -> None:
        warning = check_token_expiry(config.expires_at, datetime.now(UTC), self._warn_days)
        if warning is not None:
            self._logger.warning("%s: %s", warning.code, warning.message)
