#!/usr/bin/env python3
"""Red-first controls for the pre-landing schema-init gate (iss_63d91ca9 member 2).

The RED control reproduces the ORIGINAL defect class exactly: a plugin whose
``get_schema_definitions()`` declares a table redeclaring the platform-protected
``created_at`` field (chg_7fb2e9f8 / iss_2bc76075, repeated in iss_5f91287d).
Every static gate passed that landing; this gate must refuse it by actually
booting ``initialize_schemas`` against the candidate tree. The GREEN control is
the same fixture without the override, which must initialize cleanly.

Each control builds a private candidate tree: the gate's own scripts, a REAL
COPY of ``ananta/src`` (no symlinks -- the gate refuses a tree whose imports
resolve elsewhere, and one control proves exactly that), and a single fixture
plugin declared through ``plugins/<name>/pyproject.toml`` the way every real
plugin is discovered. Nothing here touches the shared checkout or a database.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GATE_FILES = ("__init__.py", "schema_init_gate.py", "allowlist_schema.py", "source_root.py")
_FIXTURE_PLUGIN = "schema_gate_fixture_plugin"
_PROTECTED_OVERRIDE = (
    '            "created_at": ColumnDefinition(type=ColumnType.TEXT, not_null=True, '
    'description="ISO-8601 UTC creation timestamp."),\n'
)
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)
    print(f"  PASS  {label}")


def _fixture_plugin_source(*, override: bool, constructor_raises: bool = False) -> str:
    raise_line = '        raise RuntimeError("fixture constructor refuses")\n' if constructor_raises else ""
    override_line = _PROTECTED_OVERRIDE if override else ""
    return (
        "from ananta.types.column_types import ColumnType\n"
        "from ananta.types.schema_types import ColumnDefinition, SchemaDefinition, TableSchema\n"
        "\n\n"
        "class SchemaGateFixturePlugin:\n"
        "    def __init__(self) -> None:\n"
        '        self.name = ""\n'
        f"{raise_line}"
        "\n"
        "    def get_schema_definitions(self) -> list[SchemaDefinition]:\n"
        "        columns = {\n"
        '            "snapshot_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True, unique=True),\n'
        f"{override_line}"
        "        }\n"
        "        table = TableSchema(table_name=\"dependency_snapshots\", columns=columns, id_prefix=\"dsn\")\n"
        "        return [SchemaDefinition(namespace=\"schema_gate_fixture\", version=\"1.0.0\", "
        "tables={\"dependency_snapshots\": table})]\n"
    )


def _build_tree(root: Path, *, override: bool, constructor_raises: bool = False, symlink_ananta: bool = False) -> None:
    gates = root / "quality_gates"
    gates.mkdir()
    for name in _GATE_FILES:
        shutil.copy2(_REPO_ROOT / "quality_gates" / name, gates / name)
    (root / "ananta").mkdir()
    if symlink_ananta:
        (root / "ananta" / "src").symlink_to(_REPO_ROOT / "ananta" / "src", target_is_directory=True)
    else:
        shutil.copytree(
            _REPO_ROOT / "ananta" / "src", root / "ananta" / "src",
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    plugin_root = root / "plugins" / _FIXTURE_PLUGIN
    package = plugin_root / "src" / _FIXTURE_PLUGIN
    package.mkdir(parents=True)
    (plugin_root / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{_FIXTURE_PLUGIN}"\n'
        'version = "0.0.0"\n'
        "\n"
        '[project.entry-points."ananta.plugins"]\n'
        f'{_FIXTURE_PLUGIN} = "{_FIXTURE_PLUGIN}.plugin:SchemaGateFixturePlugin"\n',
        encoding="utf-8",
    )
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "plugin.py").write_text(
        _fixture_plugin_source(override=override, constructor_raises=constructor_raises), encoding="utf-8",
    )


def _run_gate(root: Path, *, script_root: Path | None = None, allowlist: Path | None = None) -> subprocess.CompletedProcess[str]:
    script = (script_root or root) / "quality_gates" / "schema_init_gate.py"
    argv = [sys.executable, str(script), "--repo-root", str(root)]
    if allowlist is not None:
        argv += ["--allowlist", str(allowlist)]
    env = {key: value for key, value in os.environ.items() if key != "SOLET_NAME"}
    return subprocess.run(argv, capture_output=True, text=True, env=env, timeout=110, check=False)


def _empty_allowlist(root: Path) -> Path:
    path = root / "quality_gates" / "schema_init_allowlist.txt"
    path.write_text("# fixture register\n", encoding="utf-8")
    return path


def test_red_protected_field_override_is_refused() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=True)
        result = _run_gate(root, allowlist=_empty_allowlist(root))
        output = result.stdout + result.stderr
        if result.returncode != 2:
            print(output)
        _check(result.returncode == 2, f"RED: a protected-field override exits 2 (got {result.returncode})")
        _check(
            "attempts to override protected standard field(s): [created_at]" in output,
            "RED: the refusal is the standardizer's own protected-field message",
        )
        _check(
            "schema_gate_fixture::dependency_snapshots" in output and "schema_init_violation" in output,
            "RED: the finding is keyed namespace::table for the allowlist register",
        )
        _check(
            f"ananta resolved from {root.resolve()}" in output,
            "RED: the boot proved its ananta import root is the candidate tree, not the shared checkout",
        )
        _check("SOLET_NAME was unset" in output, "the gate discloses the gate-local SOLET_NAME it supplied")


def test_green_clean_schema_passes() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=False)
        result = _run_gate(root, allowlist=_empty_allowlist(root))
        output = result.stdout + result.stderr
        if result.returncode != 0:
            print(output)
        _check(result.returncode == 0, f"GREEN: the same fixture without the override exits 0 (got {result.returncode})")
        _check("booted 1 plugin(s)" in output and "initialize_schemas completes" in output, "GREEN: one fixture plugin booted and every schema initialized")


def test_plugin_boot_error_is_a_finding() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=False, constructor_raises=True)
        result = _run_gate(root, allowlist=_empty_allowlist(root))
        output = result.stdout + result.stderr
        _check(result.returncode == 2, "a plugin whose constructor raises is a blocking finding")
        _check(f"plugin_boot_error  {_FIXTURE_PLUGIN}::boot" in output and "fixture constructor refuses" in output, "the boot error names the plugin and the exception")


def test_allowlist_is_tracked_debt_not_a_skip() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=True)
        allowlist = root / "quality_gates" / "schema_init_allowlist.txt"
        allowlist.write_text(
            "schema_gate_fixture::dependency_snapshots  # owner: smoke reason: fixture expires: 2099-01-01\n"
            "schema_gate_fixture::gone  # owner: smoke reason: stale fixture expires: 2099-01-01\n",
            encoding="utf-8",
        )
        result = _run_gate(root, allowlist=allowlist)
        output = result.stdout + result.stderr
        _check(result.returncode == 0, "an allowlisted finding does not block")
        _check("ALLOWLISTED" in output and "[created_at]" in output, "the allowlisted finding is still printed in full")
        _check("[stale-allowlist]  schema_gate_fixture::gone" in output, "an entry matching nothing is reported stale")
        raw_result = subprocess.run(
            [sys.executable, str(root / "quality_gates" / "schema_init_gate.py"), "--repo-root", str(root), "--allowlist", str(allowlist), "--raw"],
            capture_output=True, text=True, timeout=110, check=False,
        )
        _check(raw_result.returncode == 2, "--raw ignores the allowlist and blocks again")


def test_wrong_tree_is_refused() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=True)
        foreign = _run_gate(root, script_root=_REPO_ROOT, allowlist=_empty_allowlist(root))
        _check(
            foreign.returncode == 64 and "running from a different tree" in foreign.stderr,
            "the gate script refuses to scan a tree it does not live in (exit 64)",
        )
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _build_tree(root, override=True, symlink_ananta=True)
        linked = _run_gate(root, allowlist=_empty_allowlist(root))
        _check(
            linked.returncode == 64 and "import-root mismatch" in linked.stderr,
            "a tree whose ananta import resolves outside it is refused before measuring (exit 64), "
            "so a symlinked or venv-resolved shared checkout can never pass as the candidate",
        )


def main() -> int:
    test_red_protected_field_override_is_refused()
    test_green_clean_schema_passes()
    test_plugin_boot_error_is_a_finding()
    test_allowlist_is_tracked_debt_not_a_skip()
    test_wrong_tree_is_refused()
    print(f"\nschema_init_gate_smoke: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
