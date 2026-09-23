"""Plan store adapter — knowledge base and focus-buffer IO for plans.

Separates plan persistence from plan logic in the thinking plugin.
The adapter holds lazy references to memory/knowledge/state services
and exposes the plan CRUD API that the plugin delegates to.
"""

from __future__ import annotations

import datetime
import logging
import re
from typing import Any, Protocol

from ananta.core.plans import (
    normalize_for_new_plan_install,
    parse,
    preserve_existing_markers,
)
from ananta.core.plans.windowing import build_plan_window
from ananta.error_handling import FrameworkError

logger = logging.getLogger(__name__)

KB_THINKING_PLANS = "thinking_plans"


# ── Protocols for service dependencies ─────────────────────────────


class MemoryServiceLike(Protocol):
    """Narrow protocol for the memory operations PlanStore needs.

    Satisfied by ``SessionScopedMemory`` (JOS-02): the view is bound to ONE
    session, so every focus operation below is session-scoped and
    ``session_id`` names the binding (the per-session cursor guards key on
    it).
    """

    session_id: str

    def get_focused(self) -> dict[str, Any]: ...
    def remember(self, *, content: str, tags: list[str], embed: bool) -> dict[str, Any]: ...
    def focus(self, memory_id: str) -> None: ...
    def unfocus(self, memory_id: str) -> None: ...
    def forget(self, memory_id: str) -> None: ...


class KnowledgeServiceLike(Protocol):
    """Narrow protocol for the knowledge service operations PlanStore needs."""

    def create_file(self, *, name: str, path: str, content: str) -> dict[str, Any]: ...
    def edit_file(self, *, name: str, path: str, content: str) -> dict[str, Any]: ...
    def read_file(self, *, name: str, path: str) -> dict[str, Any]: ...


class StateServiceLike(Protocol):
    """Narrow protocol for the state service operations PlanStore needs."""

    def generate_id(self, *, prefix: str) -> str: ...


# ── Pure helpers (no service dependencies) ─────────────────────────


def format_plan_for_knowledge_base(plan_id: str, plan_content: str) -> str:
    """Format plan content with markdown headers for knowledge base chunking."""
    lines = plan_content.split("\n")
    result_lines: list[str] = [f"# Plan {plan_id}", ""]
    for line in lines:
        step_match = re.match(r"^\[.\]\s+(\d+)\.", line)
        if step_match:
            step_num = step_match.group(1)
            result_lines.append(f"## Step {step_num}")
            result_lines.append("")
        result_lines.append(line)
    return "\n".join(result_lines)


def strip_kb_formatting(content: str) -> str:
    """Strip knowledge base formatting headers from plan content."""
    lines = content.split("\n")
    result: list[str] = []
    skip_next_blank = False
    for line in lines:
        if line.startswith("# Plan "):
            skip_next_blank = True
            continue
        if re.match(r"^## Step \d+$", line):
            skip_next_blank = True
            continue
        if skip_next_blank and not line.strip():
            skip_next_blank = False
            continue
        skip_next_blank = False
        result.append(line)
    return "\n".join(result).strip()


def extract_kb_path(plan_item: dict[str, Any]) -> str | None:
    """Extract kb_path from a focused plan item's tags."""
    tags: list[str] = plan_item.get("tags", [])
    for tag in tags:
        if tag.startswith("kb_path:"):
            return tag[len("kb_path:"):]
    return None


def set_kb_path_tag(tags: list[str], kb_path: str) -> list[str]:
    """Return tags with the kb_path tag set (replacing any existing)."""
    updated = [t for t in tags if not t.startswith("kb_path:")]
    updated.append(f"kb_path:{kb_path}")
    return updated


def extract_plan_id(plan_item: dict[str, Any]) -> str:
    """Extract plan_id from a focused plan item's tags."""
    tags: list[str] = plan_item.get("tags", [])
    for tag in tags:
        if tag.startswith("plan:pln-"):
            return tag[len("plan:"):]
    return ""


def plan_cursor(plan_id: str, parsed_plan: Any) -> str:
    """Build a cursor string for the plan advancement lineage guard."""
    current = parsed_plan.current_step_number
    if current is None:
        current = parsed_plan.first_executable_step_number
    return f"{plan_id}:{current}"


def count_plan_steps(content: str) -> int:
    """Count the number of ## Step headers in plan content."""
    return len(re.findall(r"^## Step \d+", content, re.MULTILINE))


def flush_step_summary(
    steps: list[str],
    num: str,
    desc: str,
    process: str,
    depends: str,
) -> None:
    """Flush a pending step into the summary list."""
    if num and desc and process:
        line = f"{num}. {desc} — PROCESS: {process}"
        if depends and depends.lower() != "none":
            line += f", DEPENDS: {depends}"
        steps.append(line)


def generate_plan_summary(plan_content: str, step_count: int) -> str:
    """Generate a compact plan summary for the focused memory.

    Extracts title and step metadata from the structured ``## Step N``
    plan document.  The summary starts with ``ACTIVE_PLAN:`` for plan
    continuation detection (``has_focused_plan``).
    """
    lines = plan_content.splitlines()

    title = "Untitled Plan"
    for line in lines:
        if line.startswith("# Plan: "):
            title = line[len("# Plan: "):].strip()
            break

    steps: list[str] = []
    current_num = ""
    current_desc = ""
    current_process = ""
    current_depends = ""

    for line in lines:
        step_match = re.match(r"^## Step (\d+): (.+)", line)
        if step_match:
            flush_step_summary(
                steps, current_num, current_desc, current_process, current_depends,
            )
            current_num = step_match.group(1)
            current_desc = step_match.group(2)
            current_process = ""
            current_depends = ""
        elif line.startswith("PROCESS: ") and current_num:
            current_process = line[len("PROCESS: "):].strip()
        elif line.startswith("DEPENDS: ") and current_num:
            current_depends = line[len("DEPENDS: "):].strip()

    flush_step_summary(
        steps, current_num, current_desc, current_process, current_depends,
    )

    return "\n".join([
        f"ACTIVE_PLAN: {title}",
        f"PROGRESS: 0/{step_count}",
        "",
        *steps,
    ])


# ── PlanStore adapter class ───────────────────────────────────────


class PlanStore:
    """Plan persistence adapter for the thinking plugin.

    Holds lazy service references and exposes plan CRUD operations.
    The plugin creates this after ``prepare_for_readiness()`` when
    services are available.
    """

    def __init__(
        self,
        *,
        get_memory_service: Any,
        get_knowledge_service: Any,
        state_service: StateServiceLike,
        cursor_holder: Any,
    ) -> None:
        self._get_memory_service = get_memory_service
        self._get_knowledge_service = get_knowledge_service
        self._state_service = state_service
        self._cursor_holder = cursor_holder

    # ── Knowledge base operations ──────────────────────────────────

    def write_plan_to_knowledge_base(
        self, plan_id: str, plan_content: str,
    ) -> str | None:
        """Write or update plan document in the thinking_plans knowledge base."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            logger.warning("Knowledge service unavailable — plan not written")
            return None

        date_str = datetime.date.today().isoformat()
        path = f"plans/{date_str}/{plan_id}.md"
        formatted = format_plan_for_knowledge_base(plan_id, plan_content)

        try:
            knowledge_service.create_file(
                name=KB_THINKING_PLANS, path=path, content=formatted,
            )
            logger.info("Plan created in knowledge base: thinking_plans/%s", path)
            return path
        except FileNotFoundError:
            logger.warning("thinking_plans knowledge base not installed — skipping")
            return None
        except FileExistsError:
            return self._update_plan_in_knowledge_base(knowledge_service, path, formatted)
        except Exception:
            logger.exception("Failed to write plan to knowledge base: %s", path)
            return None

    def _update_plan_in_knowledge_base(
        self,
        knowledge_service: Any,
        path: str,
        plan_content: str,
    ) -> str | None:
        """Overwrite an existing plan file in the knowledge base."""
        try:
            knowledge_service.edit_file(
                name=KB_THINKING_PLANS, path=path, content=plan_content,
            )
            logger.info("Plan updated in knowledge base: thinking_plans/%s", path)
            return path
        except Exception:
            logger.exception("Failed to update plan in knowledge base: %s", path)
            return None

    def read_plan_from_kb(self, kb_path: str) -> str | None:
        """Read full plan content from the knowledge base."""
        knowledge_service = self._get_knowledge_service()
        if knowledge_service is None:
            return None
        try:
            result = knowledge_service.read_file(
                name=KB_THINKING_PLANS, path=kb_path,
            )
            raw_content: str = result.get("content", "")
            return strip_kb_formatting(raw_content)
        except Exception:
            logger.exception("Failed to read plan from KB: %s", kb_path)
            return None

    # ── Focus buffer operations ────────────────────────────────────

    def find_focused_plan(
        self, memory_service: Any,
    ) -> dict[str, Any] | None:
        """Find the current focused plan in the focus buffer."""
        focused_items: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        for item in focused_items:
            if "plan" in item.get("tags", []):
                return item
        return None

    def focus_plan(self, memory_id: str, memory_service: Any) -> bool:
        """Unfocus existing plans and focus the new one."""
        if not memory_id:
            return False

        focused_items: list[dict[str, Any]] = memory_service.get_focused()["memories"]
        for item in focused_items:
            item_tags: list[str] = item.get("tags", [])
            if "plan" in item_tags:
                item_mid = item.get("memory_id", "")
                self.safe_unfocus(item_mid, memory_service)
                logger.info("Unfocused previous plan %s", item_mid)

        try:
            memory_service.focus(memory_id)
            return True
        except Exception:
            logger.warning("Failed to focus plan %s — buffer full", memory_id)
            return False

    def safe_unfocus(self, memory_id: str, memory_service: Any) -> bool:
        """Unfocus a memory item, returning False if it wasn't focused."""
        if not memory_id:
            return False
        try:
            memory_service.unfocus(memory_id)
            return True
        except FrameworkError:
            # memory.not_focused — item wasn't in the focus buffer.
            return False

    # ── Plan CRUD orchestration ────────────────────────────────────

    def upsert_into_existing(
        self,
        plan_text: str,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Handle upsert when a focused plan already exists."""
        existing_content: str = existing_plan.get("content", "")
        existing_parsed = parse(existing_content)
        parsed_submitted = parse(plan_text)

        current_step = existing_parsed.current_step_number
        if current_step is None:
            current_step = existing_parsed.first_executable_step_number

        if current_step is not None:
            submitted_step_numbers = {
                s.number for s in parsed_submitted.steps
            }
            if current_step not in submitted_step_numbers:
                return self.replace_plan(
                    parsed_submitted, existing_plan, memory_service,
                )

        plan_text = preserve_existing_markers(existing_parsed, parsed_submitted)

        return self.upsert_existing_plan(
            plan_text, existing_plan, memory_service,
        )

    def replace_plan(
        self,
        parsed_submitted: Any,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Replace the existing focused plan with a new numbering space."""
        old_memory_id = existing_plan.get("memory_id", "")
        if old_memory_id:
            self.safe_unfocus(old_memory_id, memory_service)
            memory_service.forget(old_memory_id)
        plan_text = normalize_for_new_plan_install(parsed_submitted)
        return self.upsert_new_plan(plan_text, memory_service)

    def install_first_plan(
        self, plan_text: str, memory_service: Any,
    ) -> dict[str, Any]:
        """Install the very first plan (no existing focused plan)."""
        parsed_submitted = parse(plan_text)
        if parsed_submitted.first_executable_step_number is not None:
            plan_text = normalize_for_new_plan_install(parsed_submitted)
        result = self.upsert_new_plan(plan_text, memory_service)
        plan_id: str = result.get("plan_id", "")
        if plan_id:
            # Per-session cursor guard (JOS-02): the view carries the session.
            self._cursor_holder.set_presented_plan_cursor(
                memory_service.session_id, plan_cursor(plan_id, parse(plan_text)),
            )
        return result

    def upsert_existing_plan(
        self,
        plan_text: str,
        existing_plan: dict[str, Any],
        memory_service: Any,
    ) -> dict[str, Any]:
        """Update an existing focused plan: write to KB, window into memory."""
        old_memory_id: str = existing_plan.get("memory_id", "")
        old_tags: list[str] = existing_plan.get("tags", ["plan"])
        plan_id = extract_plan_id(existing_plan)

        logger.info(
            "UPSERT_PLAN: plan_id=%s, text_len=%d, steps=%s",
            plan_id, len(plan_text),
            " ".join(
                f"[{ln.strip()[1]}]{ln.strip().split('.')[0][3:].strip()}"
                for ln in plan_text.split("\n")
                if ln.strip().startswith("[")
            )[:200],
        )
        kb_path: str | None = None
        if plan_id:
            kb_path = self.write_plan_to_knowledge_base(plan_id, plan_text)

        if parse(plan_text).is_complete:
            return self.complete_plan(
                plan_id, old_memory_id, memory_service,
            )

        plan_ref = plan_id if plan_id else None
        window_text = build_plan_window(plan_text, plan_ref=plan_ref)

        tags = list(old_tags)
        if kb_path:
            tags = set_kb_path_tag(tags, kb_path)

        if old_memory_id:
            memory_service.unfocus(old_memory_id)
            memory_service.forget(old_memory_id)

        remember_result: dict[str, Any] = memory_service.remember(
            content=window_text, tags=tags, embed=False,
        )
        memory_id: str = remember_result.get("memory_id", "")
        focused = self.focus_plan(memory_id, memory_service)

        return {
            "plan_id": plan_id,
            "memory_id": memory_id,
            "focused": focused,
        }

    def complete_plan(
        self,
        plan_id: str,
        memory_id: str,
        memory_service: Any,
    ) -> dict[str, Any]:
        """Clear focused memory for a completed plan."""
        if memory_id:
            self.safe_unfocus(memory_id, memory_service)
            memory_service.forget(memory_id)
        self._cursor_holder.set_presented_plan_cursor(memory_service.session_id, "")
        logger.info("Plan %s complete — cleared from focused memory", plan_id)
        return {
            "plan_id": plan_id,
            "memory_id": "",
            "focused": False,
            "status": "completed",
        }

    def upsert_new_plan(
        self,
        plan_text: str,
        memory_service: Any,
    ) -> dict[str, Any]:
        """Create a new plan: write to KB, window into memory."""
        plan_id = self._state_service.generate_id(prefix="pln-")

        kb_path = self.write_plan_to_knowledge_base(plan_id, plan_text)
        plan_ref = plan_id

        window_text = build_plan_window(plan_text, plan_ref=plan_ref)

        tags = ["plan", f"plan:{plan_id}"]
        if kb_path:
            tags = set_kb_path_tag(tags, kb_path)

        remember_result: dict[str, Any] = memory_service.remember(
            content=window_text, tags=tags, embed=False,
        )
        memory_id: str = remember_result.get("memory_id", "")
        focused = self.focus_plan(memory_id, memory_service)

        return {
            "plan_id": plan_id,
            "memory_id": memory_id,
            "focused": focused,
        }
