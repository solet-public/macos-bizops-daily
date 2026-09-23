"""Hermetic acceptance smoke for the frozen stdlib bootstrap adapter."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any

import jsonschema
from bootstrap_adapter_hba_oracle_support import check_pg_hba_scram_oracle
from bootstrap_adapter_lm_studio_contract_support import check_lm_studio_routes
from bootstrap_adapter_operation_contract_support import (
    check_absolute_homebrew_fallback,
    check_homebrew_install_route,
    check_python_install_route,
)
from bootstrap_adapter_physical_memory_contract_support import (
    check_physical_memory_guard,
    check_python_guard,
)
from bootstrap_adapter_plan_contract_support import check_homebrew_operation_plan

_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP = _ROOT / "bootstrap.py"
_SCHEMA = json.loads(
    (
        _ROOT / "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json"
    ).read_text()
)
_CHECKS = 0
_REVISION = "a" * 40
_FINGERPRINT = "sha256:" + "b" * 64
_NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)


class SmokeFailureError(AssertionError):
    """One contract discriminator failed."""


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise SmokeFailureError(label)


def _module() -> ModuleType:
    os.environ.setdefault("SOLET_NAME", "adaptertest")
    spec = importlib.util.spec_from_file_location("_bootstrap_adapter_under_test", _BOOTSTRAP)
    if spec is None or spec.loader is None:
        raise SmokeFailureError("bootstrap import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    if str(_ROOT) not in sys.path:
        sys.path.insert(0, str(_ROOT))
    spec.loader.exec_module(module)
    return module


def _completed(
    code: int = 0, stdout: str = "", stderr: str = ""
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout, stderr)


def _request(
    target: Path,
    *,
    operation_id: str = "build_instance_environment",
    operation_ref: str = "bootstrap::environment.ensure_dependency_closure",
    phase: str = "probe",
    purpose: str | None = "preview",
    approval: str | None = None,
    public_inputs: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": "11111111-1111-4111-8111-111111111111",
        "operation_id": operation_id,
        "operation_ref": operation_ref,
        "phase": phase,
        "probe_purpose": purpose,
        "attempt": 1,
        "name": "adaptertest",
        "target": str(target),
        "flow_id": "macos.repository_setup",
        "flow_source_revision": _REVISION,
        "answers_fingerprint": _FINGERPRINT,
        "approval_fingerprint": approval,
        "dry_run": phase == "probe",
        "timeout_seconds": 30,
        "public_inputs": public_inputs or {},
    }


def _execute(
    module: ModuleType, request: dict[str, Any], runner: Any, which: Any, **kwargs: Any
) -> dict[str, Any]:
    result = module.execute_adapter_request(
        request,
        runner=runner,
        which=which,
        now=lambda: _NOW,
        **kwargs,
    )
    jsonschema.Draft7Validator(_SCHEMA).validate(result)
    return result


def _prepare_target(module: ModuleType, target: Path, *, cli: bool = True) -> None:
    for _distribution, relative in module._REQUIRED_DISTRIBUTIONS:  # noqa: SLF001
        (target / relative).mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin/python3").write_text("fixture")
    if cli:
        (target / ".venv/bin/solet-bridge").write_text("fixture")
    (target / ".venv/pyvenv.cfg").write_text("home = /stable/python\n")


def _closure_payload(module: ModuleType, target: Path, scenario: str) -> dict[str, Any]:
    packages = {
        distribution: {
            "version": "1.0.0",
            "direct_url": json.dumps(
                {"url": (target / relative).resolve().as_uri(), "dir_info": {"editable": True}}
            ),
        }
        for distribution, relative in module._REQUIRED_DISTRIBUTIONS  # noqa: SLF001
    }
    if scenario == "missing_package":
        packages.pop("github_midwife_plugin")
    return {
        "pip": scenario not in {"python_only"},
        "build_backend": scenario not in {"python_only", "pip_only", "missing_backend"},
        "wheel": scenario not in {"python_only", "pip_only", "missing_backend"},
        "packages": packages,
    }


def _closure_runner(module: ModuleType, target: Path, scenario: str, calls: list[list[str]]) -> Any:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if "-I" in command and "-c" in command:
            if "packages =" not in command[-1]:
                if scenario == "dangling":
                    return _completed(1)
                return _completed(stdout="3.13.7\n")
            if scenario == "dangling":
                return _completed(1)
            return _completed(stdout=json.dumps(_closure_payload(module, target, scenario)))
        if command[-1:] == ["--version"] and Path(command[0]).name == "solet-bridge":
            return _completed(stdout="solet-bridge 0.1.0\n")
        return _completed()

    return run


def _check_closure_matrix(module: ModuleType, root: Path) -> None:
    for scenario in (
        "empty",
        "python_only",
        "pip_only",
        "missing_backend",
        "missing_package",
        "missing_cli",
        "dangling",
        "closed",
    ):
        target = root / scenario
        if scenario != "empty":
            _prepare_target(module, target, cli=scenario != "missing_cli")
        calls: list[list[str]] = []
        result = _execute(
            module,
            _request(target),
            _closure_runner(module, target, scenario, calls),
            lambda _name: None,
        )
        expected = "verified" if scenario == "closed" else "pending"
        _check(result["checkpoint_status"] == expected, f"{scenario} closure status")
        _check(
            bool(result["planned_actions"]) is (scenario != "closed"),
            f"{scenario} planned action parity",
        )
        if scenario == "closed":
            _check(
                all("pip" not in call and "venv" not in call for call in calls),
                "closed probe performs no mutation command",
            )


def _check_dependency_closure_install_order(module: ModuleType, root: Path) -> None:
    """Contracts must install before ananta resolves its declared dependency."""
    target = root / "contracts-before-ananta"
    _prepare_target(module, target)
    base = root / "stable-python3.13"
    base.write_text("fixture")
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(command))
        if command == [str(base), "--version"]:
            return _completed(stdout="Python 3.13.7\n")
        return _completed()

    apply = _request(target, phase="apply", purpose=None, approval=_FINGERPRINT)
    result = _execute(module, apply, run, lambda _name: None, base_python=str(base))
    _check(result["checkpoint_status"] == "applied", "contracts-order apply succeeds")
    editable_installs = [call[-1] for call in calls if "pip" in call and "-e" in call]
    contracts_index = next(
        (
            index
            for index, package in enumerate(editable_installs)
            if package.endswith("/solet_setup_contracts")
        ),
        None,
    )
    ananta_index = next(
        (index for index, package in enumerate(editable_installs) if package.endswith("/ananta")),
        None,
    )
    _check(
        contracts_index is not None,
        "contracts install is present (red: remove contracts from required distributions)",
    )
    _check(ananta_index is not None, "ananta install is present")
    _check(
        isinstance(contracts_index, int)
        and isinstance(ananta_index, int)
        and contracts_index < ananta_index,
        "contracts install precedes ananta (red: remove contracts from required distributions)",
    )


def _check_dependency_closure_failure_detail(module: ModuleType, root: Path) -> None:
    """Apply failures retain bounded command evidence and the existing repair instruction."""
    target = root / "failure-detail"
    _prepare_target(module, target)
    base = root / "stable-python3.13"
    base.write_text("fixture")
    marker = "dependency-closure-known-stderr"

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [str(base), "--version"]:
            return _completed(stdout="Python 3.13.7\n")
        if "pip" in command and "setuptools" in command:
            return _completed(17, stderr=marker)
        return _completed()

    apply = _request(target, phase="apply", purpose=None, approval=_FINGERPRINT)
    failed = _execute(module, apply, run, lambda _name: None, base_python=str(base))
    repair = failed["repair"]
    _check(failed["checkpoint_status"] == "failed", "failure-detail apply reports failed")
    _check(
        failed["error_kind"] == "dependency_closure_apply_failed",
        "failure-detail retains dependency-closure failure kind",
    )
    _check(
        isinstance(repair, str) and "exit 17" in repair and marker in repair,
        "failure-detail carries exit and stderr marker (red: restore generic repair)",
    )
    _check(
        isinstance(repair, str)
        and repair.endswith("Repair the target package or interpreter condition and resume."),
        "failure-detail retains original repair instruction",
    )


def _check_interrupted_resume(module: ModuleType, root: Path) -> None:
    target = root / "resume"
    for _distribution, relative in module._REQUIRED_DISTRIBUTIONS:  # noqa: SLF001
        (target / relative).mkdir(parents=True, exist_ok=True)
    (target / ".venv/bin").mkdir(parents=True)
    (target / ".venv/bin/python3").write_text("partial")
    base = root / "stable-python3.13"
    base.write_text("fixture")
    first_calls: list[list[str]] = []

    def first(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        first_calls.append(list(command))
        if command == [str(base), "--version"]:
            return _completed(stdout="Python 3.13.7\n")
        if "pip" in command and "setuptools" in command:
            return _completed(1)
        return _completed()

    apply = _request(target, phase="apply", purpose=None, approval=_FINGERPRINT)
    failed = _execute(module, apply, first, lambda _name: None, base_python=str(base))
    _check(failed["checkpoint_status"] == "failed", "interruption after venv creation is failed")
    _check(
        any("--upgrade" in call and "venv" in call for call in first_calls),
        "partial venv repair re-enters deterministic venv upgrade",
    )
    second_calls: list[list[str]] = []

    def second(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        second_calls.append(list(command))
        if command == [str(base), "--version"]:
            return _completed(stdout="Python 3.13.7\n")
        return _completed()

    applied = _execute(module, apply, second, lambda _name: None, base_python=str(base))
    _check(applied["checkpoint_status"] == "applied", "partial venv resume applies")
    _prepare_target(module, target)
    closed = _execute(
        module,
        _request(target, purpose="post_apply"),
        _closure_runner(module, target, "closed", []),
        lambda _name: None,
    )
    _check(closed["checkpoint_status"] == "verified", "resumed closure verifies on post-probe")


def _git_runner(expected: str, *, wrong: bool) -> Any:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if "HEAD^{commit}" in joined:
            return _completed(stdout=("c" * 40 if wrong else expected) + "\n")
        if "main^{commit}" in joined:
            return _completed(stdout=expected + "\n")
        if "HEAD^{tree}" in joined:
            return _completed(stdout="d" * 40 + "\n")
        if "ls-files" in joined:
            return _completed(stdout="bootstrap.py\nflow\nschema\n")
        return _completed(stdout="")

    return run


def _check_stage_probe(module: ModuleType, root: Path) -> None:
    request = _request(
        root,
        operation_id="git_checkout_valid",
        operation_ref="setup::git.verify_checkout",
        purpose="stage_exit",
    )
    wrong = _execute(
        module, request, _git_runner(_REVISION, wrong=True), lambda name: "/fixture/git" if name == "git" else None
    )
    _check(wrong["error_kind"] == "source_identity_mismatch", "wrong peeled commit fails closed")
    _check(
        not wrong["planned_actions"] and not wrong["discovered_candidates"],
        "stage output arrays empty",
    )
    right = _execute(
        module, request, _git_runner(_REVISION, wrong=False), lambda name: "/fixture/git" if name == "git" else None
    )
    _check(right["checkpoint_status"] == "verified", "right checkout stage probe verifies")
    invalid_apply = dict(
        request, phase="apply", probe_purpose=None, dry_run=False, approval_fingerprint=_FINGERPRINT
    )
    refused = _execute(
        module,
        invalid_apply,
        _git_runner(_REVISION, wrong=False),
        lambda name: "/fixture/git" if name == "git" else None,
    )
    _check(refused["error_kind"] == "adapter_protocol_error", "stage probe apply is refused")


def _postgres_runner(prefix: Path, *, role: bool = False, database: bool = False) -> Any:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if Path(command[0]).name == "brew" and command[1:2] == ["--prefix"]:
            return _completed(stdout=str(prefix) + "\n")
        if Path(command[0]).name == "brew" and command[1:2] == ["list"]:
            return _completed(stdout="postgresql@17\npgvector\n")
        if command[1:] == ["--version"] and Path(command[0]).name == "psql":
            return _completed(stdout="psql (PostgreSQL) 17.2\n")
        if Path(command[0]).name == "pg_isready":
            return _completed()
        if "pg_available_extensions" in joined:
            return _completed(stdout="1\n")
        if "pg_roles" in joined:
            return _completed(stdout="1\n" if role else "")
        if "pg_database" in joined:
            return _completed(stdout="1\n" if database else "")
        return _completed()

    return run


def _check_postgres_plans(module: ModuleType, root: Path) -> None:
    def missing_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        return _completed(stdout="" if "list" in command else "")

    missing = _execute(
        module,
        _request(
            root,
            operation_id="install_postgresql",
            operation_ref="bootstrap::postgres.install",
        ),
        missing_runner,
        lambda name: "/fixture/brew" if name == "brew" else None,
    )
    missing_ids = {item["id"] for item in missing["planned_actions"]}
    _check(
        missing_ids
        == {
            "postgres.install_homebrew_formula",
            "postgres.start_homebrew_service",
            "postgres.install_pgvector_formula",
        },
        "PostgreSQL install preview schedules pgvector when a stopped service leaves discovery unknown",
    )
    _check(
        all(
            str(item["target"]).startswith("/fixture/brew:")
            for item in missing["planned_actions"]
        ),
        "PostgreSQL preview binds the resolved Homebrew executable into approved actions",
    )
    prefix = root / "brew"
    hba = prefix / "var/postgresql@17/pg_hba.conf"
    hba.parent.mkdir(parents=True)
    hba.write_text(
        "local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n"
    )
    hba.chmod(0o600)
    configure_request = _request(
        root,
        operation_id="configure_postgresql",
        operation_ref="bootstrap::postgres.configure_solet",
        public_inputs={"solet_name": "adaptertest"},
    )
    configure = _execute(
        module,
        configure_request,
        _postgres_runner(prefix),
        lambda name: f"/fixture/{name}",
    )
    configure_ids = {item["id"] for item in configure["planned_actions"]}
    _check(
        configure_ids
        == {
            "postgres.create_role",
            "postgres.create_database",
            "postgres.create_schema",
            "postgres.activate_pgvector",
            "postgres.revoke_public_connect",
            "postgres.edit_pg_hba_scram",
            "postgres.reload_configuration",
        },
        "PostgreSQL configure preview enumerates role/database/schema/HBA actions",
    )
    inconsistent = _execute(
        module,
        configure_request,
        _postgres_runner(prefix, role=True, database=False),
        lambda name: f"/fixture/{name}",
    )
    _check(
        inconsistent["error_kind"] == "postgres_role_database_inconsistent",
        "inconsistent role/database state fails closed",
    )
    hba.chmod(0o666)
    unsafe = _execute(
        module,
        configure_request,
        _postgres_runner(prefix),
        lambda name: f"/fixture/{name}",
    )
    _check(unsafe["error_kind"] == "postgres_hba_unsafe_file", "unsafe pg_hba mode fails closed")


def _check_postgres_keg_resolution(module: ModuleType, root: Path) -> None:
    """Keg-only PostgreSQL 17 must work without an interactive PATH mutation."""

    from bootstrap_adapter.models import AdapterRuntime, RolePolicyObservation
    from bootstrap_adapter.postgres import apply_postgres_configuration

    request = _request(
        root,
        operation_id="install_postgresql",
        operation_ref="bootstrap::postgres.install",
    )
    prefix = root / "postgresql-17-keg"
    keg_bin = prefix / "bin"
    keg_binaries = {
        str(keg_bin / name) for name in ("psql", "pg_isready", "createuser", "createdb")
    }
    keg_calls: list[list[str]] = []

    def keg_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        keg_calls.append(list(command))
        joined = " ".join(command)
        if Path(command[0]).name == "brew" and command[1:] == ["list", "--formula"]:
            return _completed(stdout="postgresql@17\npgvector\n")
        if Path(command[0]).name == "brew" and command[1:] == ["--prefix", "postgresql@17"]:
            return _completed(stdout=f"{prefix}\n")
        if command == [str(keg_bin / "psql"), "--version"]:
            return _completed(stdout="psql (PostgreSQL) 17.10\n")
        if command[:1] == [str(keg_bin / "pg_isready")]:
            return _completed()
        if "pg_available_extensions" in joined:
            return _completed(stdout="1\n")
        return _completed()

    def keg_which(name: str) -> str | None:
        if name == "brew":
            return "/fixture/brew"
        return name if name in keg_binaries else None

    keg_only = _execute(module, request, keg_runner, keg_which)
    _check(
        keg_only["checkpoint_status"] == "verified",
        "keg-only PostgreSQL 17 verifies through its brew prefix",
    )
    _check(
        ["/fixture/brew", "--prefix", "postgresql@17"] in keg_calls,
        "keg-only PostgreSQL resolves the supported formula prefix",
    )

    runtime = AdapterRuntime(
        run=keg_runner,
        which=keg_which,
        now=lambda: _NOW,
        name="adaptertest",
        target=root,
    )
    policy = RolePolicyObservation(
        role_exists=False,
        database_exists=False,
        role_safe=None,
        database_owner_matches=None,
        schema_exists=False,
        schema_owner_matches=None,
        public_connect_revoked=False,
        vector_installed=False,
        hba_path=None,
        hba_safe=True,
        hba_layout_recognized=True,
        scram_present=True,
    )
    apply_postgres_configuration(
        runtime,
        policy,
        [
            {"id": "postgres.create_role"},
            {"id": "postgres.create_schema"},
            {"id": "postgres.activate_pgvector"},
            {"id": "postgres.revoke_public_connect"},
            {"id": "postgres.reload_configuration"},
        ],
    )
    apply_commands = keg_calls[-6:]
    _check(
        {command[0] for command in apply_commands} <= keg_binaries,
        "all PostgreSQL apply commands use resolved absolute keg paths",
    )

    linked_calls: list[list[str]] = []

    def linked_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        linked_calls.append(list(command))
        joined = " ".join(command)
        if Path(command[0]).name == "brew" and command[1:] == ["list", "--formula"]:
            return _completed(stdout="postgresql\npgvector\n")
        if command[1:] == ["--version"] and Path(command[0]).name == "psql":
            return _completed(stdout="psql (PostgreSQL) 17.10\n")
        if Path(command[0]).name == "pg_isready":
            return _completed()
        if "pg_available_extensions" in joined:
            return _completed(stdout="1\n")
        return _completed()

    linked = _execute(
        module,
        request,
        linked_runner,
        lambda name: f"/fixture/{name}" if name in {"brew", "psql", "pg_isready"} else None,
    )
    _check(
        linked["checkpoint_status"] == "verified",
        "linked PostgreSQL remains usable without a matching versioned keg",
    )
    _check(
        ["/fixture/brew", "--prefix", "postgresql"] in linked_calls,
        "linked PostgreSQL considers its installed unversioned formula",
    )
    _check(
        ["/fixture/psql", "--version"] in linked_calls,
        "linked PostgreSQL falls back to its working PATH binary",
    )

    absent = _execute(
        module,
        _request(
            root,
            operation_id="postgres_binary_version_valid",
            operation_ref="bootstrap::postgres.probe_version",
        ),
        lambda command, **_kwargs: _completed(stdout=""),
        lambda name: "/fixture/brew" if name == "brew" else None,
    )
    _check(absent["checkpoint_status"] == "blocked", "absent PostgreSQL remains unavailable")

    newer_prefix = root / "postgresql-18-keg"
    newer_bin = newer_prefix / "bin"
    newer_binaries = {
        str(newer_bin / name) for name in ("psql", "pg_isready", "createuser", "createdb")
    }

    def newer_runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if Path(command[0]).name == "brew" and command[1:] == ["list", "--formula"]:
            return _completed(stdout="postgresql@18\npgvector\n")
        if Path(command[0]).name == "brew" and command[1:] == ["--prefix", "postgresql@18"]:
            return _completed(stdout=f"{newer_prefix}\n")
        if command == [str(newer_bin / "psql"), "--version"]:
            return _completed(stdout="psql (PostgreSQL) 18.1\n")
        if command[:1] == [str(newer_bin / "pg_isready")]:
            return _completed()
        if "pg_available_extensions" in joined:
            return _completed(stdout="1\n")
        return _completed()

    newer = _execute(
        module,
        request,
        newer_runner,
        lambda name: (
            "/fixture/brew" if name == "brew" else name if name in newer_binaries else None
        ),
    )
    _check(
        newer["error_kind"] == "postgres_wrong_major",
        "keg-only PostgreSQL 18 reports its real incompatible major",
    )

    both_calls: list[list[str]] = []

    def both_runner(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        both_calls.append(list(command))
        if Path(command[0]).name == "brew" and command[1:] == ["list", "--formula"]:
            return _completed(stdout="postgresql@17\npostgresql@18\npgvector\n")
        return keg_runner(command, **kwargs)

    both = _execute(module, request, both_runner, keg_which)
    _check(
        both["checkpoint_status"] == "verified",
        "the supported PostgreSQL 17 keg wins when 17 and 18 are both installed",
    )
    _check(
        ["/fixture/brew", "--prefix", "postgresql@17"] in both_calls
        and ["/fixture/brew", "--prefix", "postgresql@18"] not in both_calls,
        "multiple installed PostgreSQL kegs select the supported major first",
    )


def _check_pgvector_failure_kinds(module: ModuleType, root: Path) -> None:
    request = _request(
        root,
        operation_id="pgvector_ready",
        operation_ref="bootstrap::postgres.probe_pgvector",
    )
    prefix = root / "pgvector-brew"

    service_outage = _execute(
        module,
        request,
        _postgres_runner(prefix),
        lambda name: None if Path(name).name == "pg_isready" else f"/fixture/{name}",
    )
    _check(service_outage["checkpoint_status"] == "blocked", "pgvector service outage blocks")
    _check(
        service_outage["error_kind"] == "postgres_service_not_ready",
        "pgvector service outage retains postgres service error kind",
    )

    def missing_extension(
        command: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        completed = _postgres_runner(prefix)(command)
        if "pg_extension" in " ".join(command):
            return _completed(stdout="0\n")
        return completed

    missing = _execute(
        module,
        request,
        missing_extension,
        lambda name: f"/fixture/{name}",
    )
    _check(missing["checkpoint_status"] == "blocked", "ready PostgreSQL without pgvector blocks")
    _check(
        missing["error_kind"] == "postgres_probe_not_verified",
        "pgvector extension miss retains probe-not-verified error kind",
    )


def _check_protocol_and_stdout(module: ModuleType, root: Path) -> None:
    unknown = _request(
        root, operation_id="unknown_operation", operation_ref="bootstrap::unknown.route"
    )
    result = _execute(module, unknown, lambda *_a, **_k: _completed(), lambda _name: None)
    _check(result["error_kind"] == "adapter_missing", "unknown operation fails closed")
    missing = _request(root, phase="apply", purpose=None, approval=None)
    refused = _execute(module, missing, lambda *_a, **_k: _completed(), lambda _name: None)
    _check(refused["error_kind"] == "adapter_protocol_error", "missing approval is refused")
    malformed = _request(root, phase="apply", purpose=None, approval="sha256:not-valid")
    refused = _execute(module, malformed, lambda *_a, **_k: _completed(), lambda _name: None)
    _check(refused["error_kind"] == "adapter_protocol_error", "malformed approval is refused")
    secret = _request(
        root,
        operation_id="configure_postgresql",
        operation_ref="bootstrap::postgres.configure_solet",
        public_inputs={"password": "sentinel-secret-value"},
    )
    secret_result = _execute(module, secret, lambda *_a, **_k: _completed(), lambda _name: None)
    encoded = json.dumps(secret_result)
    _check(
        "sentinel-secret-value" not in encoded and "password" not in encoded,
        "secret never enters output",
    )
    python_request = _request(
        root,
        operation_id="python_version_valid",
        operation_ref="bootstrap::python.probe_version",
        purpose="stage_exit",
    )
    completed = subprocess.run(
        [sys.executable, str(_BOOTSTRAP), "--operation-adapter"],
        input=json.dumps(python_request),
        capture_output=True,
        text=True,
        timeout=15,
        env={key: value for key, value in os.environ.items() if key != "SOLET_NAME"},
    )
    lines = completed.stdout.splitlines()
    _check(
        completed.returncode == 0 and len(lines) == 1, "adapter stdout is exactly one JSON result"
    )
    parsed = json.loads(lines[0])
    jsonschema.Draft7Validator(_SCHEMA).validate(parsed)
    _check(not completed.stderr, "valid adapter request emits no stderr diagnostics")
    _check(
        parsed["checkpoint_status"] == "verified",
        "adapter entrypoint resolves its running Python 3.13 interpreter (red: select runtime.base_python only)",
    )
    _check(
        parsed["evidence"][0]["observed"].startswith("Python 3.13"),
        "adapter entrypoint records the running interpreter version (red: select runtime.base_python only)",
    )


def main() -> int:
    module = _module()
    try:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            _check_closure_matrix(module, root)
            _check_dependency_closure_install_order(module, root)
            _check_dependency_closure_failure_detail(module, root)
            _check_interrupted_resume(module, root)
            check_python_guard(
                module=module,
                root=root,
                request_factory=_request,
                execute=_execute,
                completed=_completed,
                check=_check,
            )
            check_physical_memory_guard(
                module=module,
                root=root,
                request_factory=_request,
                execute=_execute,
                completed=_completed,
                check=_check,
            )
            check_lm_studio_routes(
                module=module,
                root=root,
                repository_root=_ROOT,
                request_factory=_request,
                execute=_execute,
                check=_check,
            )
            check_python_install_route(
                module=module,
                root=root,
                request_factory=_request,
                execute=_execute,
                completed=_completed,
                check=_check,
                fingerprint=_FINGERPRINT,
            )
            _check_stage_probe(module, root)
            check_homebrew_install_route(
                module=module,
                root=root,
                request_factory=_request,
                execute=_execute,
                completed=_completed,
                check=_check,
                fingerprint=_FINGERPRINT,
            )
            check_absolute_homebrew_fallback(
                module=module,
                root=root,
                request_factory=_request,
                execute=_execute,
                completed=_completed,
                check=_check,
            )
            check_homebrew_operation_plan(root=root, check=_check)
            _check_postgres_plans(module, root)
            check_pg_hba_scram_oracle(root, check=_check, now=_NOW)
            _check_postgres_keg_resolution(module, root)
            _check_pgvector_failure_kinds(module, root)
            _check_protocol_and_stdout(module, root)
    except (SmokeFailureError, jsonschema.ValidationError) as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"bootstrap_adapter_contract_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
