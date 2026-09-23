"""Genesis-spine smoke — `root_manifest_seed.seed_for_newborn`: the step that
rewrites a seed-born clone's `root_manifest.yaml` `solet_name:` line from
the MINTING solet's name to the newborn's.

Covers: the happy-path rename (minting name gone, newborn name present, every
other line byte-verbatim), idempotency (second run is a no-op rewrite to the
same value), the fail-loud missing-file refusal, the fail-loud
no-`solet_name:`-line refusal (and that a refused file is left untouched),
and first-line-only semantics on a pathological double-line file.

Offline: pure filesystem ops under a tmp tree; no live Postgres, no MCP.

Run directly: ``SOLET_NAME=<name> .venv/bin/python3
plugins/github_midwife_plugin/tests/root_manifest_seed_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.root_manifest_seed import (
    ROOT_MANIFEST_FILENAME,
    RootManifestSeedError,
    seed_for_newborn,
)

_CHECKS_RUN: list[str] = []

_FIXTURE_MANIFEST = (
    "schema_version: 1\n"
    "solet_name: mintersaurus\n"
    "universal:\n"
    "  files:\n"
    "    - bootstrap.py\n"
    "sanctioned: []\n"
)


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _check_happy_path_rename(root: Path) -> None:
    clone = root / "happy"
    clone.mkdir()
    (clone / ROOT_MANIFEST_FILENAME).write_text(_FIXTURE_MANIFEST)

    returned = seed_for_newborn(clone, "newbornia")
    text = returned.read_text()

    _check(
        "seed_for_newborn returns the manifest path it rewrote",
        returned == clone / ROOT_MANIFEST_FILENAME,
        str(returned),
    )
    _check(
        "newborn name present, minting name gone",
        "solet_name: newbornia\n" in text and "mintersaurus" not in text,
        text,
    )
    _check(
        "every non-name line rides through byte-verbatim",
        text == _FIXTURE_MANIFEST.replace(
            "solet_name: mintersaurus", "solet_name: newbornia"
        ),
        text,
    )

    second = seed_for_newborn(clone, "newbornia").read_text()
    _check(
        "second run is idempotent (identical content)",
        len(second) > 0 and second == text,
        second,
    )


def _check_missing_file_fail_loud(root: Path) -> None:
    clone = root / "empty"
    clone.mkdir()
    try:
        seed_for_newborn(clone, "newbornia")
    except RootManifestSeedError as exc:
        _check(
            "missing root_manifest.yaml raises naming the path",
            ROOT_MANIFEST_FILENAME in str(exc),
            str(exc),
        )
        return
    raise SmokeFailureError("missing root_manifest.yaml did NOT raise RootManifestSeedError")


def _check_missing_name_line_fail_loud(root: Path) -> None:
    clone = root / "nameless"
    clone.mkdir()
    nameless = "schema_version: 1\nuniversal:\n  files: []\n"
    (clone / ROOT_MANIFEST_FILENAME).write_text(nameless)
    try:
        seed_for_newborn(clone, "newbornia")
    except RootManifestSeedError as exc:
        _check(
            "manifest without a solet_name line raises a schema-violation error",
            "solet_name" in str(exc),
            str(exc),
        )
        after = (clone / ROOT_MANIFEST_FILENAME).read_text()
        _check(
            "a refused manifest is left byte-untouched",
            after == nameless,
            after,
        )
        return
    raise SmokeFailureError("nameless root_manifest.yaml did NOT raise RootManifestSeedError")


def _check_first_line_only_on_double_name(root: Path) -> None:
    clone = root / "double"
    clone.mkdir()
    (clone / ROOT_MANIFEST_FILENAME).write_text(
        "solet_name: mintersaurus\nsolet_name: mintersaurus\n"
    )
    text = seed_for_newborn(clone, "newbornia").read_text()
    _check(
        "only the FIRST solet_name line is rewritten (count=1 semantics)",
        text == "solet_name: newbornia\nsolet_name: mintersaurus\n",
        text,
    )


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="root_manifest_seed_smoke_") as tmp:
        root = Path(tmp)
        _check_happy_path_rename(root)
        _check_missing_file_fail_loud(root)
        _check_missing_name_line_fail_loud(root)
        _check_first_line_only_on_double_name(root)

    print(f"root_manifest_seed_smoke: {len(_CHECKS_RUN)}/{len(_CHECKS_RUN)} checks passed")
    for label in _CHECKS_RUN:
        print(f"  [ok] {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
