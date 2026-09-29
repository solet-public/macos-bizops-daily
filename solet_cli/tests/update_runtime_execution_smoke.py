"""End-to-end Step-5 runtime transition against a real Git fixture (design section 11).

The Step-4 update runs for real to ``source_advanced``; from there the runtime
preview, the second approval, and every stage run through the real Manager
executor with the seed's own handlers in process and fake launchd/bridge
seams.  Legs:

- preview at ``source_advanced`` renders every group, discloses non-zero
  target process executions and zero writes, and a foreign fingerprint is
  refused as ``probe_drift`` with no target write;
- the single-colour path reaches ``promoted`` (Step 6: ``runtime_advanced``
  continues through the final doctor and promotion in the same ``--yes``):
  dependencies verified by probe, migrations verified by probe, hydration
  writes the three managed artifacts with a backup each (a legacy unstamped
  plist is upgraded, two blocks appended), the plist digest equals the
  approval-bound expectation, bootout/bootstrap happen once, readiness then
  attestation then the runtime axis, and the journal reads ``promoted`` with
  ``verified_*`` at the candidate, ``management_state=verified`` and the
  active pointer released;
- a rerun after promotion is refused as ``probe_drift`` (already promoted) and
  rewrites nothing; every written artifact has ``before.*`` and an ``after``
  digest; the tracked tree is still CLEAN and ``git status`` shows no ``??``;
- crash injection: a crash after ``hydration_applying`` resumes and finishes
  without rewriting already-current artifacts; a crash between the runtime
  axis CAS and ``lifecycle_advanced`` resumes by re-attesting;
- a conflicting managed block on a LATER artifact restores the earlier
  written artifact byte-exact and goes terminal ``blocked`` with the conflict;
- a plist edited between ``hydration_advanced`` and lifecycle entry is
  ``probe_drift`` with no spawn;
- a healthy bridge whose LaunchAgent still reports the pre-restart process
  never publishes the runtime axis and goes terminal ``failed``;
- bootout timeout blocks, bootstrap failure fails, runtime axis stays ``None``;
- a retry-safe pip failure leaves ``dependencies_applying`` with no other
  change and the next ``--yes`` installs only what is still missing;
- D1: a bundle declaring an in-target destination the target does not ignore,
  a tracked candidate path (``CLAUDE.md``), and an in-target destination the
  target DOES ignore are classified exactly as section 6.3 says, and a
  ``.gitignore`` shipped in the candidate tree does not change the verdict;
- ``single_color_required`` overrides a visible router; a roster plugin absent
  from the candidate blocks before any write;
- the whole suite runs under the fail-on-call database spy.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    KB,
    TEMPLATES,
    FakeHost,
    Fixture,
    advance_to_source_advanced,
    build_fixture,
    bundle_document,
    data,
    db_spy,
    default_artifacts,
    expect,
    git,
    runtime_fingerprint,
    template_bytes,
)
from _step6_support import SimulatedCrash  # noqa: E402
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager import update_runtime_execution as executor  # noqa: E402
from solet_manager.errors import ProbeDriftError, UpdateBlockedError, UpdateFailedError  # noqa: E402
from solet_manager.local_state import observe_local_state  # noqa: E402
from solet_manager.managed_artifact_backup import backup_root, file_sha256  # noqa: E402
from solet_manager.models import ManagementState, ReleaseIdentity  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_local_state import CLONE_EXCLUDE_RELATIVE, paths_ignored_by_declared_exclude, rebaseline_revision  # noqa: E402
from solet_manager.update_runtime_plan import STEP5_CAPABILITIES, STEP5_MANAGED_SUB_SURFACES, STEP5_NON_TOUCH_SURFACES  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _status(fixture: Fixture) -> str:
    return cast(str, fixture.journal()["status"])


def _assert_preview(fixture: Fixture) -> str:
    preview = preview_update_instance(fixture.request)
    _check(preview.status == "runtime_preview_ready" and preview.exit_code == 0, f"runtime preview ready: {preview.status} {preview.error_kind} {preview.data.get('blocked')} {preview.data.get('lifecycle')}")
    payload = preview.data
    _assert_preview_preservation(payload["preservation"])
    lifecycle = payload["lifecycle"]
    _check((data(lifecycle, "strategy"), data(lifecycle, "zero_downtime_rollback")) == ("single_color_restart", False), "single-colour strategy discloses no zero-downtime rollback")
    artifacts = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], payload["managed_artifacts"])}
    plist_row, zshrc_row = artifacts["instance_launchagent_plist"], artifacts["shell_startup_block"]
    _check((data(plist_row, "state"), data(plist_row, "action")) == ("legacy_matched", "render_whole"), "legacy unstamped plist is recognised as a previous render")
    _check((data(zshrc_row, "state"), data(zshrc_row, "action")) == ("absent", "append_block"), "absent zshrc block will be appended")
    _check(data(lifecycle, "plist_expected_sha256") == data(plist_row, "expected_sha256"), "the cutover expects the plist hydration leaves behind (section 7.3)")
    _check(payload["dependency_actions"] == [[]], "closed environment plans no dependency action")
    limit = payload["rollback_limit"]
    _check((data(limit, "forward_only_boundary"), data(limit, "router_previous_is_code_only")) == (None, True), "rollback limit disclosed")
    _check(all(data(row, "postcondition_now") == "verified" for row in cast(list[Any], payload["migrations_pre"])), "migrations verified now")
    fingerprint = cast(str, payload["runtime_approval_fingerprint"])
    _check(fingerprint.startswith("sha256:"), "runtime fingerprint rendered")
    return fingerprint


def _assert_preview_preservation(preservation: Any) -> None:
    _check((data(preservation, "target_byte_writes"), data(preservation, "manager_state_writes")) == (0, 0), "preview writes nothing")
    _check(cast(int, data(preservation, "target_process_executions")) >= 6, "preview discloses target process executions")
    split = (data(preservation, "non_touch_surfaces"), data(preservation, "managed_sub_surfaces"), data(preservation, "capabilities"))
    _check(split == (list(STEP5_NON_TOUCH_SURFACES), list(STEP5_MANAGED_SUB_SURFACES), list(STEP5_CAPABILITIES)), "preview renders the closed surface split")


def _assert_foreign_fingerprint_refused(fixture: Fixture) -> None:
    before = fixture.paths.maintenance_inventory_path.read_bytes()
    zshrc = fixture.home / ".zshrc"
    expect(ProbeDriftError, lambda: apply_update(fixture.request, "sha256:" + "0" * 64), "foreign runtime fingerprint accepted")
    _check(_status(fixture) == "source_advanced" and fixture.paths.maintenance_inventory_path.read_bytes() == before and not zshrc.exists(), "a refused runtime approval writes nothing")


def _assert_runtime_advanced(fixture: Fixture, fingerprint: str) -> None:
    applied = apply_update(fixture.request, fingerprint)
    _check(applied.status == "promoted" and applied.exit_code == 0, f"runtime advanced then promoted: {applied.status} {applied.error_kind} {applied.message}")
    operation_id = cast(str, applied.data["operation_id"])
    _assert_axes_published(fixture, operation_id)
    journal = _last_journal(fixture, operation_id)
    _check(journal["status"] == "promoted", "journal reads promoted")
    statuses = {cast(str, data(row, "operation_id")): data(row, "status") for row in cast(list[Any], journal["runtime_operations"])}
    expected_statuses = {
        "dependencies_reconcile": "verified_by_probe",
        "migration_solet_rename": "verified_by_probe",
        "hydration_reconcile": "verified",
        "migration_export_root_containment": "verified_by_probe",
        "autostart_reconcile": "verified",
        "lifecycle_restart_single_color": "verified",
        "runtime_readiness": "verified",
        "plugin_cache_refresh": "verified_by_probe",
    }
    _check(statuses == expected_statuses, f"operation rows: {statuses}")
    executed = cast(list[str], applied.data["target_actions_executed"])
    counts = (executed.count("launchctl:bootout"), executed.count("launchctl:bootstrap"), executed.count("inventory:publish_runtime_advance"))
    _check(counts == (1, 1, 1), f"restart happened exactly once, then the axis was published: {counts}")
    _check(executed.index("launchctl:bootstrap") < executed.index("inventory:publish_runtime_advance"), "attestation precedes publication")
    _assert_hydrated(fixture)


def _last_journal(fixture: Fixture, operation_id: str) -> dict[str, Any]:
    from solet_manager.update_journal import read_update_journal  # noqa: PLC0415

    return cast(dict[str, Any], read_update_journal(fixture.paths.operation_path(fixture.record().instance_id, operation_id)))


def _assert_axes_published(fixture: Fixture, operation_id: str) -> None:
    """Step 6: promotion moved the verified axis, the management state and the pointer together."""
    record = fixture.record()
    candidate = ReleaseIdentity("https://github.com/example/seed.git", fixture.candidate.commit, fixture.candidate.tree, "r2")
    identities = record.contract_identities
    _check((record.runtime_release, record.source_release) == (candidate, candidate), "runtime axis published to the exact candidate")
    _check((identities.runtime_contract_digest, identities.source_contract_digest) == (fixture.contract, fixture.contract), "runtime contract digest is the transition bundle digest")
    _check((record.verified_release, identities.verified_contract_digest, record.management_state) == (candidate, fixture.contract, ManagementState.VERIFIED), "verified axis and management_state promoted")
    _check((record.last_verified_operation_id, record.update_eligibility.reason_codes) == (operation_id, ()), "last_verified names the update; eligibility recomputed")
    _check(record.active_operation is None, "active pointer released after promotion")


def _assert_hydrated(fixture: Fixture) -> None:
    _assert_markers(fixture)
    _check(git(fixture.target, "status", "--porcelain", "--untracked-files=all") == "", "tracked tree is CLEAN; no ?? entry under the target after hydration")
    record = fixture.record()
    operation_id = cast(str, fixture.journal()["operation_id"])
    root = backup_root(fixture.paths, record.instance_id, operation_id)
    for artifact_id in ("instance_launchagent_plist", "shell_startup_block", "user_claude_md_section"):
        present = ((root / artifact_id / "before.bytes").exists(), (root / artifact_id / "before.json").exists())
        _check(present == (True, True), f"backup exists for {artifact_id}")
    recorded = json.loads((root / "shell_startup_block" / "before.json").read_text())
    _check(recorded["absent"] is True, "absent zshrc recorded as absent before the write")
    afters = _hydration_after_digests(fixture)
    _check(len(afters) == 2, "after digests journaled for both managed blocks")
    _check({item["artifact_id"]: item["after_sha256"] for item in afters} == {"shell_startup_block": file_sha256(fixture.home / ".zshrc"), "user_claude_md_section": file_sha256(fixture.home / ".claude" / "CLAUDE.md")}, "journaled after digests equal the destinations")


def _assert_markers(fixture: Fixture) -> None:
    zshrc = (fixture.home / ".zshrc").read_text()
    shape = (zshrc.startswith("# BEGIN SOLET fixture v"), zshrc.rstrip().endswith("# END SOLET fixture"), "client/fixture.zsh" in zshrc)
    _check(shape == (True, True, True), "zshrc block appended with a versioned marker")
    claude = (fixture.home / ".claude" / "CLAUDE.md").read_text()
    shape = ("<!-- BEGIN SOLET fixture v" in claude, claude.rstrip().endswith("<!-- END SOLET fixture -->"), True)
    _check(shape == (True, True, True), "user CLAUDE.md section appended with a versioned marker")
    plist = fixture.plist_path.read_text()
    _check(plist.split("\n")[1].startswith("<!-- rendered-from: plugins/github_midwife_plugin/knowledge_base/hydration_templates/launchagent.plist.template@sha256:"), "plist carries the rendered-from stamp")


def _hydration_after_digests(fixture: Fixture) -> list[dict[str, Any]]:
    journal = fixture.journal()
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    evidences = [cast(dict[str, Any], cast(dict[str, Any], attempt)["evidence"]) for attempt in cast(list[Any], data(rows["hydration_reconcile"], "attempts"))]
    return [evidence for evidence in evidences if "after_sha256" in evidence]


def _assert_idempotent(fixture: Fixture, fingerprint: str) -> None:
    before = {path: path.read_bytes() for path in (fixture.home / ".zshrc", fixture.home / ".claude" / "CLAUDE.md", fixture.plist_path)}
    exc = expect(ProbeDriftError, lambda: apply_update(fixture.request, fingerprint), "a rerun after promotion was accepted")
    _check("already promoted" in str(exc), "rerun after promotion is refused as already promoted")
    _check({path: path.read_bytes() for path in before} == before, "rerun leaves every artifact byte-identical")
    preview = preview_update_instance(fixture.request)
    _check(preview.status == "already_current" and preview.exit_code == 0 and preview.data["approval_fingerprint"] is None, "dry-run after promotion is already_current with no fingerprint")


def _assert_partial_rollback(root: Path) -> None:
    """Section 9.3: a later artifact conflicts after an earlier write; earlier writes are restored."""
    fixture = build_fixture(root)
    (fixture.home / ".claude").mkdir(exist_ok=True)
    (fixture.home / ".claude" / "CLAUDE.md").write_text("mine\n<!-- BEGIN SOLET fixture v1 -->\nedited by hand\n<!-- END SOLET fixture -->\n")
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    _check(preview.status == "awaiting_user" and preview.exit_code == 3, "a managed-block conflict blocks the preview")
    conflicts = [row for row in cast(list[Any], preview.data["managed_artifacts"]) if data(row, "conflict") == "managed_block_unknown_origin"]
    _check(len(conflicts) == 1 and data(conflicts[0], "artifact_id") == "user_claude_md_section", "unknown-origin block reported with its path")
    # Force the conflict to appear only at apply time: approve a clean plan, then edit the block.
    (fixture.home / ".claude" / "CLAUDE.md").unlink()
    fingerprint = runtime_fingerprint(fixture)
    original = executor.RuntimeExecution._hydrate_one  # noqa: SLF001

    def hydrate(self: Any, operation: Any, inputs: Any, artifact_id: str, plan: Any) -> Any:
        if artifact_id == "user_claude_md_section":
            (fixture.home / ".claude" / "CLAUDE.md").write_text("mine\n<!-- BEGIN SOLET fixture v1 -->\nedited by hand\n<!-- END SOLET fixture -->\n")
        return original(self, operation, inputs, artifact_id, plan)

    with patch.object(executor.RuntimeExecution, "_hydrate_one", hydrate):
        exc = expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "conflict did not block")
    _check(cast(UpdateBlockedError, exc).error_kind == "managed_block_unknown_origin", "terminal reason is the original conflict")
    _check(not (fixture.home / ".zshrc").exists(), "the earlier zshrc write was restored to its absent entry state")
    _check((fixture.home / ".claude" / "CLAUDE.md").read_text().startswith("mine\n"), "the operator's file is untouched")
    journal = fixture.journal()
    _check(journal["status"] == "blocked" and data(journal["result"], "reason_code") == "managed_block_unknown_origin", "journal terminal blocked with the conflict")
    rows = {cast(str, data(row, "operation_id")): row for row in cast(list[Any], journal["runtime_operations"])}
    phases = [cast(dict[str, Any], attempt)["phase"] for attempt in cast(list[Any], data(rows["hydration_reconcile"], "attempts"))]
    _check("restore" in phases, "restore attempt journaled on the hydration row")
    _check(fixture.record().runtime_release is None, "runtime axis untouched by a blocked hydration")


def _assert_crash_resume_after_hydration(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)

    def crash(self: Any, plan: Any) -> None:
        raise RuntimeError("simulated crash after hydration writes")

    with patch.object(executor.RuntimeExecution, "_require_plist_expected", crash):
        expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _check(_status(fixture) == "hydration_applying", "journal stays at hydration_applying after the crash")
    _check((fixture.home / ".zshrc").exists(), "hydration wrote before the crash")
    pending = preview_update_instance(fixture.request)
    _check(pending.status == "runtime_resume_pending" and pending.exit_code == 3 and pending.data["recorded_runtime_approval_fingerprint"] == fingerprint, "dry-run reports the in-flight runtime plan")
    written = {path: path.read_bytes() for path in (fixture.home / ".zshrc", fixture.home / ".claude" / "CLAUDE.md", fixture.plist_path)}
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted", "resume finishes the update")
    _check({path: path.read_bytes() for path in written} == written, "resume rewrote nothing already current")
    executed = cast(list[str], resumed.data["target_actions_executed"])
    _check(not any(item.startswith("apply:hydration") or item.startswith("apply:autostart") for item in executed), "no hydration apply on resume")


def _assert_crash_between_cas_and_lifecycle_advanced(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    original = executor.RuntimeExecution._attest_and_publish  # noqa: SLF001

    def crash(self: Any, plan: Any) -> None:
        original(self, plan)
        raise RuntimeError("simulated crash after the runtime-axis CAS")

    with patch.object(executor.RuntimeExecution, "_attest_and_publish", crash):
        expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _check(_status(fixture) == "lifecycle_applying" and fixture.record().runtime_release is not None, "inventory runtime axis at N+1 while the journal is below lifecycle_advanced")
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted" and "launchctl:bootout" not in cast(list[str], resumed.data["target_actions_executed"]), "resume re-attests without a second restart")


def _assert_plist_drift_before_lifecycle(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)

    def crash(self: Any) -> None:
        raise RuntimeError("simulated crash at lifecycle entry")

    with patch.object(executor.RuntimeExecution, "_lifecycle", crash):
        expect(RuntimeError, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _check(_status(fixture) == "lifecycle_applying", "journal at lifecycle_applying")
    fixture.plist_path.write_bytes(fixture.plist_path.read_bytes() + b"<!-- operator edit -->\n")
    exc = expect(UpdateBlockedError, lambda: apply_update(fixture.request, fingerprint), "plist drift not detected")
    _check(cast(UpdateBlockedError, exc).error_kind == "probe_drift" and all(call[0] == "print" for call in fixture.host.launchctl_calls), "plist drift at lifecycle entry is probe_drift with no spawn")
    _check(fixture.record().runtime_release is None and _status(fixture) == "blocked", "runtime axis untouched; journal blocked")


def _assert_attestation_ordering(root: Path) -> None:
    host = FakeHost(pids=[4242, 4242])
    fixture = build_fixture(root, host=host)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)
    exc = expect(UpdateFailedError, lambda: apply_update(fixture.request, fingerprint), "healthy bridge with the old process accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "launchagent_start_failed", "health alone never publishes")
    _check(fixture.record().runtime_release is None and _status(fixture) == "failed", "runtime axis stays None and the journal is terminal failed")


def _assert_launchctl_failures(root: Path) -> None:
    stuck = build_fixture(root / "stuck", host=FakeHost(bootout_clears=False))
    advance_to_source_advanced(stuck)
    stuck_fp = runtime_fingerprint(stuck)
    exc = expect(UpdateBlockedError, lambda: apply_update(stuck.request, stuck_fp), "bootout timeout accepted")
    _check(cast(UpdateBlockedError, exc).error_kind == "launchagent_stop_timeout" and _status(stuck) == "blocked" and stuck.record().runtime_release is None, "bootout timeout blocks")
    failing = build_fixture(root / "failing", host=FakeHost(bootstrap_fails=True))
    advance_to_source_advanced(failing)
    failing_fp = runtime_fingerprint(failing)
    exc = expect(UpdateFailedError, lambda: apply_update(failing.request, failing_fp), "bootstrap failure accepted")
    _check(cast(UpdateFailedError, exc).error_kind == "launchagent_start_failed" and _status(failing) == "failed" and failing.record().runtime_release is None, "bootstrap failure fails")


def _assert_retry_safe_dependency(root: Path) -> None:
    host = FakeHost(closure_scenario="missing_package", pip_fail_once=True)
    fixture = build_fixture(root, host=host)
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    actions = cast(list[Any], preview.data["dependency_actions"])
    _check(actions == [["pip.install_editable.plugins.github_midwife_plugin"]], f"one missing plugin plans exactly that editable install: {actions}")
    fingerprint = cast(str, preview.data["runtime_approval_fingerprint"])
    paused = apply_update(fixture.request, fingerprint)
    _check(paused.status == "dependencies_applying" and paused.exit_code == 3 and paused.error_kind == "dependency_closure_apply_failed", f"failing pip pauses the stage: {paused.status} {paused.error_kind}")
    _check(len(host.pip_calls) == 1 and fixture.record().runtime_release is None and not (fixture.home / ".zshrc").exists(), "no other change after the retry-safe failure")
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted" and len(host.pip_calls) == 2 and "github_midwife_plugin" in host.pip_calls[1][-1], "re-entry installs only what is still missing")


def _assert_d1_destination_rules(root: Path) -> None:
    def in_target(name: str, destination: str) -> Any:
        artifact = dict(default_artifacts()[1])
        artifact["artifact_id"] = name
        artifact["logical_destination"] = destination
        return artifact

    not_ignored = build_fixture(root / "not_ignored", document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), in_target("client_shell", "{TARGET}/client/{NAME}.zsh")]))
    advance_to_source_advanced(not_ignored)
    preview = preview_update_instance(not_ignored.request)
    reasons = {(data(row, "subject"), data(row, "reason")) for row in cast(list[Any], preview.data["blocked"])}
    _check(("client_shell", "in_target_destination_not_ignored") in reasons and preview.data["runtime_approval_fingerprint"] is None, "an in-target destination the target does not ignore is refused at plan time")
    # Section 13.2: a genesis-born install's own .gitignore is untracked (??), and
    # a candidate that ships one collides with it, so Step 4 refuses before any
    # Step-5 verdict is reached; the shipped file cannot change the verdict.
    shipped_ignore = build_fixture(root / "shipped_ignore", document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), in_target("client_shell", "{TARGET}/client/{NAME}.zsh")]), extra_candidate_files={".gitignore": "client/\n"})
    (shipped_ignore.target / ".gitignore").write_text(".venv\n", encoding="utf-8")
    step4 = preview_update_instance(shipped_ignore.request)
    _check(step4.status == "awaiting_user" and step4.data["approval_fingerprint"] is None, "a candidate-shipped .gitignore collides with the install's own untracked one at Step 4")
    tracked = build_fixture(root / "tracked", document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), in_target("agent_file", "{TARGET}/docs/new.txt")]))
    (tracked.target / ".git" / "info" / "exclude").write_text(".venv/\nprofile/config/manifest.yaml\nprofile/data/\ndocs/\n")
    advance_to_source_advanced(tracked)
    preview = preview_update_instance(tracked.request)
    reasons = {(data(row, "subject"), data(row, "reason")) for row in cast(list[Any], preview.data["blocked"])}
    _check(("agent_file", "in_target_destination_tracked") in reasons, "a destination the candidate tree tracks is refused even when the target ignores it")
    ignored = build_fixture(root / "ignored", document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), in_target("client_shell", "{TARGET}/client/{NAME}.zsh")]))
    (ignored.target / ".git" / "info" / "exclude").write_text(".venv/\nprofile/config/manifest.yaml\nprofile/data/\nclient/\n")
    advance_to_source_advanced(ignored)
    preview = preview_update_instance(ignored.request)
    reasons = {(data(row, "subject"), data(row, "reason")) for row in cast(list[Any], preview.data["blocked"])}
    _check(not any(subject == "client_shell" for subject, _ in reasons), "an in-target destination the target ignores and the candidate does not track is admitted")
    fingerprint = cast(str, preview.data["runtime_approval_fingerprint"])
    _check(fingerprint is not None, "admitted in-target artifact yields a fingerprint")
    (ignored.target / ".git" / "info" / "exclude").write_text(".venv/\nprofile/config/manifest.yaml\nprofile/data/\n")
    exc = expect(ProbeDriftError, lambda: apply_update(ignored.request, fingerprint), "changed ignore source accepted")
    _check("probe_drift" in cast(ProbeDriftError, exc).error_kind, "a change to a consulted ignore source between preview and apply is probe_drift")


_ROOT = Path(__file__).resolve().parents[2]
_SKILL_TEMPLATE = "feedback_skill_SKILL.md.template"
_R56_SKILL = _ROOT / "plugins/github_midwife_plugin/tests/fixtures/hydration_predecessors/feedback_skill_r56.fixture"


def _feedback_skill_fixture(root: Path, installed: str) -> Fixture:
    """A fixture whose baseline holds the r56 feedback template, whose candidate holds the shipped one and declares the SHIPPED ``feedback_skill`` artifact, with ``installed`` at the user-scope skill path."""
    shipped = {row["artifact_id"]: row for row in json.loads((_ROOT / KB / "existing_install_flow.json").read_text())["managed_artifacts"]}
    reference = f"{TEMPLATES}/{_SKILL_TEMPLATE}"
    fixture = build_fixture(root, document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), shipped["feedback_skill"]]), baseline_extra={reference: _R56_SKILL.read_bytes()}, extra_candidate_files={reference: template_bytes(_SKILL_TEMPLATE)})
    skill = fixture.home / ".claude" / "skills" / "feedback" / "SKILL.md"
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_text(installed, encoding="utf-8")
    return fixture


def _assert_feedback_skill_refresh(root: Path) -> None:
    """r57 hands-off fix: an update replaces an installed r56 feedback skill, and leaves an operator-edited one alone."""
    old = _R56_SKILL.read_text(encoding="utf-8").replace("{{SOLET_NAME}}", "fixture")
    _check("--label defect" in old, "fixture: the installed skill carries the defective --label step")
    stale = _feedback_skill_fixture(root / "stale", old)
    skill = stale.home / ".claude" / "skills" / "feedback" / "SKILL.md"
    advance_to_source_advanced(stale)
    preview = preview_update_instance(stale.request)
    rows = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], preview.data["managed_artifacts"])}
    row = rows["feedback_skill"]
    _check(preview.status == "runtime_preview_ready" and preview.exit_code == 0, f"preview with a stale skill is ready: {preview.status} {preview.data.get('blocked')}")
    _check((data(row, "state"), data(row, "action"), data(row, "conflict")) == ("legacy_matched", "render_whole", None), "the dry-run plan shows the skill replacement as part of the approved plan")
    _check(skill.read_text(encoding="utf-8") == old, "the dry-run wrote nothing")
    applied = apply_update(stale.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(applied.status == "promoted" and applied.exit_code == 0, f"update promoted: {applied.status} {applied.error_kind} {applied.message}")
    refreshed = skill.read_text(encoding="utf-8")
    _check(refreshed.startswith("---\nname: feedback\n") and "--label defect" not in refreshed and "issues/$PARENT_NUMBER/sub_issues" not in refreshed, "the installed skill is now the fixed one, front matter first")
    root_backup = backup_root(stale.paths, stale.record().instance_id, cast(str, applied.data["operation_id"]))
    _check((root_backup / "feedback_skill" / "before.bytes").read_bytes() == old.encode("utf-8"), "the r56 skill was backed up byte-for-byte before the write")
    afters = {item["artifact_id"]: item["after_sha256"] for item in _hydration_after_digests(stale)}
    _check(afters.get("feedback_skill") == file_sha256(skill), "the journaled after digest equals the installed skill")
    _assert_edited_skill_left_alone(root / "edited", old + "\nAlways file as urgent.\n")


def _assert_edited_skill_left_alone(root: Path, edited: str) -> None:
    fixture = _feedback_skill_fixture(root, edited)
    skill = fixture.home / ".claude" / "skills" / "feedback" / "SKILL.md"
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    rows = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], preview.data["managed_artifacts"])}
    row = rows["feedback_skill"]
    _check(preview.status == "runtime_preview_ready" and not preview.data["blocked"], f"an edited skill does not block the update: {preview.status} {preview.data.get('blocked')}")
    _check((data(row, "state"), data(row, "action"), data(row, "conflict")) == ("unknown_origin", "none", None), "the preview reports the edited skill as unknown origin with no action")
    applied = apply_update(fixture.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(applied.status == "promoted", f"the update still promotes: {applied.status} {applied.error_kind}")
    _check(skill.read_text(encoding="utf-8") == edited, "the operator's edited skill is left byte-for-byte")
    root_backup = backup_root(fixture.paths, fixture.record().instance_id, cast(str, applied.data["operation_id"]))
    _check(not (root_backup / "feedback_skill").exists(), "an untouched skill takes no backup")


_FLEET_TEMPLATE = "fleet_functions.zsh.template"
_EXCLUDE_TEMPLATE = "clone_exclude_block.template"
_FLEET_MARKER = "# One function per role the operator chose in Step 4a."
_R56_FLEET = _ROOT / "plugins/github_midwife_plugin/tests/fixtures/hydration_predecessors/fleet_functions_r56.fixture"
_ROLES = "\nclaude-fixture-lead() { _claude_for_fixture Lead }\nclaude-fixture-restart-lead() {\n  tmux kill-session -t =Lead 2>/dev/null\n  _claude_for_fixture Lead\n}\n"


#: The other untracked files every genesis-born clone keeps under ``client/``; ignoring the directory moves them out of the untracked inventory too.
_CLIENT_FILES = {"client/fixture.zsh": "source client/fixture-fleet.zsh\n", "client/bin/claude-fixture": "#!/bin/zsh\nexec claude\n"}


def _r56_fleet_file(controller: str) -> str:
    """An r56 solet's fleet file as hydration wrote it, with the operator's role functions below the marker."""
    return _R56_FLEET.read_text(encoding="utf-8").replace("{{SOLET_NAME}}", "fixture").replace("{{GIT_CONTROLLER_NAME}}", controller) + _ROLES


def _fleet_fixture(root: Path, installed: str | None) -> Fixture:
    """A fixture whose baseline holds the r56 fleet template, whose candidate holds the shipped templates and declares the SHIPPED exclude block and fleet launcher, with ``installed`` at the clone's fleet file."""
    shipped = {row["artifact_id"]: row for row in json.loads((_ROOT / KB / "existing_install_flow.json").read_text())["managed_artifacts"]}
    fixture = build_fixture(
        root,
        document=lambda baseline: bundle_document(baseline, artifacts=[*default_artifacts(), shipped["clone_exclude_block"], shipped["fleet_launcher"]]),
        baseline_extra={f"{TEMPLATES}/{_FLEET_TEMPLATE}": _R56_FLEET.read_bytes()},
        extra_candidate_files={f"{TEMPLATES}/{_FLEET_TEMPLATE}": template_bytes(_FLEET_TEMPLATE), f"{TEMPLATES}/{_EXCLUDE_TEMPLATE}": template_bytes(_EXCLUDE_TEMPLATE)},
    )
    for relative, text in _CLIENT_FILES.items():
        (fixture.target / relative).parent.mkdir(parents=True, exist_ok=True)
        (fixture.target / relative).write_text(text, encoding="utf-8")
    if installed is not None:
        (fixture.target / "client" / "fixture-fleet.zsh").write_text(installed, encoding="utf-8")
    return fixture


def _clone_ignores_client(fixture: Fixture) -> bool:
    return subprocess.run(("git", "-C", str(fixture.target), "check-ignore", "-q", "--no-index", "--", "client/fixture-fleet.zsh"), check=False).returncode == 0


def _assert_fleet_launcher_refresh(root: Path) -> None:
    """r58 hands-off fix: an update refreshes an r56 fleet launcher above the role functions and never blocks on a clone that does not ignore ``client/``."""
    old = _r56_fleet_file("Lead-Git")
    stale = _fleet_fixture(root / "stale", old)
    fleet = stale.target / "client" / "fixture-fleet.zsh"
    exclude = stale.target / ".git" / "info" / "exclude"
    exclude_before = exclude.read_bytes()
    _check(not _clone_ignores_client(stale), "fixture: the clone does not ignore client/")
    advance_to_source_advanced(stale)
    preview = preview_update_instance(stale.request)
    rows = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], preview.data["managed_artifacts"])}
    _check(preview.status == "runtime_preview_ready" and preview.exit_code == 0 and not preview.data["blocked"], f"a clone that does not ignore client/ no longer blocks the update: {preview.status} {preview.data.get('blocked')}")
    _check((data(rows["clone_exclude_block"], "state"), data(rows["clone_exclude_block"], "action")) == ("absent", "append_block"), "the dry-run plan shows the exclude block as an action")
    _check((data(rows["fleet_launcher"], "state"), data(rows["fleet_launcher"], "action"), data(rows["fleet_launcher"], "conflict")) == ("legacy_matched", "render_whole", None), "the dry-run plan shows the launcher refresh as an action")
    _check((fleet.read_text(encoding="utf-8"), exclude.read_bytes()) == (old, exclude_before), "the dry-run wrote nothing")
    applied = apply_update(stale.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(applied.status == "promoted" and applied.exit_code == 0, f"update promoted: {applied.status} {applied.error_kind} {applied.message}")
    refreshed = fleet.read_text(encoding="utf-8")
    _check(refreshed.partition(_FLEET_MARKER)[2] == old.partition(_FLEET_MARKER)[2] and refreshed.count(_FLEET_MARKER) == 1, "role functions and everything below the marker are byte-identical")
    _check('  GIT_CONTROLLER_NAME="Lead-Git" \\' in refreshed.splitlines() and "_tmux_host_for_fixture" in refreshed and "_tmux_host_for_fixture" not in old, "the operator's GIT_CONTROLLER_NAME is kept and the launcher now hosts each role in tmux")
    _check(_clone_ignores_client(stale) and exclude.read_bytes().startswith(exclude_before), "the clone now ignores client/ and its other ignore lines are untouched")
    _check(all((stale.target / relative).read_text(encoding="utf-8") == text for relative, text in _CLIENT_FILES.items()), "the other untracked client files became ignored and were not touched, and the update still promoted")
    _assert_fleet_backups_and_digests(stale, cast(str, applied.data["operation_id"]), old, exclude_before)
    _assert_edited_fleet_launcher_left_alone(root / "edited", old.replace("_claude_for_fixture() {", "export MY_EDIT=1\n_claude_for_fixture() {", 1))
    _assert_absent_fleet_launcher_unaffected(root / "absent")


def _assert_fleet_backups_and_digests(fixture: Fixture, operation: str, old: str, exclude_before: bytes) -> None:
    root_backup = backup_root(fixture.paths, fixture.record().instance_id, operation)
    _check((root_backup / "fleet_launcher" / "before.bytes").read_bytes() == old.encode("utf-8") and (root_backup / "clone_exclude_block" / "before.bytes").read_bytes() == exclude_before, "both files were backed up byte-for-byte before the writes")
    afters = {item["artifact_id"]: item["after_sha256"] for item in _hydration_after_digests(fixture)}
    _check(afters.get("fleet_launcher") == file_sha256(fixture.target / "client" / "fixture-fleet.zsh") and afters.get("clone_exclude_block") == file_sha256(fixture.target / ".git" / "info" / "exclude"), "the journaled after digests equal the written files")


def _assert_edited_fleet_launcher_left_alone(root: Path, edited: str) -> None:
    fixture = _fleet_fixture(root, edited)
    fleet = fixture.target / "client" / "fixture-fleet.zsh"
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    rows = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], preview.data["managed_artifacts"])}
    _check(preview.status == "runtime_preview_ready" and not preview.data["blocked"], f"an edited launcher does not block the update: {preview.status} {preview.data.get('blocked')}")
    _check((data(rows["fleet_launcher"], "state"), data(rows["fleet_launcher"], "action"), data(rows["fleet_launcher"], "conflict")) == ("unknown_origin", "none", None), "the preview reports the edited launcher as unknown origin with no action")
    applied = apply_update(fixture.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(applied.status == "promoted", f"the update still promotes: {applied.status} {applied.error_kind}")
    _check(fleet.read_text(encoding="utf-8") == edited, "the operator's edited launcher is left byte-for-byte")
    root_backup = backup_root(fixture.paths, fixture.record().instance_id, cast(str, applied.data["operation_id"]))
    _check(not (root_backup / "fleet_launcher").exists(), "an untouched launcher takes no backup")


def _assert_absent_fleet_launcher_unaffected(root: Path) -> None:
    fixture = _fleet_fixture(root, None)
    fleet = fixture.target / "client" / "fixture-fleet.zsh"
    advance_to_source_advanced(fixture)
    preview = preview_update_instance(fixture.request)
    rows = {cast(str, data(row, "artifact_id")): row for row in cast(list[Any], preview.data["managed_artifacts"])}
    _check(preview.status == "runtime_preview_ready" and not preview.data["blocked"], f"a clone with no fleet file is not blocked: {preview.status} {preview.data.get('blocked')}")
    _check((data(rows["fleet_launcher"], "state"), data(rows["fleet_launcher"], "action")) == ("absent", "none"), "the preview shows no launcher action")
    applied = apply_update(fixture.request, cast(str, preview.data["runtime_approval_fingerprint"]))
    _check(applied.status == "promoted" and not fleet.exists(), f"the update promotes and creates no fleet file: {applied.status} {applied.error_kind}")


@dataclass(frozen=True)
class _Listing:
    values: tuple[str, ...]


@dataclass(frozen=True)
class _Facts:
    """The three listings ``observe_local_state`` reads, taken straight from Git."""

    tracked_paths: _Listing
    untracked_paths: _Listing
    staged_paths: _Listing


def _carve_facts(target: Path) -> Any:
    status = subprocess.run(("git", "-C", str(target), "status", "--porcelain=v1", "--untracked-files=all"), capture_output=True, text=True, check=True).stdout
    tracked = subprocess.run(("git", "-C", str(target), "ls-files"), capture_output=True, text=True, check=True).stdout.split()
    untracked = tuple(sorted(line[3:] for line in status.splitlines() if line.startswith("?? ")))
    return _Facts(_Listing(tuple(sorted(tracked))), _Listing(untracked), _Listing(()))


def _carve_target(root: Path) -> Path:
    """A committed clone with the untracked files a genesis-born clone keeps: ``client/`` files and an operator's ``notes.txt``."""
    target = root
    (target / "client" / "bin").mkdir(parents=True)
    subprocess.run(("git", "init", "--quiet", str(target)), check=True)
    (target / "README").write_text("r\n", encoding="utf-8")
    subprocess.run(("git", "-C", str(target), "add", "README"), check=True)
    subprocess.run(("git", "-C", str(target), "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "--quiet", "-m", "i"), check=True)
    (target / "client" / "a.zsh").write_text("alpha\n", encoding="utf-8")
    (target / "client" / "bin" / "tool").write_text("#!/bin/zsh\n", encoding="utf-8")
    (target / "notes.txt").write_text("operator notes\n", encoding="utf-8")
    return target


def _carve_admitted(root: Path, mutate: Any, *, exclude: str = "client/\n", declared: frozenset[str] = frozenset({CLONE_EXCLUDE_RELATIVE}), resume: bool = False) -> bool:
    """Whether the per-operation re-baseline admits ``mutate`` made alongside an ignore-file edit (``False`` = ``preservation_violated``).

    ``resume`` passes no in-process observation, as a re-entered update has none.
    """
    target = _carve_target(root)
    before = observe_local_state(target, _carve_facts(target))
    journal = cast(Any, {"local_state": {"current": before.snapshot()}})
    with (target / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
        stream.write(exclude)
    mutate(target)
    after = observe_local_state(target, _carve_facts(target))
    try:
        rebaseline_revision(journal, after, None if resume else before, "op_x", declared, "fixture", target)
    except UpdateFailedError as exc:
        _check(exc.error_kind == "preservation_violated", f"the refusal is preservation_violated: {exc.error_kind}")
        return False
    return True


def _replace_with_symlink(target: Path) -> None:
    (target / "client" / "a.zsh").unlink()
    os.symlink("/etc/hosts", target / "client" / "a.zsh")


def _assert_ignore_carve_out_is_narrow(root: Path) -> None:
    """Only a path that is unchanged and that the clone's own ignore file now covers is admitted, and only when that file was declared."""
    write = lambda text: (lambda target: (target / "client" / "a.zsh").write_text(text, encoding="utf-8"))  # noqa: E731
    _check(_carve_admitted(root / "control", lambda target: None), "control: an ignore-file edit alone is admitted")
    _check(not _carve_admitted(root / "undeclared", lambda target: None, declared=frozenset()), "control: without the declared ignore file the vanished files are a violation")
    _check(not _carve_admitted(root / "same_size", write("omega\n")), "a client file rewritten at the same size is a violation")
    _check(not _carve_admitted(root / "truncated", write("")), "a client file truncated to 0 bytes is a violation")
    _check(not _carve_admitted(root / "truncated_resume", write(""), resume=True), "a truncation is caught on the resume path too")
    _check(not _carve_admitted(root / "symlink", _replace_with_symlink), "a client file replaced by a symlink is a violation")
    _check(not _carve_admitted(root / "deleted", lambda target: (target / "client" / "a.zsh").unlink()), "a deleted client file is a violation")
    _check(not _carve_admitted(root / "widened", lambda target: (target / "notes.txt").write_text("CLOBBERED\n", encoding="utf-8"), exclude="client/\n*.txt\n"), "an edit hidden only by a widened ignore pattern is a violation")
    other = _carve_target(root / "other_source")
    before = observe_local_state(other, _carve_facts(other))
    (other / ".gitignore").write_text("client/\n", encoding="utf-8")
    vanished = ["client/a.zsh", "client/bin/tool"]
    _check(paths_ignored_by_declared_exclude(other, frozenset({CLONE_EXCLUDE_RELATIVE}), vanished, before.state, before) == frozenset(), "a path ignored by any source other than the clone's ignore file is not admitted")
    (other / ".gitignore").unlink()
    with (other / ".git" / "info" / "exclude").open("a", encoding="utf-8") as stream:
        stream.write("client/\n")
    _check(paths_ignored_by_declared_exclude(other, frozenset({CLONE_EXCLUDE_RELATIVE}), [*vanished, "client/gone.zsh", "notes.txt"], before.state, before) == frozenset(vanished), "unchanged paths the ignore file covers are admitted; a path the journal never held and an unignored path are not")


def _assert_fleet_crash_resume(root: Path) -> None:
    """A crash after the hydration writes and before the re-baseline resumes and promotes: the vanished client files are still admitted."""
    old = _r56_fleet_file("Lead-Git")
    fixture = _fleet_fixture(root, old)
    fleet = fixture.target / "client" / "fixture-fleet.zsh"
    exclude = fixture.target / ".git" / "info" / "exclude"
    exclude_before = exclude.read_bytes()
    advance_to_source_advanced(fixture)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["runtime_approval_fingerprint"])
    real = execution_module._Execution.rebaseline_local_state  # noqa: SLF001

    def crash(self: Any, journal: Any, operation_id: str, declared: frozenset[str]) -> Any:
        if CLONE_EXCLUDE_RELATIVE in declared:
            raise SimulatedCrash("after the hydration writes, before the re-baseline")
        return real(self, journal, operation_id, declared)

    with patch.object(execution_module._Execution, "rebaseline_local_state", crash):  # noqa: SLF001
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash injection did not fire")
    _check(exclude.read_bytes() != exclude_before and "_tmux_host_for_fixture" in fleet.read_text(encoding="utf-8"), "both writes landed before the crash")
    resumed = apply_update(fixture.request, fingerprint)
    _check(resumed.status == "promoted", f"the resume promotes: {resumed.status} {resumed.error_kind} {resumed.message}")
    _check(fleet.read_text(encoding="utf-8").partition(_FLEET_MARKER)[2] == old.partition(_FLEET_MARKER)[2], "the resume kept the role functions byte-for-byte")
    _check(all((fixture.target / relative).read_text(encoding="utf-8") == text for relative, text in _CLIENT_FILES.items()), "the other client files are untouched by the resume")


def _assert_strategy_selection(root: Path) -> None:
    forced = build_fixture(root / "forced", document=lambda baseline: bundle_document(baseline, strategy="single_color_required"), router=True)
    advance_to_source_advanced(forced)
    preview = preview_update_instance(forced.request)
    _check(data(preview.data["lifecycle"], "strategy") == "single_color_restart", "single_color_required overrides a visible router")
    unproven = build_fixture(root / "unproven", router=True, roster=("github_midwife_plugin", "macos_self_deployment_plugin"))
    advance_to_source_advanced(unproven)
    preview = preview_update_instance(unproven.request)
    _check(preview.status == "awaiting_user" and data(preview.data["lifecycle"], "unproven_reason") == "lifecycle_strategy_unproven", "router visible but attestation unreachable is unproven")
    missing_roster = build_fixture(root / "roster", roster=("github_midwife_plugin", "plugin_not_in_candidate"))
    advance_to_source_advanced(missing_roster)
    preview = preview_update_instance(missing_roster.request)
    reasons = {data(row, "reason") for row in cast(list[Any], preview.data["blocked"])}
    _check("roster_plugin_absent_in_candidate" in reasons and preview.data["runtime_approval_fingerprint"] is None, "roster plugin absent from the candidate blocks before any write")


def main() -> int:
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        fixture = build_fixture(root / "main")
        advance_to_source_advanced(fixture)
        fingerprint = _assert_preview(fixture)
        _assert_foreign_fingerprint_refused(fixture)
        _assert_runtime_advanced(fixture, fingerprint)
        _assert_idempotent(fixture, fingerprint)
        _assert_partial_rollback(root / "rollback")
        _assert_crash_resume_after_hydration(root / "crash_hydration")
        _assert_crash_between_cas_and_lifecycle_advanced(root / "crash_cas")
        _assert_plist_drift_before_lifecycle(root / "plist_drift")
        _assert_attestation_ordering(root / "attestation")
        _assert_launchctl_failures(root / "launchctl")
        _assert_retry_safe_dependency(root / "retry")
        _assert_d1_destination_rules(root / "d1")
        _assert_feedback_skill_refresh(root / "skill")
        _assert_fleet_launcher_refresh(root / "fleet")
        _assert_ignore_carve_out_is_narrow(root / "carve")
        _assert_fleet_crash_resume(root / "crash_resume")
        _assert_strategy_selection(root / "strategy")
    _check(subprocess.run(("git", "--version"), capture_output=True, check=False).returncode == 0, "git present")
    print(f"update_runtime_execution_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
