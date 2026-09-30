"""The solet hydration block in ``AGENTS.md``/``CLAUDE.md``, shared by Genesis that writes it and the Manager that carries it.

``solet create`` renders ``hydration_templates/<file>.template`` and merges the delimited
``<!-- BEGIN SOLET HYDRATION -->`` ... ``<!-- END SOLET HYDRATION -->`` block into the seed's root
``AGENTS.md`` and ``CLAUDE.md``: replaced in place when the file already carries one, appended once
otherwise.  A release that changes either file then overlaps that local edit, and ``solet-manager update``
refused every created solet (iss_f89ab692).  The Manager carries the block across the update only when
the local file is exactly the committed file plus one block as this merge writes it, and it carries it
with this same merge, so the render and the merge live here, in the one stdlib-only package both sides
import, rather than as two copies that could drift.
"""

from __future__ import annotations

__all__ = [
    "AGENT_INSTRUCTION_FILES",
    "AGENT_TEMPLATE_ROOT",
    "HYDRATION_BEGIN",
    "HYDRATION_END",
    "agent_template_path",
    "carry_agent_block",
    "is_hydrated",
    "merge_agent_block",
    "render_agent_instructions",
]

#: The two tracked root files Genesis merges the block into, relative to the target root.
AGENT_INSTRUCTION_FILES: tuple[str, ...] = ("CLAUDE.md", "AGENTS.md")
#: Where the seed ships each file's template, relative to the target root.
AGENT_TEMPLATE_ROOT = "plugins/github_midwife_plugin/knowledge_base/hydration_templates"
HYDRATION_BEGIN = "<!-- BEGIN SOLET HYDRATION -->"
HYDRATION_END = "<!-- END SOLET HYDRATION -->"


def agent_template_path(runner_file: str) -> str:
    """The seed-relative template Genesis renders for ``runner_file``."""
    return f"{AGENT_TEMPLATE_ROOT}/{runner_file}.template"


def render_agent_instructions(template: str, *, name: str, clone_dir: str) -> str:
    """The template with the instance name and its clone directory substituted, exactly as Genesis renders it."""
    return template.replace("{{SOLET_NAME}}", name).replace("{{CLONE_DIR}}", clone_dir)


def merge_agent_block(existing: str, managed: str) -> str:
    """Put ``managed``'s delimited block into ``existing``: in place of an existing block, else appended once."""
    start = managed.find(HYDRATION_BEGIN)
    finish = managed.find(HYDRATION_END)
    block = managed[start : finish + len(HYDRATION_END)] if start >= 0 and finish >= start else managed.strip()
    old_start = existing.find(HYDRATION_BEGIN)
    old_end = existing.find(HYDRATION_END)
    if old_start >= 0 and old_end >= old_start:
        return existing[:old_start] + block + existing[old_end + len(HYDRATION_END) :]
    separator = "\n\n" if existing.strip() else ""
    return existing.rstrip() + separator + block + "\n"


def is_hydrated(committed: str, local: str) -> bool:
    """``local`` carries a delimited block and is exactly ``committed`` with that block merged in, nothing else changed."""
    start = local.find(HYDRATION_BEGIN)
    return start >= 0 and local.find(HYDRATION_END) >= start and merge_agent_block(committed, local) == local


def carry_agent_block(local: str, candidate: str) -> str | None:
    """``candidate`` with ``local``'s block merged in, or ``None`` when the result does not read back as exactly that.

    ``None`` means the candidate's text leaves the block no anchor (a stray or reversed marker): merging
    again would not be a no-op, so the result is not "the candidate plus the same block".
    """
    carried = merge_agent_block(candidate, local)
    return carried if is_hydrated(candidate, carried) else None
