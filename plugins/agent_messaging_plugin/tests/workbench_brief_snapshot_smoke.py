#!/usr/bin/env python3
"""Focused smoke for source-checkout workbench brief snapshots."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from session_lifecycle_verbs_smoke import (  # noqa: E402
    _TEST_HOST,
    _check,
    _FakeDriverWithChannel,
    _install_fake_hosts,
    _prepared_project_req,
    _remove_fake_hosts,
    _spawn_req,
    _state,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle  # noqa: E402
import agent_messaging_plugin.workbench_brief_snapshot as snapshots  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH  # noqa: E402
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    CaptureLaneCharterRequest,
    VerbError,
    build_fallback_first_turn,
    capture_lane_charter,
    list_sessions,
    spawn_session,
)


class _Fixture:
    def __init__(self, root: Path) -> None:
        self.root, self.provisioned = root, 0
        self.workbench = root / "workbench"
        self._resolver = snapshots.resolve_lane_repo_root
        self._provision = lifecycle._provision_spawn_worktree  # noqa: SLF001

    def __enter__(self) -> _Fixture:
        self.workbench.mkdir()
        snapshots.resolve_lane_repo_root = self._resolve  # type: ignore[assignment]
        lifecycle._provision_spawn_worktree = self._lane  # type: ignore[assignment]  # noqa: SLF001
        _install_fake_hosts()
        return self

    def __exit__(self, *_: object) -> None:
        snapshots.resolve_lane_repo_root = self._resolver  # type: ignore[assignment]
        lifecycle._provision_spawn_worktree = self._provision  # type: ignore[assignment]  # noqa: SLF001
        _remove_fake_hosts()

    def _resolve(self, root: str = "", app_home: str = "") -> Path:
        del app_home
        _check(root == str(self.root), "snapshot resolves request repository root")
        return self.root

    def _lane(self, *args: object, **kwargs: object) -> lifecycle.LaneWorktree:
        del args
        self.provisioned += 1
        name, ident = str(kwargs["role_name"]), str(kwargs["agent_instance_id"])
        return lifecycle.LaneWorktree(self.root, self.root, self.root / "lanes" / f"{name}-{ident}", "fixture")

    @property
    def driver(self) -> _FakeDriverWithChannel:
        return session_hosts._REGISTRY[_TEST_HOST]  # type: ignore[return-value]  # noqa: SLF001

    def source(self, ref: str, data: bytes) -> Path:
        path = self.root / ref
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def spawn(self, ref: str, lane: str) -> dict[str, object]:
        return spawn_session(_state(), _spawn_req(host=_TEST_HOST, lane_id=lane, brief_ref=ref, repository_root=str(self.root)))

    def refuses(self, ref: str, code: str) -> None:
        before = self.provisioned
        self.driver.channel.sent.clear()
        actual = ""
        try:
            self.spawn(ref, f"refuse-{code}")
        except VerbError as exc:
            actual = exc.code
        _check(actual == code and before == self.provisioned and not self.driver.channel.sent, f"{code} refuses before provision")


def test_delivery_and_refusals(root: Path) -> None:
    with _Fixture(root) as fixture:
        data, ref = b"one\ntwo\n", "workbench/source.md"
        fixture.source(ref, data)
        result = fixture.spawn(ref, "source")
        sent = fixture.driver.channel.sent[0]
        _check(result["first_turn_delivered"] is True and len(fixture.driver.channel.sent) == 1 and data.decode() in sent and hashlib.sha256(data).hexdigest() in sent, "source bytes deliver once with digest")
        _check(not (root / "lanes").joinpath(ref).exists(), "lane worktree never receives copied brief")
        fixture.refuses("workbench/missing", "brief_not_found")
        (fixture.workbench / "directory").mkdir()
        fixture.refuses("workbench/directory", "brief_not_regular_file")
        fixture.refuses("workbench/../escape", "brief_ref_path_escape")
        fixture.source("workbench/target", b"x")
        (fixture.workbench / "link").symlink_to(fixture.workbench / "target")
        fixture.refuses("workbench/link", "brief_ref_symlink")
        fixture.source("workbench/nonutf", b"\xff")
        fixture.refuses("workbench/nonutf", "brief_not_utf8")
        fixture.source("workbench/nul", b"a\x00b")
        fixture.refuses("workbench/nul", "brief_contains_control_byte")


def test_boundaries_drift_and_parent_swap(root: Path) -> None:
    with _Fixture(root) as fixture:
        original_cap = snapshots._WORKBENCH_BRIEF_MAX_BYTES  # noqa: SLF001
        snapshots._WORKBENCH_BRIEF_MAX_BYTES = 10  # type: ignore[misc]  # noqa: SLF001
        try:
            for name, data in (("minus", b"a" * 9), ("exact", b"b" * 10), ("plus", b"c" * 11)):
                fixture.source(f"workbench/{name}", data)
            fixture.spawn("workbench/minus", "minus")
            fixture.spawn("workbench/exact", "exact")
            fixture.refuses("workbench/plus", "brief_too_large")
        finally:
            snapshots._WORKBENCH_BRIEF_MAX_BYTES = original_cap  # type: ignore[misc]  # noqa: SLF001
        path = fixture.source("workbench/prepared", b"v1")
        state = _state()
        request = _prepared_project_req(state, host=_TEST_HOST, lane_id="mismatch", role_name="mismatch", brief_ref="workbench/prepared", repository_root=str(root))
        state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": request.dispatch_id}}, {"brief_sha256": "0" * 64})
        _refuse_prepared(fixture, state, request, "prepared digest mismatch")
        digest = hashlib.sha256(b"v1").hexdigest()
        matching_state = _state()
        matching = _prepared_project_req(matching_state, host=_TEST_HOST, lane_id="matching", role_name="matching", brief_ref="workbench/prepared", repository_root=str(root))
        _set_prepared_digest(matching_state, matching, digest)
        before = fixture.provisioned
        spawn_session(matching_state, matching)
        _check(fixture.provisioned == before + 1, "matching prepared digest permits spawn")
        mutation_state = _state()
        mutation = _prepared_project_req(mutation_state, host=_TEST_HOST, lane_id="mutation", role_name="mutation", brief_ref="workbench/prepared", repository_root=str(root))
        _set_prepared_digest(mutation_state, mutation, digest)
        path.write_bytes(b"v2")
        _refuse_prepared(fixture, mutation_state, mutation, "prepare-to-spawn mutation")
        _parent_swap(fixture)


def _refuse_prepared(fixture: _Fixture, state: object, request: object, label: str) -> None:
    before, code = fixture.provisioned, ""
    try:
        spawn_session(state, request)  # type: ignore[arg-type]
    except VerbError as exc:
        code = exc.code
    _check(code == "brief_digest_mismatch" and before == fixture.provisioned, f"{label} fails closed")


def _set_prepared_digest(state: object, request: object, digest: str) -> None:
    state.update_state(  # type: ignore[union-attr]
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_MANAGED_DISPATCH, "filters": {"dispatch_id": request.dispatch_id}},  # type: ignore[union-attr]
        {"brief_sha256": digest},
    )


def _parent_swap(fixture: _Fixture) -> None:
    fixture.source("workbench/race", b"inside")
    outside = fixture.root / "outside"
    outside.mkdir()
    (outside / "race").write_bytes(b"outside")
    original, swapped = snapshots.os.open, False

    def replace_parent(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal swapped
        if path == "workbench" and kwargs.get("dir_fd") is not None and not swapped:
            swapped = True
            fixture.workbench.rename(fixture.root / "workbench-original")
            fixture.workbench.symlink_to(outside, target_is_directory=True)
        return original(path, flags, *args, **kwargs)

    snapshots.os.open = replace_parent  # type: ignore[assignment]  # noqa: SLF001
    try:
        fixture.refuses("workbench/race", "brief_ref_symlink")
    finally:
        snapshots.os.open = original  # type: ignore[assignment]  # noqa: SLF001
        fixture.workbench.unlink()
        (fixture.root / "workbench-original").rename(fixture.workbench)
    _check(swapped, "parent-directory symlink substitution was exercised")


def test_unchanged_paths_and_multiline(root: Path) -> None:
    expected = lifecycle.render_driver_text("spawn.fallback_first_turn", role_instruction=lifecycle._role_instruction(role_class="ephemeral", role_name=""), brief_clause=" at notes/out", spawned_by_role=lifecycle._FALLBACK_NO_SPAWNER)  # noqa: SLF001
    _check(build_fallback_first_turn(spawned_by_role="", role_class="ephemeral", role_name="", brief_ref="notes/out") == expected, "non-workbench fallback remains exact")
    with _Fixture(root) as fixture:
        charter_state = _state()
        capture_lane_charter(charter_state, CaptureLaneCharterRequest(lane_id="charter", charter_text="body", captured_at="2026-09-16T00:00:00+00:00", brief_ref="workbench/missing"))
        spawn_session(charter_state, _spawn_req(host=_TEST_HOST, lane_id="charter", brief_ref="workbench/missing", repository_root=str(root)))
        _check(fixture.driver.channel.sent[-1].endswith("body"), "charter fallback remains exact")
        for runtime, host in (("codex", _TEST_HOST), ("claude_code", "tmux")):
            data, key = b"one\ntwo\n", (runtime, host)
            fixture.source(f"workbench/{runtime}", data)
            prior, driver = session_hosts._REGISTRY.get(key), _FakeDriverWithChannel()  # noqa: SLF001
            session_hosts._REGISTRY[key] = driver  # noqa: SLF001
            try:
                spawn_session(_state(), _spawn_req(host=host, agent_runtime=runtime, lane_id=runtime, brief_ref=f"workbench/{runtime}", repository_root=str(root)))
                _check(len(driver.channel.sent) == 1 and data.decode() in driver.channel.sent[0] and hashlib.sha256(data).hexdigest() in driver.channel.sent[0], f"{runtime} multiline delivery is one exact turn")
            finally:
                if prior is None:
                    session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
                else:
                    session_hosts._REGISTRY[key] = prior  # noqa: SLF001


def test_brief_marker_has_no_retired_floor(root: Path) -> None:
    """rul_0c6ec7c7: a schema identifier in a brief no longer constrains model choice."""
    with _Fixture(root) as fixture:
        ref = "workbench/schema_brief.md"
        fixture.source(ref, b"Add a dependency_snapshots TableSchema with a ColumnDefinition per field.\n")
        before = fixture.provisioned
        state = _state()
        spawn_session(state, _spawn_req(
            host=_TEST_HOST, lane_id="floor-sonnet", brief_ref=ref, repository_root=str(root),
            dispatch_kind="infrastructure", agent_runtime="claude_code", model="claude-sonnet-5",
        ))
        _check(
            fixture.provisioned == before + 1,
            "schema-touching brief under infrastructure can provision claude-sonnet-5",
        )
        row = list_sessions(state, {"lane_id": "floor-sonnet"})["sessions"][0]
        _check(
            row.get("scope_tags") == [] and row.get("capability_floors") == [],
            f"the ledger row records no retired floor (got {row.get('capability_floors')!r})",
        )


def main() -> int:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        test_delivery_and_refusals(root)
    with tempfile.TemporaryDirectory() as directory:
        test_brief_marker_has_no_retired_floor(Path(directory))
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        test_boundaries_drift_and_parent_swap(root)
    with tempfile.TemporaryDirectory() as directory:
        test_unchanged_paths_and_multiline(Path(directory))
    return 0


if __name__ == "__main__":
    sys.exit(main())
