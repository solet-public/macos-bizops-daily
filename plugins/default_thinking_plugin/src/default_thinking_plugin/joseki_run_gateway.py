"""Plugin-side gateway composing the joseki-run domain surface (spec v3.1).

The core-side engine (`ananta.services.thinking_service.joseki_run_engine`)
orchestrates; everything domain-shaped lives HERE, composed from the
already-landed plugin pieces: card lifecycle reads, mechanical instantiation,
author-by-value WBS registration, the run-row store, and run evidence. One
plugin method (`get_joseki_run_gateway`) hands the composed object to the
service wrapper — keeping `plugin.py`'s non-process public surface +1, not
+10 (god-class gate discipline).

No new validation logic and no new storage paths: every method delegates to
a surface that already carries its own gates.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

from default_thinking_plugin.joseki_instantiation import (
    instantiate_joseki_wbs,
    mint_run_wbs_id,
)

if TYPE_CHECKING:
    from default_thinking_plugin.joseki_run_store import JosekiRunStore


class LifecycleReads(Protocol):
    """The authored-joseki lifecycle surface the gateway consumes."""

    def get(self, *, joseki_key: str) -> dict[str, Any]: ...

    def record_run(
        self, *, joseki_key: str, wbs_id: str | None = None,
    ) -> dict[str, Any]: ...


class CardReads(Protocol):
    def read(self, path: str) -> str: ...


class WbsRegistrar(Protocol):
    """The author-by-value registration surface (validates-then-stores).

    ``session_id`` is the RUN session — the WBS focus pin lands there (JOS-02).
    """

    def register(
        self, *, content: str, wbs_id: str, manifest_id: str, session_id: str,
    ) -> dict[str, Any]: ...


class PlanBuffer(Protocol):
    """The plugin's session-scoped plan-focus surface (JOS-02 landed).

    Every operation keys by the acting session; release clears the
    session's WHOLE buffer (R1 whole-buffer terminal release).
    """

    def has_focused_plan(self, *, session_id: str) -> bool: ...

    def upsert_plan(self, content: str, *, session_id: str) -> dict[str, Any]: ...

    def release_session_focus(self, *, session_id: str) -> None: ...


@dataclass(frozen=True)
class JosekiRunGateway:
    """The engine's plugin seam, composed from landed plugin pieces."""

    lifecycle: LifecycleReads
    cards: CardReads
    registrar: WbsRegistrar
    run_store: JosekiRunStore
    # The focus-buffer surface is exposed as a FIELD, not delegation methods:
    # the wiring's FocusBufferPlanInstaller consumes ``gateway.plan_buffer``
    # directly (keeps this class within the god-class public-method budget).
    plan_buffer: PlanBuffer

    # -- card + instantiation ---------------------------------------------------

    def get_authored_joseki(self, joseki_key: str) -> dict[str, Any]:
        return self.lifecycle.get(joseki_key=joseki_key)

    def read_joseki_card(self, joseki_key: str) -> str:
        return self.cards.read(f"{joseki_key}.md")

    def mint_run_wbs_id(self, joseki_key: str) -> str:
        return mint_run_wbs_id(joseki_key)

    def instantiate_run_wbs(
        self,
        *,
        card_content: str,
        joseki_key: str,
        bindings: dict[str, Any],
        wbs_id: str,
        manifest_id: str,
    ) -> dict[str, Any]:
        out = instantiate_joseki_wbs(
            card_content=card_content,
            joseki_key=joseki_key,
            bindings=bindings,
            wbs_id=wbs_id,
            manifest_id=manifest_id,
        )
        return {
            "content": out.content,
            "executable_step_count": out.executable_step_count,
            "terminal_step_number": out.terminal_step_number,
        }

    def register_run_wbs(
        self, *, content: str, wbs_id: str, manifest_id: str, session_id: str,
    ) -> dict[str, Any]:
        return self.registrar.register(
            content=content,
            wbs_id=wbs_id,
            manifest_id=manifest_id,
            session_id=session_id,
        )

    # -- run rows -----------------------------------------------------------------

    def create_run_row(
        self,
        *,
        joseki_key: str,
        wbs_id: str,
        session_id: str,
        flow_id: str,
        requester: str,
        label: str,
    ) -> str:
        return self.run_store.create(
            joseki_key=joseki_key,
            wbs_id=wbs_id,
            session_id=session_id,
            flow_id=flow_id,
            requester=requester,
            label=label,
        )

    def get_run_row(self, run_id: str) -> dict[str, Any] | None:
        return self.run_store.get(run_id=run_id)

    def get_run_row_by_wbs(self, wbs_id: str) -> dict[str, Any] | None:
        rows = self.run_store.list_runs(status=None, joseki_key=None, limit=200)
        return next((r for r in rows if r.get("wbs_id") == wbs_id), None)

    def list_run_rows(
        self,
        *,
        status: str | None,
        joseki_key: str | None,
        limit: int,
    ) -> list[dict[str, Any]]:
        return self.run_store.list_runs(
            status=status, joseki_key=joseki_key, limit=limit,
        )

    def cas_run_status(
        self,
        *,
        run_id: str,
        from_status: str,
        to_status: str,
        extra_updates: dict[str, Any] | None,
    ) -> bool:
        return self.run_store.cas_status(
            run_id=run_id,
            from_status=from_status,
            to_status=to_status,
            extra_updates=extra_updates,
        )

    def cas_increment_attempts(
        self, *, run_id: str, prior_attempts: int,
    ) -> bool:
        return self.run_store.cas_increment_attempts(
            run_id=run_id, prior_attempts=prior_attempts,
        )

    def record_current_step(self, *, run_id: str, step_number: int) -> bool:
        return self.run_store.record_current_step(
            run_id=run_id, step_number=step_number,
        )

    def record_joseki_run_evidence(
        self, *, joseki_key: str, wbs_id: str,
    ) -> dict[str, Any]:
        return self.lifecycle.record_run(joseki_key=joseki_key, wbs_id=wbs_id)
