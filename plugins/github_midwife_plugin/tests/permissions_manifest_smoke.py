"""Drift gate for the generated setup permission-manifest pair.

Run directly::

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/python3 \
      plugins/github_midwife_plugin/tests/permissions_manifest_smoke.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.permissions_manifest import (  # noqa: E402
    PermissionsManifestDriftError,
    PermissionsManifestError,
    check_artifacts,
    load_flow,
    manifest_entries,
    render_manifest,
    write_artifacts,
)

_FLOW_PATH = _PLUGIN_ROOT / "knowledge_base" / "macos_setup_flow.json"
_ARTIFACT_DIRECTORY = _PLUGIN_ROOT / "knowledge_base"
_REQUIRED_HUMAN_FIELDS = (
    "WHAT:",
    "WHY:",
    "WHEN:",
    "WHAT DENIAL DOES:",
    "SYSTEM SETTINGS PANE:",
    "GRANT ACTOR:",
)
_CHECKS: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised when a manifest regression is detected."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_determinism(flow: dict[str, Any]) -> None:
    first = render_manifest(flow)
    second = render_manifest(copy.deepcopy(flow))
    _check(
        "same setup-flow contract renders byte-identical JSON and Markdown",
        first == second,
    )


def _check_committed_artifacts_green() -> None:
    check_artifacts(_FLOW_PATH, _ARTIFACT_DIRECTORY)
    _check("committed generated artifacts byte-match the contract", True)


def _check_mutated_contract_red(flow: dict[str, Any]) -> None:
    mutated = copy.deepcopy(flow)
    mutated["permissions"]["background_items_permission"]["purpose"] += " MUTATED."
    with tempfile.TemporaryDirectory(prefix="permissions_manifest_smoke_") as tmp:
        root = Path(tmp)
        fixture_flow = root / "macos_setup_flow.json"
        fixture_flow.write_text(json.dumps(mutated, indent=2) + "\n", encoding="utf-8")
        write_artifacts(_FLOW_PATH, root)
        try:
            check_artifacts(fixture_flow, root)
        except PermissionsManifestDriftError as exc:
            _check(
                "mutated contract fixture is RED against committed-generation bytes",
                "generated artifact drift" in str(exc),
                str(exc),
            )
            return
    raise SmokeFailureError("mutated contract fixture did not produce a drift failure")


def _check_human_rendering_completeness(flow: dict[str, Any]) -> None:
    markdown = render_manifest(flow).markdown_bytes.decode("utf-8")
    entries = manifest_entries(flow)
    for entry in entries:
        heading = f"### {entry['what']} (`{entry['id']}`)"
        _check(f"human rendering has {entry['id']}", heading in markdown, heading)
        start = markdown.index(heading)
        next_heading = markdown.find("### ", start + len(heading))
        section = markdown[start:] if next_heading == -1 else markdown[start:next_heading]
        _check(
            f"human rendering carries all six review fields for {entry['id']}",
            all(field in section for field in _REQUIRED_HUMAN_FIELDS),
            section,
        )


def main() -> int:
    flow = load_flow(_FLOW_PATH)
    _check_determinism(flow)
    _check_committed_artifacts_green()
    _check_mutated_contract_red(flow)
    _check_human_rendering_completeness(flow)
    _check_lm_studio_condition(flow)
    print(f"permissions_manifest_smoke: {len(_CHECKS)}/{len(_CHECKS)} checks passed")
    return 0


def _check_lm_studio_condition(flow: dict[str, Any]) -> None:
    entries = manifest_entries(flow)
    consent = next(entry for entry in entries if entry["id"] == "lm_studio_background_service_consent")
    _check("LM Studio consent describes either existing implementation choice", consent["when"] == "When (`embeddings_implementation` equals `lm_studio`) or (`inference_implementation` equals `lm_studio`).")
    for condition in ({"any": []}, {"any": [None]}, {"all": ["invalid"]}):
        malformed = copy.deepcopy(flow)
        malformed["consents"]["lm_studio_background_service_consent"]["required_when"] = condition
        try:
            render_manifest(malformed)
        except PermissionsManifestError:
            _check("malformed compound condition refused", True)
        else:
            raise SmokeFailureError("malformed compound condition rendered silently")


if __name__ == "__main__":
    raise SystemExit(main())
