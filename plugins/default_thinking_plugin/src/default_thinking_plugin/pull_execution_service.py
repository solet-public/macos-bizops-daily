"""Pull-mode WBS execution service (Phase 4, Seam C — plugin side).

Thin service over the pure engine in
:mod:`ananta.core.plans.pull_execution`, wiring it to the durable
substrates: the WBS document (with its ``<!-- Step N: … -->`` completion
annotations) in the thinking-plans knowledge base, the ``thinking_wbs``
tracking row, and the work-product register. Holds NO session state —
disconnect/resume safety comes entirely from those durable stores.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ananta.core.plans import pull_execution
from ananta.core.plans.wbs_lifecycle import record_step_state
from ananta.error_handling import FrameworkError

from default_thinking_plugin.constants import ErrorCode

if TYPE_CHECKING:
    from ananta.core.plans.wbs_lifecycle import (
        WbsFocusManager,
        WbsKnowledgeStore,
        WbsStateService,
        WorkProductStateService,
    )

_EXECUTABLE_STATUSES = frozenset({"drafted", "ready", "in_progress", "paused"})


class PullExecutionService:
    """start / get_next / record_observation / advance for pull mode."""

    def __init__(
        self,
        *,
        state_service: WbsStateService,
        work_product_state_service: WorkProductStateService,
        knowledge_store: WbsKnowledgeStore,
        focus_manager: WbsFocusManager,
        namespace: str,
    ) -> None:
        self._state_service = state_service
        self._work_product_state_service = work_product_state_service
        self._knowledge_store = knowledge_store
        self._focus_manager = focus_manager
        self._namespace = namespace

    # ── Verb bodies ─────────────────────────────────────────────────

    def start_wbs_execution(self, wbs_id: str) -> dict[str, Any]:
        """Start or RESUME a pull-mode execution session (idempotent).

        Marks the tracking row ``in_progress``, ensures the work-product
        register exists (idempotent; carries prior-phase products), and
        returns the current position. Calling it again after a driver
        disconnect is the resume path — nothing session-scoped exists to
        lose.
        """
        record, content = self._load(wbs_id)
        status = str(record.get("status", "drafted"))
        if status not in _EXECUTABLE_STATUSES:
            raise FrameworkError(
                message=(
                    f"WBS {wbs_id!r} has status {status!r} — pull execution "
                    f"requires one of {sorted(_EXECUTABLE_STATUSES)}"
                ),
                error_code=ErrorCode.PARAMETER_ERROR,
            )

        from ananta.core.plans.wbs_lifecycle import (
            initialize_work_product_register,
        )

        initialize_work_product_register(
            wbs_id, self._work_product_state_service,
        )
        if status != "in_progress":
            self._set_wbs_status(wbs_id, "in_progress")

        envelope = pull_execution.next_step_envelope(wbs_id, content)
        executed = pull_execution.executed_step_numbers(
            content, _parse(content),
        )
        # ``resumed`` means a DRIVER already recorded observations — the
        # durable annotations. Pre-authored ``[-]``/``[X]`` markers count
        # toward the resume POSITION (executed_step_numbers) but do not
        # make a fresh start a resume.
        from ananta.core.plans.projection import parse_completed_step_numbers

        return {
            "wbs_id": wbs_id,
            "status": "in_progress",
            "resumed": bool(parse_completed_step_numbers(content)),
            "executed_step_numbers": sorted(executed),
            "next": envelope.to_payload(),
        }

    def get_next_wbs_step(self, wbs_id: str) -> dict[str, Any]:
        """Return the envelope for the next unexecuted step."""
        _, content = self._load(wbs_id)
        return pull_execution.next_step_envelope(wbs_id, content).to_payload()

    def record_wbs_step_observation(
        self,
        wbs_id: str,
        step_number: int,
        process_key: str,
        result: dict[str, Any],
        state_summary: str | None = None,
        output_artifacts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Validate an observation; record + advance ONLY when valid.

        An invalid observation returns ``accepted=False`` with the full
        error list and changes NOTHING — the durable record is untouched
        and the next envelope is unchanged.
        """
        _, content = self._load(wbs_id)
        errors = pull_execution.validate_observation(
            wbs_id, content, step_number, process_key, result,
        )
        if errors:
            return {
                "wbs_id": wbs_id,
                "step_number": step_number,
                "accepted": False,
                "errors": errors,
                "next": pull_execution.next_step_envelope(
                    wbs_id, content,
                ).to_payload(),
            }

        record_step_state(
            wbs_id=wbs_id,
            step_number=step_number,
            status="completed",
            state_summary=state_summary,
            output_artifacts=output_artifacts,
            state_service=self._state_service,
            knowledge_store=self._knowledge_store,
            focus_manager=self._focus_manager,
            namespace=self._namespace,
        )
        _, updated = self._load(wbs_id)
        return {
            "wbs_id": wbs_id,
            "step_number": step_number,
            "accepted": True,
            "errors": [],
            "next": pull_execution.next_step_envelope(
                wbs_id, updated,
            ).to_payload(),
        }

    def advance_wbs_execution(self, wbs_id: str) -> dict[str, Any]:
        """Q15 evaluation of the next step (auto_safe / agent_review).

        ``mode='auto_safe'`` carries a validated closed-world action
        definition the driver may submit MECHANICALLY (no review);
        ``mode='agent_review'`` returns control to the agent with the
        blocking reasons; ``mode='complete'`` closes the run (tracking
        row → ``completed``).
        """
        record, content = self._load(wbs_id)
        evaluation = pull_execution.advance_evaluation(wbs_id, content)
        if (
            evaluation["mode"] == pull_execution.MODE_COMPLETE
            and str(record.get("status")) != "completed"
        ):
            self._set_wbs_status(wbs_id, "completed")
        return evaluation

    # ── Internals ───────────────────────────────────────────────────

    def _set_wbs_status(self, wbs_id: str, status: str) -> None:
        """Predicated status transition on the tracking row (UPDATE-WHERE)."""
        self._work_product_state_service.update_state(
            self._namespace,
            {"table": "thinking_wbs", "filters": {"id": wbs_id}},
            {"status": status},
        )

    def _load(self, wbs_id: str) -> tuple[dict[str, Any], str]:
        """The tracking row + the annotated WBS document (durable)."""
        if not wbs_id:
            raise FrameworkError(
                message="wbs_id is required",
                error_code=ErrorCode.PARAMETER_ERROR,
            )
        result = self._state_service.read_state(
            namespace=self._namespace,
            query={
                "table": "thinking_wbs",
                "filters": {"id": wbs_id, "is_deleted": 0},
                "limit": 1,
            },
        )
        data = result.get("data")
        records = data.get("records", []) if isinstance(data, dict) else []
        if not records:
            raise FrameworkError(
                message=f"Work Breakdown Structure not found: {wbs_id}",
                error_code=ErrorCode.WBS_NOT_FOUND,
            )
        record: dict[str, Any] = records[0]
        kb_path = str(record.get("knowledge_base_path", f"wbs/{wbs_id}.md"))
        content = self._knowledge_store.read(kb_path)
        if not content:
            raise FrameworkError(
                message=(
                    f"WBS document for {wbs_id!r} is empty or unreadable "
                    f"at {kb_path!r}"
                ),
                error_code=ErrorCode.WBS_NOT_FOUND,
            )
        return record, content


def _parse(content: str) -> Any:
    """Parse WBS content via the core parser (import-cycle-free helper)."""
    from ananta.core.plans.parser import parse

    return parse(content)
