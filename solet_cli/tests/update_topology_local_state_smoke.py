"""Pure-function coverage of the Step-7 local-state frontier (design section 10, "Tests -- new").

``tracked_overlap`` over all four transition statuses x three relations x
case-fold; ``parse_raw_diff`` over mode/type/symlink/delete rows and malformed
input; ``executed_code_roots`` with and without a candidate, the roster case,
and byte-equality of the Manager's ``REQUIRED_DISTRIBUTIONS`` with the bootstrap
adapter's; the commitment (order-independence, symlink-by-target digest,
``unread`` for a protected path, a one-byte change flips it); the git-metadata
and shape rules; the B7 allowance predicate; the six host-software rows through
injected seams; the v5 journal's revision writer and its closed shapes; and the
closed Step-7 reason vocabulary with ``tracked_state_present`` retired.
"""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(_ROOT / "solet_cli" / "src"), str(_ROOT / "solet_setup_contracts" / "src"), str(_ROOT / "plugins" / "github_midwife_plugin" / "src"), str(_ROOT), str(_ROOT / "solet_cli" / "tests")]
from existing_install_inspection_contract_smoke import _classification_witnesses  # noqa: E402
from solet_manager import errors  # noqa: E402
from solet_manager.errors import StateConflictError, StateError  # noqa: E402
from solet_manager.executed_code import BOOTSTRAP_ROOTS, RosterUnreadableError, executed_code_roots  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ExistingInstallClass,
    ExistingInstallFacts,
    ObservationAvailability,
    ObservedPaths,
    ObservedRawRows,
    RawRow,
)
from solet_manager.existing_solet_diagnostics import DiagnosticStatus  # noqa: E402
from solet_manager.host_software import HostCheck, host_checks, host_requirement_reason  # noqa: E402
from solet_manager.local_state import (  # noqa: E402
    UNREAD_DIGEST,
    ObservedEntry,
    ObservedLocalState,
    allowed_service_write,
    commitment,
    compare,
    observe_local_state,
    snapshot_from_journal,
)
from solet_manager.update_journal import (  # noqa: E402
    UPDATE_JOURNAL_SCHEMA_VERSION,
    advance_update_journal,
    create_update_journal,
    record_local_state_revision,
    write_update_journal,
)
from solet_manager.update_runtime_plan import REQUIRED_DISTRIBUTIONS, RuntimeSeams  # noqa: E402
from solet_manager.update_topology import (  # noqa: E402
    OverlapRow,
    executed_code_overlap,
    git_metadata_paths,
    local_state_fact_reasons,
    parse_raw_diff,
    shape_changed_rows,
    tracked_overlap,
)

from bootstrap_adapter.dependency import REQUIRED_DISTRIBUTIONS as ADAPTER_REQUIRED  # noqa: E402

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


def _expect(kind: type[Exception], action: Any, label: str) -> None:
    try:
        action()
    except kind:
        _check(True, label)
    else:
        raise AssertionError(label)


def _assert_overlap() -> None:
    tracked = ("root_manifest.yaml", "docs/guide.md", "NOTICE")
    for status in ("A", "D", "M", "T"):
        rows = tracked_overlap(tracked, ((status, "root_manifest.yaml"), (status, "docs/guide.md/child"), (status, "docs")), case_insensitive=False)
        _check({(row.reason, row.path, row.candidate_path) for row in rows} == {("tracked_overlap_present", "root_manifest.yaml", "root_manifest.yaml"), ("tracked_overlap_present", "docs/guide.md", "docs/guide.md/child"), ("tracked_overlap_present", "docs/guide.md", "docs")}, f"three relations under status {status}")
    _check(tracked_overlap(tracked, (("M", "README.md"),), case_insensitive=False) == (), "a disjoint transition overlaps nothing")
    folded = tracked_overlap(("Notice",), (("M", "NOTICE"),), case_insensitive=True)
    _check(folded == (OverlapRow("casefold_collision", "Notice", "NOTICE", "M"),), f"case-fold overlap on an ignorecase target: {folded}")
    _check(tracked_overlap(("Notice",), (("M", "NOTICE"),), case_insensitive=False) == (), "no case-fold overlap on a case-sensitive target")
    _check(tracked_overlap(tracked, (("X", "root_manifest.yaml"),), case_insensitive=False) == (), "an unknown status is ignored by the detector")


def _assert_raw_diff() -> None:
    raw = b":100644 100644 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0NOTICE\0:100644 100755 " + b"a" * 40 + b" " + b"b" * 40 + b" M\0bin/tool\0:100644 000000 " + b"a" * 40 + b" " + b"0" * 40 + b" D\0gone\0:100644 120000 " + b"a" * 40 + b" " + b"c" * 40 + b" T\0link\0"
    rows = parse_raw_diff(raw)
    _check([(row.status, row.old_mode, row.new_mode, row.path) for row in rows] == [("M", "100644", "100644", "NOTICE"), ("M", "100644", "100755", "bin/tool"), ("D", "100644", "000000", "gone"), ("T", "100644", "120000", "link")], f"raw rows parsed: {rows}")
    changed = shape_changed_rows(rows)
    _check([row.path for row in changed] == ["bin/tool", "gone", "link"], f"content-only M survives, chmod/delete/retype are shape changes: {changed}")
    _check(parse_raw_diff(b"") == (), "an empty listing parses to no rows")
    for malformed in (b":100644 100644 a b M\0NOTICE", b":100644 100644 a b M\0\0", b"::100644 100644 100644 a b c U\0x\0", b":100644 100644 a b Z\0x\0", b"100644 100644 a b M\0x\0"):
        _expect(ValueError, lambda m=malformed: parse_raw_diff(m), f"malformed raw listing fails loud: {malformed!r}")


def _assert_git_metadata_and_facts() -> None:
    _check(git_metadata_paths(("NOTICE", "docs/.gitattributes", ".gitignore"), ("plugins/x/.gitmodules", ".gitignore", "notes.md")) == (".gitignore", "docs/.gitattributes", "plugins/x/.gitmodules"), "attributes/modules at any depth and a tracked root .gitignore; the untracked root .gitignore is admitted")
    _check(executed_code_overlap(("bootstrap.py", "bootstrap_adapter/x.py", "ananta/src/a.py", "docs/a.md", "bootstrap.pyc"), ("bootstrap.py", "bootstrap_adapter/", "ananta/")) == ("ananta/src/a.py", "bootstrap.py", "bootstrap_adapter/x.py"), "executed-code overlap is exact for files and prefix for directory roots")
    base = _classification_witnesses()[ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE]
    observed = ObservationAvailability.OBSERVED
    _check(local_state_fact_reasons(base) == (), "a clean observed tree has no fact reasons")
    unobserved = replace(base, tracked_entries=ObservedRawRows(ObservationAvailability.UNKNOWN, ()))
    _check(local_state_fact_reasons(unobserved) == ("local_state_unobserved",), "an unobserved raw-diff probe is local_state_unobserved")
    row = RawRow("100644", "100644", "a" * 40, "b" * 40, "M", "NOTICE")
    every = replace(base, staged_paths=ObservedPaths(observed, ("NOTICE",)), tracked_paths=ObservedPaths(observed, ("NOTICE", ".gitattributes")), tracked_entries=ObservedRawRows(observed, (replace(row, new_mode="100755"),)))
    _check(local_state_fact_reasons(every) == ("staged_changes_present", "tracked_shape_changed", "git_metadata_present"), f"the three fact reasons in closed order: {local_state_fact_reasons(every)}")


def _assert_executed_code_roots(root: Path) -> None:
    target = root / "target"
    (target / "profile" / "config").mkdir(parents=True)
    _check(REQUIRED_DISTRIBUTIONS == ADAPTER_REQUIRED, "the Manager's REQUIRED_DISTRIBUTIONS copy is byte-equal to the bootstrap adapter's")
    roots = executed_code_roots(target, None)
    _check(roots == tuple(sorted({*BOOTSTRAP_ROOTS, *(f"{relative}/" for _, relative in REQUIRED_DISTRIBUTIONS)})), f"an absent roster names no plugins: {roots}")
    (target / "profile" / "config" / "manifest.yaml").write_text("plugins:\n- github_midwife_plugin\n- my_plugin\n", encoding="utf-8")
    roots = executed_code_roots(target, None)
    _check("plugins/my_plugin/" in roots and "plugins/github_midwife_plugin/" in roots and "bootstrap.py" in roots and "bootstrap_adapter/" in roots and "ananta/" in roots, f"roster plugins, bootstrap and the required distributions are roots: {roots}")
    _check(all(not path.startswith(roots) for path in ("root_manifest.yaml", "AGENTS.md", "NOTICE", "docs/x", "workbench/x", ".gitignore", ".solet/genesis.json", "deployment/x")), "the real-clone shape stays outside every root")
    (target / "profile" / "config" / "manifest.yaml").write_text("plugins:\n- \n", encoding="utf-8")
    _expect(RosterUnreadableError, lambda: executed_code_roots(target, None), "an invalid roster is RosterUnreadableError")


def _assert_commitment(root: Path) -> None:
    a = ObservedEntry("b.txt", "file", "0644", 3, "1" * 64)
    b = ObservedEntry("a.txt", "file", "0644", 3, "2" * 64)
    _check(commitment((a, b)) == commitment((b, a)), "the commitment is order-independent")
    _check(commitment(()) is None, "an empty committed set has a null commitment")
    _check(commitment((a, b)) != commitment((a, replace(b, digest="3" * 64))), "a one-entry digest change flips the commitment")
    target, facts = _commitment_tree(root / "tree")
    state = _assert_observation(target, facts)
    _assert_compare_deltas(target, facts, state)


def _commitment_tree(target: Path) -> tuple[Path, ExistingInstallFacts]:
    """A tree with one tracked edit, two committed files, a KB symlink and one surface file, plus the facts naming them."""
    (target / "knowledge_bases").mkdir(parents=True)
    (target / "plugins" / "kb_plugin" / "knowledge_base").mkdir(parents=True)
    (target / "notes.md").write_text("notes\n", encoding="utf-8")
    (target / "secret.pem").write_text("secret\n", encoding="utf-8")
    (target / "profile" / "config").mkdir(parents=True)
    (target / "profile" / "config" / "identity.json").write_text("{}\n", encoding="utf-8")
    (target / "knowledge_bases" / "kb_plugin").symlink_to("../plugins/kb_plugin/knowledge_base")
    (target / "NOTICE").write_text("notice\n", encoding="utf-8")
    base = _classification_witnesses()[ExistingInstallClass.CLEAN_FAST_FORWARD_SEED_CLONE]
    observed = ObservationAvailability.OBSERVED
    facts = replace(base, tracked_paths=ObservedPaths(observed, ("NOTICE",)), untracked_paths=ObservedPaths(observed, ("notes.md", "secret.pem", "profile/config/identity.json", "knowledge_bases/kb_plugin")))
    return target, facts


def _assert_observation(target: Path, facts: ExistingInstallFacts) -> ObservedLocalState:
    state = observe_local_state(target, facts)
    by_path = {entry.path: entry for entry in state.committed}
    _check(set(by_path) == {"notes.md", "secret.pem", "knowledge_bases/kb_plugin"}, f"everything outside profile/ is committed: {sorted(by_path)}")
    _check([entry.path for entry in state.surface] == ["profile/config/identity.json"], "profile/ is the surface")
    _check(by_path["secret.pem"].digest == UNREAD_DIGEST and by_path["secret.pem"].size == 7, "a secret-suffixed entry is inventoried but never read")
    link = by_path["knowledge_bases/kb_plugin"]
    _check(link.kind == "symlink" and link.digest == hashlib.sha256(b"../plugins/kb_plugin/knowledge_base").hexdigest(), "a symlink is digested by its stored target string")
    tracked = state.state.preserved_tracked_paths[0]
    _check(tracked[0] == "NOTICE" and len(tracked[1]) == 64, "a tracked modification carries its sha256 and size")
    snapshot = state.snapshot()
    _check("committed_inventory" in snapshot and all("digest" not in row for row in cast(list[dict[str, Any]], snapshot["committed_inventory"])), "the journal snapshot carries the inventory, never per-entry digests")
    return state


def _assert_compare_deltas(target: Path, facts: ExistingInstallFacts, state: ObservedLocalState) -> None:
    reloaded = snapshot_from_journal(state.snapshot())
    unchanged = compare(reloaded, state)
    _check(unchanged.hard == () and unchanged.surface == (), "an unchanged tree compares equal to its own snapshot")
    (target / "notes.md").write_text("nates\n", encoding="utf-8")
    moved = observe_local_state(target, facts)
    delta = compare(reloaded, moved, state)
    _check(delta.committed == ("notes.md",) and delta.tracked == (), f"a same-size byte change is named with the in-process observation: {delta}")
    _check(compare(reloaded, moved).committed == ("commitment_mismatch",), "without the previous observation a same-size change is reported as commitment_mismatch, never guessed")
    (target / "profile" / "config" / "identity.json").write_text('{"x": 1}\n', encoding="utf-8")
    delta = compare(reloaded, observe_local_state(target, facts), state)
    _check(delta.surface == ("profile/config/identity.json",), "a surface change is disclosed, not a hard-tier delta")


def _assert_service_write(root: Path) -> None:
    target = root / "svc"
    (target / "knowledge_bases").mkdir(parents=True)
    (target / "plugins" / "new_plugin" / "knowledge_base").mkdir(parents=True)
    (target / "plugins" / "other" / "knowledge_base").mkdir(parents=True)
    previous = frozenset({"knowledge_bases/existing"})
    link = target / "knowledge_bases" / "new_plugin"
    link.symlink_to("../plugins/new_plugin/knowledge_base")
    _check(allowed_service_write(target, "knowledge_bases/new_plugin", previous) is not None, "the exact _create_kb_symlink write is admitted")
    _check(allowed_service_write(target, "knowledge_bases/new_plugin", previous | {"knowledge_bases/new_plugin"}) is None, "a pre-existing entry is never a creation")
    link.unlink()
    link.symlink_to("../plugins/other/knowledge_base")
    _check(allowed_service_write(target, "knowledge_bases/new_plugin", previous) is None, "a link whose name does not match its plugin is refused (round-4 note 1)")
    link.unlink()
    link.write_text("not a link\n", encoding="utf-8")
    _check(allowed_service_write(target, "knowledge_bases/new_plugin", previous) is None, "a regular file under knowledge_bases/ is refused")
    link.unlink()
    link.symlink_to("/etc/passwd")
    _check(allowed_service_write(target, "knowledge_bases/new_plugin", previous) is None, "an absolute or out-of-target link is refused")
    link.unlink()
    (target / "knowledge_bases" / "nested").mkdir()
    (target / "knowledge_bases" / "nested" / "new_plugin").symlink_to("../../plugins/new_plugin/knowledge_base")
    _check(allowed_service_write(target, "knowledge_bases/nested/new_plugin", previous) is None, "only a direct child of knowledge_bases/ is admitted")
    _check(allowed_service_write(target, "workbench/new_plugin", previous) is None, "anything elsewhere is refused")


def _assert_host_rows(root: Path) -> None:
    target = root / "hosttarget"
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / ".venv" / "pyvenv.cfg").write_text("home = x\n", encoding="utf-8")
    (target / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n", encoding="utf-8")
    bridge = target / ".venv" / "bin" / "solet-bridge"
    bridge.write_text("#!/bin/sh\n", encoding="utf-8")
    bridge.chmod(0o755)
    seams = RuntimeSeams(resolve_base_python=lambda: Path("/fake/python3.13"), which=lambda name: f"/fake/{name}" if name == "tmux" else None)
    _assert_present_host(seams, target)
    _assert_absent_and_unknown_host(seams, target)
    _assert_instance_rows(seams, target, bridge)


def _rows(seams: RuntimeSeams, target: Path) -> dict[str, HostCheck]:
    return {row.check_id: row for row in host_checks(seams, target)}


def _assert_present_host(seams: RuntimeSeams, target: Path) -> None:
    rows = _rows(seams, target)
    statuses = [row.status.value for row in rows.values()]
    _check(statuses == ["verified", "verified", "verified", "missing", "verified", "missing"] or rows["homebrew_present"].status is DiagnosticStatus.VERIFIED, f"present/absent rows are verified/missing, never unknown: {[(k, v.status.value) for k, v in rows.items()]}")
    _check(rows["tmux_present"].status is DiagnosticStatus.VERIFIED and rows["postgresql_client_present"].status is DiagnosticStatus.MISSING, "which decides tmux/psql")
    _check(host_requirement_reason(tuple(rows.values())) is None, "a present host python is no refusal")


def _assert_absent_and_unknown_host(seams: RuntimeSeams, target: Path) -> None:
    absent = _rows(replace(seams, resolve_base_python=lambda: None), target)
    _check(absent["host_python_313"].status is DiagnosticStatus.MISSING and absent["host_python_313"].reason == "host_python_313_absent", "an absent host python is missing, not unknown")
    _check(host_requirement_reason(tuple(absent.values())) == "host_requirement_missing", "an absent host python refuses the preview")

    def raising() -> Path | None:
        raise OSError("probe failed")

    unknown = _rows(replace(seams, resolve_base_python=raising), target)
    _check(unknown["host_python_313"].status is DiagnosticStatus.UNKNOWN, "a raising probe is unknown")
    _check(host_requirement_reason(tuple(unknown.values())) == "host_requirement_unknown", "an unknown host python refuses as unknown")


def _assert_instance_rows(seams: RuntimeSeams, target: Path, bridge: Path) -> None:
    python = target / ".venv" / "bin" / "python3"
    python.unlink()
    python.symlink_to(target / ".venv" / "gone")
    dangling = _rows(seams, target)["instance_python"]
    _check(dangling.status is DiagnosticStatus.MISSING and cast(dict[str, Any], dangling.observed)["dangling"] is True, "a dangling instance interpreter is missing with dangling=true")
    python.unlink()
    gone = _rows(seams, target)["instance_python"]
    _check(gone.status is DiagnosticStatus.MISSING and gone.reason == "instance_interpreter_absent", "an absent instance interpreter is missing")
    bridge.chmod(0o644)
    _check(_rows(seams, target)["instance_bridge_cli"].status is DiagnosticStatus.MISSING, "a non-executable bridge CLI is missing")


def _journal() -> dict[str, Any]:
    return cast(dict[str, Any], create_update_journal(
        operation_id="opr_" + "a" * 32, instance_id="ins_" + "b" * 32, fingerprint="sha256:" + "c" * 64, baseline_commit="1" * 40, baseline_tree="2" * 40, branch="main",
        candidate_descriptor_digest="sha256:" + "d" * 64, candidate_commit="e" * 40, candidate_tree="f" * 40, candidate_tag="r2", candidate_contract_digest="sha256:" + "9" * 64, receipt_digest="sha256:" + "0" * 64,
        planned_actions=("target.fetch_exact_candidate", "target.fast_forward_exact_candidate"), timestamp="2026-09-19T00:00:00Z",
        local_state={"preserved_tracked_paths": [{"path": "NOTICE", "sha256": "a" * 64, "size": 3}], "committed_inventory": [{"path": ".gitignore", "kind": "file", "mode": "0644", "size": 9}], "local_state_commitment": "sha256:" + "b" * 64, "preserved_surface": [{"path": "profile/config/identity.json", "kind": "file", "mode": "0644", "size": 2}]},
    ))


def _assert_journal_revisions(root: Path) -> None:
    value = _journal()
    _check(value["schema_version"] == UPDATE_JOURNAL_SCHEMA_VERSION == 5 and value["local_state"]["baseline"] == value["local_state"]["current"] and value["local_state"]["revisions"] == [], "a v5 journal starts with current == baseline and no revisions")
    current = dict(value["local_state"]["current"])
    current["preserved_tracked_paths"] = [{"path": "NOTICE", "sha256": "c" * 64, "size": 4}]
    rebaselined = record_local_state_revision(value, revision={"operation_id": "migration_solet_rename", "paths": ["NOTICE"], "before": {"NOTICE": "a" * 64}, "after": {"NOTICE": "c" * 64}}, current=current, timestamp="2026-09-19T00:00:01Z")
    _check(rebaselined["local_state"]["current"] == current and rebaselined["local_state"]["baseline"] == value["local_state"]["baseline"] and len(rebaselined["local_state"]["revisions"]) == 1, "a re-baseline moves current, never baseline")
    surface = record_local_state_revision(rebaselined, revision={"operation_id": "migration_export_root_containment", "preserved_surface_delta": ["profile/config/plugins/jira_plugin.json"]}, current=current, timestamp="2026-09-19T00:00:02Z")
    lifecycle = record_local_state_revision(surface, revision={"stage": "lifecycle", "service_writes": {"committed_additions": [{"path": "knowledge_bases/new_plugin", "kind": "symlink", "mode": "0755", "size": 34, "target_digest": "d" * 64}], "preserved_surface_delta": []}}, current=current, timestamp="2026-09-19T00:00:03Z")
    _check(len(lifecycle["local_state"]["revisions"]) == 3, "the three closed revision shapes are accepted")
    _expect(StateError, lambda: record_local_state_revision(lifecycle, revision={"stage": "nowhere", "service_writes": {"committed_additions": [], "preserved_surface_delta": []}}, current=current), "an unknown stage is refused")
    _expect(StateError, lambda: record_local_state_revision(lifecycle, revision={"operation_id": "x", "paths": ["a"]}, current=current), "a revision outside the closed shapes is refused")
    _expect(StateError, lambda: record_local_state_revision(lifecycle, revision={"operation_id": "x", "preserved_surface_delta": []}, current={**current, "local_state_commitment": None}), "a null commitment with a committed inventory is refused")
    path = root / "journal" / "opr.json"
    write_update_journal(path, None, value)
    write_update_journal(path, value, rebaselined)
    tampered = {**rebaselined, "local_state": {**rebaselined["local_state"], "baseline": current}}
    _expect(StateConflictError, lambda: write_update_journal(path, rebaselined, tampered), "a moved baseline is refused on write")
    silent = {**rebaselined, "local_state": {**rebaselined["local_state"], "current": value["local_state"]["baseline"]}}
    _expect(StateConflictError, lambda: write_update_journal(path, rebaselined, silent), "current cannot move without a revision")
    advanced = advance_update_journal(rebaselined, status="operation_published", stage_id="operation_published", note="ok")
    write_update_journal(path, rebaselined, advanced)
    _check(json.loads(path.read_text())["local_state"]["revisions"][0]["operation_id"] == "migration_solet_rename", "the revision persists")


_RETIRED_NAMES = ("tracked_state_present", "_EXECUTED_CODE_PREFIXES", "DISJOINT_LOCAL_TRACKED_CHANGES")
_STEP7_REASONS = {"executed_code_modified", "git_metadata_present", "host_requirement_missing", "host_requirement_unknown", "hydration_block_uncarriable", "instance_requirement_missing", "local_state_unobserved", "preservation_violated", "preserved_surface_in_transition", "service_offline_before_transition", "staged_changes_present", "tracked_overlap_present", "tracked_shape_changed"}


def _manager_sources() -> dict[str, str]:
    return {path.name: path.read_text(encoding="utf-8") for path in (_ROOT / "solet_cli" / "src" / "solet_manager").glob("*.py")}


def _assert_vocabulary() -> None:
    vocabularies = (errors.STEP5_REASON_CODES, errors.STEP6_REASON_CODES, errors.STEP7_REASON_CODES)
    _check(all("tracked_state_present" not in vocabulary for vocabulary in vocabularies), "tracked_state_present is retired from the vocabulary")
    _check(set(errors.STEP7_REASON_CODES) == _STEP7_REASONS, "the Step-7 reason set is closed")
    _assert_retired_names_gone(_manager_sources())


def _assert_retired_names_gone(sources: dict[str, str]) -> None:
    """Criterion 6: the retired names are gone; ``WorkingTreeCondition.CLEAN`` survives only where section 2.1 keeps it."""
    _check(not any(name in text for text in sources.values() for name in _RETIRED_NAMES), "criterion 6: the retired names are gone from the Manager source")
    clean_files = {name for name, text in sources.items() if "WorkingTreeCondition.CLEAN" in text}
    clean_uses = [line for text in sources.values() for line in text.splitlines() if "WorkingTreeCondition.CLEAN" in line]
    _check(len(clean_uses) == 4, f"criterion 6: four surviving uses of WorkingTreeCondition.CLEAN: {clean_uses}")
    _check(all("classification" in name or "target" in name for name in clean_files), f"criterion 6: WorkingTreeCondition.CLEAN survives only in _working_tree_condition, the observed-tree set and the two clean-class rows: {clean_files}")


def main() -> int:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        _assert_overlap()
        _assert_raw_diff()
        _assert_git_metadata_and_facts()
        _assert_executed_code_roots(root)
        _assert_commitment(root)
        _assert_service_write(root)
        _assert_host_rows(root)
        _assert_journal_revisions(root)
        _assert_vocabulary()
    print(f"update_topology_local_state_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
