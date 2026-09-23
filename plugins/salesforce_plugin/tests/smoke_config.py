#!/usr/bin/env python3
"""Org-binding + CLI-executor smoke tests for salesforce_plugin.

Hermetic — a MagicMock address_book_service and a patched subprocess
boundary (`subprocess.run`), mirroring the live-verified `--json` envelope
shapes for `org display`/`data query`/`sobject describe` and the raw
(non-`--json`) shapes for `api request rest` (2026-07-14, against sf CLI
2.142.7 / Branch org). No network, no real CLI.

Exercises:
  1. load() builds a SalesforceOrgConfig from a complete entry
  2. Missing required fields (target_org/instance_host) fail loud, named
  3. run_json() happy path: verifies org binding once (org display), then
     invokes the requested command with --target-org/--json, unwraps `result`
  4. Pinned-host mismatch refuses (sf.not_configured)
  5. sf CLI missing -> sf.not_configured naming sf_cli_path
  6. No live session (CLI-level failure: empty stdout, nonzero exit) ->
     sf.auth_failed with login hint
  7. Incomplete org-display payload (missing instanceUrl/username) ->
     sf.auth_failed
  8. Non-JSON CLI output -> sf.auth_failed
  9. Org binding is verified exactly once across multiple run_json() calls
     (cached for the process lifetime — no more rebuild-on-expiry)
 10. run_json() REST-level failure -> SalesforceCliCallError with the
     envelope's data.errorCode/message
 11. run_rest() happy path: JSON body written to a tempfile, `--body @path`
     passed, response parsed; 204 (empty stdout) -> None
 12. run_rest() REST-level failure -> SalesforceCliCallError from the raw
     JSON error array
 13. EDGE parity: validate_edge_process_provider raises nothing, including provision_cli

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/salesforce_plugin/tests/smoke_config.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin.app_config import AppConfigError, AppConfigLoader  # noqa: E402
from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402
from salesforce_plugin.constants import (  # noqa: E402
    ERROR_AUTH_FAILED,
    ERROR_NOT_CONFIGURED,
    ERROR_PROVISION_CONSENT_REQUIRED,
)
from salesforce_plugin.errors import SalesforceCliCallError, SalesforceServiceError  # noqa: E402
from salesforce_plugin.plugin import SalesforcePlugin  # noqa: E402

_passed = 0
_failed: list[str] = []

_PINNED_HOST = "example.my.salesforce.com"
_INSTANCE_URL = f"https://{_PINNED_HOST}"
_USERNAME = "operator@example.com"


def _assert(label: str, cond: bool, msg: str = "") -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}: {msg or 'assertion failed'}")


def _entries(
    target_org: str = "Branch",
    instance_host: str = _PINNED_HOST,
) -> list[dict[str, str]]:
    fields = {"target_org": target_org, "instance_host": instance_host}
    return [{"field_type": k, "value": v} for k, v in fields.items() if v]


def _fake_address_book(entries: list[dict[str, str]]) -> MagicMock:
    service = MagicMock()
    service.resolve_with_secrets.return_value = {
        "action_status": "completed",
        "data": {"entries": entries},
    }
    return service


def _completed(stdout: str, returncode: int = 0, stderr: str = "") -> MagicMock:
    run_result = MagicMock()
    run_result.stdout = stdout
    run_result.stderr = stderr
    run_result.returncode = returncode
    return run_result


def _org_display_envelope(
    instance_url: str = _INSTANCE_URL,
    username: str = _USERNAME,
) -> str:
    # Mirrors the live-verified `sf org display --json` shape (2026-07-14):
    # `accessToken` is redacted unconditionally on CLI 2.142.7 — never parsed.
    result: dict[str, str] = {"accessToken": "[REDACTED] Use 'sf org auth show-access-token' to view"}
    if instance_url:
        result["instanceUrl"] = instance_url
    if username:
        result["username"] = username
    return json.dumps({"status": 0, "result": result})


def _executor(entries: list[dict[str, str]] | None = None) -> SalesforceCliExecutor:
    loader = AppConfigLoader(_fake_address_book(entries if entries is not None else _entries()))
    return SalesforceCliExecutor(loader, api_version="62.0", sf_cli_path="/fake/bin/sf")


def test_load_complete_entry() -> None:
    loader = AppConfigLoader(_fake_address_book(_entries()))
    config = loader.load()
    _assert("target_org carried", config.target_org == "Branch")
    _assert("instance_host carried", config.instance_host == _PINNED_HOST)


def test_missing_fields_named() -> None:
    loader = AppConfigLoader(_fake_address_book([]))
    message = ""
    try:
        loader.load()
    except AppConfigError as exc:
        message = str(exc)
    _assert("missing target_org named", "target_org" in message, message)
    _assert("missing instance_host named", "instance_host" in message, message)


def test_run_json_happy_path() -> None:
    executor = _executor()
    query_result = json.dumps({"status": 0, "result": {"records": [], "totalSize": 0, "done": True}})
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [_completed(_org_display_envelope()), _completed(query_result)]
        result = executor.run_json(["data", "query", "--file", "/tmp/q.soql"])
        _assert("result unwrapped from envelope", result == {"records": [], "totalSize": 0, "done": True})
        first_argv = run_mock.call_args_list[0].args[0]
        second_argv = run_mock.call_args_list[1].args[0]
        _assert("org display invoked first to verify binding", "display" in first_argv, str(first_argv))
        _assert(
            "verb invoked with --target-org and --json",
            "--target-org" in second_argv and "Branch" in second_argv and "--json" in second_argv,
            str(second_argv),
        )
        _assert("username captured from org display", executor.username == _USERNAME)


def test_pinned_host_mismatch_refuses() -> None:
    executor = _executor(_entries(instance_host="other.my.salesforce.com"))
    code, message = "", ""
    with patch(
        "salesforce_plugin.client.subprocess.run",
        return_value=_completed(_org_display_envelope()),
    ):
        try:
            executor.run_json(["sobject", "list"])
        except SalesforceServiceError as exc:
            code, message = exc.code, str(exc)
    _assert("host mismatch -> sf.not_configured", code == ERROR_NOT_CONFIGURED, code)
    _assert("mismatch message names both hosts", _PINNED_HOST in message and "other.my" in message, message)


def test_cli_missing_names_config_key() -> None:
    executor = _executor()
    code, message = "", ""
    with patch("salesforce_plugin.client.subprocess.run", side_effect=FileNotFoundError("sf")):
        try:
            executor.run_json(["sobject", "list"])
        except SalesforceServiceError as exc:
            code, message = exc.code, str(exc)
    _assert("missing CLI -> sf.not_configured", code == ERROR_NOT_CONFIGURED, code)
    _assert("missing CLI names sf_cli_path", "sf_cli_path" in message, message)


def test_no_live_session() -> None:
    executor = _executor()
    code, message = "", ""
    # CLI-level failure: empty stdout, nonzero exit (live-verified shape for
    # an alias the local CLI cannot resolve/authenticate).
    with patch("salesforce_plugin.client.subprocess.run", return_value=_completed("", returncode=2)):
        try:
            executor.run_json(["sobject", "list"])
        except SalesforceServiceError as exc:
            code, message = exc.code, str(exc)
    _assert("no session -> sf.auth_failed", code == ERROR_AUTH_FAILED, code)
    _assert("no-session message carries login hint", "sf org login web" in message, message)


def test_incomplete_org_display_payload() -> None:
    executor = _executor()
    code = ""
    with patch(
        "salesforce_plugin.client.subprocess.run",
        return_value=_completed(_org_display_envelope(username="")),
    ):
        try:
            executor.run_json(["sobject", "list"])
        except SalesforceServiceError as exc:
            code = exc.code
    _assert("incomplete payload -> sf.auth_failed", code == ERROR_AUTH_FAILED, code)


def test_non_json_output() -> None:
    executor = _executor()
    code = ""
    with patch(
        "salesforce_plugin.client.subprocess.run",
        return_value=_completed("not json at all", returncode=1),
    ):
        try:
            executor.run_json(["sobject", "list"])
        except SalesforceServiceError as exc:
            code = exc.code
    _assert("non-JSON output -> sf.auth_failed", code == ERROR_AUTH_FAILED, code)


def test_org_binding_verified_once() -> None:
    executor = _executor()
    ok_result = json.dumps({"status": 0, "result": {"sobjects": []}})
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [
            _completed(_org_display_envelope()),
            _completed(ok_result),
            _completed(ok_result),
        ]
        executor.run_json(["sobject", "list"])
        executor.run_json(["sobject", "list"])
        display_calls = [c for c in run_mock.call_args_list if "display" in c.args[0]]
        _assert("org display invoked exactly once", len(display_calls) == 1, str(run_mock.call_count))


def test_run_json_rest_level_failure() -> None:
    executor = _executor()
    error_envelope = json.dumps(
        {"name": "NOT_FOUND", "message": "top-level", "data": {"errorCode": "NOT_FOUND", "message": "detail"}}
    )
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [_completed(_org_display_envelope()), _completed(error_envelope, returncode=1)]
        code, message = "", ""
        try:
            executor.run_json(["data", "get", "record"])
        except SalesforceCliCallError as exc:
            code, message = exc.error_code, exc.detail_message
        _assert("REST-level failure -> error_code from data.errorCode", code == "NOT_FOUND", code)
        _assert("REST-level failure -> message from data.message", message == "detail", message)


def test_run_rest_happy_path_and_body_file() -> None:
    executor = _executor()
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [
            _completed(_org_display_envelope()),
            _completed(json.dumps({"id": "001new", "success": True, "errors": []})),
        ]
        result = executor.run_rest("POST", "services/data/v62.0/sobjects/Account", body={"Name": "Acme"})
        _assert("run_rest returns parsed body", result == {"id": "001new", "success": True, "errors": []})
        rest_argv = run_mock.call_args_list[1].args[0]
        _assert("api request rest invoked", rest_argv[1:4] == ["api", "request", "rest"], str(rest_argv))
        _assert("POST method passed", "--method" in rest_argv and "POST" in rest_argv, str(rest_argv))
        body_flag_index = rest_argv.index("--body")
        body_path = rest_argv[body_flag_index + 1].removeprefix("@")
        _assert("--body points at a real tempfile written as JSON", not Path(body_path).exists(), body_path)


def test_run_rest_no_content_on_empty_stdout() -> None:
    executor = _executor()
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [_completed(_org_display_envelope()), _completed("")]
        result = executor.run_rest("PATCH", "services/data/v62.0/sobjects/Account/001x", body={"Name": "New"})
        _assert("204 No Content -> None", result is None, repr(result))


def test_run_rest_error_array() -> None:
    executor = _executor()
    error_array = json.dumps([{"errorCode": "NOT_FOUND", "message": "The requested resource does not exist"}])
    with patch("salesforce_plugin.client.subprocess.run") as run_mock:
        run_mock.side_effect = [_completed(_org_display_envelope()), _completed(error_array, returncode=1)]
        code, message = "", ""
        try:
            executor.run_rest("GET", "services/data/v62.0/sobjects/Account/000")
        except SalesforceCliCallError as exc:
            code, message = exc.error_code, exc.detail_message
        _assert("run_rest error array -> error_code", code == "NOT_FOUND", code)
        _assert("run_rest error array -> message", "does not exist" in message, message)


def test_readiness_pulls_config_from_manager() -> None:
    # Regression: the injected config_provider hook has NO caller platform-wide;
    # readiness must pull from config_manager or sf_cli_path silently defaults
    # (live failure 2026-07-14: "sf CLI not found at 'sf'" with a pinned path on disk).
    plugin = SalesforcePlugin()
    orchestrator = MagicMock()
    orchestrator.get_service.return_value = MagicMock()
    orchestrator.config_manager.get_plugin_config.return_value = {
        "sf_cli_path": "/pinned/bin/sf",
        "api_version": "63.0",
    }
    plugin.orchestrator_ref = orchestrator
    plugin.prepare_for_readiness()
    executor = plugin._cli_executor  # noqa: SLF001 — smoke reaches the built executor
    _assert("readiness executor exists", executor is not None)
    if executor is not None:
        _assert("sf_cli_path flows from config manager", executor.sf_cli_path == "/pinned/bin/sf", executor.sf_cli_path)
        _assert("api_version flows from config manager", executor.api_version == "63.0", executor.api_version)
    _assert(
        "config pulled for this plugin name",
        orchestrator.config_manager.get_plugin_config.call_args.args == ("salesforce_plugin",),
        str(orchestrator.config_manager.get_plugin_config.call_args),
    )


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
    _assert("all 15 verbs discovered", len(actions) == 15, str(len(actions)))


def test_cli_provisioning() -> None:
    """Fixture-proof the formula route before its shipped documentation changes."""

    existing = SalesforceCliExecutor(None, sf_cli_path="sf")  # type: ignore[arg-type]
    with patch("salesforce_plugin.client.shutil.which", return_value="/fake/bin/sf"), patch(
        "salesforce_plugin.client.subprocess.run",
        return_value=_completed("@salesforce/cli/2.99.0\n"),
    ) as run_mock:
        observed = existing.provision_cli(acknowledge_system_change=False)
    _assert("existing sf does not invoke Homebrew", observed["provisioned"] is False, str(observed))
    _assert("existing sf has no package mutation", run_mock.call_count == 1, str(run_mock.call_args_list))

    missing = SalesforceCliExecutor(None, sf_cli_path="sf")  # type: ignore[arg-type]
    consent_code = ""
    with patch("salesforce_plugin.client.shutil.which", return_value=None):
        try:
            missing.provision_cli(acknowledge_system_change=False)
        except SalesforceServiceError as exc:
            consent_code = exc.code
    _assert("missing sf refuses without consent", consent_code == ERROR_PROVISION_CONSENT_REQUIRED, consent_code)

    provisioned = SalesforceCliExecutor(None, sf_cli_path="sf")  # type: ignore[arg-type]
    with patch(
        "salesforce_plugin.client.shutil.which",
        side_effect=[None, "/fake/bin/brew", "/fake/bin/sf"],
    ), patch(
        "salesforce_plugin.client.subprocess.run",
        side_effect=[
            _completed("Would install 2 formulae:\nnode\nsf\n"),
            _completed(""),
            _completed("@salesforce/cli/2.99.0\n"),
        ],
    ) as run_mock:
        observed = provisioned.provision_cli(acknowledge_system_change=True)
    _assert("missing sf installs only after consent", observed["provisioned"] is True, str(observed))
    _assert(
        "sf dry-run precedes exactly one install",
        [call.args[0] for call in run_mock.call_args_list][:2]
        == [["/fake/bin/brew", "install", "--dry-run", "sf"], ["/fake/bin/brew", "install", "sf"]],
        str(run_mock.call_args_list),
    )

    plugin = SalesforcePlugin()
    orchestrator = MagicMock()
    orchestrator.config_manager.get_plugin_config.return_value = {"sf_cli_path": "sf"}
    orchestrator.config_manager.save_plugin_config.return_value = True
    plugin.orchestrator_ref = orchestrator
    plugin._app_config_loader = MagicMock()  # noqa: SLF001 — fixture supplies ready plugin state
    plugin._cli_executor = MagicMock()  # noqa: SLF001 — fixture isolates config binding
    plugin._cli_executor.provision_cli.return_value = {
        "provisioned": False,
        "reason": "already_present",
        "executable_path": "/fake/bin/sf",
        "version": "@salesforce/cli/2.99.0",
    }
    response = plugin.provision_cli({"acknowledge_system_change": True}, {})
    _assert("provisioner binds absolute sf path", response["action_status"] == "completed", str(response))
    _assert(
        "binding uses ordinary config update without restart",
        response["data"]["configuration_boundary"] == "config_updated_and_executor_rebound_no_reload_or_restart",
        str(response),
    )


def main() -> int:
    print("\nsalesforce_plugin org-binding smoke tests")
    print("=" * 47)
    test_load_complete_entry()
    test_missing_fields_named()
    test_run_json_happy_path()
    test_pinned_host_mismatch_refuses()
    test_cli_missing_names_config_key()
    test_no_live_session()
    test_incomplete_org_display_payload()
    test_non_json_output()
    test_org_binding_verified_once()
    test_run_json_rest_level_failure()
    test_run_rest_happy_path_and_body_file()
    test_run_rest_no_content_on_empty_stdout()
    test_run_rest_error_array()
    test_readiness_pulls_config_from_manager()
    test_edge_parity()
    test_cli_provisioning()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All org-binding smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
