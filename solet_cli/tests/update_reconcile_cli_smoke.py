"""Step-6 ``solet-manager reconcile`` and ``doctor`` argument contracts, the abandon/retire boundary (F-RB-2),
the pointer release, JSON/CLI parity, and the guarantee that no "Step-6" placeholder string survives in the
Manager source.

- argument contract: ``reconcile`` takes exactly one of ``--dry-run``/``--yes`` plus an optional
  ``--abandon``/``--release-pointer`` form; ``update`` gains ``--backup-checkpoint``; ``doctor`` takes a name;
- F-RB-2: ``--abandon --yes`` at ``prepared``, ``operation_published``, ``target_fetched`` and
  ``source_applying`` (HEAD == baseline) abandons and releases the pointer; a terminal
  ``blocked@operation_published`` (injected fetch failure) and a ``failed@target_fetched`` document are RETIRED
  (status unchanged, ``retirement`` set, pointer released, the private ref disclosed); a fresh ``--dry-run``
  then produces a new preview; abandon is refused past the fast-forward and whenever HEAD != baseline;
- n5: an instance enrolled on a genuinely divergent branch previews as ``history_diverged`` (no rewind);
- ``--release-pointer --yes`` releases a pointer naming a promoted journal and refuses a live one;
- every form renders through ``manager_cli.main`` with JSON parity.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    CANONICAL,
    ORIGIN_ID,
    Fixture,
    advance_to_source_advanced,
    build_fixture,
    data,
    db_spy,
    enroll,
    expect,
    git,
    runtime_fingerprint,
    seal,
)
from _step6_support import SimulatedCrash, cli_resume, last_update_journal  # noqa: E402
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager import update_promotion  # noqa: E402
from solet_manager.errors import AbandonRefusedError, ManagerError, UpdateBlockedError  # noqa: E402
from solet_manager.manager_cli import build_parser  # noqa: E402
from solet_manager.update_execution import apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_journal import advance_update_journal, read_update_journal, write_update_journal  # noqa: E402
from solet_manager.update_reconcile import abandon_update, preview_reconcile, release_pointer  # noqa: E402

_CHECKS = 0
_SRC = Path(__file__).resolve().parents[1] / "src"


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _assert_parser() -> None:
    parser = build_parser()
    subparsers = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))  # noqa: SLF001
    _check({"doctor", "reconcile", "update"} <= set(cast(dict[str, Any], subparsers.choices)), "doctor and reconcile subcommands exist")
    values = parser.parse_args(["reconcile", "fixture", "--dry-run", "--json"])
    _check((values.dry_run, values.yes, values.abandon, values.release_pointer, values.as_json) == (True, False, False, False, True), "reconcile --dry-run parses")
    values = parser.parse_args(["reconcile", "fixture", "--abandon", "--yes"])
    _check((values.abandon, values.yes) == (True, True), "reconcile --abandon --yes parses")
    values = parser.parse_args(["reconcile", "fixture", "--release-pointer", "--yes"])
    _check((values.release_pointer, values.yes) == (True, True), "reconcile --release-pointer --yes parses")
    values = parser.parse_args(["update", "fixture", "--yes", "--approval-fingerprint", "sha256:" + "a" * 64, "--backup-checkpoint", "fx_1"])
    _check(values.backup_checkpoint == "fx_1", "update --backup-checkpoint parses")
    values = parser.parse_args(["doctor", "fixture", "--json"])
    _check((values.name, values.as_json) == ("fixture", True), "doctor parses")
    for argv in (["reconcile", "fixture"], ["reconcile", "fixture", "--dry-run", "--yes"], ["reconcile", "fixture", "--abandon", "--release-pointer", "--yes"], ["doctor"], ["update", "fixture", "--dry-run", "--unknown"]):
        _check(_exit_code(parser, argv) == 2, f"invalid argv exits 2: {argv}")


def _exit_code(parser: argparse.ArgumentParser, argv: list[str]) -> int | None:
    try:
        parser.parse_args(argv)
    except SystemExit as exc:
        return cast(int | None, exc.code)
    return None


def _stop_at(fixture: Fixture, fingerprint: str, status: str) -> None:
    """Crash the Step-4 apply right after the journal reaches ``status``."""
    real = execution_module.write_update_journal

    def crash(path: Path, previous: Any, next_value: Any) -> None:
        real(path, previous, next_value)
        if next_value["status"] == status:
            raise SimulatedCrash(status)

    with patch.object(execution_module, "write_update_journal", crash):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), f"crash at {status} did not fire")
    _check(last_update_journal(fixture)["status"] == status, f"journal stopped at {status}")


def _assert_abandon_each_status(root: Path) -> None:
    for status in ("prepared", "operation_published", "target_fetched", "source_applying"):
        fixture = build_fixture(root / status)
        fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])
        _stop_at(fixture, fingerprint, status)
        if status == "source_applying":
            # The journal reached source_applying before the merge ran: HEAD is still the baseline.
            _check(git(fixture.target, "rev-parse", "HEAD") == fixture.baseline.commit, "HEAD at baseline before the merge")
        code, payload = cli_resume(fixture, ["reconcile", "fixture", "--abandon", "--yes"])
        _check(code == 0 and payload["status"] == "abandoned" and payload["kind"] == "existing_install_reconcile", f"abandon at {status} through the CLI: {payload['status']} {payload.get('error_kind')}")
        journal = last_update_journal(fixture)
        _check(journal["status"] == "abandoned" and data(journal["result"], "reason_code") == "operator_abandon" and fixture.record().active_operation is None, f"abandoned journal and released pointer at {status}")
        _check(payload["data"]["private_candidate_ref_retained"].startswith("refs/solet/candidates/") and payload["data"]["target_byte_writes"] == 0, "the private ref is disclosed as retained; no target byte changed")
        fresh = preview_update_instance(fixture.request)
        _check(fresh.status == "preview_ready" and fresh.data["approval_fingerprint"] is not None, f"a fresh --dry-run produces a new preview after abandon at {status}")


def _assert_retire_terminal_at_baseline(root: Path) -> None:
    fetch_failed = _blocked_by_unreachable_transport(root / "fetch")
    plan = preview_reconcile(fetch_failed.request)
    _check((plan.status, plan.exit_code, "--abandon --yes" in cast(str, plan.repair)) == ("retire_available", 3, True), "reconcile --dry-run at the baseline points at --abandon --yes")
    result = abandon_update(fetch_failed.request)
    retired = last_update_journal(fetch_failed)
    retirement = cast(dict[str, Any], retired["retirement"])
    _check((result.status, retired["status"], retirement["reason"], retirement["head_observed"]) == ("retired", "blocked", "operator_abandon", fetch_failed.baseline.commit), "terminal at baseline is retired: status unchanged, retirement set")
    _check((fetch_failed.record().active_operation, result.data["pointer_released"]) == (None, True), "the pointer is released against the retirement proof")
    _check(preview_update_instance(fetch_failed.request).status == "preview_ready", "a fresh preview follows the retirement")
    failed_doc = _foreign_failed_document(root / "failed")
    result = abandon_update(failed_doc.request)
    _check((result.status, last_update_journal(failed_doc)["status"], failed_doc.record().active_operation) == ("retired", "failed", None), "failed@target_fetched is retired the same way")


def _blocked_by_unreachable_transport(root: Path) -> Fixture:
    from dataclasses import replace  # noqa: PLC0415

    fixture = build_fixture(root)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])
    fixture.request = replace(fixture.request, transport_url=str(root / "nowhere.git"))
    exc = expect(ManagerError, lambda: apply_update(fixture.request, fingerprint), "unreachable transport accepted")
    journal = last_update_journal(fixture)
    _check((journal["status"], journal["retirement"], fixture.record().active_operation is not None) == ("blocked", None, True), f"an unreachable transport leaves blocked@operation_published with the pointer set ({type(exc).__name__})")
    fixture.request = replace(fixture.request, transport_url=str(fixture.source))
    return fixture


def _foreign_failed_document(root: Path) -> Fixture:
    """A ``failed`` document at ``target_fetched`` no Manager path writes today: retirement must still cover it."""
    fixture = build_fixture(root)
    fingerprint = cast(str, preview_update_instance(fixture.request).data["approval_fingerprint"])
    _stop_at(fixture, fingerprint, "target_fetched")
    record = fixture.record()
    path = fixture.paths.operation_path(record.instance_id, cast(str, record.active_operation and record.active_operation.operation_id))
    current = read_update_journal(path)
    write_update_journal(path, current, advance_update_journal(current, status="failed", stage_id="failed", note="foreign failure", result={"kind": "failed", "reason_code": "fixture_failure", "repair": "n/a"}))
    return fixture


def _assert_refusals(root: Path) -> None:
    fixture = build_fixture(root / "past")
    advance_to_source_advanced(fixture)
    exc = expect(AbandonRefusedError, lambda: abandon_update(fixture.request), "abandon at source_advanced accepted")
    _check("past the fast-forward" in str(exc) and fixture.record().active_operation is not None, "refused with the exact reason past the fast-forward")
    code, payload = cli_resume(fixture, ["reconcile", "fixture", "--abandon", "--yes"])
    _check(code == 3 and payload["error_kind"] == "abandon_refused", "CLI projection of the refusal is exit 3 abandon_refused")
    elsewhere = build_fixture(root / "elsewhere")
    fingerprint = cast(str, preview_update_instance(elsewhere.request).data["approval_fingerprint"])
    _stop_at(elsewhere, fingerprint, "operation_published")
    git(elsewhere.target, "-c", "user.name=x", "-c", "user.email=x@example.invalid", "commit", "--quiet", "--allow-empty", "-m", "moved")
    exc = expect(AbandonRefusedError, lambda: abandon_update(elsewhere.request), "abandon with HEAD elsewhere accepted")
    _check(elsewhere.record().active_operation is not None, "HEAD != baseline: refused, pointer kept")
    live = build_fixture(root / "live")
    fingerprint = cast(str, preview_update_instance(live.request).data["approval_fingerprint"])
    _stop_at(live, fingerprint, "operation_published")
    exc2 = expect(UpdateBlockedError, lambda: release_pointer(live.request), "release of a live pointer accepted")
    _check(cast(UpdateBlockedError, exc2).error_kind == "pointer_not_releasable", "--release-pointer refuses a pointer whose operation is not over")
    exc3 = expect(UpdateBlockedError, lambda: preview_reconcile(live.request), "reconcile --dry-run on a nonterminal accepted")
    _check(cast(UpdateBlockedError, exc3).error_kind == "update_not_terminal", "reconcile --dry-run requires a terminal or doctor_incomplete journal")


def _assert_release_pointer(root: Path) -> None:
    fixture = build_fixture(root)
    advance_to_source_advanced(fixture)
    fingerprint = runtime_fingerprint(fixture)

    def no_release(paths: Any, record: Any) -> Any:
        raise SimulatedCrash("before the release")

    with patch.object(update_promotion, "release_terminal_pointer", no_release):
        expect(SimulatedCrash, lambda: apply_update(fixture.request, fingerprint), "crash did not fire")
    _check(last_update_journal(fixture)["status"] == "promoted" and fixture.record().active_operation is not None, "promoted with the pointer still set")
    code, payload = cli_resume(fixture, ["reconcile", "fixture", "--release-pointer", "--yes"])
    _check(code == 0 and payload["status"] == "pointer_released" and payload["data"]["journal_status"] == "promoted" and fixture.record().active_operation is None, "--release-pointer --yes releases the stale pointer through the CLI")
    code, payload = cli_resume(fixture, ["reconcile", "fixture", "--release-pointer", "--yes"])
    _check(code == 0 and payload["status"] == "no_pointer", "idempotent: no pointer left")
    code, payload = cli_resume(fixture, ["doctor", "fixture"])
    _check(code == 0 and payload["kind"] == "existing_install_doctor" and payload["data"]["contract"]["kind"] == "verified", "doctor through the CLI after the release: verified contract, exit 0")


def _assert_history_diverged(root: Path) -> None:
    """n5: the enrolled baseline is a genuinely divergent branch of the seed history; no HEAD rewind anywhere."""
    fixture = build_fixture(root)
    git(fixture.source, "checkout", "--quiet", "-b", "divergent", fixture.baseline.commit)
    divergent = seal(fixture.source, "9" * 40, "8" * 64, "r1-divergent", {"DIVERGED.md": "divergent history\n"})
    git(fixture.source, "checkout", "--quiet", "main")
    subprocess.run(("git", "-C", str(fixture.target), "fetch", "--quiet", str(fixture.source), "refs/tags/r1-divergent:refs/tags/r1-divergent"), check=True, capture_output=True)
    git(fixture.target, "checkout", "--quiet", "-B", "main", divergent.commit)
    enroll(fixture.paths, fixture.target, divergent, descriptor_digest=fixture.descriptor_digest, contract=fixture.contract)
    preview = preview_update_instance(fixture.request)
    reasons = cast(list[str], cast(dict[str, Any], preview.data["topology"])["reasons"])
    _check(preview.status == "awaiting_user" and "history_diverged" in reasons and preview.data["approval_fingerprint"] is None, f"a divergent enrolled baseline previews as history_diverged: {reasons}")
    _check(git(fixture.target, "rev-parse", "HEAD") == divergent.commit and git(fixture.source, "rev-parse", "HEAD") == fixture.candidate.commit, "no HEAD was rewound to simulate the divergence")
    _check(CANONICAL.startswith("https://") and len(ORIGIN_ID) == 36, "fixture identities intact")


def _assert_no_placeholder_strings() -> None:
    hits = [path for path in sorted(_SRC.rglob("*.py")) if "Step-6" in path.read_text(encoding="utf-8")]
    _check(not hits, f"no 'Step-6' placeholder survives in the Manager source: {[str(item.relative_to(_SRC)) for item in hits]}")
    reconciliation_hits = [path for path in sorted(_SRC.rglob("*.py")) if "reconciliation is required" in path.read_text(encoding="utf-8")]
    _check(not reconciliation_hits, f"no 'reconciliation is required' placeholder survives: {reconciliation_hits}")


def main() -> int:
    _assert_parser()
    _assert_no_placeholder_strings()
    with db_spy(), TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_abandon_each_status(root / "abandon")
        _assert_retire_terminal_at_baseline(root / "retire")
        _assert_refusals(root / "refuse")
        _assert_release_pointer(root / "release")
        _assert_history_diverged(root / "diverged")
    _check(json.dumps({"ok": True}) == '{"ok": true}', "json module sanity")
    print(f"update_reconcile_cli_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
