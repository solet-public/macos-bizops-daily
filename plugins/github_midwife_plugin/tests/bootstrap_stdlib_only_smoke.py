"""Slice E smoke — `bootstrap.py` (repo-root Layer 0) is STRICTLY stdlib,
and its 7-step sequence dry-runs correctly with subprocess/network/confirm
all mocked.

`bootstrap.py` is not part of any installable package (it runs BEFORE the
venv exists), so this smoke loads it directly from its repo-root path via
`importlib.util` rather than a normal `import` statement.

Checks:
  1. A subprocess-driven AST scan proves bootstrap.py imports only stdlib
     module names (the literal build-spec wording: "runs python3.13 -c
     'import ast; assert no third-party imports'"), PLUS a negative
     control (a fabricated `import requests` line IS flagged by the same
     scan) proving the check isn't vacuously true.
  2. A full happy-path dry run: every dependency reports already-healthy,
     so every step (except the terminal handoff) reports "skipped", and
     the whole sequence completes -- zero real brew/psql/pip/network/
     filesystem-outside-tmp calls (subprocess.run, the confirm callback,
     and the HTTP getter are all injected fakes).
  3. A "needs_user_action" state (Homebrew absent) stops the sequence
     immediately -- no later step is attempted (stop-and-ask, not a
     failure, and never auto-piping an installer).
  4. A hard failure (a mocked install command exits non-zero) is caught
     as a "failed" step record, not an uncaught exception, and the
     sequence stops there too.
  5. Every formerly escaping standalone-bootstrap failure is converted to a
     typed failed step record, and a real re-run resumes at that step.

Run directly: ``.venv/bin/python3
plugins/github_midwife_plugin/tests/bootstrap_stdlib_only_smoke.py``.
"""

from __future__ import annotations

import contextlib
import getpass
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Generator
from datetime import datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BOOTSTRAP_PATH = _REPO_ROOT / "bootstrap.py"
_BOOTSTRAP_ADAPTER_PATHS = tuple(sorted((_REPO_ROOT / "bootstrap_adapter").glob("*.py")))

_CHECKS_RUN: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised on any check failure; message is the failure detail."""


def _check(label: str, condition: bool, detail: str = "") -> None:
    _CHECKS_RUN.append(label)
    if not condition:
        raise SmokeFailureError(f"{label}: {detail}")


def _load_bootstrap_module() -> ModuleType:
    if not _BOOTSTRAP_PATH.is_file():
        raise SmokeFailureError(f"bootstrap.py not found at {_BOOTSTRAP_PATH}")
    module_name = "_bootstrap_under_test"
    if module_name in sys.modules:
        return sys.modules[module_name]
    spec = importlib.util.spec_from_file_location(module_name, _BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise SmokeFailureError(f"could not build an import spec for {_BOOTSTRAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    # dataclasses (bootstrap.py uses `from __future__ import annotations`,
    # so its annotations are lazy strings) resolves types via
    # sys.modules[cls.__module__] -- the module MUST be registered before
    # exec_module runs, or @dataclass crashes with an AttributeError on
    # a None module lookup.
    sys.modules[module_name] = module
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    spec.loader.exec_module(module)
    return module


_fresh_load_counter = [0]


def _load_bootstrap_module_fresh() -> ModuleType:
    """Load a FRESH copy of bootstrap.py (unique module name, never cached) so
    a test can observe its module-level constants under a patched environment
    (`_load_bootstrap_module` caches, which would freeze the first-seen value).
    """
    _fresh_load_counter[0] += 1
    module_name = f"_bootstrap_fresh_{_fresh_load_counter[0]}"
    spec = importlib.util.spec_from_file_location(module_name, _BOOTSTRAP_PATH)
    if spec is None or spec.loader is None:
        raise SmokeFailureError(f"could not build an import spec for {_BOOTSTRAP_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module  # dataclasses needs it registered before exec
    if str(_REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(_REPO_ROOT))
    spec.loader.exec_module(module)
    return module


def _check_admin_role_is_dynamic_getuser() -> None:
    """RED-FIRST (operator-identity parameterization, 2026-07-11): _ADMIN_ROLE
    must resolve to getpass.getuser() at import time, NOT a hardcoded operator
    username ('dw'). Proven by loading bootstrap.py fresh with getpass.getuser
    patched to a sentinel: a dynamically-sourced constant picks the sentinel
    up; the pre-fix hardcoded `_ADMIN_ROLE = "dw"` ignores the patch (RED). The
    machine-independent form (never assert a literal username) that stays green
    even where getpass.getuser() happens to equal 'dw'.
    """
    sentinel = "smoke_admin_role_sentinel_not_a_real_user"
    with patch.object(getpass, "getuser", return_value=sentinel):
        patched_module = _load_bootstrap_module_fresh()
    _check(
        "bootstrap _ADMIN_ROLE is dynamically sourced from getpass.getuser() (not a hardcoded username)",
        patched_module._ADMIN_ROLE == sentinel,  # noqa: SLF001
        f"got {patched_module._ADMIN_ROLE!r}; a hardcoded admin role would ignore the patched getuser()",  # noqa: SLF001
    )
    # House-rule regression guard: the real resolved admin role equals
    # getpass.getuser() computed HERE, and is non-empty (never a literal).
    real_user = getpass.getuser()
    real_module = _load_bootstrap_module_fresh()
    _check(
        "the resolved admin role equals getpass.getuser() and is non-empty",
        bool(real_user) and real_module._ADMIN_ROLE == real_user,  # noqa: SLF001
        f"got {real_module._ADMIN_ROLE!r} vs getpass.getuser()={real_user!r}",  # noqa: SLF001
    )


def _check_database_consumes_solet_name() -> None:
    """RED-FIRST (per-solet-db, 2026-07-11): _DATABASE must resolve to
    SOLET_NAME at import (each solet's database is named after it), NOT
    a hardcoded literal name. Loading bootstrap fresh with SOLET_NAME patched
    to a sentinel: a name-consuming constant picks it up; the pre-fix hardcoded
    _DATABASE literal would ignore it (RED). Also proves the scram lines are the
    ALL-DATABASES form (decoupled from the db name) -- a per-db line would leave
    the NEXT solet's db un-gated.
    """
    sentinel = "smoke_solet_db_sentinel"
    with patch.dict(os.environ, {"SOLET_NAME": sentinel}):
        mod = _load_bootstrap_module_fresh()
    _check(
        "bootstrap _DATABASE consumes SOLET_NAME (not a hardcoded db name)",
        mod._DATABASE == sentinel,  # noqa: SLF001
        f"got {mod._DATABASE!r}",  # noqa: SLF001
    )
    _check(
        "the default-scram block is the ALL-DATABASES form, decoupled from the db AND role name",
        all(" all " in line for line in mod._DEFAULT_SCRAM_LINES)  # noqa: SLF001
        and not any(sentinel in line for line in mod._DEFAULT_SCRAM_LINES),  # noqa: SLF001
        f"got {mod._DEFAULT_SCRAM_LINES!r}",  # noqa: SLF001
    )


def _check_missing_solet_name_fails_loud() -> None:
    """bootstrap CONSUMES SOLET_NAME fail-loud (2026-07-11): loading it with
    SOLET_NAME unset must raise -- a silent default would create a mis-named
    database the newborn's state plugin never connects to.
    """
    saved = os.environ.pop("SOLET_NAME", None)
    try:
        try:
            _load_bootstrap_module_fresh()
        except RuntimeError as exc:
            _check(
                "loading bootstrap without SOLET_NAME fails loud",
                "SOLET_NAME" in str(exc),
                str(exc),
            )
        else:
            raise SmokeFailureError("missing-solet-name: bootstrap did not fail loud")
    finally:
        if saved is not None:
            os.environ["SOLET_NAME"] = saved


_AST_SCAN_SCRIPT = """
import ast, sys
mods = set()
for path in sys.argv[1:]:
    tree = ast.parse(open(path).read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                mods.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                mods.add(node.module.split(".")[0])
allowed_local = {"bootstrap_adapter"}
non_stdlib = sorted(m for m in mods if m not in sys.stdlib_module_names | allowed_local)
if non_stdlib:
    print("NON_STDLIB:" + ",".join(non_stdlib))
    sys.exit(1)
print("STDLIB_ONLY")
sys.exit(0)
"""


def _check_bootstrap_is_stdlib_only() -> None:
    expected = {
        "__init__.py",
        "models.py",
        "protocol.py",
        "dependency.py",
        "homebrew.py",
        "lm_studio.py",
        "postgres.py",
        "postgres_install.py",
        "routes.py",
    }
    _check(
        "stdlib scan covers the complete authorized bootstrap_adapter package",
        {path.name for path in _BOOTSTRAP_ADAPTER_PATHS} == expected,
        f"scanned={[path.name for path in _BOOTSTRAP_ADAPTER_PATHS]!r}",
    )
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _AST_SCAN_SCRIPT,
            str(_BOOTSTRAP_PATH),
            *(str(path) for path in _BOOTSTRAP_ADAPTER_PATHS),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    _check(
        "bootstrap.py imports only stdlib modules",
        result.returncode == 0 and "STDLIB_ONLY" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )


def _check_ast_scan_catches_a_real_third_party_import(root: Path) -> None:
    """Negative control: prove the scan script actually flags a
    non-stdlib import, so check 1's pass isn't vacuous.
    """
    poisoned = root / "poisoned_bootstrap.py"
    poisoned.write_text(_BOOTSTRAP_PATH.read_text() + "\nimport requests\n")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            _AST_SCAN_SCRIPT,
            str(poisoned),
            *(str(path) for path in _BOOTSTRAP_ADAPTER_PATHS),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    _check(
        "the AST scan flags a fabricated third-party import (negative control)",
        result.returncode == 1 and "requests" in result.stdout,
        f"stdout={result.stdout!r} stderr={result.stderr!r}",
    )


def _fake_completed(returncode: int, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


def _make_all_healthy_fake_run(
    fake_prefix: Path,
    datacl_text: str = "{owner=CTc/owner}",
    vector_installed: bool = True,
) -> Any:
    """`datacl_text` is what the R4 datacl probe sees: the default is a
    revoked-style ACL (owner-only, no PUBLIC aclitem) so the all-healthy
    fixture stays fully skipped; pass "" (NULL datacl -> Postgres built-in
    default ACL, PUBLIC holds CONNECT/TEMP) or a PUBLIC-bearing ACL to drive
    the reconciled-db-missing-revoke path. `vector_installed` is what the D12
    `pg_extension` probe sees (per-database activation, distinct from the
    machine-wide `pg_available_extensions` brew-install probe below); pass
    False to drive the reconciled-db-missing-vector-extension path."""

    def _fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd[-1:] == ["--version"] and Path(cmd[0]).name.startswith("python"):
            return _fake_completed(0, stdout="Python 3.13.7\n")
        if cmd[-1:] == ["--version"] and Path(cmd[0]).name == "solet-bridge":
            return _fake_completed(0, stdout="solet-bridge 0.1.0\n")
        if "-I" in cmd and "-c" in cmd and ".venv/bin/python3" in cmd[0]:
            if "packages =" not in cmd[-1]:
                return _fake_completed(0, stdout="3.13.7\n")
            target = Path(cmd[0]).parents[2]
            packages: dict[str, dict[str, str]] = {}
            for distribution, relative in _load_bootstrap_module()._REQUIRED_DISTRIBUTIONS:  # noqa: SLF001
                packages[distribution] = {
                    "version": "1.0.0",
                    "direct_url": json.dumps(
                        {
                            "url": (target / relative).resolve().as_uri(),
                            "dir_info": {"editable": True},
                        },
                    ),
                }
            return _fake_completed(
                0,
                stdout=json.dumps(
                    {
                        "pip": True,
                        "build_backend": True,
                        "wheel": True,
                        "packages": packages,
                    },
                ),
            )
        if cmd[-1:] == ["--version"] and Path(cmd[0]).name == "brew":
            return _fake_completed(0, stdout="Homebrew 4.4.0\n")
        if cmd[1:] == ["--prefix"]:
            return _fake_completed(0, stdout=f"{fake_prefix}\n")
        if cmd[1:] == ["--prefix", "postgresql@17"]:
            return _fake_completed(0, stdout=f"{fake_prefix}\n")
        if cmd[1:] == ["list", "--formula"]:
            return _fake_completed(0, stdout="postgresql@17\npgvector\n")
        if cmd[1:] == ["--version"] and Path(cmd[0]).name == "psql":
            return _fake_completed(0, stdout="psql (PostgreSQL) 17.2\n")
        if Path(cmd[0]).name == "pg_isready":
            return _fake_completed(0)
        if "pg_available_extensions" in " ".join(cmd):
            return _fake_completed(0, stdout="1\n")
        # D12: per-database activation probe (pg_extension), distinct from the
        # machine-wide pg_available_extensions branch above.
        if "pg_extension" in " ".join(cmd):
            return _fake_completed(0, stdout="1\n" if vector_installed else "")
        # ORDER MATTERS: the datacl probe's SQL also names pg_database, so this
        # branch must come before the bare pg_database existence branch.
        if "datacl" in " ".join(cmd):
            return _fake_completed(0, stdout=f"{datacl_text}\n")
        if "pg_roles" in " ".join(cmd):
            return _fake_completed(0, stdout="1\n")
        if "pg_database" in " ".join(cmd):
            return _fake_completed(0, stdout="1\n")
        return _fake_completed(0)

    return _fake_run


def _fake_http_get_correct_model(_url: str, _timeout: int) -> bytes:
    return b'{"data": [{"id": "text-embedding-nomic-embed-text-v1.5-embedding"}]}'


def _make_fixture_tree(root: Path) -> tuple[Path, Path]:
    """Returns (target, fake_brew_prefix). `target` looks like a real
    clone (has an existing venv, so venv_and_seed also reports skipped);
    `fake_brew_prefix/var/postgresql@17/pg_hba.conf` already carries the
    scram lines so role_and_db also reports skipped.
    """
    target = root / "clone"
    for _distribution, relative in _load_bootstrap_module()._REQUIRED_DISTRIBUTIONS:  # noqa: SLF001
        (target / relative).mkdir(parents=True, exist_ok=True)
    (target / ".venv" / "bin").mkdir(parents=True)
    (target / ".venv" / "bin" / "python3").write_text("#!/bin/sh\n")
    (target / ".venv" / "bin" / "solet-bridge").write_text("#!/bin/sh\n")
    (target / ".venv" / "pyvenv.cfg").write_text("home = /opt/homebrew/bin\n")

    fake_prefix = root / "homebrew"
    pg_hba = fake_prefix / "var" / "postgresql@17" / "pg_hba.conf"
    pg_hba.parent.mkdir(parents=True)
    fake_bin = fake_prefix / "bin"
    fake_bin.mkdir()
    for executable in ("psql", "pg_isready", "createuser", "createdb"):
        path = fake_bin / executable
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)
    module = _load_bootstrap_module()
    pg_hba.write_text("\n".join(module._DEFAULT_SCRAM_LINES) + "\n# trust block below\n")  # noqa: SLF001
    return target, fake_prefix


# The shared resolver reaches `shutil.which` for PATH candidates and for its
# keg paths. Its runner validates Homebrew with `--version`; the fixture must
# make both seams hermetic rather than relying on the minting host.
_HOST_PROBES = ("brew", "psql", "pg_isready")


@contextlib.contextmanager
def _isolated_host(module: ModuleType, *, brew_present: bool = True) -> Generator[None]:
    """Present every host probe as healthy (optionally with brew absent)."""
    real_which = module.shutil.which

    def fake(name: str) -> str | None:
        if name == "brew":
            return f"/opt/fake/bin/{name}" if brew_present else None
        if name in _HOST_PROBES:
            return f"/opt/fake/bin/{name}"
        if Path(name).is_absolute():
            return name
        return real_which(name)

    module.shutil.which = fake
    try:
        yield
    finally:
        module.shutil.which = real_which


def _quietly(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a bootstrap step with its command echo contained.

    `bootstrap._print_command` echoes each command, and the psql commands carry
    `getpass.getuser()` as the admin role. That is CORRECT in the product — the
    identity is derived at runtime, never a literal, so the shipped file holds no
    operator identity — but it put the running user's name into this smoke's
    console output and therefore into gate logs. Contained here rather than masked
    in `bootstrap.py`, which would degrade the adopter-facing transparency the echo
    exists to provide.
    """
    with contextlib.redirect_stdout(io.StringIO()):
        return fn(*args, **kwargs)


def _run_steps_isolated(module: ModuleType, ctx: Any, *, brew_present: bool = True) -> list[dict[str, Any]]:
    """Drive the step sequence with the HOST fully isolated, and its echo contained.

    Two things this closes, both of which leaked the ambient machine into a smoke
    that documents itself as running with "subprocess/network/confirm all mocked":

    1. `probe_homebrew()` reaches `shutil.which('brew')` directly — not through
       `ctx.run` — so no mock intercepted it and the happy path silently required
       the MINTING MACHINE to have Homebrew. It failed on any machine without it,
       while the product was behaving exactly as designed (absent Homebrew is a
       `needs_user_action` stop-and-ask). The brew-ABSENT leg below already patched
       this and said why in a comment; the patch was simply never applied to the
       leg that needs brew PRESENT.
    2. `bootstrap._print_command` echoes each command, and the role it prints is
       `getpass.getuser()`. That is correct in the product — the identity is DERIVED
       at runtime, never a literal, so the shipped file carries no operator identity
       — but it means this smoke's CONSOLE OUTPUT carried the running user's name
       into gate logs. Captured here rather than masked in `bootstrap.py`, which
       would degrade the adopter-facing transparency that echo exists for.
    """
    with (
        _isolated_host(module, brew_present=brew_present),
        contextlib.redirect_stdout(io.StringIO()),
    ):
        return module.run_steps(ctx)


def _check_happy_path_dry_run(root: Path) -> None:
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)

    ctx = module.BootstrapContext(
        target=target,
        run=_make_all_healthy_fake_run(fake_prefix),
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    steps = _run_steps_isolated(module, ctx)

    _check(
        "happy-path dry run executes all 7 steps in order",
        [s["step_name"] for s in steps]
        == [
            "homebrew",
            "postgres",
            "pgvector",
            "role_and_db",
            "lm_server",
            "venv_and_seed",
            "handoff",
        ],
        f"got {[s['step_name'] for s in steps]!r}",
    )
    _check(
        "every already-healthy step reports skipped",
        all(s["status"] == "skipped" for s in steps[:-1]),
        f"got {[(s['step_name'], s['status']) for s in steps]!r}",
    )
    _check(
        "the terminal handoff step completes",
        steps[-1]["status"] == "completed",
        f"got {steps[-1]!r}",
    )


def _check_homebrew_absent_stops_immediately(root: Path) -> None:
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)

    def _fake_run_no_brew(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if Path(cmd[0]).name == "brew":
            raise FileNotFoundError("brew not found")
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_fake_run_no_brew,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    # Same isolation as the happy path, brew forced ABSENT. Previously this leg
    # carried the only copy of the which() patch; the helper now shares it so the
    # two legs cannot drift apart again.
    steps = _run_steps_isolated(module, ctx, brew_present=False)

    _check(
        "Homebrew absence stops the sequence at step 1 -- no later step attempted",
        [s["step_name"] for s in steps] == ["homebrew"] and steps[0]["status"] == "needs_user_action",
        f"got {steps!r}",
    )


def _check_keg_only_postgres_matches_adapter(root: Path) -> None:
    """Layer 0 and the adapter must agree when Postgres is keg-only.

    RED before this repair: Layer 0's bare ``shutil.which('psql')`` returned
    None here while the adapter's keg resolver reported ready PostgreSQL 17.
    """

    module = _load_bootstrap_module()
    from bootstrap_adapter.models import AdapterRuntime
    from bootstrap_adapter.postgres import postgres_observation

    target, fake_prefix = _make_fixture_tree(root)
    recorded: list[list[str]] = []

    def fake_which(name: str) -> str | None:
        if name == "brew":
            return "/opt/fake/bin/brew"
        return name if Path(name).is_absolute() else None

    def recording_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(cmd)
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    previous_which = module.shutil.which
    module.shutil.which = fake_which
    try:
        context = module.BootstrapContext(
            target=target,
            run=recording_run,
            confirm=lambda _message: True,
            http_get=_fake_http_get_correct_model,
        )
        layer_zero_state, layer_zero_detail = module.probe_postgres(context)
    finally:
        module.shutil.which = previous_which

    adapter = postgres_observation(
        AdapterRuntime(recording_run, fake_which, datetime.now, "keg_only_smoke", target),
    )
    _check(
        "keg-only PostgreSQL 17 is healthy in both Layer 0 and the adapter",
        layer_zero_state is module.PostgresState.RUNNING_HEALTHY_COMPATIBLE and adapter.psql_present and adapter.major == 17 and adapter.ready,
        f"layer_zero=({layer_zero_state.value!r}, {layer_zero_detail!r}) adapter={adapter!r}",
    )
    expected_bin = str(fake_prefix / "bin")
    postgres_commands = [command for command in recorded if Path(command[0]).name in {"psql", "pg_isready"}]
    _check(
        "keg-only Layer 0 and adapter commands use the resolved absolute PostgreSQL bin directory",
        bool(postgres_commands) and all(command[0].startswith(expected_bin + "/") for command in postgres_commands),
        f"commands={postgres_commands!r}",
    )


def _check_homebrew_standard_prefix_when_path_missing(root: Path) -> None:
    """Validated standard Homebrew paths work without a launch-shell PATH entry."""

    module = _load_bootstrap_module()
    target, _fake_prefix = _make_fixture_tree(root)

    def fake_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if cmd == ["/opt/homebrew/bin/brew", "--version"]:
            return _fake_completed(0, stdout="Homebrew 4.4.0\n")
        raise FileNotFoundError(cmd[0])

    context = module.BootstrapContext(target, fake_run, lambda _message: True)
    previous_which = module.shutil.which
    module.shutil.which = lambda _name: None
    try:
        state = module.probe_homebrew(context)
    finally:
        module.shutil.which = previous_which
    _check(
        "Homebrew at its standard absolute prefix is present when launch PATH omits it",
        state is module.HomebrewState.PRESENT,
        state.value,
    )


def _check_missing_homebrew_is_named_not_file_not_found(root: Path) -> None:
    """Formerly bare brew calls surface a normal state when Homebrew is absent."""

    module = _load_bootstrap_module()
    target, _fake_prefix = _make_fixture_tree(root)

    def missing_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        raise FileNotFoundError(cmd[0])

    context = module.BootstrapContext(target, missing_run, lambda _message: True)
    previous_which = module.shutil.which
    module.shutil.which = lambda _name: None
    try:
        postgres_step = module.ensure_postgres(context)
        hba_path = module._pg_hba_path(context)  # noqa: SLF001
    finally:
        module.shutil.which = previous_which
    _check(
        "missing Homebrew produces a named postgres needs-user-action state",
        postgres_step["status"] == "needs_user_action" and "Homebrew executable" in str(postgres_step.get("detail")),
        f"step={postgres_step!r}",
    )
    _check(
        "the formerly unguarded Homebrew pg_hba probe returns no path instead of FileNotFoundError",
        hba_path is None,
        f"hba_path={hba_path!r}",
    )


def _check_hard_failure_is_caught_not_raised(root: Path) -> None:
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    # Force venv_and_seed to attempt real work (no pre-existing venv).
    (target / ".venv" / "bin" / "python3").unlink()
    (target / ".venv" / "bin" / "solet-bridge").unlink()
    (target / ".venv" / "bin").rmdir()
    (target / ".venv" / "pyvenv.cfg").unlink()
    (target / ".venv").rmdir()

    def _fake_run_venv_creation_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "venv" in cmd:
            return _fake_completed(1, stdout="simulated venv creation failure")
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_fake_run_venv_creation_fails,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    steps = _run_steps_isolated(module, ctx)

    _check(
        "a hard subprocess failure is caught as a 'failed' step record, not an uncaught exception",
        [s["step_name"] for s in steps]
        == [
            "homebrew",
            "postgres",
            "pgvector",
            "role_and_db",
            "lm_server",
            "venv_and_seed",
        ]
        and steps[-1]["status"] == "failed",
        f"got {steps!r}",
    )
    _check(
        "the sequence stops at the failure -- handoff never attempted",
        "handoff" not in [s["step_name"] for s in steps],
        f"got {[s['step_name'] for s in steps]!r}",
    )


def _check_role_creation_failure_names_homebrew_convention(root: Path) -> None:
    """The fail-loud diagnostic (operator-identity parameterization, 2026-07-11):
    when `createuser -U <resolved-admin>` fails (the resolved getpass.getuser()
    role is not a Postgres superuser -- the failure mode on a non-Homebrew or
    mis-initialized machine), the raised BootstrapError must NAME the Homebrew
    convention (superuser = OS login user) and the resolved admin role, so an
    operator on a fresh machine gets an actionable message rather than a bare
    exit code.
    """
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)

    def _fake_run_role_absent_createuser_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "pg_roles" in joined or "pg_database" in joined:
            return _fake_completed(0, stdout="")  # role/db ABSENT -> createuser is attempted
        if Path(cmd[0]).name == "createuser":
            return _fake_completed(1, stderr="simulated createuser failure")
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_fake_run_role_absent_createuser_fails,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    try:
        _quietly(module.ensure_role_and_db, ctx)
    except module.BootstrapError as exc:
        _check(
            "role-creation failure names the Homebrew superuser convention",
            "Homebrew" in str(exc),
            str(exc),
        )
        _check(
            "role-creation failure names the dynamically-resolved admin role (getpass.getuser())",
            getpass.getuser() in str(exc),
            f"{str(exc)!r} did not name resolved admin role {getpass.getuser()!r}",
        )
    else:
        raise SmokeFailureError("role-creation-failure-diagnostic: ensure_role_and_db did not raise")


def _check_handoff_failure_surfaces_stderr_fatal_text(root: Path) -> None:
    """Codex must-fix (README rider, 2026-07-09): genesis.py's main()
    prints its "FATAL: ..." diagnostic to STDERR, but the prior handoff()
    raise only ever included stdout's tail -- a genesis failure surfaced
    to the driving agent as a bare "exit <n>" with NO FATAL text at all.
    Codex's exact repro, inverted to green: a fake handoff subprocess
    with stderr='FATAL: genesis failed: sentinel failure', stdout='' --
    the raised BootstrapError message must contain that FATAL text.
    """
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    sentinel = "FATAL: genesis failed: sentinel failure"

    def _fake_run_handoff_fails(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if "github_midwife_plugin.genesis" in cmd:
            return _fake_completed(1, stdout="", stderr=sentinel)
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_fake_run_handoff_fails,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    try:
        module.handoff(ctx)
    except module.BootstrapError as exc:
        _check(
            "a handoff failure's raised BootstrapError contains genesis.py's stderr FATAL text",
            sentinel in str(exc),
            str(exc),
        )
    else:
        raise SmokeFailureError("handoff-failure-surfaces-stderr-fatal-text: handoff() did not raise")


def _completed_bootstrap_run(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    return _fake_completed(0)


def _check_pre_fix_scram_escape(safe_hba: Path) -> None:
    """Prove the old unguarded read/write shape escaped its OSError boundary."""

    with patch.object(Path, "read_text", side_effect=PermissionError("scram read sentinel")):
        try:
            original = safe_hba.read_text() if safe_hba.is_file() else ""
            safe_hba.write_text(original)
        except PermissionError as exc:
            pre_fix_scram_escape = exc
        else:
            raise SmokeFailureError("pre-fix scram writer did not reach the injected OSError boundary")
    _check(
        "pre-fix scram read OSError escaped the standalone writer",
        isinstance(pre_fix_scram_escape, PermissionError) and "scram read sentinel" in str(pre_fix_scram_escape),
        repr(pre_fix_scram_escape),
    )


def _escape_cases(module: ModuleType, safe_hba: Path, unrecognized_hba: Path) -> list[tuple[str, str, Any, Any]]:
    """Return the four real failure/retry pairs for the resume contract."""

    def scram_failure(context: Any) -> dict[str, Any]:
        with patch.object(Path, "read_text", side_effect=PermissionError("scram read sentinel")):
            module._write_default_scram_block(context, safe_hba)  # noqa: SLF001
        return {"step_name": "role_and_db", "status": "completed"}

    def scram_retry(context: Any) -> dict[str, Any]:
        with patch.object(module, "_postgres_command", return_value=["psql"]):
            module._write_default_scram_block(context, safe_hba)  # noqa: SLF001
        return {"step_name": "role_and_db", "status": "completed"}

    def handoff_failure(context: Any) -> dict[str, Any]:
        original_run = context.run
        context.run = lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("handoff sentinel"))
        try:
            return module.handoff(context)
        finally:
            context.run = original_run

    def handoff_retry(context: Any) -> dict[str, Any]:
        return module.handoff(context)

    def closure_failure(context: Any) -> dict[str, Any]:
        import bootstrap_adapter

        with patch.object(bootstrap_adapter, "probe_dependency_closure", return_value=(object(), {})):
            module._probe_dependency_closure(context)  # noqa: SLF001
        return {"step_name": "venv_and_seed", "status": "completed"}

    def closure_retry(context: Any) -> dict[str, Any]:
        import bootstrap_adapter

        with patch.object(
            bootstrap_adapter,
            "probe_dependency_closure",
            return_value=(bootstrap_adapter.ClosureState.PRESENT_CLOSED, {}),
        ):
            module._probe_dependency_closure(context)  # noqa: SLF001
        return {"step_name": "venv_and_seed", "status": "completed"}

    def layout_failure(context: Any) -> dict[str, Any]:
        module._write_default_scram_block(context, unrecognized_hba)  # noqa: SLF001
        return {"step_name": "role_and_db", "status": "completed"}

    def layout_retry(context: Any) -> dict[str, Any]:
        safe_hba.write_text(
            "local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n",
            encoding="utf-8",
        )
        with patch.object(module, "_postgres_command", return_value=["psql"]):
            module._write_default_scram_block(context, safe_hba)  # noqa: SLF001
        return {"step_name": "role_and_db", "status": "completed"}

    return [
        ("role_and_db", "unrecognized or unsafe", scram_failure, scram_retry),
        ("handoff", "handoff", handoff_failure, handoff_retry),
        ("venv_and_seed", "unrecognized dependency-closure state", closure_failure, closure_retry),
        ("role_and_db", "unrecognized or unsafe", layout_failure, layout_retry),
    ]


def _check_escape_case(
    module: ModuleType,
    target: Path,
    completed_hba: Path,
    step_name: str,
    expected_error: str,
    failing_runner: Any,
    retry_runner: Any,
) -> None:
    """Assert a typed record, then a fresh-context probe-and-rerun."""

    events: list[str] = []
    observed: list[BaseException] = []

    def prior_runner(_context: Any) -> dict[str, Any]:
        events.append("prior")
        return {
            "step_name": "prior",
            "status": "skipped" if module._scram_lines_present(completed_hba) else "completed",  # noqa: SLF001
        }

    def wrapped_failure(context: Any) -> dict[str, Any]:
        events.append("failure")
        try:
            return failing_runner(context)
        except module.BootstrapError as exc:
            observed.append(exc)
            raise

    def wrapped_retry(context: Any) -> dict[str, Any]:
        events.append("retry")
        return retry_runner(context)

    def later_runner(_context: Any) -> dict[str, Any]:
        events.append("later")
        return {"step_name": "later", "status": "completed"}

    failed_steps = module.run_steps(
        module.BootstrapContext(target, _completed_bootstrap_run, lambda _message: True),
        (("prior", prior_runner), (step_name, wrapped_failure), ("later", later_runner)),
    )
    _check(
        f"{step_name} injected failure reaches run_steps as BootstrapError",
        len(observed) == 1 and isinstance(observed[0], module.BootstrapError),
        f"observed={observed!r}",
    )
    _check(
        f"{step_name} writes the typed failure record and its cause",
        len(failed_steps) == 2 and failed_steps[-1].get("step_name") == step_name and failed_steps[-1].get("status") == "failed" and expected_error in str(failed_steps[-1].get("error")),
        f"got {failed_steps!r}",
    )
    _check_layout_remediation(expected_error, failed_steps[-1])
    rerun_steps = module.run_steps(
        module.BootstrapContext(target, _completed_bootstrap_run, lambda _message: True),
        (("prior", prior_runner), (step_name, wrapped_retry), ("later", later_runner)),
    )
    _check(
        f"{step_name} rerun probes completed work, skips it, and reattempts the failed step",
        events == ["prior", "failure", "prior", "retry", "later"] and rerun_steps[0].get("status") == "skipped" and [step["step_name"] for step in rerun_steps] == ["prior", step_name, "later"],
        f"events={events!r} steps={rerun_steps!r}",
    )


def _check_layout_remediation(expected_error: str, failed_step: dict[str, Any]) -> None:
    if expected_error != "unrecognized or unsafe":
        return
    error = str(failed_step.get("error"))
    _check(
        "unrecognized pg_hba.conf refusal names its remediation and preserves fail-closed behavior",
        "inspect the file" in error and "extend the recognizer or edit it manually" in error and "will not auto-write" in error,
        f"got {failed_step!r}",
    )


def _check_role_db_rerun_probe(module: ModuleType, target: Path, fake_prefix: Path) -> None:
    role_context = module.BootstrapContext(
        target,
        _make_all_healthy_fake_run(fake_prefix),
        lambda _message: True,
        _fake_http_get_correct_model,
    )
    _check(
        "rerun RoleDbState probe recognizes completed role and database work",
        module.probe_role_and_db(role_context) is module.RoleDbState.PRESENT_HEALTHY,
        "role/db probe did not report PRESENT_HEALTHY",
    )


def _check_resume_records_for_previously_escaping_failures(root: Path) -> None:
    """Each converted exception must write a typed record and permit rerun."""

    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    safe_hba = target / "safe_pg_hba.conf"
    safe_hba.write_text("local all all trust\nhost all all 127.0.0.1/32 trust\nhost all all ::1/128 trust\n", encoding="utf-8")
    unrecognized_hba = target / "unrecognized_pg_hba.conf"
    unrecognized_hba.write_text("local all all md5\n", encoding="utf-8")
    completed_hba = target / "completed_pg_hba.conf"
    completed_hba.write_text("\n".join(module._DEFAULT_SCRAM_LINES) + "\n", encoding="utf-8")  # noqa: SLF001
    _check_pre_fix_scram_escape(safe_hba)
    for case in _escape_cases(module, safe_hba, unrecognized_hba):
        _check_escape_case(module, target, completed_hba, *case)
    _check_role_db_rerun_probe(module, target, fake_prefix)


def _record_venv_seed_commands(root: Path) -> list[list[str]]:
    """Drive ensure_venv_and_seed on a venv-less fixture with a recording fake
    runner; assert completion and return the recorded command list."""
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    (target / ".venv" / "bin" / "python3").unlink()
    (target / ".venv" / "bin" / "solet-bridge").unlink()
    (target / ".venv" / "bin").rmdir()
    (target / ".venv" / "pyvenv.cfg").unlink()
    (target / ".venv").rmdir()

    recorded: list[list[str]] = []

    def _recording_run(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        recorded.append(list(cmd))
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_recording_run,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    step = _quietly(module.ensure_venv_and_seed, ctx)
    _check(
        "venv_and_seed completes under the recording fake",
        step["status"] == "completed",
        f"{step!r}",
    )
    return recorded


def _check_venv_seed_preinstalls_build_backend(root: Path) -> None:
    """Cold-agent acceptance findings F-4 + F-5 (2026-07-12): a stock py3.13
    venv ships pip only (editable installs died BackendUnavailable), and the
    seed plugin's pyproject pins macos_vault_plugin (pip resolved it against
    PyPI and died). ensure_venv_and_seed must run: venv creation → the
    pip/setuptools/wheel pre-install → editable installs of exactly
    solet_setup_contracts → ananta → macos_vault_plugin → github_midwife_plugin,
    in that order.
    """
    recorded = _record_venv_seed_commands(root)
    backend_idx = [i for i, c in enumerate(recorded) if "install" in c and c[-3:] == ["pip", "setuptools", "wheel"]]
    editable_idx = [i for i, c in enumerate(recorded) if "--no-build-isolation" in c]
    venv_idx = [i for i, c in enumerate(recorded) if c[1:3] == ["-m", "venv"]]

    counts_ok = (len(venv_idx), len(backend_idx), len(editable_idx)) == (1, 1, 5)
    _check(
        "exactly one venv creation, one build-backend pre-install, five editable installs",
        counts_ok,
        f"venv={venv_idx} backend={backend_idx} editable={editable_idx} recorded={recorded!r}",
    )
    _check(
        "ordering: venv creation → build-backend install → editable installs",
        venv_idx[0] < backend_idx[0] < min(editable_idx),
        f"venv={venv_idx} backend={backend_idx} editable={editable_idx}",
    )
    editable_targets = [recorded[i][-1] for i in editable_idx]
    expected_suffixes = (
        "/solet_setup_contracts",
        "/ananta",
        "/plugins/macos_vault_plugin",
        "/plugins/github_midwife_plugin",
        "/plugins/agent_messaging_plugin",
    )
    order_ok = all(t.endswith(s) for t, s in zip(editable_targets, expected_suffixes, strict=True))
    _check(
        "editable install order: ananta → macos_vault_plugin → github_midwife_plugin → agent_messaging_plugin",
        order_ok,
        f"targets={editable_targets!r}",
    )


def _check_confirm_interactive_eof_is_decline() -> None:
    """Cold-agent acceptance finding F-3 (2026-07-12): a non-interactive stdin
    (agent-driven run, no TTY) crashed confirm_interactive with an unhandled
    EOFError and a raw traceback. EOF must read as a DECLINE so the step
    surfaces its normal needs_user_action record instead.
    """
    module = _load_bootstrap_module()
    with patch("builtins.input", side_effect=EOFError):
        declined = module.confirm_interactive("fixture message")
    _check(
        "EOF on stdin reads as decline (False), never an unhandled crash",
        declined is False,
        f"got {declined!r}",
    )


def _check_confirm_interactive_assume_yes_env() -> None:
    """Agent-driven bootstrap needs a declared non-interactive approval path.
    SOLET_ASSUME_YES=1 should approve without reading stdin, unlike blind
    `yes |` piping which does not leave intent in the environment.
    """
    module = _load_bootstrap_module()
    with (
        patch.dict(os.environ, {"SOLET_ASSUME_YES": "1"}),
        patch("builtins.input", side_effect=SmokeFailureError("input should not be read")),
    ):
        accepted = module.confirm_interactive("fixture message")
    _check(
        "SOLET_ASSUME_YES=1 approves without reading stdin",
        accepted is True,
        f"got {accepted!r}",
    )


def _check_failed_step_summary_surfaces_error() -> None:
    """Finding F-6 (2026-07-12 cold run): a failed step record
    carries its reason under 'error' (run_steps' BootstrapError wrap), but the
    pre-fix summary chain read detail/state only -- venv_and_seed printed
    '[failed] venv_and_seed:' with the pip resolution error silently
    swallowed. The summary line must surface 'error' when 'detail' is absent.
    """
    module = _load_bootstrap_module()
    line = module._step_summary_line(  # noqa: SLF001
        {
            "step_name": "venv_and_seed",
            "status": "failed",
            "error": "sentinel pip resolution failure",
        }
    )
    _check(
        "a failed step's summary line carries its 'error' text",
        "sentinel pip resolution failure" in line and "[failed] venv_and_seed" in line,
        line,
    )


def _check_role_db_inconsistent_state_needs_user_action(root: Path) -> None:
    """Per-role isolation (2026-07-12): a role-present/db-absent (or vice-versa)
    state is a genuinely INCONSISTENT partial state, NOT the expected
    second-solet case any more. Under per-role isolation both the role AND
    the database are named after the solet, so a clean second solet on
    an already-provisioned machine is fully ABSENT (the normal create path) — a
    half-present state is an inconsistency. The detail must surface as
    needs_user_action naming the inconsistency and the RE-RUN instruction.
    """
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)

    def _fake_run_role_present_db_absent(cmd: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(cmd)
        if "pg_roles" in joined:
            return _fake_completed(0, stdout="1")
        if "pg_database" in joined:
            return _fake_completed(0, stdout="")
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_fake_run_role_present_db_absent,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    step = _quietly(module.ensure_role_and_db, ctx)
    detail = str(step.get("detail", ""))
    _check(
        "inconsistent role/db state surfaces as needs_user_action",
        step["status"] == "needs_user_action" and len(detail) > 0,
        f"{step!r}",
    )
    _check(
        "the detail names the inconsistent partial state AND the re-run instruction",
        "inconsistent partial state" in detail and "RE-RUN bootstrap.py" in detail,
        detail,
    )


def _check_reconciled_db_missing_revoke_applies_r4(root: Path) -> None:
    """RED-FIRST (cold-run finding D3, 2026-07-13): a PRESENT_HEALTHY
    role+db pair reached by MANUAL reconciliation (the runbook's stop-and-ask
    resolution) skipped the create-path entirely -- including its R4
    `REVOKE ... FROM PUBLIC` -- so a minimally-reconciled db shipped
    PUBLIC-connectable, silently violating the isolation invariant. The step
    must probe datacl and apply the revoke itself. Two PUBLIC-open shapes:
    NULL datacl (Postgres built-in default ACL) and an explicit PUBLIC aclitem.
    """
    module = _load_bootstrap_module()
    _target, fake_prefix = _make_fixture_tree(root)

    for datacl_text in ("", "{=Tc/owner,owner=CTc/owner}"):
        recorded: list[list[str]] = []

        # Loop-scoped values bound as default args (the B023-safe idiom) --
        # the closure is also invoked synchronously within the iteration, but
        # default-binding makes that safety structural instead of incidental.
        def _recording_run(
            cmd: list[str],
            _sink: list[list[str]] = recorded,
            _datacl: str = datacl_text,
            **_kwargs: object,
        ) -> subprocess.CompletedProcess[str]:
            _sink.append(cmd)
            return _make_all_healthy_fake_run(fake_prefix, datacl_text=_datacl)(cmd)

        ctx = module.BootstrapContext(
            target=_target,
            run=_recording_run,
            confirm=lambda _msg: True,
            http_get=_fake_http_get_correct_model,
        )
        step = _quietly(module.ensure_role_and_db, ctx)
        _check(
            f"PRESENT_HEALTHY with PUBLIC-open datacl {datacl_text!r} completes (not skipped)",
            step.get("status") == "completed",
            f"got {step!r}",
        )
        revokes = [c for c in recorded if any("REVOKE CONNECT, TEMP" in part for part in c)]
        _check(
            f"the R4 REVOKE is executed on the reconciled db (datacl {datacl_text!r})",
            len(revokes) == 1 and any(module._DATABASE in part for part in revokes[0]),  # noqa: SLF001
            f"recorded psql/create commands: {recorded!r}",
        )
        createuser_calls = [c for c in recorded if Path(c[0]).name == "createuser"]
        _check(
            f"no createuser/createdb re-run on the already-healthy pair (datacl {datacl_text!r})",
            not createuser_calls,
            f"got {createuser_calls!r}",
        )


def _check_reconciled_db_missing_vector_extension_applies_d12(root: Path) -> None:
    """RED-FIRST (cold-boot finding D12, 2026-07-13): a PRESENT_HEALTHY
    role+db pair whose database predates (or was reconciled without) the D12
    fix never has the `vector` extension CREATEd in it -- `ensure_pgvector`
    only confirms the extension's FILES are available machine-wide via brew,
    it never activates the extension in any specific database. Every
    macos_free_minimal schema declaring a vector column crash-loops at first
    boot without this. The step must probe `pg_extension` and apply
    `CREATE EXTENSION IF NOT EXISTS vector` itself.
    """
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    recorded: list[list[str]] = []

    def _recording_run(cmd: list[str], _sink: list[list[str]] = recorded, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _sink.append(cmd)
        return _make_all_healthy_fake_run(fake_prefix, vector_installed=False)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_recording_run,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    step = _quietly(module.ensure_role_and_db, ctx)
    _check(
        "PRESENT_HEALTHY with vector extension missing completes (not skipped)",
        step.get("status") == "completed",
        f"got {step!r}",
    )
    creates = [c for c in recorded if any("CREATE EXTENSION IF NOT EXISTS vector" in part for part in c)]
    _check(
        "the D12 CREATE EXTENSION is executed against this solet's own database",
        len(creates) == 1 and any(module._DATABASE in part for part in creates[0]),  # noqa: SLF001
        f"recorded psql/create commands: {recorded!r}",
    )
    createuser_calls = [c for c in recorded if Path(c[0]).name == "createuser"]
    _check(
        "no createuser/createdb re-run on the already-healthy pair",
        not createuser_calls,
        f"got {createuser_calls!r}",
    )


def _check_absent_role_db_create_path_activates_vector_extension(root: Path) -> None:
    """D12 create-path coverage: a fully-ABSENT role+db (the normal fresh-newborn
    path) must have `CREATE EXTENSION IF NOT EXISTS vector` in its create
    sequence -- a freshly createdb'd database never has any extension active,
    regardless of what's available machine-wide.
    """
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    recorded: list[list[str]] = []

    def _recording_run(cmd: list[str], _sink: list[list[str]] = recorded, **_kwargs: object) -> subprocess.CompletedProcess[str]:
        _sink.append(cmd)
        joined = " ".join(cmd)
        if "pg_roles" in joined or "pg_database" in joined:
            return _fake_completed(0, stdout="")  # role/db ABSENT -> the create path runs
        return _make_all_healthy_fake_run(fake_prefix)(cmd)

    ctx = module.BootstrapContext(
        target=target,
        run=_recording_run,
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    step = _quietly(module.ensure_role_and_db, ctx)
    _check(
        "ABSENT role/db create path completes",
        step.get("status") == "completed",
        f"got {step!r}",
    )
    creates = [c for c in recorded if any("CREATE EXTENSION IF NOT EXISTS vector" in part for part in c)]
    _check(
        "the D12 CREATE EXTENSION is part of the fresh-create sequence",
        len(creates) == 1 and any(module._DATABASE in part for part in creates[0]),  # noqa: SLF001
        f"recorded psql/create commands: {recorded!r}",
    )
    create_idx = next(i for i, c in enumerate(recorded) if Path(c[0]).name == "createdb")
    extension_idx = next(i for i, c in enumerate(recorded) if "CREATE EXTENSION IF NOT EXISTS vector" in " ".join(c))
    _check(
        "CREATE EXTENSION runs AFTER createdb (the database must exist first)",
        extension_idx > create_idx,
        f"createdb at {create_idx}, CREATE EXTENSION at {extension_idx}: {recorded!r}",
    )


def _check_fully_healthy_role_db_skips_with_revoke_verified(root: Path) -> None:
    """The all-healthy fixture (role+db present, scram lines present, datacl
    already revoked-style, vector extension already active) must report
    skipped -- and its state string names both the revoke and the vector
    extension so the audit trail shows they were VERIFIED, not assumed."""
    module = _load_bootstrap_module()
    target, fake_prefix = _make_fixture_tree(root)
    ctx = module.BootstrapContext(
        target=target,
        run=_make_all_healthy_fake_run(fake_prefix),
        confirm=lambda _msg: True,
        http_get=_fake_http_get_correct_model,
    )
    step = _quietly(module.ensure_role_and_db, ctx)
    _check(
        "fully-healthy role_and_db (incl. verified revoke and vector extension) reports skipped",
        step.get("status") == "skipped" and "revoke" in str(step.get("state", "")) and "vector" in str(step.get("state", "")),
        f"got {step!r}",
    )


def main() -> int:
    try:
        _check_bootstrap_is_stdlib_only()
        _check_admin_role_is_dynamic_getuser()
        _check_database_consumes_solet_name()
        _check_missing_solet_name_fails_loud()
        with tempfile.TemporaryDirectory() as tmp:
            _check_ast_scan_catches_a_real_third_party_import(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_happy_path_dry_run(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_homebrew_absent_stops_immediately(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_keg_only_postgres_matches_adapter(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_homebrew_standard_prefix_when_path_missing(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_missing_homebrew_is_named_not_file_not_found(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_hard_failure_is_caught_not_raised(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_role_creation_failure_names_homebrew_convention(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_handoff_failure_surfaces_stderr_fatal_text(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_resume_records_for_previously_escaping_failures(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_venv_seed_preinstalls_build_backend(Path(tmp))
        _check_confirm_interactive_eof_is_decline()
        _check_confirm_interactive_assume_yes_env()
        _check_failed_step_summary_surfaces_error()
        with tempfile.TemporaryDirectory() as tmp:
            _check_role_db_inconsistent_state_needs_user_action(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_reconciled_db_missing_revoke_applies_r4(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_reconciled_db_missing_vector_extension_applies_d12(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_absent_role_db_create_path_activates_vector_extension(Path(tmp))
        with tempfile.TemporaryDirectory() as tmp:
            _check_fully_healthy_role_db_skips_with_revoke_verified(Path(tmp))
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        print(f"  ({len(_CHECKS_RUN)} checks attempted before failure)", file=sys.stderr)
        return 1

    print(f"bootstrap_stdlib_only_smoke OK: {len(_CHECKS_RUN)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
