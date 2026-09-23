"""Zuora plugin constants.

Single source of truth for every magic value: the address-book entry + field
identifiers, error codes (``zuora.*`` prefix), result types, and caps. No
magic strings anywhere else in the plugin.

Auth model (2026-06-20 design, hardened to the wave-2 security posture):
OAuth 2.0 client-credentials — the simplest of the platform's connector auth
models. No browser, no callback server. The bearer token is short-lived
(~1h), re-mintable, and held ONLY in process memory — never vaulted (the
durable credential is the client_secret, not the token).
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import-time for the scoped vault key.

    Mirrors the fast-fail helper in salesforce_plugin.constants / jira_plugin.constants.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "zuora_plugin.constants: SOLET_NAME env var is "
            "required to resolve the scoped client_secret vault key.",
        )
    return name


_SOLET: Final[str] = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "zuora_plugin"
PLUGIN_VERSION: Final[str] = "1.1.0"

# ---------------------------------------------------------------------------
# Chain-consumed vault key — the OAuth client_secret.
# ---------------------------------------------------------------------------
# The "zuora_tenant" address-book entry's ``client_secret`` field stores a
# ``vault::<solet>.default_address_book_plugin.zuora_client_secret``
# reference. CHAIN-CONSUMED via ``resolve_with_secrets`` (never read directly
# under this plugin's identity), so it lives in the RESOLVER's namespace —
# post-2026-06-07 vault namespace enforcement requires the key's ``<plugin>``
# segment to equal the retrieving caller. Therefore NOT declared in
# get_required_vault_keys / get_declared_vault_keys (both return []).
VAULT_KEY_CLIENT_SECRET: Final[str] = (
    f"{_SOLET}.default_address_book_plugin.zuora_client_secret"
)

# ---------------------------------------------------------------------------
# Address book entry — Zuora tenant identity + credentials
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "zuora_tenant"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Zuora tenant identity + credentials (base_url environment selector, "
    "client_id, client_secret vault-ref) for zuora_plugin."
)
ADDRESS_BOOK_FIELD_BASE_URL: Final[str] = "base_url"
ADDRESS_BOOK_FIELD_CLIENT_ID: Final[str] = "client_id"
ADDRESS_BOOK_FIELD_CLIENT_SECRET: Final[str] = "client_secret"

# Known Zuora REST environments (base_url IS the environment selector).
ZUORA_BASE_URL_US_PRODUCTION: Final[str] = "https://rest.zuora.com"
ZUORA_BASE_URL_EU_PRODUCTION: Final[str] = "https://rest.eu.zuora.com"
ZUORA_BASE_URL_SANDBOX: Final[str] = "https://rest.apisandbox.zuora.com"

# ---------------------------------------------------------------------------
# HTTP client knobs
# ---------------------------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
CONFIG_KEY_REQUEST_TIMEOUT_SECONDS: Final[str] = "request_timeout_seconds"
# Re-fetch the bearer this many seconds before its recorded expiry (clock-skew
# margin), matching the design appendix's token-manager sketch.
TOKEN_REFRESH_MARGIN_SECONDS: Final[float] = 30.0

# ---------------------------------------------------------------------------
# Caps + export (business-data limits + data-export migration, 2026-08-02 —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
# data_query, bulk_export, list_subscriptions, and list_invoices now ALWAYS
# write to a caller-supplied output_tsv_path — never records inline, at any
# size (07-29 data-export requirement, unconditional; the former INLINE_BYTE_CAP/blob-export
# branch is deleted, not lowered). DEFAULT_ROW_LIMIT is the fetch ceiling
# absent an explicit, acknowledged override, matching salesforce/postgres's
# reconciled default — zuora has no vendor-imposed ceiling below 500 for any
# of these four verbs. get_object/get_invoice (single-record fetch-by-id,
# §1.2) are unaffected and stay inline.
# ---------------------------------------------------------------------------
DEFAULT_ROW_LIMIT: Final[int] = 500

# data_query's override ceiling — a single /v1/action/query call comfortably
# covers this (vendor per-call cap is 2000, see ZUORA_QUERY_PAGE_ROW_CAP), so
# data_query never needs the queryMore loop. For pulls beyond this, use
# bulk_export (same override mechanism, higher hard cap, queryMore-driven).
DATA_QUERY_MAX_ROWS_CAP: Final[int] = 1000

# bulk_export's override ceiling — the N>>500 route (§7.2 Pattern A). Reachable
# now via the queryMore continuation loop (billing_actions._run_zoql_query);
# OURS-ARBITRARY, same class as postgres's EXPORT_ROW_CAP / salesforce's
# SOQL_EXPORT_ROW_CAP, not a vendor ceiling.
BULK_EXPORT_ROW_CAP: Final[int] = 50_000

# list_subscriptions / list_invoices override ceiling — Pattern B (caller's
# script loops the ordinary read under the override), matching jira's
# list_comments and marketo's get_leads. No dedicated bulk verb exists or is
# being built for either (§7.3); this is the row_limit hard cap for the
# internal per-account pagination loop.
LIST_ROW_LIMIT_CAP: Final[int] = 5_000

# Vendor per-call ceiling on Zuora's ZOQL query endpoint (POST /v1/action/query,
# POST /v1/action/queryMore) — VENDOR-IMPOSED, citation: Zuora v1 API reference,
# operationId Action_POSTquery, "Limitations": "The number of records returned
# is limited to 2000 records." Continuation is a SEPARATE queryMore call keyed
# on the queryLocator the query response returns whenever done=false.
ZUORA_QUERY_PAGE_ROW_CAP: Final[int] = 2000

# Vendor per-call ceiling on Zuora's page/pageSize-paginated list endpoints
# (POST /v1/subscriptions/accounts/{account-key} today; list_invoices' legacy
# endpoint's own pagination support is unconfirmed, see billing_actions'
# module docstring) — VENDOR-IMPOSED, citation: Zuora v1 API reference,
# component GLOBAL_REQUEST_pageSize, "maximum: 40, default: 20" (operationId
# GET_SubscriptionsByAccount references this component directly).
ZUORA_LIST_PAGE_SIZE_MAX: Final[int] = 40

TSV_SUFFIX: Final[str] = ".tsv"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"

# ---------------------------------------------------------------------------
# Override friction (§5) — required together or not at all; absent means the
# effective limit is DEFAULT_ROW_LIMIT.
# ---------------------------------------------------------------------------
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"

# ---------------------------------------------------------------------------
# Object types this connector CRUDs (Object/Actions API).
# ---------------------------------------------------------------------------
SUPPORTED_OBJECT_TYPES: Final[frozenset[str]] = frozenset(
    {"Account", "Subscription", "Invoice", "Payment", "Product"}
)

# ---------------------------------------------------------------------------
# Error codes (zuora.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"

ERROR_NOT_CONFIGURED: Final[str] = "zuora.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "zuora.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "zuora.auth_failed"
ERROR_OBJECT_NOT_FOUND: Final[str] = "zuora.object_not_found"
ERROR_VALIDATION_FAILED: Final[str] = "zuora.validation_failed"
ERROR_RATE_LIMITED: Final[str] = "zuora.rate_limited"
ERROR_QUERY_FAILED: Final[str] = "zuora.query_failed"
ERROR_API_ERROR: Final[str] = "zuora.api_error"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "zuora.export_path_refused"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_DATA_QUERY: Final[str] = "zuora_data_query_result"
RESULT_TYPE_GET_OBJECT: Final[str] = "zuora_get_object_result"
RESULT_TYPE_CREATE_OBJECT: Final[str] = "zuora_create_object_result"
RESULT_TYPE_UPDATE_OBJECT: Final[str] = "zuora_update_object_result"
RESULT_TYPE_LIST_SUBSCRIPTIONS: Final[str] = "zuora_list_subscriptions_result"
RESULT_TYPE_GET_INVOICE: Final[str] = "zuora_get_invoice_result"
RESULT_TYPE_LIST_INVOICES: Final[str] = "zuora_list_invoices_result"
RESULT_TYPE_BULK_EXPORT: Final[str] = "zuora_bulk_export_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "zuora_test_connection_result"
