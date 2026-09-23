#!/usr/bin/env python3
"""Migrate a deployed solet's machine surface from homunculus-era names.

The P2 rename (2026-08-13) cut every live identifier over to solet forms:
the ``solet-bridge`` CLI console script, the ``SOLET_*`` environment family, the
``solet_name`` root-manifest key, launchd labels ``local.solet.<name>``, and
the ``--solet`` flag on the router/ingress/tunnel entry points. Deployed
machines carry the OLD names in persistent state that no release cutover
rewrites. This script migrates exactly that persistent surface, fail-loud:

  1. LaunchAgent plists in ~/Library/LaunchAgents whose Label / env /
     ProgramArguments carry homunculus-era names. Which plists are in remit is
     decided by OWNERSHIP — what the job executes (this checkout, ``~/.ananta``,
     or a ``macos_self_deployment_plugin`` module) — never by its label, so a
     solet-owned agent named after a vendor binary is still migrated. The
     codex-wake toolchain and every other third-party agent (Adobe, Google,
     Steam, homebrew) fail that test and are deliberately NOT touched (P3).
     Also backfills ``StandardOutPath``/``StandardErrorPath`` when a pre-June
     plist predates ``autostart_manager``'s log redirection.
  2. ``~/.zshrc`` launcher functions: ``AGENT_WAKE_CLI="homunculus"`` values.
  3. The repo checkout's editable venv: reinstall agent_messaging_plugin so
     the ``solet-bridge`` console script replaces the ``homunculus`` shim (no alias
     is kept — no-shims culture).
  4. The checkout's root_manifest.yaml ``homunculus_name:`` key (a git-tracked
     file — on the origin checkout this lands with P2 itself; on an adopter's
     seed clone the seed update delivers it, and this step is a no-op guard).
  5. ``~/.claude.json``'s ``mcpServers.*.env`` blocks: ``HOMUNCULUS_*`` keys
     renamed to ``SOLET_*``. Refuses while a Claude Code process is running
     (it holds this file in memory and rewrites it on exit, silently
     clobbering an in-session edit) — quit Claude Code first.

Loaded agents are booted out (with a bounded wait for the old label to
actually disappear from ``launchctl print`` before bootstrapping — the old
process's wind-down is otherwise a race) before their plist is rewritten and
bootstrapped back afterwards under the new label/filename. Old plist files
are removed, not kept.

Run without flags for a DRY RUN (prints the full plan, mutates nothing).
Run with ``--apply`` to execute. Idempotent: already-migrated pieces are
reported and skipped; re-running --apply against an already-migrated
surface must produce byte-identical files, never a spurious rewrite.
Any un-migratable state aborts before mutation.

Run with ``--scan-stale`` for a report-only, case-insensitive scan of the
solet-owned LaunchAgent plists, ``~/.claude/``, the project ``.claude/``, and
``CLAUDE*.md`` for surviving homunculus-era references, grouped by surface.
This mode never writes anything — see ``scan_stale`` for why some hits can't
be auto-fixed at all.

Residual guard (manual, platform must be up): after relaunch, enumerate the
plugin-config store and fail loud on any ``homunculus_name`` key — see the
seed-update runbook step this script ships with.
"""
from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from macos_self_deployment_plugin.constants import AUTOSTART_LOG_DIR_DEFAULT

REPO = Path(__file__).resolve().parent.parent.parent
LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
ZSHRC = Path.home() / ".zshrc"
CLAUDE_JSON = Path.home() / ".claude.json"
CLAUDE_DIR = Path.home() / ".claude"

# A LaunchAgent is in remit when it RUNS our code, never because of what it is
# CALLED. The prior test skipped every ``com.openai.*`` label to spare the
# codex-wake toolchain (P3, a genuinely separate deployed system). That intent
# is legitimate and is preserved below — but the test was written against the
# vendor's NAME, and ``com.openai.tunnel-client.<solet>`` is OUR supervisor,
# launched from OUR venv, named after the vendor binary it supervises. It was
# skipped, kept its pre-rebrand ``--homunculus`` flag, and the tunnel died
# silently on its next start — four days before anyone noticed, because the
# only symptom was an ABSENCE of inbound traffic.
# See workbench/2026-08-15_chatgpt_mcp_registration_report_lane_ad.md.
#
# ``_arg_renames`` would have fixed that file: it rewrites the flag regardless
# of label. The defect was never the rewrite, only the candidate selection.
SOLET_LABEL_PREFIXES = ("local.homunculus.", "local.solet.")
SOLET_OWNED_MODULE_PREFIX = "macos_self_deployment_plugin"
ANANTA_HOME = Path.home() / ".ananta"

ENV_RENAMES = re.compile(r"^HOMUNCULUS_(?=[A-Z_]+$)")
LABEL_RENAME = re.compile(r"^local\.homunculus\.")
ARG_FLAG = "--homunculus"

# Bounded wait for a booted-out label to actually clear before re-bootstrap.
# Independent of any router-side socket-reclaim wait — deliberately not
# coupled to it.
_BOOTOUT_WAIT_TIMEOUT_S = 10.0
_BOOTOUT_POLL_INTERVAL_S = 0.5

# Stores under ~/.claude/ that are historical records, never rewritten: a
# transcript, log, or per-past-session artifact. Distinct from sessions/,
# which is LIVE per-process state (see scan_stale's docstring).
HISTORICAL_STORE_GLOBS = (
    "projects/*/*.jsonl",
    "file-history/**",
    "history.jsonl",
    "jobs/**",
    "paste-cache/**",
    "backups/**",
    "**/*.pre-*",
    "shell-snapshots/**",
    "tasks/**",
    "todos/**",
    "telemetry/**",
    "debug/**",
    "session-env/**",
    "daemon.log",
)

# Live per-process state, NOT a transcript — a hit here is real and worth
# reporting, but the remedy is relaunching that session, never hand-editing
# a running process's own state file.
LIVE_PROCESS_STATE_GLOB = "sessions/**"

STALE_TOKEN = re.compile(r"homunculus", re.IGNORECASE)


@dataclass
class PlistPlan:
    old_path: Path
    new_path: Path
    old_label: str
    new_label: str
    env_renames: list[tuple[str, str]] = field(default_factory=list)
    arg_renames: list[tuple[int, str, str]] = field(default_factory=list)
    was_loaded: bool = False
    add_log_keys: bool = False
    log_path: Path | None = None


def _run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, check=check)


def _gui_domain() -> str:
    return f"gui/{os.getuid()}"


def _is_loaded(label: str) -> bool:
    probe = _run(["launchctl", "print", f"{_gui_domain()}/{label}"], check=False)
    return probe.returncode == 0


def _wait_until_unloaded(label: str) -> None:
    """Bounded poll for ``label`` to clear after ``bootout``, fail-loud on timeout.

    ``bootout`` returns before the old process has actually finished winding
    down; bootstrapping immediately races that teardown. Never an unbounded
    spin — a label still loaded after the deadline aborts rather than
    silently proceeding into the race.
    """
    deadline = time.monotonic() + _BOOTOUT_WAIT_TIMEOUT_S
    while time.monotonic() < deadline:
        if not _is_loaded(label):
            return
        time.sleep(_BOOTOUT_POLL_INTERVAL_S)
    raise SystemExit(
        f"REFUSING: {label} still loaded {_BOOTOUT_WAIT_TIMEOUT_S}s after "
        "bootout — not bootstrapping over a still-winding-down process"
    )


def _env_renames(payload: dict[str, object]) -> list[tuple[str, str]]:
    env_keys = [str(k) for k in payload.get("EnvironmentVariables", {})]  # type: ignore[union-attr]
    return [(k, ENV_RENAMES.sub("SOLET_", k)) for k in env_keys if ENV_RENAMES.match(k)]


def _argv(payload: dict[str, object]) -> list[str]:
    """A plist's ``ProgramArguments`` as strings — the single decode point."""
    return [str(a) for a in payload.get("ProgramArguments", [])]  # type: ignore[union-attr]


def _arg_renames(payload: dict[str, object]) -> list[tuple[int, str, str]]:
    return [(i, a, "--solet") for i, a in enumerate(_argv(payload)) if a == ARG_FLAG]


def _is_solet_owned(payload: dict[str, object]) -> bool:
    """Is this LaunchAgent OURS to migrate?

    Ownership is decided by what the job EXECUTES, in three ways, any one of
    which is sufficient:

    1. the label follows the solet convention (``local.homunculus.*`` /
       ``local.solet.*``) — the plists ``autostart_manager`` writes;
    2. any argv element references this checkout or ``~/.ananta`` — covers a
       job run from the checkout venv or from a released venv under
       ``~/.ananta/releases/<solet>/…``, whatever it is labelled;
    3. any argv element names a ``macos_self_deployment_plugin`` module — the
       ``-m <module>`` form used by the router, the MCP ingress, and the
       tunnel supervisor.

    Everything else — Adobe, Google, Steam, homebrew, the codex-wake
    toolchain — is out of remit and must never be touched, not even to add a
    harmless-looking key. That protection is the whole reason a filter exists
    here; this replaces WHICH test decides it, not WHETHER one does.
    """
    label = str(payload.get("Label", ""))
    if label.startswith(SOLET_LABEL_PREFIXES):
        return True
    roots = (str(REPO), str(ANANTA_HOME))
    return any(
        any(root in arg for root in roots) or arg.startswith(SOLET_OWNED_MODULE_PREFIX)
        for arg in _argv(payload)
    )


def _plan_one_plist(path: Path) -> PlistPlan | None:
    """Plan a single plist's migration; ``None`` when it needs nothing."""
    try:
        payload = plistlib.loads(path.read_bytes())
    except Exception as exc:  # noqa: BLE001 — refuse loudly below
        raise SystemExit(f"REFUSING: cannot parse {path}: {exc}") from exc
    if not _is_solet_owned(payload):
        return None
    label = str(payload.get("Label", ""))
    env_renames = _env_renames(payload)
    arg_renames = _arg_renames(payload)
    new_label = LABEL_RENAME.sub("local.solet.", label)
    # Log-key backfill stays scoped to the LABEL convention, deliberately
    # narrower than ownership: the log filename is derived from the label's
    # final segment (``local.solet.<name>`` → ``<name>_autostart.log``), which
    # is only meaningful for plists ``autostart_manager`` itself renders.
    # Deriving it from an arbitrary owned label would invent a solet name.
    is_autostart_managed = label.startswith(SOLET_LABEL_PREFIXES)
    add_log_keys = is_autostart_managed and (
        "StandardOutPath" not in payload or "StandardErrorPath" not in payload
    )
    if not (env_renames or arg_renames or new_label != label or add_log_keys):
        return None
    # Derived the same way autostart_manager._render_plist derives it — the
    # solet name is the new label's final segment (local.solet.<name>).
    solet_name = new_label.rsplit(".", 1)[-1]
    log_path = Path(AUTOSTART_LOG_DIR_DEFAULT).expanduser() / f"{solet_name}_autostart.log"
    return PlistPlan(
        old_path=path,
        new_path=path.with_name(path.name.replace("local.homunculus.", "local.solet.")),
        old_label=label,
        new_label=new_label,
        env_renames=env_renames,
        arg_renames=arg_renames,
        was_loaded=_is_loaded(label),
        add_log_keys=add_log_keys,
        log_path=log_path if add_log_keys else None,
    )


def plan_plists() -> list[PlistPlan]:
    """Plan every solet-owned LaunchAgent. Ownership is tested per-payload in
    ``_plan_one_plist`` — never by filename, which is what let a vendor-named
    plist of ours escape the P2 rename."""
    return [
        plan
        for plan in map(_plan_one_plist, sorted(LAUNCH_AGENTS.glob("*.plist")))
        if plan is not None
    ]


def apply_plist(plan: PlistPlan) -> None:
    payload = plistlib.loads(plan.old_path.read_bytes())
    payload["Label"] = plan.new_label
    env = payload.get("EnvironmentVariables")
    if env:
        for old_key, new_key in plan.env_renames:
            env[new_key] = env.pop(old_key)
    args = payload.get("ProgramArguments")
    if args:
        for idx, _, new in plan.arg_renames:
            args[idx] = new
    if plan.add_log_keys and plan.log_path is not None:
        plan.log_path.parent.mkdir(parents=True, exist_ok=True)
        payload.setdefault("StandardOutPath", str(plan.log_path))
        payload.setdefault("StandardErrorPath", str(plan.log_path))
    if plan.was_loaded:
        _run(["launchctl", "bootout", f"{_gui_domain()}/{plan.old_label}"], check=False)
        _wait_until_unloaded(plan.old_label)
    with plan.new_path.open("wb") as f:
        plistlib.dump(payload, f)
    if plan.new_path != plan.old_path:
        plan.old_path.unlink()
    if plan.was_loaded:
        _run(["launchctl", "bootstrap", _gui_domain(), str(plan.new_path)])


def plan_zshrc() -> int:
    if not ZSHRC.exists():
        return 0
    return ZSHRC.read_text(encoding="utf-8").count('AGENT_WAKE_CLI="homunculus"')


def apply_zshrc() -> None:
    text = ZSHRC.read_text(encoding="utf-8")
    ZSHRC.write_text(
        text.replace('AGENT_WAKE_CLI="homunculus"', 'AGENT_WAKE_CLI="solet-bridge"'),
        encoding="utf-8",
    )


def plan_manifest() -> bool:
    manifest = REPO / "root_manifest.yaml"
    return bool(re.search(r"^homunculus_name:", manifest.read_text(encoding="utf-8"), re.M))


def apply_manifest() -> None:
    manifest = REPO / "root_manifest.yaml"
    text = manifest.read_text(encoding="utf-8")
    manifest.write_text(re.sub(r"^homunculus_name:", "solet_name:", text, count=1, flags=re.M),
                        encoding="utf-8")


def shim_state() -> tuple[bool, bool]:
    bin_dir = REPO / ".venv" / "bin"
    return (bin_dir / "homunculus").exists(), (bin_dir / "solet-bridge").exists()


def apply_reinstall() -> None:
    pip = REPO / ".venv" / "bin" / "pip"
    _run([str(pip), "install", "-e", str(REPO / "plugins" / "agent_messaging_plugin")])
    old_shim, new_shim = shim_state()
    if not new_shim:
        raise SystemExit("REFUSING to finish: reinstall did not create .venv/bin/solet-bridge")
    if old_shim:
        (REPO / ".venv" / "bin" / "homunculus").unlink()
        print("  removed lingering .venv/bin/homunculus shim (no-shims)")


def _claude_code_running() -> bool | None:
    """``True``/``False`` when determinable; ``None`` when ambiguous.

    Detect by process, never by lockfile — Claude Code holds ``~/.claude.json``
    in memory and rewrites it on exit, so a lockfile can't see an in-session
    edit coming. ``pgrep -x`` matches the exact ``comm`` the CLI actually runs
    under (confirmed empirically: ``claude``, distinct from the unrelated
    Claude.app Electron process, which never shows this exact name). Any
    outcome other than a clean match/no-match — a missing ``pgrep``, an
    unexpected exit code — is ambiguous and must refuse rather than pass: a
    false negative here silently clobbers the operator's config.
    """
    try:
        probe = _run(["pgrep", "-x", "claude"], check=False)
    except FileNotFoundError:
        return None
    if probe.returncode == 0:
        return True
    if probe.returncode == 1:
        return False
    return None


def plan_claude_json() -> dict[str, list[tuple[str, str]]]:
    if not CLAUDE_JSON.exists():
        return {}
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    servers = data.get("mcpServers", {})
    plan: dict[str, list[tuple[str, str]]] = {}
    for name, cfg in servers.items():
        env = cfg.get("env", {}) if isinstance(cfg, dict) else {}
        renames = [(k, ENV_RENAMES.sub("SOLET_", k)) for k in env if ENV_RENAMES.match(k)]
        if renames:
            plan[name] = renames
    return plan


def apply_claude_json(plan: dict[str, list[tuple[str, str]]]) -> None:
    running = _claude_code_running()
    if running is not False:
        why = (
            "a Claude Code process is running"
            if running else "process-running status could not be determined"
        )
        raise SystemExit(
            f"REFUSING to write {CLAUDE_JSON}: {why}. Quit Claude Code first, "
            "then re-run with --apply — it holds this file in memory and "
            "rewrites it on exit, silently clobbering an in-session edit."
        )
    data = json.loads(CLAUDE_JSON.read_text(encoding="utf-8"))
    servers = data["mcpServers"]
    for name, renames in plan.items():
        env = servers[name]["env"]
        for old_key, new_key in renames:
            env[new_key] = env.pop(old_key)
    CLAUDE_JSON.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")


@dataclass
class StaleHit:
    path: Path
    count: int
    category: str  # "historical" | "live_process_state" | "fixable"


def _relative_to_home(path: Path) -> Path | None:
    try:
        return path.relative_to(CLAUDE_DIR)
    except ValueError:
        return None


def _classify_claude_dir_path(rel: Path) -> str:
    if any(rel.full_match(pattern) for pattern in HISTORICAL_STORE_GLOBS):
        return "historical"
    if rel.full_match(LIVE_PROCESS_STATE_GLOB):
        return "live_process_state"
    return "fixable"


def _count_stale_token(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return 0
    return len(STALE_TOKEN.findall(text))


def _scan_tree(root: Path, *, classify: bool) -> list[StaleHit]:
    if not root.exists():
        return []
    hits: list[StaleHit] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        count = _count_stale_token(path)
        if not count:
            continue
        if classify:
            rel = _relative_to_home(path)
            category = _classify_claude_dir_path(rel) if rel is not None else "fixable"
        else:
            category = "fixable"
        hits.append(StaleHit(path=path, count=count, category=category))
    return hits


def _scan_launch_agents() -> list[StaleHit]:
    """Report stale tokens in the LaunchAgent plists this script MAY rewrite.

    Report mode previously covered ``~/.claude``, the project ``.claude/`` and
    ``CLAUDE*.md`` — but NOT ``~/Library/LaunchAgents``, the one surface apply
    mode actually edits. A report that cannot see where the writer writes is
    worse than no report: it is actively reassuring. An adopter ran the
    migration, read a clean report, and their tunnel had already stopped
    working (2026-08-15).

    Scoped to solet-owned plists by the same ``_is_solet_owned`` test the
    planner uses, so report and apply cover exactly the same set. A stale
    token in a third-party agent is not ours to report as fixable, because it
    is not ours to fix.
    """
    if not LAUNCH_AGENTS.exists():
        return []
    hits: list[StaleHit] = []
    for path in sorted(LAUNCH_AGENTS.glob("*.plist")):
        try:
            payload = plistlib.loads(path.read_bytes())
        except Exception:  # noqa: BLE001 — a report never aborts on one bad file
            continue
        if not _is_solet_owned(payload):
            continue
        if count := _count_stale_token(path):
            hits.append(StaleHit(path=path, count=count, category="fixable"))
    return hits


def scan_stale() -> list[StaleHit]:
    """Report-only, case-insensitive scan for surviving ``homunculus`` references.

    Three categories, because the remedy differs by category — this never
    auto-rewrites anything, in any category:

    - ``historical``: a transcript, log, or other append-only record
      (``HISTORICAL_STORE_GLOBS``). Rewriting one falsifies the record —
      leave it alone, always.
    - ``live_process_state``: a running session's own state
      (``~/.claude/sessions/<pid>.json``). Not a transcript, so the
      historical rationale doesn't apply — but it is fixed by relaunching
      that session, never by hand-editing a live process's state file.
    - ``fixable``: everything else — a plain edit is safe, EXCEPT a memory
      fact (any ``.md`` file under a ``memory/`` directory), where the
      filename, its frontmatter ``name:`` slug, and every ``[[link]]``
      pointing at it must move together; a blind substitution breaks that
      triple and leaves dangling links.
    """
    hits = _scan_launch_agents()
    hits += _scan_tree(CLAUDE_DIR, classify=True)
    hits += _scan_tree(REPO / ".claude", classify=False)
    hits += [
        StaleHit(path=p, count=n, category="fixable")
        for p in sorted(REPO.glob("CLAUDE*.md"))
        if (n := _count_stale_token(p))
    ]
    return hits


def print_stale_report(hits: list[StaleHit]) -> None:
    print(f"== migrate_to_solet [--scan-stale] == {len(hits)} file(s) with hits")
    by_category: dict[str, list[StaleHit]] = {"historical": [], "live_process_state": [], "fixable": []}
    for hit in hits:
        by_category[hit.category].append(hit)
    labels = {
        "fixable": "FIXABLE — safe to edit directly (memory facts: move filename + "
                   "frontmatter name: + every [[link]] together, never a blind substitution)",
        "live_process_state": "LIVE PROCESS STATE — fix by relaunching that session, "
                               "never by hand-editing the file",
        "historical": "HISTORICAL — leave alone, never rewrite (falsifies the record)",
    }
    for category in ("fixable", "live_process_state", "historical"):
        group = by_category[category]
        print(f"\n{labels[category]} ({len(group)}):")
        for hit in group:
            print(f"  {hit.path} ({hit.count} hit(s))")


def _print_plist_plan(plans: list[PlistPlan]) -> None:
    print(f"LaunchAgents to migrate: {len(plans)}")
    for p in plans:
        loaded = "loaded" if p.was_loaded else "not loaded"
        print(f"  {p.old_path.name} → {p.new_path.name} [{loaded}]")
        print(f"    label {p.old_label} → {p.new_label}")
        for old_key, new_key in p.env_renames:
            print(f"    env {old_key} → {new_key}")
        for idx, old, new in p.arg_renames:
            print(f"    arg[{idx}] {old} → {new}")
        if p.add_log_keys:
            print(f"    add StandardOutPath/StandardErrorPath → {p.log_path}")


def _claude_json_detection_status(running: bool | None) -> str:
    if running is True:
        return "Claude Code IS running — apply will refuse; quit it first"
    if running is False:
        return "no Claude Code process detected — apply will proceed"
    return "Claude Code process status UNDETERMINED — apply will refuse (fail-safe)"


def _print_claude_json_plan(plan: dict[str, list[tuple[str, str]]], running: bool | None) -> int:
    count = sum(len(v) for v in plan.values())
    print(f"~/.claude.json mcpServers env keys to rename: {count}")
    for server_name, renames in plan.items():
        for old_key, new_key in renames:
            print(f"  mcpServers.{server_name}.env {old_key} → {new_key}")
    if count:
        print(f"  Claude Code process detection: {_claude_json_detection_status(running)}")
    return count


def _apply_all(
    plans: list[PlistPlan],
    zsh_hits: int,
    manifest_old: bool,
    claude_json_plan: dict[str, list[tuple[str, str]]],
) -> None:
    for p in plans:
        apply_plist(p)
        print(f"migrated {p.new_path.name}")
    if zsh_hits:
        apply_zshrc()
        print("migrated ~/.zshrc launcher functions")
    if manifest_old:
        apply_manifest()
        print("migrated root_manifest.yaml key")
    if claude_json_plan:
        apply_claude_json(claude_json_plan)
        print("migrated ~/.claude.json mcpServers env keys")
    apply_reinstall()
    print("reinstalled agent_messaging_plugin; `solet-bridge` CLI active")
    print("NEXT (manual, from the runbook): refresh installed coordination-hooks "
          "plugin caches, wait for the platform log to go stable, then run the "
          "config-store residual check (fail loud on any homunculus_name key).")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="execute (default: dry run)")
    parser.add_argument(
        "--scan-stale", action="store_true",
        help="report-only scan for surviving homunculus-era references; mutates nothing",
    )
    args = parser.parse_args()

    if args.scan_stale:
        print_stale_report(scan_stale())
        return 0

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"== migrate_to_solet [{mode}] ==")

    plans = plan_plists()
    zsh_hits = plan_zshrc()
    manifest_old = plan_manifest()
    old_shim, new_shim = shim_state()
    claude_json_plan = plan_claude_json()
    claude_json_running = _claude_code_running()

    _print_plist_plan(plans)
    print(f"~/.zshrc AGENT_WAKE_CLI values to flip: {zsh_hits}")
    print(f"root_manifest.yaml old key present: {manifest_old}")
    print(f"venv shims: homunculus={old_shim} solet-bridge={new_shim}")
    _print_claude_json_plan(claude_json_plan, claude_json_running)

    if not args.apply:
        print("dry run complete — re-run with --apply to execute")
        return 0

    _apply_all(plans, zsh_hits, manifest_old, claude_json_plan)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
