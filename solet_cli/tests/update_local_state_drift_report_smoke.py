"""``source_transition_incomplete`` for changed local state names every drifted path with its sub-case and repair (iss_46de4f1d, public #87).

Hermetic: a real two-commit Git repository in a temporary directory (a predecessor and a candidate that adds two files), the approval snapshot
taken while the source stage had not yet advanced, then ``verify_local_state`` called after HEAD moved.  The check itself is unchanged, and
the first leg proves it: with the operator's files still displaced nothing is refused.  Then, with every displaced file put back:

- one refusal names every drifted path (not the first), each with the lead phrase older releases printed;
- an untracked local-only file, a path now tracked at the new HEAD, a path now missing and a preserved local edit that changed again
  each carry their own sub-case and repair, and only the last one is never told to restore from HEAD;
- the repair keeps ``Do not reset`` and says the checkout is read-only between ``source_advanced`` and the runtime ``--yes``;
- the ``commitment_mismatch`` pseudo-path is not a path and is rendered once, without a probe;
- forty drifted paths list twenty and count the rest.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src"), str(_ROOT), str(_ROOT / "solet_cli" / "tests")]
import solet_manager.update_local_state as update_local_state  # noqa: E402
from existing_install_inspection_contract_smoke import _facts  # noqa: E402
from solet_manager.errors import SourceTransitionIncompleteError  # noqa: E402
from solet_manager.existing_install_inspection import ExistingInstallFacts, ObservationAvailability, ObservedPaths, RepositoryRelation  # noqa: E402
from solet_manager.local_state import observe_local_state  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402

_CHECKS = 0
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_OBSERVED = ObservationAvailability.OBSERVED


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _git(target: Path, *args: str) -> str:
    done = subprocess.run(("git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args), cwd=target, capture_output=True, text=True, env=_ENV, check=False)
    if done.returncode:
        raise AssertionError(f"git {' '.join(args)}: {done.stderr}")
    return done.stdout.strip()


def _write(target: Path, path: str, text: str) -> None:
    (target / path).write_text(text, encoding="utf-8")


def _facts_of(tracked: tuple[str, ...], untracked: tuple[str, ...]) -> ExistingInstallFacts:
    return replace(_facts(RepositoryRelation.CANONICAL), tracked_paths=ObservedPaths(_OBSERVED, tracked), untracked_paths=ObservedPaths(_OBSERVED, untracked))


def _journal(target: Path, tracked: tuple[str, ...], untracked: tuple[str, ...]) -> dict[str, JsonValue]:
    """The journal a ``source_advanced`` update holds: the local state as approval observed it."""
    current = observe_local_state(target, _facts_of(tracked, untracked)).snapshot()
    return {"status": "source_advanced", "local_state": {"current": current}, "runtime_operations": []}


def _refusal(target: Path, journal: dict[str, JsonValue], tracked: tuple[str, ...], untracked: tuple[str, ...]) -> SourceTransitionIncompleteError | None:
    try:
        update_local_state.verify_local_state(target, _facts_of(tracked, untracked), journal, None)
    except SourceTransitionIncompleteError as exc:
        return exc
    return None


def _build(root: Path) -> tuple[Path, dict[str, JsonValue], dict[str, JsonValue]]:
    """A target at the predecessor with the approval state written, then advanced to the candidate; returns the two journals."""
    target = root / "target"
    target.mkdir()
    _git(target, "init", "-q")
    _write(target, "edited.py", "v0\n")
    _write(target, "base.txt", "base\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "predecessor")
    predecessor = _git(target, "rev-parse", "HEAD")
    _write(target, "new_in_candidate.py", "candidate\n")
    _write(target, "moved.txt", "shipped\n")
    _git(target, "add", "-A")
    _git(target, "commit", "-q", "-m", "candidate")
    candidate = _git(target, "rev-parse", "HEAD")
    _git(target, "checkout", "-q", predecessor)
    # Approval: one preserved local edit, two local-only files the candidate is about to ship or lose; the displaced files are absent.
    _write(target, "edited.py", "v1\n")
    _write(target, "moved.txt", "shipped\n")
    _write(target, "gone.txt", "gone\n")
    _write(target, "same.txt", "aaaa\n")
    displaced = _journal(target, ("edited.py",), ())
    with_inventory = _journal(target, ("edited.py",), ("gone.txt", "moved.txt", "same.txt"))
    _git(target, "checkout", "-q", "-f", candidate)
    _write(target, "edited.py", "v1\n")
    return target, displaced, with_inventory


def _check_control_and_every_path(target: Path, displaced: dict[str, JsonValue], with_inventory: dict[str, JsonValue]) -> None:
    _check(_refusal(target, displaced, ("edited.py",), ()) is None, "control: with the displaced files still displaced the check passes, exactly as before")
    _check_each_condition_alone(target, displaced)
    _check_service_write_allowance(target, displaced)
    (target / "gone.txt").unlink()
    _write(target, "keep.txt", "keep\n")
    _write(target, "new_in_candidate.py", "mine\n")
    _write(target, "edited.py", "v2\n")
    _write(target, "same.txt", "longer than approved\n")
    exc = _refusal(target, with_inventory, ("edited.py", "new_in_candidate.py"), ("keep.txt", "same.txt"))
    _check(exc is not None, "strictness: every condition that raised before still raises")
    refused = cast(SourceTransitionIncompleteError, exc)
    _check((refused.error_kind, refused.exit_code) == ("source_transition_incomplete", 3), f"error_kind and exit code are unchanged: {refused.error_kind} {refused.exit_code}")
    _check_sub_cases(str(refused), refused.repair or "")


def _has(text: str, *needles: str) -> bool:
    return all(needle in text for needle in needles)


def _check_sub_cases(message: str, repair: str) -> None:
    _check(_has(message, "edited.py", "new_in_candidate.py", "keep.txt", "moved.txt", "gone.txt", "same.txt"), f"the message names every drifted path, not the first: {message}")
    _check(_has(message, "preserved tracked path edited.py no longer matches the recorded local state (expected_sha256=", "committed local state changed at keep.txt (inventory_changed)"), "each path keeps the lead phrase older releases printed, byte for byte")
    _check(_has(message, "keep.txt (inventory_changed): untracked local-only file") and _has(repair, "`mv keep.txt <scratch>`"), f"a local-only file is labelled and told to leave the checkout again: {repair}")
    _check(_has(message, "new_in_candidate.py no longer matches the recorded local state", "tracked at the new HEAD, not a preserved edit") and _has(repair, "restore -- new_in_candidate.py`"), "a path tracked at the new HEAD is labelled and told to be restored from HEAD")
    _check(_has(message, "moved.txt (inventory_changed): now tracked at the new HEAD") and _has(repair, "restore -- moved.txt`"), "a local-only file the candidate now ships is tracked at HEAD, found by the read-only probe")
    _check(_has(message, "gone.txt (inventory_changed): in the approved inventory, now missing") and _has(repair, "gone.txt: put it back where it was"), "a missing path is told to be put back")
    _check(_has(message, "a preserved local edit changed again") and "restore -- edited.py" not in repair, "a preserved edit that changed again is never told to be restored from HEAD")
    _check(_has(message, "same.txt (inventory_changed): in the approved inventory, changed since") and _has(repair, "same.txt: put it back exactly as approved (kind file, mode 0644, size 5)") and "mv same.txt" not in repair, f"a present approved path whose kind, mode or size moved is told to be put back, never moved out: {repair}")
    _check(_has(repair, "Do not reset", "read-only", "source_advanced"), f"the repair keeps Do not reset and names the read-only window: {repair}")


def _check_each_condition_alone(target: Path, displaced: dict[str, JsonValue]) -> None:
    """Strictness, path by path: each tier's condition refuses on its own, and the same path put back as approved does not."""
    _write(target, "keep.txt", "keep\n")
    _check(_refusal(target, displaced, ("edited.py",), ("keep.txt",)) is not None, "strictness: an untracked file restored after approval still refuses alone")
    (target / "keep.txt").unlink()
    _write(target, "new_in_candidate.py", "mine\n")
    _check(_refusal(target, displaced, ("edited.py", "new_in_candidate.py"), ()) is not None, "strictness: a tracked path restored over HEAD's bytes still refuses alone")
    _git(target, "checkout", "-q", "--", "new_in_candidate.py")
    _write(target, "edited.py", "v2\n")
    _check(_refusal(target, displaced, ("edited.py",), ()) is not None, "strictness: a preserved edit that changed again still refuses alone")
    _write(target, "edited.py", "v1\n")
    _check(_refusal(target, displaced, ("edited.py",), ()) is None, "control: with every path as approved again nothing is refused")


def _check_service_write_allowance(target: Path, displaced: dict[str, JsonValue]) -> None:
    """The B7 allowance leg: only under a service-write status is a new ``knowledge_bases/<p>`` link the solet's own, and nothing else rides along with it."""
    (target / "plugins" / "foo" / "knowledge_base").mkdir(parents=True)
    (target / "knowledge_bases").mkdir()
    os.symlink("../plugins/foo/knowledge_base", target / "knowledge_bases" / "foo")
    link, running, tracked = "knowledge_bases/foo", {**displaced, "status": "runtime_reconciling"}, ("edited.py",)
    report = update_local_state.verify_local_state(target, _facts_of(tracked, (link,)), running, None)
    _check([item["path"] for item in report.additions] == [link], "runtime_reconciling: an admitted knowledge_bases link alone passes and is disclosed as a service write")
    _check(_refusal(target, displaced, tracked, (link,)) is not None, "strictness: the same admitted link under source_advanced still refuses (the allowance is status-gated)")
    os.symlink("../elsewhere", target / "knowledge_bases" / "bar")
    wrong = _refusal(target, running, tracked, (link, "knowledge_bases/bar"))
    _check(wrong is not None and _has(str(wrong), "knowledge_bases/bar") and "knowledge_bases/foo" not in str(wrong), f"strictness: under runtime_reconciling a wrong-target link still refuses, and only it is named: {wrong}")
    _write(target, "keep.txt", "keep\n")
    beside = _refusal(target, running, tracked, (link, "keep.txt"))
    _check(beside is not None and _has(str(beside), "keep.txt") and "knowledge_bases/foo" not in str(beside), f"strictness: under runtime_reconciling a restored untracked file beside an admitted link still refuses: {beside}")
    (target / "keep.txt").unlink()
    for name in ("foo", "bar"):
        (target / "knowledge_bases" / name).unlink()
    (target / "knowledge_bases").rmdir()
    shutil.rmtree(target / "plugins" / "foo")


def _check_preserved_edit_alone(target: Path) -> None:
    exc = _refusal(target, _journal_with_edit_only(target), ("edited.py",), ())
    _check(exc is not None and "a preserved local edit changed again" in str(exc), "a preserved edit that changed again is refused and labelled")
    repair = cast(SourceTransitionIncompleteError, exc).repair or ""
    _check("git restore" not in repair and "restore" not in repair.replace("never restore it from HEAD", ""), f"its repair never names git restore: {repair}")
    _check("sha256 " in repair and "edited.py: put back the bytes the commitment recorded" in repair, "it says to put back the recorded bytes")
    _check("Do not reset" in repair, "and keeps Do not reset")


def _journal_with_edit_only(target: Path) -> dict[str, JsonValue]:
    """The approval state in which the edit was v1; the file now reads v2."""
    _write(target, "edited.py", "v1\n")
    journal = _journal(target, ("edited.py",), ())
    _write(target, "edited.py", "v2\n")
    return journal


def _check_commitment_mismatch(target: Path) -> None:
    _write(target, "edited.py", "v1\n")
    _write(target, "same.txt", "aaaa\n")
    journal = _journal(target, ("edited.py",), ("same.txt",))
    _write(target, "same.txt", "bbbb\n")
    exc = _refusal(target, journal, ("edited.py",), ("same.txt",))
    message = str(exc)
    _check(exc is not None and "committed local state changed at commitment_mismatch (inventory_changed)" in message and message.count("commitment_mismatch") == 1 and ": untracked" not in message, f"the pseudo-path is rendered once, unclassified: {message}")
    _check("Do not reset" in (cast(SourceTransitionIncompleteError, exc).repair or ""), "and keeps Do not reset")
    _write(target, "same.txt", "aaaa\n")
    _check(_refusal(target, journal, ("edited.py",), ("same.txt",)) is None, "control: put back byte for byte, the same check passes")
    (target / "same.txt").unlink()


def _check_listing_cap(target: Path) -> None:
    journal = _journal(target, ("edited.py",), ())
    names = tuple(f"many_{index:02d}.txt" for index in range(40))
    for name in names:
        _write(target, name, "x\n")
    exc = _refusal(target, journal, ("edited.py",), names)
    message, repair = str(exc), (cast(SourceTransitionIncompleteError, exc).repair or "")
    _check(exc is not None and message.count("many_") == 20 and "(+20 more, run `git -C " in message, f"forty drifted paths list twenty and count the rest: {message.count('many_')} {message[-120:]}")
    _check(repair.count("mv many_") == 20 and "(+20 more" in repair, "the repair lists the same twenty")
    _check(len(message) + len(repair) < 12000, "the refusal stays bounded")
    for name in names:
        (target / name).unlink()


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        target, displaced, with_inventory = _build(Path(temporary).resolve())
        _check_control_and_every_path(target, displaced, with_inventory)
        _check_preserved_edit_alone(target)
        _check_commitment_mismatch(target)
        _check_listing_cap(target)
    print(f"update_local_state_drift_report_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
