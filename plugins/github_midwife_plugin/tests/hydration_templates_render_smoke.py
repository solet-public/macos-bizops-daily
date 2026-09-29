"""Hydration-templates render smoke.

Nothing renders `hydration_templates/*.template` in code — the runbook is
explicit that hydration is driving-agent-performed literal string
substitution, not genesis code. That means a bad template (a token that
doesn't survive substitution, a shell file that's syntactically broken
once rendered, a JSON file that isn't valid JSON once rendered) has no
other gate catching it before an agent renders it live into someone's
`~/.zshrc` or `.claude/settings.json`. This smoke simulates the
documented render contract (literal `{{TOKEN}}` replacement, per
`TEMPLATE_VARS.md`) against every template file and checks the result:

1. No `{{...}}`-shaped token survives substitution (an undocumented or
   misspelled token would otherwise render literally into the user's
   file).
2. Every `.zsh`-shaped template (and every `client/bin/*` script
   template) parses as valid zsh once rendered (`zsh -n`).
3. `claude_settings.json.template` is valid JSON once rendered, and each
   rendered command hook parses as shell.
4. `TEMPLATE_VARS.md`'s file-map table lists every `*.template` file on
   disk, and lists no file that isn't on disk (catches map drift).
5. The directory stays FLAT (the KB-ingestion exclude pattern relies on
   this, per `TEMPLATE_VARS.md`'s own note).
6. The generated CLAUDE.md and prompt hook lead with the no-MCP local
   `<name>` command, and never reintroduce the old "skip Step Zero until
   MCP is registered" guidance.
7. `TEMPLATE_VARS.md`'s refresh-class table has one row per template with a
   class from the closed vocabulary, and its `managed` rows are exactly the
   templates `existing_install_flow.json` declares as managed artifacts.

Run directly: ``.venv/bin/python3 plugins/github_midwife_plugin/tests/hydration_templates_render_smoke.py``.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "knowledge_base" / "hydration_templates"
_TEMPLATE_VARS = _TEMPLATES_DIR / "TEMPLATE_VARS.md"

_RENDER_TOKENS = {
    "{{SOLET_NAME}}": "iris",
    "{{CLONE_DIR}}": "/Users/example/Workspace/iris",
    "{{CLONE_DIR_ZSH}}": "'/Users/example/Workspace/iris'",
    "{{HYDRATION_DATE}}": "2026-07-13",
    "{{BACKUP_PATH}}": "/Users/example/.zshrc.pre-iris-hydration-20260713",
    # DERIVED from {{SOLET_NAME}} by the documented `_` -> `-` transform, not a
    # free-standing value: "iris" has no underscore, so it renders through unchanged.
    # Deliberately NOT a literal unrelated to the name above — a fixture that let the
    # two drift would stop modelling the derivation the ruling requires.
    "{{MARKETPLACE_NAME}}": "iris",
    # The ARMED render. The solo render is the same launcher with the whole
    # GIT_CONTROLLER_NAME line deleted (absence is the exemption), which is a
    # hydration edit rather than a token value, so it is checked separately
    # below rather than modelled as a second fixture value here.
    "{{GIT_CONTROLLER_NAME}}": "Git-Controller",
    # Existing-install managed-artifact templates (design section 6.1).
    "{{SHELL_FILE_ZSH}}": "'/Users/example/Workspace/iris/client/iris.zsh'",
    "{{RUNTIME_DIR}}": "/Users/example/.ananta/runtime/iris",
    "{{PATH_ENV}}": "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
}

_ZSH_SHAPED = {
    "zshrc.template",
    "zshrc_block.template",
    "solet.zsh.template",
    "claude_launcher.template",
    "codex_launcher.template",
    "launch.template",
    "fleet_functions.zsh.template",
}

_TOKEN_RE = re.compile(r"\{\{[A-Z_]+\}\}")

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str) -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _render(content: str) -> str:
    for token, value in _RENDER_TOKENS.items():
        content = content.replace(token, value)
    return content


def _check_directory_is_flat() -> None:
    entries = list(_TEMPLATES_DIR.iterdir())
    subdirs = [e for e in entries if e.is_dir()]
    _check("directory stays flat (no subdirectories)", not subdirs, f"found subdirectories: {subdirs}")


def _check_file_map_matches_disk() -> None:
    on_disk = {p.name for p in _TEMPLATES_DIR.glob("*.template")}
    documented = set(re.findall(r"`([a-zA-Z0-9_.]+\.template)`", _TEMPLATE_VARS.read_text(encoding="utf-8")))
    _check(
        "every on-disk template is documented in TEMPLATE_VARS.md's file map",
        on_disk <= documented,
        f"undocumented templates: {on_disk - documented}",
    )
    _check(
        "TEMPLATE_VARS.md documents no template absent from disk",
        documented <= on_disk,
        f"documented-but-missing templates: {documented - on_disk}",
    )


_REFRESH_CLASSES = frozenset({"managed", "refresh-safe-manual", "operator-input-manual", "user-owned"})
_REFRESH_ROW = re.compile(r"^\| `([a-zA-Z0-9_.]+\.template)` \| `([a-z-]+)` \|", re.MULTILINE)
_BUNDLE = _TEMPLATES_DIR.parent / "existing_install_flow.json"


def _check_refresh_classes_match_disk_and_bundle() -> None:
    """Every template has exactly one refresh class, and `managed` is exactly what the shipped bundle declares."""
    text = _TEMPLATE_VARS.read_text(encoding="utf-8")
    section = text.split("## Refresh class on update", 1)[1].split("\n## ", 1)[0].split("This directory stays", 1)[0]
    rows = _REFRESH_ROW.findall(section)
    classes = dict(rows)
    on_disk = {p.name for p in _TEMPLATES_DIR.glob("*.template")}
    _check("refresh-class table has one row per template", len(rows) == len(classes) and set(classes) == on_disk, f"rows={sorted(classes)} missing={sorted(on_disk - set(classes))} extra={sorted(set(classes) - on_disk)}")
    _check("every refresh class is in the closed vocabulary", set(classes.values()) <= _REFRESH_CLASSES, f"unknown classes: {sorted(set(classes.values()) - _REFRESH_CLASSES)}")
    declared = {Path(row["template_ref"]).name for row in json.loads(_BUNDLE.read_text(encoding="utf-8"))["managed_artifacts"]}
    managed = {name for name, refresh_class in classes.items() if refresh_class == "managed"}
    _check("the `managed` rows are exactly the templates existing_install_flow.json declares", bool(declared) and managed == declared, f"managed-only={sorted(managed - declared)} declared-only={sorted(declared - managed)}")
    _check("the fleet file is `managed` only through the clone-exclude block that makes its destination ignored", classes["fleet_functions.zsh.template"] == "managed" and classes["clone_exclude_block.template"] == "managed", f"fleet class: {classes['fleet_functions.zsh.template']}, exclude class: {classes['clone_exclude_block.template']}")


def _check_no_surviving_tokens(name: str, rendered: str) -> None:
    leftover = _TOKEN_RE.findall(rendered)
    _check(f"{name}: no undocumented {{...}} token survives render", not leftover, f"leftover tokens: {leftover}")


def _check_zsh_syntax(name: str, rendered: str) -> None:
    result = subprocess.run(
        ["zsh", "-n", "/dev/stdin"], input=rendered, capture_output=True, text=True, timeout=10,
    )
    _check(f"{name}: rendered content is valid zsh (zsh -n)", result.returncode == 0, result.stderr.strip())


def _check_json_syntax(name: str, rendered: str) -> None:
    try:
        json.loads(rendered)
        ok, detail = True, ""
    except json.JSONDecodeError as exc:
        ok, detail = False, str(exc)
    _check(f"{name}: rendered content is valid JSON", ok, detail)


def _iter_entry_commands(
    event_name: str, entry_index: int, entry: object,
) -> list[tuple[str, str]]:
    entry_hooks = entry.get("hooks") if isinstance(entry, dict) else None
    if not isinstance(entry_hooks, list):
        return []
    commands: list[tuple[str, str]] = []
    for hook_index, hook in enumerate(entry_hooks):
        if (
            isinstance(hook, dict)
            and hook.get("type") == "command"
            and isinstance(hook.get("command"), str)
        ):
            commands.append(
                (f"{event_name}[{entry_index}].hooks[{hook_index}]", hook["command"]),
            )
    return commands


def _iter_claude_hook_commands(config: object) -> list[tuple[str, str]]:
    if not isinstance(config, dict):
        return []
    hooks = config.get("hooks")
    if not isinstance(hooks, dict):
        return []
    commands: list[tuple[str, str]] = []
    for event_name, event_entries in hooks.items():
        if not isinstance(event_entries, list):
            continue
        for entry_index, entry in enumerate(event_entries):
            commands.extend(_iter_entry_commands(event_name, entry_index, entry))
    return commands


def _check_claude_hook_commands_parse(name: str, rendered: str) -> None:
    config = json.loads(rendered)
    for label, command in _iter_claude_hook_commands(config):
        result = subprocess.run(
            ["zsh", "-n", "/dev/stdin"], input=command, capture_output=True, text=True, timeout=10,
        )
        _check(
            f"{name}: rendered hook command parses as zsh ({label})",
            result.returncode == 0,
            result.stderr.strip(),
        )


def _check_all_templates() -> None:
    for path in sorted(_TEMPLATES_DIR.glob("*.template")):
        rendered = _render(path.read_text(encoding="utf-8"))
        _check_no_surviving_tokens(path.name, rendered)
        if path.name in _ZSH_SHAPED:
            _check_zsh_syntax(path.name, rendered)
        if path.name in {
            "claude_settings.json.template",
            "marketplace_json.template",
            "codex_marketplace_json.template",
        }:
            _check_json_syntax(path.name, rendered)
        if path.name == "claude_settings.json.template":
            _check_claude_hook_commands_parse(path.name, rendered)


def _check_no_mcp_first_generated_guidance() -> None:
    _check_generated_agent_instruction_files()
    _check_generated_hook_surfaces()


def _check_generated_agent_instruction_files() -> None:
    rendered = {
        name: _render((_TEMPLATES_DIR / f"{name}.template").read_text(encoding="utf-8"))
        for name in ("AGENTS.md", "CLAUDE.md")
    }
    shared_snippets = (
        "iris call service_interface::knowledge_service::search",
        '"top_k": 8',
        "iris call service_interface::session_ledger_service::search_event_content",
        '"limit": 8',
        "<!-- BEGIN SOLET HYDRATION -->",
        "<!-- END SOLET HYDRATION -->",
        "MCP is optional and exists only for the operator who explicitly asks for it",
        "the default.",
        "There is no offline query script shipped in",
        "Before editing code, changing config, or explaining a subsystem",
        "search iris's knowledge base",
        "router bridge blue-green local deployment architecture",
    )
    stale_markers = (
        "Pre-boot exception",
        "until iris's MCP bridge is registered",
        "Once the bridge is up",
        "skip everything in this section",
        "plugins/default_knowledge_plugin/tools/query_knowledge_base.py",
        "Codex solet wake runbook",
    )
    for name, content in rendered.items():
        for snippet in shared_snippets:
            _check(
                f"{name}: shared bootstrap contains {snippet!r}",
                snippet in content,
                "paired instruction templates drifted",
            )
        for marker in stale_markers:
            _check(
                f"{name}: stale generated-guidance marker absent: {marker!r}",
                marker not in content,
                f"found {marker!r}",
            )

    _check(
        "AGENTS.md: runner-specific audience is generic",
        "AGENTS.md-convention\nsession" in rendered["AGENTS.md"],
        "must not hardcode one AGENTS.md-reading runner",
    )
    _check(
        "CLAUDE.md: runner-specific audience is Claude Code",
        "Claude Code session" in rendered["CLAUDE.md"],
        "missing Claude Code audience",
    )
    for snippet in (
        "client/bin/codex-iris",
        "plugin::agent_messaging_plugin::peer_inbox",
        '"agent_session_id":"' + "'\"$AGENT_SESSION_ID\"'" + '",',
        "next_after_created_at",
        "next_role_cursor",
        "role_section_status",
    ):
        _check(
            f"AGENTS.md: stock-Codex receive contract contains {snippet!r}",
            snippet in rendered["AGENTS.md"],
            "missing generated stock-Codex launcher or durable-inbox guidance",
        )


def _check_generated_hook_surfaces() -> None:
    settings = _render(
        (_TEMPLATES_DIR / "claude_settings.json.template").read_text(encoding="utf-8"),
    )
    _check_settings_hook_content(settings)
    _check_rename_and_fleet_templates()
    _check_settings_hook_kb_guidance(settings)
    _check_marketplace_matches_settings(settings)
    _check_git_controller_export()
    _check_solet_name_export()
    _check_coordination_receipt_export()


_SETTINGS_HOOK_CHECKS: tuple[tuple[str, tuple[str, ...], tuple[str, ...], str], ...] = (
    (
        "settings: registers the marketplace with the measured directory-source shape",
        (
            '"extraKnownMarketplaces"',
            '"source": "directory"',
            '"path": "/Users/example/Workspace/iris"',
        ),
        (),
        "user-scope settings must register the seed's marketplace as a local "
        "directory source (measured shape, WS-5a probe finding 1)",
    ),
    (
        "settings: enables the plugin under the DERIVED marketplace name",
        ('"coordination-hooks@iris": true',),
        (),
        "enabledPlugins key must be coordination-hooks@<derived marketplace name>; "
        "a key pinned to any other literal is wrong in every tree but its own",
    ),
    (
        "settings: ships NO inline hooks — the plugin owns all three",
        (),
        (
            "SOLET_STEP_ZERO_HOOK",
            "SOLET_ROLE_RECLAIM_HOOK",
            "SOLET_WAKE_HOOK",
            '"hooks"',
        ),
        "the three inline hooks moved into the coordination-hooks plugin; leaving "
        "them here double-fires every reminder and races two wakers on one spool lock",
    ),
)


def _missing_and_forbidden(
    text: str, required: tuple[str, ...], forbidden: tuple[str, ...],
) -> tuple[list[str], list[str]]:
    missing = [snippet for snippet in required if snippet not in text]
    present = [snippet for snippet in forbidden if snippet in text]
    return missing, present


# The three inline hooks moved into the plugin (WS-5b-core §2.3), so the old
# "ALL THREE settings hooks guard on the label" assertion has no template rows left
# to check. The INVARIANT it encoded is unchanged and still load-bearing at user
# scope — an unlabeled `claude` session anywhere on the machine gets zero output —
# so it is RE-ASSERTED against the artifact that now owns it, rather than deleted
# along with the rows. Deleting it would have retired a live safety property because
# its subject moved.
#
# Deliberately not "every hook": EVERY registered entrypoint BUT ONE is label-gated.
# The exception, git_controller_gate.py, is label-INDEPENDENT by design — it arms on
# GIT_CONTROLLER_NAME's presence and evaluates wherever armed, so zero-output for
# unlabeled sessions holds for it via its arming boundary, not via a label guard.
# Asserting "all of them" would be false; asserting a COUNT would be stale by the
# next landing — measured 2026-08-01 the roster was 6 registrations of 5 distinct
# entrypoints, and the "five of six" that a sibling lane's measurement produced that
# same morning was already wrong by afternoon, because operator_guidance.js was
# removed underneath it. State the RULE and name the exception; let the count move.
# DERIVED from the shipped hooks.json, never pinned. A hardcoded roster went stale
# inside one build of this very change (operator_guidance.js was removed under it by
# a sibling lane's landing), which is the whole argument: the roster is a moving
# target, so the test reads it from the artifact instead of restating it.
# Only the EXCEPTION is named by hand, because it is a documented design decision
# rather than an observation — and naming it is what stops "five" from silently
# becoming "four label-gated plus one nobody checked".
#
# check_messages_reminder.py and wake_waiter.py were ALREADY armed on
# AGENT_SESSION_ID, never AGENT_SESSION_LABEL, in their pre-2026-08-08 JS form
# too -- this check only ever passed for them by accident, matching an
# unrelated historical comment ("re-keyed from AGENT_SESSION_LABEL on
# 2026-08-01") rather than real arming logic. The coordination-hooks
# Python promotion (2026-08-08) surfaced this: the new files' docstrings
# don't happen to repeat that same incidental phrase, so the vacuous match
# stopped occurring and this check went red for the first time -- correctly,
# since it was never actually verifying these two hooks' real contract.
#
# heartbeat_report_alive.py (R4 vendoring, 2026-08-10) is armed on
# AGENT_INSTANCE_ID, and its source never references AGENT_SESSION_LABEL at
# all -- a clean fit for this bucket, same shape as the three above.
# rotation_due_watch.py is NOT added here despite ALSO arming on
# AGENT_INSTANCE_ID: its source legitimately contains the string
# "AGENT_SESSION_LABEL" (it reads the label to enrich its notification TEXT,
# never to arm), so it happens to satisfy the opposite branch's plain
# substring check today -- correctly, by coincidence of what that branch
# actually tests (string presence), not because it is truly label-gated.
# Disclosed here so a future reader does not "fix" it into this set and
# break that branch's real, if narrower, guarantee.
#
# capture.py and session_context.py (R4 vendoring Package B, 2026-08-10)
# arm on neither AGENT_SESSION_LABEL nor any other env var at all -- their
# precondition is a filesystem-presence check (this agent's own memory
# directory existing), and neither file's source references
# AGENT_SESSION_LABEL. drain.py/hydrate_render.py/index_render.py/sync.py
# never appear in this smoke's roster at all (it derives the roster from
# hooks.json's actual registrations, and those four are deliberately never
# wired there -- see manifest_consistency_smoke.py's
# AGENT_INVOKED_CLI_UTILITIES design note), so they need no entry here.
#
# rotation_due_notice.py (L4b, 2026-08-17) is the same filesystem-presence
# shape as capture.py/session_context.py, one step further out: its
# precondition is neither an env var nor a directory but the PRESENCE OF A
# MARKER FILE for this session generation, and its source references
# AGENT_SESSION_LABEL nowhere. It cannot be label-gated without inventing a
# dependency it does not have -- the sibling that DOES read the label
# (rotation_due_watch.py) is the one that composes the notification text; this
# one only carries that already-written text to the session. Belongs here for
# the reason the bucket exists (it no-ops cleanly in an unlabeled user-scope
# install), not as an exemption from the opposite branch.
#
# capture_session_mapping.py and headless_tool_allowlist_gate.py (0.8.0,
# seed feedback #40/§51.1, 2026-08-24 -- newly REGISTERED in hooks.json this
# release, previously never in this smoke's derived roster at all) are the
# same env-presence shape as heartbeat_report_alive.py, on their OWN
# spawn-only env vars rather than AGENT_INSTANCE_ID alone:
# capture_session_mapping.py no-ops unless BOTH
# ANANTA_SESSION_MAPPING_SPOOL_DIR and AGENT_INSTANCE_ID are set;
# headless_tool_allowlist_gate.py no-ops (allows) unless
# FLEET_HEADLESS_TOOL_ALLOWLIST is set. Neither references AGENT_SESSION_LABEL
# in its source. An ordinary unlabeled user-scope session carries none of
# those three spawn-only env vars either, so both no-op cleanly there --
# label-independent by a different, but equally real, arming boundary.
_LABEL_INDEPENDENT_HOOKS = frozenset(
    {
        "git_controller_gate.py", "check_messages_reminder.py", "wake_waiter.py",
        "heartbeat_report_alive.py", "capture.py", "session_context.py",
        "rotation_due_notice.py", "capture_session_mapping.py",
        "headless_tool_allowlist_gate.py",
    },
)
_PLUGIN_HOOKS_DIR = (
    _TEMPLATES_DIR.parents[1] / "claude_plugin" / "coordination-hooks" / "hooks"
)


def _shipped_hook_entrypoints() -> set[str]:
    """Every distinct script the shipped hooks.json actually registers."""
    manifest = json.loads(
        (_PLUGIN_HOOKS_DIR / "hooks.json").read_text(encoding="utf-8"),
    )
    return {
        Path(hook["args"][0]).name
        for groups in manifest["hooks"].values()
        for group in groups
        for hook in group["hooks"]
        if hook.get("args")
    }


def _check_settings_label_guards(settings: str) -> None:
    _check(
        "settings: no inline hook survives to guard (all three moved to the plugin)",
        "AGENT_SESSION_LABEL" not in settings,
        "the settings template must ship no hooks at all after WS-5b-core",
    )
    entrypoints = _shipped_hook_entrypoints()
    # A roster that silently emptied would make every per-hook check below vacuous,
    # so the population is asserted non-empty before anything is concluded from it.
    _check(
        "plugin: hooks.json registers at least one entrypoint to check",
        bool(entrypoints),
        "an empty roster would make the label-guard checks pass by not running",
    )
    for name in sorted(entrypoints):
        path = _PLUGIN_HOOKS_DIR / name
        body = path.read_text(encoding="utf-8") if path.is_file() else ""
        if name in _LABEL_INDEPENDENT_HOOKS:
            _check(
                f"plugin hook {name}: label-INDEPENDENT, armed by env-presence",
                path.is_file() and "AGENT_SESSION_LABEL" not in body,
                "this entrypoint is documented as NOT reading the label — if it gains "
                "a label guard, the arming boundary changed and TEMPLATE_VARS must too",
            )
            continue
        _check(
            f"plugin hook {name}: no-ops unless AGENT_SESSION_LABEL is set",
            path.is_file() and "AGENT_SESSION_LABEL" in body,
            "user-scope install requires this hook to no-op cleanly in unlabeled "
            "sessions; a registered-but-missing file fails here rather than passing "
            "vacuously",
        )


def _check_settings_hook_content(settings: str) -> None:
    _check_settings_label_guards(settings)
    for label, required, forbidden, detail in _SETTINGS_HOOK_CHECKS:
        missing, present = _missing_and_forbidden(settings, required, forbidden)
        _check(
            label,
            not missing and not present,
            f"{detail} (missing snippets: {missing}; forbidden snippets present: {present})",
        )


def _check_rename_and_fleet_templates() -> None:
    rename_skill = _render(
        (_TEMPLATES_DIR / "rename_skill_SKILL.md.template").read_text(encoding="utf-8"),
    )
    _check(
        "rename skill: watch is the declared default, transports never silently cross",
        "iris watch --role" in rename_skill
        and "declared, never probed, never silently crossed" in rename_skill
        and "FLEET_TRANSPORT:-watch" in rename_skill
        and "suggest MCP to make renaming work" in rename_skill
        and "peer_holds_role" in rename_skill,
        "rename skill must lead with the declared-transport contract "
        "(watch default, fail-loud, no silent transport crossing)",
    )
    fleet = _render(
        (_TEMPLATES_DIR / "fleet_functions.zsh.template").read_text(encoding="utf-8"),
    )
    _check(
        "fleet launcher: MCP development channel is opt-in, default off",
        "iris_FLEET_MCP_CHANNELS" in fleet
        and "exec claude \\\n      --dangerously-load-development-channels" not in fleet,
        "fleet template must not hardcode the MCP development-channel flag",
    )
    _check(
        "fleet launcher: exports the declared transport with the watch default",
        'FLEET_TRANSPORT="${iris_FLEET_TRANSPORT:-watch}"' in fleet,
        "fleet launcher must export FLEET_TRANSPORT from the per-name "
        "operator knob, defaulting to watch",
    )
    _check_fleet_tmux_hosting(fleet)


def _check_fleet_tmux_hosting(fleet: str) -> None:
    """Fleet sessions must be created in a named tmux session, never a bare tab.

    Headless sessions are unsupported: the solet drives and wakes a session
    through its tmux pane. The shipped template once carried no tmux at all, so
    a user's fleet launcher started sessions the solet could not reach.
    """
    for snippet in (
        "_tmux_host_for_iris()",
        'new-session -d -s "$host"',
        'has-session -t "=$host"',
        "attach-session -t",
        "switch-client -t",
        "zsh -ic ${(q)inner_cmd}",
        "_claude_for_iris ${(q)role} ${(q)model} ${(q)effort}",
        "exec claude",
    ):
        _check(
            f"fleet launcher: tmux host path contains {snippet!r}",
            snippet in fleet,
            "fleet template must host each role in a named tmux session with "
            "quoted re-entry and an idempotent attach",
        )
    _check(
        "fleet launcher: no operator home path baked into the template",
        "/Users/" not in fleet and "/opt/homebrew/bin/tmux" not in fleet,
        "fleet template must stay generic and find tmux on PATH",
    )
    with tempfile.TemporaryDirectory() as scratch:
        script = Path(scratch) / "iris-fleet.zsh"
        script.write_text(fleet, encoding="utf-8")
        missing = subprocess.run(
            ["/bin/zsh", "-f", "-c", f"PATH=/nonexistent; source {script}; _claude_for_iris Coordinator"],
            capture_output=True,
            text=True,
            check=False,
        )
    _check(
        "fleet launcher: missing tmux refuses loudly and never runs claude",
        missing.returncode == 1 and "REFUSED: tmux is not installed" in missing.stderr,
        f"expected exit 1 with a REFUSED message, got {missing.returncode}: {missing.stderr!r}",
    )


def _check_git_controller_export() -> None:
    """The git gate's arming boundary is an export, so the export is the surface.

    `git_controller_gate.py` reads no session label: whether the gate evaluates
    at all is decided entirely by where `GIT_CONTROLLER_NAME` is exported
    (D-5a.3-i, Architect's (a)+(c) ruling). Two failure shapes this catches:
    one launcher armed and the other not — a session class that escapes the
    gate by which launcher started it — and a solo render that deletes the line
    but leaves the file syntactically broken. The gate is fail-OPEN when unset,
    so neither shape announces itself at runtime.
    """
    launchers = {
        name: _render((_TEMPLATES_DIR / name).read_text(encoding="utf-8"))
        for name in (
            "claude_launcher.template",
            "codex_launcher.template",
            "fleet_functions.zsh.template",
        )
    }
    armed = {
        name: f'GIT_CONTROLLER_NAME="{_RENDER_TOKENS["{{GIT_CONTROLLER_NAME}}"]}"' in body
        for name, body in launchers.items()
    }
    _check(
        "git gate: ALL launchers export GIT_CONTROLLER_NAME, or the gate is escapable",
        all(armed.values()),
        f"armed per launcher: {armed} — the gate reads no session label, so a "
        "launcher without the export starts sessions the gate never evaluates",
    )
    for name, body in launchers.items():
        solo = "\n".join(
            line for line in body.splitlines() if "GIT_CONTROLLER_NAME=" not in line
        )
        _check(
            f"git gate: {name} still parses as zsh with the line deleted (solo exemption)",
            subprocess.run(
                ["zsh", "-n", "/dev/stdin"],
                input=solo,
                capture_output=True,
                text=True,
                timeout=10,
            ).returncode
            == 0,
            "the solo exemption is performed by DELETING the export line; if that "
            "breaks the launcher, hydration will keep the line instead and arm a "
            "gate the operator was told is off",
        )


def _check_solet_name_export() -> None:
    """Every launcher must export SOLET_NAME, or in-session gate scripts
    and vault-scoped plugin code fail loudly depending on which launcher
    started the shell — §31.1: the daemon launcher (launch.template) always
    exported it; the session launchers (claude/codex/fleet-functions) did
    not, until the 2026-08-02 fix this check pins in place. Unlike
    GIT_CONTROLLER_NAME there is no solo-exemption deletion path for this
    var — it names which solet the shell talks to, not an optional
    fleet-safety control, so every launcher must carry it unconditionally.
    """
    launchers = {
        name: _render((_TEMPLATES_DIR / name).read_text(encoding="utf-8"))
        for name in (
            "launch.template",
            "claude_launcher.template",
            "codex_launcher.template",
            "fleet_functions.zsh.template",
        )
    }
    armed = {
        name: f'SOLET_NAME="{_RENDER_TOKENS["{{SOLET_NAME}}"]}"' in body
        or f'SOLET_NAME={_RENDER_TOKENS["{{SOLET_NAME}}"]}' in body
        for name, body in launchers.items()
    }
    _check(
        "solet name: ALL launchers export SOLET_NAME, or run_smokes.py "
        "and vault-scoped plugin code fail loudly depending on which launcher "
        "started the session",
        all(armed.values()),
        f"armed per launcher: {armed}",
    )


def _check_coordination_receipt_export() -> None:
    """The standalone launcher must bind receipt lookup to profile data, not cwd."""
    rendered = _render((_TEMPLATES_DIR / "claude_launcher.template").read_text(encoding="utf-8"))
    expected = "AGENT_COORDINATION_RECEIPT_PATH=\"/Users/example/Workspace/iris/profile/data/coordination-hooks/claude/installation.v1.json\""
    _check(
        "Claude launcher exports the fixed profile-data coordination receipt path",
        expected in rendered,
        "owner verification must not guess a receipt from the caller cwd",
    )


def _check_marketplace_matches_settings(settings: str) -> None:
    """The catalogue and the settings entry must agree, or the plugin silently vanishes.

    Three independent literals have to line up across two files: the marketplace NAME,
    the plugin NAME, and the `coordination-hooks@<marketplace>` key that joins them.
    Any one of them drifting produces the same symptom — a session that starts fine
    with the plugin simply absent, no error anywhere — which is the zero-hooks defect
    one level up. Derived from the rendered files, never pinned to a literal.
    """
    marketplace = json.loads(
        _render(
            (_TEMPLATES_DIR / "marketplace_json.template").read_text(encoding="utf-8"),
        ),
    )
    settings_json = json.loads(settings)
    market_name = marketplace["name"]
    plugin_names = [entry["name"] for entry in marketplace["plugins"]]
    expected_key = f"{plugin_names[0]}@{market_name}" if plugin_names else ""

    _check(
        "marketplace: registered under the same name the settings entry keys on",
        market_name in settings_json.get("extraKnownMarketplaces", {}),
        f"catalogue declares {market_name!r}, settings registers "
        f"{list(settings_json.get('extraKnownMarketplaces', {}))}",
    )
    _check(
        "marketplace: enabledPlugins key is DERIVED from the catalogue, not pinned",
        expected_key in settings_json.get("enabledPlugins", {}),
        f"expected {expected_key!r} (plugin@marketplace, both read from the "
        f"catalogue); settings has {list(settings_json.get('enabledPlugins', {}))}",
    )
    _check(
        "marketplace: owner is a name with no email (operator ruling 2026-08-01)",
        set(marketplace["owner"]) == {"name"},
        f"owner carries {sorted(marketplace['owner'])}; this lane ships an identity "
        "with no person attached",
    )
    _check(
        "marketplace: plugin source is a relative string, not the "
        "extraKnownMarketplaces object form",
        all(
            isinstance(entry["source"], str)
            and entry["source"]
            and not entry["source"].startswith("/")
            for entry in marketplace["plugins"]
        ),
        "a PLUGIN entry's source must be a plain string relative to the "
        "marketplace root (Claude Code 2.1.220 silently stubs the object form "
        "here); the absolute {\"source\": \"directory\", \"path\": ...} object "
        "is the correct shape for extraKnownMarketplaces ONLY, not for this "
        f"position — got {[entry['source'] for entry in marketplace['plugins']]!r}",
    )


def _check_settings_hook_kb_guidance(settings: str) -> None:
    """KB-first guidance moved OFF the settings hook — re-asserted where it landed.

    WS-5b-core §2.3 deleted the inline settings hooks, so these two invariants have no
    hook text left to check. Both still hold and both still ship: the deployment-facing
    how-and-where is `CLAUDE.md.template`'s job under the 2026-08-01 design ruling
    (hooks carry generic what-and-why; CLAUDE.md carries the specifics). Re-pointing
    rather than deleting — the assertions outlived their old venue, not their subject.
    """
    _check(
        "settings: KB-first guidance is no longer carried by a settings hook",
        "knowledge base" not in settings.lower(),
        "the settings template ships no hooks and therefore no guidance text",
    )
    claude_md = _render(
        (_TEMPLATES_DIR / "CLAUDE.md.template").read_text(encoding="utf-8"),
    )
    _check(
        "CLAUDE.md: editing/config/explaining requires a KB search first",
        "Before editing code, changing config, or explaining a subsystem" in claude_md
        and "search iris's knowledge base for the current design record" in claude_md,
        "missing implementation/debugging KB-first guidance on the surface that now owns it",
    )
    _check(
        "CLAUDE.md: the router/bridge layers are reachable by a named search term",
        "router bridge blue-green local deployment architecture" in claude_md,
        "missing the router/bridge search term — several distinct layers get loosely "
        "called 'the bridge', which is exactly what this pointer disambiguates",
    )


def main() -> int:
    try:
        _check_directory_is_flat()
        _check_file_map_matches_disk()
        _check_refresh_classes_match_disk_and_bundle()
        _check_all_templates()
        _check_no_mcp_first_generated_guidance()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"hydration_templates_render_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
