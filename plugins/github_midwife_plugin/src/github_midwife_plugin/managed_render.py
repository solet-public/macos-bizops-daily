"""Versioned managed-block markers and ``rendered-from`` stamps for hydration renders.

Every managed artifact a release ships is content-addressed by the digest of
its template (existing-install design section 6.1): a managed block opens
with ``… SOLET <name> v<digest8>`` and a whole-file render carries one
``rendered-from: <template_ref>@<digest>`` line.  Genesis and the
existing-install adapters both render through these helpers, so every new
render is stamped and a later release can tell "rendered from the previous
template" from "edited by the operator" without guessing.  Legacy renders
(unversioned ``# BEGIN SOLET <name>`` markers, unstamped files) are recognised
as such, never rewritten silently.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[2] / "knowledge_base" / "hydration_templates"
TEMPLATE_ROOT_REF = "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
_DIGEST8 = re.compile(r"[0-9a-f]{8}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class BlockMatch:
    """One marker-delimited block found in an operator-owned file."""

    start: int
    end: int
    begin_line: str
    stamped_digest8: str | None
    body: str

    @property
    def stamped(self) -> bool:
        return self.stamped_digest8 is not None


def sha256_text(content: str) -> str:
    return f"sha256:{hashlib.sha256(content.encode('utf-8')).hexdigest()}"


def sha256_bytes(content: bytes) -> str:
    return f"sha256:{hashlib.sha256(content).hexdigest()}"


def template_digest(template_path: Path) -> str:
    return sha256_bytes(template_path.read_bytes())


def render_tokens(template_text: str, values: dict[str, str]) -> str:
    """Literal ``{{TOKEN}}`` replacement, never ``str.format`` (templates carry live ``$VAR``)."""
    rendered = template_text
    for token, value in values.items():
        rendered = rendered.replace(token, value)
    return rendered


def zsh_quote(value: str) -> str:
    """One zsh word that permits no expansion or command substitution."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def stamp_line(stamp_template: str, template_ref: str, digest: str) -> str:
    return stamp_template.replace("{TEMPLATE_REF}", template_ref).replace("{TEMPLATE_DIGEST}", digest)


def marker_lines(begin_template: str, end_template: str, name: str, digest: str) -> tuple[str, str]:
    """The versioned begin line and the named end line for one render."""
    digest8 = digest.removeprefix("sha256:")[:8]
    begin = begin_template.replace("{NAME}", name).replace("{TEMPLATE_DIGEST8}", digest8)
    end = end_template.replace("{NAME}", name)
    return begin, end


def _split_begin(begin_template: str, name: str) -> tuple[str, str]:
    """``(head, suffix)``: the text before `` v{TEMPLATE_DIGEST8}`` and the text after it."""
    with_name = begin_template.replace("{NAME}", name)
    marker = " v{TEMPLATE_DIGEST8}"
    index = with_name.index(marker)
    return with_name[:index], with_name[index + len(marker) :]


def find_blocks(existing: str, begin_template: str, end_template: str, name: str) -> list[BlockMatch]:
    """Every block whose begin line is this name's marker (versioned or legacy), in file order."""
    head, suffix = _split_begin(begin_template, name)
    end_line = end_template.replace("{NAME}", name)
    lines = existing.split("\n")
    matches: list[BlockMatch] = []
    offset = 0
    index = 0
    while index < len(lines):
        line = lines[index]
        begin_at = offset
        if _is_begin_line(line, head, suffix):
            digest8 = _stamped_digest8(line, head, suffix)
            end_index = next((j for j in range(index + 1, len(lines)) if lines[j] == end_line), None)
            if end_index is None:
                matches.append(BlockMatch(begin_at, len(existing), line, digest8, "\n".join(lines[index + 1 :])))
                break
            body = "\n".join(lines[index + 1 : end_index])
            end_at = begin_at + len("\n".join(lines[index : end_index + 1]))
            # Include the trailing newline after the end marker when present.
            if end_at < len(existing) and existing[end_at] == "\n":
                end_at += 1
            matches.append(BlockMatch(begin_at, end_at, line, digest8, body))
            offset = end_at
            index = end_index + 1
            continue
        offset += len(line) + 1
        index += 1
    return matches


def _is_begin_line(line: str, head: str, suffix: str) -> bool:
    if not line.startswith(head):
        return False
    rest = line[len(head) :]
    if rest == "" or rest == suffix.rstrip() or rest == suffix:
        return True  # legacy unversioned marker (`# BEGIN SOLET name`, `<!-- BEGIN SOLET name -->`)
    return rest.startswith(" v") and rest.endswith(suffix)


def _stamped_digest8(line: str, head: str, suffix: str) -> str | None:
    rest = line[len(head) :]
    if not rest.startswith(" v"):
        return None
    version = rest[2 : len(rest) - len(suffix)] if suffix else rest[2:]
    return version if _DIGEST8.fullmatch(version) else None


def block_text(begin_line: str, body: str, end_line: str) -> str:
    """The exact bytes of one rendered block: begin, body, end, each newline-terminated."""
    normalised = body if body.endswith("\n") else body + "\n"
    return f"{begin_line}\n{normalised}{end_line}\n"


def replace_block(existing: str, match: BlockMatch, new_block: str) -> str:
    return existing[: match.start] + new_block + existing[match.end :]


def append_block(existing: str, new_block: str) -> str:
    separator = "\n" if not existing or existing.endswith("\n") else "\n\n"
    return existing + separator + new_block if existing else new_block


def strip_marker_lines(rendered: str, begin_template: str, end_template: str, name: str) -> str:
    """Drop a template's own legacy marker lines so the body renders under the versioned marker."""
    head, suffix = _split_begin(begin_template, name)
    end_line = end_template.replace("{NAME}", name)
    lines = rendered.split("\n")
    while lines and _is_begin_line(lines[0], head, suffix):
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()
    while lines and lines[-1] == end_line:
        lines.pop()
        while lines and lines[-1] == "":
            lines.pop()
    return "\n".join(lines) + "\n"


def stamped_digest(rendered: str, stamp_template: str, template_ref: str) -> str | None:
    """The digest a whole-file render carries in its stamp line, or ``None`` when unstamped."""
    prefix = stamp_template.replace("{TEMPLATE_REF}", template_ref).split("{TEMPLATE_DIGEST}")[0]
    for line in rendered.split("\n"):
        if line.startswith(prefix):
            found = _DIGEST.search(line)
            return found.group(0) if found else None
    return None


def insert_stamp(rendered: str, stamp: str, *, after_line: int) -> str:
    """Insert one stamp line after ``after_line`` lines (0 = at the top)."""
    lines = rendered.split("\n")
    lines.insert(after_line, stamp)
    return "\n".join(lines)


def remove_stamp(rendered: str, stamp_template: str, template_ref: str) -> str:
    prefix = stamp_template.replace("{TEMPLATE_REF}", template_ref).split("{TEMPLATE_DIGEST}")[0]
    return "\n".join(line for line in rendered.split("\n") if not line.startswith(prefix))


__all__ = [
    "TEMPLATES",
    "TEMPLATE_ROOT_REF",
    "BlockMatch",
    "append_block",
    "block_text",
    "find_blocks",
    "insert_stamp",
    "marker_lines",
    "remove_stamp",
    "render_tokens",
    "replace_block",
    "sha256_bytes",
    "sha256_text",
    "stamp_line",
    "stamped_digest",
    "strip_marker_lines",
    "template_digest",
    "zsh_quote",
]
