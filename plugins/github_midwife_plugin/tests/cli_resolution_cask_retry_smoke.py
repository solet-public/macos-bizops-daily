"""iss_e1d5285b: resolve_executable PATH fallback and _cli_available's bounded
post-apply retry, mirroring the already-fixed bootstrap_adapter class of bug."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))

from github_midwife_plugin.installation_doctor import _cli_available  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import (  # noqa: E402
    CommandOutcome,
    resolve_executable,
)

_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _WhichRuntime:
    """resolvable maps each searchable which-argument to its resolved absolute path."""

    def __init__(self, resolvable: dict[str, str]) -> None:
        self.home = Path("/tmp/fixture-home")
        self.resolvable = resolvable
        self.which_calls: list[str] = []

    def run(self, argv: tuple[str, ...], **_: object) -> CommandOutcome:
        if argv[0] == "/usr/bin/which":
            target = argv[1]
            self.which_calls.append(target)
            resolved = self.resolvable.get(target)
            if resolved is not None:
                return CommandOutcome(0, False, 1, resolved, "")
            return CommandOutcome(1, False, 1, "", "")
        raise AssertionError(f"unexpected command: {argv!r}")


def _request(*, probe_purpose: str) -> AdapterRequest:
    return AdapterRequest(
        request_id="8f2f3ed3-03fc-4f58-915e-eb400a172a67",
        operation_id="codex_cli_available",
        operation_ref="hydration::codex.probe_cli",
        phase="probe",
        probe_purpose=probe_purpose,
        attempt=1,
        name="fixture",
        target=Path("/tmp/fixture-target"),
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=True,
        timeout_seconds=30,
        public_inputs={},
    )


def _check_resolve_executable_path_fallback() -> None:
    direct = _WhichRuntime({"codex": "/usr/local/bin/codex"})
    _check(resolve_executable(direct, "codex") == "/usr/local/bin/codex", "bare PATH resolution still works")

    fallback = _WhichRuntime({"/opt/homebrew/bin/codex": "/opt/homebrew/bin/codex"})
    _check(
        resolve_executable(fallback, "codex") == "/opt/homebrew/bin/codex",
        "restricted PATH falls back to the standard Homebrew bin directory",
    )
    _check(
        fallback.which_calls == ["codex", "/opt/homebrew/bin/codex"],
        "fallback tries the bare name first, then /opt/homebrew/bin",
    )

    unresolved = _WhichRuntime({})
    _check(resolve_executable(unresolved, "codex") is None, "genuinely absent executable stays unresolved")


def _check_cli_available_pre_install_never_retries() -> None:
    runtime = _WhichRuntime({})
    with patch("github_midwife_plugin.installation_doctor.time.sleep") as sleep:
        result = _cli_available(_request(probe_purpose="pre_apply"), runtime)
    _check(result["checkpoint_status"] == "blocked", "pre-install probe with codex absent reports blocked")
    _check(sleep.call_count == 0, "a non-post_apply probe never retries -- the tool is legitimately absent pre-install")
    _check(
        runtime.which_calls == ["codex", "/opt/homebrew/bin/codex", "/usr/local/bin/codex", "/tmp/fixture-home/.local/bin/codex"],
        "pre-install probe resolves once (bare name + the fixed fallbacks, the runtime home's native directory last), never retries the whole resolution",
    )


def _check_cli_available_post_apply_retries_until_resolved() -> None:
    runtime = _WhichRuntime({})
    bare_attempts = 0

    def run(argv: tuple[str, ...], **_: object) -> CommandOutcome:
        nonlocal bare_attempts
        if argv[0] == "/usr/bin/which":
            target = argv[1]
            runtime.which_calls.append(target)
            if target == "codex":
                bare_attempts += 1
                if bare_attempts >= 3:
                    return CommandOutcome(0, False, 1, "/usr/local/bin/codex", "")
            return CommandOutcome(1, False, 1, "", "")
        if argv[0] == "/usr/local/bin/codex":
            return CommandOutcome(0, False, 1, "codex-cli 1.0.0", "")
        raise AssertionError(f"unexpected command: {argv!r}")

    runtime.run = run  # type: ignore[method-assign]
    with patch("github_midwife_plugin.installation_doctor.time.sleep") as sleep:
        result = _cli_available(_request(probe_purpose="post_apply"), runtime)
    _check(result["checkpoint_status"] == "verified", "post_apply probe retries until the cask-linked executable resolves")
    _check(sleep.call_count == 2, "retries stop as soon as resolution succeeds -- 2 failed attempts, then success on the 3rd")


def _check_cli_available_post_apply_retry_is_bounded() -> None:
    runtime = _WhichRuntime({})
    with patch("github_midwife_plugin.installation_doctor.time.sleep") as sleep:
        result = _cli_available(_request(probe_purpose="post_apply"), runtime)
    _check(
        result["checkpoint_status"] == "blocked" and result["error_kind"] == "codex_cli_failed",
        "post_apply retry exhaustion still reports the honest blocked/codex_cli_failed outcome",
    )
    _check(sleep.call_count == 4, "retry is bounded (5 attempts, 4 sleeps between them), never infinite")


def main() -> int:
    _check_resolve_executable_path_fallback()
    _check_cli_available_pre_install_never_retries()
    _check_cli_available_post_apply_retries_until_resolved()
    _check_cli_available_post_apply_retry_is_bounded()
    print(f"cli_resolution_cask_retry_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
