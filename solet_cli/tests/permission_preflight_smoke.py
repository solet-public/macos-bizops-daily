"""Regression coverage for the consolidated resolved permission pre-flight."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.completion_verifier import resolved_completion_probe_ids  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.create_execution import execute_create  # noqa: E402
from solet_manager.models import CommandResult, ExitCode  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.permission_preflight import (  # noqa: E402
    permission_preflight_fingerprint_content,
    render_permission_preflight,
)
from solet_manager.plan_builder import SetupPlan  # noqa: E402
from solet_manager.preview_engine import acquisition_preview  # noqa: E402
from solet_manager.preview_rendering import approval_fingerprint  # noqa: E402
from solet_manager.registry import InstanceRegistry  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import (  # noqa: E402
    Transaction,
    canonical_sha256,
    write_transaction,
)

_CONTRACTS = (
    Path(__file__).resolve().parents[2]
    / "plugins"
    / "github_midwife_plugin"
    / "knowledge_base"
)


def _plan(*, session_sources: list[str]) -> SetupPlan:
    return SetupPlan(
        answers={
            "decisions": {
                "setup_profile": "free",
                "autostart": "disabled",
                "coding_agents": ["codex"],
                "session_sources": session_sources,
            },
            "consents": {"system_change_consent": True},
        },
        operations=(),
        unresolved_decisions=(),
        unresolved_consents=(),
    )


def _items(preflight: dict[str, object]) -> dict[str, dict[str, object]]:
    raw = preflight["items"]
    if not isinstance(raw, list):
        raise AssertionError("red: pre-flight item list is absent")
    return {
        str(item["id"]): item
        for item in raw
        if isinstance(item, dict) and isinstance(item.get("id"), str)
    }


def _headless_preflight(bundle: ContractBundle, selected: SetupPlan) -> dict[str, object]:
    unreadable = {
        "session_sources:entry:codex_session_roots_readable": {
            "error_kind": "codex_session_roots_unreadable",
            "repair": "Approve and expose only readable current-user session roots.",
            "evidence": [],
        }
    }
    with patch("solet_manager.permission_preflight.display_session_attached", return_value=False):
        return render_permission_preflight(bundle, selected, unreadable)


def _assert_headless_subset(headless: dict[str, object]) -> None:
    headless_items = _items(headless)
    if "codex_session_files_permission" not in headless_items:
        raise AssertionError("red: selected Codex session permission is absent")
    if "claude_session_files_permission" in headless_items:
        raise AssertionError("red: skipped Claude capability leaked into pre-flight")
    if "claude_plugin_configuration_permission" in headless_items:
        raise AssertionError("red: skipped Claude plugin permission leaked into pre-flight")
    codex_remediation = headless_items["codex_session_files_permission"]["remediation"]
    if not isinstance(codex_remediation, dict) or codex_remediation.get("settings_url") is not None:
        raise AssertionError("red: headless Settings action was emitted without an attached display")
    guidance = headless.get("headless_guidance")
    if not isinstance(guidance, str) or "launchctl kickstart" not in guidance or "5900" not in guidance:
        raise AssertionError("red: headless attach-a-screen guidance is missing")


def _assert_posix_absent_has_no_settings_link(bundle: ContractBundle, selected: SetupPlan) -> None:
    absent = {
        "session_sources:entry:codex_session_roots_readable": {
            "error_kind": None,
            "repair": None,
            "evidence": [{"id": "codex_session_roots_absent"}],
        }
    }
    with patch("solet_manager.permission_preflight.display_session_attached", return_value=True):
        absent_preflight = render_permission_preflight(bundle, selected, absent)
    absent_remediation = _items(absent_preflight)["codex_session_files_permission"]["remediation"]
    if not isinstance(absent_remediation, dict) or (
        absent_remediation.get("cause") != "posix_absent"
        or absent_remediation.get("settings_url") is not None
    ):
        raise AssertionError("red: POSIX-absent root was offered a Settings deep-link")


def _assert_doctor_subset(bundle: ContractBundle, selected: SetupPlan) -> None:
    completion = resolved_completion_probe_ids(bundle, selected.answers)
    if (
        "codex_session_roots_readable" not in completion
        or "claude_session_roots_readable" in completion
    ):
        raise AssertionError("red: doctor completion subset differs from selected session roots")


def _assert_unresolved_later_stage_disclosure(
    bundle: ContractBundle,
    selected: SetupPlan,
) -> None:
    unresolved_answers = dict(selected.answers)
    decisions = unresolved_answers["decisions"]
    if not isinstance(decisions, dict):
        raise AssertionError("red: fixture has no decisions object")
    unresolved_decisions = dict(decisions)
    del unresolved_decisions["session_sources"]
    unresolved_answers["decisions"] = unresolved_decisions
    unresolved_plan = SetupPlan(
        answers=unresolved_answers,
        operations=(),
        unresolved_decisions=("session_sources",),
        unresolved_consents=(),
    )
    with patch("solet_manager.permission_preflight.display_session_attached", return_value=True):
        preflight = render_permission_preflight(bundle, unresolved_plan, {})
    items = _items(preflight)
    for permission_id in (
        "codex_session_files_permission",
        "claude_session_files_permission",
    ):
        item = items.get(permission_id)
        if item is None:
            raise AssertionError(
                "red: unresolved later-stage decision omitted a permission from "
                "the approved pre-flight"
            )
        if item.get("resolution_state") != "awaiting_decision":
            raise AssertionError(
                "red: unresolved later-stage permission was not labelled awaiting_decision"
            )
    semantic = permission_preflight_fingerprint_content(preflight)
    expected = {
        "permission_id": "codex_session_files_permission",
        "cause_class": "not_currently_blocked",
        "resolution_state": "awaiting_decision",
    }
    if expected not in semantic:
        raise AssertionError(
            "red: approval fingerprint semantic content omits the unresolved Codex permission"
        )


def _seed() -> SeedLock:
    return SeedLock(
        repository="https://github.com/solet-public/macos-bizops.git",
        release_tag="release-1",
        commit="a" * 40,
        tree_hash="b" * 40,
        archive_sha256="c" * 64,
        profile="macos-bizops",
    )


def _fingerprint(
    bundle: ContractBundle,
    plan: SetupPlan,
    observations: dict[str, object],
    *,
    display_attached: bool = False,
    target: Path = Path("/tmp/permission-fingerprint"),
) -> str:
    with patch(
        "solet_manager.permission_preflight.display_session_attached",
        return_value=display_attached,
    ):
        preflight = render_permission_preflight(bundle, plan, observations)
    return approval_fingerprint(
        bundle=bundle,
        seed=_seed(),
        name="permission-fingerprint",
        target=target,
        plan=plan,
        probe_observations={},
        consent_states={},
        planned_actions=[],
        permission_preflight=preflight,
    )


def _acquisition_fingerprint(
    bundle: ContractBundle,
    plan: SetupPlan,
    observations: dict[str, object],
) -> str:
    with patch("solet_manager.permission_preflight.display_session_attached", return_value=False):
        preflight = render_permission_preflight(bundle, plan, observations)
    config = CreateConfig(
        name="permission-fingerprint",
        target=Path("/tmp/permission-fingerprint"),
        autostart=False,
        decisions={},
    )
    preview = acquisition_preview(
        config=config,
        seed=_seed(),
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        deferred_decisions=(),
        rendered_decisions=[],
        unresolved_decisions=(),
        decision_prompts=[],
        permission_preflight=preflight,
    )
    return str(preview.data["approval_fingerprint"])


def _legacy_fingerprint(
    bundle: ContractBundle,
    plan: SetupPlan,
    *,
    target: Path = Path("/tmp/permission-fingerprint"),
) -> str:
    """Reproduce the persisted approval shape from before this migration."""

    seed = _seed()
    return canonical_sha256(
        {
            "flow_id": bundle.flow_id,
            "flow_source_revision": bundle.source_revision,
            "seed_identity": seed.identity_dict(),
            "name": "permission-fingerprint",
            "target": str(target),
            "answers": plan.answers,
            "answers_fingerprint": canonical_sha256(plan.answers),
            "operations": [],
            "probe_observations": {},
            "consents": {},
            "planned_actions": [],
        }
    )


def _assert_fingerprint_binding(bundle: ContractBundle, selected: SetupPlan) -> None:
    unreadable = {
        "session_sources:entry:codex_session_roots_readable": {
            "error_kind": "codex_session_roots_unreadable",
            "repair": "Approve readable roots.",
            "evidence": [],
        }
    }
    clean = _fingerprint(bundle, selected, {})
    changed = _fingerprint(bundle, selected, unreadable)
    recomputed = _fingerprint(bundle, selected, {})
    display_changed = _fingerprint(
        bundle,
        selected,
        unreadable,
        display_attached=True,
    )
    acquisition_clean = _acquisition_fingerprint(bundle, selected, {})
    acquisition_changed = _acquisition_fingerprint(bundle, selected, unreadable)
    if clean == changed:
        raise AssertionError(
            "red: permission pre-flight state changed without changing approval fingerprint"
        )
    if clean != recomputed:
        raise AssertionError(
            "red: identical permission pre-flight state changed the recomputed fingerprint"
        )
    if changed != display_changed:
        raise AssertionError(
            "red: volatile GUI display detail changed the permission fingerprint"
        )
    if acquisition_clean == acquisition_changed:
        raise AssertionError(
            "red: acquisition approval fingerprint omitted permission pre-flight state"
        )

    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        paths = ManagerPaths.resolve(explicit_home=root / "manager", home=root)
        config = CreateConfig(
            name="permission-fingerprint",
            target=root / "target",
            autostart=False,
            decisions={},
        )
        current = _fingerprint(bundle, selected, {}, target=config.target)
        old = _legacy_fingerprint(bundle, selected, target=config.target)
        if old == current:
            raise AssertionError(
                "red: permission-preflight binding did not invalidate the old preimage"
            )
        transaction = Transaction.create(
            name=config.name,
            target=config.target,
            input_fingerprint=canonical_sha256(config.to_identity_dict()),
            answers=selected.answers,
            seed=_seed(),
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            flow_contract_digest=bundle.contract_digest,
            stage_ids=tuple(bundle.stages),
            completion_probe_ids=bundle.completion_probe_ids,
        ).approve(old)
        journal = paths.transaction_path(config.name)
        write_transaction(journal, transaction)
        before = journal.read_bytes()

        def preview_call(*_args: object, **_kwargs: object) -> CommandResult:
            return CommandResult(
                kind="create_preview",
                status="preview_ready",
                message="fresh preflight",
                exit_code=ExitCode.OK,
                data={"approval_fingerprint": current},
            )

        result = execute_create(
            paths=paths,
            contract_directory=None,
            seed_lock_path=root / "unused-seed.lock.json",
            registry=InstanceRegistry(paths.registry_path),
            preview_call=preview_call,
            config=config,
            approved_fingerprint=old,
        )
        if result.error_kind != "approval_stale":
            raise AssertionError(
                "red: persisted pre-change approval was not refused as approval_stale"
            )
        if "permission preflight" not in result.message or result.repair != (
            "Run a fresh dry-run, review permission preflight, and re-approve."
        ):
            raise AssertionError(
                "red: approval_stale refusal lacks the required preflight remediation"
            )
        if config.target.exists() or journal.read_bytes() != before:
            raise AssertionError("red: stale approval reached a mutation boundary")


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    selected = _plan(session_sources=["codex_local"])
    headless = _headless_preflight(bundle, selected)
    _assert_headless_subset(headless)
    _assert_posix_absent_has_no_settings_link(bundle, selected)
    _assert_doctor_subset(bundle, selected)
    _assert_unresolved_later_stage_disclosure(bundle, selected)
    _assert_fingerprint_binding(bundle, selected)
    headless_items = _items(headless)
    if "system_change_consent" not in headless_items:
        raise AssertionError("red: blocking denial item is not rendered up front")
    print("permission_preflight_smoke OK: 20 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
