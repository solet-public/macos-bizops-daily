"""Update refusals that used to drop the outcome they had just read now report it (iss_67d2597e, public #86).

Hermetic, on the same fake runtime and Git target as ``existing_install_operations_smoke``:

- ``export_root_ambiguous`` names each installed connector with the roots it holds, prints ``(none)`` for one that holds
  none, and stays inside the 512 characters ``result()`` cuts a repair at, ellipsizing entries rather than dropping a
  connector or the remedy;
- a refused ``claude plugin uninstall`` / ``install`` / ``marketplace add`` carries the exit code (or the timeout) and
  the CLI's stderr, with secret-shaped text removed, because the Manager refuses a whole envelope that carries it;
- ``coding_agent_running`` says when ``pgrep`` could not answer, and why, instead of saying a process is running.
"""

from __future__ import annotations

import json
import plistlib
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast

_REPO = Path(__file__).resolve().parents[3]
sys.path[:0] = [str(Path(__file__).resolve().parent), str(_REPO / "solet_cli" / "src"), str(_REPO / "solet_setup_contracts" / "src")]
import existing_install_operations_smoke as base  # noqa: E402
from existing_install_operations_smoke import FakeRuntime, _request  # noqa: E402
from github_midwife_plugin.export_root_validation import BUSINESS_CONNECTOR_PLUGINS  # noqa: E402
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import REPAIR_LIMIT  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, bounded_command_outcome, describe_outcome  # noqa: E402
from solet_manager.adapter_validation import public_string  # noqa: E402

_CHECKS = 0
_NAME = base._NAME  # noqa: SLF001
_EXPORT = "existing::migration.export_root_containment"
_RENAME = "existing::migration.solet_rename"
_CACHE = "existing::runtime.plugin_cache_refresh"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _Runtime(FakeRuntime):
    """The operations smoke's runtime, plus a pgrep that can fail and Claude CLI steps that can fail or find nothing installed."""

    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.pgrep_outcome: CommandOutcome | None = None
        self.claude_visible = True
        self.claude_failures: dict[tuple[str, ...], CommandOutcome] = {}

    def _claude(self, argv: tuple[str, ...], output_limit: int) -> CommandOutcome:
        if argv[1:3] in self.claude_failures:
            return self.claude_failures[argv[1:3]]
        if argv[1:3] == ("plugin", "list") and not self.claude_visible:
            return bounded_command_outcome(returncode=0, timed_out=False, duration_ms=1, stdout="[]", stderr="", output_limit=output_limit)
        return super()._claude(argv, output_limit)

    def _host_probe(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[:2] == ("/usr/bin/pgrep", "-x") and self.pgrep_outcome is not None:
            return self.pgrep_outcome
        return super()._host_probe(argv)


def _dispatch(target: Path, runtime: _Runtime, ref: str, *, phase: str = "probe") -> dict[str, Any]:
    return cast(dict[str, Any], dispatch_request(_request(target, ref, phase=phase), runtime))


def _ambiguous_repair(target: Path, runtime: _Runtime) -> str:
    refused = _dispatch(target, runtime, _EXPORT)
    _check(refused["checkpoint_status"] == "blocked" and refused["error_kind"] == "export_root_ambiguous", f"disagreeing connectors refuse as export_root_ambiguous: {refused['checkpoint_status']} {refused['error_kind']}")
    return cast(str, refused["repair"])


def _check_export_root(root: Path) -> None:
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    runtime.home.mkdir()
    config = target / "profile" / "config" / "plugins"
    config.mkdir(parents=True)
    work, stale = str(root / "work"), str(root / "stale")
    for plugin, roots in (("jira_plugin", [work]), ("salesforce_plugin", [work, stale])):
        (target / "plugins" / plugin).mkdir()
        (config / f"{plugin}.json").write_text(json.dumps({"export_allowed_roots": roots}))
    repair = _ambiguous_repair(target, runtime)
    _check(f"salesforce_plugin={work}, {stale}; jira_plugin={work}." in repair, f"the refusal names both connectors and their roots, in the connector roster's order: {repair}")
    _check("profile/config/plugins/<plugin>.json" in repair and f"solet-manager update {_NAME} --dry-run" in repair, f"the refusal names the files to edit and the command to run again: {repair[-260:]}")
    empty = next(plugin for plugin in BUSINESS_CONNECTOR_PLUGINS if plugin not in ("jira_plugin", "salesforce_plugin"))
    (target / "plugins" / empty).mkdir()
    _check(f"{empty}=(none)" in _ambiguous_repair(target, runtime), "a connector holding no root prints (none)")
    for plugin in BUSINESS_CONNECTOR_PLUGINS:
        (target / "plugins" / plugin).mkdir(exist_ok=True)
        (config / f"{plugin}.json").write_text(json.dumps({"export_allowed_roots": [f"/{plugin}/{'a' * 300}", f"/{plugin}/{'b' * 300}"]}))
    long_repair = _ambiguous_repair(target, runtime)
    _check(len(long_repair) <= REPAIR_LIMIT, f"seven connectors with 300-character roots stay inside the {REPAIR_LIMIT}-character cap result() cuts at: {len(long_repair)}")
    _check(all(f"{plugin}=" in long_repair for plugin in BUSINESS_CONNECTOR_PLUGINS) and "…" in long_repair, f"an over-long listing ellipsizes each entry and still names every connector: {long_repair}")
    _check(long_repair.endswith("--dry-run again."), f"and keeps the remedy, which result() would otherwise cut off: {long_repair[-120:]}")


def _check_coding_agent_state(root: Path) -> None:
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    (runtime.home / "Library" / "LaunchAgents").mkdir(parents=True)
    (runtime.home / "Library" / "LaunchAgents" / f"local.homunculus.{_NAME}.plist").write_bytes(plistlib.dumps({"Label": f"local.homunculus.{_NAME}", "ProgramArguments": ["--homunculus", _NAME], "EnvironmentVariables": {"HOMUNCULUS_NAME": _NAME}}))
    (runtime.home / ".claude.json").write_text(json.dumps({"mcpServers": {_NAME: {"env": {"HOMUNCULUS_NAME": _NAME}}}}))
    runtime.claude_running = True
    running = _dispatch(target, runtime, _RENAME, phase="apply")
    _check(running["error_kind"] == "coding_agent_running" and "could not be determined" not in running["repair"], f"a running Claude Code is reported as running: {running['repair']}")
    runtime.claude_running = False
    for outcome, reason in ((CommandOutcome(None, False, 0, "", "", executable_missing=True), "pgrep missing"), (CommandOutcome(None, True, 0, "", ""), "pgrep timed out"), (CommandOutcome(2, False, 1, "", ""), "pgrep exited 2")):
        runtime.pgrep_outcome = outcome
        unknown = _dispatch(target, runtime, _RENAME, phase="apply")
        _check(unknown["error_kind"] == "coding_agent_running" and "could not be determined" in unknown["repair"] and reason in unknown["repair"] and unknown["repair"] != running["repair"], f"an undetermined Claude Code state names why ({reason}): {unknown['repair']}")


def _cache_target(root: Path) -> tuple[Path, _Runtime]:
    """The operations smoke's plugin-cache fixture, settled as create leaves it, then with a stale hook so a reinstall is planned."""
    target, _ = base._target(root)  # noqa: SLF001
    runtime = _Runtime(root / "home")
    runtime.home.mkdir()
    cache = runtime.home / ".claude" / "plugins" / "cache" / _NAME / "coordination-hooks" / "1.0.0"
    (cache / "hooks").mkdir(parents=True)
    (target / "profile").mkdir(exist_ok=True)
    (target / ".venv/bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin/python3").write_text("fixture")
    (runtime.home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {f"coordination-hooks@{_NAME}": [{"installPath": str(cache)}]}}))
    base._copy_hooks(target, cache)  # noqa: SLF001
    base._settle_as_create(target, runtime, cache)  # noqa: SLF001
    (cache / "hooks" / "wake_waiter.py").write_bytes(b"# stale\n")
    return target, runtime


def _check_cli_refusals(root: Path) -> None:
    target, runtime = _cache_target(root)
    for step, kind in (("uninstall", "claude_plugin_uninstall_failed"), ("install", "claude_plugin_install_failed")):
        runtime.claude_failures = {("plugin", step): CommandOutcome(7, False, 1, "", "boom")}
        failed = _dispatch(target, runtime, _CACHE, phase="apply")
        _check(failed["error_kind"] == kind and "exit 7" in failed["repair"] and "'boom'" in failed["repair"], f"{step} refusal carries the exit code and stderr: {failed['repair']}")
        _check(failed["repair"].endswith("--dry-run` again.") and len(failed["repair"]) < REPAIR_LIMIT, f"and keeps its remedy inside the cap: {len(failed['repair'])}")
    runtime.claude_failures = {("plugin", "uninstall"): CommandOutcome(None, True, 1, "", "")}
    _check("timed out" in _dispatch(target, runtime, _CACHE, phase="apply")["repair"], "a timed-out CLI step says it timed out")
    runtime.claude_failures = {("plugin", "uninstall"): CommandOutcome(1, False, 1, "", "token: hunter2 " + "x" * 600)}
    secret = _dispatch(target, runtime, _CACHE, phase="apply")["repair"]
    _check("hunter2" not in secret and "exit 1" in secret and secret.endswith("--dry-run` again."), f"secret-shaped stderr never reaches the repair, and a long stderr never cuts its remedy: {secret}")
    runtime.claude_failures = {}


def _check_marketplace_refusals(root: Path) -> None:
    target, runtime = _cache_target(root)
    plugin_json = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json"
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text("{}")
    runtime.claude_visible = False
    for step, vector, kind in (("marketplace add", ("plugin", "marketplace"), "claude_marketplace_add_failed"), ("install", ("plugin", "install"), "claude_plugin_install_failed")):
        runtime.claude_failures = {vector: CommandOutcome(9, False, 1, "", "nope")}
        failed = _dispatch(target, runtime, _CACHE, phase="apply")
        _check(failed["error_kind"] == kind and "exit 9" in failed["repair"] and "'nope'" in failed["repair"], f"{step} registration refusal carries the exit code and stderr: {failed['error_kind']} {failed['repair']}")


#: stderr a real CLI could print that the Manager's validator refuses whole: a solet keg path, secret-shaped text, both, and a keg path
#: that only appears once an inner one is cut out.
_HOSTILE_STDERR = (
    "failed to read /opt/homebrew/Cellar/solet/1.2.3/libexec/hooks/hook.json",
    "token: hunter2",
    "Authorization: Bearer abc.def-123 at /usr/local/Cellar/solet/9/bin/claude",
    "/Cellar/so/Cellar/solet/let/ password=x",
    "line one\nsecret =\n  swordfish " + "y" * 300,
)


def _validated(text: str, label: str) -> None:
    """The Manager's own rule for an envelope's repair (``_result_from_fields``): ``public_string`` at 2048 characters."""
    try:
        public_string(text, "repair", maximum=2048)
    except (TypeError, ValueError) as exc:
        raise AssertionError(f"{label}: the Manager's validator refuses it ({exc}): {text}") from exc


def _check_manager_validator(root: Path) -> None:
    """iss_67d2597e (review round 2): every repair built from CLI output passes the Manager's real ``public_string``, not a copy of it."""
    for stderr in _HOSTILE_STDERR:
        refused = False
        try:
            public_string(f"refused ({stderr})", "repair", maximum=2048)
        except ValueError:
            refused = True
        _check(refused, f"control: the Manager's validator does refuse this stderr as it is: {stderr[:50]!r}")
        _validated(describe_outcome(CommandOutcome(3, False, 1, "", stderr)), f"describe_outcome {stderr[:30]!r}")
    target, runtime = _cache_target(root)
    for stderr in _HOSTILE_STDERR:
        for vector, kind in ((("plugin", "uninstall"), "claude_plugin_uninstall_failed"), (("plugin", "install"), "claude_plugin_install_failed")):
            runtime.claude_failures = {vector: CommandOutcome(5, False, 1, "", stderr)}
            failed = _dispatch(target, runtime, _CACHE, phase="apply")
            _check(failed["error_kind"] == kind, f"{kind} still refuses as itself")
            _validated(failed["repair"], f"{kind} {stderr[:30]!r}")
        runtime.claude_failures = {("plugin", "list"): CommandOutcome(5, False, 1, "", stderr)}
        listed = _dispatch(target, runtime, _CACHE)
        _check(listed["error_kind"] == "claude_plugin_list_failed", "a failed list refuses as itself")
        _validated(listed["repair"], f"claude_plugin_list_failed {stderr[:30]!r}")
    runtime.claude_failures = {}
    _check_marketplace_validator(root / "market")


def _check_marketplace_validator(root: Path) -> None:
    target, runtime = _cache_target(root)
    plugin_json = target / "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/.claude-plugin/plugin.json"
    plugin_json.parent.mkdir(parents=True, exist_ok=True)
    plugin_json.write_text("{}")
    runtime.claude_visible = False
    for stderr in _HOSTILE_STDERR:
        for vector in (("plugin", "marketplace"), ("plugin", "install")):
            runtime.claude_failures = {vector: CommandOutcome(5, False, 1, "", stderr)}
            failed = _dispatch(target, runtime, _CACHE, phase="apply")
            _validated(failed["repair"], f"registration {vector[1]} {stderr[:30]!r}")


def main() -> int:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _check_export_root(root / "export")
        _check_coding_agent_state(root / "agent")
        _check_cli_refusals(root / "cli")
        _check_marketplace_refusals(root / "marketplace")
        _check_manager_validator(root / "validator")
    print(f"existing_install_repair_text_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
