"""Marketo plugin constants.

Single source of truth for every magic value: the address-book entry + field
identifiers, error codes (``marketo.*`` prefix), the Marketo REST error-code
map, result types, and caps. No magic strings anywhere else in the plugin.

Auth model: OAuth 2.0 "2-legged" client-credentials against a Marketo
LaunchPoint custom service (Admin > Integration > LaunchPoint). The token is
minted with an HTTP GET against ``<base_url>/identity/oauth/token`` — the
simplest of the platform's connector auth models alongside Zuora's. The
bearer token is short-lived (~3600s), re-mintable, and held ONLY in process
memory — never vaulted (the durable credential is the client_secret, not the
token).

Error model (the key divergence from zuora_plugin, sourced from
https://experienceleague.adobe.com/en/docs/marketo-developer/marketo/rest/error-codes,
2026-07-28): Marketo REST endpoints return HTTP 200 for almost every API-level
fault — the fault lives in the JSON envelope's ``success: false`` +
``errors: [{code, message}]``. Only the identity/token endpoint and true
transport faults (5xx, malformed JSON) use real HTTP status codes. See
``errors.py`` for the code -> (our error code, retryable) map built from this
table.
"""

import os
from typing import Final


def _solet_or_fail() -> str:
    """Resolve SOLET_NAME at import-time for the scoped vault key.

    Mirrors the fast-fail helper in salesforce_plugin.constants / zuora_plugin.constants.
    """
    name = os.environ.get("SOLET_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "marketo_plugin.constants: SOLET_NAME env var is "
            "required to resolve the scoped client_secret vault key.",
        )
    return name


_SOLET: Final[str] = _solet_or_fail()

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "marketo_plugin"
PLUGIN_VERSION: Final[str] = "1.1.0"

# ---------------------------------------------------------------------------
# Chain-consumed vault key — the OAuth client_secret.
# ---------------------------------------------------------------------------
# The "marketo_instance" address-book entry's ``client_secret`` field stores a
# ``vault::<solet>.default_address_book_plugin.marketo_client_secret``
# reference. CHAIN-CONSUMED via ``resolve_with_secrets`` (never read directly
# under this plugin's identity), so it lives in the RESOLVER's namespace —
# vault namespace enforcement requires the key's ``<plugin>`` segment to equal
# the retrieving caller. Therefore NOT declared in get_required_vault_keys /
# get_declared_vault_keys (both return []).
VAULT_KEY_CLIENT_SECRET: Final[str] = (
    f"{_SOLET}.default_address_book_plugin.marketo_client_secret"
)

# ---------------------------------------------------------------------------
# Address book entry — Marketo instance identity + credentials
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "marketo_instance"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Marketo Engage instance identity + credentials (base_url REST endpoint, "
    "client_id, client_secret vault-ref) for marketo_plugin."
)
ADDRESS_BOOK_FIELD_BASE_URL: Final[str] = "base_url"
ADDRESS_BOOK_FIELD_CLIENT_ID: Final[str] = "client_id"
ADDRESS_BOOK_FIELD_CLIENT_SECRET: Final[str] = "client_secret"

# The identity (token) endpoint lives under the same host as the REST base_url
# — Marketo does not split these across separate hosts.
IDENTITY_TOKEN_PATH: Final[str] = "/identity/oauth/token"

# ---------------------------------------------------------------------------
# HTTP client knobs
# ---------------------------------------------------------------------------
DEFAULT_REQUEST_TIMEOUT_SECONDS: Final[float] = 30.0
CONFIG_KEY_REQUEST_TIMEOUT_SECONDS: Final[str] = "request_timeout_seconds"
# Re-fetch the bearer this many seconds before its recorded expiry (clock-skew
# margin), matching zuora_plugin's http_client.
TOKEN_REFRESH_MARGIN_SECONDS: Final[float] = 30.0
DEFAULT_TOKEN_TTL_SECONDS: Final[float] = 3600.0

# Client-side throttle (D0.3 sync-verb migration, 2026-08-09 — the plugin's
# prior full seriality accidentally kept concurrency at 1; the background
# worker removes that, so these are now real limits, not inherited ones).
# Margin under Marketo's 615 "10 concurrent in-flight" cap.
MARKETO_MAX_CONCURRENT_CALLS: Final[int] = 8
# Margin under Marketo's 606 "100 calls/20s" cap.
MARKETO_RATE_WINDOW_SECONDS: Final[float] = 20.0
MARKETO_RATE_WINDOW_MAX_CALLS: Final[int] = 90

# ---------------------------------------------------------------------------
# Business-data limits + data-export migration (2026-08-02 —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md,
# §7.1). describe_lead_fields, get_leads, list_activity_types, get_activities,
# list_campaigns, and list_static_lists now ALWAYS write to a caller-supplied
# output_tsv_path — never records inline, at any size (the former blob-export/
# INLINE_BYTE_CAP branch is deleted, not lowered; blob storage retires from
# this plugin entirely). get_api_usage is UNCHANGED — small, bounded, no PII,
# not part of the six verbs §7.1 touches.
#
# Internal pagination is hidden by design (2026-08-03 hide-paging change,
# operator ruling "the paging is an implementation detail that should be
# hidden", design doc §5.4/§7.2 as amended, ruled doc-wide). get_leads, get_activities,
# list_campaigns, and list_static_lists now carry the standard §5
# acknowledge_default_limit_override/row_limit pair for the first time —
# reversing the original Tier-2 build's "nothing to bind on" reasoning, which
# held only for the 300/call VENDOR ceiling (still true, still un-raisable,
# see MARKETO_LIST_PAGE_ROW_CAP below) and not for the CUMULATIVE multi-call
# fetch these verbs now perform internally. See marketing_actions.py's module
# docstring for the full shape.
# ---------------------------------------------------------------------------
TSV_SUFFIX: Final[str] = ".tsv"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"
# Vendor per-call ceiling shared by get_leads/get_activities/list_campaigns/
# list_static_lists — VENDOR-IMPOSED, citation: design doc §3 census,
# "Get Lead Activities / list-style reads return 300/page server-side."
MARKETO_LIST_PAGE_ROW_CAP: Final[int] = 300
MIME_JSON: Final[str] = "application/json"

# §5 override friction — default/hard-cap for the cumulative internal fetch
# on get_leads/get_activities/list_campaigns/list_static_lists. 500/5,000
# are the design doc's doc-wide defaults (§5.4), not marketo-specific
# numbers — 5,000 matches zuora's LIST_ROW_LIMIT_CAP precedent exactly,
# ruled by Coordinator-Day 2026-08-03 for any connector with no separate
# bulk-export verb. Beyond the hard cap: no resumption — re-invoke with a
# narrower filter/date-range (§5.4), never a carried-forward token.
DEFAULT_ROW_LIMIT: Final[int] = 500
MARKETO_LIST_ROW_LIMIT_CAP: Final[int] = 5_000
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"

# Get Multiple Leads — filterValues + Describe: Marketo caps most filter
# batches at 300 values per call.
MAX_FILTER_VALUES: Final[int] = 300
# Sync Lead / Delete Lead / List membership: 300 records per batch call.
MAX_BATCH_RECORDS: Final[int] = 300
# Request Campaign / Trigger Campaign: Marketo caps triggered leads at 100 per call.
MAX_TRIGGER_LEADS: Final[int] = 100
# Merge Leads: Marketo caps losing leads at 25 per call (server-enforced since
# 2026-03-31 — over-cap calls get error 1080). When mergeInCRM=true, Marketo
# itself further restricts a CRM-synced merge to exactly ONE losing lead.
MAX_MERGE_LOSING_LEADS: Final[int] = 25
MAX_MERGE_LOSING_LEADS_CRM: Final[int] = 1

# Get Lead Activities: Marketo caps ``leadIds`` at 30 and ``activityTypeIds``
# at 10 per call, and returns 300 activity items per page. Verified 2026-07-28
# against the Adobe REST "Activities" reference, which states leadIds limits
# results to "up to 30 leads, supplied as a comma-separated list" and
# activityTypeIds accepts "up to ten activity type Ids as a comma-separated
# list". The 10-id cap is enforced here because an over-cap call otherwise
# fails server-side mid-remediation.
MAX_ACTIVITY_LEAD_IDS: Final[int] = 30
MAX_ACTIVITY_TYPE_IDS: Final[int] = 10
ACTIVITY_PAGING_TOKEN_PATH: Final[str] = "/rest/v1/activities/pagingtoken.json"
ACTIVITIES_PATH: Final[str] = "/rest/v1/activities.json"
ACTIVITY_TYPES_PATH: Final[str] = "/rest/v1/activities/types.json"
API_USAGE_PATH: Final[str] = "/rest/v1/stats/usage.json"

LEAD_ACTIONS: Final[frozenset[str]] = frozenset(
    {"createOrUpdate", "createOnly", "updateOnly", "createDuplicate"}
)
DEFAULT_LEAD_ACTION: Final[str] = "createOrUpdate"
# Adobe's Sync Leads contract defaults an omitted lookupField to email and
# describes id as the system-managed unique key. Both are identifiers rather
# than intended write targets for the read-only-field preflight.
DEFAULT_LEAD_LOOKUP_FIELD: Final[str] = "email"
LEAD_ID_FIELD: Final[str] = "id"
# Adobe's Get Leads documentation says these six fields are returned when the
# caller omits ``fields``. Evidence class: documented, not measured. Membership
# here does not claim that Marketo marks any field REST read-only; the live
# describe response remains the authority for that separate property.
GET_LEADS_DEFAULT_FIELDS: Final[frozenset[str]] = frozenset(
    {"id", "email", "updatedAt", "createdAt", "firstName", "lastName"}
)
LEADS_DESCRIBE_PATH: Final[str] = "/rest/v1/leads/describe.json"

# ---------------------------------------------------------------------------
# Error codes (marketo.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "marketo.export_path_refused"

ERROR_NOT_CONFIGURED: Final[str] = "marketo.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "marketo.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "marketo.auth_failed"
# The API user's Role is missing an "Access API" permission checkbox (e.g.
# Read-Write Person, Read-Only Activity, Read-Only/Read-Write Campaign,
# Execute Campaign) — a
# Users & Roles fix, NOT a bad/expired token. Distinct from ERROR_AUTH_FAILED
# (601/602) so the remediation text points at the right console screen.
ERROR_PERMISSION_DENIED: Final[str] = "marketo.permission_denied"
# The API user isn't assigned to the workspace/partition the request targets
# (Marketo code 1008) — a workspace/partition assignment problem, NOT an
# Access API Role permission. Distinct from ERROR_PERMISSION_DENIED because
# the fix is a different admin screen (workspace/partition user assignment,
# not Users & Roles > Roles).
ERROR_PARTITION_ACCESS_DENIED: Final[str] = "marketo.partition_access_denied"
ERROR_OBJECT_NOT_FOUND: Final[str] = "marketo.object_not_found"
ERROR_VALIDATION_FAILED: Final[str] = "marketo.validation_failed"
ERROR_RATE_LIMITED: Final[str] = "marketo.rate_limited"
ERROR_QUOTA_EXCEEDED: Final[str] = "marketo.daily_quota_exceeded"
ERROR_QUERY_FAILED: Final[str] = "marketo.query_failed"
ERROR_API_ERROR: Final[str] = "marketo.api_error"
# D0.3 sync-verb migration (2026-08-09): the four internally-paginating
# list/search verbs now dispatch as async jobs; this is check_marketo_job_status'
# not-found case, distinct from ERROR_OBJECT_NOT_FOUND (a Marketo-side 702).
ERROR_JOB_NOT_FOUND: Final[str] = "marketo.job_not_found"

# ---------------------------------------------------------------------------
# Marketo REST error-code map: numeric code (string) -> (our error code, retryable)
#
# Sourced 2026-07-28 from the official Adobe Marketo Engage REST API error
# codes reference (developers.marketo.com / experienceleague.adobe.com).
# 601/602 are handled specially by http_client (re-mint-once-and-retry) and
# only surface as ERROR_AUTH_FAILED if the retry itself fails. Any code not
# present here falls back to ERROR_API_ERROR (never a guess dressed as a
# specific classification).
# ---------------------------------------------------------------------------
MARKETO_ERROR_CODE_MAP: Final[dict[str, tuple[str, bool]]] = {
    # 6xx — transport/auth/quota class (response-level, request failed whole)
    "601": (ERROR_AUTH_FAILED, True),  # access token invalid
    "602": (ERROR_AUTH_FAILED, True),  # access token expired
    "603": (ERROR_PERMISSION_DENIED, False),  # access denied (Access API Role permission missing)
    "604": (ERROR_API_ERROR, True),  # request time-out / db contention
    "606": (ERROR_RATE_LIMITED, True),  # rate limit exceeded (100 calls/20s)
    "607": (ERROR_QUOTA_EXCEEDED, False),  # daily quota reached
    "609": (ERROR_INVALID_PARAMS, False),  # invalid JSON
    "610": (ERROR_API_ERROR, False),  # requested resource not found (bad URI)
    "612": (ERROR_INVALID_PARAMS, False),  # invalid content type
    "613": (ERROR_INVALID_PARAMS, False),  # invalid multipart request
    "615": (ERROR_RATE_LIMITED, True),  # concurrent access limit (10 in-flight)
    # 7xx — data/validation class
    "701": (ERROR_INVALID_PARAMS, False),  # field cannot be blank
    "702": (ERROR_OBJECT_NOT_FOUND, False),  # no data found
    "709": (ERROR_VALIDATION_FAILED, False),  # business rule violation
    "714": (ERROR_VALIDATION_FAILED, False),  # unable to find default record type (merge)
    # 1xxx — record-level class (usually inside a batch result's own `reasons`,
    # but the same codes appear at the top level for single-object verbs)
    "1001": (ERROR_VALIDATION_FAILED, False),  # invalid value type mismatch
    "1002": (ERROR_INVALID_PARAMS, False),  # missing required parameter
    "1003": (ERROR_VALIDATION_FAILED, False),  # invalid data for endpoint/mode
    "1004": (ERROR_OBJECT_NOT_FOUND, False),  # lead not found (updateOnly)
    "1005": (ERROR_VALIDATION_FAILED, False),  # lead already exists (createOnly)
    "1006": (ERROR_VALIDATION_FAILED, False),  # field not found
    "1007": (ERROR_VALIDATION_FAILED, False),  # multiple leads match lookup criteria
    "1008": (ERROR_PARTITION_ACCESS_DENIED, False),  # no access to partition
    "1013": (ERROR_OBJECT_NOT_FOUND, False),  # object not found by id
    "1018": (ERROR_VALIDATION_FAILED, False),  # native CRM integration blocks operation
    "1026": (ERROR_VALIDATION_FAILED, False),  # custom objects not enabled
    "1037": (ERROR_VALIDATION_FAILED, False),  # lead already in/past target status
    "1080": (ERROR_INVALID_PARAMS, False),  # merge leadIds batch exceeds the 25-id server cap
}
# Codes that trigger exactly-one token re-mint-and-retry before classification.
MARKETO_AUTH_RETRY_CODES: Final[frozenset[str]] = frozenset({"601", "602"})

# ---------------------------------------------------------------------------
# check_setup — read-only capability probes + their Access API permission
# mapping (verb, human label, permission-checkbox-name-or-None). A ``None``
# permission means the mapping is NOT confirmed against Marketo's own docs
# (researched 2026-07-28: "Read-Write Person" is confirmed for list
# *membership* writes, but the permission gating plain list *enumeration* is
# unpinned) — the remediation text for that probe must say "unconfirmed,
# check the Access API tree" rather than name a specific checkbox, so an
# operator never adds the WRONG permission chasing a guess.
# ---------------------------------------------------------------------------
CHECK_SETUP_PROBES: Final[tuple[tuple[str, str, str | None], ...]] = (
    ("describe_lead_fields", "Lead field schema (read)", "Read-Only Person"),
    ("get_leads", "Lead query (read)", "Read-Only Person"),
    ("list_activity_types", "Activity type catalog (read)", "Read-Only Activity"),
    ("get_api_usage", "Current-day API usage (read)", None),
    ("list_campaigns", "Campaign listing (read)", "Read-Only Campaign"),
    ("list_static_lists", "Static list listing (read)", None),
)
# Write/execute verbs whose Access API permission CANNOT be safely probed
# without performing the write/execute itself — check_setup reports these as
# unverified rather than guessing; any permission gap surfaces as
# marketo.permission_denied on first real use of the verb.
CHECK_SETUP_UNVERIFIED_WRITE_VERBS: Final[tuple[str, ...]] = (
    "create_or_update_leads",
    "delete_leads",
    "merge_leads",
    "add_leads_to_list",
    "remove_leads_from_list",
    "trigger_campaign",
)

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_CHECK_SETUP: Final[str] = "marketo_check_setup_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "marketo_test_connection_result"
RESULT_TYPE_DESCRIBE_LEAD_FIELDS: Final[str] = "marketo_describe_lead_fields_result"
RESULT_TYPE_GET_LEADS: Final[str] = "marketo_get_leads_result"
RESULT_TYPE_CREATE_OR_UPDATE_LEADS: Final[str] = "marketo_create_or_update_leads_result"
RESULT_TYPE_DELETE_LEADS: Final[str] = "marketo_delete_leads_result"
RESULT_TYPE_LIST_CAMPAIGNS: Final[str] = "marketo_list_campaigns_result"
RESULT_TYPE_TRIGGER_CAMPAIGN: Final[str] = "marketo_trigger_campaign_result"
RESULT_TYPE_LIST_STATIC_LISTS: Final[str] = "marketo_list_static_lists_result"
RESULT_TYPE_ADD_LEADS_TO_LIST: Final[str] = "marketo_add_leads_to_list_result"
RESULT_TYPE_REMOVE_LEADS_FROM_LIST: Final[str] = "marketo_remove_leads_from_list_result"
RESULT_TYPE_MERGE_LEADS: Final[str] = "marketo_merge_leads_result"
RESULT_TYPE_GET_ACTIVITIES: Final[str] = "marketo_get_activities_result"
RESULT_TYPE_LIST_ACTIVITY_TYPES: Final[str] = "marketo_list_activity_types_result"
RESULT_TYPE_GET_API_USAGE: Final[str] = "marketo_get_api_usage_result"
RESULT_TYPE_CHECK_MARKETO_JOB_STATUS: Final[str] = "marketo_check_job_status_result"

# ---------------------------------------------------------------------------
# D0.3 sync-verb migration (2026-08-09) — async job dispatch for the four
# internally-paginating list/search verbs whose vendor round-trips are the
# worst loop-hold shape in the corpus (get_leads, get_activities,
# list_campaigns, list_static_lists). See marketing_actions.py's module
# docstring for the prepare/execute split this enables.
# ---------------------------------------------------------------------------
MARKETO_ASYNC_ACTION_NAMES: Final[frozenset[str]] = frozenset(
    {"get_leads", "get_activities", "list_campaigns", "list_static_lists"}
)
PARAM_JOB_ID: Final[str] = "job_id"
