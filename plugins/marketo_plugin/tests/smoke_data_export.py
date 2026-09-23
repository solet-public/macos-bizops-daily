#!/usr/bin/env python3
"""Data-export containment-gate smoke tests for marketo_plugin.

Business-data limits + data-export migration, 2026-08-02
(workbench/2026-08-02_business_data_limits_and_spill_floor_design_coordinator_day.md,
§7.1). describe_lead_fields, get_leads, list_activity_types, get_activities,
list_campaigns, and list_static_lists now ALWAYS write to the caller-supplied
output_tsv_path — blob storage has retired from this plugin entirely.

Hermetic — a MagicMock standing in for MarketoClient, no live instance. Drives
the REAL export_containment gate (via the plugin's own ``_export_path_gate``,
bound to a fake config_provider) and the real ``assert_export_path_allowed``
directly — the containment boundary is exactly what must not be mocked.

Exercises:
  1. red-first: export path OUTSIDE every allowed root -> ExportPathRefusedError,
     no file written, the vendor call never runs
  2. red-first: EMPTY export_allowed_roots -> refused naming the config key
  3. red-first: RELATIVE or BLANK configured root -> refused as misconfigured
  4. a path INSIDE an allowed root is admitted and the vendor call proceeds
  5. check_setup's probes work with NO export_allowed_roots configured at all —
     a permission check must never depend on the operator's real export
     workspace being set up (regression guard on the tempfile-passthrough
     design of _run_read_probe)
  6. the plugin's _export_path_gate rejects a malformed (non-list)
     export_allowed_roots config value loudly, not silently

Run:
    SOLET_NAME=<name> .venv/bin/python3 \
        plugins/marketo_plugin/tests/smoke_data_export.py

Exits 0 on success, 1 on first failure.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "marketo_plugin" / "src"))

from marketo_plugin import marketing_actions  # noqa: E402
from marketo_plugin.constants import CONFIG_KEY_EXPORT_ALLOWED_ROOTS  # noqa: E402
from marketo_plugin.errors import MarketoServiceError  # noqa: E402
from marketo_plugin.export_containment import (  # noqa: E402
    ExportPathRefusedError,
    assert_export_path_allowed,
)
from marketo_plugin.plugin import MarketoPlugin  # noqa: E402

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


def _gate_for(roots: list[str]) -> Any:
    def gate(output_tsv_path: str) -> str:
        return assert_export_path_allowed(
            output_tsv_path, roots, config_key=CONFIG_KEY_EXPORT_ALLOWED_ROOTS, plugin_name="marketo_plugin",
        )

    return gate


def _fake_get_json_client() -> Any:
    client = MagicMock()
    client.get_json.return_value = {"success": True, "result": []}
    return client


def test_export_path_outside_allowed_root_refused() -> None:
    with tempfile.TemporaryDirectory(prefix="marketo_allowed_") as allowed_root, tempfile.TemporaryDirectory(prefix="marketo_outside_") as outside_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(outside_root) / "out.tsv")
        client = _fake_get_json_client()
        raised = False
        try:
            marketing_actions.list_activity_types(client, {"output_tsv_path": out_path}, gate)
        except ExportPathRefusedError:
            raised = True
        _assert("outside-root path refused", raised)
        _assert("vendor never called when the path is refused", client.get_json.call_count == 0)
        _assert("no file written", not Path(out_path).exists())


def test_empty_allowed_roots_refused() -> None:
    gate = _gate_for([])
    client = _fake_get_json_client()
    raised: ExportPathRefusedError | None = None
    try:
        marketing_actions.list_activity_types(client, {"output_tsv_path": "/tmp/x.tsv"}, gate)
    except ExportPathRefusedError as exc:
        raised = exc
    _assert("empty export_allowed_roots refuses", raised is not None)
    _assert("refusal names the config key", raised is not None and CONFIG_KEY_EXPORT_ALLOWED_ROOTS in str(raised))


def test_relative_root_misconfigured() -> None:
    gate = _gate_for(["relative/path"])
    client = _fake_get_json_client()
    raised = False
    try:
        marketing_actions.list_activity_types(client, {"output_tsv_path": "/tmp/x.tsv"}, gate)
    except ExportPathRefusedError:
        raised = True
    _assert("relative configured root refused as misconfigured", raised)


def test_path_inside_allowed_root_admitted() -> None:
    with tempfile.TemporaryDirectory(prefix="marketo_allowed_") as allowed_root:
        gate = _gate_for([allowed_root])
        out_path = str(Path(allowed_root) / "out.tsv")
        client = MagicMock()
        client.get_json.return_value = {"success": True, "result": [{"id": 1, "name": "Send Email"}]}
        result = marketing_actions.list_activity_types(client, {"output_tsv_path": out_path}, gate)
        _assert("admitted path resolves and the vendor call proceeds", client.get_json.call_count == 1)
        _assert("file written under the allowed root", Path(out_path).exists())
        _assert("row_count reflects the written record", result["row_count"] == 1)


def test_check_setup_needs_no_export_allowed_roots() -> None:
    """check_setup is a permission probe, not an export — it must succeed even
    when export_allowed_roots is entirely unconfigured (the operator has not
    opted any workspace root in yet). Regression guard on _run_read_probe's
    tempfile-passthrough design: if a future edit accidentally routed the
    probes through the plugin's real _export_path_gate, this would fail with
    marketo.export_path_refused instead of running the probes."""
    client = MagicMock()
    client.get_json.return_value = {"success": True, "result": [], "searchableFields": ["id"]}
    result = marketing_actions.check_setup(client)
    _assert("check_setup succeeds with zero export_allowed_roots configured", result["reads_verified"] is True)
    _assert("check_setup ran all 6 read probes", len(result["checks"]) == 6, str(result["checks"]))


def test_plugin_export_path_gate_rejects_malformed_config() -> None:
    plugin = MarketoPlugin()
    plugin.config_provider = {CONFIG_KEY_EXPORT_ALLOWED_ROOTS: "not-a-list"}
    raised: MarketoServiceError | None = None
    try:
        plugin._export_path_gate("/tmp/x.tsv")
    except MarketoServiceError as exc:
        raised = exc
    _assert("malformed export_allowed_roots config raises loudly", raised is not None)
    _assert(
        "malformed-config error names the config key",
        raised is not None and CONFIG_KEY_EXPORT_ALLOWED_ROOTS in str(raised),
        str(raised),
    )


def main() -> int:
    print("\nmarketo_plugin data-export containment-gate smoke tests")
    print("=" * 57)
    test_export_path_outside_allowed_root_refused()
    test_empty_allowed_roots_refused()
    test_relative_root_misconfigured()
    test_path_inside_allowed_root_admitted()
    test_check_setup_needs_no_export_allowed_roots()
    test_plugin_export_path_gate_rejects_malformed_config()
    print()
    print(f"Results: {_passed} passed, {len(_failed)} failed")
    if _failed:
        print("FAILED:", _failed)
        return 1
    print("All data-export containment-gate smoke tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
