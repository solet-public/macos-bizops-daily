"""Report-only census of the host-shared LM Studio login job."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable
from pathlib import Path

from .doctor_advisory_result import advisory_unknown, advisory_verified, advisory_warn
from .lm_studio_diagnostics import selected_lm_studio_roles
from .models import InstanceRecord, JsonValue

type LoginReader = Callable[[], tuple[int, str, str]]
_LABEL = "local.solet.lm-studio"


def _login_state() -> tuple[int, str, str]:
    try:
        completed = subprocess.run(("/bin/launchctl", "print", f"gui/{os.getuid()}/{_LABEL}"), capture_output=True, text=True, check=False, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        return -1, "", str(exc)
    return completed.returncode, completed.stdout, completed.stderr


def collect_lm_studio_advisories(record: InstanceRecord, *, reader: LoginReader | None = None) -> list[JsonValue]:
    if not selected_lm_studio_roles(Path(record.target)):
        return []
    code, stdout, stderr = (reader or _login_state)()
    expected: dict[str, JsonValue] = {"label": _LABEL, "loaded": True}
    observed: dict[str, JsonValue] = {"exit_code": code}
    arguments = ("doctor::lm_studio_login_agent", "LM Studio shared login job", expected, observed, "launchctl print")
    if code == 0 and "state =" in stdout and _LABEL in stdout:
        return [advisory_verified(*arguments)]
    if code != -1 and "Could not find service" in stderr:
        return [advisory_warn(*arguments, "lm_studio_login_agent_absent", "The shared login job is absent. Incomplete setup can resume with solet create; completed hosts require restoring the named host condition, then solet doctor.")]
    return [advisory_unknown(*arguments, "lm_studio_login_agent_unknown", "launchctl did not provide a readable job state.")]
