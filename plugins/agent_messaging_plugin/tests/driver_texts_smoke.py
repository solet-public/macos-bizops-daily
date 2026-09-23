#!/usr/bin/env python3
"""Negative controls for driver-text placeholder validation."""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))
from agent_messaging_plugin import driver_texts  # noqa: E402


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        original = driver_texts._ROOT
        driver_texts._ROOT = root
        try:
            (root / "undeclared.json").write_text(
                json.dumps({"template": "{missing}", "placeholders": []}),
            )
            (root / "omitted.json").write_text(
                json.dumps({"template": "{needed}", "placeholders": ["needed"]}),
            )
            for key in ("undeclared", "omitted"):
                try:
                    driver_texts.render_driver_text(key)
                except RuntimeError:
                    continue
                raise AssertionError(f"{key} did not fail loud")
        finally:
            driver_texts._ROOT = original
    print("2 negative controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
