"""Focused LM Studio bootstrap-route assertions for the adapter smoke."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

_LM_STUDIO_ROUTES = (
    ("install_lm_studio", "setup::lm_studio.install"),
    ("start_lm_studio_server", "setup::lm_studio.start_server"),
    ("pull_lm_studio_embedding_model", "setup::lm_studio.pull_embedding"),
    ("load_lm_studio_embedding_model", "setup::lm_studio.load_embedding"),
    ("pull_lm_studio_inference_model", "setup::lm_studio.pull_inference"),
    ("load_lm_studio_inference_model", "setup::lm_studio.load_inference"),
    ("install_lm_studio_login_agent", "setup::lm_studio.install_login_agent"),
    ("lm_studio_cli_available", "setup::lm_studio.cli_available"),
    ("lm_studio_server_ready", "setup::lm_studio.server_ready"),
    ("lm_studio_embedding_artifact_present", "setup::lm_studio.embedding_artifact_present"),
    ("lm_studio_embedding_model_served", "setup::lm_studio.embedding_model_served"),
    ("lm_studio_inference_artifact_present", "setup::lm_studio.inference_artifact_present"),
    ("lm_studio_inference_model_served", "setup::lm_studio.inference_model_served"),
    ("lm_studio_login_agent_valid", "setup::lm_studio.login_agent_valid"),
    ("lm_studio_jit_disabled", "setup::lm_studio.jit_disabled"),
)
_VALID_INPUTS = {
    "embeddings_implementation": "lm_studio",
    "inference_implementation": "lm_studio",
    "lm_studio_base_url": "http://127.0.0.1:1234/v1",
}
_INVALID_INPUTS = {**_VALID_INPUTS, "lm_studio_base_url": "http://not-the-reviewed-loopback/v1"}


def _no_host_command(*_args: object, **_kwargs: object) -> subprocess.CompletedProcess[str]:
    raise AssertionError("invalid LM Studio inputs must not invoke a host command")


def _apply_install_runner(home: Path, calls: list[tuple[list[str], dict[str, object]]]) -> Any:
    """Model curl and sh while requiring the pinned script on sh's stdin."""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append((command, kwargs))
        if command[0] == "/usr/bin/curl":
            return subprocess.CompletedProcess(command, 0, 'APP_VERSION="0.0.24-1"\necho installer\n', "")
        if command[0] == "/bin/sh":
            script = kwargs.get("input")
            if script != 'APP_VERSION="0.0.23-1"\necho installer\n':
                return subprocess.CompletedProcess(command, 1, "", "installer script missing from stdin")
            cli = home / ".lmstudio/bin/lms"
            cli.parent.mkdir(parents=True)
            cli.write_text("fixture executable\n", encoding="utf-8")
            cli.chmod(0o700)
            return subprocess.CompletedProcess(command, 0, "installer consumed stdin\n", "")
        raise AssertionError(f"unexpected host command: {command}")

    return run


def _silent_install_runner(home: Path) -> Any:
    """Model the prior silent shell exit even if a fixture leaves a CLI behind."""

    def run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[0] == "/usr/bin/curl":
            return subprocess.CompletedProcess(command, 0, 'APP_VERSION="0.0.24-1"\necho installer\n', "")
        if command[0] == "/bin/sh":
            if kwargs.get("input") != 'APP_VERSION="0.0.23-1"\necho installer\n':
                raise AssertionError("installer script was not forwarded to the shell")
            cli = home / ".lmstudio/bin/lms"
            cli.parent.mkdir(parents=True)
            cli.write_text("stale fixture executable\n", encoding="utf-8")
            return subprocess.CompletedProcess(command, 0, "", "")
        raise AssertionError(f"unexpected host command: {command}")

    return run


def check_lm_studio_routes(
    *,
    module: ModuleType,
    root: Path,
    repository_root: Path,
    request_factory: Any,
    execute: Any,
    check: Any,
) -> None:
    """Every declared LM Studio route must resolve under base Python."""
    for operation_id, operation_ref in _LM_STUDIO_ROUTES:
        response = execute(
            module,
            request_factory(
                root,
                operation_id=operation_id,
                operation_ref=operation_ref,
                public_inputs=_INVALID_INPUTS,
            ),
            _no_host_command,
            lambda _name: None,
        )
        check(
            response["checkpoint_status"] == "blocked"
            and response["error_kind"] == "lm_studio_inputs_invalid",
            f"{operation_id} resolves through the base-Python LM Studio adapter",
        )
    target = root / "lm-studio-target"
    registry = target / "plugins/github_midwife_plugin/knowledge_base/profile_templates/lm_studio_models.yaml"
    registry.parent.mkdir(parents=True)
    registry.write_text(
        (
            repository_root
            / "plugins/github_midwife_plugin/knowledge_base/profile_templates/lm_studio_models.yaml"
        ).read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    with patch.dict(os.environ, {"HOME": str(root / "lm-studio-home")}):
        preview = execute(
            module,
            request_factory(
                target,
                operation_id="install_lm_studio",
                operation_ref="setup::lm_studio.install",
                public_inputs=_VALID_INPUTS,
            ),
            _no_host_command,
            lambda _name: None,
        )
    check(
        preview["checkpoint_status"] == "pending"
        and preview["planned_actions"][0]["id"] == "lm_studio.install",
        "base-Python LM Studio install preview validates the pinned registry without host commands",
    )
    home = root / "lm-studio-apply-home"
    calls: list[tuple[list[str], dict[str, object]]] = []
    with patch.dict(os.environ, {"HOME": str(home)}):
        applied = execute(
            module,
            request_factory(
                target,
                operation_id="install_lm_studio",
                operation_ref="setup::lm_studio.install",
                phase="apply",
                purpose=None,
                approval="sha256:" + "b" * 64,
                public_inputs=_VALID_INPUTS,
            ),
            _apply_install_runner(home, calls),
            lambda _name: None,
        )
    check(
        applied["checkpoint_status"] == "applied"
        and (home / ".lmstudio/bin/lms").is_file(),
        "apply install delivers the pinned installer body to sh and materializes the CLI (red: drop input)",
    )
    check(
        [command for command, _kwargs in calls]
        == [
            ["/usr/bin/curl", "-fsSL", "--max-time", "30", "https://lmstudio.ai/install.sh"],
            ["/bin/sh", "-s", "--", "--quiet", "--no-modify-path"],
        ],
        "apply install uses the reviewed curl and shell vectors",
    )
    silent_home = root / "lm-studio-silent-apply-home"
    with patch.dict(os.environ, {"HOME": str(silent_home)}):
        silent = execute(
            module,
            request_factory(
                target,
                operation_id="install_lm_studio",
                operation_ref="setup::lm_studio.install",
                phase="apply",
                purpose=None,
                approval="sha256:" + "b" * 64,
                public_inputs=_VALID_INPUTS,
            ),
            _silent_install_runner(silent_home),
            lambda _name: None,
        )
    check(
        silent["checkpoint_status"] == "blocked"
        and silent["error_kind"] == "lm_studio_installer_did_not_run"
        and silent["evidence"][0]["id"] == "lm_studio_installer_outcome",
        "a silent zero-exit installer is distinct from CLI unavailability (red: remove silence guard)",
    )
