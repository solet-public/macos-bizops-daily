#!/usr/bin/env python3
"""Hermetic contract test for the generated stock-Codex launcher."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.dont_write_bytecode = True

PLUGIN_DIR = Path(__file__).resolve().parents[1]
TEMPLATE_DIR = PLUGIN_DIR / "knowledge_base" / "hydration_templates"
LAUNCHER_TEMPLATE = TEMPLATE_DIR / "codex_launcher.template"
MARKETPLACE_TEMPLATE = TEMPLATE_DIR / "codex_marketplace_json.template"
TOKENS = {
    "{{SOLET_NAME}}": "iris",
    "{{MARKETPLACE_NAME}}": "iris",
    "{{GIT_CONTROLLER_NAME}}": "Git-Controller",
}


class Results:
    def __init__(self) -> None:
        self.checks = 0
        self.failures: list[str] = []

    def check(self, condition: bool, label: str, detail: str = "") -> None:
        self.checks += 1
        if not condition:
            suffix = f": {detail}" if detail else ""
            self.failures.append(f"{label}{suffix}")


def _render(source: str, clone_dir: Path) -> str:
    values = {
        "{{CLONE_DIR}}": str(clone_dir),
        "{{CLONE_DIR_ZSH}}": _zsh_quote(str(clone_dir)),
        **TOKENS,
    }
    rendered = source
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    return rendered


def _zsh_quote(value: str) -> str:
    """Mirror the production hydration renderer's one-word zsh quoting."""

    return "'" + value.replace("'", "'\"'\"'") + "'"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _run_launcher(
    work: Path,
    *,
    transport: str,
    watch_mode: str,
) -> tuple[subprocess.CompletedProcess[str], Path, Path, Path]:
    clone_dir = work / "seed clone"
    bin_dir = work / "bin"
    runtime_dir = work / "runtime"
    clone_dir.mkdir()
    bin_dir.mkdir()

    launcher = work / "codex-iris"
    launcher.write_text(
        _render(LAUNCHER_TEMPLATE.read_text(encoding="utf-8"), clone_dir),
        encoding="utf-8",
    )
    launcher.chmod(0o755)

    watch_marker = work / "watch.marker"
    codex_marker = work / "codex.marker"
    _write_executable(
        bin_dir / "iris",
        """#!/bin/zsh
print -r -- "$@" >"$WATCH_MARKER"
print -r -- "session=$AGENT_SESSION_ID" >>"$WATCH_MARKER"
print -r -- "role=$AGENT_ROLE" >>"$WATCH_MARKER"
if [[ "$WATCH_MODE" == "arm" ]]; then
  print -r -- '{"watch": "armed"}'
  exit 0
fi
print -u2 -- 'synthetic watcher refusal'
exit 7
""",
    )
    _write_executable(
        bin_dir / "stock-codex",
        """#!/bin/zsh
print -r -- "argv=$*" >"$CODEX_MARKER"
print -r -- "identity=$AGENT_IDENTITY" >>"$CODEX_MARKER"
print -r -- "label=$AGENT_SESSION_LABEL" >>"$CODEX_MARKER"
print -r -- "role=$AGENT_ROLE" >>"$CODEX_MARKER"
print -r -- "session=$AGENT_SESSION_ID" >>"$CODEX_MARKER"
print -r -- "wake_cli=$AGENT_WAKE_CLI" >>"$CODEX_MARKER"
print -r -- "transport=$FLEET_TRANSPORT" >>"$CODEX_MARKER"
print -r -- "git_controller=$GIT_CONTROLLER_NAME" >>"$CODEX_MARKER"
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "CODEX_BIN": str(bin_dir / "stock-codex"),
            "CODEX_MARKER": str(codex_marker),
            "PATH": f"{bin_dir}:{env.get('PATH', '')}",
            "WATCH_MARKER": str(watch_marker),
            "WATCH_MODE": watch_mode,
            "XDG_RUNTIME_DIR": str(runtime_dir),
            "iris_FLEET_TRANSPORT": transport,
        }
    )
    proc = subprocess.run(  # noqa: S603
        [str(launcher), "Coordinator-Codex", "--model", "test-model"],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return proc, watch_marker, codex_marker, runtime_dir


def _line_value(body: str, prefix: str) -> str:
    return next(
        (line.removeprefix(prefix) for line in body.splitlines() if line.startswith(prefix)),
        "",
    )


def check_source_contract(res: Results) -> None:
    source = LAUNCHER_TEMPLATE.read_text(encoding="utf-8")
    res.check(source.count('export AGENT_SESSION_ID="ases-') == 1, "session id is single-sourced")
    for required in (
        'export AGENT_IDENTITY="codex"',
        'export AGENT_SESSION_LABEL="$role"',
        'export AGENT_ROLE="$role"',
        'export AGENT_WAKE_CLI="{{SOLET_NAME}}"',
        '--agent-id codex',
        '--role "$AGENT_ROLE"',
        '--exit-with-parent "$$"',
        '"watch": "armed"',
        'exec "$codex_bin" "$@"',
    ):
        res.check(required in source, f"launcher carries {required}")
    res.check(
        source.count("--no-spool") == 0,
        "--no-spool is never passed (CDX-06, 2026-08-24: inbox_consumer.py "
        "is now the Stop handler that drains the wake-hook spool)",
        str(source.count("--no-spool")),
    )
    for forbidden in ("nohup", "disown", "setsid", "--dangerously-", "mcp_servers"):
        occurrences = source.count(forbidden)
        allowed_comment_only = forbidden in {"nohup", "disown", "setsid"} and occurrences == 1
        res.check(
            occurrences == 0 or allowed_comment_only,
            f"launcher does not execute or enable {forbidden}",
            str(occurrences),
        )


def check_watch_launch(res: Results, work: Path) -> None:
    proc, watch_marker, codex_marker, runtime_dir = _run_launcher(
        work,
        transport="watch",
        watch_mode="arm",
    )
    res.check(proc.returncode == 0, "watch launch exits through stock Codex", proc.stderr)
    res.check(watch_marker.is_file(), "watch transport starts the watcher")
    res.check(codex_marker.is_file(), "watch transport execs stock Codex")
    if not watch_marker.is_file() or not codex_marker.is_file():
        return
    watch = watch_marker.read_text(encoding="utf-8")
    codex = codex_marker.read_text(encoding="utf-8")
    watch_argv_line = watch.splitlines()[0]
    res.check(
        watch_argv_line.startswith("watch --agent-id codex --role Coordinator-Codex"),
        "watcher receives the stock identity and durable role",
        watch,
    )
    res.check("--exit-with-parent " in watch_argv_line, "watcher is parent-bound", watch)
    res.check(
        watch_argv_line.split().count("--no-spool") == 0,
        "watcher does NOT receive --no-spool (CDX-06, 2026-08-24: "
        "inbox_consumer.py drains the wake-hook spool as a Stop hook)",
        watch_argv_line,
    )
    res.check(_line_value(codex, "identity=") == "codex", "Codex inherits stock identity")
    res.check(_line_value(codex, "label=") == "Coordinator-Codex", "Codex inherits label")
    res.check(_line_value(codex, "role=") == "Coordinator-Codex", "Codex inherits role")
    res.check(_line_value(codex, "wake_cli=") == "iris", "Codex inherits wake CLI")
    res.check(_line_value(codex, "transport=") == "watch", "Codex inherits watch transport")
    res.check(
        _line_value(codex, "git_controller=") == "Git-Controller",
        "Codex inherits the configured controller role",
    )
    res.check(
        _line_value(watch, "session=") == _line_value(codex, "session="),
        "watcher and Codex inherit the same stable session id",
        f"watch={watch!r} codex={codex!r}",
    )
    logs = list((runtime_dir / "ananta").glob("iris.ases-*.watch.log"))
    res.check(len(logs) == 1, "watch output uses the runtime-dir log convention", repr(logs))


def check_transport_and_failure_controls(res: Results, work: Path) -> None:
    mcp_work = work / "mcp"
    mcp_work.mkdir()
    mcp, watch_marker, codex_marker, _runtime = _run_launcher(
        mcp_work,
        transport="mcp",
        watch_mode="arm",
    )
    res.check(mcp.returncode == 0, "MCP transport still launches stock Codex", mcp.stderr)
    res.check(not watch_marker.exists(), "MCP transport never starts a watcher")
    res.check(codex_marker.is_file(), "MCP transport reaches stock Codex")
    if codex_marker.is_file():
        codex = codex_marker.read_text(encoding="utf-8")
        res.check(_line_value(codex, "transport=") == "mcp", "MCP transport is explicit")

    failed_work = work / "failed"
    failed_work.mkdir()
    failed, failed_watch, failed_codex, _runtime = _run_launcher(
        failed_work,
        transport="watch",
        watch_mode="fail",
    )
    res.check(failed.returncode == 0, "watch refusal does not suppress stock Codex", failed.stderr)
    res.check(failed_watch.is_file(), "watch refusal ran the intended watcher")
    res.check(failed_codex.is_file(), "watch refusal still reaches stock Codex")
    res.check("watcher did not arm; see " in failed.stderr, "watch refusal is visible")
    res.check("synthetic watcher refusal" in failed.stderr, "watch refusal includes the log tail")


def check_marketplace(res: Results, work: Path) -> None:
    rendered = _render(MARKETPLACE_TEMPLATE.read_text(encoding="utf-8"), work)
    value = json.loads(rendered)
    res.check(value["name"] == "iris", "Codex marketplace uses the derived seed name")
    res.check(len(value["plugins"]) == 1, "Codex marketplace has one reviewed plugin")
    plugin = value["plugins"][0]
    res.check(plugin["name"] == "coordination-hooks", "Codex marketplace names the plugin")
    res.check(plugin["source"]["source"] == "local", "Codex marketplace uses local source")
    res.check(
        plugin["source"]["path"]
        == "./plugins/github_midwife_plugin/codex_plugin/coordination-hooks",
        "Codex marketplace resolves to the shipped Codex plugin bytes",
    )
    res.check(plugin["policy"]["installation"] == "AVAILABLE", "plugin remains operator-installable")
    res.check(plugin["policy"]["authentication"] == "ON_INSTALL", "install requires operator action")


def main() -> int:
    res = Results()
    check_source_contract(res)
    with tempfile.TemporaryDirectory(prefix="codex-hydration-launcher-") as raw:
        work = Path(raw)
        watch_work = work / "watch"
        watch_work.mkdir()
        check_watch_launch(res, watch_work)
        check_transport_and_failure_controls(res, work)
        check_marketplace(res, work)
    if res.failures:
        print("codex_hydration_launcher_smoke FAILED", file=sys.stderr)
        for failure in res.failures:
            print(f"- {failure}", file=sys.stderr)
        return 1
    print(f"codex_hydration_launcher_smoke OK: {res.checks} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
