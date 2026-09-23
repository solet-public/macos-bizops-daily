"""Plan graft transitions — splice, complete, renumber, inject headers.

Pure functions for manipulating plan text during WBS graft operations.
The graft transaction itself (reading WBS, resolving focused plan,
persisting results) remains on the thinking plugin; these helpers
handle the text transformations.
"""

from __future__ import annotations

import re

from ananta.core.plans.windowing import ACTIVE_WBS_HEADER_RE, WORK_MANIFEST_HEADER_RE


def splice_execution_tail(
    plan_text: str,
    anchor_step: int,
    graft_content: str,
) -> str:
    """Keep steps 1..anchor, drop future tail, append renumbered graft.

    The graft content may use its own 1-based numbering. This method
    renumbers all step headers in the graft to start at anchor_step + 1.
    """
    plan_lines = plan_text.split("\n")
    step_re = re.compile(r"^(\[.\])\s+(\d+)\.")

    cut_idx = len(plan_lines)
    anchor_found = False
    for i, line in enumerate(plan_lines):
        m = step_re.match(line.lstrip())
        if not m:
            continue
        step_num = int(m.group(2))
        if step_num == anchor_step:
            anchor_found = True
        elif anchor_found:
            cut_idx = i
            break

    prefix = "\n".join(plan_lines[:cut_idx]).rstrip()
    renumbered = renumber_steps(graft_content, start=anchor_step + 1)
    return f"{prefix}\n\n{renumbered}"


def complete_graft_step(plan_text: str) -> str:
    """Mark the current ``[>]`` step as ``[X]`` after a graft."""
    if "[>]" not in plan_text:
        return plan_text
    return plan_text.replace("[>]", "[X]", 1)


def ensure_active_marker(plan_text: str, anchor_step: int) -> str:
    """Ensure the plan has a ``[>]`` marker after a graft splice.

    Scans for the first ``[ ]`` step after the anchor and activates it.
    Returns unchanged if a ``[>]`` marker already exists.
    """
    if "[>]" in plan_text:
        return plan_text

    step_re = re.compile(r"^(\[[ \-]\])\s+(\d+)\.")
    lines = plan_text.split("\n")
    for i, line in enumerate(lines):
        m = step_re.match(line.lstrip())
        if not m:
            continue
        step_num = int(m.group(2))
        marker = m.group(1)
        if step_num > anchor_step and marker == "[ ]":
            lines[i] = lines[i].replace("[ ]", "[>]", 1)
            break

    return "\n".join(lines)


def inject_wbs_headers(
    plan_text: str,
    wbs_content: str,
    wbs_id: str,
    work_product_run_id: str | None = None,
) -> str:
    """Update WORK_MANIFEST, ACTIVE_WBS, and ACTIVE_WORK_PRODUCT_RUN headers.

    Extracts the manifest ID from the WBS document and sets headers
    so that ``build_plan_window`` auto-detects them. When
    ``work_product_run_id`` is provided, the shared run identity is
    also injected so multiple joseki fragments share artifact provenance.
    """
    from ananta.core.plans.windowing import ACTIVE_WORK_PRODUCT_RUN_RE

    manifest_match = WORK_MANIFEST_HEADER_RE.search(wbs_content)
    manifest_id = manifest_match.group(1) if manifest_match else None

    if ACTIVE_WBS_HEADER_RE.search(plan_text):
        plan_text = ACTIVE_WBS_HEADER_RE.sub(
            f"ACTIVE_WBS: {wbs_id}", plan_text,
        )
        if manifest_id and WORK_MANIFEST_HEADER_RE.search(plan_text):
            plan_text = WORK_MANIFEST_HEADER_RE.sub(
                f"WORK_MANIFEST: {manifest_id}", plan_text,
            )
        if work_product_run_id:
            if ACTIVE_WORK_PRODUCT_RUN_RE.search(plan_text):
                plan_text = ACTIVE_WORK_PRODUCT_RUN_RE.sub(
                    f"ACTIVE_WORK_PRODUCT_RUN: {work_product_run_id}",
                    plan_text,
                )
            else:
                # Insert after ACTIVE_WBS line
                plan_text = ACTIVE_WBS_HEADER_RE.sub(
                    f"ACTIVE_WBS: {wbs_id}\nACTIVE_WORK_PRODUCT_RUN: {work_product_run_id}",
                    plan_text,
                )
        return plan_text

    header_lines: list[str] = []
    if manifest_id:
        header_lines.append(f"WORK_MANIFEST: {manifest_id}")
    header_lines.append(f"ACTIVE_WBS: {wbs_id}")
    if work_product_run_id:
        header_lines.append(f"ACTIVE_WORK_PRODUCT_RUN: {work_product_run_id}")

    return "\n".join(header_lines) + "\n" + plan_text


def renumber_steps(text: str, start: int) -> str:
    """Renumber all step headers in *text* sequentially from *start*."""
    step_re = re.compile(r"^(\[.\])\s+(\d+)\.")
    lines = text.split("\n")
    counter = start
    result: list[str] = []
    for line in lines:
        m = step_re.match(line.lstrip())
        if m:
            indent = line[: len(line) - len(line.lstrip())]
            line = indent + step_re.sub(
                f"{m.group(1)} {counter}.", line.lstrip(), count=1,
            )
            counter += 1
        result.append(line)
    return "\n".join(result)
