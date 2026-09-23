#!/usr/bin/env python3
"""Hermetic controls for the flow probe registry gate."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from quality_gates.flow_probe_registry_gate import (  # noqa: E402
    Finding,
    _relayed_findings,
    classify_live_schema,
)


def main() -> int:
    red, keys = _relayed_findings(
        "fabricated",
        "plugin::synthetic_plugin.missing_probe",
        {"plugin::synthetic_plugin.missing_probe"},
        set(),
    )
    assert keys == {"plugin::synthetic_plugin::missing_probe"}
    assert red == [Finding("FPR-REGISTRY-MISSING", "fabricated", "plugin::synthetic_plugin.missing_probe", "platform registry source has no @service_interface_process or @platform_process declaration for plugin::synthetic_plugin::missing_probe", "plugin::synthetic_plugin::missing_probe")]
    assert not _relayed_findings("ok", "plugin::synthetic_plugin.present", {"plugin::synthetic_plugin.present"}, {"plugin::synthetic_plugin::present"})[0]
    assert classify_live_schema(1, '403 {"code":"bridge.process_not_allowed","key":"setup::nonsense::at_all"}') == "undecidable"
    assert classify_live_schema(1, "bridge.invalid_process_key") == "missing"
    print("flow_probe_registry_gate_smoke: 5 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
