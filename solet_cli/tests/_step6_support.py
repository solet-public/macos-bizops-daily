"""Step-6 fixture support: the systematic crash sweep, bundle variants, doctor fixtures, CLI resume.

Everything Step-5's ``_step5_support`` does not need lives here (design section
6.3): ``CrashSweep`` runs one scenario to completion without faults to record
the reference outcome, then replays it with a ``SimulatedCrash`` injected at
every write boundary (each journal, inventory and doctor-journal write, after
the real write) or every apply boundary (each adapter apply, reconciliation
call, ``launchctl`` bootout/bootstrap, write-kind bridge call, and Step-4
``fetch``/``merge``, after the real effect and before the executor can journal
it).  After each crash a FRESH ``apply_update`` resumes and the sweep asserts
the reference terminal, the reference inventory row (modulo timestamps), the
reference target/HOME byte map, bounded mutation counters, and an immutable
journal prefix.  ``db_spy`` wraps every run.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _step5_support import (  # noqa: E402
    KB,
    FakeHost,
    Fixture,
    advance_to_source_advanced,
    build_fixture,
    bundle_document,
    default_operations,
    operation,
    runtime_fingerprint,
)
from solet_manager import doctor_journal as doctor_journal_module  # noqa: E402
from solet_manager import existing_install_doctor as doctor_module  # noqa: E402
from solet_manager import maintenance_inventory as inventory_module  # noqa: E402
from solet_manager import update_execution as execution_module  # noqa: E402
from solet_manager import update_journal as journal_module  # noqa: E402
from solet_manager import update_runtime_execution as executor_module  # noqa: E402
from solet_manager.errors import ManagerError  # noqa: E402
from solet_manager.models import InstanceInventoryRecordV2, JsonValue  # noqa: E402
from solet_manager.update_execution import UpdateRequest, apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_journal import read_update_journal  # noqa: E402
from solet_manager.update_runtime_plan import RuntimeSeams  # noqa: E402

__all__ = [
    "CrashSweep",
    "GitArgvObserver",
    "SimulatedCrash",
    "SweepOutcome",
    "byte_map",
    "cli_resume",
    "forward_only_document",
    "knowledge_removal_document",
    "last_update_journal",
    "manual_migration_document",
    "run_to_promoted",
    "sweep_slice",
]

FIXTURE_MIGRATION_KEY = "service_interface::fixture_state_service::apply_release_migration"
FIXTURE_FORWARD_KEY = "service_interface::fixture_state_service::forward_only_migration"
_TIMESTAMPS = frozenset({"updated_at", "last_inspected_at", "last_verified_at", "created_at"})
_READ_ONLY_GIT = frozenset({"rev-parse", "config", "status", "diff-tree", "diff", "show", "ls-tree", "merge-base", "check-ignore", "cat-file", "for-each-ref", "log", "rev-list", "symbolic-ref", "worktree", "submodule", "remote", "ls-files", "var", "describe"})
_WRITE_GIT = frozenset({"fetch", "merge"})


class SimulatedCrash(RuntimeError):  # noqa: N818 - the design names it SimulatedCrash (section 6.1)
    """Raised by a sweep wrapper after the real effect happened and before the executor could record it."""


@dataclass
class GitArgvObserver:
    """Records every Git argv the Manager runs against the target (F-RB-1)."""

    argv: list[tuple[str, ...]] = field(default_factory=lambda: [])

    def verbs(self) -> set[str]:
        verbs: set[str] = set()
        for args in self.argv:
            verb = next((item for item in args if not item.startswith("-") and item not in {"core.fsmonitor=false", "advice.diverging=false"} and not item.startswith("core.hooksPath=")), "")
            verbs.add(verb)
        return verbs

    def assert_forward_only(self) -> None:
        verbs = self.verbs()
        forbidden = verbs & {"reset", "checkout", "restore", "stash", "rebase", "clean", "update-ref", "push", "commit", "branch", "tag"}
        assert not forbidden, f"destructive or unexpected git verbs observed: {sorted(forbidden)}"
        unknown = verbs - _READ_ONLY_GIT - _WRITE_GIT
        assert not unknown, f"git verbs outside the measured vocabulary: {sorted(unknown)}"
        for args in self.argv:
            if "merge" in args:
                assert "--ff-only" in args, f"merge without --ff-only: {args}"


@contextlib.contextmanager
def observe_git() -> Iterator[GitArgvObserver]:
    observer = GitArgvObserver()
    original = execution_module._run_git  # noqa: SLF001

    def wrapped(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        observer.argv.append(tuple(args))
        return original(cwd, args, hooks_dir=hooks_dir)

    with patch.object(execution_module, "_run_git", wrapped):
        yield observer


def fixture_roles(fixture: Fixture) -> dict[str, str]:
    """Every fixture-specific identity and its role, so two fixtures of one scenario compare equal."""
    roles = {
        fixture.baseline.commit: "<baseline.commit>",
        fixture.baseline.tree: "<baseline.tree>",
        fixture.candidate.commit: "<candidate.commit>",
        fixture.candidate.tree: "<candidate.tree>",
        fixture.contract: "<contract>",
        fixture.descriptor_digest: "<descriptor>",
        hashlib.sha256(fixture.baseline.provenance).hexdigest(): "<baseline.provenance>",
        hashlib.sha256(fixture.candidate.provenance).hexdigest(): "<candidate.provenance>",
        fixture.baseline.seed_id: "<baseline.seed_id>",
        fixture.candidate.seed_id: "<candidate.seed_id>",
        str(fixture.root): "<ROOT>",
    }
    return roles


def byte_map(fixture: Fixture) -> dict[str, str]:
    """sha256 of every regular file under target and HOME, keyed by relative path, with the fixture's own
    identities normalised out of file contents so two fixtures of the same scenario compare equal; ``.git`` excluded."""
    result: dict[str, str] = {}
    roles = [(value.encode(), role.encode()) for value, role in fixture_roles(fixture).items()]
    for root in (fixture.target, fixture.home):
        for path in sorted(root.rglob("*")):
            if not path.is_file() or ".git" in path.parts:
                continue
            if root == fixture.target and path.relative_to(root).parts[:2] == ("profile", "data"):
                # Seed-side runtime state (cutover receipts keyed by random rec_ ids); a preserved-never
                # surface the Manager never writes, so it is outside the byte map by construction.
                continue
            content = path.read_bytes()
            for value, role in roles:
                content = content.replace(value, role)
            result[f"{root.name}/{path.relative_to(root)}"] = hashlib.sha256(content).hexdigest()
    return result


def record_shape(fixture: Fixture) -> dict[str, JsonValue]:
    """The inventory row with every fixture-specific identity replaced by its role, so two fixtures compare equal."""
    record = fixture.record()
    raw = inventory_module.serialize_maintenance_inventory_v2((record,))["records"]
    row = cast(dict[str, JsonValue], cast(list[JsonValue], raw)[0])
    for key in _TIMESTAMPS:
        row.pop(key, None)
    roles = dict(fixture_roles(fixture))
    roles[record.instance_id] = "<instance>"
    if record.last_verified_operation_id is not None:
        roles[record.last_verified_operation_id] = "<last_verified_operation>"
    if record.active_operation is not None:
        roles[record.active_operation.operation_id] = "<active_operation>"
    text = json.dumps(row, sort_keys=True)
    for value, role in roles.items():
        text = text.replace(value, role)
    shape = cast(dict[str, JsonValue], json.loads(text))
    target = cast(dict[str, JsonValue], shape["target"])
    target["filesystem_identity"] = "<identity>"
    target["parent_filesystem_identity"] = "<identity>"
    return shape


def fresh_request(fixture: Fixture) -> UpdateRequest:
    """A new request object with the same paths and seams: the resume is always a fresh process (section 4.5)."""
    return replace(fixture.request)


def run_to_promoted(fixture: Fixture) -> str:
    """The reference happy path from an enrolled row: Step 4, runtime approval, runtime + doctor + promotion."""
    if fixture.record().active_operation is None and fixture.record().source_release.commit != fixture.candidate.commit:
        advance_to_source_advanced(fixture)
    elif fixture.record().active_operation is None:
        preview = preview_update_instance(fixture.request)
        assert preview.status == "verify_preview_ready", preview
        applied = apply_update(fixture.request, cast(str, preview.data["approval_fingerprint"]))
        assert applied.status == "source_advanced", applied
    fingerprint = runtime_fingerprint(fixture)
    result = apply_update(fixture.request, fingerprint)
    assert result.status == "promoted", (result.status, result.error_kind, result.message)
    return result.status


@dataclass
class SweepOutcome:
    """One crash point and what the resumed run reached."""

    mode: str
    index: int
    boundary: str
    crashed_status: str
    resumed_status: str


@dataclass
class CrashSweep:
    """Section 6.1: reference run, then a crash at every boundary with a fresh resume after each."""

    build: Callable[[Path], Fixture]
    scenario: Callable[[Fixture], str]
    root: Path
    expected_terminal: str = "promoted"
    counter_allowances: dict[str, int] = field(default_factory=lambda: {"apply": 0, "launchctl": 0, "bridge": 1, "cutover": 1, "inventory": 2, "pip": 1})
    reference_row: dict[str, JsonValue] | None = None
    reference_bytes: dict[str, str] | None = None
    reference_counters: dict[str, int] | None = None
    reference_journal_attempts: int = 0
    outcomes: list[SweepOutcome] = field(default_factory=lambda: [])

    # --- reference ---------------------------------------------------------------------

    def reference(self) -> tuple[Fixture, int, int]:
        """Run once without faults; count the write and apply boundaries it crossed."""
        fixture = self.build(self.root / "reference")
        writes = _WriteCounter()
        applies = _ApplyCounter(fixture.host)
        with writes.counting(), applies.counting(fixture):
            status = self.scenario(fixture)
        assert status == self.expected_terminal, (status, self.expected_terminal)
        self.reference_row = record_shape(fixture)
        self.reference_bytes = byte_map(fixture)
        self.reference_counters = _counters(fixture)
        self.reference_journal_attempts = len(cast(list[JsonValue], last_update_journal(fixture)["attempts"]))
        return fixture, writes.count, applies.count

    # --- sweep --------------------------------------------------------------------------

    def sweep_writes(self, total: int, part: int = 1, parts: int = 1) -> None:
        """Every write boundary, or the ``part``-th of ``parts`` equal slices so one smoke stays inside the gate's per-smoke budget."""
        for index in sweep_slice(total, part, parts):
            self._one(index, "write", total)

    def sweep_applies(self, total: int, part: int = 1, parts: int = 1) -> None:
        for index in sweep_slice(total, part, parts):
            self._one(index, "apply", total)

    def _one(self, index: int, mode: str, total: int) -> None:
        fixture = self.build(self.root / f"{mode}-{index}")
        crashed_status = "prepared"
        boundary = "none"
        injector = _WriteCounter(crash_at=index) if mode == "write" else _ApplyCounter(fixture.host, crash_at=index)
        try:
            with injector.counting(fixture) if isinstance(injector, _ApplyCounter) else injector.counting():
                status = self.scenario(fixture)
            # No crash fired: the boundary count moved between reference and this run, which is itself a finding.
            raise AssertionError(f"{mode} crash {index}/{total} never fired; the run ended {status}")
        except SimulatedCrash as exc:
            boundary = str(exc)
            crashed_status = _status_of(fixture)
        crashed_attempts = _attempt_prefix(fixture)
        resumed = self._resume(fixture)
        self.outcomes.append(SweepOutcome(mode, index, boundary, crashed_status, resumed))
        self._assert_reference(fixture, index, mode, boundary, crashed_attempts)

    def _resume(self, fixture: Fixture) -> str:
        """A fresh process resumes with the recorded fingerprint(s) until the reference terminal or a stop."""
        for _ in range(6):
            journal = last_update_journal(fixture)
            status = cast(str, journal["status"])
            if status in {"blocked", "failed", "abandoned", "promoted"}:
                return _resume_terminal(fixture, journal, status)
            if fixture.record().active_operation is None and status != "prepared":
                return status
            try:
                result = apply_update(fresh_request(fixture), _fingerprint_for(fixture, journal))
            except ManagerError:
                return _status_of(fixture)
            if result.exit_code != 0:
                return result.status
            if result.status != "source_advanced" or journal["runtime_approval"] is not None:
                return result.status
        return _status_of(fixture)

    def _assert_reference(self, fixture: Fixture, index: int, mode: str, boundary: str, crashed_attempts: list[JsonValue]) -> None:
        label = f"{mode} crash {index} at {boundary}"
        status = _status_of(fixture)
        assert status == self.expected_terminal, f"{label}: resumed to {status}, reference {self.expected_terminal}; result={last_update_journal(fixture)['result']}; attempts={[cast(dict[str, JsonValue], item)['note'] for item in cast(list[JsonValue], last_update_journal(fixture)['attempts'])][-3:]}"
        row = record_shape(fixture)
        assert row == self.reference_row, f"{label}: inventory row differs from the reference: {_diff(row, cast(dict[str, JsonValue], self.reference_row))}"
        bytes_now = byte_map(fixture)
        assert bytes_now == self.reference_bytes, f"{label}: target/HOME bytes differ from the reference: {_diff(cast(dict[str, JsonValue], bytes_now), cast(dict[str, JsonValue], self.reference_bytes))}"
        counters = _counters(fixture)
        for key, value in counters.items():
            allowed = cast(dict[str, int], self.reference_counters).get(key, 0) + self.counter_allowances.get(key, 0)
            assert value <= allowed, f"{label}: mutation counter {key}={value} exceeds reference+allowance {allowed}"
        final_attempts = cast(list[JsonValue], last_update_journal(fixture)["attempts"])
        assert final_attempts[: len(crashed_attempts)] == crashed_attempts, f"{label}: journal attempts lost after the crash"


def _resume_terminal(fixture: Fixture, journal: dict[str, JsonValue], status: str) -> str:
    """A terminal journal with the pointer still set: the operator's next --yes is refused, but its on-sight
    repairs (pointer release after promoted/abandoned, needs_attention after blocked/failed) still run."""
    if fixture.record().active_operation is None:
        return status
    with contextlib.suppress(ManagerError):
        apply_update(fresh_request(fixture), _fingerprint_for(fixture, journal))
    if status in {"promoted", "abandoned"}:
        assert fixture.record().active_operation is None, "the stale pointer was not released on sight"
    return status


def sweep_slice(total: int, part: int, parts: int) -> range:
    """The 1-based crash indices of slice ``part`` of ``parts``; the slices partition ``1..total`` exactly."""
    if not 1 <= part <= parts:
        raise ValueError(f"part {part} outside 1..{parts}")
    size, remainder = divmod(total, parts)
    start = 1 + (part - 1) * size + min(part - 1, remainder)
    stop = start + size + (1 if part <= remainder else 0)
    return range(start, stop)


def _diff(left: dict[str, JsonValue], right: dict[str, JsonValue]) -> dict[str, JsonValue]:
    keys = set(left) | set(right)
    return {key: {"now": left.get(key), "reference": right.get(key)} for key in sorted(keys) if left.get(key) != right.get(key)}


def _status_of(fixture: Fixture) -> str:
    return cast(str, last_update_journal(fixture)["status"])


def last_update_journal(fixture: Fixture) -> dict[str, JsonValue]:
    record = fixture.record()
    operation_id = record.active_operation.operation_id if record.active_operation is not None else record.last_verified_operation_id
    if operation_id is None or operation_id.endswith("5" * 32):
        journals = sorted((fixture.paths.operations_dir / record.instance_id).glob("opr_*.json"), key=lambda path: path.stat().st_mtime)
        assert journals, "no update journal exists yet"
        for path in reversed(journals):
            raw = json.loads(path.read_text())
            if raw.get("kind") == "update":
                return read_update_journal(path)
        raise AssertionError("no update journal exists yet")
    return read_update_journal(fixture.paths.operation_path(record.instance_id, operation_id))


def _attempt_prefix(fixture: Fixture) -> list[JsonValue]:
    try:
        return list(cast(list[JsonValue], last_update_journal(fixture)["attempts"]))
    except AssertionError:
        return []


def _fingerprint_for(fixture: Fixture, journal: dict[str, JsonValue]) -> str:
    approval = journal["runtime_approval"]
    if approval is not None:
        return cast(str, cast(dict[str, JsonValue], approval)["fingerprint"])
    if journal["status"] == "source_advanced":
        return runtime_fingerprint(fixture)
    return cast(str, cast(dict[str, JsonValue], journal["approval"])["fingerprint"])


def _counters(fixture: Fixture) -> dict[str, int]:
    host = fixture.host
    return {
        "launchctl": sum(1 for call in host.launchctl_calls if call[0] in {"bootout", "bootstrap"}),
        "bridge": sum(1 for key, _ in host.bridge_calls if not key.endswith("attest_runtime_code") and not key.endswith("::search")),
        "pip": len(host.pip_calls),
        "apply": sum(1 for _, phase, _ in host.adapter_calls if phase == "apply"),
    }


# --- injectors ------------------------------------------------------------------------------


class _WriteCounter:
    """Counts (and optionally crashes after) every durable Manager-state write of the reference run."""

    def __init__(self, crash_at: int | None = None) -> None:
        self.count = 0
        self.crash_at = crash_at

    def _tick(self, boundary: str) -> None:
        self.count += 1
        if self.crash_at is not None and self.count == self.crash_at:
            raise SimulatedCrash(f"{boundary}#{self.count}")

    @contextlib.contextmanager
    def counting(self) -> Iterator[None]:
        real_journal = journal_module.write_update_journal
        real_inventory = inventory_module.write_maintenance_inventory_v2
        real_doctor = doctor_journal_module.write_doctor_journal

        def journal(path: Path, previous: dict[str, JsonValue] | None, next_value: dict[str, JsonValue]) -> None:
            real_journal(path, previous, next_value)
            self._tick(f"update_journal:{next_value['status']}")

        def inventory(path: Path, records: tuple[InstanceInventoryRecordV2, ...]) -> None:
            real_inventory(path, records)
            self._tick("inventory")

        def doctor(path: Path, previous: dict[str, JsonValue] | None, next_value: dict[str, JsonValue]) -> None:
            real_doctor(path, previous, next_value)
            self._tick("doctor_journal")

        with (
            patch.object(execution_module, "write_update_journal", journal),
            patch.object(executor_module, "write_update_journal", journal),
            patch.object(inventory_module, "write_maintenance_inventory_v2", inventory),
            patch.object(doctor_module, "write_doctor_journal", doctor),
        ):
            yield


class _ApplyCounter:
    """Counts (and optionally crashes after) every target/platform mutation the reference run performs."""

    def __init__(self, host: FakeHost, crash_at: int | None = None) -> None:
        self.host = host
        self.count = 0
        self.crash_at = crash_at

    def _tick(self, boundary: str) -> None:
        self.count += 1
        if self.crash_at is not None and self.count == self.crash_at:
            raise SimulatedCrash(f"{boundary}#{self.count}")

    @contextlib.contextmanager
    def counting(self, fixture: Fixture) -> Iterator[None]:
        seams = cast(RuntimeSeams, fixture.request.runtime_seams)
        real_adapter, real_reconciliation, real_bridge, real_launchctl = seams.invoke_adapter, seams.invoke_reconciliation, seams.invoke_bridge, seams.launchctl
        real_git = execution_module._run_git  # noqa: SLF001

        def adapter(registry: Any, request: Any) -> Any:
            result = real_adapter(registry, request)
            if request.phase == "apply":
                self._tick(f"adapter_apply:{request.operation_id}")
            return result

        def reconciliation(registry: Any, envelope: Any, timeout: int) -> Any:
            outcome = real_reconciliation(registry, envelope, timeout)
            if envelope.get("phase") in {"apply", "recover"}:
                self._tick(f"reconciliation:{envelope.get('phase')}")
            return outcome

        def bridge(registry: Any, key: str, arguments: Any, kind: str, timeout: int) -> Any:
            data = real_bridge(registry, key, arguments, kind, timeout)
            if kind in {"migration", "knowledge"}:
                self._tick(f"bridge:{kind}")
            return data

        def launchctl(registry: Any, verb: str, arguments: Any, timeout: int) -> Any:
            completed = real_launchctl(registry, verb, arguments, timeout)
            if verb in {"bootout", "bootstrap"}:
                self._tick(f"launchctl:{verb}")
            return completed

        def git(cwd: Path, args: tuple[str, ...], *, hooks_dir: Path | None = None) -> subprocess.CompletedProcess[bytes]:
            completed = real_git(cwd, args, hooks_dir=hooks_dir)
            if hooks_dir is not None and completed.returncode == 0:
                self._tick(f"git:{args[0]}")
            return completed

        patched = replace(seams, invoke_adapter=adapter, invoke_reconciliation=reconciliation, invoke_bridge=bridge, launchctl=launchctl)
        fixture.request = replace(fixture.request, runtime_seams=patched)
        try:
            with patch.object(execution_module, "_run_git", git):
                yield
        finally:
            fixture.request = replace(fixture.request, runtime_seams=seams)


# --- bundle variants (section 6.2 fixtures) -----------------------------------------------------


def manual_migration_document(baseline: Any) -> dict[str, Any]:
    """F-B-5: ``migration_export_root_containment`` with ``retry_policy=manual`` (type T3)."""
    operations = default_operations()
    for row in operations:
        if row["operation_id"] == "migration_export_root_containment":
            row["retry_policy"] = "manual"
    return bundle_document(baseline, operations=operations)


def forward_only_document(baseline: Any) -> dict[str, Any]:
    """F-M-2: a ``database_forward_only`` platform migration declared last (type T7)."""
    operations = [*default_operations(), operation("platform_forward_only", "existing::runtime.platform_migration", "instance_bridge", "runtime_reconcile", "database_forward_only", "forward_only", [], retry="manual", confirm=True)]
    return bundle_document(baseline, operations=operations)


def additive_migration_document(baseline: Any) -> dict[str, Any]:
    """F-M-1 / T6: a retry-safe additive platform migration."""
    operations = [*default_operations(), operation("platform_additive", "existing::runtime.platform_migration", "instance_bridge", "runtime_reconcile", "database_additive", "reversible", [], retry="retry_safe", confirm=False)]
    return bundle_document(baseline, operations=operations)


def knowledge_removal_document(baseline: Any) -> dict[str, Any]:
    """F-RT-8: the candidate removes ``kbx/removed_article.md``; the bundle declares the ``kbx`` knowledge base."""
    return bundle_document(baseline, knowledge_removals=["kbx"])


REMOVED_ARTICLE = f"{KB}/kbx/removed_article.md"
REMOVED_ARTICLE_TEXT = "---\ntitle: Retired Procedure\n---\n\n# Retired Procedure\n\nThis article is removed by the candidate release.\n"


def build_knowledge_removal_fixture(root: Path, *, host: FakeHost | None = None) -> Fixture:
    """Baseline carries the article; the candidate tree omits it (a real deletion in the seed history)."""
    return build_fixture(root, document=knowledge_removal_document, host=host, baseline_extra={REMOVED_ARTICLE: REMOVED_ARTICLE_TEXT}, candidate_removals=(REMOVED_ARTICLE,))


# --- CLI resume -----------------------------------------------------------------------------------


def cli_resume(fixture: Fixture, argv: list[str]) -> tuple[int, dict[str, Any]]:
    """Resume through ``manager_cli.main`` in-process with the fixture's loader, transport and seams injected (CLI/JSON parity)."""
    from solet_manager import manager_cli  # noqa: PLC0415
    from solet_manager.paths import ManagerPaths  # noqa: PLC0415

    captured: list[str] = []
    template = fixture.request

    def request_factory(name: str, manager_paths: ManagerPaths, **kwargs: Any) -> UpdateRequest:
        return replace(template, name=name, manager_paths=manager_paths, operator_selections=kwargs.get("operator_selections", {}))

    with (
        patch.object(manager_cli.ManagerPaths, "resolve", classmethod(lambda cls, **_kwargs: fixture.paths)),
        patch.object(manager_cli, "UpdateRequest", request_factory),
        patch("builtins.print", lambda *args, **_kwargs: captured.append(" ".join(str(item) for item in args))),
    ):
        code = manager_cli.main([*argv, "--json"])
    return code, json.loads("\n".join(captured))


def remove_tree(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)


def env_path_for_subprocess() -> dict[str, str]:
    return {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
