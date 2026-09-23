"""Regression helper for public diagnostics at the setup-command boundary."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome


def run_genesis_diagnostic_regression(
    *,
    target: Path,
    runtime: Any,
    raw_request: Callable[..., dict[str, object]],
    handlers: dict[str, Callable[[AdapterRequest, Any], JsonObject]],
    check: Callable[[object, str], None],
) -> None:
    """Pin redaction through the real marker-less genesis adapter failure path."""

    diagnostic_target = target / "genesis-diagnostic-loss"
    python = diagnostic_target / ".venv" / "bin" / "python3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    secret_values = (
        "client-secret-should-not-escape",
        "access-token-should-not-escape",
        "bearer-token-should-not-escape",
        "password-should-not-escape",
        "api-key-should-not-escape",
        "credential-should-not-escape",
    )
    runtime.responses[(str(python), "-m", "github_midwife_plugin.genesis")] = CommandOutcome(
        returncode=1,
        timed_out=False,
        duration_ms=47_792,
        stdout="",
        stderr=(
            "FATAL: genesis failed: stale macOS Keychain vault state\n"
            f"client_secret={secret_values[0]}\n"
            f"access_token: {secret_values[1]}\n"
            f"Authorization: Bearer {secret_values[2]}\n"
            f"password={secret_values[3]}\n"
            f"api-key: {secret_values[4]}\n"
            f"credential={secret_values[5]}\n"
        ),
    )
    request = AdapterRequest.from_dict(
        raw_request(
            diagnostic_target,
            operation_id="run_genesis",
            operation_ref="genesis::solet.run",
            phase="apply",
            purpose=None,
            public_inputs={"autostart": "disabled", "setup_profile": "fixture"},
        )
    )
    failed = handlers[request.operation_ref](request, runtime)
    reason = cast(JsonObject, failed["reason"])
    check(
        all(value not in str(reason["stderr_diagnostic"]) for value in secret_values),
        "marker-less genesis failure redacts every credential-shaped stderr value",
    )
    check(
        all(
            expected in str(reason["stderr_diagnostic"])
            for expected in (
                "client_secret=<redacted>",
                "access_token: <redacted>",
                "Authorization: Bearer <redacted>",
                "password=<redacted>",
                "api-key: <redacted>",
                "credential=<redacted>",
            )
        ),
        "marker-less genesis failure preserves labels while redacting values",
    )
    check(
        "stale macOS Keychain vault state" in str(reason["stderr_diagnostic"]),
        "marker-less genesis failure retains its public diagnostic",
    )
    check(
        not (diagnostic_target / "profile/data/github_midwife/attempt.json").exists()
        and not (diagnostic_target / ".solet/genesis.json").exists(),
        "real failed genesis adapter path has no target-local marker to replace stderr",
    )
