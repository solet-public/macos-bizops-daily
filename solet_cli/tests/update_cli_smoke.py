"""``solet-manager update`` argument contract and closed error/exit projection."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]
from solet_manager.manager_cli import build_parser, main as manager_main  # noqa: E402, I001


def _run(argv: list[str]) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with redirect_stdout(output):
        exit_code = manager_main(argv)
    text = output.getvalue()
    return exit_code, cast(dict[str, object], json.loads(text)) if argv[-1] == "--json" else {"human": text}


def _assert_parser(parser: argparse.ArgumentParser) -> None:
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    assert set(subparsers.choices) == {"inspect", "import", "update", "doctor", "reconcile"}
    values = parser.parse_args(["update", "fixture", "--dry-run", "--json"])
    assert values.name == "fixture" and values.dry_run and not values.yes and values.as_json
    values = parser.parse_args(["update", "fixture", "--yes", "--approval-fingerprint", "sha256:" + "a" * 64])
    assert values.yes and values.approval_fingerprint == "sha256:" + "a" * 64
    for argv in (["update", "fixture"], ["update", "fixture", "--dry-run", "--yes"], ["update", "fixture", "--target", "/tmp/x", "--dry-run"], ["update", "fixture", "--force", "--dry-run"]):
        try:
            parser.parse_args(argv)
        except SystemExit:
            continue
        raise AssertionError(f"parser accepted {argv}")


def main() -> int:
    _assert_parser(build_parser())
    with TemporaryDirectory() as temporary, patch.dict(os.environ, {"SOLET_HOME": temporary}):
        code, rendered = _run(["update", "fixture", "--dry-run", "--json"])
        assert code == 3 and rendered["error_kind"] == "instance_unmanaged" and rendered["status"] == "blocked", rendered
        code, rendered = _run(["update", "fixture", "--yes", "--json"])
        assert code == 2 and rendered["error_kind"] == "approval_fingerprint_required", rendered
        code, rendered = _run(["update", "fixture", "--yes", "--approval-fingerprint", "nope", "--json"])
        assert code == 2 and rendered["error_kind"] == "approval_fingerprint_malformed", rendered
        code, rendered = _run(["update", "Fixture", "--dry-run", "--json"])
        assert code == 2 and rendered["error_kind"] == "invalid_invocation", rendered
        code, rendered = _run(["update", "../escape", "--dry-run", "--json"])
        assert code == 2 and rendered["error_kind"] == "invalid_invocation", rendered
        code, rendered = _run(["update", "fixture", "--dry-run"])
        assert code == 3 and "Error: instance_unmanaged" in str(rendered["human"])
        assert not (Path(temporary) / "state").exists() and not (Path(temporary) / "cache").exists()
    print("update_cli_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
