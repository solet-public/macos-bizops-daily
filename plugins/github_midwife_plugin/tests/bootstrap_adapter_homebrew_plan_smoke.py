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

from bootstrap_adapter.homebrew import (
    HomebrewInstallError,
    _inspect_homebrew_plan,
    homebrew_guard_environment,
    run_homebrew_install_required,
)
from bootstrap_adapter.models import AdapterRuntime, PostgresObservation, RolePolicyObservation
from bootstrap_adapter.routes import (
    _coding_tool_route,
    _postgres_configure_route,
    _postgres_install_route,
    _upgraded_dependency_evidence,
)

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
_R44_POSTGRES_UPGRADE_PLAN = """==> Would install 1 formula:
postgresql@17
==> Would install 1 dependency for postgresql@17:
krb5
==> Would upgrade 2 dependencies for postgresql@17:
readline
xz
==> Would install 1 formula:
postgresql@17
==> Would install 1 dependency for postgresql@17:
krb5
==> Would upgrade 2 dependencies for postgresql@17:
readline
xz
"""

# r53 fresh macOS 26 guest, create_apply3 (iss_6130e3f2), verbatim.
_RECEIPT_R53_LLAMA_CPP_STDOUT = """==> Would install 1 formula:
llama.cpp
==> Downloading https://ghcr.io/v2/homebrew/core/llama.cpp/manifests/0.4.0
==> Would install 2 dependencies for llama.cpp:
libomp
ggml
==> Would install 1 formula:
llama.cpp
==> Would install 2 dependencies for llama.cpp:
libomp
ggml
"""

def _homebrew_plan_is_exact(output: str, *, kind: str, package: str) -> bool:
    return _inspect_homebrew_plan(output, kind=kind, package=package).exact


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
    _check(
        failed_postgres["error_kind"] == "postgres_install_failed",
        "non-upgrade PostgreSQL Homebrew failures retain their existing error kind",
    )

    oversized = "x" * 16_385
    bounded = _coding_tool_route(_request("install_codex_cli"), _runtime(oversized, "", 1))
    _check(
        bounded["stdout"] == oversized[:16_384],
        "Homebrew failure stdout is bounded at the envelope schema limit",
    )


def _check_r44_postgres_dependency_upgrade_proceeds() -> None:
    """The verbatim r44 plan is the formula's own requirement: install proceeds, names are evidence."""

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if command[1:] == ["install", "--dry-run", "postgresql@17"]:
            return subprocess.CompletedProcess(command, 0, _R44_POSTGRES_UPGRADE_PLAN, "")
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 0, f"==> Would install 1 formula:\n{command[-1]}\n", "")
        return subprocess.CompletedProcess(command, 0, "", "")

    runtime = AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 23, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    fresh = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
    refreshed = PostgresObservation(True, "/fixture/brew", True, True, 17, True, False)
    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=fresh),
        patch("bootstrap_adapter.postgres_install.postgres_observation", return_value=refreshed),
        patch("bootstrap_adapter.postgres_install.wait_for_postgres_ready", return_value=True),
    ):
        applied = _postgres_install_route(_request("install_postgresql"), runtime)
    _check(applied["checkpoint_status"] == "applied", "r44 dependency upgrades no longer stop the PostgreSQL install")
    _check(
        commands
        == [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "install", "postgresql@17"],
            ["/fixture/brew", "services", "start", "postgresql@17"],
            ["/fixture/brew", "install", "--dry-run", "pgvector"],
            ["/fixture/brew", "install", "pgvector"],
        ],
        "the install is the plain formula install: no brew upgrade, no --ignore-dependencies",
    )
    recorded = [item for item in applied["evidence"] if item["id"] == "homebrew.upgraded_dependencies"]
    _check(
        len(recorded) == 1 and recorded[0]["observed"] == ["readline", "xz"],
        "the upgraded dependency names readline, xz are recorded as evidence",
    )
    jsonschema.Draft7Validator(_SCHEMA).validate(applied)


def _check_python_framework_upgrade_is_flagged() -> None:
    """An accepted python@3.13 upgrade is the one that can strand another solet's Keychain ACLs (iss_d62aeab7)."""

    runtime = AdapterRuntime(
        run=lambda command, **_kwargs: subprocess.CompletedProcess(command, 0, "", ""),
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 29, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    ordinary = _upgraded_dependency_evidence(runtime, ("readline", "xz"))
    _check([item["id"] for item in ordinary] == ["homebrew.upgraded_dependencies"], "an ordinary upgrade adds no python warning")
    flagged = _upgraded_dependency_evidence(runtime, ("readline", "python@3.13"))
    _check(
        [item["id"] for item in flagged] == ["homebrew.upgraded_dependencies", "homebrew.python_framework_upgraded"],
        "a python@3.13 upgrade adds its own evidence item after the names",
    )
    warning = flagged[1]
    _check(warning["observed"] == ["python@3.13"], "the warning records the framework that moved")
    _check(
        "-25293" in warning["summary"] and "Always Allow" in warning["summary"],
        "the warning names the failure and the recovery",
    )
    _check(flagged[0]["observed"] == ["readline", "python@3.13"], "the full upgrade list is still recorded unchanged")
    evidence_schema = {"$ref": "#/definitions/evidence", "definitions": _SCHEMA["definitions"]}
    for item in flagged:
        jsonschema.Draft7Validator(evidence_schema).validate(item)


def _check_dependency_upgrade_grammar_stays_closed() -> None:
    """Only a complete, counted upgrade block for the requested package is accepted."""

    head = "==> Would install 1 formula:\npostgresql@17\n"
    accepted = {
        "plural": head + "==> Would upgrade 2 dependencies for postgresql@17:\nreadline\nxz\n",
        "singular": head + "==> Would upgrade 1 dependency for postgresql@17:\nreadline\n",
        "versioned_items": head + "==> Would upgrade 1 dependency for postgresql@17:\nreadline 8.2.13\n",
    }
    refused = {
        "declares_two_lists_one": head + "==> Would upgrade 2 dependencies for postgresql@17:\nreadline\n",
        "declares_one_lists_two": head + "==> Would upgrade 1 dependency for postgresql@17:\nreadline\nxz\n",
        "unrelated_parent": head + "==> Would upgrade 1 dependency for unrelated:\nreadline\n",
        "malformed_item": head + "==> Would upgrade 1 dependency for postgresql@17:\nRead Line!\n",
        "unprefixed_notice": head + "Would upgrade 1 dependency for postgresql@17:\nreadline\n\x1b[0m",
        "formula_upgrade": head + "==> Would upgrade 1 formula:\npython@3.13\n",
        "reinstall": head + "==> Would reinstall 1 dependency for postgresql@17:\nreadline\n",
    }
    for name, plan in accepted.items():
        _check(
            _homebrew_plan_is_exact(plan, kind="formula", package="postgresql@17"),
            f"{name} dependency upgrade block is accepted",
        )
    for name, plan in refused.items():
        _check(
            not _homebrew_plan_is_exact(plan, kind="formula", package="postgresql@17"),
            f"{name} dependency upgrade block stays refused",
        )
    inspected = _inspect_homebrew_plan(accepted["plural"], kind="formula", package="postgresql@17")
    _check(inspected.upgraded_dependencies == ("readline", "xz"), "accepted upgrades are reported in plan order")

    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        commands.append(command)
        if "--dry-run" in command:
            return subprocess.CompletedProcess(command, 0, refused["declares_two_lists_one"], "")
        return subprocess.CompletedProcess(command, 1, "", "")

    runtime = AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 9, 23, tzinfo=UTC),
        name="fixture",
        target=Path("/fixture"),
    )
    try:
        run_homebrew_install_required(runtime, "/fixture/brew", "postgresql@17", "PostgreSQL")
    except HomebrewInstallError:
        pass
    else:
        raise AssertionError("a count-mismatched upgrade block was accepted")
    _check(
        commands == [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "list", "--versions", "postgresql@17"],
        ],
        "a count-mismatched upgrade block runs no install command",
    )


def _check_incomplete_and_decorated_plans_refuse_before_mutation() -> None:
    """Incomplete, decorated, and unrecognized plan lines cannot authorize install."""

    head = "==> Would install 1 formula:\npostgresql@17\n"
    upgrade = "==> Would upgrade 1 dependency for postgresql@17:\nreadline\n"
    dependency_head = "==> Would install 1 dependency for postgresql@17:\n"
    raw_control_lines = {
        "vertical_tab": "postgresql@17\x0b",
        "form_feed": "postgresql@17\x0c",
        "next_line": "postgresql@17\x85",
        "carriage_return": "postgresql@17\r",
        "line_separator": "postgresql@17\u2028",
        "paragraph_separator": "postgresql@17\u2029",
        "stderr_vertical_tab": "metadata\x0b",
    }
    blank_only_cases = frozenset(
        {"blank_stdout_spaces", "blank_stderr_spaces", "blank_stdout_newline", "blank_stderr_newline"}
    )
    cases = {
        "vertical_tab": (head[:-1] + "\x0b\n", ""),
        "form_feed": (head[:-1] + "\x0c\n", ""),
        "next_line": (head[:-1] + "\x85\n", ""),
        "carriage_return": (head[:-1] + "\r\n", ""),
        "line_separator": (head[:-1] + "\u2028\n", ""),
        "paragraph_separator": (head[:-1] + "\u2029\n", ""),
        "stderr_vertical_tab": (head, "metadata\x0b\n"),
        "blank_stdout_spaces": ("   ", ""),
        "blank_stderr_spaces": ("", "   "),
        "blank_stdout_newline": ("\n", ""),
        "blank_stderr_newline": ("", "\n"),
        "truncated_stdout": (head + "notice: download metadata\n" * 800 + upgrade, ""),
        "truncated_stderr": (head, "notice: download metadata\n" * 800 + upgrade),
        "unprefixed_upgrade_notice": (head + "Upgrading readline\n", ""),
        "versioned_upgrade_notice": (head + dependency_head + "Upgrading 1.0\n", ""),
        "versioned_unlink_notice": (head + dependency_head + "Unlinking 1.0\n", ""),
        "versioned_link_notice": (head + dependency_head + "Linking 1.0\n", ""),
        "lowercase_unlink_notice": (head + dependency_head + "unlinking 1.0\n", ""),
        "lowercase_link_notice": (head + dependency_head + "linking 1.0\n", ""),
        "lowercase_unlisted_gerund": (head + dependency_head + "staging 1.0\n", ""),
        "titlecase_unlisted_action": (head + dependency_head + "Staging 1.0\n", ""),
        "unknown_versioned_dependency": (head + dependency_head + "glorp 1.0\n", ""),
        "unknown_bare_dependency": (head + dependency_head + "glorp\n", ""),
        "unknown_plan_line": (head + "Other pending operation\n", ""),
        "ansi": (
            head
            + "\x1b[32m==> Would upgrade 1 dependency for postgresql@17:\x1b[0m\n"
            + "\x1b[32mreadline\x1b[0m\n",
            "",
        ),
        "unrecognized": (head + "==> Would replace 1 dependency for postgresql@17:\nreadline\n", ""),
        "mixed_case": (head + "==> would upgrade 1 dependency for postgresql@17:\nreadline\n", ""),
    }
    for control_name, control in (
        ("vertical_tab", "\x0b"),
        ("form_feed", "\x0c"),
        ("next_line", "\x85"),
        ("carriage_return", "\r"),
        ("line_separator", "\u2028"),
        ("paragraph_separator", "\u2029"),
    ):
        for stream in ("stdout", "stderr"):
            name = f"control_only_{control_name}_{stream}"
            cases[name] = (control, "") if stream == "stdout" else ("", control)
            raw_control_lines[name] = control

    def check_case(name: str, stdout: str, stderr: str, *, installed: bool) -> None:
        calls: list[list[str]] = []
        state = "installed" if installed else "absent"
        expected_calls = [
            ["/fixture/brew", "install", "--dry-run", "postgresql@17"],
            ["/fixture/brew", "list", "--versions", "postgresql@17"],
        ]

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if "--dry-run" in command:
                return subprocess.CompletedProcess(command, 0, stdout, stderr)
            if command[1:] == ["list", "--versions", "postgresql@17"]:
                return subprocess.CompletedProcess(
                    command,
                    0 if installed else 1,
                    "postgresql@17 17.10\n" if installed else "",
                    "",
                )
            raise AssertionError(f"{name}/{state} invoked an unapproved package mutation: {command}")

        runtime = AdapterRuntime(
            run=run,
            which=lambda _name: "/fixture/brew",
            now=lambda: datetime(2026, 9, 23, tzinfo=UTC),
            name="fixture",
            target=Path("/fixture"),
        )
        output_complete = len(stdout) <= 16_384 and len(stderr) <= 16_384
        reported_line: str | None = None
        try:
            run_homebrew_install_required(runtime, "/fixture/brew", "postgresql@17", "PostgreSQL")
        except HomebrewInstallError as exc:
            reported_line = exc.unrecognized_line
            _check(
                exc.outcome.stdout == stdout[:16_384]
                and exc.outcome.stderr == stderr[:16_384]
                and exc.outcome.output_complete == output_complete,
                f"{name}/{state} keeps bounded diagnostics",
            )
            _check(
                (reported_line is not None and "unrecognized dry-run line" in str(exc))
                if output_complete and name not in blank_only_cases else reported_line is None,
                f"{name}/{state} reports a complete unrecognized line only with complete evidence",
            )
        else:
            raise AssertionError(f"{name}/{state} was permitted")
        if name in raw_control_lines:
            _check(
                reported_line == raw_control_lines[name],
                f"{name}/{state} reports the unsplit raw control line",
            )
        _check(
            calls == expected_calls,
            f"{name}/{state} refused before any mutating Homebrew command",
        )

        calls.clear()
        observation = PostgresObservation(True, "/fixture/brew", False, False, None, False, None)
        with patch("bootstrap_adapter.routes.postgres_observation", return_value=observation):
            failed = _postgres_install_route(_request("install_postgresql"), runtime)
        _check(
            failed["checkpoint_status"] == "failed"
            and failed["error_kind"] == "postgres_install_failed",
            f"{name}/{state} route refuses with the generic failure kind",
        )
        repair = str(failed["repair"])
        _check(
            ("Unrecognized Homebrew dry-run line" in repair and repr(reported_line[:160]) in repair)
            if reported_line is not None else "Unrecognized Homebrew dry-run line" not in repair,
            f"{name}/{state} route reports the unrecognized line without guessing an upgrade",
        )
        _check(
            calls == expected_calls,
            f"{name}/{state} route stops before install or service start",
        )
        jsonschema.Draft7Validator(_SCHEMA).validate(failed)

    for name, (stdout, stderr) in cases.items():
        for installed in (False, True):
            check_case(name, stdout, stderr, installed=installed)

    with patch.dict("os.environ", {"HOMEBREW_COLOR": "1"}):
        environment = homebrew_guard_environment()
    _check(
        environment["HOMEBREW_NO_COLOR"] == "1" and "HOMEBREW_COLOR" not in environment,
        "guard disables inherited Homebrew color during dry-run and apply",
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
        _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n"
            "==> Would install 1 dependency for postgresql@17:\nreadline 8.3\n",
            kind="formula",
            package="postgresql@17",
        ),
        "lowercase versioned dependency item remains an approved install plan",
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
    _check(
        _homebrew_plan_is_exact(
            _RECEIPT_R53_LLAMA_CPP_STDOUT + _RECEIPT_054_POSTGRES_STDERR,
            kind="formula",
            package="llama.cpp",
        ),
        "r53 llama.cpp plan with its reviewed libomp and ggml dependencies is accepted",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\nllama.cpp\n"
            "==> Would install 3 dependencies for llama.cpp:\nlibomp\nggml\nopenssl@3\n",
            kind="formula",
            package="llama.cpp",
        ),
        "an unreviewed llama.cpp dependency remains refused",
    )
    _check(
        not _homebrew_plan_is_exact(
            "==> Would install 1 formula:\npostgresql@17\n==> Would install 1 dependency for postgresql@17:\nlibomp\n",
            kind="formula",
            package="postgresql@17",
        ),
        "llama.cpp's reviewed dependencies are not approved for other packages",
    )
    _check_failure_envelopes()
    _check_r44_postgres_dependency_upgrade_proceeds()
    _check_python_framework_upgrade_is_flagged()
    _check_dependency_upgrade_grammar_stays_closed()
    _check_incomplete_and_decorated_plans_refuse_before_mutation()
    _check_fresh_postgres_plans_pgvector_before_apply()
    _check_postgres_service_readiness_outcomes()
    _check_postgres_configuration_failure_keeps_psql_diagnostics()
    print("bootstrap_adapter_homebrew_plan_smoke OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
