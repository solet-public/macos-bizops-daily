#!/usr/bin/env python3
"""Assert the plugin's SECURITY.md/README.md claims still describe the tree.

This is the highest-value test in the suite, because the documents ARE the
security review. The failure mode it defends has already happened twice in this
plugin's short history: adding a fifth hook falsified SECURITY.md's
"fixed string literal" claim and its hook counts, and `plugin.json`'s
description had silently omitted `wake_waiter.js` before that. Both were caught
by a human reading carefully. Neither would be caught twice.

Everything here is a source-level check on the shipped files -- an import/require
graph, an exec-form manifest walk, and prose-vs-tree consistency. It is not a
syscall trace: it proves the code does not NAME a network, subprocess, or
file-write primitive, which is what makes the SECURITY.md claims auditable by
reading. Behavioural proof of the wake waiter's one-bit claim lives in
`wake_waiter_smoke.py`, and of the reminders' fixed-literal claim in
`reminder_hooks_smoke.py`.

Run directly; exit 0 on success, non-zero on failure.
"""

from __future__ import annotations

import sys

# Must precede the _harness import: CPython caches a module's bytecode when it
# first LOADS the module, so a flag set inside _harness cannot suppress _harness's
# own .pyc. Keeping the artifact under review free of stray files wins over
# import ordering here.
sys.dont_write_bytecode = True

import ast  # noqa: E402
import json  # noqa: E402
import re  # noqa: E402
from pathlib import Path  # noqa: E402

from _harness import (  # noqa: E402
    HOOKS_DIR,
    PLUGIN_ROOT,
    TESTS_DIR,
    Results,
    is_stdlib_module,
    preflight,
)

# Each entry-point hook and the words a prose surface may use to name it. A new
# hook must be added here, and then every document below must mention it -- that
# coupling is the whole point of this file.
HOOK_KEYWORDS: dict[str, tuple[str, ...]] = {
    "step_zero_reminder.py": ("knowledge-base-first", "kb-first", "knowledge base"),
    "check_messages_reminder.py": ("check-your-messages", "check your messages"),
    "role_binding_reminder.py": ("role-binding", "role binding"),
    "wake_waiter.py": ("wake waiter", "idle-wake", "wake_waiter"),
    "git_controller_gate.py": ("git-mutation", "git-controller", "git controller"),
    "heartbeat_report_alive.py": ("heartbeat", "report_alive", "report-alive"),
    "rotation_due_watch.py": ("rotation-due", "rotation due"),
    "rotation_due_notice.py": ("rotation-due notice", "rotation_due_notice"),
    "capture.py": ("memory-passthrough", "memory passthrough"),
    "session_context.py": ("memory-passthrough", "memory passthrough"),
    "drain.py": ("memory-passthrough", "memory passthrough"),
    "hydrate_render.py": ("memory-passthrough", "memory passthrough"),
    "index_render.py": ("memory-passthrough", "memory passthrough"),
    "sync.py": ("memory-passthrough", "memory passthrough"),
    "headless_tool_allowlist_gate.py": ("spawn-injected",),
    "capture_session_mapping.py": ("spawn-injected",),
}

# R4 seed-packaging audit, Package B (2026-08-10): files that are
# entry-point-SHAPED (no underscore prefix, so _entry_point_hooks() picks
# them up) but are NOT Claude Code hooks at all -- they never fire
# automatically on any event; the agent invokes them directly via Bash, on
# its own judgment, following session_context.py's own printed
# instructions. Exempted ONLY from "must be wired in hooks.json" --
# every OTHER check (documentation, stdlib-only, no-network-unless-
# disclosed, subprocess/file-write shape) still applies in full.
AGENT_INVOKED_CLI_UTILITIES = frozenset(
    {"drain.py", "hydrate_render.py", "index_render.py", "sync.py"},
)
NON_HOOK_MODULES = frozenset({"coordination_owner.py"})

# R4 Package C (2026-08-10): files that are entry-point-SHAPED and are ALSO
# invoked by a DIFFERENT plugin's spawn mechanism -- a spawned headless/tmux
# worker's host adapter (agent_messaging_plugin) references these by path in
# a generated Claude Code `--settings` blob at spawn time, independent of
# this plugin's own hooks.json wiring. They ship here purely as the fallback
# copy a born clone carries (rung 2 of the adapters' own resolution ladder);
# the origin checkout's `.claude/hooks/<file>` is rung 1 and the primary
# copy. As of `0.8.0` (seed feedback #40/§51.1, 2026-08-24) both are ALSO
# registered in hooks.json -- the two delivery routes are deliberately
# independent, not mutually exclusive: a managed policy's
# `strictPluginOnlyCustomization: ["hooks"]` strips the adapter's own
# `--settings` injection, and the hooks.json registration is what survives
# that policy for a plugin already in `strictKnownMarketplaces`. A session
# under a policy that strips neither gets both -- harmless by design, see
# each file's own module docstring. Kept as a named set (rather than folded
# into the ordinary "must be wired" hooks) so the check below asserts BOTH
# facts explicitly: registered here, AND still the adapter's own fallback
# copy, never one or the other.
SPAWN_INJECTED_HOOKS = frozenset(
    {"headless_tool_allowlist_gate.py", "capture_session_mapping.py"},
)

# Documents that must name every hook. hooks.json and plugin.json carry a
# one-line description; README.md and SECURITY.md carry the prose.
PROSE_SURFACES = ("hooks/hooks.json", ".claude-plugin/plugin.json", "README.md", "SECURITY.md")

# Module names that would falsify "no network I/O of any kind in any hook".
NETWORK_MODULES = frozenset(
    {
        "http", "https", "http2", "net", "dgram", "tls", "dns", "socket",
        "urllib", "urllib2", "urllib3", "httplib", "http.client", "ftplib",
        "telnetlib", "smtplib", "requests", "httpx", "aiohttp", "websocket",
        "websockets",
    }
)

# Source-level names that would falsify "No hook writes a file as an action of
# its own" for a hook NOT in FILE_WRITE_CAPABLE_HOOKS. Reads are fine and
# expected (the gate reads the session file). heartbeat_report_alive.py and
# rotation_due_watch.py are the two DISCLOSED exceptions: both write small,
# secret-free throttle/latch marker files (a timestamp, nothing else) under
# AGENT_HEARTBEAT_MARKER_DIR (or a temp-dir fallback for the latter) -- see
# SECURITY.md's "heartbeat and rotation-due" section for the full contract.
WRITE_PRIMITIVES = (
    "write_text", "write_bytes", "os.remove", "os.unlink", "os.mkdir", "os.makedirs",
    "shutil.", "tempfile.NamedTemporary", "os.rename",
    # R4 seed-packaging audit, Package B (2026-08-10): the memory-passthrough
    # files' own write idiom is `open(path, "w"/"a", encoding=...)`, which
    # none of the tokens above catch -- without these two, hydrate_render.py
    # and capture.py would pass this check vacuously (no primitive NAMED)
    # despite genuinely writing files directly in their own source, not via
    # delegation. Narrow on purpose: matches this codebase's own consistent
    # `open(x, "w"/"a", encoding=...)` style, not every possible open() call.
    '"w", encoding', '"a", encoding',
)
# Two distinct write shapes: MARKER_ONLY writes a bare timestamp and
# nothing else (Package A's two hooks, checked below); CONTENT_BEARING
# writes real fact content sourced from an already-authenticated export
# snapshot or the agent's own append-only journal, never held to
# MARKER_ONLY's narrower bare-timestamp claim (membership here only feeds
# the write-primitive/subprocess-shape checks above, not a content-source
# claim -- narrowing that claim precisely enough to check accurately was
# tried and dropped this pass, see the commit message's disclosed gaps).
MARKER_ONLY_WRITE_HOOKS = frozenset({"heartbeat_report_alive.py", "rotation_due_watch.py"})
CONTENT_BEARING_WRITE_HOOKS = frozenset(
    {
        "capture.py", "hydrate_render.py", "index_render.py", "_journal.py",
        # R4 Package C (2026-08-10): writes a real content record (agent_instance_id,
        # claude_session_id, captured_at, capture_source) to a file-per-firing spool,
        # not a bare timestamp -- same CONTENT_BEARING class as capture.py's journal
        # entry, never MARKER_ONLY's narrower bare-timestamp claim.
        "capture_session_mapping.py",
        # L4b (2026-08-17): rewrites the self-notify marker it just surfaced,
        # stamping `surfaced_at` INTO the existing record so the latch and the
        # record it latches cannot disagree. That carries the marker's content
        # back out with it, so it is CONTENT_BEARING rather than MARKER_ONLY --
        # the bare-timestamp claim would be false of it.
        "rotation_due_notice.py",
    },
)
FILE_WRITE_CAPABLE_HOOKS = MARKER_ONLY_WRITE_HOOKS | CONTENT_BEARING_WRITE_HOOKS

# Source-level names that would falsify "no hook outside SUBPROCESS_CAPABLE_HOOKS
# executes a subprocess." wake_waiter.py, heartbeat_report_alive.py, and
# rotation_due_watch.py are the three DISCLOSED exceptions -- each is checked
# against its OWN fixed-shape contract below (no shell=True, subprocess.run
# only, a fixed/bounded argv), not just membership in the set.
# "subprocess." (WITH the trailing dot), never the bare word "subprocess" --
# several of this plugin's own docstrings use the plain English phrase "a
# hook subprocess has no MCP bridge" (memory-passthrough files), which a
# bare-substring match would misclassify as a real subprocess.run/Popen call.
# Actual usage always appears as `subprocess.<method>(` somewhere in the same
# file even when `import subprocess` itself has no trailing dot, so this
# stays a true-positive-preserving, false-positive-eliminating narrowing --
# same class of fix as the wake-waiter argv-shape regex above.
SUBPROCESS_PRIMITIVES = (
    "subprocess.", "os.system", "os.popen", "os.execv", "os.execl", "os.spawn", "os.fork",
    "pty.spawn", "commands.getoutput",
)
SUBPROCESS_OWNER = "wake_waiter.py"
SUBPROCESS_CAPABLE_HOOKS = frozenset(
    {"wake_waiter.py", "heartbeat_report_alive.py", "rotation_due_watch.py", "sync.py"},
)
# The fixed process_key each hook's subprocess.run call must carry verbatim --
# same shape-fixity property as the wake waiter's argv check below, applied to
# the solet-CLI callers. sync.py legitimately calls more than one
# process_key (export once, upsert per pending entry); its own named
# constant EXPORT_PROCESS_KEY is checked as the representative fixed value --
# proving the fixed-shape/no-shell/subprocess.run-only properties hold, the
# same bar every other solet-calling hook here is held to.
SOLET_CALLER_PROCESS_KEYS = {
    "heartbeat_report_alive.py": "plugin::agent_messaging_plugin::report_alive",
    "rotation_due_watch.py": "plugin::agent_messaging_plugin::session_status",
    "sync.py": "service_interface::memory_service::export_memories",
}

_NUMBER_WORDS = {
    1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen", 15: "fifteen",
    16: "sixteen", 17: "seventeen", 18: "eighteen", 19: "nineteen", 20: "twenty",
}

# Shell metacharacters that must never appear in an exec-form argv. `${VAR}` is
# Claude Code's own manifest substitution and is expressly allowed; `$(` is not.
SHELL_METACHARACTERS = ("$(", "`", ";", "|", "&", ">", "<", "\n")

ALLOWED_COMMANDS = frozenset({"python3"})
PLUGIN_ROOT_TOKEN = "${CLAUDE_PLUGIN_ROOT}/hooks/"

# Cadence ruling (2026-08-11, cadence-only): step_zero_reminder.py and
# check_messages_reminder.py moved from UserPromptSubmit to
# SessionStart(startup|resume|clear) so they stop re-firing every prompt
# turn. The 2026-08-01 always-armed ruling for step_zero_reminder.py (no
# environment gate) is untouched by this move and stays pinned by
# reminder_hooks_smoke.py's check_step_zero_fires_everywhere.
# session_context.py was never in scope for this move (it is the
# memory-passthrough context-gauge hook, not a reminder) and keeps its
# pre-existing UserPromptSubmit + SessionStart bindings.
REQUIRED_SESSION_START_CADENCE = {
    "step_zero_reminder.py",
    "check_messages_reminder.py",
    "role_binding_reminder.py",
}
# RED MUTATION: re-adding either of these two to any UserPromptSubmit entry.
FORBIDDEN_ON_USER_PROMPT_SUBMIT = frozenset(
    {"step_zero_reminder.py", "check_messages_reminder.py"}
)


def _read(relative: str) -> str:
    return (PLUGIN_ROOT / relative).read_text(encoding="utf-8")


def _read_prose(relative: str) -> str:
    """Read a document with newlines collapsed, so hard-wrapped phrases still match."""
    return re.sub(r"\s+", " ", _read(relative))


def _entry_point_hooks() -> list[str]:
    """Hook scripts Claude Code invokes directly (siblings start with '_')."""
    return sorted(
        path.name
        for path in HOOKS_DIR.iterdir()
        if path.is_file() and path.suffix in {".js", ".py"} and not path.name.startswith("_")
        and path.name not in NON_HOOK_MODULES
    )


def _sibling_modules() -> list[str]:
    return sorted(
        path.name
        for path in HOOKS_DIR.iterdir()
        if path.is_file() and path.suffix == ".py" and path.name.startswith("_")
    )


def _manifest_entries(manifest: dict[str, object]) -> list[dict[str, object]]:
    """Flatten hooks.json into the individual hook invocation records."""
    entries: list[dict[str, object]] = []
    hooks = manifest.get("hooks")
    if not isinstance(hooks, dict):
        return entries
    for groups in hooks.values():
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict):
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                continue
            for entry in inner:
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries


def _py_imports(source: str, filename: str) -> set[str]:
    tree = ast.parse(source, filename=filename)
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module.split(".")[0])
    return modules


def check_manifest_is_exec_form(res: Results, entries: list[dict[str, object]]) -> None:
    """The property the plugin exists for: no shell anywhere in the invocation path."""
    for entry in entries:
        label = json.dumps(entry.get("args", entry))[:70]
        res.check(entry.get("type") == "command", f"exec-form type: {label}", f"got {entry.get('type')!r}")
        command = entry.get("command")
        res.check(
            command in ALLOWED_COMMANDS,
            f"bare interpreter, not a shell: {label}",
            f"command={command!r} not in {sorted(ALLOWED_COMMANDS)}",
        )
        args = entry.get("args")
        if not res.check(
            isinstance(args, list),
            f"args is an array, not a string: {label}",
            f"got {type(args).__name__}",
        ):
            continue
        assert isinstance(args, list)
        for arg in args:
            if not res.check(isinstance(arg, str), f"arg is a string: {label}"):
                continue
            found = [meta for meta in SHELL_METACHARACTERS if meta in arg]
            res.check(not found, f"no shell metacharacter in arg: {arg}", f"found {found}")
        first = args[0] if args else None
        res.check(
            isinstance(first, str) and first.startswith(PLUGIN_ROOT_TOKEN),
            f"script path is plugin-root relative: {label}",
            f"got {first!r}",
        )


def _group_scripts(group: object) -> set[str]:
    """The script basenames a single hooks.json matcher-group invokes."""
    if not isinstance(group, dict):
        return set()
    inner = group.get("hooks")
    if not isinstance(inner, list):
        return set()
    scripts: set[str] = set()
    for entry in inner:
        args = entry.get("args") if isinstance(entry, dict) else None
        if isinstance(args, list) and args and isinstance(args[0], str):
            scripts.add(Path(args[0]).name)
    return scripts


def _group_matcher(group: object) -> str:
    matcher = group.get("matcher", "") if isinstance(group, dict) else ""
    return matcher if isinstance(matcher, str) else ""


def _bind_by_event_matcher(hooks: dict[str, object]) -> dict[tuple[str, str], set[str]]:
    """Flatten hooks.json into {(event, matcher): {script names}}.

    check_tree_matches_manifest proves each script is wired SOMEWHERE; this
    keeps the (event, matcher) grouping, unlike _manifest_entries' full
    flatten, because check_reminder_cadence's claim is specifically about
    which matcher group a script sits in, not merely that it is referenced
    anywhere in the manifest.
    """
    bound: dict[tuple[str, str], set[str]] = {}
    for event, groups in hooks.items():
        if not isinstance(event, str) or not isinstance(groups, list):
            continue
        for group in groups:
            key = (event, _group_matcher(group))
            bound.setdefault(key, set()).update(_group_scripts(group))
    return bound


def check_reminder_cadence(res: Results, manifest: dict[str, object]) -> None:
    """Pin the 2026-08-11 cadence ruling directly against hooks.json."""
    hooks = manifest.get("hooks")
    if not res.check(isinstance(hooks, dict), "hooks.json has a hooks object"):
        return
    assert isinstance(hooks, dict)
    bound = _bind_by_event_matcher(hooks)

    session_start = bound.get(("SessionStart", "startup|resume|clear"), set())
    res.check(
        REQUIRED_SESSION_START_CADENCE <= session_start,
        "step_zero/check_messages/role_binding reminders all fire on "
        "SessionStart(startup|resume|clear)",
        f"missing {sorted(REQUIRED_SESSION_START_CADENCE - session_start)}",
    )

    on_user_prompt_submit: set[str] = set()
    for (event, _matcher), scripts in bound.items():
        if event == "UserPromptSubmit":
            on_user_prompt_submit |= scripts
    offenders = on_user_prompt_submit & FORBIDDEN_ON_USER_PROMPT_SUBMIT
    res.check(
        not offenders,
        "step_zero/check_messages carry no UserPromptSubmit binding",
        f"found {sorted(offenders)} still bound to UserPromptSubmit",
    )
    # session_context.py is deliberately NOT in FORBIDDEN_ON_USER_PROMPT_SUBMIT
    # -- it keeps its pre-existing UserPromptSubmit binding, unaffected by
    # this ruling.
    res.check(
        "session_context.py" in on_user_prompt_submit,
        "session_context.py keeps its pre-existing UserPromptSubmit binding "
        "(out of scope for the reminder-cadence move)",
        f"UserPromptSubmit carries {sorted(on_user_prompt_submit)}",
    )


def check_tree_matches_manifest(res: Results, entries: list[dict[str, object]], hooks: list[str]) -> None:
    referenced: set[str] = set()
    for entry in entries:
        args = entry.get("args")
        if isinstance(args, list) and args and isinstance(args[0], str):
            referenced.add(Path(args[0]).name)

    for name in referenced:
        res.check((HOOKS_DIR / name).is_file(), f"hooks.json path exists on disk: {name}")

    for name in hooks:
        if name in AGENT_INVOKED_CLI_UTILITIES:
            res.check(
                name not in referenced,
                f"agent-invoked CLI utility is NOT wired in hooks.json: {name}",
                "this file is documented as never auto-firing -- a hooks.json "
                "entry for it would be a real behavior change, not just a "
                "classification error",
            )
            continue
        if name in SPAWN_INJECTED_HOOKS:
            res.check(
                name in referenced,
                f"spawn-injected worker hook IS ALSO wired in hooks.json: {name}",
                "this file is documented as registered here specifically so it "
                "survives a managed policy that strips a spawned worker's own "
                "adapter-generated --settings copy (strictPluginOnlyCustomization) "
                "-- a missing hooks.json entry would silently reopen seed feedback "
                "#40/§51.1 for that exact host class",
            )
            continue
        res.check(name in referenced, f"hook is wired in hooks.json: {name}", "present on disk but never invoked")

    orphans = referenced - set(hooks)
    res.check(not orphans, "no hooks.json entry points at a missing script", f"dangling: {sorted(orphans)}")

    res.check(
        set(hooks) == set(HOOK_KEYWORDS),
        "HOOK_KEYWORDS covers exactly the hooks on disk",
        f"on disk {sorted(set(hooks) - set(HOOK_KEYWORDS))}, "
        f"in table only {sorted(set(HOOK_KEYWORDS) - set(hooks))}",
    )


def check_siblings_are_not_orphans(res: Results, siblings: list[str], hooks: list[str]) -> None:
    """A '_'-prefixed module is exempt from the manifest only if a hook imports it."""
    imported: set[str] = set()
    for name in hooks:
        if not name.endswith(".py"):
            continue
        for module in _py_imports((HOOKS_DIR / name).read_text(encoding="utf-8"), name):
            imported.add(f"{module}.py")
    for sibling in siblings:
        res.check(sibling in imported, f"sibling module is imported by a hook: {sibling}", "unreferenced file in the shipped tree")


def check_every_hook_is_documented(res: Results, hooks: list[str]) -> None:
    for surface in PROSE_SURFACES:
        text = _read_prose(surface).lower()
        for name in hooks:
            # Naming the script file itself is always sufficient documentation;
            # the prose synonyms exist for surfaces that describe rather than list.
            keywords = (name, *HOOK_KEYWORDS.get(name, ()))
            res.check(
                any(word.lower() in text for word in keywords),
                f"{surface} names {name}",
                f"none of {list(keywords)} present",
            )


def check_security_md_counts(res: Results, hooks: list[str]) -> None:
    text = _read_prose("SECURITY.md")
    total = len(hooks)
    reminders = len([name for name in hooks if name.endswith("_reminder.py")])

    res.check(
        f"is {_NUMBER_WORDS[total]} Claude Code hooks" in text,
        "SECURITY.md hook count matches the tree",
        f"tree has {total}; expected the phrase 'is {_NUMBER_WORDS[total]} Claude Code hooks'",
    )
    res.check(
        f"{_NUMBER_WORDS[reminders]} context reminders" in text,
        "SECURITY.md reminder count matches the tree",
        f"tree has {reminders} *_reminder.py",
    )
    inert = total - len(SUBPROCESS_CAPABLE_HOOKS)
    res.check(
        f"other {_NUMBER_WORDS[inert]} hooks execute nothing" in text,
        "SECURITY.md 'execute nothing' count matches the tree",
        f"expected 'other {_NUMBER_WORDS[inert]} hooks execute nothing' "
        f"({total} total minus the {len(SUBPROCESS_CAPABLE_HOOKS)} disclosed subprocess-capable hooks)",
    )
    default_off = total - 1  # every hook except the unconditionally-armed step_zero_reminder.py
    # .lower() here (unlike the two checks above): this phrase is expected to
    # open its own sentence in prose ("Six of the seven..."), so a literal
    # lowercase match would wrongly fail on ordinary sentence-initial
    # capitalization.
    res.check(
        f"{_NUMBER_WORDS[default_off]} of the {_NUMBER_WORDS[total]} hooks are default-off" in text.lower(),
        "SECURITY.md default-off count matches the tree",
        f"expected '{_NUMBER_WORDS[default_off]} of the {_NUMBER_WORDS[total]} hooks are default-off'",
    )


def check_no_network(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        modules = _py_imports(source, name)
        offending = sorted(modules & NETWORK_MODULES)
        res.check(not offending, f"no network module in {name}", f"imports {offending}")


def check_no_file_writes(res: Results, hooks: list[str], siblings: list[str]) -> None:
    for name in hooks + siblings:
        if name in FILE_WRITE_CAPABLE_HOOKS:
            continue
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        hits = [token for token in WRITE_PRIMITIVES if token in source]
        res.check(not hits, f"no file-write primitive in {name}", f"found {hits}")


def check_file_write_hooks_write_only_markers(res: Results) -> None:
    """The MARKER_ONLY file-writers may only write a throttle/latch marker
    -- a timestamp string -- never anything content-bearing. Source-level:
    proves no OTHER write target exists, e.g. no writing to a path built from
    stdin/tool-input data, which would be a materially different claim than
    'writes its own fixed marker file'."""
    for name in sorted(MARKER_ONLY_WRITE_HOOKS):
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        res.check(
            "write_text(str(time.time()))" in source,
            f"{name}'s only write is a bare timestamp marker",
            "expected the fixed 'write_text(str(time.time()))' shape",
        )


def check_subprocess_capable_hooks(res: Results, hooks: list[str], siblings: list[str]) -> None:
    owners: list[str] = []
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        if any(token in source for token in SUBPROCESS_PRIMITIVES):
            owners.append(name)
    res.check(
        set(owners) == SUBPROCESS_CAPABLE_HOOKS,
        "exactly the disclosed hooks can execute a subprocess",
        f"expected {sorted(SUBPROCESS_CAPABLE_HOOKS)}, found {sorted(owners)}",
    )

    for name, process_key in SOLET_CALLER_PROCESS_KEYS.items():
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        res.check(
            re.search(r"\bshell\s*=\s*True", source) is None,
            f"{name} never passes a shell option",
            "a `shell=True` option appears in the source",
        )
        res.check(
            source.count("subprocess.run") >= 1
            and not re.search(r"\bsubprocess\.(Popen|call|check_call|check_output)\s*\(|\bos\.system\s*\(", source),
            f"{name} uses subprocess.run only",
            "a different subprocess-family call is present",
        )
        res.check(
            process_key in source,
            f"{name}'s solet-call argv carries its fixed process_key",
            f"expected {process_key!r} to appear in the source as a named constant",
        )
        res.check(
            re.search(r'\["solet-bridge",\s*"call",', source) is not None,
            f"{name} invokes solet via a fixed argv prefix",
            'expected the literal ["solet-bridge", "call", ...] argument vector',
        )

    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    # `shell=True` is the option that would turn a fixed argv into a shell
    # string. Match the option form, not the word -- the source says "no shell"
    # in prose, and a comment must not be able to fail this check.
    res.check(
        re.search(r"\bshell\s*=\s*True", waiter) is None,
        f"{SUBPROCESS_OWNER} never passes a shell option",
        "a `shell=True` option appears in the source",
    )
    res.check(
        waiter.count("subprocess.run") >= 1
        and not re.search(r"\bsubprocess\.(Popen|call|check_call|check_output)\s*\(|\bos\.system\s*\(", waiter),
        f"{SUBPROCESS_OWNER} uses subprocess.run only",
        "a different subprocess-family call is present",
    )
    # The argv is fixed in SHAPE: literal subcommand, literal flag, and one
    # numeric element that can only come out of resolve_max_wait_s() — a
    # validated positive integer, never raw environment text. The behavioural
    # half (malformed overrides fall back loudly to the default) is pinned by
    # wake_waiter_smoke.py's bounded-wait cases.
    res.check(
        '[cli, "wake", "--max-wait", str(resolve_max_wait_s())]' in waiter,
        f"{SUBPROCESS_OWNER} argv is fixed",
        'expected the literal ["wake", "--max-wait", str(resolve_max_wait_s())] argument vector',
    )


def check_child_output_is_unread(res: Results) -> None:
    """SECURITY.md: the child's stdout/stderr are 'dropped unread'.

    This has to be a SOURCE check, not a behavioural one. Switching the spawn to
    `stdout=subprocess.PIPE` reads the child's streams into the parent --
    falsifying "unread" -- while still emitting nothing, so no black-box test
    can see it. Mutation testing found exactly this hole (against the prior
    Node implementation's equivalent `stdio: "pipe"` shape; the risk carries
    over unchanged to this hook's Python `subprocess.run` call).
    """
    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    for stream in ("stdin", "stdout", "stderr"):
        res.check(
            re.search(rf"\b{stream}\s*=\s*subprocess\.DEVNULL", waiter) is not None,
            f"{SUBPROCESS_OWNER} spawns with {stream} set to DEVNULL",
            f"{stream}=subprocess.DEVNULL not found in the source",
        )
    for attr in ("result.stdout", "result.stderr", "result.output"):
        res.check(
            attr not in waiter,
            f"{SUBPROCESS_OWNER} never reads {attr}",
            "the child's output is referenced in the source",
        )


def check_docs_name_the_bounded_wake_argv(res: Results) -> None:
    """README.md/SECURITY.md must describe the wake waiter's REAL argv.

    The failing mutation this names: the waiter's argument vector changes
    (as it did on 2026-08-09, when the bounded `--max-wait` was added) and a
    prose surface keeps describing the old shape -- README.md did exactly
    that, claiming "the single fixed argument `wake`" for three days while
    the source and SECURITY.md both already carried the bound. The expected
    tokens are DERIVED from the source literal (the same one
    check_subprocess_capable_hooks pins), so a future argv change goes red
    here until every prose surface names the new shape -- the same
    derive-from-the-wiring class as reminder_hooks_smoke.py's
    check_manifest_bound_events_echo.
    """
    waiter = (HOOKS_DIR / SUBPROCESS_OWNER).read_text(encoding="utf-8")
    argv_literal = re.search(
        r'\[cli,\s*((?:"[^"]+",\s*)+)str\(resolve_max_wait_s\(\)\)\]', waiter,
    )
    res.check(
        argv_literal is not None,
        f"{SUBPROCESS_OWNER}'s argv literal is extractable for doc coupling",
        "the argv literal's shape changed; update this check alongside the docs",
    )
    tokens = re.findall(r'"([^"]+)"', argv_literal.group(1)) if argv_literal else []
    for surface in ("README.md", "SECURITY.md"):
        text = _read_prose(surface)
        for token in tokens:
            res.check(
                token in text,
                f"{surface} names the waiter argv token {token!r}",
                f"the source argv carries {token!r} but {surface} never mentions it",
            )
        res.check(
            "AGENT_WAKE_MAX_WAIT_S" in text,
            f"{surface} names the wait-bound override variable",
            "AGENT_WAKE_MAX_WAIT_S absent",
        )
        # The stale pre-bound form closes its backtick right after `wake`;
        # the current form always continues with the bound.
        res.check(
            re.search(r"\$AGENT_WAKE_CLI wake`", text) is None,
            f"{surface} does not describe the stale unbounded argv",
            "found the pre-2026-08-09 `$AGENT_WAKE_CLI wake` (no --max-wait) form",
        )


# The one disclosed, narrow exception to "stdlib-only": rotation_due_watch.py
# imports agent_messaging_plugin.rotation_thresholds -- a zero-third-party-
# dependency SAME-PLATFORM module (not a PyPI package; ships alongside this
# plugin in every capability bundle), resolved via CLAUDE_PROJECT_DIR and
# imported inside a try/except that degrades gracefully (warns + skips the
# notify-threshold computation) if it is ever absent. Never silently widen
# this set -- a real third-party or unbounded dependency belongs in
# SECURITY.md's Supply chain section, not just here.
ALLOWED_CROSS_PLATFORM_IMPORTS: dict[str, frozenset[str]] = {
    "rotation_due_watch.py": frozenset({"agent_messaging_plugin"}),
}


def check_stdlib_only(res: Results, hooks: list[str], siblings: list[str]) -> None:
    sibling_modules = {Path(name).stem for name in siblings}
    # AGENT_INVOKED_CLI_UTILITIES are legitimate import targets for each
    # other within this plugin (hydrate_render.py imports index_render;
    # sync.py imports drain and hydrate_render directly to avoid an extra
    # subprocess layer) -- same "local module, not a third-party package"
    # reasoning as sibling_modules above, just for the non-underscore set.
    utility_modules = {
        *(Path(name).stem for name in AGENT_INVOKED_CLI_UTILITIES),
        *(Path(name).stem for name in NON_HOOK_MODULES),
    }
    for name in hooks + siblings:
        source = (HOOKS_DIR / name).read_text(encoding="utf-8")
        allowed_cross_platform = ALLOWED_CROSS_PLATFORM_IMPORTS.get(name, frozenset())
        for module in sorted(_py_imports(source, name)):
            res.check(
                is_stdlib_module(module)
                or module in sibling_modules
                or module in utility_modules
                or module in allowed_cross_platform,
                f"{name} imports only the Python stdlib (plus its disclosed exceptions)",
                f"{module!r} is neither stdlib, a sibling module, a local "
                "utility module, nor a disclosed exception",
            )


def check_verification_section(res: Results) -> None:
    """SECURITY.md's Verification section must cite tests that actually exist.

    GTE-12: a citation may be the bare plugin-relative form (`tests/<name>.py`)
    OR a full repo-relative path ending in it -- ACCEPT BOTH, never swap one
    for the other, because the cited-path gate (a checkout-wide instrument
    this artifact does not control) resolves every backtick token repo-root-
    absolute only, while the bare form is what stays meaningful when this
    plugin is reviewed standalone with no ancestor `plugins/.../` path to
    resolve against.

    The full form is recognized ONLY by the `coordination-hooks/` boundary
    immediately before `tests/`, not by any arbitrary prefix: this table also
    carries pre-existing CHECKOUT-EXTERNAL citations one directory shape off
    from this plugin's own (e.g. `.claude/hooks/tests/...`,
    `plugins/agent_messaging_plugin/tests/...`) that legitimately point
    OUTSIDE this plugin and must never be checked against PLUGIN_ROOT. A
    prefix-agnostic `tests/<name>.py` suffix match would misfire on those --
    caught live the first time this widened (GTE-12's own build, three false
    FAILs). Requiring the plugin's own directory name as the anchor is what
    keeps "cites one of MY tests" and "cites someone else's tests/ dir that
    happens to share a filename shape" distinguishable without hardcoding
    this plugin's ancestor path (still self-contained: `coordination-hooks`
    is this plugin's OWN name, the same invariant `PLUGIN_ROOT` already
    encodes structurally, never an assumption about what sits above it).
    """
    text = _read("SECURITY.md")
    if not res.check("## Verification" in text, "SECURITY.md has a Verification section"):
        return
    raw_cited = set(re.findall(r"`((?:[\w./-]*coordination-hooks/)?tests/[A-Za-z0-9_]+\.py)`", text))
    cited = {citation.rsplit("coordination-hooks/", 1)[-1] for citation in raw_cited}
    res.check(bool(cited), "Verification section cites at least one test file")
    for relative in sorted(cited):
        res.check((PLUGIN_ROOT / relative).is_file(), f"cited test exists: {relative}")

    on_disk = {
        f"tests/{path.name}"
        for path in TESTS_DIR.iterdir()
        if path.is_file() and path.name.endswith("_smoke.py")
    }
    uncited = on_disk - cited
    res.check(not uncited, "every smoke in tests/ is cited in SECURITY.md", f"uncited: {sorted(uncited)}")


def check_artifact_is_self_contained(res: Results) -> None:
    """No test may reach outside the plugin, or the artifact stops standing alone.

    This is what the pre-existing repo-side driver could NOT satisfy: it resolves
    ``parents[3]`` to reach ``.claude/hooks/tests/``, which does not exist once
    the plugin directory is handed to a reviewer on its own.
    """
    escapes = ("parents[2]", "parents[3]", "parents[4]", "/.claude/", "os.pardir")
    for path in sorted(TESTS_DIR.glob("*.py")):
        # This file DEFINES the escape patterns, so its own source necessarily
        # contains them; scanning it would be a guaranteed self-hit.
        if path.name == Path(__file__).name:
            continue
        source = path.read_text(encoding="utf-8")
        for token in escapes:
            res.check(
                token not in source,
                f"{path.name} does not escape the plugin root",
                f"found {token!r}",
            )


def check_plugin_manifest_loads(res: Results) -> None:
    """The manifest that MOUNTS every hook must itself be loadable.

    Claude Code validates `.claude-plugin/plugin.json` as a whole: one
    malformed optional field rejects the ENTIRE manifest and registers zero
    hooks. Every other check in this file — and all 176 gate cases next door
    — verifies hook scripts that, in that state, never run at all. Measured
    2026-07-31: `"homepage": "<<PLUGIN_HOMEPAGE_URL>>"` failed URL validation
    and Claude Code reported "Registered 0 hooks from 1 plugins".

    So: no unsubstituted `<<…>>` placeholder may survive anywhere in the
    manifest, and `homepage`, if present, must be a real absolute URL. This
    is the manifest-level member of the same family as the gate's routing
    bug — the thing that carries input to the checked code is as much part
    of the artifact as the code.
    """
    relative = ".claude-plugin/plugin.json"
    path = PLUGIN_ROOT / relative
    if not res.check(path.is_file(), f"plugin manifest exists: {relative}"):
        return
    raw = path.read_text(encoding="utf-8")

    placeholders = sorted(set(re.findall(r"<<[^<>]+>>", raw)))
    res.check(
        not placeholders,
        f"no unsubstituted placeholder survives in {relative}",
        f"found {placeholders} — no substitution machinery exists for the "
        f"`<<…>>` form, so these ship verbatim",
    )
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as exc:
        res.check(False, f"{relative} is valid JSON", str(exc))
        return

    homepage = manifest.get("homepage")
    if homepage is not None:
        res.check(
            isinstance(homepage, str)
            and re.match(r"^https?://[^\s<>]+\.[^\s<>]+", homepage) is not None,
            "manifest 'homepage' is an absolute http(s) URL",
            f"got {homepage!r} — this field alone rejects the whole manifest",
        )
    author = manifest.get("author")
    if author is not None:
        email = author.get("email") if isinstance(author, dict) else None
        if email is not None:
            res.check(
                isinstance(email, str) and re.match(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$", email) is not None,
                "manifest author.email is a real address",
                f"got {email!r}",
            )


def main() -> int:
    preflight()
    res = Results("coordination-hooks — manifest and security-claim consistency")

    hooks = _entry_point_hooks()
    siblings = _sibling_modules()
    manifest = json.loads(_read("hooks/hooks.json"))
    entries = _manifest_entries(manifest)

    print(f"Entry-point hooks: {len(hooks)} — {', '.join(hooks)}")
    print(f"Sibling modules:   {len(siblings)} — {', '.join(siblings) or 'none'}")
    print(f"Manifest entries:  {len(entries)}")
    print("-" * 68)

    res.check(bool(hooks), "at least one hook exists", "hooks/ has no entry points")
    res.check(bool(entries), "hooks.json registers at least one hook")

    check_manifest_is_exec_form(res, entries)
    check_reminder_cadence(res, manifest)
    check_tree_matches_manifest(res, entries, hooks)
    check_siblings_are_not_orphans(res, siblings, hooks)
    check_every_hook_is_documented(res, hooks)
    check_security_md_counts(res, hooks)
    check_no_network(res, hooks, siblings)
    check_no_file_writes(res, hooks, siblings)
    check_file_write_hooks_write_only_markers(res)
    check_subprocess_capable_hooks(res, hooks, siblings)
    check_child_output_is_unread(res)
    check_docs_name_the_bounded_wake_argv(res)
    check_stdlib_only(res, hooks, siblings)
    check_verification_section(res)
    check_artifact_is_self_contained(res)
    check_plugin_manifest_loads(res)

    return res.finish()


if __name__ == "__main__":
    sys.exit(main())
