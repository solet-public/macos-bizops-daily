#!/usr/bin/env python3
"""Record CRUD + classification smoke tests for salesforce_plugin.

Hermetic — a ``MagicMock`` standing in for ``SalesforceCliExecutor``
(``run_json``/``run_rest``/``api_version`` mocked directly), no live org, no
subprocess.

Exercises:
  1. get_record — via `data get record --record-id`, attributes stripped,
     optional fields trims client-side (keeping Id)
  2. describe_sobject — via `sobject describe`, trimmed field shape
  3. list_sobjects — via `run_rest` GET on the global describe endpoint
     (beta command — the only CLI surface with name+label together),
     name/label shape
  4. create_record — via `run_rest` POST with a JSON body; id/success shape;
     rejects empty fields
  5. update_record — via `run_rest` PATCH with a JSON body; rejects empty
     fields; a 204 (None) response still reports success
  6. delete_record — via `data delete record --record-id`
  7. TOPOLOGY-LEAK (SECURITY): auth/session-expired/permission classes
     classify to a GENERIC message that NEVER contains the CLI's raw detail
     text; not-found/malformed-query classes carry response-body detail
  8. plugin-level: a SalesforceCliCallError raised by a verb classifies
     cleanly through `_run` (no retry mechanism anymore — the CLI manages
     its own credential refresh)
  9. EDGE parity: validate_edge_process_provider raises nothing

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_records.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin import record_actions  # noqa: E402
from salesforce_plugin.errors import (  # noqa: E402
    SalesforceCliCallError,
    classify_salesforce_error,
)
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

_ORG_HOST_MARKER = "SECRET-ORG-myorg.my.salesforce.com"

_passed = 0
_failed: list[str] = []


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _fake_executor(*, api_version: str = "62.0") -> MagicMock:
    executor = MagicMock()
    executor.api_version = api_version
    return executor


def test_get_record() -> None:
    executor = _fake_executor()
    executor.run_json.return_value = {"attributes": {"type": "Account"}, "Id": "001x", "Name": "Acme"}
    result = record_actions.get_record(executor, {"sobject": "Account", "id": "001x"})
    _assert("attributes stripped", "attributes" not in result["record"])
    _assert("record fields carried", result["record"]["Name"] == "Acme")
    argv = executor.run_json.call_args.args[0]
    _assert(
        "invoked via data get record --record-id",
        argv == ["data", "get", "record", "--sobject", "Account", "--record-id", "001x"],
        str(argv),
    )


def test_get_record_with_fields() -> None:
    executor = _fake_executor()
    executor.run_json.return_value = {
        "attributes": {"type": "Account"},
        "Id": "001x",
        "Name": "Acme",
        "Website": "acme.example.com",
    }
    result = record_actions.get_record(executor, {"sobject": "Account", "id": "001x", "fields": ["Name"]})
    _assert("trimmed to requested field + Id", result["record"] == {"Id": "001x", "Name": "Acme"}, str(result))


def test_describe_sobject() -> None:
    executor = _fake_executor()
    executor.run_json.return_value = {
        "fields": [
            {"name": "Name", "type": "string", "label": "Account Name", "nillable": False, "updateable": True},
            {"name": "Id", "type": "id", "label": "Record ID", "nillable": False, "updateable": False},
        ]
    }
    result = record_actions.describe_sobject(executor, {"sobject": "Account"})
    _assert("field name/type carried", result["fields"][0]["name"] == "Name")
    _assert("nillable/updateable carried", result["fields"][0]["updateable"] is True)
    argv = executor.run_json.call_args.args[0]
    _assert("invoked via sobject describe", argv == ["sobject", "describe", "--sobject", "Account"], str(argv))


def test_list_sobjects() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = {
        "sobjects": [
            {"name": "Account", "label": "Account", "custom": False},
            {"name": "Contact", "label": "Contact", "custom": False},
        ]
    }
    result = record_actions.list_sobjects(executor, {})
    _assert(
        "sobjects shape",
        result["sobjects"] == [{"name": "Account", "label": "Account"}, {"name": "Contact", "label": "Contact"}],
    )
    method, path = executor.run_rest.call_args.args
    _assert("GET on the global describe endpoint", method == "GET" and path.endswith("/sobjects/"), path)


def test_create_record() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = {"id": "001new", "success": True, "errors": []}
    result = record_actions.create_record(executor, {"sobject": "Account", "fields": {"Name": "Acme"}})
    _assert("new id carried", result["id"] == "001new")
    _assert("success True", result["success"] is True)
    method, path = executor.run_rest.call_args.args
    _assert("POST to the sobject collection endpoint", method == "POST" and path.endswith("/sobjects/Account"), path)
    _assert("body carries fields verbatim", executor.run_rest.call_args.kwargs.get("body") == {"Name": "Acme"})
    raised = False
    try:
        record_actions.create_record(executor, {"sobject": "Account", "fields": {}})
    except ValueError:
        raised = True
    _assert("empty fields rejected", raised)


def test_update_record() -> None:
    executor = _fake_executor()
    executor.run_rest.return_value = None  # 204 No Content
    result = record_actions.update_record(executor, {"sobject": "Account", "id": "001x", "fields": {"Name": "New"}})
    _assert("update success True even on empty (204) response", result["success"] is True)
    method, path = executor.run_rest.call_args.args
    _assert("PATCH to the record endpoint", method == "PATCH" and path.endswith("/sobjects/Account/001x"), path)
    _assert("body carries fields verbatim", executor.run_rest.call_args.kwargs.get("body") == {"Name": "New"})
    raised = False
    try:
        record_actions.update_record(executor, {"sobject": "Account", "id": "001x", "fields": {}})
    except ValueError:
        raised = True
    _assert("empty fields rejected", raised)


def test_delete_record() -> None:
    executor = _fake_executor()
    executor.run_json.return_value = {"id": "001x", "success": True}
    result = record_actions.delete_record(executor, {"sobject": "Account", "id": "001x"})
    _assert("delete success True", result["success"] is True)
    argv = executor.run_json.call_args.args[0]
    _assert(
        "invoked via data delete record --record-id",
        argv == ["data", "delete", "record", "--sobject", "Account", "--record-id", "001x"],
        str(argv),
    )


def test_classify_topology_leak() -> None:
    auth_err = SalesforceCliCallError("INVALID_LOGIN", f"invalid_client_id for {_ORG_HOST_MARKER}")
    code, message = classify_salesforce_error(auth_err)
    _assert("auth-class -> sf.auth_failed", code == "sf.auth_failed")
    _assert("auth message hides org host marker", _ORG_HOST_MARKER not in message, message)

    expired_err = SalesforceCliCallError("INVALID_SESSION_ID", f"session expired on {_ORG_HOST_MARKER}")
    code, message = classify_salesforce_error(expired_err)
    _assert("expired-session-class -> sf.session_expired", code == "sf.session_expired")
    _assert("expired message hides org host marker", _ORG_HOST_MARKER not in message, message)

    perm_err = SalesforceCliCallError("INSUFFICIENT_ACCESS_OR_READONLY", f"denied on {_ORG_HOST_MARKER}")
    code, message = classify_salesforce_error(perm_err)
    _assert("permission-class -> sf.permission_denied", code == "sf.permission_denied")
    _assert("permission message hides org host marker", _ORG_HOST_MARKER not in message, message)

    not_found_err = SalesforceCliCallError("NOT_FOUND", "Account ID 001x not found")
    code, message = classify_salesforce_error(not_found_err)
    _assert("not-found -> sf.not_found", code == "sf.not_found")
    _assert("not-found carries response-body detail", "001x" in message, message)

    malformed_err = SalesforceCliCallError("MALFORMED_QUERY", "unexpected token")
    code, message = classify_salesforce_error(malformed_err)
    _assert("malformed-query -> sf.malformed_query", code == "sf.malformed_query")

    unexpected_err = RuntimeError(f"a local bug near {_ORG_HOST_MARKER}")
    code, message = classify_salesforce_error(unexpected_err)
    _assert("unrecognized exception -> sf.api_error catch-all", code == "sf.api_error", code)
    _assert("catch-all never stringifies the raw exception", _ORG_HOST_MARKER not in message, message)


def test_plugin_level_classifies_cli_call_error() -> None:
    plugin = SalesforcePlugin()
    plugin.logger = None
    plugin._cli_executor = MagicMock()  # noqa: SLF001 — smoke reaches internal state directly

    def _produce(_executor: Any) -> dict[str, Any]:
        raise SalesforceCliCallError("NOT_FOUND", "Account ID 001x not found")

    result = plugin._run(_produce, "test_verb")  # noqa: SLF001
    _assert("plugin classifies a REST-level fault cleanly", result["action_status"] == "error")
    _assert("classified code surfaces", result["error"]["code"] == "sf.not_found", result["error"])


def test_edge_parity() -> None:
    from ananta.core.plugins.action_discovery import discover_actions
    from ananta.core.process_registry.plugin_registration_validator import (
        PluginRegistrationValidator,
    )

    plugin = SalesforcePlugin()
    actions = discover_actions(plugin)
    raised = None
    try:
        PluginRegistrationValidator().validate_edge_process_provider(
            "salesforce_plugin", plugin, actions
        )
    except Exception as exc:  # FrameworkError on mismatch
        raised = exc
    _assert("EDGE parity: validator raises nothing", raised is None, str(raised))
    _assert("all 15 verbs discovered", len(actions) == 15)


def main() -> int:
    print("\nsalesforce_plugin record CRUD smoke tests")
    print("=" * 47)
    test_get_record()
    test_get_record_with_fields()
    test_describe_sobject()
    test_list_sobjects()
    test_create_record()
    test_update_record()
    test_delete_record()
    test_classify_topology_leak()
    test_plugin_level_classifies_cli_call_error()
    test_edge_parity()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All record CRUD smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
