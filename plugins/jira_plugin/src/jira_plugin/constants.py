"""Jira Cloud connector constants.

Single source of truth for every magic value: the pinned REST API version,
the address-book entry + field identifiers, the chain-consumed vault key, error
codes (``jira.*`` prefix), result types, and caps. No magic strings anywhere
else in the plugin.

Auth model (operator-ratified 2026-07-09, umbrella design §7): Jira Cloud +
HTTP basic-auth on a DEDICATED scoped Atlassian service account (account email +
API token). No OAuth 3LO, no browser flow, no callback server — durable headless
auth. The API token EXPIRES (Atlassian default 1yr, 1-365d configurable); the
plugin is expiry-aware (``check_token_expiry``) and warns loudly rather than
letting it lapse into a mystery 401. A personal-account token is a documented
drop-in fallback (same transport, same entry shape).
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import-time for the scoped vault key.

    The chain-consumed api_token vault key follows the
    ``<solet>.<plugin>.<credential>`` convention. Mirrors the fast-fail
    helper in g_suite_plugin.constants / schwab_market_data_plugin.constants /
    soundcloud_artist_studio_plugin.constants.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "jira_plugin.constants: SOLET_NAME env var is required to "
            "resolve the scoped api_token vault key.",
        )
    return name


_SOLET: Final[str] = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "jira_plugin"
PLUGIN_VERSION: Final[str] = "1.0.0"

# Blob storage namespace (attachment downloads + JQL result exports).
BLOB_NAMESPACE: Final[str] = "jira_plugin"

# ---------------------------------------------------------------------------
# Jira client options
# ---------------------------------------------------------------------------
# Pinned deliberately to REST API v2 (plain-text description/comment bodies).
# v3 uses ADF (Atlassian Document Format) JSON documents for those fields,
# which would leak ADF structure into verb params; v2 keeps bodies plain text.
JIRA_REST_API_VERSION: Final[str] = "2"
# Sub-key inside the address-book-resolved value that is NOT part of the entry
# but the client option dict; kept as a constant so plugin.yaml/client agree.
JIRA_OPTION_REST_API_VERSION: Final[str] = "rest_api_version"

DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
DEFAULT_TOKEN_EXPIRY_WARN_DAYS: Final[int] = 14

# ---------------------------------------------------------------------------
# Chain-consumed vault key — the api_token secret.
# ---------------------------------------------------------------------------
# The "jira_site" address-book entry's ``api_token`` field stores a
# ``vault::<solet>.default_address_book_plugin.jira_api_token`` reference.
# CHAIN-CONSUMED via ``resolve_with_secrets`` (never read directly under this
# plugin's identity), so it lives in the RESOLVER's namespace
# (``default_address_book_plugin``) — post-2026-06-07 vault namespace
# enforcement requires the key's ``<plugin>`` segment to equal the retrieving
# caller. Therefore NOT declared in get_required_vault_keys /
# get_declared_vault_keys (both return []). This plugin holds NO plugin-owned
# runtime vault keys. Canonical convention: VAULT_AND_ADDRESS_BOOK.md
# §"Pattern: Plugin Configuration via Address Book + Vault".
VAULT_KEY_API_TOKEN: Final[str] = (
    f"{_SOLET}.default_address_book_plugin.jira_api_token"
)

# ---------------------------------------------------------------------------
# Address book entry — Jira site identity + credentials
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "jira_site"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Jira Cloud site identity + credentials (base_url, service-account email, "
    "api_token vault-ref, token expires_at, scope_note) for jira_plugin."
)
ADDRESS_BOOK_FIELD_BASE_URL: Final[str] = "base_url"
ADDRESS_BOOK_FIELD_EMAIL: Final[str] = "email"
ADDRESS_BOOK_FIELD_API_TOKEN: Final[str] = "api_token"
ADDRESS_BOOK_FIELD_EXPIRES_AT: Final[str] = "expires_at"
ADDRESS_BOOK_FIELD_SCOPE_NOTE: Final[str] = "scope_note"

# ---------------------------------------------------------------------------
# Caps (business-data limits, 2026-08-03 operator revision —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md
# §0.1/§5.4). Jira EXITED the data-export requirement (operator veto, "no PII in Jira —
# just company internal accounts"): jql_search/list_comments are LIMITS-ONLY,
# g_suite-class -- inline returns, no containment gate, no caller-supplied
# path. §5.4 (paging-must-be-hidden ruling) also retired §5.3's "nothing to
# raise to" disposition: the connector now pages INTERNALLY across
# Atlassian's 100/call vendor ceiling (JQL_PAGE_SIZE / COMMENTS_PAGE_SIZE
# below) up to the effective row limit and returns ONE complete result --
# no caller-visible continuation token. That means there IS something for an
# override to raise (how many internal calls happen), so both verbs get the
# full acknowledge_default_limit_override/row_limit mechanism (§5), which
# they previously did not.
# ---------------------------------------------------------------------------
JQL_PAGE_SIZE: Final[int] = 100
COMMENTS_PAGE_SIZE: Final[int] = 100

# Effective-limit override pair (§5, §5.4) -- shared by both verbs. CAP=5,000
# is §5.4's doc-wide default for connectors with no separate bulk-export verb
# (zuora's LIST_ROW_LIMIT_CAP precedent; Claude-D landed marketo on the same
# number independently). Latency math (Day-required, §5.4): at DEFAULT_ROW_
# LIMIT, ceil(500/100)=5 internal calls; at ROW_LIMIT_CAP, ceil(5000/100)=50
# internal calls -- each a sequential HTTP round-trip inside one verb
# invocation, disclosed in both verbs' process descriptions.
DEFAULT_ROW_LIMIT: Final[int] = 500
ROW_LIMIT_CAP: Final[int] = 5_000
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"

# Defense-in-depth circuit breaker on the internal pagination loop --
# independent of ROW_LIMIT_CAP, bounds worst-case call count even if a future
# cap is raised well past today's 5,000 (mirrors zuora's
# _MAX_QUERY_MORE_CALLS=64 pattern; sized up since jira's cap/page-size ratio
# needs 50 calls at today's cap, so 100 gives real headroom). Never expected
# to bind in practice.
MAX_INTERNAL_CALLS: Final[int] = 100

# ---------------------------------------------------------------------------
# Error codes (jira.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
# Service / lifecycle
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_BLOB_STORAGE_NOT_AVAILABLE: Final[str] = "blob_storage_service_not_available"
# Config-load (address book entry resolution)
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"
ERROR_EXPIRES_AT_INVALID: Final[str] = "expires_at_invalid"

# jira.* — surfaced to callers of the Jira verbs
ERROR_NOT_CONFIGURED: Final[str] = "jira.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "jira.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "jira.auth_failed"
ERROR_PERMISSION_DENIED: Final[str] = "jira.permission_denied"
ERROR_NOT_FOUND: Final[str] = "jira.not_found"
ERROR_RATE_LIMITED: Final[str] = "jira.rate_limited"
# 400 class. A shared, verb-agnostic classifier cannot know a 400 is a bad JQL
# vs a bad create-field, so this is the general bad-request code (detail-allowed
# — a 400 always describes the caller's own query/fields). The per-verb process
# JSON for jql_search names malformed JQL as the common cause. (Deviation from
# the design's jira.malformed_jql, flagged to the coordinator.)
ERROR_BAD_REQUEST: Final[str] = "jira.bad_request"
ERROR_API_ERROR: Final[str] = "jira.api_error"
# Expiry-awareness signal code (loud structured warning; §7.3).
ERROR_TOKEN_EXPIRING: Final[str] = "jira.token_expiring"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_JQL_SEARCH: Final[str] = "jira_jql_search_result"
RESULT_TYPE_GET_ISSUE: Final[str] = "jira_get_issue_result"
RESULT_TYPE_CREATE_ISSUE: Final[str] = "jira_create_issue_result"
RESULT_TYPE_UPDATE_ISSUE: Final[str] = "jira_update_issue_result"
RESULT_TYPE_DELETE_ISSUE: Final[str] = "jira_delete_issue_result"
RESULT_TYPE_ADD_COMMENT: Final[str] = "jira_add_comment_result"
RESULT_TYPE_LIST_COMMENTS: Final[str] = "jira_list_comments_result"
RESULT_TYPE_LIST_TRANSITIONS: Final[str] = "jira_list_transitions_result"
RESULT_TYPE_TRANSITION_ISSUE: Final[str] = "jira_transition_issue_result"
RESULT_TYPE_DOWNLOAD_ATTACHMENT: Final[str] = "jira_download_attachment_result"
RESULT_TYPE_ADD_ATTACHMENT: Final[str] = "jira_add_attachment_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "jira_test_connection_result"
