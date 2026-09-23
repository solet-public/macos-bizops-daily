"""Salesforce error classification — typed codes + topology-safe messages.

Full CLI delegation (operator-ratified 2026-07-14, replacing the sf-CLI
session-borrow executor — dead on current CLI releases because `sf org
display --json` now redacts `accessToken` unconditionally, verified live
against CLI 2.142.7): every verb shells to the `sf` CLI, which classifies its
own outcome one of two ways.

1. **CLI-level fault** — the CLI could not even reach the Salesforce API:
   binary missing, subprocess timeout, or the target org alias doesn't
   resolve to a live session (`sf org login web` never run, or revoked).
   These raise ``SalesforceServiceError`` directly from ``client.py`` with an
   ``sf.*`` code already assigned — nothing to classify here.
2. **REST-level fault** — the CLI reached the API and Salesforce rejected the
   call. The `--json`-enabled commands (`data query/get/delete`, `sobject
   describe`, `org display`) wrap this in a `{name, message, data:
   {errorCode, message}}` error envelope; the beta `api request rest` path
   (used by create/update/list_sobjects — see `record_actions.py`) has no
   `--json` support and instead prints the raw REST error body, a JSON array
   of `{errorCode, message}` dicts, on failure. Both shapes are normalized by
   `client.py`'s parsers into one ``SalesforceCliCallError(error_code,
   detail_message)`` before reaching this module.

TOPOLOGY HYGIENE (umbrella design §1.6/§2.4): auth/session/permission/
rate-limit classes NEVER surface the driver's raw detail message (Salesforce
error text is not guaranteed free of the org's own identifiers). Detail-
allowed classes (not-found/malformed-query) build their message from
``detail_message`` ONLY — the Salesforce RESPONSE BODY, which describes the
caller's own record/query, not our org host.
"""

from __future__ import annotations

from .constants import (
    ERROR_API_ERROR,
    ERROR_AUTH_FAILED,
    ERROR_MALFORMED_QUERY,
    ERROR_NOT_FOUND,
    ERROR_PERMISSION_DENIED,
    ERROR_RATE_LIMITED,
    ERROR_SESSION_EXPIRED,
)

# Salesforce REST errorCode strings -> this plugin's sf.* classification.
_ERROR_CODE_PERMISSION_DENIED: frozenset[str] = frozenset(
    {"INSUFFICIENT_ACCESS_OR_READONLY", "INSUFFICIENT_ACCESS"}
)
_ERROR_CODE_RATE_LIMITED: frozenset[str] = frozenset({"REQUEST_LIMIT_EXCEEDED"})
_ERROR_CODE_NOT_FOUND: frozenset[str] = frozenset({"NOT_FOUND"})
_ERROR_CODE_MALFORMED_QUERY: frozenset[str] = frozenset(
    {"MALFORMED_QUERY", "INVALID_FIELD", "INVALID_TYPE", "MALFORMED_ID"}
)
_ERROR_CODE_SESSION_EXPIRED: frozenset[str] = frozenset({"INVALID_SESSION_ID"})
_ERROR_CODE_AUTH_FAILED: frozenset[str] = frozenset({"INVALID_LOGIN"})

_LOGIN_HINT: str = "re-establish it with: sf org login web."


class SalesforceServiceError(Exception):
    """A typed plugin-internal fault — CLI-level (never reached the API) or
    infrastructure (e.g. blob storage unavailable at point-of-use).

    Carries an ``sf.*``/service code the plugin's ``_run`` surfaces verbatim.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SalesforceCliCallError(Exception):
    """A classified REST-level failure — the CLI reached the API and got an error.

    ``error_code`` is the Salesforce REST errorCode string (e.g.
    ``NOT_FOUND``); ``detail_message`` is the Salesforce response body's
    message. Built exclusively by ``client.py``'s envelope/REST-response
    parsers from the sf CLI's `--json` error envelope or the `api request
    rest` raw error array — never from a Python driver exception (there is
    no driver anymore).
    """

    def __init__(self, error_code: str, detail_message: str) -> None:
        super().__init__(detail_message)
        self.error_code = error_code
        self.detail_message = detail_message


def classify_salesforce_error(exc: Exception) -> tuple[str, str]:
    """Return ``(sf.* code, topology-safe message)``.

    Any exception other than ``SalesforceCliCallError`` is an unexpected
    local fault (a bug, not a classified Salesforce response) — it is never
    stringified into the message, only mapped to the generic catch-all.
    """
    if not isinstance(exc, SalesforceCliCallError):
        return ERROR_API_ERROR, "Salesforce API error (unexpected)."
    code = exc.error_code
    if code in _ERROR_CODE_PERMISSION_DENIED:
        return (
            ERROR_PERMISSION_DENIED,
            "Salesforce denied access. The integration user lacks permission for "
            "this object or operation.",
        )
    if code in _ERROR_CODE_RATE_LIMITED:
        return ERROR_RATE_LIMITED, "Salesforce API request limit exceeded. Retry after a short delay."
    if code in _ERROR_CODE_NOT_FOUND:
        return ERROR_NOT_FOUND, _detail("Salesforce resource not found", exc)
    if code in _ERROR_CODE_MALFORMED_QUERY:
        return ERROR_MALFORMED_QUERY, _detail("Salesforce rejected the SOQL query", exc)
    if code in _ERROR_CODE_SESSION_EXPIRED:
        return ERROR_SESSION_EXPIRED, f"Salesforce session expired and could not be renewed — {_LOGIN_HINT}"
    if code in _ERROR_CODE_AUTH_FAILED:
        return ERROR_AUTH_FAILED, f"Salesforce authentication failed — {_LOGIN_HINT}"
    # Catch-all: an unrecognized errorCode. Never stringify detail_message here —
    # an unclassified code means we don't know whether its text is topology-safe.
    return ERROR_API_ERROR, f"Salesforce API error ({code or 'unknown'})."


def _detail(prefix: str, exc: SalesforceCliCallError) -> str:
    """Build a detail message from the RESPONSE BODY only (never raw CLI output)."""
    text = exc.detail_message.strip()
    return f"{prefix}: {text}" if text else f"{prefix}."
