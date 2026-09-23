#!/usr/bin/env python3
"""Hermetic smoke for salesforce_plugin's offline sf CLI probe."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "plugins" / "salesforce_plugin" / "src"))

from salesforce_plugin.client import SalesforceCliExecutor  # noqa: E402


def main() -> int:
    executor = SalesforceCliExecutor(None, sf_cli_path="sf")  # type: ignore[arg-type]
    with patch("salesforce_plugin.client.shutil.which", return_value="/fake/bin/sf"), patch(
        "salesforce_plugin.client.subprocess.run",
        return_value=subprocess.CompletedProcess(["sf", "--version"], 0, "@salesforce/cli/2.99.0\n", ""),
    ) as run_mock:
        result = executor.probe_cli()
    assert result == {"executable_path": "/fake/bin/sf", "version": "@salesforce/cli/2.99.0", "executable": True, "configured": False}, result
    assert run_mock.call_args.args[0] == ["/fake/bin/sf", "--version"], run_mock.call_args

    with patch("salesforce_plugin.client.shutil.which", return_value=None):
        missing = executor.probe_cli()
    assert missing == {"executable_path": "", "version": "", "executable": False, "configured": False}, missing
    print("PASS offline Salesforce CLI probe")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
