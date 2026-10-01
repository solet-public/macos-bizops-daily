#!/usr/bin/env python3
"""Unit smoke for the L1 verb bodies (``session_lifecycle_verbs.py``):
``spawn_session``, ``list_sessions``, ``session_status``, ``clear_session``,
``compact_session``, ``drive_session``, ``terminate_session``,
``retire_session``, ``report_alive``. Against ``RealShapeState`` (real
provider ActionResult envelopes).

``clear_session``/``compact_session`` are tested against a trivial FAKE
``HostDriver``/``DriverChannel`` pair, monkeypatched into
``session_hosts._REGISTRY`` under a test-only host name — isolates the
VERB's own logic (terminal-state guard, park transition, error-token
mapping) from the real ``headless`` driver's subprocess machinery, which
has its own dedicated smoke (``headless_adapter_smoke.py``).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_lifecycle_verbs_smoke.py
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import tempfile
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    ROLE_CLASS_EPHEMERAL,
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

import agent_messaging_plugin.lane_worktrees as lane_worktrees  # noqa: E402
import agent_messaging_plugin.model_dispatch_policy as model_dispatch_policy  # noqa: E402
import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle_verbs  # noqa: E402
from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_SESSION,
    WORK_CLASS_ANALYSIS_DELIVERABLE,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    resolve_lane_charter,
    set_host_ref,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    DEFAULT_REPORT_BY_SECONDS,
    FIRST_TURN_SOURCE_CHARTER,
    FIRST_TURN_SOURCE_FALLBACK,
    ArmSessionDependencyRequest,
    CaptureLaneCharterRequest,
    SpawnSessionRequest,
    VerbError,
    arm_session_dependency,
    build_fallback_first_turn,
    capture_lane_charter,
    clear_session,
    compact_session,
    drive_session,
    list_sessions,
    report_alive,
    resolve_local_name,
    retire_session,
    session_status,
    spawn_session,
    terminate_session,
)
from agent_messaging_plugin.session_role_claim_store import (  # noqa: E402
    CardinalityGatedClaim,
    read_session_role_claim,
    win_cardinality_gate,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def test_lane_worktree_root_requires_app_home() -> None:
    """A scratch checkout must never become a provisioning root by cwd inference."""
    with tempfile.TemporaryDirectory() as raw:
        scratch = Path(raw) / "scratch-checkout"
        (scratch / ".git").mkdir(parents=True)
        previous_app_home = os.environ.pop("APP_HOME", None)
        previous_cwd = Path.cwd()
        try:
            os.chdir(scratch)
            try:
                lifecycle_verbs._resolve_lane_repo_root()  # noqa: SLF001
            except VerbError as exc:
                _check(
                    exc.code == "lane_worktree_app_home_required",
                    "APP_HOME-unset scratch checkout fails closed instead of inheriting cwd",
                )
            else:
                _check(False, "APP_HOME-unset scratch checkout must fail closed")
        finally:
            os.chdir(previous_cwd)
            if previous_app_home is None:
                os.environ.pop("APP_HOME", None)
            else:
                os.environ["APP_HOME"] = previous_app_home


def _spawn_req(**overrides: object) -> SpawnSessionRequest:
    base: dict[str, object] = {
        "role_class": "ephemeral",
        "lane_id": "lane-1",
        "brief_ref": "fixtures/brief.md",
        "work_class": WORK_CLASS_ANALYSIS_DELIVERABLE,
        "budget_line": "budget-1",
        "host": "operator",
        "directed_by": "operator:none",
        "dispatch_kind": "infrastructure",
    }
    base.update(overrides)
    return SpawnSessionRequest(**base)  # type: ignore[arg-type]


def _prepared_project_req(
    state: StateManagementInterface,
    **overrides: object,
) -> SpawnSessionRequest:
    """Mint the server-side preparing row required by project spawn tests.

    The row is written as already past its register-Unit step (design
    unt_57725090): these tests exercise spawn, not the mint.
    """
    req = _spawn_req(role_class=ROLE_CLASS_PROJECT, **overrides)
    req = replace(req, unit_id=req.unit_id or "unt_lifecycle-fixture")
    dispatch_id = f"mdp-test-{time.time_ns()}"
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_MANAGED_DISPATCH,
            "record": {
                "dispatch_id": dispatch_id,
                "state": "preparing",
                "current_agent_instance_id": "",
                "lane_id": req.lane_id,
                "role_name": req.role_name,
                "role_class": req.role_class,
                "work_class": req.work_class,
                "budget_line": req.budget_line,
                "brief_ref": req.brief_ref,
                "brief_sha256": "",
                "repository_root": req.repository_root,
                "agent_runtime": req.agent_runtime,
                "host": str(req.host or ""),
                "visibility": req.visibility,
                "model": req.model,
                "effort": req.effort,
                "report_by_seconds": req.report_by_seconds,

                "allowed_hosts": [str(req.host or "headless")],
                "allowed_tools": list(req.allowed_tools),
                "permission_mode": req.permission_mode,
                "transport": req.transport,
                "allow_askuserquestion": req.allow_askuserquestion,
                "local_name": req.local_name,
                "degraded_hooks_acknowledged": req.degraded_hooks_acknowledged,
                "version": 0,
                "attempt_number": 1,
                "spawned_by_role": req.spawned_by_role,
                "spawned_by_instance_id": req.spawned_by_instance_id,
                "directed_by": req.directed_by,
                "unit_id": req.unit_id,
                "next_required_action": "spawn_current_attempt",
            },
        },
    )
    return replace(req, dispatch_id=dispatch_id)


def test_raw_project_spawn_requires_prepared_dispatch() -> None:
    """Fixture 16: raw managed-work creation has no compatibility fallback."""
    state = _state()
    code = ""
    try:
        spawn_session(
            state,
            _spawn_req(role_class=ROLE_CLASS_PROJECT, role_name="Project-Builder"),
        )
    except VerbError as exc:
        code = exc.code
    _check(code == "managed_dispatch_required", "16 raw project spawn is hard-refused")


def test_spawn_errors() -> None:
    state = _state()
    for kwargs, code in [
        ({"role_class": "bogus"}, "unknown_role_class"),
        ({"work_class": "bogus"}, "unknown_work_class"),
        ({"budget_line": ""}, "budget_line_required"),
        ({"role_class": ROLE_CLASS_PRINCIPAL, "role_name": "Some-Office"}, "role_not_legislated"),
        ({"role_name": "Coordinator-Main"}, "reserved_role_name"),
    ]:
        raised_code = None
        try:
            spawn_session(state, _spawn_req(**kwargs))
        except VerbError as exc:
            raised_code = exc.code
        _check(raised_code == code, f"spawn_session({kwargs}) -> {code} (got {raised_code!r})")

    # host_mechanism_missing: "screen" is explicitly documented unsupported
    # (skeleton §2) and never gets a driver -- "tmux" no longer works for
    # this case now that D2 registered it (session_lifecycle_verbs_smoke.py
    # must track the registry, not assume any one name stays unregistered).
    missing = None
    try:
        spawn_session(state, _spawn_req(host="screen"))
    except VerbError as exc:
        missing = exc.code
    _check(
        missing == "host_mechanism_missing",
        "spawn_session(host='screen') -> host_mechanism_missing",
    )

    # headless and tmux (D2) are BOTH registered now, but fail closed
    # (host_cannot_spawn) in an unconfigured environment (no
    # SOLET_NAME / permission mode / .mcp.json) -- config remedies,
    # never a silent bypass-permissions default. Own lane_id each so
    # neither perturbs the operator row-count assertion below.
    headless_cannot_spawn = None
    os.environ.pop("FLEET_HEADLESS_PERMISSION_MODE", None)
    try:
        spawn_session(state, _spawn_req(host="headless", lane_id="lane-headless-cfg"))
    except VerbError as exc:
        headless_cannot_spawn = exc.code
    _check(
        headless_cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='headless') in an unconfigured environment -> "
        f"host_cannot_spawn (got {headless_cannot_spawn!r})",
    )

    tmux_cannot_spawn = None
    try:
        spawn_session(state, _spawn_req(host="tmux", lane_id="lane-tmux-cfg"))
    except VerbError as exc:
        tmux_cannot_spawn = exc.code
    _check(
        tmux_cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='tmux') in an unconfigured environment -> "
        f"host_cannot_spawn (got {tmux_cannot_spawn!r})",
    )

    # host_cannot_spawn: operator is degenerate, and the ledger row is left
    # terminated (half-failed-spawn cleanup), not stuck in 'spawning'.
    cannot_spawn = None
    try:
        spawn_session(state, _spawn_req(host="operator"))
    except VerbError as exc:
        cannot_spawn = exc.code
    _check(
        cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='operator') -> host_cannot_spawn",
    )
    rows = list_sessions(state, {"lane_id": "lane-1"})["sessions"]
    _check(
        len(rows) == 1 and rows[0]["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a known-doomed dispatch (host_cannot_spawn) transitions the ledger "
        "row straight to terminated, not left stuck in 'spawning'",
    )


def test_model_dispatch_policy_refusals_and_allowances() -> None:
    """The policy is enforced before a host dispatch or ledger write."""
    state = _state()
    missing = None
    try:
        spawn_session(state, _spawn_req(dispatch_kind=""))
    except VerbError as exc:
        missing = exc.code
    _check(missing == "dispatch_kind_required", "omitted dispatch_kind refuses")

    # dispatch_kind is provenance only; a profiled cross-vendor pair must not
    # be refused before the selector's cost/capability decision can run.
    unfiltered = None
    try:
        spawn_session(
            state,
            _spawn_req(dispatch_kind="fix", agent_runtime="codex", model="gpt-6-astra"),
        )
    except VerbError as exc:
        unfiltered = exc.code
    _check(unfiltered != "dispatch_policy_violation", "fix does not restore a pair allowlist")

    scoped = None
    try:
        spawn_session(
            state,
            _spawn_req(
                dispatch_kind="infrastructure", agent_runtime="claude_code", model="claude-sonnet-5",
                scope_tags=("state_schema",),
            ),
        )
    except VerbError as exc:
        scoped = exc.code
    _check(
        scoped != "capability_floor_violation",
        "state_schema scope tag does not impose the retired model floor",
    )

    allowed = None
    try:
        allowed = spawn_session(
            state,
            _spawn_req(dispatch_kind="fix", agent_runtime="claude_code", model="claude-opus-5"),
        )
    except VerbError as exc:
        allowed = exc.code
    _check(allowed != "dispatch_policy_violation", "fix with claude-opus-5 is not refused by policy")

    _install_fake_hosts()
    codex_host_key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_HOST)
    session_hosts._REGISTRY[codex_host_key] = _FakeDriverWithChannel()  # noqa: SLF001
    try:
        fixed = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="policy-fix", dispatch_kind="fix",
                agent_runtime="codex", model="gpt-5.6-terra",
            ),
        )
        _check(bool(fixed.get("agent_instance_id")), "fix with gpt-5.6-terra passes")

        original_policy_path = model_dispatch_policy._POLICY_PATH  # noqa: SLF001 -- policy fixture
        with tempfile.TemporaryDirectory() as raw:
            fixture_policy_path = Path(raw) / "policy.json"
            fixture_policy_path.write_text(original_policy_path.read_text(encoding="utf-8"), encoding="utf-8")
            try:
                model_dispatch_policy._POLICY_PATH = fixture_policy_path  # type: ignore[misc]  # noqa: SLF001
                same_vendor_inactive_override = spawn_session(
                    state,
                    _spawn_req(
                        host=_TEST_HOST, lane_id="policy-review-same-inactive", dispatch_kind="review",
                        agent_runtime="codex", model="gpt-5.6-terra", reviewed_report_vendor="codex",
                    ),
                )
                _check(
                    bool(same_vendor_inactive_override.get("agent_instance_id")),
                    "review by report author vendor is accepted without a pairing override",
                )
            finally:
                model_dispatch_policy._POLICY_PATH = original_policy_path  # type: ignore[misc]  # noqa: SLF001

        same_vendor = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="policy-review-same-active", dispatch_kind="review",
                agent_runtime="codex", model="gpt-5.6-terra", reviewed_report_vendor="codex",
            ),
        )
        _check(
            bool(same_vendor.get("agent_instance_id")),
            "review by report author vendor is accepted without pair authority",
        )

        review = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="policy-review-cross", dispatch_kind="review",
                agent_runtime="codex", model="gpt-5.6-terra", reviewed_report_vendor="claude_code",
            ),
        )
        _check(bool(review.get("agent_instance_id")), "cross-vendor review passes")

        infrastructure = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="policy-infrastructure", dispatch_kind="infrastructure",
                agent_runtime="claude_code", model="unprofiled-fixture-model",
            ),
        )
        _check(bool(infrastructure.get("agent_instance_id")), "infrastructure accepts any model")
    finally:
        session_hosts._REGISTRY.pop(codex_host_key, None)  # noqa: SLF001
        _remove_fake_hosts()


def test_spawn_role_class_conflict() -> None:
    state = _state()
    # Legislate 'Some-Office' as principal directly (simulating the D4
    # governance act, out of scope here).
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE,
            "record": {
                "external_id": role_binding_external_id("Some-Office"),
                "role": "Some-Office",
                "role_class": ROLE_CLASS_PRINCIPAL,
            },
        },
    )
    conflict = None
    try:
        spawn_session(
            state, _spawn_req(role_class=ROLE_CLASS_PROJECT, role_name="Some-Office"),
        )
    except VerbError as exc:
        conflict = exc.code
    _check(
        conflict == "role_class_conflict",
        "spawning a 'project' claim against an already-principal-legislated "
        "name -> role_class_conflict",
    )


def test_local_name_defaults_by_role_class() -> None:
    """W6: role_name for a PROJECT-class role, lane_id for everything else."""
    _check(
        resolve_local_name(
            role_class=ROLE_CLASS_PROJECT, role_name="Git-Controller", lane_id="lane-1",
        ) == "Git-Controller",
        "a project-class role is named for the ROLE (it IS the role)",
    )
    _check(
        resolve_local_name(
            role_class=ROLE_CLASS_EPHEMERAL, role_name="Helper", lane_id="lane-1",
        ) == "lane-1",
        "a non-project role stays lane-named (no behaviour change for lane work)",
    )
    _check(
        resolve_local_name(role_class=ROLE_CLASS_PROJECT, role_name="", lane_id="lane-1")
        == "lane-1",
        "a project spawn with no role_name falls back to lane_id",
    )


def test_spawn_refuses_second_session_under_a_live_local_name() -> None:
    """W6 OPERATOR RULING: refused loudly, never a suffix, never an eviction."""
    state = _state()
    _install_fake_hosts()
    try:
        spawn_session(
            state,
            _prepared_project_req(
                state, host=_TEST_HOST, role_name="Git-Controller",
            ),
        )
        code, message = None, ""
        try:
            spawn_session(
                state,
                _prepared_project_req(
                    state, host=_TEST_HOST, role_name="Git-Controller",
                ),
            )
        except VerbError as exc:
            code, message = exc.code, exc.message
        _check(code == "local_name_already_held", f"the second spawn is refused (got {code!r})")
        _check(
            "Git-Controller" in message and "terminate_session" in message,
            "the refusal names the contested name AND the verb that frees it",
        )
        _check("agi-" in message, "and identifies the incumbent by agent_instance_id")
    finally:
        _remove_fake_hosts()


def test_spawn_allows_replacement_after_incumbent_terminated() -> None:
    """The other half of terminate-then-spawn: the refusal must not strand the
    name. This is also why the gate keys on a NON-TERMINAL row — a crashed
    worker swept to 'terminated' frees its name with no operator in the loop,
    the same crash-succession posture the claim path keeps for dead holders."""
    state = _state()
    _install_fake_hosts()
    try:
        first = spawn_session(
            state,
            _prepared_project_req(
                state, host=_TEST_HOST, role_name="Git-Controller",
            ),
        )
        terminate_session(
            state, agent_instance_id=first["agent_instance_id"], directed_by="operator:none",
        )
        replaced = spawn_session(
            state,
            _prepared_project_req(
                state, host=_TEST_HOST, role_name="Git-Controller",
            ),
        )
        _check(
            replaced["agent_instance_id"] != first["agent_instance_id"],
            "once the incumbent is terminated the replacement spawns normally",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_does_not_claim_the_role_binding() -> None:
    """W6 OPERATOR RULING: spawning must NOT claim the durable binding —
    claiming adds a name and never releases the incumbent's, so an automatic
    claim at spawn would evict whoever holds the role."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state,
            _prepared_project_req(
                state, host=_TEST_HOST, role_name="Git-Controller",
            ),
        )
        rows = require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_ROLE_BINDING,
                    "filters": {"external_id": role_binding_external_id("Git-Controller")},
                },
            ),
        )
        _check(rows == [], "no role_binding row exists after the spawn -- nothing was claimed")
        row = read_managed_session(state, result["agent_instance_id"])
        _check(
            row.get("role_name") == "Git-Controller",
            "the ledger records role_name as the spawn's stated INTENT",
        )
        _check(
            row.get("local_name") == "Git-Controller",
            "and local_name, which is what the worker is actually named",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_lane_named_workers_do_not_collide_on_role() -> None:
    """Two ephemeral spawns on DIFFERENT lanes must both proceed — the
    refusal is about one contested name, not about spawning generally."""
    state = _state()
    _install_fake_hosts()
    try:
        spawn_session(state, _spawn_req(host=_TEST_HOST, lane_id="lane-a"))
        code = None
        try:
            spawn_session(state, _spawn_req(host=_TEST_HOST, lane_id="lane-b"))
        except VerbError as exc:
            code = exc.code
        _check(code is None, f"a spawn on a different lane is unaffected (got {code!r})")
    finally:
        _remove_fake_hosts()


def test_list_and_status() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-x", lane_id="lane-x", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    row = session_status(state, "agi-x")
    _check(row["lifecycle_state"] == LIFECYCLE_SPAWNING, "session_status returns the ledger row")
    _check(
        row["coordination_state"] == "legacy_unsupervised",
        "nonterminal historical row projects as legacy_unsupervised",
    )

    not_found = None
    try:
        session_status(state, "agi-nonexistent")
    except VerbError as exc:
        not_found = exc.code
    _check(not_found == "session_not_found", "session_status(unknown) -> session_not_found")

    listed = list_sessions(state, {"lane_id": "lane-x"})["sessions"]
    _check(len(listed) == 1, "list_sessions filters by lane_id")
    _check(
        listed[0]["coordination_state"] == "legacy_unsupervised",
        "list_sessions exposes the same legacy projection",
    )
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-x",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_TERMINATED,
        directed_by="test",
    )
    _check(
        session_status(state, "agi-x")["coordination_state"] == "legacy_unmanaged",
        "terminal historical row projects as legacy_unmanaged",
    )


_TEST_HOST = "test-driver-host"
_TEST_HOST_NO_CHANNEL = "test-driver-host-no-channel"
_TEST_HOST_RUNTIME_ROUTING = "test-driver-runtime-routing"


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeVerifyingChannel(_FakeChannel):
    def verify_driven(self, text: str) -> bool:
        return text in self.sent


class _FakeParkInterruptingVerifyingChannel(_FakeVerifyingChannel):
    def __init__(self) -> None:
        super().__init__()
        self.interrupts = 0

    def interrupt_park(self) -> str:
        self.interrupts += 1
        return "interrupted_park"


class _FakeDriverWithChannel:
    def __init__(self) -> None:
        self.channel = _FakeChannel()

    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class _FakeDriverNoChannel:
    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _install_fake_hosts() -> None:
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch
    session_hosts._REGISTRY[_TEST_HOST_NO_CHANNEL] = _FakeDriverNoChannel()  # noqa: SLF001


def _remove_fake_hosts() -> None:
    session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001
    session_hosts._REGISTRY.pop(_TEST_HOST_NO_CHANNEL, None)  # noqa: SLF001


def test_omitted_host_resolves_to_tmux_before_spawn() -> None:
    """A raw spawn with no host must select tmux before it allocates a lane."""
    state = _state()
    key = (session_hosts.DEFAULT_AGENT_RUNTIME, "tmux")
    original_driver = session_hosts._REGISTRY[key]  # noqa: SLF001 -- test seam
    previous_env_host = os.environ.pop("FLEET_SESSION_HOST", None)
    fake_driver = _FakeDriverWithChannel()
    session_hosts._REGISTRY[key] = fake_driver  # noqa: SLF001 -- test seam
    try:
        result = spawn_session(
            state,
            _spawn_req(
                host=None,
                visibility="tmux",
                lane_id="lane-omitted-host-default",
            ),
        )
        _check(
            result["host"] == "tmux",
            "spawn_session(host omitted) resolves to tmux before any host side effect",
        )
        _check(
            bool(fake_driver.channel.sent),
            "the tmux-resolved fake driver receives the bootstrap turn",
        )
        terminate_session(
            state,
            agent_instance_id=str(result["agent_instance_id"]),
            directed_by="operator:none",
        )
    finally:
        session_hosts._REGISTRY[key] = original_driver  # noqa: SLF001 -- test seam
        if previous_env_host is None:
            os.environ.pop("FLEET_SESSION_HOST", None)
        else:
            os.environ["FLEET_SESSION_HOST"] = previous_env_host


def test_visibility_host_consistency() -> None:
    """Visibility must describe the resolved spawn host, not just the row."""
    state = _state()
    cases = (("visible", "headless"), ("tmux", "headless"), ("headless", "tmux"))
    for visibility, host in cases:
        code = _spawn_error_code(
            state,
            _spawn_req(
                host=host,
                visibility=visibility,
                lane_id=f"lane-visibility-{visibility}-{host}",
            ),
        )
        _check(
            code == "visibility_host_mismatch",
            f"visibility={visibility!r} with host={host!r} refuses before spawn",
        )
    aligned = _spawn_error_code(
        state,
        _spawn_req(host="tmux", visibility="visible", lane_id="lane-visible-tmux-cfg"),
    )
    _check(
        aligned == "host_cannot_spawn",
        "visibility='visible' with host='tmux' clears consistency validation",
    )
    previous_env_host = os.environ.pop("FLEET_SESSION_HOST", None)
    try:
        omitted_headless = _spawn_error_code(
            state,
            _spawn_req(host=None, visibility="headless", lane_id="lane-omitted-headless"),
        )
    finally:
        if previous_env_host is None:
            os.environ.pop("FLEET_SESSION_HOST", None)
        else:
            os.environ["FLEET_SESSION_HOST"] = previous_env_host
    _check(
        omitted_headless == "visibility_host_mismatch",
        "host omission cannot turn an asserted headless visibility into a tmux spawn",
    )


def _spawn_error_code(state: StateManagementInterface, req: SpawnSessionRequest) -> str | None:
    try:
        spawn_session(state, req)
    except VerbError as exc:
        return exc.code
    return None


# -- session_terminal fire+deliver (2026-08-04, acceptance Test C fix slice) --

_TEST_HOST_RAISING = "test-driver-host-raising-channel"


class _FakeChannelRaising:
    def send(self, text: str) -> None:
        del text
        raise RuntimeError("driver channel exploded")


class _FakeDriverRaisingChannel:
    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannelRaising:
        del host_ref
        return _FakeChannelRaising()

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _arm_session_terminal(
    state: StateManagementInterface, *, waiter_instance_id: str, dying_instance_id: str,
) -> None:
    armed = arm_session_dependency(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id=waiter_instance_id,
            condition_kind=CONDITION_SESSION_TERMINAL,
            condition_ref=dying_instance_id,
        ),
    )
    _check(armed.get("armed") is True, "the session_terminal edge armed cleanly via the real verb")


def test_terminate_fires_and_delivers_session_terminal_edge() -> None:
    """Coordinator-seat ruling (1), 2026-08-04: terminate_session now OWNS firing +
    best-effort delivering armed session_terminal edges. Named failing
    mutation: removing the drive_on_delivery call inside
    _fire_session_terminal_dependencies reds ONLY the channel assertion
    below (session_terminal_edges_fired stays 1) -- proving fire and
    deliver are independently exercised, not one assertion covering both."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-1", lane_id="lane-term", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-1", lane_id="lane-term", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-1", dying_instance_id="agi-dying-1",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = terminate_session(state, agent_instance_id="agi-dying-1", directed_by="operator:none")
        _check(result["already_terminal"] is False, "the dying session transitions to terminated")
        _check(
            result.get("session_terminal_edges_fired") == 1,
            f"terminate_session reports exactly 1 edge fired (got {result})",
        )
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            f"the waiter's driver channel receives exactly one drive-on-delivery notice (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


def test_retire_composes_terminate_no_double_delivery() -> None:
    """Coordinator-seat ruling (2): retire_session composes terminate_session as its
    OWN first step and must not ALSO fire/deliver a second time. Named
    failing mutation: restoring retire_session's own separate
    _fire_session_terminal_dependencies call (the pre-fix design) would
    double the channel.sent count (edges already fired-guarded server-side,
    so it would not double-count `dependencies_fired`, but a NEW edge armed
    between the two calls in the old design could have double-delivered --
    this test pins the single-call-site invariant directly)."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-2", lane_id="lane-retire", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-2", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-2", lane_id="lane-retire", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-2", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-2", dying_instance_id="agi-dying-2",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = retire_session(state, agent_instance_id="agi-dying-2", directed_by="operator:none")
        _check(
            result["already_retired"] is False
            and result["dependencies_fired"] == 1
            and result["host_action"] == "none_available",
            f"retire_session reports exactly 1 edge fired via composition, and that the "
            f"operator-hosted process was not stopped (got {result})",
        )
        _check(
            len(driver.channel.sent) == 1,
            f"the waiter's channel receives exactly ONE notice, not two, under composition "
            f"(got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


def test_terminate_delivery_fault_is_contained() -> None:
    """Position 4 (containment, carried from slice 1): a raising driver
    channel must never fail terminate_session itself -- the fire (ledger
    write) already succeeded before delivery was even attempted. Named
    failing mutation: removing drive_on_delivery's own internal try/except
    (session_lifecycle_verbs.py) would let this RuntimeError propagate and
    red this entire test with an unhandled exception."""
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-3", lane_id="lane-fault", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-3", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-3", lane_id="lane-fault", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_RAISING,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-3", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-3", dying_instance_id="agi-dying-3",
        )
        raised = False
        result: dict[str, Any] = {}
        try:
            result = terminate_session(state, agent_instance_id="agi-dying-3", directed_by="operator:none")
        except Exception:  # noqa: BLE001 -- the whole point is it must NOT raise
            raised = True
        _check(not raised, "a raising waiter driver channel does not propagate out of terminate_session")
        _check(
            result.get("already_terminal") is False
            and result.get("session_terminal_edges_fired") == 1,
            f"terminate_session still succeeds and still reports the edge fired (got {result})",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST_RAISING, None)  # noqa: SLF001


def test_already_terminal_catches_orphaned_edge() -> None:
    """Coordinator-seat ruling (3), included: a repeat terminate_session call on an
    already-terminal row still sweeps for an edge armed AFTER the session
    died -- the success path only runs once per transition and could never
    catch this orphan class otherwise. Named failing mutation: removing the
    firing call from the already_terminal branch reds this test's
    session_terminal_edges_fired/channel assertions while leaving the
    already_terminal=True assertion green (proving the two are
    independently exercised)."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-4", lane_id="lane-orphan", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-4", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        # Terminate FIRST, with no edge armed yet -- nothing to fire.
        first = terminate_session(state, agent_instance_id="agi-dying-4", directed_by="operator:none")
        _check(
            first["already_terminal"] is False and first.get("session_terminal_edges_fired") == 0,
            f"the first terminate call fires nothing -- no edge was armed yet (got {first})",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-4", lane_id="lane-orphan", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-4", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        # Arm the edge AFTER the target already died -- the orphan case.
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-4", dying_instance_id="agi-dying-4",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        second = terminate_session(state, agent_instance_id="agi-dying-4", directed_by="operator:none")
        _check(second["already_terminal"] is True, "the repeat call still reports already_terminal")
        _check(
            second.get("session_terminal_edges_fired") == 1,
            f"the repeat call catches the orphaned edge (got {second})",
        )
        _check(
            len(driver.channel.sent) == 1,
            f"the orphaned edge's waiter is still notified (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


# -- lane_charter capture + spawn_session first-turn (phase 2 slice 6) --


def test_capture_lane_charter_validation_errors() -> None:
    state = _state()
    for kwargs, code in [
        ({"lane_id": "", "charter_text": "text", "captured_at": "2026-08-06T00:00:00Z"},
         "missing_lane_id"),
        ({"lane_id": "lane-1", "charter_text": "", "captured_at": "2026-08-06T00:00:00Z"},
         "missing_charter_text"),
        ({"lane_id": "lane-1", "charter_text": "text", "captured_at": ""},
         "missing_captured_at"),
    ]:
        raised_code = None
        try:
            capture_lane_charter(state, CaptureLaneCharterRequest(**kwargs))  # type: ignore[arg-type]
        except VerbError as exc:
            raised_code = exc.code
        _check(raised_code == code, f"capture_lane_charter({kwargs}) -> {code} (got {raised_code!r})")


def test_capture_lane_charter_is_insert_only_and_supersedes_by_recency() -> None:
    """RED-FIRST intent: a naive 'update in place' implementation would make
    resolve_lane_charter return whichever row was written LAST regardless of
    captured_at ordering AND would leave only one row on record; this test
    pins BOTH the supersede-by-recency read and (indirectly, since a second
    capture never errors as a duplicate) the insert-only write."""
    state = _state()
    first = capture_lane_charter(
        state,
        CaptureLaneCharterRequest(
            lane_id="lane-charter-supersede",
            charter_text="First charter text.",
            captured_at="2026-08-06T00:00:00+00:00",
            brief_ref="workbench/first.md",
            directed_by="operator:none",
        ),
    )
    _check(first["charter_text"] == "First charter text.", "the first capture stores its own text")
    resolved_after_first = resolve_lane_charter(state, "lane-charter-supersede")
    _check(
        resolved_after_first is not None
        and resolved_after_first.charter_text == "First charter text."
        and resolved_after_first.brief_ref == "workbench/first.md"
        and resolved_after_first.captured_at == "2026-08-06T00:00:00+00:00",
        f"resolve_lane_charter returns the only row on file, full record "
        f"(got {resolved_after_first!r})",
    )

    second = capture_lane_charter(
        state,
        CaptureLaneCharterRequest(
            lane_id="lane-charter-supersede",
            charter_text="Second, superseding charter text.",
            captured_at="2026-08-06T00:01:00+00:00",
            brief_ref="workbench/second.md",
            directed_by="operator:none",
        ),
    )
    _check(
        second["charter_text"] == "Second, superseding charter text.",
        "the second capture stores its own text",
    )
    resolved_after_second = resolve_lane_charter(state, "lane-charter-supersede")
    _check(
        resolved_after_second is not None
        and resolved_after_second.charter_text == "Second, superseding charter text.",
        f"RED-vs-GREEN: resolve_lane_charter follows the LATEST captured_at, superseding "
        f"the first row rather than editing it or picking arbitrarily (got "
        f"{resolved_after_second!r})",
    )


def test_resolve_lane_charter_empty_for_unknown_lane() -> None:
    state = _state()
    _check(
        resolve_lane_charter(state, "lane-with-no-charter-ever-captured") is None,
        "a lane with no captured charter resolves to None, not a fault",
    )


def test_spawn_session_drives_charter_as_first_turn_byte_exact() -> None:
    """RED-FIRST (phase 2 slice 6, Finding 0's fix): before this slice,
    spawn_session drove NO first turn at all — on the CLAUDE watch path, a
    watch-armed worker's Stop-hook wake loop, which only arms once the first
    turn completes, would never arm for a pristine spawn (a managed CODEX
    worker has no such loop to arm — codex-0147-dead-spool-retirement,
    2026-08-13 — but still needs its first turn driven the same way). Also
    pins the design-check-in
    ruling's byte-exact fidelity requirement end to end: capture -> resolve
    -> drive must never re-serialize or mangle the operator's words — now
    scoped to the charter BODY specifically, since phase 3's provenance
    frame (2026-08-06 incident finding, coordinator-seat ruling) wraps it in
    non-charter framing text; the frame must never touch the body itself."""
    state = _state()
    _install_fake_hosts()
    try:
        charter_text = "Operator's verbatim founding words — apostrophes and em—dashes intact."
        captured = capture_lane_charter(
            state,
            CaptureLaneCharterRequest(
                lane_id="lane-charter-spawn",
                charter_text=charter_text,
                captured_at="2026-08-06T00:00:00+00:00",
                brief_ref="workbench/brief.md",
                directed_by="operator:none",
            ),
        )
        result = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="lane-charter-spawn",
                spawned_by_role="watch-migration-impl",
            ),
        )
        _check(
            result["first_turn_source"] == FIRST_TURN_SOURCE_CHARTER,
            f"spawn_session reports first_turn_source='charter' when a lane_charter row "
            f"is on file (got {result['first_turn_source']!r})",
        )
        _check(result["first_turn_delivered"] is True, "the first turn was delivered")
        _check(result["first_turn_error"] == "", "no error on a successful delivery")
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        _check(len(driver.channel.sent) == 1, "exactly one first-turn send")
        driven_text = driver.channel.sent[0] if driver.channel.sent else ""
        _check(
            driven_text.endswith(captured["charter_text"]) and driven_text.endswith(charter_text),
            f"RED-vs-GREEN: the driven text ENDS WITH the exact stored charter_text, "
            f"byte-exact — no mangling anywhere in capture -> resolve -> drive (got "
            f"{driven_text!r})",
        )
        _check(
            "NOT a live conversation" in driven_text
            and "not yet registered" in driven_text
            and result["agent_instance_id"] in driven_text
            and "watch-migration-impl" in driven_text
            and "workbench/brief.md" in driven_text
            and "2026-08-06T00:00:00+00:00" in driven_text,
            f"the provenance frame (not-live-conversation + not-yet-registered + agent_instance_id "
            f"+ spawned_by_role + brief_ref + captured_at) wraps the charter body (got "
            f"{driven_text!r})",
        )
    finally:
        _remove_fake_hosts()


def test_charter_frame_ships_instruments_and_the_claim_first_instruction() -> None:
    """LIF-05 — measured 2026-08-19 on ``lane-drive-honesty``: a
    charter-founded lane judged that it could not verify its dispatcher was
    real or that the operator sentence quoted in its charter had ever been
    said, DECLINED the work, and went idle at the prompt.

    ★ THE REFUSAL WAS CORRECT AND THIS MUST NOT SUPPRESS IT. A relayed quote
    is unverifiable from the lane's seat — the fleet's own rule about a
    relayed assent hardening into a fabricated ruling, turned on the
    dispatcher. So this pins the OPPOSITE of an obedience nudge: the frame
    must concede the limit, hand over first-party instruments instead of
    insistence, and say in as many words that declining is legitimate. An
    assertion below fails if a future edit tries to buy compliance instead.

    The trap that made the incident expensive was second-order: claiming the
    role binding was step 1 of the charter the lane refused, so it never
    claimed, and peer_send_by_name answered role_binding_vacant — the fleet
    could not reach BY NAME the one session that most needed a follow-up.
    Hence the claim instruction is stated FIRST and independently of the
    work, and names the row's own role_name so the lane never has to guess.
    """
    state = _state()
    _install_fake_hosts()
    try:
        charter_text = "Operator's verbatim founding words — em—dashes intact."
        capture_lane_charter(
            state,
            CaptureLaneCharterRequest(
                lane_id="lane-lif05", charter_text=charter_text,
                captured_at="2026-08-19T00:00:00+00:00", brief_ref="workbench/brief.md",
                directed_by="operator:none",
            ),
        )
        spawn_session(
            state,
            _prepared_project_req(
                state,
                host=_TEST_HOST, lane_id="lane-lif05", role_name="lane-lif05",
                spawned_by_role="coordinator-seat",
            ),
        )
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        driven = driver.channel.sent[0] if driver.channel.sent else ""
        _check(
            driven.endswith(charter_text),
            "the charter BODY is still byte-exact and still last — the frame only prefixes",
        )
        _check(
            "session_status" in driven and "peer_list" in driven,
            "the frame names the checks the lane can run first-party, instead of "
            "asserting the frame and expecting belief",
        )
        _check(
            "not the ruling itself" in driven,
            "the frame CONCEDES the one thing the lane genuinely cannot verify: an "
            "operator sentence quoted inside the charter",
        )
        _check(
            "Declining this work is a legitimate outcome" in driven,
            "ANTI-SUPPRESSION: the frame says standing down is legitimate — a version "
            "that buys compliance instead fails here",
        )
        _check(
            "CLAIM YOUR ROLE BINDING FIRST" in driven,
            "the claim instruction is separated from the work and comes first",
        )
        _check(
            "'lane-lif05'" in driven and "cannot evict anyone" in driven,
            "...and names the row's OWN role_name, plus why claiming it evicts nobody "
            "(peer_claim_role refuses a live incumbent — the 2026-08-14 ruling stands)",
        )
        _check(
            "reach you BY NAME" in driven,
            "the frame states the CONSEQUENCE of not claiming — the addressability trap "
            "itself, which is what made the incident unrecoverable by message",
        )
    finally:
        _remove_fake_hosts()


def test_ephemeral_charter_frame_never_requests_a_role() -> None:
    """An ephemeral charter is not a nameless project-role spawn."""
    state = _state()
    _install_fake_hosts()
    try:
        capture_lane_charter(
            state,
            CaptureLaneCharterRequest(
                lane_id="lane-lif05-noname", charter_text="body",
                captured_at="2026-08-19T00:00:00+00:00", brief_ref="workbench/brief.md",
                directed_by="operator:none",
            ),
        )
        spawn_session(
            state, _spawn_req(host=_TEST_HOST, lane_id="lane-lif05-noname"),
        )
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        driven = driver.channel.sent[0] if driver.channel.sent else ""
        _check(
            "no role exists or will be assigned" in driven,
            "an ephemeral charter states that no durable role will be assigned",
        )
        _check(
            "CLAIM YOUR ROLE BINDING FIRST" not in driven
            and "ask rather than inventing one" not in driven,
            "an ephemeral charter neither claims nor asks for a role name",
        )
        _check(
            "`solet-bridge inbox`" in driven and "`solet inbox`" not in driven,
            "the charter frame names the bridge inbox command, never the manager command",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_drives_fallback_when_no_charter_on_file() -> None:
    """Ordering-ruling guard (a): the no-charter fallback must be a small,
    single-shot, terminating turn — never a question, never a wait."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST, lane_id="lane-charter-none-spawn"),
        )
        _check(
            result["first_turn_source"] == FIRST_TURN_SOURCE_FALLBACK,
            f"spawn_session reports first_turn_source='fallback' when no lane_charter row "
            f"is on file (got {result['first_turn_source']!r})",
        )
        _check(result["first_turn_delivered"] is True, "the fallback turn was delivered")
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        expected = build_fallback_first_turn(
            spawned_by_role="",
            role_class="ephemeral",
            role_name="",
            brief_ref="fixtures/brief.md",
        )
        _check(
            driver.channel.sent == [expected],
            f"the exact rendered fallback text is driven, unmodified (got "
            f"{driver.channel.sent!r})",
        )
        _check(
            "{" not in expected and "}" not in expected,
            "the JSON template is rendered with no unsubstituted placeholder reaching a pane",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_preserves_agent_runtime() -> None:
    """Regression for the live 2026-08-22 Codex spawn failure.

    The spawn half resolved ``(agent_runtime='codex', host='headless')`` and
    created a real Codex app-server, but the immediate first-turn half rebuilt
    the driver lookup without ``agent_runtime``.  That silently selected the
    default Claude driver, which could not recognize the Codex host reference,
    and returned ``first_turn_delivered=False`` over a live stranded worker.

    The two runtime drivers deliberately disagree here: only the Codex driver
    has a channel.  Dropping runtime propagation therefore makes this fixture
    red without any real process or model turn.
    """
    state = _state()
    codex_key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_HOST_RUNTIME_ROUTING)
    claude_key = (session_hosts.AGENT_RUNTIME_CLAUDE_CODE, _TEST_HOST_RUNTIME_ROUTING)
    prior_codex = session_hosts._REGISTRY.get(codex_key)  # noqa: SLF001
    prior_claude = session_hosts._REGISTRY.get(claude_key)  # noqa: SLF001
    codex_driver = _FakeDriverWithChannel()
    session_hosts._REGISTRY[codex_key] = codex_driver  # noqa: SLF001
    session_hosts._REGISTRY[claude_key] = _FakeDriverNoChannel()  # noqa: SLF001
    try:
        result = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST_RUNTIME_ROUTING,
                agent_runtime=session_hosts.AGENT_RUNTIME_CODEX,
                lane_id="lane-codex-runtime-first-turn",
            ),
        )
        _check(
            result["first_turn_delivered"] is True,
            "Codex spawn resolves the first-turn channel through the Codex runtime driver",
        )
        _check(
            len(codex_driver.channel.sent) == 1,
            "the first turn reaches the same runtime-specific driver that performed spawn",
        )
    finally:
        if prior_codex is None:
            session_hosts._REGISTRY.pop(codex_key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[codex_key] = prior_codex  # noqa: SLF001
        if prior_claude is None:
            session_hosts._REGISTRY.pop(claude_key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[claude_key] = prior_claude  # noqa: SLF001


def test_fallback_first_turn_hands_off_to_the_spawner() -> None:
    """SPN-01 — measured 2026-08-19 (wave-2 dispatch): two of four freshly
    spawned lanes sat idle ~30 minutes on one bootstrap turn each, and the
    only difference from the two that started was a post-spawn driving
    message (confirmed by intervention, 2/2).

    The mechanism is in the TEXT, not the transport. The old fallback said
    "acknowledge, then stop; your work dispatch arrives separately over the
    peer channel" — a handoff to NOBODY. It names a dispatch that exists only
    if a seat remembers, and it ends the turn with the lane holding nothing
    and having told no one it is waiting. The turn now hands off to the
    SPAWNER, which is the only side of the gap this module controls.

    Also pins the guard the original text existed to satisfy: small,
    completing in ONE turn, never a question and never a wait — so a "fix"
    that tells the lane to poll or block for its dispatch fails here.
    """
    state = _state()
    _install_fake_hosts()
    try:
        spawn_session(
            state,
            _prepared_project_req(
                state,
                host=_TEST_HOST, lane_id="lane-spn01", role_name="lane-spn01",
                brief_ref="fixtures/spn01-brief.md", spawned_by_role="coordinator-seat",
            ),
        )
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        driven = driver.channel.sent[0] if driver.channel.sent else ""
        _check(
            "coordinator-seat" in driven and "awaiting dispatch" in driven,
            "the bootstrap turn ends by TELLING THE SPAWNER it is up and waiting — the "
            "handoff the old text did not have",
        )
        _check(
            "Do not skip this one" in driven,
            "...and says why, in the words of the measurement: a turn that ends without "
            "telling anyone is how a lane sits idle while its dispatcher believes it started",
        )
        _check(
            "'lane-spn01'" in driven and "report_alive" in driven,
            "the turn claims the row's OWN role name and arms the heartbeat contract — "
            "addressable and visible to the overdue sweep, not merely awake",
        )
        _check(
            "fixtures/spn01-brief.md" in driven,
            "the turn points the lane at its own recorded brief_ref",
        )
        _check(
            "none is a wait" in driven and "Then stop." in driven,
            "ORDERING GUARD: the turn still completes in one turn and ends in a stop — a "
            "version that tells the lane to poll or block for its dispatch reds here",
        )
        _check(
            "?" not in driven,
            "...and asks the lane NO question (the guard's other half — a question turn "
            "strands a worker that has nobody to answer it)",
        )
    finally:
        _remove_fake_hosts()


def test_ephemeral_fallback_preserves_an_absolute_brief_ref() -> None:
    """An out-of-scope absolute ref retains the pre-snapshot fallback behavior."""
    state = _state()
    _install_fake_hosts()
    try:
        brief_ref = "/tmp/briefs/codex-bootstrap.md sha256 deadbeef"
        spawn_session(
            state,
            _spawn_req(host=_TEST_HOST, lane_id="lane-spn01-bare", brief_ref=brief_ref),
        )
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        _check(
            len(driver.channel.sent) == 1 and brief_ref in driver.channel.sent[0],
            "an absolute non-workbench brief_ref retains the literal fallback behavior",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_failure_is_visible_not_blocking() -> None:
    """Ordering-ruling guard (b): a first-turn delivery failure must be
    VISIBLE (surfaced in the spawn result) and must NEVER block the spawn
    itself — the row still lands, just with first_turn_delivered=False."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST_NO_CHANNEL, lane_id="lane-charter-no-channel"),
        )
        _check(
            result["first_turn_delivered"] is False,
            "a host with no driver channel reports first_turn_delivered=False",
        )
        _check(
            result["first_turn_error"] != "",
            f"RED-vs-GREEN: the failure is surfaced in the spawn result, never silent "
            f"(got first_turn_error={result['first_turn_error']!r})",
        )
        _check(
            result["lifecycle_state"] == LIFECYCLE_SPAWNING,
            "the spawn itself is NOT blocked by a first-turn delivery failure",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_send_raising_is_contained() -> None:
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST_RAISING, lane_id="lane-charter-raising"),
        )
        _check(
            result["first_turn_delivered"] is False,
            "a driver channel that raises on send() reports first_turn_delivered=False",
        )
        _check(
            "driver channel exploded" in result["first_turn_error"],
            f"the underlying exception text is surfaced (got {result['first_turn_error']!r})",
        )
        _check(
            result["lifecycle_state"] == LIFECYCLE_SPAWNING,
            "the spawn itself is NOT blocked by a raising driver channel",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST_RAISING, None)  # noqa: SLF001


def test_clear_session_sends_and_can_park() -> None:
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-clear-1", lane_id="lane-clear", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-clear-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = clear_session(
            state, agent_instance_id="agi-clear-1", park=False, directed_by="operator:none",
        )
        _check(driver.channel.sent == ["/clear"], "clear_session sends '/clear' over the channel")
        _check(
            result == {
                "lifecycle_state": LIFECYCLE_LIVE, "parked": False,
                "dispatched": True, "cleared": None,
                "clear_verification": "unsupported_on_driver",
            },
            "clear_session(park=False) does not transition lifecycle_state",
        )
        # GAU-09: this fixture's channel has no read-back surface, so the
        # verb must say the effect is UNVERIFIED rather than imply success.
        # Asserted as a whole-dict equality on purpose -- a field silently
        # dropped from the envelope is exactly the regression that would put
        # the "success means cleared" reading back.
        _check(
            result["cleared"] is None,
            "GAU-09: a channel with no read-back surface never claims cleared=True",
        )
        _check(
            read_managed_session(state, "agi-clear-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is still 'live' after a non-parking clear",
        )

        parked_result = clear_session(
            state, agent_instance_id="agi-clear-1", park=True, directed_by="operator:none",
        )
        _check(
            driver.channel.sent == ["/clear", "/clear"],
            "clear_session(park=True) still sends '/clear' before parking",
        )
        _check(
            parked_result == {
                "lifecycle_state": LIFECYCLE_PARKED, "parked": True,
                "dispatched": True, "cleared": None,
                "clear_verification": "unsupported_on_driver",
            },
            "clear_session(park=True) drives live -> parked",
        )
        _check(
            read_managed_session(state, "agi-clear-1")["lifecycle_state"] == LIFECYCLE_PARKED,
            "the ledger row is now 'parked'",
        )
    finally:
        _remove_fake_hosts()


def test_compact_session_sends_no_park() -> None:
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-compact-1", lane_id="lane-compact", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-compact-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001
        result = compact_session(state, agent_instance_id="agi-compact-1")
        _check(
            driver.channel.sent == ["/compact"],
            "compact_session sends '/compact' over the channel",
        )
        _check(
            result == {"lifecycle_state": LIFECYCLE_LIVE},
            "compact_session never parks (no lifecycle transition)",
        )
    finally:
        _remove_fake_hosts()


def test_drive_session_dispatches_and_unparks() -> None:
    """Green legs + their named failing mutations: removing the
    ``channel.send(text)`` call reds "sends the text"; removing the
    parked->live transition reds "drives parked -> live"; removing the
    confirmed-drive guard from ``_rearm_report_by`` reds "unverified drive
    preserves the reporting deadline"."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-1", lane_id="lane-drive", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        # Dispatch-at-spawn: a 'spawning' row is drivable (the brief can be
        # sent the moment spawn_session returns); the registration hook
        # still owns the spawning->live edge, so no transition here.
        report_by_before_first = read_managed_session(state, "agi-drive-1").get("report_by")
        spawning_result = drive_session(
            state, agent_instance_id="agi-drive-1", text="Brief: build the tmux driver.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent == ["Brief: build the tmux driver."],
            "drive_session sends the text over the channel",
        )
        _check(
            spawning_result == {
                "lifecycle_state": LIFECYCLE_SPAWNING, "unparked": False,
                "dispatched": True, "submitted": None,
                "drive_verification": "unsupported_on_driver",
                "drive_on_delivery_detail": None,
            },
            "drive_session on a 'spawning' row dispatches without a transition",
        )
        report_by_after_first = read_managed_session(state, "agi-drive-1").get("report_by")
        _check(
            report_by_after_first == report_by_before_first,
            "an unsupported-on-driver dispatch preserves the reporting deadline",
        )

        # parked -> live: the §3.2 "new dispatch through the driver channel"
        # edge, owned by this verb alone.
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-1", from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_PARKED, directed_by="steward:none",
        )
        driver.channel = _FakeParkInterruptingVerifyingChannel()
        report_by_before_confirmed = read_managed_session(state, "agi-drive-1").get("report_by")
        unpark_result = drive_session(
            state, agent_instance_id="agi-drive-1", text="Follow-up: land it.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent == ["Follow-up: land it."],
            "a second drive_session sends the follow-up text",
        )
        _check(
            driver.channel.interrupts == 1,
            "a parked drive interrupts its declared parked-pane channel before sending text",
        )
        _check(
            unpark_result.get("drive_on_delivery_detail") == "interrupted_park",
            "a parked drive reports the measured interrupt recovery detail",
        )
        _check(
            unpark_result == {
                "lifecycle_state": LIFECYCLE_LIVE, "unparked": True,
                "dispatched": True, "submitted": True,
                "drive_verification": "confirmed",
                "drive_on_delivery_detail": "interrupted_park",
            },
            "drive_session drives parked -> live (unparked=True)",
        )
        _check(
            read_managed_session(state, "agi-drive-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is 'live' after driving a parked session",
        )
        confirmed_row = read_managed_session(state, "agi-drive-1")
        _check(
            confirmed_row.get("report_by") != report_by_before_confirmed
            and confirmed_row.get("report_by_source") == "confirmed_drive",
            "only a confirmed drive re-arms report_by with confirmed-drive provenance",
        )
    finally:
        _remove_fake_hosts()


def test_drive_session_errors() -> None:
    _install_fake_hosts()
    try:
        state = _state()

        not_found = None
        try:
            drive_session(
                state, agent_instance_id="agi-nonexistent", text="work", directed_by="x",
            )
        except VerbError as exc:
            not_found = exc.code
        _check(not_found == "session_not_found", "drive_session(unknown) -> session_not_found")

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-err", lane_id="lane-drive-err", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001
        empty = None
        try:
            drive_session(state, agent_instance_id="agi-drive-err", text="  ", directed_by="x")
        except VerbError as exc:
            empty = exc.code
        _check(empty == "empty_text", "drive_session(blank text) -> empty_text")
        _check(
            driver.channel.sent == [],
            "a refused empty dispatch never touches the channel",
        )

        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-err", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED, directed_by="operator:none",
        )
        conflict = None
        try:
            drive_session(state, agent_instance_id="agi-drive-err", text="work", directed_by="x")
        except VerbError as exc:
            conflict = exc.code
        _check(
            conflict == "lifecycle_state_conflict",
            "drive_session on a terminated row -> lifecycle_state_conflict",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-nochan", lane_id="lane-drive-nochan", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_NO_CHANNEL,
            ),
        )
        unsupported = None
        try:
            drive_session(
                state, agent_instance_id="agi-drive-nochan", text="work", directed_by="x",
            )
        except VerbError as exc:
            unsupported = exc.code
        _check(
            unsupported == "unsupported_on_host",
            "drive_session on a host with no driver channel -> unsupported_on_host",
        )
    finally:
        _remove_fake_hosts()


def test_clear_and_compact_errors() -> None:
    _install_fake_hosts()
    try:
        state = _state()

        not_found = None
        try:
            clear_session(state, agent_instance_id="agi-nonexistent", park=False, directed_by="x")
        except VerbError as exc:
            not_found = exc.code
        _check(not_found == "session_not_found", "clear_session(unknown) -> session_not_found")

        not_found_compact = None
        try:
            compact_session(state, agent_instance_id="agi-nonexistent")
        except VerbError as exc:
            not_found_compact = exc.code
        _check(
            not_found_compact == "session_not_found",
            "compact_session(unknown) -> session_not_found",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-terminal", lane_id="lane-terminal", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-terminal", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED, directed_by="operator:none",
        )
        conflict = None
        try:
            clear_session(state, agent_instance_id="agi-terminal", park=False, directed_by="x")
        except VerbError as exc:
            conflict = exc.code
        _check(
            conflict == "lifecycle_state_conflict",
            "clear_session on a terminated row -> lifecycle_state_conflict",
        )
        compact_conflict = None
        try:
            compact_session(state, agent_instance_id="agi-terminal")
        except VerbError as exc:
            compact_conflict = exc.code
        _check(
            compact_conflict == "lifecycle_state_conflict",
            "compact_session on a terminated row -> lifecycle_state_conflict",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-no-channel", lane_id="lane-no-channel", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_NO_CHANNEL,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-no-channel", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        unsupported = None
        try:
            clear_session(state, agent_instance_id="agi-no-channel", park=False, directed_by="x")
        except VerbError as exc:
            unsupported = exc.code
        _check(
            unsupported == "unsupported_on_host",
            "clear_session on a host with no driver channel -> unsupported_on_host",
        )

        unsupported_operator = None
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-operator", lane_id="lane-operator", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-operator", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        try:
            compact_session(state, agent_instance_id="agi-operator")
        except VerbError as exc:
            unsupported_operator = exc.code
        _check(
            unsupported_operator == "unsupported_on_host",
            "compact_session on the real degenerate 'operator' driver -> unsupported_on_host",
        )
    finally:
        _remove_fake_hosts()


def test_terminate_idempotent() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-t", lane_id="lane-t", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-t", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    first = terminate_session(state, agent_instance_id="agi-t", directed_by="operator:none")
    _check(first["already_terminal"] is False, "first terminate_session lands the transition")
    second = terminate_session(state, agent_instance_id="agi-t", directed_by="operator:none")
    _check(
        second["already_terminal"] is True,
        "a second terminate_session is idempotent (already_terminal)",
    )

    not_found = None
    try:
        terminate_session(state, agent_instance_id="agi-none", directed_by="operator:none")
    except VerbError as exc:
        not_found = exc.code
    _check(not_found == "session_not_found", "terminate_session(unknown) -> session_not_found")

    # Pin the operator-host decision explicitly (2026-08-03/04 Dawn ruling):
    # every normal peer-registered fleet session is 'operator'-hosted and
    # was never dispatched through spawn_session, so the degenerate
    # driver's terminate() always raises HostCannotSpawnError ("I didn't
    # spawn this, I can't kill it") -- that MUST be tolerated as "no host
    # action available", not propagated as a verb failure, or no
    # operator-hosted row could ever reach 'terminated' (wedging
    # session_sweep.sweep_lane_closed_dependencies for any lane touched by
    # a non-headless session). test_terminate_idempotent above already
    # exercises this path (host="operator") and passing IS the pin; this
    # assertion just makes the "why" explicit for a future reader.
    _check(
        first["lifecycle_state"] == LIFECYCLE_TERMINATED and not first["already_terminal"],
        "an operator-hosted row still reaches 'terminated' without the verb "
        "raising -- the degenerate driver's HostCannotSpawnError is "
        "tolerated, not propagated",
    )


def _real_short_lived_popen_fn(*_a: object, **_k: object) -> subprocess.Popen[str]:
    """Ignores the incoming cmd/env entirely -- spawns a real, harmless,
    short-lived child so the test exercises actual OS-level kill mechanics
    without ever invoking the real ``claude`` binary (mirrors
    ``headless_adapter_smoke.py``'s pattern)."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


def test_terminate_session_kills_the_real_headless_process() -> None:
    """2026-08-03/04 Dawn ruling, on the live e2e's finding: retire_session
    returned 'retired' over a process that kept running 12+ seconds later.
    terminate_session (session_lifecycle_verbs.py) now resolves the row's
    host driver and calls its REAL terminate() before the ledger write --
    this asserts the actual OS process dies, not just that the verb
    returns ok (a green here must fail if the driver.terminate() call is
    ever dropped again -- 'a green lies four ways', name the failing
    mutation). Swaps a real HeadlessHostDriver (harmless popen_fn, no real
    claude binary) into session_hosts._REGISTRY['headless'] for the
    duration of the test."""
    with tempfile.TemporaryDirectory() as tmp:
        mcp_config = Path(tmp) / ".mcp.json"
        mcp_config.write_text("{}")
        # R4 Package C (2026-08-10): populate rung 1 so the worker-hook
        # resolution ladder resolves -- matching a real dev checkout's own
        # shape, same fixture pattern as headless_adapter_smoke.py's own.
        hooks_dir = Path(tmp) / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for _hook_name in _WORKER_INJECTED_HOOK_FILENAMES:
            (hooks_dir / _hook_name).write_text("#!/usr/bin/env python3\n")
        real_driver = HeadlessHostDriver(
            claude_bin=sys.executable, solet_name="testhom",
            permission_mode="bypassPermissions", mcp_config_path=mcp_config,
            cwd=Path(tmp), popen_fn=_real_short_lived_popen_fn,
        )
        prior = session_hosts._REGISTRY.get("headless")  # noqa: SLF001 -- test-only monkeypatch
        session_hosts._REGISTRY["headless"] = real_driver  # noqa: SLF001
        try:
            state = _state()
            agent_instance_id = "agi-real-reap"
            insert_managed_session(
                state,
                ManagedSessionSpec(
                    agent_instance_id=agent_instance_id, lane_id="lane-reap", brief_ref="",
                    work_class="read_only", budget_line="b1", host="headless",
                ),
            )
            transition_lifecycle_state(
                state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE, directed_by="operator:none",
            )
            host_ref = real_driver.spawn(
                {"agent_instance_id": agent_instance_id, "lane_id": "lane-reap"},
            )
            set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)
            _check(
                real_driver.alive(host_ref),
                "precondition: the real spawned process is alive before terminate_session",
            )

            result = terminate_session(
                state, agent_instance_id=agent_instance_id, directed_by="operator:none",
                grace_seconds=2,
            )
            _check(
                result["lifecycle_state"] == LIFECYCLE_TERMINATED,
                "terminate_session lands the ledger transition",
            )

            deadline = time.monotonic() + 5.0
            dead = False
            while time.monotonic() < deadline:
                if not real_driver.alive(host_ref):
                    dead = True
                    break
                time.sleep(0.1)
            _check(
                dead,
                "terminate_session actually killed the real OS process, not "
                "just the ledger row",
            )
        finally:
            if prior is not None:
                session_hosts._REGISTRY["headless"] = prior  # noqa: SLF001
            else:
                session_hosts._REGISTRY.pop("headless", None)  # noqa: SLF001


def test_retire_idempotent_and_redrivable() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-r", lane_id="lane-r", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
        ),
    )
    # ManagedSessionSpec has no agent_session_id field (it arrives later, at
    # the registration hook, §3.2) — set it directly to simulate that.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-r"}},
        {"agent_session_id": "ases-r"},
    )
    win_cardinality_gate(
        state, CardinalityGatedClaim(
            agent_session_id="ases-r", requested_role="Some-Lane", agent_instance_id="agi-r",
        ),
    )
    _check(
        read_session_role_claim(state, "ases-r") is not None,
        "precondition: the session_role_claim row exists before retire",
    )
    first = retire_session(state, agent_instance_id="agi-r", directed_by="operator:none")
    _check(first["already_retired"] is False, "first retire_session lands terminated -> retired")
    _check(
        read_managed_session(state, "agi-r")["lifecycle_state"] == LIFECYCLE_RETIRED,
        "the ledger row is now 'retired'",
    )
    _check(
        read_session_role_claim(state, "ases-r") is None,
        "retire_session's step 2 (populated agent_session_id path) actually "
        "prunes the session_role_claim cardinality row -- this path is only "
        "reachable with agent_session_id populated, which is exactly what "
        "the registration hook now does for every real spawn",
    )
    second = retire_session(state, agent_instance_id="agi-r", directed_by="operator:none")
    _check(
        second["already_retired"] is True,
        "re-running retire_session on an already-retired row is a no-op "
        "(re-drivable by construction, never wedged)",
    )


def test_report_alive() -> None:
    state = _state()

    def _report(status: str) -> dict[str, object]:
        return report_alive(
            state, agent_instance_id="agi-a", status=status, directed_by="operator:none",
        )

    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-a", lane_id="lane-a", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    before = read_managed_session(state, "agi-a").get("report_by")
    result = _report("idle")
    _check(result["lifecycle_state"] == "idle", "report_alive(idle) transitions live -> idle")
    after = read_managed_session(state, "agi-a")
    _check(after["report_by"] != before, "report_alive re-arms report_by even ON a transition")

    same_status = _report("idle")
    _check(
        same_status["recovered"] is False,
        "reporting the SAME status again is a no-op transition (still re-arms, see below)",
    )
    after2 = read_managed_session(state, "agi-a")
    _check(
        after2["report_by"] != after["report_by"],
        "report_alive re-arms report_by even when NO transition occurs "
        "(the same-status branch)",
    )
    heartbeat = _report("heartbeat")
    after_heartbeat = read_managed_session(state, "agi-a")
    _check(
        heartbeat["lifecycle_state"] == LIFECYCLE_IDLE
        and after_heartbeat["lifecycle_state"] == LIFECYCLE_IDLE,
        "a passive heartbeat preserves an explicit idle status",
    )
    _check(
        after_heartbeat.get("last_heartbeat_at") is not None
        and after_heartbeat.get("report_by") == after2.get("report_by"),
        "a passive heartbeat records liveness without rearming report_by",
    )
    later = _report("working")
    _check(
        later["lifecycle_state"] == LIFECYCLE_LIVE
        and read_managed_session(state, "agi-a").get("status_source") == "explicit_self_report",
        "a later explicit working report supersedes idle with explicit provenance",
    )

    # overdue recovery
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state=LIFECYCLE_LIVE,
        to_state=LIFECYCLE_OVERDUE, directed_by="sweep:none",
    )
    recovered = _report("working")
    _check(recovered["recovered"] is True, "a late report from 'overdue' is flagged recovered=True")
    _check(recovered["lifecycle_state"] == LIFECYCLE_LIVE, "overdue -> live on a 'working' report")

    # parked/terminal conflict
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state=LIFECYCLE_LIVE,
        to_state="parked", directed_by="steward:none",
    )
    conflict = None
    try:
        _report("working")
    except VerbError as exc:
        conflict = exc.code
    _check(
        conflict == "lifecycle_state_conflict",
        "a report on a 'parked' row -> lifecycle_state_conflict",
    )

    unknown_status = None
    try:
        _report("bogus")
    except VerbError as exc:
        unknown_status = exc.code
    _check(unknown_status == "unknown_status", "report_alive(status='bogus') -> unknown_status")


def test_report_alive_parked_refusal_is_distinct_from_the_terminal_one() -> None:
    """LIF-04 — a PARKED row and a TERMINAL row both refuse ``report_alive``
    with the same code, and until now with the same SENTENCE ("state skew ...
    parked/terminal rows never self-report back to life"). They are not the
    same fact, and the shared sentence taught the wrong one.

    Measured 2026-08-19 on ``lane-seed-remint``: a park suppresses the
    HEARTBEAT CONTRACT ONLY. The pane, the stop-hook wake path and the
    messaging verbs stay live, so a parked session keeps waking, keeps reading
    its inbox and can still send — and a session in exactly that position is
    the caller this refusal answers. "State skew" invites it to conclude its
    own row is wrong; the truth is that its row is right, park is a steward's
    deliberate state, and only ``drive_session`` takes ``parked -> live``
    back. A terminal row is the opposite fact: it IS finished.

    The error TOKEN must not move (callers key on it) — so the discriminator
    this pins is that the two messages differ and that the parked one carries
    the measured scope. Collapsing them back into one sentence reds this.
    """
    state = _state()

    def _refusal(agent_instance_id: str) -> VerbError | None:
        try:
            report_alive(
                state, agent_instance_id=agent_instance_id, status="working",
                directed_by="operator:none",
            )
        except VerbError as exc:
            return exc
        return None

    for instance_id, terminal in (("agi-lif04-parked", False), ("agi-lif04-dead", True)):
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id=instance_id, lane_id="lane-lif04", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id=instance_id, from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        transition_lifecycle_state(
            state, agent_instance_id=instance_id, from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED if terminal else LIFECYCLE_PARKED,
            directed_by="steward:none",
        )

    parked = _refusal("agi-lif04-parked")
    dead = _refusal("agi-lif04-dead")
    _check(
        parked is not None and dead is not None,
        "both a parked row and a terminal row still refuse report_alive",
    )
    _check(
        parked is not None and dead is not None
        and parked.code == "lifecycle_state_conflict" == dead.code,
        "the error TOKEN is unchanged on both — LIF-04 moves the message, not the code",
    )
    parked_message = parked.message if parked is not None else ""
    dead_message = dead.message if dead is not None else ""
    _check(
        parked_message != dead_message,
        "the two refusals are not the same sentence",
    )
    lowered = parked_message.lower()
    _check(
        "state skew" not in lowered,
        "the parked refusal drops the 'state skew' framing — a parked lane's row is "
        "RIGHT, and telling it otherwise is what sends it retrying",
    )
    _check(
        "not evidence that you are dead" in lowered,
        "the parked refusal says outright that it is NOT a death notice",
    )
    _check(
        "report_by" in lowered and "heartbeat contract only" in lowered,
        "the parked refusal names the MEASURED scope: the heartbeat contract, and only that",
    )
    _check(
        "wake" in lowered and "inbox" in lowered,
        "...and that the pane/wake/inbox stay live through a park (LIF-04's whole point)",
    )
    _check(
        "drive_session" in lowered,
        "the parked refusal names who owns the way back: a steward's drive_session",
    )
    _check(
        "drive_session" not in dead_message.lower(),
        "the terminal refusal does NOT offer the parked row's way back — different fact",
    )


def _report_by_delta_seconds(row: dict[str, object]) -> float:
    report_by = datetime.fromisoformat(str(row["report_by"]))
    if report_by.tzinfo is None:
        report_by = report_by.replace(tzinfo=UTC)
    return (report_by - datetime.now(UTC)).total_seconds()


def test_rearm_report_by_honors_spawn_window() -> None:
    """D2-lane-tail fix: _rearm_report_by must re-arm from the SPAWN's own
    report_by_seconds, not the hardcoded DEFAULT_REPORT_BY_SECONDS (300s).
    RED-FIRST shape: a row spawned with report_by_seconds=900 that re-arms
    to ~300s (the pre-fix bug) fails the >800s assertion below -- this is
    the exact live-measured regression (a worker's deadline silently
    shortened on its first report/drive)."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-window", lane_id="lane-window", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
            report_by_seconds=900,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-window", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-window", status="idle", directed_by="operator:none")
    delta = _report_by_delta_seconds(read_managed_session(state, "agi-window"))
    _check(
        delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"RED-vs-GREEN: report_alive re-arms a 900s-window row to ~900s out "
        f"(got {delta:.0f}s), not silently shortened to the {DEFAULT_REPORT_BY_SECONDS}s "
        "default",
    )

    # Fallback case: a row that never requested a custom window (0/absent,
    # e.g. a legacy row) must still re-arm correctly via the default -- the
    # fix must not break the no-custom-window path.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-nowindow", lane_id="lane-nowindow", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-nowindow", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-nowindow", status="idle", directed_by="operator:none")
    fallback_delta = _report_by_delta_seconds(read_managed_session(state, "agi-nowindow"))
    _check(
        DEFAULT_REPORT_BY_SECONDS - 30 < fallback_delta <= DEFAULT_REPORT_BY_SECONDS + 5,
        f"GREEN: a row with no custom window still falls back to the "
        f"{DEFAULT_REPORT_BY_SECONDS}s default (got {fallback_delta:.0f}s)",
    )


def test_insert_managed_session_arms_report_by_for_non_operator_hosts() -> None:
    """A4 Slice 0 (measured gap, not assumed): a caller-omitted
    report_by_seconds (0) used to leave the row with NO report_by at all
    until the first report_alive/drive_session call -- invisible to
    sweep_overdue_sessions the whole spawn-to-first-report window, for any
    non-operator host. RED-FIRST shape: reverting the insert-time default
    makes the headless/tmux assertions below fail (report_by stays None)
    while the operator assertion keeps passing -- the fix must touch
    exactly the non-operator path, never the operator carve-out."""
    state = _state()

    # Operator host, no report_by_seconds: unchanged -- no contract by
    # design (sweep_overdue_sessions' own documented carve-out).
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-op-nowindow", lane_id="lane-op", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
        ),
    )
    op_row = read_managed_session(state, "agi-op-nowindow")
    _check(
        op_row.get("report_by") is None,
        "operator host + report_by_seconds=0 -> report_by stays None (no contract by design)",
    )

    # Headless host, no report_by_seconds: must be armed immediately at
    # insert, not left null until a later report/drive call.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-headless-nowindow", lane_id="lane-headless", brief_ref="",
            work_class="read_only", budget_line="b1", host="headless",
        ),
    )
    headless_row = read_managed_session(state, "agi-headless-nowindow")
    _check(
        headless_row.get("report_by") is not None,
        "headless host + report_by_seconds=0 -> report_by armed at insert, not left null",
    )
    headless_delta = _report_by_delta_seconds(headless_row)
    _check(
        DEFAULT_REPORT_BY_SECONDS - 30 < headless_delta <= DEFAULT_REPORT_BY_SECONDS + 5,
        f"headless default-armed report_by lands ~{DEFAULT_REPORT_BY_SECONDS}s out "
        f"(got {headless_delta:.0f}s)",
    )
    _check(
        headless_row.get("report_by_seconds") == DEFAULT_REPORT_BY_SECONDS,
        "the resolved default is written back into report_by_seconds -- the row "
        "stays self-describing for the next _rearm_report_by call",
    )

    # Tmux host, no report_by_seconds: same coverage, different host string.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-tmux-nowindow", lane_id="lane-tmux", brief_ref="",
            work_class="read_only", budget_line="b1", host="tmux",
        ),
    )
    tmux_row = read_managed_session(state, "agi-tmux-nowindow")
    _check(
        tmux_row.get("report_by") is not None,
        "tmux host + report_by_seconds=0 -> report_by armed at insert, not left null",
    )

    # A caller-supplied window is never overridden by the default.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-headless-window", lane_id="lane-headless-w", brief_ref="",
            work_class="read_only", budget_line="b1", host="headless",
            report_by_seconds=900,
        ),
    )
    explicit_row = read_managed_session(state, "agi-headless-window")
    explicit_delta = _report_by_delta_seconds(explicit_row)
    _check(
        explicit_delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"an explicit report_by_seconds=900 is honored, not overridden by the "
        f"{DEFAULT_REPORT_BY_SECONDS}s default (got {explicit_delta:.0f}s)",
    )
    _check(
        explicit_row.get("report_by_seconds") == 900,
        "an explicit report_by_seconds survives untouched in the row",
    )


def test_drive_session_rearm_honors_spawn_window() -> None:
    """Same fix, the drive_session call site."""
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch
    try:
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-window", lane_id="lane-drive-window", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
                report_by_seconds=900,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-window", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        drive_session(
            state, agent_instance_id="agi-drive-window", text="hello",
            directed_by="steward:none",
        )
        delta = _report_by_delta_seconds(read_managed_session(state, "agi-drive-window"))
        _check(
            delta > DEFAULT_REPORT_BY_SECONDS + 100,
            f"drive_session re-arms a 900s-window row to ~900s out (got "
            f"{delta:.0f}s), not the {DEFAULT_REPORT_BY_SECONDS}s default",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001


def test_provision_surfaces_typed_dirty_stale_worktree_warning() -> None:
    """SPN-04: one retained dirty tree cannot hide or block another lane spawn."""
    repo_root = Path("/fixture-repo")
    stale_path = Path("/fixture-lanes/stale--agi-stale")
    requested = lifecycle_verbs.LaneWorktree(
        repo_root=repo_root,
        root=Path("/fixture-lanes"),
        path=Path("/fixture-lanes/fresh--agi-fresh"),
        branch="lane/fresh/agi-fresh",
    )
    warning = lane_worktrees.DirtyStaleWorktreeSkippedWarning(
        path=stale_path,
        git_diagnostic="git worktree remove refused dirty tracked.txt",
    )
    original_root = lifecycle_verbs._resolve_lane_repo_root  # noqa: SLF001
    original_inventory = lifecycle_verbs._active_lane_worktree_inventory  # noqa: SLF001
    original_sweep = lifecycle_verbs.sweep_orphaned_lane_worktrees  # noqa: SLF001
    original_worktree_for = lifecycle_verbs.lane_worktree_for  # noqa: SLF001
    original_provision = lifecycle_verbs.provision_lane_worktree  # noqa: SLF001
    emitted: list[str] = []

    class _WarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            emitted.append(record.getMessage())

    handler = _WarningCapture(level=logging.WARNING)
    lifecycle_verbs.logger.addHandler(handler)  # noqa: SLF001
    try:
        lifecycle_verbs._resolve_lane_repo_root = lambda _root="": repo_root  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._active_lane_worktree_inventory = (  # type: ignore[assignment]  # noqa: SLF001
            lambda _state, _repo: lane_worktrees.ActiveLaneWorktreeInventory(
                paths=(), terminal_paths=(), cleanup_safe=True, incomplete_rows=(),
            )
        )
        lifecycle_verbs.sweep_orphaned_lane_worktrees = (  # type: ignore[assignment]  # noqa: SLF001
            lambda *_args, **_kwargs: lane_worktrees.LaneWorktreeSweep(
                removed=(), skipped=(warning,),
            )
        )
        lifecycle_verbs.lane_worktree_for = (  # type: ignore[assignment]  # noqa: SLF001
            lambda *_args, **_kwargs: requested
        )
        lifecycle_verbs.provision_lane_worktree = (  # type: ignore[assignment]  # noqa: SLF001
            lambda worktree: _check(
                worktree == requested,
                "a dirty stale tree does not block the requested lane provision",
            )
        )
        result = lifecycle_verbs._provision_spawn_worktree(  # noqa: SLF001
            _state(), role_name="fresh", agent_instance_id="agi-fresh",
        )
    finally:
        lifecycle_verbs.logger.removeHandler(handler)  # noqa: SLF001
        lifecycle_verbs._resolve_lane_repo_root = original_root  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._active_lane_worktree_inventory = original_inventory  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.sweep_orphaned_lane_worktrees = original_sweep  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.lane_worktree_for = original_worktree_for  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.provision_lane_worktree = original_provision  # type: ignore[assignment]  # noqa: SLF001
    _check(result == requested, "the requested lane is returned after the stale-tree warning")
    _check(
        emitted == [
            "dirty_stale_worktree_skipped: path=/fixture-lanes/stale--agi-stale "
            "git_diagnostic=git worktree remove refused dirty tracked.txt"
        ],
        "typed warning carries the exact stale path and Git diagnostic",
    )


def test_active_worktree_inventory_preserves_drift_and_refuses_unknown_owner() -> None:
    """Durable active rows protect paths; terminal rows alone authorize removal."""
    with tempfile.TemporaryDirectory() as raw:
        repo_root = Path(raw).resolve()

        def row(**overrides: object) -> dict[str, object]:
            record: dict[str, object] = {
                "agent_instance_id": "agi-inventory",
                "role_name": "inventory-role",
                "local_name": "",
                "lifecycle_state": LIFECYCLE_LIVE,
                "provisioning_mode": "worktree",
                "lane_repo_root": str(repo_root),
            }
            record.update(overrides)
            return record

        def inventory(rows: list[dict[str, object]]) -> lane_worktrees.ActiveLaneWorktreeInventory:
            state = _state()
            for record in rows:
                state.write_state(
                    AGENT_ROLE_BINDING_NAMESPACE,
                    {"table": TABLE_MANAGED_SESSION, "record": record},
                )
            return lifecycle_verbs._active_lane_worktree_inventory(state, repo_root)  # noqa: SLF001

        drift = inventory([
            row(agent_instance_id="agi-drift", role_name="new-role", local_name="old-local"),
        ])
        drift_paths = {
            lane_worktrees.lane_worktree_for(
                repo_root, role_name=name, agent_instance_id="agi-drift",
            ).path
            for name in ("new-role", "old-local")
        }
        _check(
            drift.cleanup_safe and drift_paths.issubset(drift.paths),
            "active role/local-name drift preserves both possible lane paths",
        )
        protected = inventory([
            row(agent_instance_id="", role_name="", local_name=""),
            row(agent_instance_id="agi-bad-root", lane_repo_root=""),
            row(agent_instance_id="agi-bad-mode", provisioning_mode="unknown"),
        ])
        _check(
            not protected.cleanup_safe and protected.incomplete_rows,
            "incomplete active ownership disables automatic cleanup",
        )
        terminal = inventory([
            row(
                agent_instance_id="agi-terminal",
                role_name="terminal-role",
                lifecycle_state=LIFECYCLE_TERMINATED,
            ),
        ])
        terminal_path = lane_worktrees.lane_worktree_for(
            repo_root, role_name="terminal-role", agent_instance_id="agi-terminal",
        ).path
        _check(
            terminal.cleanup_safe and terminal.terminal_paths == (terminal_path,),
            "only a complete terminal worktree row supplies cleanup eligibility",
        )
        supported_modes = inventory([
            row(
                agent_instance_id="agi-terminal-supported-mode",
                role_name="terminal-supported-mode",
                lifecycle_state=LIFECYCLE_TERMINATED,
            ),
            row(
                agent_instance_id="agi-operator-existing",
                provisioning_mode="operator_existing_checkout",
                role_name="",
            ),
            row(
                agent_instance_id="agi-synthetic",
                provisioning_mode="synthetic_no_worktree",
                role_name="",
            ),
        ])
        supported_terminal = lane_worktrees.lane_worktree_for(
            repo_root,
            role_name="terminal-supported-mode",
            agent_instance_id="agi-terminal-supported-mode",
        ).path
        _check(
            supported_modes.cleanup_safe
            and supported_modes.terminal_paths == (supported_terminal,),
            "supported non-worktree rows preserve unrelated terminal cleanup eligibility",
        )
        overlap = inventory([
            row(agent_instance_id="agi-shared", role_name="shared-role"),
            row(
                agent_instance_id="agi-shared",
                role_name="shared-role",
                lifecycle_state=LIFECYCLE_TERMINATED,
            ),
            row(
                agent_instance_id="agi-invalid-terminal",
                lifecycle_state=LIFECYCLE_TERMINATED,
                local_name="../invalid",
            ),
        ])
        _check(
            overlap.cleanup_safe
            and len(overlap.paths) == 1
            and not overlap.terminal_paths,
            "active ownership overrides and malformed terminal identity cannot authorize removal",
        )
        paged = inventory([
            row(
                agent_instance_id=f"agi-page-{index}",
                role_name=f"page-{index}",
                lifecycle_state=LIFECYCLE_PARKED,
            )
            for index in range(101)
        ])
        final_path = lane_worktrees.lane_worktree_for(
            repo_root, role_name="page-100", agent_instance_id="agi-page-100",
        ).path
        _check(
            paged.cleanup_safe and final_path in paged.paths,
            "a protected owner beyond the first page remains protected",
        )


def main() -> int:
    test_active_worktree_inventory_preserves_drift_and_refuses_unknown_owner()
    test_provision_surfaces_typed_dirty_stale_worktree_warning()
    fixture_temp = tempfile.TemporaryDirectory()
    fixture_root = Path(fixture_temp.name)
    original_provision = lifecycle_verbs._provision_spawn_worktree  # noqa: SLF001
    original_adjudicate = lifecycle_verbs._adjudicate_retire_lane_worktree  # noqa: SLF001
    original_retire = lifecycle_verbs._retire_lane_worktree  # noqa: SLF001
    original_remove = lifecycle_verbs.remove_lane_worktree  # noqa: SLF001
    provisioning_calls: list[tuple[str, str, lifecycle_verbs.LaneWorktree]] = []
    retirement_calls: list[dict[str, object]] = []
    cleanup_calls: list[lifecycle_verbs.LaneWorktree] = []

    def fixture_provision(
        state: StateManagementInterface, *, role_name: str, agent_instance_id: str, repository_root: str,
    ) -> lifecycle_verbs.LaneWorktree:
        del state, repository_root
        worktree = lifecycle_verbs.LaneWorktree(
            repo_root=fixture_root, root=fixture_root, path=fixture_root,
            branch=f"fixture/{role_name}/{agent_instance_id}",
        )
        provisioning_calls.append((role_name, agent_instance_id, worktree))
        return worktree

    def fixture_retire(row: dict[str, object]) -> str:
        retirement_calls.append(row)
        return "removed"

    def fixture_adjudicate(row: dict[str, object]) -> None:
        del row

    def fixture_cleanup(worktree: lifecycle_verbs.LaneWorktree) -> None:
        cleanup_calls.append(worktree)

    lifecycle_verbs._provision_spawn_worktree = fixture_provision  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs._adjudicate_retire_lane_worktree = fixture_adjudicate  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs._retire_lane_worktree = fixture_retire  # type: ignore[assignment]  # noqa: SLF001
    lifecycle_verbs.remove_lane_worktree = fixture_cleanup  # type: ignore[assignment]  # noqa: SLF001
    try:
        test_lane_worktree_root_requires_app_home()
        test_spawn_errors()
        test_omitted_host_resolves_to_tmux_before_spawn()
        test_visibility_host_consistency()
        test_model_dispatch_policy_refusals_and_allowances()
        test_spawn_role_class_conflict()
        test_raw_project_spawn_requires_prepared_dispatch()
        test_local_name_defaults_by_role_class()
        test_spawn_refuses_second_session_under_a_live_local_name()
        test_spawn_allows_replacement_after_incumbent_terminated()
        test_spawn_does_not_claim_the_role_binding()
        test_spawn_lane_named_workers_do_not_collide_on_role()
        test_list_and_status()
        test_clear_session_sends_and_can_park()
        test_compact_session_sends_no_park()
        test_drive_session_dispatches_and_unparks()
        test_drive_session_errors()
        test_clear_and_compact_errors()
        test_terminate_idempotent()
        test_terminate_session_kills_the_real_headless_process()
        test_retire_idempotent_and_redrivable()
        test_report_alive()
        test_report_alive_parked_refusal_is_distinct_from_the_terminal_one()
        test_rearm_report_by_honors_spawn_window()
        test_insert_managed_session_arms_report_by_for_non_operator_hosts()
        test_drive_session_rearm_honors_spawn_window()
        test_terminate_fires_and_delivers_session_terminal_edge()
        test_retire_composes_terminate_no_double_delivery()
        test_terminate_delivery_fault_is_contained()
        test_already_terminal_catches_orphaned_edge()
        test_capture_lane_charter_validation_errors()
        test_capture_lane_charter_is_insert_only_and_supersedes_by_recency()
        test_resolve_lane_charter_empty_for_unknown_lane()
        test_spawn_session_drives_charter_as_first_turn_byte_exact()
        test_charter_frame_ships_instruments_and_the_claim_first_instruction()
        test_ephemeral_charter_frame_never_requests_a_role()
        test_spawn_session_drives_fallback_when_no_charter_on_file()
        test_spawn_session_first_turn_preserves_agent_runtime()
        test_fallback_first_turn_hands_off_to_the_spawner()
        test_ephemeral_fallback_preserves_an_absolute_brief_ref()
        test_spawn_session_first_turn_failure_is_visible_not_blocking()
        test_spawn_session_first_turn_send_raising_is_contained()
        _check(
            bool(provisioning_calls)
            and all(
                worktree.root == fixture_root
                and worktree.repo_root == fixture_root
                and worktree.path == fixture_root
                and worktree.branch == f"fixture/{role_name}/{agent_instance_id}"
                for role_name, agent_instance_id, worktree in provisioning_calls
            ),
            "fixture provisioning records every spawn's expected lane branch and root",
        )
        _check(
            any(str(row.get("lifecycle_state") or "") == LIFECYCLE_TERMINATED for row in retirement_calls),
            "fixture teardown records retirement after the terminal transition",
        )
        _check(
            bool(cleanup_calls),
            "fixture cleanup records host-failed spawn teardown rather than silently swallowing it",
        )
    finally:
        lifecycle_verbs._provision_spawn_worktree = original_provision  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._adjudicate_retire_lane_worktree = original_adjudicate  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs._retire_lane_worktree = original_retire  # type: ignore[assignment]  # noqa: SLF001
        lifecycle_verbs.remove_lane_worktree = original_remove  # type: ignore[assignment]  # noqa: SLF001
        fixture_temp.cleanup()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
