"""Restricted manager CLI surface smoke test."""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from typing import Never, cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src")]

from solet_manager import manager_cli  # noqa: E402, I001
from solet_manager.manager_cli import build_parser, main as manager_main  # noqa: E402, I001
from solet_manager.existing_install_inspection import (  # noqa: E402, I001
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    InstalledInspectionMetadata,
)
from solet_manager.release_lock import SeedLock  # noqa: E402, I001

_LOCK_VALUE: dict[str, object] = {
    "allowed_repository_migrations": [],
    "archive_sha256": "e" * 64,
    "channel_id": "stable",
    "commit": "a" * 40,
    "existing_install_contract": {
        "bundle_digest": "sha256:" + "f" * 64,
        "flow_id": "existing-install",
        "flow_schema_version": 1,
    },
    "profile": "macos-bizops",
    "provenance": {
        "bundle_name": "macos-bizops",
        "manifest_sha256": "b" * 64,
        "origin_id": "123e4567-e89b-12d3-a456-426614174001",
        "platform": "local",
        "provenance_sha256": "c" * 64,
        "schema_version": 1,
        "seed_id": "bb782efa-a331-5748-bc29-7f012b9a5f9d",
        "source_commit": "a" * 40,
        "source_date": "2026-09-16T00:00:00+00:00",
    },
    "release_tag": "release-2026-09-16",
    "repository": "https://github.com/solet-public/macos-bizops.git",
    "schema_version": 3,
    "tree_hash": "d" * 40,
}
_LOCK_BYTES = (json.dumps(_LOCK_VALUE, indent=2, sort_keys=True) + "\n").encode()


def _metadata_loader(channel: str, tracker: object) -> InstalledInspectionMetadata:
    identity = ChannelInspectionIdentity(
        "stable",
        "https://example.invalid/repository.git",
        "r1",
        "a" * 40,
        "b" * 40,
        "macos-bizops",
        "c" * 64,
        "123e4567-e89b-12d3-a456-426614174001",
        "123e4567-e89b-12d3-a456-426614174001",
        "d" * 64,
        ExistingInstallContractIdentity("existing-install", 1, "sha256:" + "e" * 64),
        "catalog",
        "f" * 64,
        "seed",
        "0" * 64,
        "sha256:" + "2" * 64,
        "anchors",
        "1" * 64,
    )
    return InstalledInspectionMetadata(identity, cast(SeedLock, None), ())


def _metadata_failure(channel: str, tracker: object) -> Never:
    raise ValueError("wheel RECORD mismatch")


def _run_cli(argv: list[str], loader: object) -> tuple[int, dict[str, object]]:
    output = io.StringIO()
    with (
        patch("solet_manager.manager_cli.load_installed_inspection_metadata", loader),
        redirect_stdout(output),
    ):
        exit_code = manager_main(argv)
    return exit_code, cast(dict[str, object], json.loads(output.getvalue()))


def _assert_import_update_parse(parser: argparse.ArgumentParser) -> None:
    imported = parser.parse_args(["import", "fixture", "--target", "/tmp/existing", "--channel", "stable", "--dry-run", "--json"])
    assert imported.command == "import" and imported.name == "fixture"
    assert imported.target == Path("/tmp/existing") and imported.channel == "stable"
    assert imported.dry_run and not imported.yes and imported.as_json
    updated = parser.parse_args(["update", "fixture", "--dry-run", "--json"])
    assert updated.command == "update" and updated.name == "fixture"
    assert updated.dry_run and not updated.yes and updated.as_json


def _assert_usage_error_without_mode(parser: argparse.ArgumentParser) -> None:
    for verb in ("import", "update"):
        try:
            with redirect_stderr(io.StringIO()):
                parser.parse_args([verb, "fixture"])
        except SystemExit as error:
            assert error.code == 2, f"{verb} without a mode or target must be a usage error"
        else:
            raise AssertionError(f"{verb} accepted no --dry-run/--yes mode")


def _assert_parser_contract(parser: argparse.ArgumentParser) -> None:
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
    choices = subparsers.choices
    # Later manager verbs (doctor, reconcile, ...) are allowed; the existing-install trio must stay wired.
    assert {"inspect", "import", "update"} <= set(choices)
    _assert_import_update_parse(parser)
    _assert_usage_error_without_mode(parser)
    values = parser.parse_args(["inspect", "--target", "/tmp/existing", "--channel", "stable", "--json"])
    assert values.target == Path("/tmp/existing") and values.channel == "stable" and values.as_json


def _assert_command_wiring() -> None:
    """Each verb reaches its own handler: the dispatch table and a real dispatch through main."""
    assert manager_cli._COMMANDS["inspect"] is manager_cli._inspect_result
    assert manager_cli._COMMANDS["import"] is manager_cli._import_result
    assert manager_cli._COMMANDS["update"] is manager_cli._update_result
    with tempfile.TemporaryDirectory() as directory:
        home = Path(directory).resolve()
        (home / "target").mkdir()
        with patch.dict(os.environ, {"SOLET_HOME": str(home)}):
            for argv, kind in (
                (
                    ["import", "fixture", "--target", str(home / "target"), "--channel", "stable", "--dry-run", "--json"],
                    "existing_install_import",
                ),
                (["update", "fixture", "--dry-run", "--json"], "existing_install_update"),
            ):
                output = io.StringIO()
                with patch("solet_manager.manager_cli.load_installed_inspection_metadata", _metadata_failure):
                    with redirect_stdout(output):
                        exit_code = manager_main(argv)
                rendered = cast(dict[str, object], json.loads(output.getvalue()))
                assert exit_code != 0, f"{argv[0]}: the fixture is refused, not applied"
                assert rendered["kind"] == kind, f"{argv[0]} rendered {rendered['kind']!r}, expected {kind!r}"


def main() -> int:
    _assert_parser_contract(build_parser())
    _assert_command_wiring()
    with tempfile.TemporaryDirectory() as directory:
        prefix = Path(directory)
        lock = prefix / "share" / "solet" / "seed.lock.json"
        lock.parent.mkdir(parents=True)
        lock.write_bytes(_LOCK_BYTES)
        target = prefix / "target"
        target.mkdir()
        original_prefix = sys.prefix
        try:
            sys.prefix = str(prefix)
            exit_code, rendered = _run_cli(
                ["inspect", "--target", str(target), "--channel", "stable", "--json"],
                _metadata_loader,
            )
            missing_exit, missing = _run_cli(
                ["inspect", "--target", str(prefix / "missing"), "--channel", "stable", "--json"],
                _metadata_loader,
            )
            metadata_exit, metadata_failure = _run_cli(
                ["inspect", "--target", str(target), "--channel", "stable", "--json"],
                _metadata_failure,
            )
        finally:
            sys.prefix = original_prefix
        assert exit_code == 1 and rendered["error_kind"] == "inspection_failed"
        data = rendered["data"]
        assert isinstance(data, dict)
        channel = data["channel"]
        assert isinstance(channel, dict) and channel["channel_id"] == "stable"
        assert missing_exit == 2 and missing["error_kind"] == "target_identity_invalid"
        assert metadata_exit == 1 and metadata_failure["error_kind"] == "inspection_failed"
        failure_data = metadata_failure["data"]
        assert isinstance(failure_data, dict)
        assert failure_data["inspection_error"] == "wheel RECORD mismatch"
    print("existing_install_inspection_cli_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
