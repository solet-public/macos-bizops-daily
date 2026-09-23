"""Salesforce plugin constants.

Single source of truth for every magic value: the address-book entry + field
identifiers, the sf CLI invocation knobs, the pinned API version, error codes
(``sf.*`` prefix), result types, and caps. No magic strings anywhere else in
the plugin.

Auth model (operator-ratified 2026-07-14, full CLI delegation — replacing
the sf-CLI session-borrow client factory, dead on current CLI releases
because `sf org display --json` now redacts `accessToken` unconditionally):
every verb shells out to the `sf` CLI itself. The durable credential is the
CLI's own keychain-backed refresh token, established once via
``sf org login web`` — the platform stores NO Salesforce secret of its own,
and no access token of any kind ever enters this process.
"""

from typing import Final

# ---------------------------------------------------------------------------
# Plugin identity
# ---------------------------------------------------------------------------
PLUGIN_NAME: Final[str] = "salesforce_plugin"
PLUGIN_VERSION: Final[str] = "3.0.0"

# ---------------------------------------------------------------------------
# Address book entry — which CLI org this plugin is allowed to talk to
# ---------------------------------------------------------------------------
ADDRESS_BOOK_ENTRY_NAME: Final[str] = "salesforce_org"
ADDRESS_BOOK_ENTRY_TYPE: Final[str] = "api"
ADDRESS_BOOK_ENTRY_DESCRIPTION: Final[str] = (
    "Salesforce org binding for salesforce_plugin: target_org (the sf CLI "
    "alias or username every verb is invoked against) and instance_host (the "
    "pinned my-domain host that alias must resolve to)."
)
ADDRESS_BOOK_FIELD_TARGET_ORG: Final[str] = "target_org"
ADDRESS_BOOK_FIELD_INSTANCE_HOST: Final[str] = "instance_host"

# ---------------------------------------------------------------------------
# sf CLI invocation
# ---------------------------------------------------------------------------
# Path to the sf binary. The default resolves via PATH; deployments where the
# platform process's PATH does not carry the CLI (LaunchAgent) pin an absolute
# path via the plugin config key below.
CONFIG_KEY_SF_CLI_PATH: Final[str] = "sf_cli_path"
DEFAULT_SF_CLI_PATH: Final[str] = "sf"
SF_CLI_TIMEOUT_SECONDS: Final[float] = 30.0

# ---------------------------------------------------------------------------
# API version pin (never floats — an operator override still resolves
# through this same config key, plugin.yaml's `api_version`).
# ---------------------------------------------------------------------------
DEFAULT_API_VERSION: Final[str] = "62.0"
CONFIG_KEY_API_VERSION: Final[str] = "api_version"

# ---------------------------------------------------------------------------
# Caps + export (business-data limits + data-export migration, 2026-08-02 —
# workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md).
# Both soql_query and export_soql now ALWAYS write to a caller-supplied path
# (07-29 data-export requirement, unconditional — the former INLINE_BYTE_CAP/inline-return
# branch is deleted, not lowered: no record-read verb returns record values
# inline at any size). DEFAULT_ROW_LIMIT is the fetch ceiling absent an
# explicit, acknowledged override; SOQL_MAX_RECORDS_CAP is soql_query's
# override ceiling, SOQL_EXPORT_ROW_CAP is export_soql's (the "N>>500"
# Pattern-A route, §7.2) — both are pushed into the sf CLI's own
# SF_ORG_MAX_QUERY_LIMIT env override (soql_actions._run_soql), never
# fetch-everything-then-truncate.
# ---------------------------------------------------------------------------
DEFAULT_ROW_LIMIT: Final[int] = 500
SOQL_MAX_RECORDS_CAP: Final[int] = 1000
# SOQL_EXPORT_ROW_CAP reconciled per Reviewer-D's §3.2 adjudication + Dawn's
# ruling (2026-08-02): the number is REAL and REACHABLE (jsforce autoFetch
# pages through the REST API's own nextRecordsUrl up to the passed
# SF_ORG_MAX_QUERY_LIMIT), but its attribution is OURS-ARBITRARY, not a
# Salesforce-imposed ceiling — 50,000 is the Apex governor limit (SOQL run
# from inside Apex code), a different call path this plugin never uses (it
# shells to the sf CLI -> jsforce REST client, whose actual vendor fact is a
# 2,000/call REST query batch size with no vendor total ceiling). Never cite
# 50,000 as a Salesforce limit in a process description; cite the 2,000/call
# REST fact if a vendor number is wanted at all.
SOQL_EXPORT_ROW_CAP: Final[int] = 50_000
TSV_SUFFIX: Final[str] = ".tsv"
CSV_SUFFIX: Final[str] = ".csv"
CONFIG_KEY_EXPORT_ALLOWED_ROOTS: Final[str] = "export_allowed_roots"
# Bulk v2 ingest input containment — the read-side mirror of
# export_allowed_roots (refuse-all default; realpath+commonpath containment
# via import_containment.py). See knowledge_base/01_salesforce_overview.md
# "Bulk v2 ingest" section for the full posture.
CONFIG_KEY_IMPORT_ALLOWED_ROOTS: Final[str] = "import_allowed_roots"

# ---------------------------------------------------------------------------
# Bulk v2 ingest — operation set (hardDelete is explicitly EXCLUDED: bypasses
# the recycle bin, needs an org permission, and is not obviously inside the
# RATIFY-2 acceptable-loss ratification — 2026-08-24 plan §1).
# ---------------------------------------------------------------------------
BULK_INGEST_OPERATIONS: Final[frozenset[str]] = frozenset({"insert", "update", "upsert", "delete"})
BULK_INGEST_HARD_DELETE_OPERATION: Final[str] = "hardDelete"
BULK_JOB_ABORT_STATE: Final[str] = "Aborted"

# ---------------------------------------------------------------------------
# Override friction (§5) — required together or not at all; absent means the
# effective limit is DEFAULT_ROW_LIMIT.
# ---------------------------------------------------------------------------
PARAM_ACKNOWLEDGE_OVERRIDE: Final[str] = "acknowledge_default_limit_override"
PARAM_ROW_LIMIT: Final[str] = "row_limit"

# ---------------------------------------------------------------------------
# Error codes (sf.* prefix — surfaced to callers of the verbs)
# ---------------------------------------------------------------------------
ERROR_ADDRESS_BOOK_NOT_AVAILABLE: Final[str] = "address_book_service_not_available"
ERROR_ADDRESS_BOOK_ENTRY_MISSING: Final[str] = "address_book_entry_missing"
ERROR_ADDRESS_BOOK_ENTRY_INCOMPLETE: Final[str] = "address_book_entry_incomplete"

ERROR_NOT_CONFIGURED: Final[str] = "sf.not_configured"
ERROR_INVALID_PARAMS: Final[str] = "sf.invalid_params"
ERROR_AUTH_FAILED: Final[str] = "sf.auth_failed"
ERROR_SESSION_EXPIRED: Final[str] = "sf.session_expired"
ERROR_PERMISSION_DENIED: Final[str] = "sf.permission_denied"
ERROR_NOT_FOUND: Final[str] = "sf.not_found"
ERROR_MALFORMED_QUERY: Final[str] = "sf.malformed_query"
ERROR_RATE_LIMITED: Final[str] = "sf.rate_limited"
ERROR_API_ERROR: Final[str] = "sf.api_error"
ERROR_EXPORT_PATH_REFUSED: Final[str] = "sf.export_path_refused"
ERROR_IMPORT_PATH_REFUSED: Final[str] = "sf.import_path_refused"
ERROR_PROVISION_CONSENT_REQUIRED: Final[str] = "sf.provision_consent_required"

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------
RESULT_TYPE_SOQL_QUERY: Final[str] = "salesforce_soql_query_result"
RESULT_TYPE_EXPORT_SOQL: Final[str] = "salesforce_export_soql_result"
RESULT_TYPE_GET_RECORD: Final[str] = "salesforce_get_record_result"
RESULT_TYPE_DESCRIBE_SOBJECT: Final[str] = "salesforce_describe_sobject_result"
RESULT_TYPE_LIST_SOBJECTS: Final[str] = "salesforce_list_sobjects_result"
RESULT_TYPE_CREATE_RECORD: Final[str] = "salesforce_create_record_result"
RESULT_TYPE_UPDATE_RECORD: Final[str] = "salesforce_update_record_result"
RESULT_TYPE_DELETE_RECORD: Final[str] = "salesforce_delete_record_result"
RESULT_TYPE_TEST_CONNECTION: Final[str] = "salesforce_test_connection_result"
RESULT_TYPE_PROBE_CLI: Final[str] = "salesforce_probe_cli_result"
RESULT_TYPE_PROVISION_CLI: Final[str] = "salesforce_provision_cli_result"
RESULT_TYPE_BULK_INGEST_SUBMIT: Final[str] = "salesforce_bulk_ingest_submit_result"
RESULT_TYPE_BULK_JOB_STATUS: Final[str] = "salesforce_bulk_job_status_result"
RESULT_TYPE_BULK_JOB_RESULTS: Final[str] = "salesforce_bulk_job_results_result"
RESULT_TYPE_BULK_JOB_ABORT: Final[str] = "salesforce_bulk_job_abort_result"
