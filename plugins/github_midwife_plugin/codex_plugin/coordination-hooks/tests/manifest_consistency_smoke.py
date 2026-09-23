#!/usr/bin/env python3
"""Pin the reviewed stock-Codex hook inventory and routing contract."""

from __future__ import annotations

import ast
import importlib.util
import json
import re
import sys
from collections import Counter
from pathlib import Path
from types import ModuleType
from typing import Any

sys.dont_write_bytecode = True

from _harness import HOOKS_DIR, PLUGIN_ROOT, Results, preflight  # noqa: E402

# Cadence ruling (2026-08-11, cadence-only): step_zero and check_messages
# moved from UserPromptSubmit to SessionStart(startup|resume|clear) so they
# stop re-firing every prompt turn. The 2026-08-01 always-armed ruling for
# step_zero_reminder.js is untouched by this move (no environment gate was
# added or removed) and stays pinned by reminder_hooks_smoke.py's
# check_step_zero_fires_everywhere. RED MUTATION for this Counter: re-adding
# either reminder to any UserPromptSubmit entry, or dropping either from the
# SessionStart(startup|resume|clear) group.
#
# The current official hook contract defines background command hooks. Live
# stock-process execution remains a separate acceptance leg. The context
# reporter is deliberately async so transcript parsing and the platform call
# cannot hold the turn boundary; it reports context only and never requests
# continuation or consumes peer messages. CDX-06 (2026-08-24) adds a SECOND
# Stop hook, inbox_consumer.py, which is deliberately SYNCHRONOUS -- measured
# live (workbench/2026-08-24_cdx06_isolated_stop_hook_proof.md) that an
# async hook's decision output is discarded, so only a synchronous hook can
# gate/continue a turn. The finite observer has an eight-second manifest
# deadline around its two-second wake and report children; unknown wake results
# deliberately do not report a completed observation.
EXPECTED = Counter(
    {
        ("SessionStart", "startup|resume|clear", "step_zero_reminder.js"): 1,
        ("SessionStart", "startup|resume|clear", "check_messages_reminder.js"): 1,
        ("SessionStart", "startup|resume|clear", "role_binding_reminder.js"): 1,
        ("PreToolUse", "^Bash$", "git_controller_gate.py"): 1,
        ("Stop", "", "context_status_reporter.py"): 1,
        ("Stop", "", "inbox_consumer.py"): 1,
    }
)
COMMAND_RE = re.compile(
    r"^(node|python3) \$\{PLUGIN_ROOT\}/hooks/([A-Za-z0-9_]+\.(?:js|py))$"
)
HOOK_KEYWORDS = {
    "step_zero_reminder.js": ("project-orientation", "step-zero"),
    "check_messages_reminder.js": ("coordination reminder", "unread-message"),
    "role_binding_reminder.js": ("role-binding", "role binding"),
    "git_controller_gate.py": ("git-controller", "git controller"),
    "context_status_reporter.py": ("context-status", "context status"),
    "inbox_consumer.py": ("inbox-consumer", "peer inbox"),
}
DOCUMENTS = (
    "hooks/hooks.json",
    "README.md",
    "SECURITY.md",
    ".codex-plugin/plugin.json",
)
NETWORK_MODULES = frozenset(
    {"aiohttp", "http", "httpx", "requests", "socket", "urllib", "websocket", "websockets"}
)
PROCESS_MODULES = frozenset({"commands", "pty", "subprocess"})
PYTHON_WRITE_CALLS = frozenset(
    {"makedirs", "mkdir", "open", "remove", "rename", "replace", "unlink", "write_bytes", "write_text"}
)
NODE_WRITE_TOKENS = (
    "appendFile",
    "copyFile",
    "createWriteStream",
    "mkdir",
    "rename",
    "rmSync",
    "unlink",
    "writeFile",
)
NODE_PROCESS_TOKENS = ("child_process", "execFile", "execSync", "spawn(", "spawnSync")


def _load_gate() -> ModuleType:
    path = HOOKS_DIR / "git_controller_gate.py"
    spec = importlib.util.spec_from_file_location("codex_gate_manifest_smoke", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _entry_points() -> set[str]:
    return {
        path.name
        for path in HOOKS_DIR.iterdir()
        if path.is_file()
        and path.suffix in {".js", ".py"}
        and not path.name.startswith("_")
    }


def _python_imports(tree: ast.AST) -> set[str]:
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imports.add(node.module.split(".")[0])
    return imports


def _check_javascript_source(res: Results, path: Path, source: str) -> None:
    requires = set(re.findall(r"require\(\s*['\"]([^'\"]+)['\"]\s*\)", source))
    res.check(
        requires <= {"fs"},
        f"{path.name} requires only reviewed built-ins",
        repr(requires),
    )
    res.check(
        not any(token in source for token in NODE_WRITE_TOKENS),
        f"{path.name} has no file-write primitive",
    )
    process_tokens = [token for token in NODE_PROCESS_TOKENS if token in source]
    res.check(
        not process_tokens,
        f"{path.name} has no child-process primitive",
        repr(process_tokens),
    )


def _check_python_source(res: Results, path: Path, source: str) -> None:
    tree = ast.parse(source, filename=path.name)
    imports = _python_imports(tree)
    external = {
        module
        for module in imports
        if module not in sys.stdlib_module_names
        and not (HOOKS_DIR / f"{module}.py").is_file()
    }
    res.check(
        not external,
        f"{path.name} imports only stdlib or local modules",
        repr(sorted(external)),
    )
    res.check(
        not imports.intersection(NETWORK_MODULES),
        f"{path.name} imports no network module",
    )
    process_imports = imports.intersection(PROCESS_MODULES)
    if path.name in {"context_status_reporter.py", "inbox_consumer.py"}:
        res.check(
            process_imports == {"subprocess"},
            f"{path.name} imports only its declared process module",
            repr(sorted(process_imports)),
        )
    else:
        res.check(
            not process_imports,
            f"{path.name} imports no process module",
            repr(sorted(process_imports)),
        )
    call_names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, (ast.Attribute, ast.Name))
    }
    write_calls = call_names.intersection(PYTHON_WRITE_CALLS)
    res.check(
        not write_calls,
        f"{path.name} has no explicit file-write call",
        repr(sorted(write_calls)),
    )


def _check_source_contract(res: Results) -> None:
    for path in sorted(HOOKS_DIR.iterdir()):
        if not path.is_file() or path.suffix not in {".js", ".py"}:
            continue
        source = path.read_text(encoding="utf-8")
        if path.suffix == ".js":
            _check_javascript_source(res, path, source)
            continue
        _check_python_source(res, path, source)


def _record_entry(
    res: Results,
    event: str,
    matcher: str,
    entry: object,
    actual: Counter[tuple[str, str, str]],
    referenced: set[str],
) -> None:
    res.check(isinstance(entry, dict), f"{event} hook entry is an object")
    if not isinstance(entry, dict):
        return
    res.check(entry.get("type") == "command", f"{event} handler is a command")
    command = entry.get("command")
    match = COMMAND_RE.fullmatch(command) if isinstance(command, str) else None
    res.check(
        match is not None,
        f"{event} command has the stock reviewed shape",
        repr(command),
    )
    res.check(
        isinstance(entry.get("timeout"), int) and entry["timeout"] > 0,
        f"{event} timeout is a positive integer",
    )
    res.check(
        isinstance(entry.get("statusMessage"), str) and bool(entry["statusMessage"]),
        f"{event} statusMessage is non-empty",
    )
    if match is None:
        return
    script = match.group(2)
    if script == "context_status_reporter.py":
        res.check(entry.get("async") is True, "context reporter is explicitly async")
    else:
        res.check("async" not in entry, f"{script} has no undeclared async mode")
    if script == "inbox_consumer.py":
        res.check(
            entry.get("timeout") == 8,
            "inbox consumer has the paired 8-second Stop timeout",
            repr(entry.get("timeout")),
        )
    referenced.add(script)
    actual[(event, matcher, script)] += 1


def _record_group(
    res: Results,
    event: str,
    group: object,
    actual: Counter[tuple[str, str, str]],
    referenced: set[str],
) -> None:
    res.check(isinstance(group, dict), f"{event} group is an object")
    if not isinstance(group, dict):
        return
    matcher = group.get("matcher", "")
    entries = group.get("hooks")
    res.check(isinstance(matcher, str), f"{event} matcher is a string")
    res.check(isinstance(entries, list), f"{event} group has a hook list")
    if not isinstance(matcher, str) or not isinstance(entries, list):
        return
    for entry in entries:
        _record_entry(res, event, matcher, entry, actual, referenced)


def _collect_inventory(
    res: Results, hooks: dict[object, object]
) -> tuple[Counter[tuple[str, str, str]], set[str]]:
    actual: Counter[tuple[str, str, str]] = Counter()
    referenced: set[str] = set()
    for event, groups in hooks.items():
        res.check(isinstance(event, str), "event name is a string")
        res.check(isinstance(groups, list), f"{event} groups are a list")
        if not isinstance(event, str) or not isinstance(groups, list):
            continue
        for group in groups:
            _record_group(res, event, group, actual, referenced)
    return actual, referenced


def _check_documentation(res: Results, referenced: set[str]) -> None:
    entry_points = _entry_points()
    res.check(
        referenced == entry_points,
        "every handler on disk is registered exactly by inventory",
    )
    documents = {
        document: (PLUGIN_ROOT / document).read_text(encoding="utf-8").lower()
        for document in DOCUMENTS
    }
    for name in sorted(referenced):
        res.check((HOOKS_DIR / name).is_file(), f"registered handler exists: {name}")
        terms = (name, *HOOK_KEYWORDS[name])
        for document, text in documents.items():
            res.check(
                any(term in text for term in terms),
                f"{document} names {name}",
                f"none of {terms!r} present",
            )


def _check_gate_routing(
    res: Results, actual: Counter[tuple[str, str, str]]
) -> None:
    gate = _load_gate()
    routed = getattr(gate, "CODEX_GATED_TOOL_NAMES", None)
    res.check(routed == frozenset({"Bash"}), "gate dispatch surface is exactly Bash")
    pretool_matchers = {
        matcher
        for event, matcher, script in actual
        if event == "PreToolUse" and script == "git_controller_gate.py"
    }
    res.check(
        pretool_matchers == {"^Bash$"},
        "manifest routes exactly the gate dispatch surface",
    )


def _check_security_claim(res: Results) -> None:
    security = " ".join(
        (PLUGIN_ROOT / "SECURITY.md").read_text(encoding="utf-8").split()
    )
    adversarial_claim = (
        "even when `AGENT_IDENTITY`, `AGENT_INSTANCE_ID`, `AGENT_SESSION_LABEL`, "
        "`AGENT_SESSION_ID`, and the hook payload's `session_id` are all set to "
        "`Git-Controller`, a missing or non-controller `AGENT_ROLE` still blocks a "
        "detected mutation."
    )
    res.check(
        adversarial_claim in security,
        "SECURITY.md pins the adversarial identity result",
    )


def _check_stop_binding(res: Results, hooks: dict[object, object]) -> None:
    """The reporter must remain a single background Stop definition."""
    stop_groups = hooks.get("Stop")
    res.check(isinstance(stop_groups, list), "manifest carries the Stop reporter binding")
    if not isinstance(stop_groups, list):
        return
    res.check(len(stop_groups) == 1, "manifest carries one Stop group", repr(stop_groups))


def main() -> int:
    preflight()
    res = Results("Codex plugin-manifest consistency")
    raw: Any = json.loads((HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"))
    res.check(isinstance(raw, dict), "hooks.json is an object")
    if not isinstance(raw, dict):
        return res.finish()
    res.check(set(raw) == {"hooks"}, "stock manifest has only the top-level hooks key")
    hooks = raw.get("hooks")
    res.check(isinstance(hooks, dict), "hooks is an object")
    if not isinstance(hooks, dict):
        return res.finish()

    actual, referenced = _collect_inventory(res, hooks)

    res.check(actual == EXPECTED, "registered event/matcher/script inventory is exact", repr(actual))
    _check_stop_binding(res, hooks)
    _check_documentation(res, referenced)
    _check_gate_routing(res, actual)
    _check_source_contract(res)
    _check_security_claim(res)
    return res.finish()


if __name__ == "__main__":
    raise SystemExit(main())
