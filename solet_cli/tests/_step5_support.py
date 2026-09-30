"""Shared fixture for the Step-5 (existing-install runtime transition) smokes.

Builds a real Git seed with a baseline and a candidate release, a cloned
target at the baseline, a Manager home, a private ``HOME`` with the operator's
files, and enrols the target so the Step-4 update can run for real up to
``source_advanced``.  The candidate commit carries a real transition bundle
whose digest the descriptor binds, exactly as a staged release would.

Target adapters are exercised IN PROCESS through the seed's own
``setup_adapter.dispatch_request`` and the bootstrap adapter's
``execute_adapter_request`` (so the seed-side D2 validator and the real
hydration three-way engine run), while the host seams that would touch
launchd, the router, or a running service are programmable fakes.  Nothing
here opens a database connection; ``db_spy`` proves it for every caller.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(_ROOT / "solet_cli" / "src"),
    str(_ROOT / "solet_setup_contracts" / "src"),
    str(_ROOT / "plugins" / "github_midwife_plugin" / "src"),
    str(_ROOT),
]
from github_midwife_plugin.setup_adapter import dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome, bounded_command_outcome  # noqa: E402
from solet_manager._existing_install_inspection_metadata import InstalledUpdateDescriptor  # noqa: E402
from solet_manager.adapter_protocol import OperationRequest, OperationResult  # noqa: E402
from solet_manager.existing_install_adapters import ExistingInstallAdapterRegistry  # noqa: E402
from solet_manager.existing_install_inspection import (  # noqa: E402
    ChannelInspectionIdentity,
    ExistingInstallContractIdentity,
    InstalledInspectionMetadata,
)
from solet_manager.maintenance_inventory import read_maintenance_inventory_v2, write_maintenance_inventory_v2  # noqa: E402
from solet_manager.models import (  # noqa: E402
    ChannelIdentity,
    ContractIdentities,
    FilesystemIdentity,
    InstanceInventoryRecordV2,
    JsonValue,
    ManagementOrigin,
    ManagementState,
    ObservedProvenanceIdentity,
    ReleaseIdentity,
    ServiceIdentity,
    TargetIdentity,
    UpdateEligibility,
    UpdateEligibilityState,
)
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.reconciliation_request import ReconciliationOutcome, parse_reconciliation_response  # noqa: E402
from solet_manager.release_lock import seed_lock_from_fields  # noqa: E402
from solet_manager.seed_lock_parser import parse_seed_lock_bytes  # noqa: E402
from solet_manager.update_execution import UpdateRequest, apply_update, preview_update_instance  # noqa: E402
from solet_manager.update_journal import read_update_journal  # noqa: E402
from solet_manager.update_runtime_plan import RuntimeSeams  # noqa: E402

from bootstrap_adapter.routes import execute_adapter_request  # noqa: E402

ORIGIN_ID = "123e4567-e89b-12d3-a456-426614174001"
CANONICAL = "https://github.com/example/seed.git"
KB = "plugins/github_midwife_plugin/knowledge_base"
TEMPLATES = f"{KB}/hydration_templates"
ADAPTER_MODULE = "plugins/github_midwife_plugin/src/github_midwife_plugin/setup_adapter.py"
_ENV = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
_REPO_KB = _ROOT / KB
HOOKS = "plugins/github_midwife_plugin/claude_plugin/coordination-hooks/hooks"
HOOK_FILES = ("coordination_owner.py", "heartbeat_report_alive.py", "rotation_due_watch.py", "wake_waiter.py", "hooks.json")
PATH_ENV = "/opt/homebrew/bin:/opt/homebrew/sbin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(frozen=True)
class Release:
    commit: str
    tree: str
    provenance: bytes
    seed_id: str
    manifest: str
    source_commit: str
    tag: str


@dataclass
class FakeHost:
    """Programmable launchd / bridge / reconciliation behaviour for one fixture."""

    pids: list[int] = field(default_factory=lambda: [4242, 4343])
    target: Path = Path("/fixture/target")
    loaded: bool = True
    health: list[dict[str, JsonValue]] = field(default_factory=lambda: [{"status": "healthy"}])
    attestations: list[dict[str, JsonValue]] = field(default_factory=lambda: [])
    reconciliation: Callable[[dict[str, JsonValue]], str] | None = None
    launchctl_calls: list[tuple[str, ...]] = field(default_factory=lambda: [])
    bridge_calls: list[tuple[str, dict[str, JsonValue]]] = field(default_factory=lambda: [])
    bootstrap_fails: bool = False
    bootout_clears: bool = True
    claude_running: bool = False
    closure_scenario: str = "closed"
    pip_calls: list[list[str]] = field(default_factory=lambda: [])
    adapter_calls: list[tuple[str, str, str | None]] = field(default_factory=lambda: [])
    pip_fail_once: bool = False
    installed: bool = False
    #: Step-6 host seams: the closed ``ps`` vector, knowledge search answers, Keychain metadata.
    processes: list[dict[str, JsonValue]] | None = None
    search: Callable[[dict[str, JsonValue]], dict[str, JsonValue]] | None = None
    keychain_present: bool = True
    ps_calls: int = 0
    ps_fails: bool = False

    def run_ps(self, timeout: int) -> subprocess.CompletedProcess[str]:
        del timeout
        self.ps_calls += 1
        if self.ps_fails:
            raise OSError("process table unavailable")
        rows = self.processes if self.processes is not None else [{"pid": self.pids[0], "lstart": "Fri Sep 18 12:00:00 2026", "command": "{TARGET}/.venv/bin/python3 -m ananta.cli --app-home {TARGET}/profile"}]
        lines = [f"{row['pid']} {row['lstart']} {str(row['command']).replace('{TARGET}', str(self.target))}" for row in rows]
        return subprocess.CompletedProcess(("/bin/ps",), 0, "\n".join(lines) + "\n", "")

    def run_security(self, service: str, account: str) -> subprocess.CompletedProcess[str]:
        del account
        return subprocess.CompletedProcess(("security",), 0 if self.keychain_present else 44, f'"svce"<blob>="{service}"\n', "")

    def launchctl(self, registry: ExistingInstallAdapterRegistry, verb: str, arguments: tuple[str, ...], timeout: int) -> subprocess.CompletedProcess[str]:
        del registry, timeout
        self.launchctl_calls.append((verb, *arguments))
        if verb == "print":
            if not self.loaded:
                return subprocess.CompletedProcess(("launchctl",), 113, "", "Could not find service")
            return subprocess.CompletedProcess(("launchctl",), 0, f"\tstate = running\n\tpid = {self.pids[0]}\n", "")
        if verb == "bootout":
            if self.bootout_clears:
                self.loaded = False
            return subprocess.CompletedProcess(("launchctl",), 0, "", "")
        if verb == "bootstrap":
            if self.bootstrap_fails:
                return subprocess.CompletedProcess(("launchctl",), 5, "", "Input/output error")
            self.loaded = True
            if len(self.pids) > 1:
                self.pids.pop(0)
            return subprocess.CompletedProcess(("launchctl",), 0, "", "")
        return subprocess.CompletedProcess(("launchctl",), 0, "", "")

    def read_health(self, registry: ExistingInstallAdapterRegistry, timeout: int) -> dict[str, JsonValue]:
        del registry, timeout
        return self.health[0] if len(self.health) == 1 else self.health.pop(0)

    def invoke_bridge(self, registry: ExistingInstallAdapterRegistry, key: str, arguments: dict[str, JsonValue], kind: str, timeout: int) -> dict[str, JsonValue]:
        del registry, timeout
        self.bridge_calls.append((key, arguments))
        if kind == "self_deployment.attest":
            if not self.attestations:
                raise _attest_unreachable()
            return self.attestations[0] if len(self.attestations) == 1 else self.attestations.pop(0)
        if kind == "knowledge.read":
            return {"results": []} if self.search is None else self.search(arguments)
        return {"status": "ok", "process_key": key}

    def invoke_reconciliation(self, registry: ExistingInstallAdapterRegistry, envelope: dict[str, JsonValue], timeout: int) -> ReconciliationOutcome:
        del registry, timeout
        if self.reconciliation is None:
            raise _attest_unreachable()
        return parse_reconciliation_response(self.reconciliation(envelope))


def _attest_unreachable() -> Exception:
    from solet_manager.errors import AdapterError  # noqa: PLC0415

    return AdapterError("bridge call did not succeed: attestation unreachable")


@dataclass
class Fixture:
    root: Path
    source: Path
    target: Path
    home: Path
    paths: ManagerPaths
    baseline: Release
    candidate: Release
    contract: str
    descriptor_digest: str
    request: UpdateRequest
    host: FakeHost
    record_name: str = "fixture"

    @property
    def plist_path(self) -> Path:
        return self.home / "Library" / "LaunchAgents" / "local.solet.fixture.plist"

    def record(self) -> InstanceInventoryRecordV2:
        records = read_maintenance_inventory_v2(self.paths.maintenance_inventory_path)
        assert len(records) == 1
        return records[0]

    def journal(self) -> dict[str, JsonValue]:
        """The active update journal; after promotion (Step 6) the pointer is released and the last verified operation is it."""
        record = self.record()
        operation_id = record.active_operation.operation_id if record.active_operation is not None else record.last_verified_operation_id
        assert operation_id is not None
        return read_update_journal(self.paths.operation_path(record.instance_id, operation_id))


def _claude_outcome(argv: tuple[str, ...], output_limit: int) -> CommandOutcome:
    if argv[1:3] == ("plugin", "list"):
        return bounded_command_outcome(returncode=0, timed_out=False, duration_ms=1, stdout=json.dumps([{"id": "coordination-hooks@fixture", "enabled": True}]), stderr="", output_limit=output_limit)
    return CommandOutcome(0, False, 1, "", "")


class SeedRuntime:
    """In-process seed ``Runtime``: real files under the fixture HOME, canned host commands."""

    def __init__(self, home: Path, host: FakeHost, target: Path) -> None:
        self.home = home
        self.host = host
        self.target = target
        self.commands: list[tuple[str, ...]] = []
        self.writes: list[Path] = []

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = 4096,
    ) -> CommandOutcome:
        del timeout_seconds, input_text
        self.commands.append(argv)
        if argv[0] == "git":
            completed = subprocess.run(argv, cwd=cwd, capture_output=True, text=True, env={**_ENV, **(extra_env or {})}, check=False)
            return bounded_command_outcome(returncode=completed.returncode, timed_out=False, duration_ms=1, stdout=completed.stdout, stderr=completed.stderr, output_limit=output_limit)
        if argv[0] == "/bin/launchctl":
            return CommandOutcome(1, False, 1, "", "Could not find service")
        if argv[0] == "/fixture/bin/claude":
            if argv[1:3] == ("plugin", "install"):
                self._refresh_plugin_cache()
            return _claude_outcome(argv, output_limit)
        return self._host_probe(argv)

    def _refresh_plugin_cache(self) -> None:
        """The fake ``claude plugin install``: the cache copy becomes the tree's shipped hooks (Step 6, F-RT-6)."""
        cache = self.home / ".claude" / "plugins" / "cache" / "fixture" / "coordination-hooks" / "1.0.0" / "hooks"
        shipped = self.target / HOOKS
        if not shipped.is_dir():
            return
        cache.mkdir(parents=True, exist_ok=True)
        for entry in shipped.iterdir():
            if entry.is_file():
                (cache / entry.name).write_bytes(entry.read_bytes())

    def _host_probe(self, argv: tuple[str, ...]) -> CommandOutcome:
        if argv[:2] == ("/usr/bin/which", "claude"):
            return CommandOutcome(0, False, 1, "/fixture/bin/claude\n", "")
        if argv[:1] == ("/usr/bin/which",):
            return CommandOutcome(1, False, 1, "", "")
        if argv[:2] == ("/usr/bin/pgrep", "-x"):
            return CommandOutcome(0 if self.host.claude_running else 1, False, 1, "", "")
        return CommandOutcome(0, False, 1, "", "")

    def http_json(self, url: str, *, timeout_seconds: int, payload: dict[str, JsonValue] | None = None) -> tuple[int, JsonValue]:
        del url, timeout_seconds, payload
        return 503, None

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        self.writes.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        path.chmod(mode)


def make_seams(fixture_home: Path, host: FakeHost, target: Path) -> RuntimeSeams:
    runtime = SeedRuntime(fixture_home, host, target)

    def invoke_adapter(registry: ExistingInstallAdapterRegistry, request: OperationRequest) -> OperationResult:
        host.adapter_calls.append((request.operation_ref, request.phase, request.probe_purpose))
        payload = json.loads(json.dumps(request.to_dict(), sort_keys=True))
        if request.operation_ref == "existing::dependencies.reconcile":
            raw = execute_adapter_request(payload, runner=_closure_runner(host, registry.target), which=lambda _name: None, base_python=str(sys.executable))
        else:
            raw = dispatch_request(AdapterRequest.from_json(json.dumps(payload)), runtime)
        return OperationResult.from_dict(cast(dict[str, JsonValue], raw), request)

    host.target = target
    return RuntimeSeams(
        home=fixture_home,
        invoke_adapter=invoke_adapter,
        invoke_reconciliation=host.invoke_reconciliation,
        invoke_bridge=host.invoke_bridge,
        read_health=host.read_health,
        launchctl=host.launchctl,
        uid=501,
        run_ps=host.run_ps,
        run_security=host.run_security,
        # Step 7 section 4.1: the two host-software seams, bound to the building interpreter and to "nothing on PATH"
        # so no Step 5/6 smoke ever consults the developer's host for Python 3.13, brew, tmux or psql.  The one
        # exception is the fixture's own ``claude`` (the path ``_host_probe`` answers), which the source preview
        # requires (iss_646b54b6).
        resolve_base_python=lambda: Path(sys.executable),
        which=lambda name: "/fixture/bin/claude" if name == "claude" else None,
    )


def _closure_runner(host: FakeHost, target: Path) -> Callable[..., subprocess.CompletedProcess[str]]:
    from bootstrap_adapter.dependency import REQUIRED_DISTRIBUTIONS  # noqa: PLC0415

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "-I" in command and "-c" in command:
            if "packages =" not in command[-1]:
                return subprocess.CompletedProcess(command, 0, "3.13.7\n", "")
            pieces = list(REQUIRED_DISTRIBUTIONS)
            plugins_root = target / "plugins"
            if plugins_root.is_dir():
                pieces.extend((entry.name, f"plugins/{entry.name}") for entry in sorted(plugins_root.iterdir()) if entry.is_dir() and entry.name not in {rel.rsplit("/", 1)[-1] for _, rel in REQUIRED_DISTRIBUTIONS})
            packages = {
                distribution: {"version": "1.0.0", "direct_url": json.dumps({"url": (target / relative).resolve().as_uri(), "dir_info": {"editable": True}})}
                for distribution, relative in pieces
            }
            if host.closure_scenario == "missing_package" and not host.installed:
                packages.pop("github_midwife_plugin")
            return subprocess.CompletedProcess(command, 0, json.dumps({"pip": True, "build_backend": True, "wheel": True, "packages": packages}), "")
        if command[-1:] == ["--version"] and Path(command[0]).name == "solet-bridge":
            return subprocess.CompletedProcess(command, 0, "solet-bridge 0.1.0\n", "")
        if "pip" in command:
            host.pip_calls.append(list(command))
            if host.pip_fail_once and len(host.pip_calls) == 1:
                return subprocess.CompletedProcess(command, 1, "", "injected pip failure")
            host.installed = True
            return subprocess.CompletedProcess(command, 0, "", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    return run


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(("git", "-C", str(repo), *args), check=True, capture_output=True, text=True, env=_ENV)
    return completed.stdout.strip()


def _stamp(source_commit: str, manifest: str) -> tuple[bytes, str]:
    seed_id = str(uuid.uuid5(uuid.UUID(ORIGIN_ID), f"{source_commit}:{manifest}::"))
    stamp = {
        "ancestry": [],
        "bundle": {"name": "macos-bizops", "platform": "local"},
        "lineage": [],
        "manifest_sha256": manifest,
        "origin_id": ORIGIN_ID,
        "schema_version": 1,
        "seed_id": seed_id,
        "signature": None,
        "source_commit": source_commit,
        "source_date": "2026-09-16T00:00:00+00:00",
    }
    return (json.dumps(stamp, indent=2, sort_keys=True) + "\n").encode(), seed_id


def seal(repo: Path, source_commit: str, manifest: str, tag: str, files: dict[str, str | bytes]) -> Release:
    provenance, seed_id = _stamp(source_commit, manifest)
    (repo / "PROVENANCE.json").write_bytes(provenance)
    for name, content in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
    git(repo, "add", "-A", "-f")
    message = "\n".join(
        (
            "Seed bundle (factory-sealed)",
            "",
            f"Seed-Id: {seed_id}",
            f"Origin-Id: {ORIGIN_ID}",
            f"Manifest-SHA256: {manifest}",
            f"Assembled-Ref: {source_commit}",
            "License-Policy: public_apache",
            "Minted-At: 2026-09-16T00:00:00+00:00",
        )
    )
    git(repo, "commit", "--quiet", "-m", message)
    git(repo, "tag", "-a", tag, "-m", tag)
    return Release(git(repo, "rev-parse", "HEAD"), git(repo, "rev-parse", "HEAD^{tree}"), provenance, seed_id, manifest, source_commit, tag)


def template_bytes(name: str) -> bytes:
    return (_REPO_KB / "hydration_templates" / name).read_bytes()


def digest_of(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def default_artifacts() -> list[dict[str, Any]]:
    plist, zshrc, claude = (digest_of(template_bytes(name)) for name in ("launchagent.plist.template", "zshrc_block.template", "user_claude_md_section.template"))
    return [
        {
            "artifact_id": "instance_launchagent_plist",
            "kind": "launchd_plist",
            "logical_destination": "{HOME}/Library/LaunchAgents/local.solet.{NAME}.plist",
            "preservation_class": "manager_generated_whole",
            "marker": None,
            "stamp": "<!-- rendered-from: {TEMPLATE_REF}@{TEMPLATE_DIGEST} -->",
            "template_ref": f"{TEMPLATES}/launchagent.plist.template",
            "template_digest": plist,
            "previous_template_digests": [plist],
        },
        {
            "artifact_id": "shell_startup_block",
            "kind": "managed_block",
            "logical_destination": "{HOME}/.zshrc",
            "preservation_class": "operator_owned_with_managed_block",
            "marker": {"begin": "# BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8}", "end": "# END SOLET {NAME}"},
            "stamp": None,
            "template_ref": f"{TEMPLATES}/zshrc_block.template",
            "template_digest": zshrc,
            "previous_template_digests": [zshrc],
        },
        {
            "artifact_id": "user_claude_md_section",
            "kind": "managed_block",
            "logical_destination": "{HOME}/.claude/CLAUDE.md",
            "preservation_class": "operator_owned_with_managed_block",
            "marker": {"begin": "<!-- BEGIN SOLET {NAME} v{TEMPLATE_DIGEST8} -->", "end": "<!-- END SOLET {NAME} -->"},
            "stamp": None,
            "template_ref": f"{TEMPLATES}/user_claude_md_section.template",
            "template_digest": claude,
            "previous_template_digests": [claude],
        },
    ]


def operation(operation_id: str, ref: str, runner: str, stage: str, mutation: str, rollback: str, inputs: list[str], *, retry: str = "retry_safe", confirm: bool = True, applies: Any = "any") -> dict[str, Any]:
    return {
        "operation_id": operation_id,
        "operation_ref": ref,
        "runner": runner,
        "stage": stage,
        "mutation_class": mutation,
        "rollback_class": rollback,
        "retry_policy": retry,
        "idempotency_key": "sha256(candidate_commit, operation_id, instance_id)",
        "applies_when": applies,
        "precondition_probe_refs": [f"{operation_id}_probe"],
        "postcondition_probe_refs": [f"{operation_id}_probe"],
        "public_inputs": inputs,
        "requires_confirmation": confirm,
    }


def default_operations() -> list[dict[str, Any]]:
    return [
        operation("dependencies_reconcile", "existing::dependencies.reconcile", "bootstrap", "dependencies", "venv", "reversible", ["declared_closure"], confirm=False),
        operation("migration_solet_rename", "existing::migration.solet_rename", "target_adapter", "migrations_pre", "launchd", "backup_required", []),
        operation("migration_export_root_containment", "existing::migration.export_root_containment", "target_adapter", "migrations_pre", "filesystem_managed_artifact", "backup_required", []),
        operation("hydration_reconcile", "existing::hydration.reconcile", "target_adapter", "hydration", "filesystem_managed_artifact", "backup_required", ["artifact_ids", "planned_destinations"]),
        operation("autostart_reconcile", "existing::autostart.reconcile", "target_adapter", "hydration", "launchd", "backup_required", ["artifact_ids", "planned_destinations"]),
        operation("plugin_cache_refresh", "existing::runtime.plugin_cache_refresh", "target_adapter", "runtime_reconcile", "plugin_cache", "reversible", []),
    ]


def bundle_document(baseline: Release, *, artifacts: list[dict[str, Any]] | None = None, operations: list[dict[str, Any]] | None = None, strategy: str = "router_preferred", knowledge_removals: list[str] | None = None) -> dict[str, Any]:
    return {
        "flow_id": "existing-install",
        "schema_version": 1,
        "supported_predecessors": [
            {
                "repository": CANONICAL,
                "commit": baseline.commit,
                "tree": baseline.tree,
                "provenance_sha256": hashlib.sha256(baseline.provenance).hexdigest(),
                "seed_id": baseline.seed_id,
                "origin_id": ORIGIN_ID,
                "manifest_sha256": baseline.manifest,
                "legacy_anchor_id": None,
            }
        ],
        "source_operation": {"operation_ref": "existing::source.fast_forward", "runner": "manager", "planned_actions": ["manager.acquire_update_candidate", "target.fetch_exact_candidate", "target.fast_forward_exact_candidate"]},
        "runtime_operations": default_operations() if operations is None else operations,
        "managed_artifacts": default_artifacts() if artifacts is None else artifacts,
        "dependency_closure": {"additions": [], "removals": []},
        "knowledge_removals": [] if knowledge_removals is None else knowledge_removals,
        "lifecycle": {"strategy": strategy, "readiness_budget_seconds": 5, "readiness_release_signal": "bridge_health_healthy", "verification_modules": ["ananta.cli"]},
    }


def bundle_files(document: dict[str, Any]) -> dict[str, bytes]:
    return {
        "existing_install_flow.json": (json.dumps(document, indent=2, sort_keys=True) + "\n").encode(),
        "existing_install_flow.schema.json": (_REPO_KB / "existing_install_flow.schema.json").read_bytes(),
        "setup_adapter_envelope.schema.json": (_REPO_KB / "setup_adapter_envelope.schema.json").read_bytes(),
    }


def bundle_digest(files: dict[str, bytes]) -> str:
    digest = hashlib.sha256()
    for name in sorted(files):
        digest.update(name.encode())
        digest.update(b"\0")
        digest.update(files[name])
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


def candidate_tree_files(files: dict[str, bytes], *, extra: dict[str, str | bytes] | None = None) -> dict[str, str | bytes]:
    tree: dict[str, str | bytes] = {f"{KB}/{name}": content for name, content in files.items()}
    for name in ("launchagent.plist.template", "zshrc_block.template", "user_claude_md_section.template"):
        tree[f"{TEMPLATES}/{name}"] = template_bytes(name)
    tree[ADAPTER_MODULE] = (_ROOT / ADAPTER_MODULE).read_bytes()
    for name in HOOK_FILES:
        tree[f"{HOOKS}/{name}"] = (_ROOT / HOOKS / name).read_bytes()
    tree["README.md"] = "release n+1\n"
    tree["docs/new.txt"] = "new\n"
    if extra:
        tree.update(extra)
    return tree


def descriptor(candidate: Release, contract: str) -> bytes:
    value = {
        "schema_version": 3,
        "channel_id": "stable",
        "repository": CANONICAL,
        "release_tag": candidate.tag,
        "commit": candidate.commit,
        "tree_hash": candidate.tree,
        "archive_sha256": "f" * 64,
        "profile": "macos-bizops",
        "provenance": {
            "schema_version": 1,
            "provenance_sha256": hashlib.sha256(candidate.provenance).hexdigest(),
            "seed_id": candidate.seed_id,
            "origin_id": ORIGIN_ID,
            "manifest_sha256": candidate.manifest,
            "bundle_name": "macos-bizops",
            "platform": "local",
            "source_commit": candidate.source_commit,
            "source_date": "2026-09-16T00:00:00+00:00",
        },
        "existing_install_contract": {"flow_id": "existing-install", "flow_schema_version": 1, "bundle_digest": contract},
        "allowed_repository_migrations": [],
    }
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode()


def installed(candidate: Release, raw: bytes, contract: str) -> InstalledUpdateDescriptor:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    identity = ChannelInspectionIdentity(
        "stable",
        CANONICAL,
        candidate.tag,
        candidate.commit,
        candidate.tree,
        "macos-bizops",
        hashlib.sha256(candidate.provenance).hexdigest(),
        candidate.seed_id,
        ORIGIN_ID,
        candidate.manifest,
        ExistingInstallContractIdentity("existing-install", 1, contract),
        "catalog",
        "1" * 64,
        "seed",
        digest[7:],
        digest,
        "anchors",
        "2" * 64,
    )
    metadata = InstalledInspectionMetadata(identity, seed_lock_from_fields(parse_seed_lock_bytes(raw)), ())
    return InstalledUpdateDescriptor(metadata, raw)


def enroll(paths: ManagerPaths, target: Path, baseline: Release, *, descriptor_digest: str, contract: str, router: bool = False, truthful: bool = False, descriptor_bytes: bytes | None = None) -> InstanceInventoryRecordV2:
    """Write the v2 row.  ``truthful=True`` (Step 6, G14) shapes it exactly as ``_finalize_import`` leaves it:
    a real cached inspection bundle, an import journal at ``verified``, and ``last_verified_*`` naming that import."""
    target_stat, parent_stat = target.stat(), target.parent.stat()
    now = "2026-09-18T00:00:00Z"
    record = InstanceInventoryRecordV2(
        "ins_" + hashlib.sha256(str(target).encode()).hexdigest()[:32],
        "fixture",
        TargetIdentity(str(target), FilesystemIdentity(target_stat.st_dev, target_stat.st_ino), FilesystemIdentity(parent_stat.st_dev, parent_stat.st_ino)),
        ManagementOrigin.IMPORT,
        ManagementState.DIAGNOSTIC,
        UpdateEligibility(UpdateEligibilityState.AVAILABLE, ()),
        ServiceIdentity(
            str(target / ".venv/bin/solet"),
            str(target / ".venv/bin/solet-bridge"),
            str(target.parent / "bin/fixture"),
            None,
            None,
            str(target / "profile"),
            "local.solet.fixture",
            "local.solet.fixture.router" if router else None,
            str(target.parent / "router.sock") if router else None,
        ),
        ChannelIdentity("stable", descriptor_digest, CANONICAL),
        ObservedProvenanceIdentity("strict", "sha256:" + hashlib.sha256(baseline.provenance).hexdigest(), baseline.seed_id, ORIGIN_ID, "sha256:" + baseline.manifest, None),
        ReleaseIdentity(CANONICAL, baseline.commit, baseline.tree, baseline.tag),
        None,
        None,
        ContractIdentities(contract, None, None, None, None),
        "sha256:" + "4" * 64,
        None,
        "opr_" + "5" * 32,
        now,
        now,
        now,
        now,
    )
    if truthful:
        record = _truthful_row(paths, record, cast(bytes, descriptor_bytes))
    write_maintenance_inventory_v2(paths.maintenance_inventory_path, (record,))
    return record


def _truthful_row(paths: ManagerPaths, record: InstanceInventoryRecordV2, descriptor_bytes: bytes) -> InstanceInventoryRecordV2:
    from dataclasses import replace  # noqa: PLC0415

    from solet_manager.import_enrollment import _NON_TOUCH, compute_import_key, compute_operation_id  # noqa: PLC0415
    from solet_manager.maintenance_journal import append_maintenance_attempt, create_import_maintenance_operation, maintenance_evidence, transition_maintenance_operation, write_maintenance_operation  # noqa: PLC0415
    from solet_manager.state_io import write_content_addressed_json  # noqa: PLC0415
    from solet_manager.update_execution import UpdateRequest, enrolled_metadata, inspect_with_metadata  # noqa: PLC0415

    from solet_setup_contracts import canonical_sha256  # noqa: PLC0415

    write_maintenance_inventory_v2(paths.maintenance_inventory_path, (record,))
    request = UpdateRequest(record.name, paths)
    inspection = inspect_with_metadata(request, record, enrolled_metadata(record, parse_seed_lock_bytes(descriptor_bytes)))
    result = inspection.to_command_result()
    bundle: dict[str, JsonValue] = {
        "schema_version": 1,
        "kind": "existing_install_import_inspection",
        "request": {"name": record.name, "canonical_target": record.target.canonical_path, "channel_id": "stable"},
        "target_identity": result.data["target"],
        "channel_identity": result.data["channel"],
        "facts": result.data["source"],
        "classification": result.data["classification"],
        "checks": result.data["checks"],
        "service_identity": {"launchagent_label": record.service_identity.launchagent_label},
        "preservation": result.data["preservation"],
    }
    digest = canonical_sha256(bundle)
    write_content_addressed_json(paths.contract_cache_path(digest), bundle, digest)
    identity = inspection.target_identity
    key = compute_import_key(target_device=identity.target_device, target_inode=identity.target_inode, canonical_target=str(identity.canonical_display), solet_name=record.name, committed_provenance_seed_id=record.observed_provenance.seed_id, committed_head=record.source_release.commit)
    operation_id = compute_operation_id(key)
    fingerprint = canonical_sha256([digest, record.instance_id, operation_id, list(_NON_TOUCH)])
    document = create_import_maintenance_operation(
        operation_id=operation_id, instance_id=record.instance_id, idempotency_key=key, name=record.name, canonical_target=record.target.canonical_path,
        target_device=identity.target_device, target_inode=identity.target_inode, channel_id="stable", provenance_seed_id=record.observed_provenance.seed_id,
        head_commit=record.source_release.commit, head_tree=record.source_release.tree, inspection_bundle_digest=digest, diagnostic_contract_digest=record.contract_identities.diagnostic_contract_digest,
        approval_fingerprint=fingerprint, manager_write_paths=(str(paths.maintenance_inventory_path),), non_touch_surfaces=_NON_TOUCH,
    )
    path = paths.operation_path(record.instance_id, operation_id)
    write_maintenance_operation(path, None, document)
    current = document
    for stage_id, status in (("inspection_bundle_cached", "bundle_cached"), ("inventory_published", "inventory_published"), ("enrollment_verified", "verified")):
        attempt = len(cast(list[JsonValue], current["attempts"]))
        nxt = append_maintenance_attempt(current, stage_id=stage_id, status="verified", evidence=(maintenance_evidence(operation_id, attempt, "state", stage_id, fingerprint),))
        nxt = transition_maintenance_operation(nxt, status=status, result={"kind": "imported", "inventory_instance_id": record.instance_id} if status == "verified" else None)
        write_maintenance_operation(path, current, nxt)
        current = nxt
    return replace(record, inspection_bundle_digest=digest, last_verified_operation_id=operation_id)


def _target_path(root: Path, router: bool) -> Path:
    """Where the fixture clone lives.  A router install launches as a materialized supervisor: its plist names ``releases/current``
    (``derive_launch_topology``), and only that shape can attest, so router cutover applies to it and never to the direct launch
    ``solet create`` writes (iss_6fd900ab)."""
    return root / "releases" / "current" / "target" if router else root / "target"


def build_fixture(root: Path, *, document: Callable[[Release], dict[str, Any]] | None = None, router: bool = False, roster: tuple[str, ...] = ("github_midwife_plugin",), legacy_plist: bool = True, host: FakeHost | None = None, extra_candidate_files: dict[str, str | bytes] | None = None, truthful: bool = False, at_candidate: bool = False, baseline_extra: dict[str, str | bytes] | None = None, candidate_removals: tuple[str, ...] = ()) -> Fixture:
    """``at_candidate=True`` (Step 6, F-ZD-1) clones the target already AT the candidate release and enrols it there."""
    root.mkdir(parents=True, exist_ok=True)
    source = root / "source"
    source.mkdir()
    git(source, "init", "--quiet", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    baseline_tree: dict[str, str | bytes] = {"README.md": "release n\n", f"{KB}/setup_adapter_envelope.schema.json": (_REPO_KB / "setup_adapter_envelope.schema.json").read_bytes()}
    for name in ("zshrc_block.template", "user_claude_md_section.template", "launchagent.plist.template"):
        baseline_tree[f"{TEMPLATES}/{name}"] = template_bytes(name)
    baseline_tree[ADAPTER_MODULE] = (_ROOT / ADAPTER_MODULE).read_bytes()
    if baseline_extra:
        baseline_tree.update(baseline_extra)
    baseline = seal(source, "a" * 40, "b" * 64, "r1", baseline_tree)
    document_value = bundle_document(baseline) if document is None else document(baseline)
    files = bundle_files(document_value)
    contract = bundle_digest(files)
    for removed in candidate_removals:
        (source / removed).unlink()
    candidate = seal(source, "c" * 40, "d" * 64, "r2", candidate_tree_files(files, extra=extra_candidate_files))
    target = _target_path(root, router)
    _clone(source, target)
    enrolled = candidate if at_candidate else baseline
    git(target, "checkout", "--quiet", "-B", "main", enrolled.commit)
    git(target, "remote", "set-url", "origin", CANONICAL)
    _write_ignored(target, roster)
    home = root / "home"
    (home / "Library" / "LaunchAgents").mkdir(parents=True)
    cache = home / ".claude" / "plugins" / "cache" / "fixture" / "coordination-hooks" / "1.0.0"
    (cache / "hooks").mkdir(parents=True)
    for name in HOOK_FILES:
        (cache / "hooks" / name).write_bytes((_ROOT / HOOKS / name).read_bytes())
    (home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({"plugins": {"coordination-hooks@fixture": [{"installPath": str(cache)}]}}), encoding="utf-8")
    fake_host = FakeHost() if host is None else host
    if legacy_plist:
        from github_midwife_plugin.autostart import render_launchagent_plist  # noqa: PLC0415

        (home / "Library" / "LaunchAgents" / "local.solet.fixture.plist").write_bytes(render_launchagent_plist("fixture", target, home, template_text=None, stamp=None, stamped=False))
    paths = ManagerPaths(root / "manager" / "config", root / "manager" / "state", root / "manager" / "cache")
    for directory in (paths.config_dir, paths.state_dir, paths.cache_dir):
        directory.mkdir(parents=True, mode=0o700)
    raw = descriptor(candidate, contract)
    installed_descriptor = installed(candidate, raw, contract)
    digest = installed_descriptor.metadata.channel_identity.descriptor_digest
    enroll(paths, target, enrolled, descriptor_digest=digest, contract=contract, router=router, truthful=truthful, descriptor_bytes=raw)
    request = UpdateRequest("fixture", paths, descriptor_loader=lambda channel, tracker: installed_descriptor, transport_url=str(source), runtime_seams=make_seams(home, fake_host, target))
    return Fixture(root, source, target, home, paths, baseline, candidate, contract, digest, request, fake_host)


def _clone(source: Path, target: Path) -> None:
    """Clone the fixture seed; a clone that fails once (a transient host-side git error) is retried with its stderr surfaced."""
    for attempt in (1, 2):
        completed = subprocess.run(("git", "clone", "--quiet", str(source), str(target)), check=False, capture_output=True, text=True, env=_ENV)
        if completed.returncode == 0:
            return
        if target.exists():
            import shutil  # noqa: PLC0415

            shutil.rmtree(target)
        if attempt == 2:
            raise AssertionError(f"fixture clone failed twice: {completed.stderr.strip()}")


def _write_ignored(target: Path, roster: tuple[str, ...]) -> None:
    """Genesis-shaped ignored state: a venv marker, a preserved roster, a runtime dir; all ``!!`` never ``??``."""
    exclude = target / ".git" / "info" / "exclude"
    exclude.write_text(".venv/\nprofile/config/manifest.yaml\nprofile/data/\n", encoding="utf-8")
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / ".venv" / "bin" / "python3").write_text("fixture\n")
    (target / ".venv" / "bin" / "solet-bridge").write_text("fixture\n")
    # Step 7 section 7.1: the doctor's ``instance_python``/``instance_bridge_cli`` rows read the venv marker and the exec bit.
    (target / ".venv" / "pyvenv.cfg").write_text("home = /fixture\nversion = 3.13.0\n")
    (target / ".venv" / "bin" / "solet-bridge").chmod(0o755)
    (target / "profile" / "config").mkdir(parents=True, exist_ok=True)
    (target / "profile" / "config" / "manifest.yaml").write_text("plugins:\n" + "".join(f"- {plugin}\n" for plugin in roster), encoding="utf-8")
    assert git(target, "status", "--porcelain") == ""


def advance_to_source_advanced(fixture: Fixture) -> str:
    """Run the real Step-4 preview and apply; returns the source approval fingerprint."""
    preview = preview_update_instance(fixture.request)
    assert preview.status == "preview_ready", preview
    fingerprint = cast(str, preview.data["approval_fingerprint"])
    applied = apply_update(fixture.request, fingerprint)
    assert applied.status == "source_advanced", applied
    return fingerprint


def runtime_fingerprint(fixture: Fixture) -> str:
    preview = preview_update_instance(fixture.request)
    assert preview.status == "runtime_preview_ready", (preview.status, preview.error_kind, preview.data.get("blocked"), preview.data.get("lifecycle"))
    return cast(str, preview.data["runtime_approval_fingerprint"])


@contextmanager
def db_spy() -> Generator[list[str]]:
    """Fail loud on any database access across the caller's whole run (design section 11)."""
    seen: list[str] = []

    class _Poison(ModuleType):
        def __getattr__(self, name: str) -> object:
            import traceback  # noqa: PLC0415

            frames = "".join(traceback.format_stack(limit=60)[:-1])
            seen.append(f"{self.__name__}.{name}\n{frames}")
            raise AssertionError(f"database access attempted: {self.__name__}.{name}")

    saved = {name: sys.modules.get(name) for name in ("psycopg", "asyncpg", "ananta.interfaces.state_management_interface")}
    for name in saved:
        sys.modules[name] = _Poison(name)
    try:
        yield seen
    finally:
        _restore_modules(saved)
        forbidden = [name for name in sys.modules if name.startswith("ananta.") and "state_management" in name and not isinstance(sys.modules[name], _Poison)]
        assert not forbidden, forbidden
        assert not seen, seen


def _restore_modules(saved: dict[str, ModuleType | None]) -> None:
    for name, module in saved.items():
        if module is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = module


def expect(kind: type[Exception], action: Callable[[], object], label: str) -> Exception:
    try:
        action()
    except kind as exc:
        return exc
    raise AssertionError(label)


def data(value: JsonValue, key: str) -> JsonValue:
    assert isinstance(value, dict), value
    return value[key]
