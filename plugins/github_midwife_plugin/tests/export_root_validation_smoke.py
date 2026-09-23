"""Red-first smoke for hydration-time export-root validation (07-29 ruling §3).

Covers the mutual-containment predicate and persistence behavior new in
this release: `export_root_validation.py`. Three of the four cases named in
the Tier-0 dispatch (root-contains-app_home rejected, root-inside-app_home
rejected, valid-root accepted-and-persisted); the fourth
(empty-allow-list-refusal) is the connectors' own pre-existing invariant,
already covered where the gate itself lives —
`plugins/external_postgres_plugin/tests/smoke_data_export.py` (mirrored in
`salesforce_plugin/tests/smoke_soql.py` and
`snowflake_plugin/tests/smoke_data_export.py`) — and is not reimplemented here;
this module never produces an empty-list state, it only ever appends.

Offline: no real Postgres, no network, no real `~/.claude`. All paths are
tmp-directory fixtures.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/export_root_validation_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import os
import sys
import tempfile
from pathlib import Path

_PLUGIN_ROOT = Path(__file__).resolve().parents[1]
_SRC = _PLUGIN_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from github_midwife_plugin.export_root_validation import (
    BUSINESS_CONNECTOR_PLUGINS,
    ExportRootRejectedError,
    assert_export_root_valid,
    configure_export_root,
)

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on the first failed check; the smoke stops immediately."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _real(path: Path) -> str:
    """realpath a fixture path — macOS tmp dirs resolve through a symlink
    (``/var/folders/...`` -> ``/private/var/folders/...``), and the module
    under test always compares realpath-resolved values."""
    return os.path.realpath(str(path))


def _make_clone(root: Path, connector_names: tuple[str, ...]) -> Path:
    clone_root = root / "clone"
    for name in connector_names:
        (clone_root / "plugins" / name).mkdir(parents=True)
    app_home = root / "clone" / "profile"
    app_home.mkdir(parents=True)
    return clone_root


def _check_root_equals_app_home_rejected() -> None:
    try:
        assert_export_root_valid("/Users/op/Workspace/solet/profile", "/Users/op/Workspace/solet/profile")
    except ExportRootRejectedError as exc:
        _check("root equal to app_home rejected", "contains (or equals)" in str(exc), str(exc))
    else:
        raise SmokeFailureError("root-equals-app_home: did not raise")


def _check_root_contains_app_home_rejected() -> None:
    # The exact hazard the ruling calls "the default case on the developer's
    # own box, not an edge case": app_home nested inside the natural answer.
    try:
        assert_export_root_valid("/Users/op/Workspace", "/Users/op/Workspace/solet/profile")
    except ExportRootRejectedError as exc:
        _check("root-contains-app_home rejected", "contains (or equals)" in str(exc), str(exc))
    else:
        raise SmokeFailureError("root-contains-app_home: did not raise")


def _check_root_inside_app_home_rejected() -> None:
    try:
        assert_export_root_valid("/Users/op/Workspace/solet/profile/exports", "/Users/op/Workspace/solet/profile")
    except ExportRootRejectedError as exc:
        _check("root-inside-app_home rejected", "INSIDE the solet home" in str(exc), str(exc))
    else:
        raise SmokeFailureError("root-inside-app_home: did not raise")


def _check_relative_root_rejected() -> None:
    try:
        assert_export_root_valid("Workspace", "/Users/op/Workspace/solet/profile")
    except ExportRootRejectedError as exc:
        _check("non-absolute root rejected", "ABSOLUTE" in str(exc), str(exc))
    else:
        raise SmokeFailureError("relative-root: did not raise")


def _check_disjoint_root_accepted() -> None:
    resolved = assert_export_root_valid("/Users/op/Workspace/jobs", "/Users/op/Workspace/solet/profile")
    _check("disjoint root accepted", resolved == "/Users/op/Workspace/jobs", resolved)


def _check_configure_writes_only_installed_connectors(root: Path) -> None:
    installed = ("jira_plugin", "g_suite_plugin")
    clone_root = _make_clone(root, installed)
    app_home = str(clone_root / "profile")

    written = configure_export_root(
        clone_root, app_home, str(root / "jobs"),
        connector_plugins=BUSINESS_CONNECTOR_PLUGINS,
    )
    _check(
        "configure_export_root touches only installed connectors",
        set(written) == set(installed),
        f"got {sorted(written)}",
    )
    for name in installed:
        config_path = clone_root / "profile" / "config" / "plugins" / f"{name}.json"
        _check(f"{name} config persisted to disk", config_path.is_file(), str(config_path))
        on_disk = json.loads(config_path.read_text(encoding="utf-8"))
        _check(
            f"{name} export_allowed_roots contains the new root",
            on_disk.get("export_allowed_roots") == [_real(root / "jobs")],
            f"got {on_disk!r}",
        )


def _check_configure_is_additive_and_idempotent(root: Path) -> None:
    clone_root = _make_clone(root, ("jira_plugin",))
    app_home = str(clone_root / "profile")
    config_path = clone_root / "profile" / "config" / "plugins" / "jira_plugin.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps({"export_allowed_roots": ["/existing/root"]}), encoding="utf-8")

    configure_export_root(clone_root, app_home, str(root / "jobs"))
    on_disk = json.loads(config_path.read_text(encoding="utf-8"))
    _check(
        "existing operator-added root preserved",
        "/existing/root" in on_disk["export_allowed_roots"],
        f"got {on_disk!r}",
    )
    _check(
        "new root appended, not replacing",
        _real(root / "jobs") in on_disk["export_allowed_roots"],
        f"got {on_disk!r}",
    )

    configure_export_root(clone_root, app_home, str(root / "jobs"))
    on_disk_again = json.loads(config_path.read_text(encoding="utf-8"))
    _check(
        "re-running with the same root does not duplicate",
        on_disk_again["export_allowed_roots"].count(_real(root / "jobs")) == 1,
        f"got {on_disk_again!r}",
    )


def _check_configure_rejects_containing_root(root: Path) -> None:
    clone_root = _make_clone(root, ("jira_plugin",))
    app_home = str(clone_root / "profile")
    try:
        configure_export_root(clone_root, app_home, str(root))
    except ExportRootRejectedError:
        _check("configure_export_root enforces the same validation", True)
    else:
        raise SmokeFailureError("configure_export_root: accepted a root containing app_home")
    config_path = clone_root / "profile" / "config" / "plugins" / "jira_plugin.json"
    _check("rejected write leaves no config file behind", not config_path.exists(), str(config_path))


def main() -> int:
    try:
        _check_root_equals_app_home_rejected()
        _check_root_contains_app_home_rejected()
        _check_root_inside_app_home_rejected()
        _check_relative_root_rejected()
        _check_disjoint_root_accepted()
        with tempfile.TemporaryDirectory() as tmp:
            _check_configure_writes_only_installed_connectors(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_configure_is_additive_and_idempotent(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_configure_rejects_containing_root(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"export_root_validation_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
