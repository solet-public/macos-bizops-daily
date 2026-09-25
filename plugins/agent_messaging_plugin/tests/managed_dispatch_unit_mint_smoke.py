#!/usr/bin/env python3
"""Red-first smoke for the atomic register Unit mint on managed dispatch.

Acceptance T1-T14 of design ``unt_57725090`` section 6 (fix unit
``unt_a9f1776c``), plus T15: the declared brief repository root
(``iss_cc71df1b``, folded into round 3).  Every register call goes through the real
``PsoletRegisterUnitClient`` to a recording fake ``psolet`` script, so the
smoke asserts the exact argv the platform issues -- not only that some call
happened.  The fake keeps a JSON Unit store so uniqueness, reconciliation
after a commit-then-timeout, retirement and link events are observable, and
it refuses any flag the real project-solet parser does not declare (flag sets
copied from project-solet ``cli_shortcuts.py``/``cli_unit.py`` at master
``1509c24``).

Set ``MANAGED_DISPATCH_UNIT_MINT_ARGV_EXPORT`` to a file path to append every
recorded argv there, for replay through project-solet's real parser.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/managed_dispatch_unit_mint_smoke.py
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import sys
import tempfile
import traceback
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, replace
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    pass

from _recorded_lane_worktree_fixture import RecordedLaneWorktreeFixture  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import (  # noqa: E402
    InvocationSchemaGenerator,
)
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from managed_dispatch_smoke import (  # noqa: E402
    T0,
    _coordinator_actor,
    _ReplacementDriver,
    _spawn_request,
    _spec,
    _state,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    DISPATCH_FAILED_START,
    DispatchActor,
    DispatchError,
    DispatchSpec,
    dispatch_managed_work,
    prepare_managed_dispatch,
    read_managed_dispatch,
    resolve_managed_dispatch,
)
from agent_messaging_plugin.managed_dispatch import (  # noqa: E402
    _ensure_register_unit as ensure_unit,  # pyright: ignore[reportPrivateUsage]
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.register_unit_client import RegisterActor  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_MANAGED_DISPATCH,
    TABLE_MANAGED_DISPATCH_EVENT,
    TABLE_MANAGED_SESSION,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    spawn_session,
)

HOST = "unit-mint-fixture-host"
REPO_ID = "rep_fixture-solet"
LANE = "coordination-fixture"
DISPATCH_ID = "mdp-fixture"
DEFAULT_KEY = f"{LANE}-{DISPATCH_ID}"
SOURCE_REF = f"managed-dispatch:{DISPATCH_ID}"
_ARGV_EXPORT = os.environ.get("MANAGED_DISPATCH_UNIT_MINT_ARGV_EXPORT", "")

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _check_all(conditions: tuple[object, ...], label: str) -> None:
    _check(all(conditions), label)


def _code(result: object) -> str:
    return result.code if isinstance(result, DispatchError) else ""


def _message(result: object) -> str:
    return result.message if isinstance(result, DispatchError) else ""


def _data(result: object) -> dict[str, Any]:
    return result.data if isinstance(result, DispatchError) else {}


_FAKE_PSOLET = r'''#!/usr/bin/env python3
import hashlib, json, sys, time
from datetime import date
from pathlib import Path
D = Path(__DIR__)
args = sys.argv[1:]
with open(D / "argv.jsonl", "a", encoding="utf-8") as handle:
    handle.write(json.dumps(args) + "\n")
config = json.loads((D / "config.json").read_text())
store = json.loads((D / "store.json").read_text())
IDENTITY = {"--solet", "--agent-instance-id", "--agent-session-id", "--session-name", "--role-name",
            "--model", "--lane-id", "--agent-id", "--actor-model", "--actor-effort", "--work-class", "--dsn-env"}

def usage(message):
    sys.stderr.write("usage error: " + message + "\n"); sys.exit(2)

def refuse(message):
    sys.stderr.write("project-solet: " + message + "\n"); sys.exit(1)

def save():
    (D / "store.json").write_text(json.dumps(store))

def behaviour(name):
    queue = config.get(name, [])
    if not queue:
        return "ok"
    chosen = queue.pop(0)
    (D / "config.json").write_text(json.dumps(config))
    return chosen

def parse(tokens, positionals, allowed, required=(), repeatable=()):
    found, options, index = [], {}, 0
    while index < len(tokens):
        token = tokens[index]
        if token.startswith("--"):
            if token not in allowed:
                usage("unrecognized arguments: " + token)
            if index + 1 >= len(tokens):
                usage("expected one argument: " + token)
            options.setdefault(token, []).append(tokens[index + 1]); index += 2
        else:
            found.append(token); index += 1
    if len(found) != positionals:
        usage("positional arguments: " + " ".join(found))
    for flag in required:
        if flag not in options:
            usage("the following arguments are required: " + flag)
    return found, {flag: (values if flag in repeatable else values[-1]) for flag, values in options.items()}

def show(unit):
    return {"unit": [{key: unit[key] for key in ("id", "unit_key", "kind", "repository_id",
                                                 "dispatch_source_ref", "is_deleted", "model", "effort")}],
            "unit_state_event": [{"to_state": unit["state"], "observed_at": "2026-09-25T00:00:00",
                                  "created_at": "2026-09-25T00:00:00", "is_deleted": False}]}

def dispatched(rest):
    allowed = IDENTITY | {"--brief", "--expected-sha256", "--kind", "--reference-basis", "--reference-basis-reason",
                          "--effort", "--scope", "--addresses", "--unit-key", "--dispatch-source-ref", "--repo",
                          "--brief-repo"}
    (lane,), options = parse(rest, 1, allowed, ("--brief", "--repo"), ("--addresses", "--scope"))
    chosen = behaviour("dispatched")
    if chosen == "timeout_before_commit":
        time.sleep(30)
    if not options.get("--model", "").strip() or not options.get("--effort", "").strip():
        refuse("model and effort are required and must not be blank")
    kind = options.get("--kind", "fix")
    if not kind.strip():
        refuse("kind is required")
    basis = options.get("--reference-basis")
    if kind == "fix" and basis not in ("existing_pattern", "no_existing_pattern"):
        refuse("fix units require reference_basis (existing_pattern or no_existing_pattern)")
    if kind != "fix" and basis is not None:
        refuse("reference_basis applies only to fix units")
    brief = Path(options["--brief"])
    try:
        brief.resolve().relative_to(Path(options.get("--brief-repo", options["--repo"])).resolve())
    except ValueError:
        refuse("brief path is outside the brief repository")
    if not brief.is_file():
        refuse("source brief exact bytes are not registered")
    digest = hashlib.sha256(brief.read_bytes()).hexdigest()
    if options.get("--expected-sha256") not in (None, digest):
        refuse("document SHA-256 changed or does not match --expected-sha256")
    if chosen == "refuse":
        refuse("source brief exact bytes are not registered")
    key = options.get("--unit-key") or f"{lane}-{date.today().isoformat()}"
    if any(unit["unit_key"] == key for unit in store["units"].values()):
        refuse('duplicate key value violates unique constraint "unit_unit_key_key"')
    unit_id = "unt_fake-" + hashlib.sha256(key.encode()).hexdigest()[:12]
    store["units"][unit_id] = {
        "id": unit_id, "unit_key": key, "kind": kind, "repository_id": config["repository_id"],
        "dispatch_source_ref": options.get("--dispatch-source-ref") or str(brief), "is_deleted": False,
        "state": "dispatched", "model": options["--model"], "effort": options["--effort"],
        "addresses": options.get("--addresses", []), "events": [], "retired": None,
    }
    save()
    if chosen == "timeout_after_commit":
        time.sleep(30)
    print(json.dumps({"unit_id": unit_id, "brief_revision_id": "drv_fake", "brief_sha256": digest,
                      "brief_target_repository_id": config.get("brief_target_repository_id", config["repository_id"]),
                      "conflict_count": 0, "source_brief_revision_id": "drv_fake_source"}))

def unit_command(verb, rest):
    if verb == "list":
        _, options = parse(rest, 0, {"--state", "--solet", "--actor-id", "--dsn-env"})
        if behaviour("list") == "fail":
            refuse("database unavailable")
        # Exact row shape of project-solet unit_repository.list_units.
        rows = [{"unit_id": u["id"], "unit_key": u["unit_key"], "kind": u["kind"],
                 "dispatch_source_ref": u["dispatch_source_ref"], "state": u["state"],
                 "state_observed_at": "2026-09-25T00:00:00-07:00"}
                for u in store["units"].values()
                if "--state" not in options or u["state"] == options["--state"]]
        print(json.dumps(rows)); return
    if verb == "show":
        (unit_id,), _ = parse(rest, 1, {"--dsn-env"})
        if unit_id not in store["units"]:
            refuse("unit not found: " + unit_id)
        print(json.dumps(show(store["units"][unit_id]))); return
    if verb == "record-event":
        (unit_id,), options = parse(rest, 1, IDENTITY | {"--event-kind", "--detail", "--target-event", "--observed-at"},
                                    ("--event-kind", "--detail"))
        if options["--event-kind"] not in ("correction", "observation", "withdrawal"):
            usage("invalid choice: " + options["--event-kind"])
        if behaviour("record_event") == "fail" or unit_id not in store["units"]:
            refuse("record-event failed")
        store["units"][unit_id]["events"].append({"kind": options["--event-kind"], "detail": json.loads(options["--detail"])})
        save(); print(json.dumps({"event_id": "uev_fake"})); return
    if verb == "retire":
        (unit_id,), options = parse(rest, 1, IDENTITY | {"--state", "--reason", "--survivor-unit-id"},
                                    ("--state", "--reason", "--solet", "--agent-instance-id"))
        if options["--state"] not in ("abandoned", "cancelled", "completed", "superseded"):
            usage("invalid choice: " + options["--state"])
        if behaviour("retire") == "fail" or unit_id not in store["units"]:
            refuse("retire failed")
        store["units"][unit_id]["state"] = options["--state"]
        store["units"][unit_id]["retired"] = options["--reason"]
        save(); print(json.dumps({"state_event_id": "use_fake"})); return
    usage("unknown unit command " + verb)

if args[:3] == ["db", "repository", "resolve"]:
    parse(args[3:], 1, {"--dsn-env"})
    # Only a registered checkout resolves; a plain directory never does.
    if behaviour("resolve") == "refuse" or not (Path(args[3]) / ".git").exists():
        refuse("zero or ambiguous repositories match " + args[3])
    print(json.dumps({"repository_id": config["repository_id"], "repository_name": "fixture-solet"}))
elif args[:1] == ["dispatched"]:
    dispatched(args[1:])
elif args[:2] == ["db", "unit"] and len(args) > 2:
    unit_command(args[2], args[3:])
else:
    usage("unknown command " + " ".join(args[:3]))
'''


class _FakePsolet:
    """A recording ``psolet`` on disk with a JSON Unit store."""

    def __init__(self, directory: Path) -> None:
        directory.mkdir(parents=True)
        self.directory = directory
        self.script = directory / "psolet"
        self.script.write_text(_FAKE_PSOLET.replace("__DIR__", repr(str(directory))), encoding="utf-8")
        self.script.chmod(self.script.stat().st_mode | stat.S_IXUSR)
        (directory / "argv.jsonl").write_text("", encoding="utf-8")
        self.configure()
        self._write("store.json", {"units": {}})

    def _write(self, name: str, payload: object) -> None:
        (self.directory / name).write_text(json.dumps(payload), encoding="utf-8")

    def configure(self, **behaviours: object) -> None:
        self._write("config.json", {"repository_id": REPO_ID, **behaviours})

    def seed_unit(self, unit_id: str, **fields: object) -> None:
        store = self.store()
        store["units"][unit_id] = {
            "id": unit_id, "unit_key": f"seed-{unit_id}", "kind": "fix", "repository_id": REPO_ID,
            "dispatch_source_ref": "grooming lane", "is_deleted": False, "state": "dispatched",
            "model": "gpt-6-astra", "effort": "xhigh", "addresses": [], "events": [], "retired": None,
            **fields,
        }
        self._write("store.json", store)

    def store(self) -> dict[str, Any]:
        return cast(dict[str, Any], json.loads((self.directory / "store.json").read_text(encoding="utf-8")))

    def units(self) -> list[dict[str, Any]]:
        return list(cast(dict[str, dict[str, Any]], self.store()["units"]).values())

    def argvs(self) -> list[list[str]]:
        lines = (self.directory / "argv.jsonl").read_text(encoding="utf-8").splitlines()
        return [cast(list[str], json.loads(line)) for line in lines if line]

    def commands(self) -> list[str]:
        return [" ".join(argv[:3]) if argv[0] == "db" else argv[0] for argv in self.argvs()]

    def client(self, *, timeout_seconds: float = 20.0) -> Any:
        from agent_messaging_plugin.register_unit_client import (  # noqa: PLC0415
            PsoletRegisterUnitClient,
        )

        return PsoletRegisterUnitClient(str(self.script), solet_name="fixture-solet", timeout_seconds=timeout_seconds)

    def export(self) -> None:
        if _ARGV_EXPORT:
            with open(_ARGV_EXPORT, "a", encoding="utf-8") as handle:
                for argv in self.argvs():
                    handle.write(json.dumps(argv) + "\n")


def _flag(argv: list[str], flag: str) -> str:
    return argv[argv.index(flag) + 1] if flag in argv else ""


def _link_events(fake: _FakePsolet) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], json.loads(_flag(argv, "--detail")))
        for argv in fake.argvs()
        if argv[:3] == ["db", "unit", "record-event"] and _flag(argv, "--event-kind") == "observation"
    ]


class _OrderedDriver(_ReplacementDriver):
    """Records how many register calls had happened when the host spawned."""

    def __init__(self, fake: _FakePsolet, *, fail_start: bool = False) -> None:
        super().__init__(fail_start=fail_start)
        self.fake = fake
        self.register_calls_at_spawn: list[int] = []

    def spawn(self, spec: dict[str, object]) -> str:
        self.register_calls_at_spawn.append(len(self.fake.argvs()))
        return super().spawn(spec)


class _Harness:
    """One dispatch world: state, lane root, fake register, recorded host."""

    def __init__(self, tmp: Path, fixture: RecordedLaneWorktreeFixture, *, fail_start: bool = False) -> None:
        self.tmp = tmp
        self.fixture = fixture
        self.root = tmp / "repo"
        (self.root / ".git").mkdir(parents=True)
        (self.root / "workbench").mkdir()
        self.fake = _FakePsolet(tmp / "psolet")
        self.state = _state()
        self.driver = _OrderedDriver(self.fake, fail_start=fail_start)
        self.provisions_before = len(fixture.provisioning_calls)

    def spec(self, **overrides: Any) -> DispatchSpec:
        brief = self.root / "workbench" / "brief.md"
        brief.write_text("exact immutable brief\n", encoding="utf-8")
        values: dict[str, Any] = {
            "brief_ref": str(brief),
            "brief_sha256": hashlib.sha256(brief.read_bytes()).hexdigest(),
            "unit_id": "",
            "repository_root": str(self.root),
            "host": HOST,
            "allowed_hosts": [HOST],
            "local_name": "Unit-Mint-Fixture",
            "dispatch_kind": "fix",
            "reference_basis": "existing_pattern",
        }
        values.update(overrides)
        return _spec(self.tmp, **values)

    def dispatch(self, spec: DispatchSpec, register: Any = None) -> dict[str, Any] | DispatchError:
        try:
            return dispatch_managed_work(
                self.state,
                spec,
                _spawn_request(spec),
                register=register or self.fake.client(),
                now=T0,
            )
        except DispatchError as exc:
            return exc

    def retry(self, dispatch_id: str = DISPATCH_ID, register: Any = None) -> dict[str, Any]:
        row = read_managed_dispatch(self.state, dispatch_id)
        return resolve_managed_dispatch(
            self.state,
            dispatch_id=dispatch_id,
            event_id=f"evt-retry-{row['version']}",
            action="request_retry",
            actor=_coordinator_actor(),
            prior_version=int(row["version"]),
            payload={"reason": "retry after register repair"},
            observed_at=T0 + timedelta(seconds=5),
            register=register or self.fake.client(),
        )

    def cancel(self, dispatch_id: str = DISPATCH_ID) -> dict[str, Any]:
        row = read_managed_dispatch(self.state, dispatch_id)
        return resolve_managed_dispatch(
            self.state,
            dispatch_id=dispatch_id,
            event_id=f"evt-cancel-{row['version']}",
            action="cancel",
            actor=_coordinator_actor(),
            prior_version=int(row["version"]),
            payload={"reason": "operator withdrew the work"},
            observed_at=T0 + timedelta(seconds=6),
            register=self.fake.client(),
        )

    def row(self, dispatch_id: str = DISPATCH_ID) -> dict[str, Any]:
        return read_managed_dispatch(self.state, dispatch_id)

    def _records(self, table: str) -> list[dict[str, Any]]:
        result = self.state.query_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": table, "filters": {"is_deleted": 0}})
        return cast(list[dict[str, Any]], result["data"]["records"])

    def sessions(self) -> list[dict[str, Any]]:
        return self._records(TABLE_MANAGED_SESSION)

    def dispatch_rows(self) -> list[dict[str, Any]]:
        return self._records(TABLE_MANAGED_DISPATCH)

    def events(self, kind: str) -> list[dict[str, Any]]:
        return [event for event in self._records(TABLE_MANAGED_DISPATCH_EVENT) if event.get("event_kind") == kind]

    def no_side_effects(self) -> bool:
        return (
            not self.sessions()
            and not self.driver.spawned_instances
            and not self.driver.register_calls_at_spawn
            and len(self.fixture.provisioning_calls) == self.provisions_before
        )


@contextmanager
def _harness(fixture: RecordedLaneWorktreeFixture, *, fail_start: bool = False) -> Generator[_Harness]:
    key = (session_hosts.AGENT_RUNTIME_CODEX, HOST)
    prior = session_hosts._REGISTRY.get(key)  # noqa: SLF001
    with tempfile.TemporaryDirectory() as raw:
        world = _Harness(Path(raw), fixture, fail_start=fail_start)
        session_hosts._REGISTRY[key] = world.driver  # noqa: SLF001
        try:
            yield world
        finally:
            world.fake.export()
            if prior is None:
                session_hosts._REGISTRY.pop(key, None)  # noqa: SLF001
            else:
                session_hosts._REGISTRY[key] = prior  # noqa: SLF001


@contextmanager
def _environment(**values: str | None) -> Generator[None]:
    prior = {name: os.environ.get(name) for name in values}
    try:
        for name, value in values.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in prior.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def test_mint_runs_before_spawn_and_links_both_rows(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        spec = world.spec(addresses=("iss_fixture-1",))
        result = world.dispatch(spec)
        _check(isinstance(result, dict), "T1 dispatch with an empty unit_id succeeds")
        argvs = world.fake.argvs()
        root = str(world.root.resolve())
        _check(argvs[0] == ["db", "repository", "resolve", root], "T1 repository resolved from the lane root")
        mint = argvs[1] if len(argvs) > 1 else []
        _check(mint[:2] == ["dispatched", LANE], "T1 second register call is the mint")
        _check_all(
            (
                _flag(mint, "--unit-key") == DEFAULT_KEY,
                _flag(mint, "--dispatch-source-ref") == SOURCE_REF,
                _flag(mint, "--repo") == root,
                _flag(mint, "--brief") == spec.brief_ref,
                _flag(mint, "--expected-sha256") == spec.brief_sha256,
                _flag(mint, "--kind") == "fix",
                _flag(mint, "--model") == spec.model,
                _flag(mint, "--effort") == spec.effort,
                _flag(mint, "--addresses") == "iss_fixture-1",
                _flag(mint, "--reference-basis") == "existing_pattern",
            ),
            "T1 mint argv pins key, source ref, repo, brief digest, tier, addresses and basis",
        )
        _check_all(
            (
                _flag(mint, "--agent-instance-id") == spec.spawned_by_instance_id,
                _flag(mint, "--role-name") == spec.spawned_by_role,
                _flag(mint, "--agent-id") == "fixture-solet_platform",
                "--agent-session-id" not in mint,
            ),
            "T1 mint is attributed to the dispatching coordinator",
        )
        _check(world.driver.register_calls_at_spawn == [2], "T1 the mint completes before the host spawns")
        row = world.row()
        units = world.fake.units()
        sessions = world.sessions()
        _check_all(
            (
                len(units) == 1,
                len(sessions) == 1,
                row["unit_id"] == units[0]["id"] == sessions[0]["unit_id"],
            ),
            "T1 managed_dispatch.unit_id == managed_session.unit_id == receipt unit_id",
        )
        _check_all(
            (
                row["unit_mint_state"] == "minted",
                row["unit_mint_key"] == DEFAULT_KEY,
                row["unit_repository_id"] == REPO_ID,
                cast(dict[str, Any], row["unit_mint_receipt"]).get("unit_id") == row["unit_id"],
            ),
            "T1 mint state, key, repository and verbatim receipt are persisted",
        )
        ensure = world.events("ensure_unit")
        _check_all(
            (
                len(ensure) == 1,
                ensure[0]["accepted"],
                cast(dict[str, Any], ensure[0]["payload"]).get("unit_id") == row["unit_id"],
            ),
            "T1 an accepted ensure_unit event records the Unit",
        )
        links = _link_events(world.fake)
        _check_all(
            (
                [link["event"] for link in links] == ["spawned"],
                links[0]["dispatch_id"] == DISPATCH_ID,
                links[0]["schema"] == "managed-dispatch-link/v1",
                world.fake.commands()[-1] == "db unit record-event",
            ),
            "T1 a spawned observation follows the spawn",
        )


def test_mint_refusal_leaves_no_session_worktree_or_host(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        world.fake.configure(dispatched=["refuse"])
        result = world.dispatch(world.spec())
        _check(
            _code(result) == "unit_mint_refused",
            "T2 a register refusal fails the dispatch with unit_mint_refused",
        )
        row = world.row()
        _check_all(
            (
                row["state"] == DISPATCH_FAILED_START,
                row["next_required_action"] == "decide_retry_or_cancel",
                str(row["terminal_reason"]).startswith("unit_mint_refused:"),
            ),
            "T2 the dispatch row is failed_start awaiting a coordinator decision",
        )
        _check(world.no_side_effects(), "T2 no session row, host spawn or worktree exists")
        rejected = world.events("ensure_unit")
        _check_all(
            (
                len(rejected) == 1,
                not rejected[0]["accepted"],
                rejected[0]["rejection_code"] == "unit_mint_refused",
            ),
            "T2 the refused ensure is audited",
        )
        _check(world.fake.units() == [], "T2 the register holds no Unit")


def test_blank_kind_refuses_before_any_register_call(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        result = world.dispatch(world.spec(dispatch_kind=" ", reference_basis=""))
        _check_all(
            (
                _code(result) == "dispatch_kind_required",
                world.fake.argvs() == [],
                world.no_side_effects(),
            ),
            "T2 a blank dispatch_kind refuses dispatch_kind_required before any register call",
        )


def test_register_unavailable_refuses_before_spawn(fixture: RecordedLaneWorktreeFixture) -> None:
    from agent_messaging_plugin.register_unit_client import (  # noqa: PLC0415
        PsoletRegisterUnitClient,
    )

    with _harness(fixture) as world:
        missing = PsoletRegisterUnitClient(str(world.tmp / "no-such-psolet"), solet_name="fixture-solet")
        result = world.dispatch(world.spec(), register=missing)
        _check(
            _code(result) == "unit_register_unavailable",
            "T3 a missing psolet binary refuses with unit_register_unavailable",
        )
        _check(world.row()["state"] == DISPATCH_FAILED_START, "T3 the dispatch row is failed_start")
        _check(world.no_side_effects(), "T3 no session row, host spawn or worktree exists")


def test_stale_identity_refuses_before_any_register_call(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        spec = world.spec()
        plugin = AgentMessagingPlugin()
        stale = DispatchActor("agi-coordinator", "ases-rebound-elsewhere", "live_peer_binding")
        plugin._get_state_service = lambda: world.state  # type: ignore[method-assign]  # noqa: SLF001
        plugin._dispatch_actor_from_state = lambda _state: stale  # type: ignore[method-assign]  # noqa: SLF001
        plugin._register_unit_client = world.fake.client  # type: ignore[method-assign]  # noqa: SLF001
        raw = {**asdict(spec), "allowed_tools": list(spec.allowed_tools), "scope_tags": [], "selection_receipt": {}}
        response = plugin.dispatch_managed_work({"parameters": raw}, {})
        text = json.dumps(response, default=str)
        _check("coordinator_authority_denied" in text, "T4 a re-bound role refuses coordinator_authority_denied")
        _check(world.fake.argvs() == [], "T4 no register call was issued")
        _check(world.dispatch_rows() == [], "T4 no dispatch row exists")


def _verify_case(
    fixture: RecordedLaneWorktreeFixture,
    label: str,
    expected_code: str,
    seed: Callable[[_FakePsolet], None],
) -> None:
    with _harness(fixture) as world:
        seed(world.fake)
        result = world.dispatch(world.spec(unit_id="unt_seed"))
        _check_all(
            (
                _code(result) == expected_code,
                world.no_side_effects(),
                "dispatched" not in world.fake.commands(),
            ),
            f"T5 {label} refuses {expected_code} with zero sessions and no mint",
        )


def test_supplied_unit_is_verified_not_reminted(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        world.fake.seed_unit("unt_seed", unit_key="grooming-key")
        result = world.dispatch(world.spec(unit_id="unt_seed"))
        _check(isinstance(result, dict), "T5 a live matching Unit is accepted")
        _check("dispatched" not in world.fake.commands(), "T5 a supplied Unit is never re-minted")
        row = world.row()
        _check_all(
            (
                row["unit_id"] == "unt_seed",
                row["unit_mint_state"] == "verified",
                row["unit_mint_key"] == "grooming-key",
                world.sessions()[0]["unit_id"] == "unt_seed",
            ),
            "T5 the verified Unit is recorded on the dispatch and the session",
        )
        links = [link["event"] for link in _link_events(world.fake)]
        _check(links == ["tier_recorded", "spawned"], "T5 tier_recorded then spawned observations are recorded")
        second = world.dispatch(world.spec(unit_id="unt_seed", dispatch_id="mdp-second", lane_id="second-lane"))
        _check_all(
            (
                _code(second) == "unit_already_dispatched",
                len(world.sessions()) == 1,
            ),
            "T5 a second live dispatch on the same Unit refuses unit_already_dispatched",
        )
    _verify_case(fixture, "an absent Unit", "unit_not_found", lambda fake: None)
    _verify_case(fixture, "a terminal Unit", "unit_not_dispatchable", lambda fake: fake.seed_unit("unt_seed", state="completed"))
    _verify_case(fixture, "an authorized Unit", "unit_not_dispatchable", lambda fake: fake.seed_unit("unt_seed", state="authorized"))
    _verify_case(fixture, "a deleted Unit", "unit_not_found", lambda fake: fake.seed_unit("unt_seed", is_deleted=True))
    _verify_case(
        fixture, "another repository's Unit", "unit_repository_mismatch",
        lambda fake: fake.seed_unit("unt_seed", repository_id="rep_other"),
    )
    _verify_case(fixture, "a Unit of another kind", "unit_kind_mismatch", lambda fake: fake.seed_unit("unt_seed", kind="review"))


def test_post_mint_spawn_failure_keeps_unit_and_retry_reuses_it(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture, fail_start=True) as world:
        result = world.dispatch(world.spec())
        failed = world.row()
        _check_all(
            (
                isinstance(result, DispatchError),
                _data(result).get("unit_id") == failed["unit_id"] != "",
                failed["state"] == DISPATCH_FAILED_START,
            ),
            "T6 a post-mint spawn failure is failed_start and returns the retained unit_id",
        )
        _check(
            [link["event"] for link in _link_events(world.fake)] == ["failed_start"],
            "T6 the failed start is observed on the Unit",
        )
        mints_before = world.fake.commands().count("dispatched")
        world.driver.fail_start = False
        retried = world.retry()
        _check(world.fake.commands().count("dispatched") == mints_before == 1, "T6 retry issues no new mint")
        replacement = [s for s in world.sessions() if s.get("agent_instance_id") == retried["current_agent_instance_id"]]
        _check_all(
            (
                retried["unit_id"] == failed["unit_id"],
                len(world.fake.units()) == 1,
                len(replacement) == 1,
                replacement[0]["unit_id"] == failed["unit_id"],
                all(session["unit_id"] == failed["unit_id"] for session in world.sessions()),
            ),
            "T6 the replacement attempt carries the original Unit; the register holds one Unit",
        )
        _check(
            [link["event"] for link in _link_events(world.fake)] == ["failed_start", "spawned"],
            "T6 the replacement attempt is observed as spawned",
        )


def test_repository_is_resolved_from_lane_root_never_cwd(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        elsewhere = world.tmp / "unrelated-checkout"
        (elsewhere / ".git").mkdir(parents=True)
        (world.root / "profile").mkdir()
        cwd = Path.cwd()
        try:
            os.chdir(elsewhere)
            with _environment(APP_HOME=str(world.root / "profile")):
                result = world.dispatch(world.spec(repository_root=""))
        finally:
            os.chdir(cwd)
        _check(isinstance(result, dict), "T7 an APP_HOME-derived lane root dispatches")
        _check(
            world.fake.argvs()[0] == ["db", "repository", "resolve", str(world.root.resolve())],
            "T7 resolution uses APP_HOME's parent even with cwd in another checkout",
        )
    with _harness(fixture) as world:
        with _environment(APP_HOME=None):
            result = world.dispatch(world.spec(repository_root=""))
        _check_all(
            (
                _code(result) == "lane_worktree_app_home_required",
                world.fake.argvs() == [],
                world.no_side_effects(),
            ),
            "T7 no root and no APP_HOME refuses before any register call",
        )
    with _harness(fixture) as world:
        result = world.dispatch(world.spec(repository_id="rep_expected-elsewhere"))
        _check_all(
            (
                _code(result) == "unit_repository_mismatch",
                world.fake.commands() == ["db repository resolve"],
                world.no_side_effects(),
            ),
            "T7 an expected repository_id that differs from resolution refuses before the mint",
        )
    with _harness(fixture) as world:
        world.fake.configure(resolve=["refuse"])
        result = world.dispatch(world.spec())
        _check_all(
            (
                _code(result) == "unit_repository_unresolved",
                world.no_side_effects(),
            ),
            "T7 an unregistered lane root refuses unit_repository_unresolved",
        )


def test_duplicate_request_yields_one_unit(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        first = world.dispatch(world.spec(unit_key="shared-key"))
        second = world.dispatch(world.spec(unit_key="shared-key", dispatch_id="mdp-second", local_name="Second-Fixture"))
        _check(isinstance(first, dict), "T8 the first dispatch mints the pinned key")
        _check_all(
            (
                _code(second) == "unit_mint_refused",
                "unique" in _message(second).lower(),
                len(world.sessions()) == 1,
            ),
            "T8 a second dispatch pinning the same key is refused by the register's UNIQUE key",
        )
        retried = world.retry("mdp-second")
        _check_all(
            (
                retried["state"] == DISPATCH_FAILED_START,
                str(retried["terminal_reason"]).startswith("unit_mint_conflict:"),
                world.fake.commands().count("dispatched") == 2,
            ),
            "T8 its retry reconciles by key, finds another dispatch's Unit, and never mints a third",
        )
        _check(len(world.fake.units()) == 1 and len(world.sessions()) == 1, "T8 the register holds exactly one Unit")


def test_uncertain_mint_reconciles_by_pinned_key(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        world.fake.configure(dispatched=["timeout_after_commit"])
        result = world.dispatch(world.spec(), register=world.fake.client(timeout_seconds=5.0))
        row = world.row()
        _check_all(
            (
                isinstance(result, dict),
                row["unit_mint_state"] == "adopted",
                len(world.fake.units()) == 1,
                row["unit_id"] == world.fake.units()[0]["id"],
            ),
            "T9 a commit-then-timeout mint is adopted by its pinned key",
        )
        _check_all(
            (
                world.fake.commands()[:4] == ["db repository resolve", "dispatched", "db unit list", "db unit show"],
                world.fake.commands().count("dispatched") == 1,
                len(world.sessions()) == 1,
            ),
            "T9 reconciliation precedes any second mint and the spawn proceeds once",
        )
    with _harness(fixture) as world:
        world.fake.seed_unit("unt_foreign", unit_key=DEFAULT_KEY, dispatch_source_ref="managed-dispatch:mdp-other")
        world.fake.configure(dispatched=["timeout_before_commit"])
        result = world.dispatch(world.spec(), register=world.fake.client(timeout_seconds=5.0))
        _check_all(
            (
                _code(result) == "unit_mint_conflict",
                world.row()["state"] == DISPATCH_FAILED_START,
                world.no_side_effects(),
            ),
            "T9 a key held by another dispatch refuses unit_mint_conflict with no spawn",
        )
    with _harness(fixture) as world:
        world.fake.configure(dispatched=["timeout_before_commit", "timeout_after_commit"])
        result = world.dispatch(world.spec(), register=world.fake.client(timeout_seconds=5.0))
        _check_all(
            (
                _code(result) == "unit_mint_uncertain",
                world.no_side_effects(),
                world.fake.commands().count("dispatched") == 2,
            ),
            "T9 two uncertain mints refuse unit_mint_uncertain with no spawn",
        )
        retried = world.retry()
        _check_all(
            (
                retried["unit_mint_state"] == "adopted",
                world.fake.commands().count("dispatched") == 2,
                len(world.fake.units()) == 1,
                len(world.sessions()) == 1,
            ),
            "T9 a later retry adopts the committed Unit without minting again",
        )


def test_cancel_retires_only_a_pre_uptake_self_minted_unit(fixture: RecordedLaneWorktreeFixture) -> None:
    # failed_start is terminal (cancel refuses it), so "minted, never taken up"
    # is a preparing row whose Unit was ensured before its spawn completed.
    with _harness(fixture) as world:
        prepared = prepare_managed_dispatch(world.state, world.spec(), now=T0)
        actor = RegisterActor(prepared["spawned_by_instance_id"], "", prepared["spawned_by_role"], LANE)
        ensure_unit(world.state, prepared, world.fake.client(), actor, reconcile_first=False)
        cancelled = world.cancel()
        retire = [argv for argv in world.fake.argvs() if argv[:3] == ["db", "unit", "retire"]]
        _check_all(
            (
                cancelled["unit_retired"] is True,
                len(retire) == 1,
                _flag(retire[0], "--state") == "cancelled",
                _flag(retire[0], "--reason").startswith(f"{SOURCE_REF} cancelled before uptake:"),
                world.fake.units()[0]["state"] == "cancelled",
            ),
            "T10 a self-minted Unit no attempt took up is retired cancelled",
        )
    with _harness(fixture) as world:
        world.fake.seed_unit("unt_seed")
        world.dispatch(world.spec(unit_id="unt_seed"))
        cancelled = world.cancel()
        _check_all(
            (
                cancelled["unit_retired"] is False,
                "db unit retire" not in world.fake.commands(),
                _link_events(world.fake)[-1]["event"] == "cancelled",
            ),
            "T10 a verified Unit is only annotated on cancel",
        )
    with _harness(fixture) as world:
        world.dispatch(world.spec())
        cancelled = world.cancel()
        _check_all(
            (
                cancelled["unit_retired"] is False,
                "db unit retire" not in world.fake.commands(),
                _link_events(world.fake)[-1]["event"] == "cancelled",
            ),
            "T10 a minted Unit whose attempt had uptake is only annotated on cancel",
        )


def test_annotation_failure_never_blocks_dispatch_transition(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        world.fake.configure(record_event=["fail"])
        result = world.dispatch(world.spec())
        failures = world.events("register_annotation_failed")
        _check_all(
            (
                isinstance(result, dict),
                world.row()["state"] != DISPATCH_FAILED_START,
                len(failures) == 1,
                cast(dict[str, Any], failures[0]["payload"]).get("event") == "spawned",
            ),
            "T11 a failed spawned annotation leaves the dispatch live and is recorded",
        )
    with _harness(fixture, fail_start=True) as world:
        world.fake.configure(record_event=["fail"])
        result = world.dispatch(world.spec())
        _check_all(
            (
                isinstance(result, DispatchError),
                _code(result) != "unit_annotation_refused",
                world.row()["state"] == DISPATCH_FAILED_START,
                len(world.events("register_annotation_failed")) == 1,
            ),
            "T11 a failed failed_start annotation keeps the spawn error and is recorded",
        )


def test_prepared_dispatch_without_ensured_unit_cannot_spawn(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        spec = world.spec(unit_id="unt_carried-not-ensured")
        prepared = prepare_managed_dispatch(world.state, spec, now=T0)
        _check(prepared["next_required_action"] == "ensure_unit", "T12 a prepared row starts at ensure_unit")
        try:
            spawn_session(world.state, replace(_spawn_request(spec), dispatch_id=DISPATCH_ID))
            code = ""
        except VerbError as exc:
            code = exc.code
        _check(code == "unit_not_ensured" and world.no_side_effects(), "T12 raw spawn of an un-ensured dispatch refuses")


def test_dispatch_managed_work_declares_unit_parameters(fixture: RecordedLaneWorktreeFixture) -> None:
    del fixture
    metadata = AgentMessagingPlugin.dispatch_managed_work._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
    schema = InvocationSchemaGenerator().generate(
        "plugin::agent_messaging_plugin::dispatch_managed_work",
        {name: parameter.to_dict() for name, parameter in metadata.parameters.items()},
    )
    arguments = cast(dict[str, Any], cast(dict[str, Any], schema["properties"])["arguments"])
    properties = cast(dict[str, Any], arguments["properties"])
    for name in (
        "unit_id", "repository_id", "unit_key", "addresses", "reference_basis", "reference_basis_reason",
        "brief_repository_root",
    ):
        _check_all(
            (
                name in metadata.parameters,
                name in properties,
            ),
            f"T13 {name} is in the discoverable dispatch_managed_work schema",
        )
    provisioning = AgentMessagingPlugin.provision_role_session._platform_process_metadata  # type: ignore[attr-defined]  # noqa: SLF001
    for name in ("reference_basis", "reference_basis_reason", "brief_repository_root"):
        _check(
            name in provisioning.parameters,
            f"T13 provision_role_session declares {name} so a fix-kind provisioning can mint",
        )


def _outside_brief(directory: Path) -> tuple[Path, str]:
    directory.mkdir(parents=True, exist_ok=True)
    brief = directory / "R3_BRIEF.md"
    brief.write_text("brief stored outside the lane root\n", encoding="utf-8")
    return brief, hashlib.sha256(brief.read_bytes()).hexdigest()


def _refused_before_mint(world: _Harness, result: object, code: str) -> bool:
    return (
        _code(result) == code
        and "dispatched" not in world.fake.commands()
        and world.fake.units() == []
        and world.no_side_effects()
        and world.row()["state"] == DISPATCH_FAILED_START
    )


def _states_ruling(result: object) -> bool:
    return all(text in _message(result) for text in ("a home-directory reports folder", "NOT supported", "registered repository's workbench"))


def test_brief_in_lane_root_needs_no_declared_root(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        result = world.dispatch(world.spec())
        mint = next((argv for argv in world.fake.argvs() if argv[:1] == ["dispatched"]), [])
        _check_all(
            (isinstance(result, dict), bool(mint), "--brief-repo" not in mint),
            "T15 an in-root brief mints with no --brief-repo",
        )


def test_declared_brief_root_mints_production_shaped_brief(fixture: RecordedLaneWorktreeFixture) -> None:
    # Production shape: an absolute brief_ref in another checkout's workbench.
    with _harness(fixture) as world:
        other = world.tmp / "project-solet"
        (other / ".git").mkdir(parents=True)
        brief, digest = _outside_brief(other / "workbench" / "a9f1776c")
        result = world.dispatch(world.spec(brief_ref=str(brief), brief_sha256=digest, brief_repository_root=str(other)))
        argvs = world.fake.argvs()
        mint = next((argv for argv in argvs if argv[:1] == ["dispatched"]), [])
        _check(isinstance(result, dict), "T15 a brief under the declared brief_repository_root mints and spawns")
        _check_all(
            (
                _flag(mint, "--brief") == str(brief),
                _flag(mint, "--brief-repo") == str(other.resolve()),
                _flag(mint, "--repo") == str(world.root.resolve()),
            ),
            "T15 the mint pins --brief-repo to the declared root and keeps --repo on the lane root",
        )
        _check_all(
            (
                argvs[:2] == [
                    ["db", "repository", "resolve", str(world.root.resolve())],
                    ["db", "repository", "resolve", str(other.resolve())],
                ],
                world.fake.commands()[2] == "dispatched",
            ),
            "T15 the declared brief root is resolved as a registered repository before the mint",
        )
        row = world.row()
        _check_all(
            (
                row["unit_repository_id"] == REPO_ID,
                cast(dict[str, Any], row["unit_mint_request"]).get("brief_repository_root") == str(other),
                len(world.sessions()) == 1,
            ),
            "T15 the declared root is pinned on the dispatch and the Unit stays the lane root's",
        )


def test_brief_outside_lane_root_is_never_inferred(fixture: RecordedLaneWorktreeFixture) -> None:
    with _harness(fixture) as world:
        other = world.tmp / "project-solet"
        (other / ".git").mkdir(parents=True)
        brief, digest = _outside_brief(other / "workbench")
        result = world.dispatch(world.spec(brief_ref=str(brief), brief_sha256=digest))
        _check_all(
            (
                _refused_before_mint(world, result, "unit_brief_outside_repository"),
                world.fake.commands() == ["db repository resolve"],
                _states_ruling(result),
            ),
            "T15 an undeclared brief outside the lane root refuses loudly with no mint",
        )
    with _harness(fixture) as world:
        other = world.tmp / "project-solet"
        (other / ".git").mkdir(parents=True)
        result = world.dispatch(world.spec(brief_repository_root=str(other)))
        _check_all(
            (_refused_before_mint(world, result, "unit_brief_outside_repository"), _states_ruling(result)),
            "T15 a brief outside its declared brief_repository_root refuses",
        )
    with _harness(fixture) as world:
        result = world.dispatch(world.spec(brief_repository_root="relative/checkout"))
        _check(
            _refused_before_mint(world, result, "unit_brief_repository_invalid"),
            "T15 a relative brief_repository_root refuses",
        )


def test_home_reports_brief_is_refused_with_the_ruling(fixture: RecordedLaneWorktreeFixture) -> None:
    # A home-directory reports folder is not a checkout: 59 of 240 dispatches since 09-20 kept
    # briefs there, and the PSM ruling is that the mint refuses them.
    for declared in (False, True):
        with _harness(fixture) as world:
            reports = world.tmp / "home_reports"
            brief, digest = _outside_brief(reports / "2026-09-25")
            overrides: dict[str, Any] = {"brief_ref": str(brief), "brief_sha256": digest}
            if declared:
                overrides["brief_repository_root"] = str(reports)
            result = world.dispatch(world.spec(**overrides))
            code = "unit_brief_repository_unregistered" if declared else "unit_brief_outside_repository"
            _check_all(
                (_refused_before_mint(world, result, code), _states_ruling(result)),
                f"T15 a home-directory reports brief ({'declared' if declared else 'undeclared'} root) refuses {code}"
                " and names the ruling",
            )


def test_register_adoption_module_is_gone(fixture: RecordedLaneWorktreeFixture) -> None:
    del fixture
    _check(
        Path(ananta.__file__).resolve().is_relative_to(REPO_ROOT.resolve()),
        "T14 ananta resolves from the tree under test",
    )
    _check(
        importlib.util.find_spec("ananta.core.orchestration.register_adoption") is None,
        "T14 the superseded U-C register_adoption module is removed",
    )


_TESTS: tuple[Callable[[RecordedLaneWorktreeFixture], None], ...] = (
    test_mint_runs_before_spawn_and_links_both_rows,
    test_mint_refusal_leaves_no_session_worktree_or_host,
    test_blank_kind_refuses_before_any_register_call,
    test_register_unavailable_refuses_before_spawn,
    test_stale_identity_refuses_before_any_register_call,
    test_supplied_unit_is_verified_not_reminted,
    test_post_mint_spawn_failure_keeps_unit_and_retry_reuses_it,
    test_repository_is_resolved_from_lane_root_never_cwd,
    test_duplicate_request_yields_one_unit,
    test_uncertain_mint_reconciles_by_pinned_key,
    test_cancel_retires_only_a_pre_uptake_self_minted_unit,
    test_annotation_failure_never_blocks_dispatch_transition,
    test_prepared_dispatch_without_ensured_unit_cannot_spawn,
    test_dispatch_managed_work_declares_unit_parameters,
    test_register_adoption_module_is_gone,
    test_brief_in_lane_root_needs_no_declared_root,
    test_declared_brief_root_mints_production_shaped_brief,
    test_brief_outside_lane_root_is_never_inferred,
    test_home_reports_brief_is_refused_with_the_ruling,
)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw, RecordedLaneWorktreeFixture(Path(raw)) as fixture:
        for test in _TESTS:
            try:
                test(fixture)
            except Exception:  # noqa: BLE001 - a crashing test is a red result, not an abort
                _failed.append(test.__name__)
                print(f"  FAIL  {test.__name__} raised:\n{traceback.format_exc()}")
    print(f"\nmanaged dispatch unit mint smoke: {_passed} passed, {len(_failed)} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
