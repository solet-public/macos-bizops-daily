"""The coordination-hook interpreter pin, shared by the installer that writes it and the Manager that recognizes it.

The seed ships each coordination-hook manifest with bare ``python3`` hook
commands.  The target's coding-agent install stage (``<cli>.patch_hook_interpreter``)
rewrites every such command to the instance's own ``<target>/.venv/bin/python3``
and writes the manifest back, so every real solet carries a tracked modification
under an executed-code root.  The Manager admits that modification through an
update only when the working-tree bytes are exactly this transform applied to
the committed blob, so the transform lives here, in the one stdlib-only package
both sides import, rather than as two copies that could drift.
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import cast

__all__ = [
    "CLAUDE_HOOK_MANIFEST",
    "CODEX_HOOK_MANIFEST",
    "HOOK_MANIFEST_PATHS",
    "INSTANCE_INTERPRETER",
    "HookManifestError",
    "hook_commands",
    "instance_interpreter",
    "pin_hook_interpreter",
]

#: The two tracked manifests the install stage rewrites, relative to the target root.
CLAUDE_HOOK_MANIFEST = "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks/hooks.json"
CODEX_HOOK_MANIFEST = "plugins/github_midwife_plugin/codex_plugin/coordination-hooks/hooks/hooks.json"
HOOK_MANIFEST_PATHS: tuple[str, ...] = (CLAUDE_HOOK_MANIFEST, CODEX_HOOK_MANIFEST)
#: The instance interpreter, relative to the target root; joined, never resolved (the venv entry is a symlink).
INSTANCE_INTERPRETER = ".venv/bin/python3"

type _Hook = dict[str, object]


class HookManifestError(ValueError):
    """The manifest is not a JSON object with a ``hooks`` object."""


def instance_interpreter(target: Path) -> str:
    """The absolute interpreter string the pin writes for ``target``: the literal join, as the installer builds it."""
    return str(target / INSTANCE_INTERPRETER)


def pin_hook_interpreter(raw: bytes, interpreter: str) -> bytes | None:
    """Return ``raw`` with every bare ``python3`` hook command bound to ``interpreter``, or ``None`` when none is bare.

    ``python3`` becomes ``interpreter``; ``python3 <args>`` becomes the
    shell-quoted ``interpreter`` followed by the unchanged ``<args>``; every
    other command is untouched.  A changed manifest is re-serialised as
    ``json.dumps(indent=2)`` plus a trailing newline -- the exact bytes the
    installer writes.  Raises :class:`HookManifestError` for bytes that are not
    a UTF-8 JSON object carrying a ``hooks`` object.
    """
    manifest = _manifest(raw)
    changed = False
    for hook in _hook_records(manifest):
        replacement = _absolute_python_command(cast(str, hook["command"]), interpreter)
        if replacement is not None:
            hook["command"] = replacement
            changed = True
    if not changed:
        return None
    return (json.dumps(manifest, indent=2) + "\n").encode("utf-8")


def hook_commands(raw: bytes) -> list[str]:
    """Every hook ``command`` string the manifest declares, in document order."""
    return [cast(str, hook["command"]) for hook in _hook_records(_manifest(raw))]


def _manifest(raw: bytes) -> dict[str, object]:
    try:
        value: object = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HookManifestError(f"hook manifest is not UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict) or not all(isinstance(key, str) for key in cast(dict[object, object], value)):
        raise HookManifestError("hook manifest is not a JSON object")
    manifest = cast(dict[str, object], value)
    if not isinstance(manifest.get("hooks"), dict):
        raise HookManifestError("hook manifest lacks the hooks object")
    return manifest


def _hook_records(manifest: dict[str, object]) -> list[_Hook]:
    records: list[_Hook] = []
    for event_entries in cast(dict[str, object], manifest["hooks"]).values():
        if not isinstance(event_entries, list):
            continue
        for entry in cast(list[object], event_entries):
            if not isinstance(entry, dict):
                continue
            hooks = cast(dict[str, object], entry).get("hooks")
            if not isinstance(hooks, list):
                continue
            for hook in cast(list[object], hooks):
                if isinstance(hook, dict) and isinstance(cast(_Hook, hook).get("command"), str):
                    records.append(cast(_Hook, hook))
    return records


def _absolute_python_command(command: str, interpreter: str) -> str | None:
    if command == "python3":
        return interpreter
    if command.startswith("python3 "):
        return shlex.quote(interpreter) + command[len("python3") :]
    return None
