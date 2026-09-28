#!/usr/bin/env python3
"""Pre-landing schema-init gate: BOOT ``initialize_schemas`` against the tree under test.

Why this exists (iss_63d91ca9, iev_1850901f recommendation 4; iss_5f91287d)
---------------------------------------------------------------------------

On 2026-09-19 a landing declared a state table whose ``ColumnDefinition`` map
redeclared the platform-managed ``created_at`` field. Every static gate in the
landing battery -- ruff, pyright, radon, the god-class check, the SQL-access
lockdown -- passed it, because none of them exercise what the platform does
with a schema at startup. ``SchemaStandardizer._validate_no_protected_field_overrides``
exists precisely to refuse that override, and it did: on every blue-green
candidate boot after the landing, crashing ``initialize_schemas`` and leaving
four 0-byte swap logs as the only evidence. A second landing repeated the
exact defect class the next day (``release_runs``, ``iss_5f91287d``).

This gate closes that gap by executing the startup path, not linting it. It
runs, in a child interpreter whose import roots are the tree under test:

1. ``CoreSchemaDefinitions.get_all_core_schemas()`` -- the core schemas;
2. every plugin the tree declares under the ``ananta.plugins`` entry-point
   group in ``plugins/*/pyproject.toml``: import the class, instantiate it
   exactly as ``PluginInitializer.create_plugin_instance`` does (no-arg
   constructor), and collect its ``get_schema_definitions()`` through the real
   ``collect_schemas``;
3. the discovery and session-ledger vector schemas (with a placeholder
   embedding dimension -- the standardizer never reads the value);
4. the real ``SchemaManager.initialize_schemas`` over every schema, one at a
   time so every violation is reported rather than only the first, with the
   plugin-schema lifecycle service replaced by a recorder. Standardization,
   protected-field validation, ``SchemaDefinition.validate()`` and lifecycle
   serialization all run for real; only the database write is recorded
   instead of performed, so the gate needs no Postgres and no live solet.

A plugin whose constructor or ``get_schema_definitions()`` raises is a finding
too (``plugin_boot_error``): it would crash startup the same way.

WRONG-TREE DEFENCE. A lane worktree's ``.venv`` is a symlink to the shared
checkout's, whose editable ``.pth`` pointers name the SHARED checkout, so a
naive ``import ananta`` from a worktree tests master, not the candidate
(``iss_ec0db9c7`` / ``iss_77fe09ad`` class). The child is started with
``PYTHONPATH`` set to the tree's own ``ananta/src``, every ``plugins/*/src``
and (when the tree carries it) ``solet_setup_contracts/src`` -- the one required
editable distribution outside those two, which a plugin imports at boot
(``iss_f1d8cfc2``) -- which sort ahead of site-packages, and it REFUSES (exit 64)
unless ``ananta.__file__`` actually resolves inside the tree it was told to
boot. The gate script itself also refuses to run from a different tree than
``--repo-root`` (``assert_running_from_source_root``).

SCOPE. The gate always boots the WHOLE tree; a scope-file, when given, is
reported as ``schema_touching: true|false`` for the landing report only. A
whole-tree boot is cheap (~6s) and strictly stronger than a scoped one --
a change to a shared builder can break a table declared elsewhere.

The ``--allowlist`` file is a tracked-debt register, never a skip path: one
``<namespace>::<table>`` (schema violations) or ``<plugin_name>::boot``
(plugin boot errors) key per line under the mandatory owner/reason/expires
schema. Allowlisted findings still print; removing an entry is the unit of
remediation progress. A live boot-crash is NOT allowlistable by ruling
(fleet-steward ruling, 2026-09-20): the gate reddening the tree until the fix lands is the
correct verdict.

Exit codes:
  0  -- every schema initialized (or every finding is allowlisted)
  2  -- one or more non-allowlisted findings
  64 -- usage error: bad arguments, wrong tree, unreadable manifests, or the
        child could not prove its import roots
  70 -- the child crashed before producing a verdict (a crash is not a finding)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402
from quality_gates.source_root import (  # noqa: E402
    SourceRootError,
    assert_running_from_source_root,
    resolve_source_root,
)

_EXIT_OK = 0
_EXIT_BLOCKING = 2
_EXIT_USAGE = 64
_EXIT_CRASH = 70
_ENTRY_POINT_GROUP = "ananta.plugins"
_BUNDLED_VENV_PREFIX = ".venv"
_DEFAULT_EMBEDDING_DIMENSIONS = 768
_GATE_SOLET_NAME = "schema-init-gate"
_CHILD_TIMEOUT_SECONDS = 240
_WORKER_FLAG = "--boot-worker"
_SCHEMA_MARKERS = ("get_schema_definitions", "ColumnDefinition", "TableSchema", "SchemaDefinition")
_FINDING_SCHEMA_INIT = "schema_init_violation"
_FINDING_PLUGIN_BOOT = "plugin_boot_error"


@dataclass(frozen=True)
class _Finding:
    kind: str
    key: str
    detail: str
    allowlisted: bool = False


@dataclass(frozen=True)
class _EntryPoint:
    plugin_name: str
    module: str
    attribute: str
    plugin_dir: str


class _GateUsageError(RuntimeError):
    """A precondition the gate cannot measure around; exit 64."""


# ---------------------------------------------------------------------------
# Parent side: enumerate the tree, run the child, interpret its verdict.
# ---------------------------------------------------------------------------


def _import_roots(root: Path) -> list[Path]:
    roots = [root / "ananta" / "src"]
    plugins_dir = root / "plugins"
    if plugins_dir.is_dir():
        roots.extend(
            sorted(
                child / "src"
                for child in plugins_dir.iterdir()
                if (child / "src").is_dir() and not child.name.startswith(_BUNDLED_VENV_PREFIX)
            ),
        )
    contracts = root / "solet_setup_contracts" / "src"
    if contracts.is_dir():
        roots.append(contracts)
    missing = [str(path) for path in roots if not path.is_dir()]
    if missing:
        raise _GateUsageError(f"import roots missing from the tree under test: {missing}")
    return roots


def _entry_points(root: Path) -> list[_EntryPoint]:
    """Every ``ananta.plugins`` entry point the tree's own manifests declare."""
    found: list[_EntryPoint] = []
    for manifest in sorted((root / "plugins").glob("*/pyproject.toml")):
        try:
            data = tomllib.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise _GateUsageError(f"unreadable plugin manifest {manifest}: {exc}") from exc
        group = data.get("project", {}).get("entry-points", {}).get(_ENTRY_POINT_GROUP, {})
        if not isinstance(group, dict):
            raise _GateUsageError(f"{manifest}: [project.entry-points.\"{_ENTRY_POINT_GROUP}\"] is not a table")
        for plugin_name, target in group.items():
            module, separator, attribute = str(target).partition(":")
            if not separator or not module or not attribute:
                raise _GateUsageError(f"{manifest}: entry point {plugin_name!r} is not module:attribute ({target!r})")
            found.append(_EntryPoint(str(plugin_name), module, attribute, manifest.parent.name))
    if not found:
        raise _GateUsageError(f"no {_ENTRY_POINT_GROUP} entry points found under {root / 'plugins'}")
    return found


def _child_environment(root: Path, roots: list[Path]) -> tuple[dict[str, str], str | None]:
    """The child's env, and a disclosure line when SOLET_NAME had to be supplied."""
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(str(path) for path in roots)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["SCHEMA_INIT_GATE_ROOT"] = str(root)
    disclosure: str | None = None
    if not env.get("SOLET_NAME", "").strip():
        # Several plugins resolve scoped vault-key NAMES from SOLET_NAME at
        # import time and refuse without it, exactly as they would at a real
        # startup -- which always has one. The value never reaches a schema
        # shape, so a gate-local name is faithful; it is disclosed, not silent.
        env["SOLET_NAME"] = _GATE_SOLET_NAME
        disclosure = f"SOLET_NAME was unset; the schema boot ran with SOLET_NAME={_GATE_SOLET_NAME}"
    return env, disclosure


def _run_child(root: Path, entry_points: list[_EntryPoint], dimensions: int) -> tuple[dict[str, object], str]:
    """Boot in a child whose import roots are the tree under test; return (verdict, disclosures)."""
    roots = _import_roots(root)
    env, disclosure = _child_environment(root, roots)
    request = {
        "root": str(root),
        "embedding_dimensions": dimensions,
        "entry_points": [asdict(entry) for entry in entry_points],
    }
    argv = [sys.executable, str(Path(__file__).resolve()), _WORKER_FLAG]
    try:
        completed = subprocess.run(
            argv, input=json.dumps(request), capture_output=True, text=True, env=env, cwd=str(root),
            timeout=_CHILD_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise _ChildCrashError(f"schema boot child timed out after {_CHILD_TIMEOUT_SECONDS}s") from exc
    verdict = _parse_child_verdict(completed)
    return verdict, disclosure or ""


class _ChildCrashError(RuntimeError):
    """The child produced no verdict; exit 70."""


def _parse_child_verdict(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    marker_start, marker_end = "===SCHEMA_INIT_GATE_VERDICT_BEGIN===", "===SCHEMA_INIT_GATE_VERDICT_END==="
    stdout = completed.stdout
    start, end = stdout.find(marker_start), stdout.find(marker_end)
    if start < 0 or end < 0:
        raise _ChildCrashError(
            f"schema boot child exited {completed.returncode} without a verdict.\n"
            f"--- child stdout ---\n{stdout.strip()}\n--- child stderr ---\n{completed.stderr.strip()}",
        )
    payload = stdout[start + len(marker_start):end].strip()
    try:
        verdict = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise _ChildCrashError(f"schema boot child verdict is not JSON: {exc}") from exc
    if not isinstance(verdict, dict):
        raise _ChildCrashError("schema boot child verdict is not an object")
    if verdict.get("usage_error"):
        raise _GateUsageError(str(verdict["usage_error"]))
    return verdict


def _load_allowlist(path: Path) -> frozenset[str]:
    return frozenset(_load_tagged_allowlist(path))


def _findings(verdict: dict[str, object], allowlist: frozenset[str], raw_mode: bool) -> list[_Finding]:
    raw_findings = verdict.get("findings")
    if not isinstance(raw_findings, list):
        raise _ChildCrashError("schema boot child verdict has no findings list")
    findings: list[_Finding] = []
    for entry in raw_findings:
        if not isinstance(entry, dict):
            raise _ChildCrashError("schema boot child finding is not an object")
        key = str(entry["key"])
        findings.append(
            _Finding(
                kind=str(entry["kind"]), key=key, detail=str(entry["detail"]),
                allowlisted=not raw_mode and key in allowlist,
            ),
        )
    return findings


def _scope_touches_schema(scope_file: Path | None, root: Path) -> bool | None:
    """Whether any scoped path names a schema-declaration identifier. Report-only."""
    if scope_file is None:
        return None
    try:
        lines = scope_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise _GateUsageError(f"unreadable scope file {scope_file}: {exc}") from exc
    for line in lines:
        rel = line.strip()
        if not rel or not rel.endswith(".py"):
            continue
        path = root / rel
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if any(marker in text for marker in _SCHEMA_MARKERS):
            return True
    return False


def _print_findings(findings: list[_Finding]) -> None:
    if not findings:
        return
    print("\nschema-init findings:")
    for finding in sorted(findings, key=lambda f: (f.allowlisted, f.kind, f.key)):
        marker = "ALLOWLISTED" if finding.allowlisted else "FINDING    "
        print(f"  {marker}  {finding.kind}  {finding.key}")
        for line in finding.detail.splitlines():
            print(f"               {line}")


def _print_stale(stale: set[str]) -> None:
    if not stale:
        return
    print(f"\n[stale-allowlist] {len(stale)} entry(ies) no longer match any finding:")
    for key in sorted(stale):
        print(f"  [stale-allowlist]  {key}")
    print("  → fixed, renamed, or deleted. Remove these entries.")


def _report(verdict: dict[str, object], findings: list[_Finding], allowlist_active: bool, touching: bool | None) -> int:
    live = [f for f in findings if not f.allowlisted]
    print(
        f"\nschema-init gate: booted {verdict.get('plugins_booted')} plugin(s), "
        f"{verdict.get('schema_count')} schema(s), {verdict.get('installed')} lifecycle install(s) recorded; "
        f"ananta resolved from {verdict.get('ananta_file')}",
    )
    if touching is not None:
        print(f"  scope schema_touching: {'true' if touching else 'false'} (the whole tree was booted regardless)")
    if allowlist_active:
        print(f"  tracked debt (allowlisted, still reported): {len(findings) - len(live)}")
    if live:
        print(f"  ❌ {len(live)} non-allowlisted schema-init finding(s) -- initialize_schemas would crash at startup")
        return _EXIT_BLOCKING
    print("  ✅ initialize_schemas completes for every schema in the tree")
    return _EXIT_OK


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--repo-root", type=Path, required=True, help="The tree under test; must contain this script.")
    parser.add_argument("--allowlist", type=Path, default=None, help="Tracked-debt register (owner/reason/expires schema).")
    parser.add_argument("--raw", action="store_true", help="Diagnostic: allowlist IGNORED; every finding blocks.")
    parser.add_argument("--scope-file", type=Path, default=None, help="Sorted repo-relative landing scope; reported only.")
    parser.add_argument(
        "--embedding-dimensions", type=int, default=_DEFAULT_EMBEDDING_DIMENSIONS,
        help="Placeholder vector width for the dimension-parameterised schemas (never read by the standardizer).",
    )
    return parser


def main(argv: list[str]) -> int:
    if argv[:1] == [_WORKER_FLAG]:
        return _boot_worker()
    args = _build_parser().parse_args(argv)
    try:
        root = resolve_source_root(args.repo_root)
        assert_running_from_source_root(Path(__file__), root)
        entry_points = _entry_points(root)
        touching = _scope_touches_schema(args.scope_file, root)
        allowlist: frozenset[str] = frozenset()
        if args.allowlist is not None:
            allowlist = _load_allowlist(args.allowlist)
        verdict, disclosure = _run_child(root, entry_points, args.embedding_dimensions)
        findings = _findings(verdict, allowlist, args.raw)
    except (SourceRootError, _GateUsageError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return _EXIT_USAGE
    except _ChildCrashError as exc:
        print(f"CRASH: {exc}", file=sys.stderr)
        return _EXIT_CRASH
    if disclosure:
        print(f"note: {disclosure}")
    _print_findings(findings)
    allowlist_active = args.allowlist is not None and not args.raw
    if allowlist_active:
        _print_stale(set(allowlist) - {f.key for f in findings})
    return _report(verdict, findings, allowlist_active, touching)


# ---------------------------------------------------------------------------
# Child side: the actual boot. Imports the platform only here, only from the
# import roots the parent pinned, and proves it before measuring anything.
# ---------------------------------------------------------------------------


class _RecordingSchemaService:
    """Stands in for plugin_schema_service: records installs instead of writing DDL."""

    def __init__(self) -> None:
        self.installed: list[str] = []

    def install_plugin_schema(self, namespace: str, declared_schema_json: dict[str, object]) -> dict[str, str]:
        # Same signature as the state plugins' install_plugin_schema; the
        # canonical JSON shape is what the real lifecycle would write, so an
        # unserializable declaration surfaces here exactly as at startup.
        json.dumps(declared_schema_json)
        self.installed.append(namespace)
        return {"status": "recorded_by_schema_init_gate"}


def _emit(verdict: dict[str, object]) -> int:
    print("===SCHEMA_INIT_GATE_VERDICT_BEGIN===")
    print(json.dumps(verdict, sort_keys=True))
    print("===SCHEMA_INIT_GATE_VERDICT_END===")
    return 0


def _prove_import_root(root: Path) -> str:
    import ananta  # noqa: PLC0415

    ananta_file = Path(str(ananta.__file__)).resolve()
    if not ananta_file.is_relative_to(root.resolve()):
        raise _GateUsageError(
            f"import-root mismatch: ananta resolved to {ananta_file}, outside the tree under test {root}. "
            "The gate would have measured a different checkout (the wrong-tree defect class).",
        )
    return str(ananta_file)


def _boot_plugin_schemas(entries: list[dict[str, str]], findings: list[dict[str, str]]) -> tuple[list[object], int]:
    import importlib  # noqa: PLC0415

    from ananta.core.plugins.capabilities import collect_schemas, is_schema_provider  # noqa: PLC0415

    schemas: list[object] = []
    booted = 0
    for entry in entries:
        plugin_name = entry["plugin_name"]
        try:
            module = importlib.import_module(entry["module"])
            plugin_class = getattr(module, entry["attribute"])
            instance = plugin_class()
            instance.name = plugin_name
            if is_schema_provider(instance):
                schemas.extend(collect_schemas({plugin_name: instance}))
            booted += 1
        except Exception as exc:  # noqa: BLE001 -- every raise here is a startup crash to report
            findings.append(
                {"kind": _FINDING_PLUGIN_BOOT, "key": f"{plugin_name}::boot", "detail": f"{type(exc).__name__}: {exc}"},
            )
    return schemas, booted


def _platform_schemas(dimensions: int) -> list[object]:
    from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: PLC0415
    from ananta.llm.session_ledger.schema import build_session_ledger_event_embeddings_schema  # noqa: PLC0415
    from ananta.services.discovery_service import get_discovery_schema_definitions  # noqa: PLC0415

    schemas: list[object] = list(CoreSchemaDefinitions.get_all_core_schemas())
    schemas.extend(get_discovery_schema_definitions(dimensions))
    schemas.append(build_session_ledger_event_embeddings_schema(dimensions))
    return schemas


def _initialize_each(schemas: list[object], findings: list[dict[str, str]]) -> int:
    """Run the REAL SchemaManager.initialize_schemas per schema; return installs recorded."""
    from typing import cast  # noqa: PLC0415

    from ananta.services.schema_manager import SchemaManager  # noqa: PLC0415
    from ananta.types.schema_types import SchemaDefinition  # noqa: PLC0415

    recorder = _RecordingSchemaService()
    manager = SchemaManager(cast("object", None), recorder)  # type: ignore[arg-type]
    for schema in schemas:
        definition = cast("SchemaDefinition", schema)
        try:
            manager.initialize_schemas([definition])
        except Exception as exc:  # noqa: BLE001 -- the startup path's own refusal IS the finding
            tables = ",".join(sorted(definition.tables))
            findings.append(
                {
                    "kind": _FINDING_SCHEMA_INIT,
                    "key": f"{definition.namespace}::{tables}",
                    "detail": _describe_exception(exc),
                },
            )
    return len(recorder.installed)


def _describe_exception(exc: BaseException) -> str:
    """The startup error and, when it wrapped one, the root cause it wrapped."""
    lines = [f"{type(exc).__name__}: {exc}"]
    cause = exc.__cause__
    while cause is not None:
        lines.append(f"caused by {type(cause).__name__}: {cause}")
        cause = cause.__cause__
    return "\n".join(lines)


def _boot_worker() -> int:
    request = json.loads(sys.stdin.read())
    root = Path(str(request["root"]))
    try:
        ananta_file = _prove_import_root(root)
    except _GateUsageError as exc:
        return _emit({"usage_error": str(exc)})
    findings: list[dict[str, str]] = []
    schemas = _platform_schemas(int(request["embedding_dimensions"]))
    plugin_schemas, booted = _boot_plugin_schemas(list(request["entry_points"]), findings)
    schemas.extend(plugin_schemas)
    installed = _initialize_each(schemas, findings)
    return _emit(
        {
            "ananta_file": ananta_file,
            "plugins_booted": booted,
            "schema_count": len(schemas),
            "installed": installed,
            "findings": findings,
        },
    )


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
