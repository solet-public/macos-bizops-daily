#!/usr/bin/env python3
"""Validate every setup-flow probe against the source resolver that owns it.

The macOS setup flow uses three closed resolver surfaces.  Bootstrap probes
resolve through ``bootstrap_adapter.routes._ROUTES``; target-local probes
resolve through ``probe_handlers`` or ``operation_handlers``; and only the
references assigned to ``_GENERIC_PROCESS_REFS`` relay to the platform process
registry.  Consequently, a ``403 bridge.process_not_allowed`` is never used as
evidence that a process exists: a bridge may apply export policy before an
existence lookup.

This gate is deliberately offline.  ``--live-schema`` is an optional audit
instrument, not a gate prerequisite.  It compares only platform-registry
references, classifying a 403 as undecidable rather than registered.
"""

from __future__ import annotations

import argparse
import ast
import json
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

_REPO_IMPORT_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_IMPORT_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_IMPORT_ROOT))

from quality_gates.allowlist_schema import load_allowlist as _load_tagged_allowlist  # noqa: E402

_FLOW = Path("plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json")
_BOOTSTRAP_ROUTES = Path("bootstrap_adapter/routes.py")
_INSTALLATION_DOCTOR = Path("plugins/github_midwife_plugin/src/github_midwife_plugin/installation_doctor.py")
_SETUP_OPERATIONS = Path("plugins/github_midwife_plugin/src/github_midwife_plugin/setup_operations.py")
_ALLOWLIST = Path("quality_gates/flow_probe_registry_gate_allowlist.txt")
_SERVICE_ROOT = Path("ananta/src/ananta/services")
_PLUGIN_ROOT = Path("plugins")


@dataclass(frozen=True)
class Finding:
    """One source-derived resolver failure."""

    check: str
    probe_id: str
    probe_ref: str
    detail: str
    allowlist_key: str | None = None

    def render(self, allowed: bool) -> str:
        prefix = "[allowlisted] " if allowed else ""
        return f"{prefix}{self.check} probe={self.probe_id!r} ref={self.probe_ref!r}: {self.detail}"


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _string_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Constant):
            continue
        if not isinstance(node.value.value, str):
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                constants[target.id] = node.value.value
    return constants


def _decorator_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Call):
        return _decorator_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _keyword_string(decorator: ast.Call, name: str, constants: dict[str, str]) -> str | None:
    for keyword in decorator.keywords:
        if keyword.arg != name:
            continue
        if isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
            return keyword.value.value
        if isinstance(keyword.value, ast.Name):
            return constants.get(keyword.value.id)
    return None


def _platform_processes(root: Path) -> set[str]:
    """Return platform keys declared by the two registration decorators."""
    return (_service_declarations(root) | _plugin_declarations(root)) & _manifest_keys(root)


def _manifest_keys(root: Path) -> set[str]:
    paths = (root / "ananta/knowledge_base/processes").glob("*/*.json")
    paths = (*paths, *(root / _PLUGIN_ROOT).glob("*/knowledge_base/processes/*.json"))
    return {payload["process_key"] for path in paths if isinstance((payload := json.loads(path.read_text(encoding="utf-8"))).get("process_key"), str)}


def _service_declarations(root: Path) -> set[str]:
    return _decorated_keys(root / _SERVICE_ROOT, "service_interface_process", "service_interface")


def _plugin_declarations(root: Path) -> set[str]:
    return _decorated_keys(root / _PLUGIN_ROOT, "platform_process", "plugin")


def _decorated_keys(root: Path, decorator_name: str, prefix: str) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*.py"):
        found.update(_keys_from_path(path, root, decorator_name, prefix))
    return found


def _keys_from_path(path: Path, root: Path, decorator_name: str, prefix: str) -> set[str]:
    if path.name == "__init__.py" or any(part.startswith(".venv") for part in path.parts):
        return set()
    constants = _string_constants(tree := _parse(path))
    provider = path.relative_to(root).parts[0] if prefix == "plugin" else None
    return {key for node in _decorated_functions(tree) for decorator in node.decorator_list if isinstance(decorator, ast.Call) and _decorator_name(decorator) == decorator_name for key in _decorator_keys(decorator, node.name, prefix, provider, constants)}


def _decorated_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    return [node for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]


def _decorator_keys(decorator: ast.Call, function_name: str, prefix: str, plugin_name: str | None, constants: dict[str, str]) -> set[str]:
    provider = plugin_name if prefix == "plugin" else _keyword_string(decorator, "provider", constants)
    if provider is None:
        return set()
    return {f"{prefix}::{provider}::{_keyword_string(decorator, 'name', constants) or function_name}"}


def _string_set_assignment(tree: ast.Module, name: str) -> set[str]:
    for node in tree.body:
        if not isinstance(node, ast.Assign) or not any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            continue
        if not isinstance(node.value, (ast.Set, ast.List, ast.Tuple)):
            return set()
        return {item.value for item in node.value.elts if isinstance(item, ast.Constant) and isinstance(item.value, str)}
    return set()


def _dict_items_in_function(tree: ast.Module, function_name: str) -> dict[str, str]:
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef) or node.name != function_name:
            continue
        items: dict[str, str] = {}
        for child in ast.walk(node):
            if not isinstance(child, ast.Dict):
                continue
            for key, value in zip(child.keys, child.values, strict=True):
                if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Name):
                    items[key.value] = value.id
        return items
    return {}


def _bootstrap_route_refs(root: Path) -> set[str]:
    tree = _parse(root / _BOOTSTRAP_ROUTES)
    return {ref for node in tree.body for ref in _route_refs(node)}


def _route_refs(node: ast.stmt) -> set[str]:
    if not isinstance(node, ast.AnnAssign) or not isinstance(node.value, ast.Dict):
        return set()
    if not isinstance(node.target, ast.Name) or node.target.id != "_ROUTES":
        return set()
    return {value.elts[0].value for value in node.value.values if isinstance(value, ast.Tuple) and value.elts and isinstance(value.elts[0], ast.Constant) and isinstance(value.elts[0].value, str)}


def _function_process_calls(tree: ast.Module) -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {}
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef):
            continue
        for child in ast.walk(node):
            if not isinstance(child, ast.Call) or _decorator_name(child.func) != "_solet_call":
                continue
            if len(child.args) > 2 and isinstance(child.args[2], ast.Constant) and isinstance(child.args[2].value, str):
                calls.setdefault(node.name, set()).add(child.args[2].value)
    return calls


def _target_local_refs(root: Path) -> tuple[dict[str, str], set[str], dict[str, set[str]]]:
    doctor = _parse(root / _INSTALLATION_DOCTOR)
    operations = _parse(root / _SETUP_OPERATIONS)
    generic = _string_set_assignment(doctor, "_GENERIC_PROCESS_REFS")
    direct = _dict_items_in_function(doctor, "probe_handlers")
    direct.update(_dict_items_in_function(operations, "operation_handlers"))
    calls: dict[str, set[str]] = {}
    source_root = root / "plugins/github_midwife_plugin/src/github_midwife_plugin"
    for path in source_root.rglob("*.py"):
        for name, keys in _function_process_calls(_parse(path)).items():
            calls.setdefault(name, set()).update(keys)
    return direct, generic, calls


def _process_key(reference: str) -> str | None:
    try:
        prefix, remainder = reference.split("::", maxsplit=1)
        provider, function = remainder.rsplit(".", maxsplit=1)
    except ValueError:
        return None
    if prefix not in {"plugin", "service_interface"}:
        return None
    return f"{prefix}::{provider}::{function}"


def _load_flow(root: Path, flow_path: Path) -> dict[str, dict[str, str]]:
    raw = json.loads((root / flow_path).read_text(encoding="utf-8"))
    probes = raw.get("probes")
    if not isinstance(probes, dict):
        raise ValueError("flow.probes must be an object")
    result: dict[str, dict[str, str]] = {}
    for probe_id, definition in probes.items():
        if not isinstance(probe_id, str) or not isinstance(definition, dict):
            raise ValueError("flow.probes has a non-object entry")
        probe_ref = definition.get("probe_ref")
        runner = definition.get("runner")
        if not isinstance(probe_ref, str) or not isinstance(runner, str):
            raise ValueError(f"probe {probe_id!r} must declare string probe_ref and runner")
        result[probe_id] = {"probe_ref": probe_ref, "runner": runner}
    return result


def find_findings(root: Path, flow_path: Path) -> tuple[list[Finding], set[str]]:
    """Resolve every declared probe using the source-owned resolver precedence."""
    flow = _load_flow(root, flow_path)
    bootstrap = _bootstrap_route_refs(root)
    direct, generic, handler_calls = _target_local_refs(root)
    registered = _platform_processes(root)
    findings: list[Finding] = []
    registry_keys: set[str] = set()
    for probe_id, definition in flow.items():
        found, keys = _resolve_probe(probe_id, definition, bootstrap, direct, generic, handler_calls, registered)
        findings.extend(found)
        registry_keys.update(keys)
    return findings, registry_keys


def _resolve_probe(probe_id: str, definition: dict[str, str], bootstrap: set[str], direct: dict[str, str], generic: set[str], calls: dict[str, set[str]], registered: set[str]) -> tuple[list[Finding], set[str]]:
    reference = definition["probe_ref"]
    if definition["runner"] == "manager":
        return [], set()
    if definition["runner"] == "bootstrap":
        return _bootstrap_finding(probe_id, reference, bootstrap), set()
    if reference in direct:
        return _dedicated_findings(probe_id, reference, direct[reference], calls, registered), set()
    return _relayed_findings(probe_id, reference, generic, registered)


def _bootstrap_finding(probe_id: str, reference: str, routes: set[str]) -> list[Finding]:
    return [] if reference in routes else [Finding("FPR-BOOTSTRAP-MISSING", probe_id, reference, "not declared in bootstrap_adapter.routes._ROUTES")]


def _dedicated_findings(probe_id: str, reference: str, handler: str, calls: dict[str, set[str]], registered: set[str]) -> list[Finding]:
    declared = _process_key(reference)
    findings: list[Finding] = []
    for actual in calls.get(handler, set()):
        if actual not in registered:
            findings.append(Finding("FPR-DEDICATED-ACTUAL-MISSING", probe_id, reference, f"dedicated handler {handler!r} calls unregistered {actual}", actual))
        if declared != actual:
            findings.append(Finding("FPR-DEDICATED-DRIFT", probe_id, reference, f"dedicated handler {handler!r} calls {actual}, not declared {declared or reference}", f"dedicated::{probe_id}::{declared or reference}::{actual}"))
    return findings


def _relayed_findings(probe_id: str, reference: str, generic: set[str], registered: set[str]) -> tuple[list[Finding], set[str]]:
    if reference not in generic:
        return [Finding("FPR-TARGET-LOCAL-MISSING", probe_id, reference, "not declared by target-local probe_handlers() or operation_handlers()")], set()
    key = _process_key(reference)
    if key is None:
        return [Finding("FPR-REGISTRY-REF-MALFORMED", probe_id, reference, "generic process reference cannot form a plugin or service_interface process key")], set()
    return ([] if key in registered else [Finding("FPR-REGISTRY-MISSING", probe_id, reference, f"platform registry source has no @service_interface_process or @platform_process declaration for {key}", key)]), {key}


def load_allowlist(path: Path) -> set[str]:
    """Mandatory owner/reason/expires schema (D-3-structural, iss_23fa51b5)."""

    return set(_load_tagged_allowlist(path))


def classify_live_schema(exit_code: int, output: str) -> str:
    """Classify bridge schema output without mistaking export denial for existence."""
    if exit_code == 0:
        return "registered"
    if "bridge.invalid_process_key" in output:
        return "missing"
    if "bridge.process_not_allowed" in output:
        return "undecidable"
    return "error"


def _live_findings(keys: Iterable[str], command: str) -> list[Finding]:
    findings: list[Finding] = []
    for key in sorted(keys):
        completed = subprocess.run(  # noqa: S603 -- operator-selected schema binary
            [command, "schema", key], capture_output=True, text=True, check=False
        )
        live = classify_live_schema(completed.returncode, completed.stdout + completed.stderr)
        if live == "undecidable":
            print(f"LIVE UNDECIDABLE {key}: export policy denied pre-existence lookup")
        elif live == "error":
            findings.append(Finding("FPR-LIVE-SCHEMA-ERROR", key, key, "schema command did not return a recognizable bridge result"))
    return findings


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--flow", type=Path, default=_FLOW)
    parser.add_argument("--allowlist", type=Path, default=_ALLOWLIST)
    parser.add_argument("--live-schema", metavar="COMMAND", help="optional bridge command for a non-blocking source/live audit")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _run_gate(Path.cwd(), args)


def _run_gate(root: Path, args: argparse.Namespace) -> int:
    try:
        findings, keys = find_findings(root, args.flow)
        allowlist = load_allowlist(root / args.allowlist)
    except (OSError, SyntaxError, ValueError, json.JSONDecodeError) as exc:
        print(f"FPR HARNESS ERROR: {exc}", file=sys.stderr)
        return 2
    _append_stale_allowlist_findings(findings, allowlist)
    blocking = _render_findings(findings, allowlist)
    blocking = _render_live_findings(blocking, keys, args.live_schema)
    print(f"flow_probe_registry_gate: {len(keys)} registry refs, {len(findings)} findings")
    return 1 if blocking else 0


def _append_stale_allowlist_findings(findings: list[Finding], allowlist: set[str]) -> None:
    active = {finding.allowlist_key for finding in findings if finding.allowlist_key}
    findings.extend(Finding("FPR-ALLOWLIST-STALE", key, key, "allowlist entry no longer names a current missing registry key") for key in sorted(allowlist - active))


def _render_findings(findings: list[Finding], allowlist: set[str]) -> bool:
    blocking = False
    for finding in findings:
        allowed = finding.allowlist_key is not None and finding.allowlist_key in allowlist
        print(finding.render(allowed))
        blocking = blocking or not allowed
    return blocking


def _render_live_findings(blocking: bool, keys: set[str], command: str | None) -> bool:
    if command is None:
        return blocking
    for finding in _live_findings(keys, command):
        print(finding.render(False))
        blocking = True
    return blocking


if __name__ == "__main__":
    raise SystemExit(main())
