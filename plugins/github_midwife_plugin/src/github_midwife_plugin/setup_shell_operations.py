"""Managed shell hydration operations."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .managed_render import (
    TEMPLATE_ROOT_REF,
    append_block,
    block_text,
    find_blocks,
    marker_lines,
    replace_block,
    template_digest,
)
from .setup_adapter_contract import AdapterRequest, JsonObject, planned_action, result
from .setup_adapter_runtime import Runtime
from .setup_operations import _TEMPLATES, _verified, read_file

_ZSHRC_BLOCK_TEMPLATE = "zshrc_block.template"
_ZSHRC_MARKER_BEGIN = "# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}"
_ZSHRC_MARKER_END = "# END SOLET {NAME}"
_STAMP = "# rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST}"
_STAMPED_SHELL_TEMPLATES = (
    "solet.zsh.template",
    "claude_launcher.template",
    "codex_launcher.template",
    "launch.template",
    "fleet_functions.zsh.template",
)


def shell(request: AdapterRequest, runtime: Runtime) -> JsonObject:
    desired = _rendered_files(request)
    zshrc = runtime.home / ".zshrc"
    block = _zshrc_block(request)
    changed_paths = [
        path for path, (content, _mode) in desired.items() if read_file(path) != content
    ]
    zshrc_changed = _merge_block(read_file(zshrc), block, request.name) != read_file(zshrc)
    if request.phase == "probe":
        return _shell_preview(request, runtime, zshrc, changed_paths, zshrc_changed)
    return _apply_shell(request, runtime, desired, zshrc, block)


def _shell_preview(
    request: AdapterRequest,
    runtime: Runtime,
    zshrc: Path,
    changed_paths: list[Path],
    zshrc_changed: bool,
) -> JsonObject:
    actions = [
        planned_action(
            action_id=f"shell.write.{index}",
            title=f"Write managed hydration artifact {path.name}",
            mutation_kind="file_write",
            target=str(path),
            evidence_ref="managed_artifact_drift",
        )
        for index, path in enumerate(changed_paths, start=1)
    ]
    if zshrc_changed:
        actions.extend(_zshrc_actions(request, runtime, zshrc))
    if not actions:
        return _verified(
            request, "shell_integration", "managed shell artifacts are current", str(zshrc)
        )
    return result(
        request, status="pending", actions=actions, repair="Approve the exact managed files shown."
    )


def _zshrc_actions(request: AdapterRequest, runtime: Runtime, zshrc: Path) -> list[JsonObject]:
    actions = []
    if zshrc.is_file():
        actions.append(
            planned_action(
                action_id="shell.backup_zshrc",
                title="Back up the existing zsh startup file",
                mutation_kind="file_backup",
                target=str(_backup_path(runtime.home, request.name)),
                evidence_ref="zshrc_requires_managed_block",
            )
        )
    actions.append(
        planned_action(
            action_id="shell.merge_zshrc_block",
            title="Merge the reviewed solet source block into .zshrc",
            mutation_kind="file_write",
            target=str(zshrc),
            evidence_ref="zshrc_requires_managed_block",
        )
    )
    return actions


def _apply_shell(
    request: AdapterRequest,
    runtime: Runtime,
    desired: dict[Path, tuple[str, int]],
    zshrc: Path,
    block: str,
) -> JsonObject:
    for path, (content, mode) in desired.items():
        if read_file(path) != content:
            runtime.atomic_write(path, content, mode=mode)
    current = read_file(zshrc)
    merged = _merge_block(current, block, request.name)
    if merged != current:
        if zshrc.is_file():
            backup = _backup_path(runtime.home, request.name)
            runtime.atomic_write(backup, current, mode=0o600)
        runtime.atomic_write(zshrc, merged, mode=0o644)
    return result(request, status="applied", retry_safe=True)


def _rendered_files(request: AdapterRequest) -> dict[Path, tuple[str, int]]:
    marketplace = request.name.replace("_", "-")
    values = {
        "{{SOLET_NAME}}": request.name,
        "{{CLONE_DIR}}": str(request.target),
        "{{HYDRATION_DATE}}": datetime.now(UTC).date().isoformat(),
        "{{BACKUP_PATH}}": "managed-by-executable-hydration",
        "{{MARKETPLACE_NAME}}": marketplace,
        "{{GIT_CONTROLLER_NAME}}": str(request.public_inputs.get("git_controller_name", "")),
    }
    shell_values = {**values, "{{CLONE_DIR_ZSH}}": _zsh_quote(str(request.target))}
    mapping = {
        "solet.zsh.template": (request.target / "client" / f"{request.name}.zsh", 0o644),
        "claude_launcher.template": (
            request.target / "client/bin" / f"claude-{request.name}",
            0o755,
        ),
        "codex_launcher.template": (request.target / "client/bin" / f"codex-{request.name}", 0o755),
        "launch.template": (request.target / "client/bin" / f"launch-{request.name}", 0o755),
        "claude_session_overlay.json.template": (
            request.target / "client/claude-session-overlay.json",
            0o644,
        ),
        "marketplace_json.template": (request.target / ".claude-plugin/marketplace.json", 0o644),
        "codex_marketplace_json.template": (
            request.target / ".agents/plugins/marketplace.json",
            0o644,
        ),
    }
    if values["{{GIT_CONTROLLER_NAME}}"]:
        mapping["fleet_functions.zsh.template"] = (
            request.target / "client" / f"{request.name}-fleet.zsh",
            0o644,
        )
    rendered: dict[Path, tuple[str, int]] = {}
    for template_name, (destination, mode) in mapping.items():
        if template_name.endswith(".json.template"):
            content = _render_json(_TEMPLATES / template_name, values)
        else:
            content = _render(_TEMPLATES / template_name, shell_values)
            if not values["{{GIT_CONTROLLER_NAME}}"]:
                content = "\n".join(
                    line for line in content.splitlines() if "GIT_CONTROLLER_NAME=" not in line
                ) + "\n"
            if template_name in _STAMPED_SHELL_TEMPLATES:
                content = _stamped(content, template_name)
        rendered[destination] = (content, mode)
    for runner_file in ("CLAUDE.md", "AGENTS.md"):
        destination = request.target / runner_file
        managed = _render(_TEMPLATES / f"{runner_file}.template", values)
        rendered[destination] = (_merge_agent_block(read_file(destination), managed), 0o644)
    return rendered


def _render(template: Path, values: dict[str, str]) -> str:
    content = template.read_text(encoding="utf-8")
    for token, value in values.items():
        content = content.replace(token, value)
    return content


def _render_json(template: Path, values: dict[str, str]) -> str:
    """Render JSON templates as data so substitution cannot corrupt JSON syntax."""

    def replace_tokens(value: object) -> object:
        if isinstance(value, str):
            for token, replacement in values.items():
                value = value.replace(token, replacement)
            return value
        if isinstance(value, list):
            return [replace_tokens(item) for item in value]
        if isinstance(value, dict):
            return {key: replace_tokens(item) for key, item in value.items()}
        return value

    parsed = json.loads(template.read_text(encoding="utf-8"))
    return json.dumps(replace_tokens(parsed), indent=2) + "\n"


def _zsh_quote(value: str) -> str:
    """Return one zsh word without permitting expansion or command substitution."""

    return "'" + value.replace("'", "'\"'\"'") + "'"


def _merge_agent_block(existing: str, managed: str) -> str:
    begin = "<!-- BEGIN SOLET HYDRATION -->"
    end = "<!-- END SOLET HYDRATION -->"
    start = managed.find(begin)
    finish = managed.find(end)
    block = (
        managed[start : finish + len(end)] if start >= 0 and finish >= start else managed.strip()
    )
    old_start = existing.find(begin)
    old_end = existing.find(end)
    if old_start >= 0 and old_end >= old_start:
        return existing[:old_start] + block + existing[old_end + len(end) :]
    separator = "\n\n" if existing.strip() else ""
    return existing.rstrip() + separator + block + "\n"


def _stamped(content: str, template_name: str) -> str:
    """Insert the ``rendered-from`` stamp after the shebang (or at the top) of a shell render."""
    stamp = _STAMP.replace("{TEMPLATE_REF}", f"{TEMPLATE_ROOT_REF}/{template_name}").replace(
        "{TEMPLATE_DIGEST}", template_digest(_TEMPLATES / template_name)
    )
    lines = content.split("\n")
    lines.insert(1 if lines and lines[0].startswith("#!") else 0, stamp)
    return "\n".join(lines)


def _zshrc_block(request: AdapterRequest) -> str:
    """The versioned managed block: ``# BEGIN SOLET <name> v<digest8>`` … ``# END SOLET <name>``.

    The body renders from ``zshrc_block.template`` so its digest is what the
    marker carries; the body is byte-identical to the block genesis wrote
    before versioning, which is what lets an existing-install update recognise
    a legacy unversioned block as its own previous render.
    """
    template = _TEMPLATES / _ZSHRC_BLOCK_TEMPLATE
    shell_path = _zsh_quote(str(request.target / "client" / f"{request.name}.zsh"))
    body = template.read_text(encoding="utf-8").replace("{{SHELL_FILE_ZSH}}", shell_path)
    begin, end = marker_lines(_ZSHRC_MARKER_BEGIN, _ZSHRC_MARKER_END, request.name, template_digest(template))
    return block_text(begin, body, end)


def _merge_block(existing: str, block: str, name: str) -> str:
    """Replace this name's block in place (versioned or legacy marker) or append it once."""
    blocks = find_blocks(existing, _ZSHRC_MARKER_BEGIN, _ZSHRC_MARKER_END, name)
    if blocks:
        return replace_block(existing, blocks[0], block)
    return append_block(existing, block)


def _backup_path(home: Path, name: str) -> Path:
    stem = home / f".zshrc.pre-{name}-hydration-{datetime.now(UTC).strftime('%Y%m%d')}"
    candidate = stem
    index = 2
    while candidate.exists():
        candidate = Path(f"{stem}-{index}")
        index += 1
    return candidate
