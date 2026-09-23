"""Hermetic receipt fixtures for the bootstrap Homebrew mutation guard."""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import jsonschema

# ruff: noqa: E402

_ROOT = Path(__file__).resolve().parents[3]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

_SCHEMA = json.loads((_ROOT / "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json").read_text())

from bootstrap_adapter.homebrew import _homebrew_plan_is_exact
from bootstrap_adapter.models import AdapterRuntime, PostgresObservation, RolePolicyObservation
from bootstrap_adapter.routes import _coding_tool_route, _postgres_configure_route, _postgres_install_route

_RECEIPT_049_CODEX = "==> Would install 1 cask:\ncodex\n"
_RECEIPT_054_POSTGRES_STDOUT = """codex-cli 0.153.4
2.1.236 (Claude Code)
==> Would install 1 formula:
postgresql@17
==> Downloading https://ghcr.io/v2/homebrew/core/postgresql/17/manifests/17.11
Already downloaded: /Users/admin/Library/Caches/Homebrew/downloads/e4ff9c3d52f936d2bd96532d8a86426abfd82dc11b1176d5feefe727ffb449e8--postgresql@17-17.11.bottle_manifest.json
==> Would install 1 dependency for postgresql@17:
krb5
==> Would install 1 formula:
postgresql@17
==> Would install 1 dependency for postgresql@17:
krb5
"""
_RECEIPT_054_POSTGRES_STDERR = """Warning: `$HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK` is set: not checking for outdated
dependents or dependents with broken linkage!
"""


def _check(condition: object, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _request(operation_id: str) -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "phase": "apply",
        "probe_purpose": None,
        "request_id": "00000000-0000-0000-0000-000000000000",
        "name": "fixture",
    }


def _runtime(stdout: str, stderr: str, returncode: int) -> AdapterRuntime:
    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew fixture\n", "")
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)

    return AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )


def _apply_failure_runtime(stdout: str, stderr: str) -> AdapterRuntime:
    """Return an exact dry-run plan followed by a failed mutating invocation."""

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "Homebrew fixture\n", "")
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 0, _RECEIPT_049_CODEX, "")
        return subprocess.CompletedProcess(command, 1, stdout, stderr)

    return AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )


def _check_failure_envelopes() -> None:
    stdout = "Homebrew stdout marker\n"
    stderr = "Homebrew stderr marker\n"
    failed_coding_tool = _coding_tool_route(_request("install_codex_cli"), _runtime(stdout, stderr, 1))
    _check(
        failed_coding_tool["stdout"] == stdout and failed_coding_tool["stderr"] == stderr,
        "coding-tool failure preserves exact captured Homebrew streams",
    )

    failed_apply = _coding_tool_route(_request("install_codex_cli"), _apply_failure_runtime(stdout, stderr))
    _check(
        failed_apply["stdout"] == stdout and failed_apply["stderr"] == stderr,
        "coding-tool apply failure preserves exact captured Homebrew streams",
    )

    observation = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
    with patch("bootstrap_adapter.routes.postgres_observation", return_value=observation):
        failed_postgres = _postgres_install_route(_request("install_postgresql"), _runtime(stdout, stderr, 1))
    _check(
        failed_postgres["stdout"] == stdout and failed_postgres["stderr"] == stderr,
        "PostgreSQL Homebrew failure preserves exact captured streams",
    )

    oversized = "x" * 16_385
    bounded = _coding_tool_route(_request("install_codex_cli"), _runtime(oversized, "", 1))
    _check(
        bounded["stdout"] == oversized[:16_384],
        "Homebrew failure stdout is bounded at the envelope schema limit",
    )


def _check_fresh_postgres_plans_pgvector_before_apply() -> None:
    """A stopped service must disclose pgvector before applying the reviewed plan."""

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--dry-run" in command:
            package = command[-1]
            return subprocess.CompletedProcess(
                command,
                0,
                f"==> Would install 1 formula:\n{package}\n",
                "",
            )
        return subprocess.CompletedProcess(command, 0, "", "")

    runtime = AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    fresh = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
    refreshed = PostgresObservation(True, "/fixture/brew", True, True, 17, True, False)
    preview_request = _request("install_postgresql")
    preview_request["phase"] = "probe"
    with (
        patch(
            "bootstrap_adapter.routes.postgres_observation",
            return_value=fresh,
        ) as observe,
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=refreshed) as refresh_observe,
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=True),
    ):
        preview = _postgres_install_route(preview_request, runtime)
        applied = _postgres_install_route(_request("install_postgresql"), runtime)

    _check(
        [action["id"] for action in preview["planned_actions"]]
        == [
            "postgres.install_homebrew_formula",
            "postgres.start_homebrew_service",
            "postgres.install_pgvector_formula",
        ],
        "preview exposes every PostgreSQL action that apply may execute",
    )
    _check(
        applied["checkpoint_status"] == "applied",
        "fresh PostgreSQL install applies successfully",
    )
    _check(
        observe.call_count == 2 and refresh_observe.call_count == 1,
        "service start re-observes PostgreSQL before readiness-dependent work",
    )
    _check(
        commands
        == [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "install", "postgresql@17"],
            ["/fixture/brew", "services", "start", "postgresql@17"],
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "install", "pgvector"],
        ],
        "PostgreSQL apply executes the pgvector package action disclosed in its approved plan",
    )


def _check_postgres_service_readiness_outcomes() -> None:
    """Cover every service command/readiness outcome without a live daemon."""

    ready = PostgresObservation(True, "/fixture/brew", True, True, 17, True, True)
    down = PostgresObservation(True, "/fixture/brew", True, True, 17, False, None)

    no_op_calls: list[list[str]] = []

    def no_op_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        no_op_calls.append(command)
        return subprocess.CompletedProcess(command, 0, "", "")

    no_op_runtime = AdapterRuntime(
        run=no_op_run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    with patch("bootstrap_adapter.routes.postgres_observation", return_value=ready):
        no_op = _postgres_install_route(_request("install_postgresql"), no_op_runtime)
    _check(
        no_op["checkpoint_status"] == "applied" and not no_op_calls,
        "package-present and service-ready PostgreSQL is an apply no-op",
    )

    def service_runtime(returncode: int, stdout: str = "", stderr: str = "") -> AdapterRuntime:
        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            return subprocess.CompletedProcess(command, returncode, stdout, stderr)

        return AdapterRuntime(
            run=run,
            which=lambda name: "/fixture/brew" if name == "brew" else None,
            now=lambda: datetime(2026, 9, 6, tzinfo=UTC),
            name="fixture",
            target=Path("/fixture"),
        )

    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=ready),
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=True),
    ):
        started = _postgres_install_route(_request("install_postgresql"), service_runtime(0))
    _check(
        started["checkpoint_status"] == "applied",
        "a stopped service becomes ready after its successful start command",
    )

    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=ready),
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=True),
    ):
        already_running = _postgres_install_route(_request("install_postgresql"), service_runtime(3, stderr="already running\n"))
    _check(
        already_running["checkpoint_status"] == "applied",
        "a nonzero service start is accepted only after fresh readiness proof",
    )

    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=False),
    ):
        not_ready = _postgres_install_route(_request("install_postgresql"), service_runtime(0, stdout="start accepted\n"))
    _check(
        not_ready["error_kind"] == "postgres_service_not_ready" and not_ready["exit_code"] == 0 and not_ready["duration_ms"] >= 0 and not_ready["stdout"] == "start accepted\n",
        "zero-exit service start that remains down is reported as not ready with its receipt",
    )

    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=down),
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=False),
    ):
        failed = _postgres_install_route(_request("install_postgresql"), service_runtime(7, "service stdout\n", "service stderr\n"))
    _check(
        failed["error_kind"] == "postgres_service_start_failed" and failed["exit_code"] == 7 and failed["stdout"] == "service stdout\n" and failed["stderr"] == "service stderr\n" and failed["reason"] is not None,
        "nonzero service start that remains down preserves real failure diagnostics",
    )


def _check_postgres_configuration_failure_keeps_psql_diagnostics() -> None:
    observed = PostgresObservation(True, "/fixture/brew", True, True, 17, True, True)
    policy = RolePolicyObservation(
        role_exists=True,
        database_exists=True,
        role_safe=True,
        database_owner_matches=True,
        schema_exists=False,
        schema_owner_matches=None,
        public_connect_revoked=True,
        vector_installed=True,
        hba_path=None,
        hba_safe=True,
        hba_layout_recognized=True,
        scram_present=True,
    )
    diagnostics_by_stream = {
        "stderr": "psql: error: permission denied for schema fixture\n",
        "stdout": "psql: schema fixture output\n",
    }
    for stream, diagnostics in diagnostics_by_stream.items():
        runtime = _runtime(
            diagnostics if stream == "stdout" else "",
            diagnostics if stream == "stderr" else "",
            1,
        )
        with (
            patch("bootstrap_adapter.routes.postgres_observation", return_value=observed),
            patch("bootstrap_adapter.routes.role_policy_observation", return_value=policy),
            patch(
                "bootstrap_adapter.postgres.postgres_binaries",
                return_value={"psql": "/fixture/psql"},
            ),
        ):
            failed = _postgres_configure_route(_request("configure_postgresql"), runtime)

        _check(
            failed["checkpoint_status"] == "failed" and failed["error_kind"] == "postgres_configuration_failed",
            f"failing PostgreSQL configuration returns its canonical {stream} failure envelope",
        )
        _check(
            diagnostics.strip() in str(failed["repair"]),
            f"failing psql {stream} reaches the PostgreSQL configuration result",
        )

    for stream in ("stderr", "stdout"):
        diagnostics = "psql: " + ("X" * 2048)
        runtime = _runtime(
            diagnostics if stream == "stdout" else "",
            diagnostics if stream == "stderr" else "",
            1,
        )
        with (
            patch("bootstrap_adapter.routes.postgres_observation", return_value=observed),
            patch("bootstrap_adapter.routes.role_policy_observation", return_value=policy),
            patch(
                "bootstrap_adapter.postgres.postgres_binaries",
                return_value={"psql": "/fixture/psql"},
            ),
        ):
            failed = _postgres_configure_route(_request("configure_postgresql"), runtime)

        repair = str(failed["repair"])
        _check(
            repair.startswith("PostgreSQL schema creation failed (exit 1): psql: "),
            f"long psql {stream} keeps failure context before truncation",
        )
        _check(
            "[truncated," in repair and "chars total]" in repair,
            f"long psql {stream} reports explicit repair truncation",
        )
        _check(
            len(repair) <= 2048,
            f"long psql {stream} repair stays within the envelope cap",
        )
        jsonschema.Draft7Validator(_SCHEMA).validate(failed)


def main() -> int:
    _check(
        _homebrew_plan_is_exact(_RECEIPT_049_CODEX, kind="cask", package="codex"),
        "receipt-049 arrow-prefixed Codex cask plan is accepted",
    )
    _check(
        _homebrew_plan_is_exact(
            _RECEIPT_054_POSTGRES_STDOUT + _RECEIPT_054_POSTGRES_STDERR,
            kind="formula",
            package="postgresql@17",
        ),
        "receipt-054 repeated PostgreSQL dependency closure and warning are accepted",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 2 formulae:\npostgresql@17\nunrelated\n",
            kind="formula",
            package="postgresql@17",
        ),
        "two unrelated top-level packages remain refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n==> Would upgrade 1 formula:\npython@3.13\n",
            kind="formula",
            package="postgresql@17",
        ),
        "a hidden upgrade remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n==> Would install 1 dependency for unrelated:\nkrb5\n",
            kind="formula",
            package="postgresql@17",
        ),
        "an unrelated dependency-block parent remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n==> Would install 1 dependency for postgresql@17:\nkrb5\ngssapi\n",
            kind="formula",
            package="postgresql@17",
        ),
        "a dependency block with more items than its declared count remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "Warning: Homebrew produced no install plan\n",
            kind="formula",
            package="postgresql@17",
        ),
        "output without a package-plan heading remains refused",
    )
    _check_failure_envelopes()
    _check_fresh_postgres_plans_pgvector_before_apply()
    _check_postgres_service_readiness_outcomes()
    _check_postgres_configuration_failure_keeps_psql_diagnostics()
    print("bootstrap_adapter_homebrew_plan_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
