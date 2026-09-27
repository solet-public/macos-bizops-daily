"""Focused smoke for closed `${SOLET_APP_HOME}` profile override expansion.

Run directly with ``.venv/bin/python3
plugins/github_midwife_plugin/tests/asset_root_materialize_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.config_materialize import (
    ConfigMaterializeError,
    apply_profile_overrides,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _profile() -> dict[str, object]:
    return {
        "profile_name": "macos-bizops",
        "plugin_config_overrides": {
            "core_ai_plugin": {
                "asset_root": "${SOLET_APP_HOME}/data/model-assets",
                "owner": "${SOLET_NAME}",
                "nested": {"resolved_root": "${SOLET_APP_HOME}/data/model-assets"},
                "unknown_placeholder": "${UNSUPPORTED_TOKEN}",
            },
        },
    }


def _check_two_absolute_clone_paths(root: Path) -> None:
    clone_targets = (
        root / 'clone "one" \\ with spaces' / "checkout",
        root / "clone-two" / "checkout",
    )
    resolved_roots: list[str] = []

    for index, target in enumerate(clone_targets):
        config_dir = target / "profile" / "config" / "plugins"
        config_dir.mkdir(parents=True)
        core_config_path = config_dir / "core_ai_plugin.json"
        core_config_path.write_text(
            json.dumps({"existing_setting": {"kept": True}, "asset_root": "stale-root"}),
            encoding="utf-8",
        )
        unrelated_config_path = config_dir / "unrelated_plugin.json"
        unrelated_bytes = b'{"unrelated_setting": "preserve exact bytes"}\n'
        unrelated_config_path.write_bytes(unrelated_bytes)

        # Wrong environment values prove materialization uses explicit inputs.
        with patch.dict(
            os.environ,
            {"SOLET_APP_HOME": "/environment/wrong", "SOLET_NAME": "wrong-name"},
        ):
            written = apply_profile_overrides(target, _profile(), "macos-bizops")

        expected_root = str(target / "profile" / "data" / "model-assets")
        resolved_roots.append(expected_root)
        actual = json.loads(core_config_path.read_text(encoding="utf-8"))
        _check(
            f"clone {index + 1} writes its explicit absolute acquisition root",
            actual.get("asset_root") == expected_root and Path(actual["asset_root"]).is_absolute(),
            f"expected {expected_root!r}, got {actual.get('asset_root')!r}",
        )
        _check(
            f"clone {index + 1} preserves existing Core AI settings",
            actual.get("existing_setting") == {"kept": True},
            f"got {actual!r}",
        )
        _check(
            f"clone {index + 1} uses explicit Solet name and leaves unknown tokens literal",
            actual.get("owner") == "macos-bizops"
            and actual.get("unknown_placeholder") == "${UNSUPPORTED_TOKEN}",
            f"got {actual!r}",
        )
        _check(
            f"clone {index + 1} expands placeholders in nested override strings",
            actual.get("nested") == {"resolved_root": expected_root},
            f"got {actual.get('nested')!r}",
        )
        _check(
            f"clone {index + 1} reports only the intended config file",
            written == [core_config_path],
            f"got {written!r}",
        )
        _check(
            f"clone {index + 1} leaves unrelated plugin config byte-for-byte unchanged",
            unrelated_config_path.read_bytes() == unrelated_bytes,
            "unrelated plugin config changed",
        )

    _check(
        "different absolute clone paths produce different asset roots",
        resolved_roots[0] != resolved_roots[1],
        f"got {resolved_roots!r}",
    )


def _check_relative_target_fails_closed(root: Path) -> None:
    original_cwd = Path.cwd()
    try:
        os.chdir(root)
        relative_target = Path("relative-clone")
        config_dir = relative_target / "profile" / "config" / "plugins"
        config_dir.mkdir(parents=True)
        try:
            apply_profile_overrides(relative_target, _profile(), "macos-bizops")
        except ConfigMaterializeError as exc:
            _check(
                "relative target fails with an explicit absolute-path error",
                "requires an absolute target profile path" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("relative target silently produced an asset root")
    finally:
        os.chdir(original_cwd)


def _check_legacy_solet_name_bytes(root: Path) -> None:
    target = root / "legacy-name-clone"
    config_dir = target / "profile" / "config" / "plugins"
    config_dir.mkdir(parents=True)
    override: dict[str, object] = {
        "user": "${SOLET_NAME}",
        "description": "profile for ${SOLET_NAME}",
        "unknown_placeholder": "${UNSUPPORTED_TOKEN}",
    }
    profile: dict[str, object] = {"plugin_config_overrides": {"state_plugin": override}}

    with patch.dict(os.environ, {"SOLET_NAME": "wrong-environment-name"}):
        apply_profile_overrides(target, profile, "macos-bizops")

    expected_value = json.loads(json.dumps(override).replace("${SOLET_NAME}", "macos-bizops"))
    expected_bytes = (json.dumps(expected_value, indent=2) + "\n").encode("utf-8")
    actual_bytes = (config_dir / "state_plugin.json").read_bytes()
    _check(
        "legacy SOLET_NAME-only output remains byte-for-byte compatible",
        actual_bytes == expected_bytes,
        f"expected {expected_bytes!r}, got {actual_bytes!r}",
    )


def main() -> int:
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _check_two_absolute_clone_paths(root)
            _check_relative_target_fails_closed(root)
            _check_legacy_solet_name_bytes(root)
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"asset_root_materialize_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
