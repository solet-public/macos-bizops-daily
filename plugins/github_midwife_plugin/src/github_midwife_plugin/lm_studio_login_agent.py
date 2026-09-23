"""Idempotent host-shared LM Studio login registration, never solet-owned."""

from __future__ import annotations

import json
import os
import plistlib
import shlex
from pathlib import Path

from .lm_studio_models import ModelArtifact, cli_path, contained_regular_file
from .lm_studio_settings import settings_path
from .setup_adapter_runtime import Runtime

LABEL = "local.solet.lm-studio"


def login_paths(home: Path) -> tuple[Path, Path]:
    return (
        home / "Library/LaunchAgents" / f"{LABEL}.plist",
        home / "Library/Application Support/Solet/LM Studio/start.sh",
    )


def render_login_agent(home: Path, models: dict[str, ModelArtifact]) -> tuple[str, str]:
    """Render stable host paths and a fixed-vector helper independent of any target.

    Every solet renders identical bytes. Models already provisioned by another
    solet remain eligible on later embedding-only installs. No teardown API is
    provided: operator ruling rul_5d2b3a95 makes this shared host infrastructure.
    """

    _, helper = login_paths(home)
    lms = str(cli_path(home))
    read_jit = shlex.join(("/usr/bin/plutil", "-extract", "justInTimeModelLoading", "raw", "-expect", "bool", "-o", "-", str(settings_path(home))))
    require_jit_off = f'[ "$({read_jit})" = false ]'
    lines = ["#!/bin/sh", "set -eu", *_loaded_state_function(), require_jit_off, shlex.join((lms, "daemon", "up")), shlex.join((lms, "server", "start", "--port", "1234", "--bind", "127.0.0.1")), require_jit_off]
    for role in ("embeddings", "inference"):
        model = models[role]
        artifact = model.path(home)
        source = {
            "type": "huggingface",
            "owner": model.repository.split("/", 1)[0],
            "repo": model.repository.split("/", 1)[1],
            "file": model.filename,
        }
        validation_argv = shlex.join(
            (
                str(home),
                str(artifact),
                str(home / ".lmstudio/.internal/model-data.json"),
                str(model.size_bytes),
                f"{model.repository}/{model.filename}",
                json.dumps(source, sort_keys=True, separators=(",", ":")),
            )
        )
        identifier = shlex.quote(model.api_identifier)
        lines.extend(
            (
                f"model_artifact_present {validation_argv} || exit 1",
                f"state=$(model_state {identifier}) || exit 1",
                'case "$state" in',
                "  loaded) ;;",
                "  not-loaded)",
                "    " + shlex.join((lms, *model.load_argv)),
                f"    state=$(model_state {identifier}) || exit 1",
                '    [ "$state" = loaded ] || exit 1',
                "    ;;",
                "  *) exit 1 ;;",
                "esac",
            )
        )
    lines.extend(("attempt=0", "while [ \"$attempt\" -lt 30 ]; do", "  if /usr/bin/curl --fail --silent --max-time 2 http://127.0.0.1:1234/v1/models >/dev/null; then exit 0; fi", "  attempt=$((attempt + 1))", "  /bin/sleep 1", "done", "exit 1", ""))
    plist = {
        "Label": LABEL,
        "ProgramArguments": ["/bin/sh", str(helper)],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 30,
        "AbandonProcessGroup": True,
        "ProcessType": "Background",
        "EnvironmentVariables": {"HOME": str(home), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
        "StandardOutPath": str(home / ".lmstudio/solet-startup.stdout.log"),
        "StandardErrorPath": str(home / ".lmstudio/solet-startup.stderr.log"),
    }
    return plistlib.dumps(plist, sort_keys=True).decode(), "\n".join(lines)


def _loaded_state_function() -> tuple[str, ...]:
    """Render bounded collection reads plus exact local artifact validation."""

    collection_parser = "\n".join(
        (
            "import json",
            "import sys",
            "identifier = sys.argv[1]",
            "payload = json.load(sys.stdin)",
            "rows = payload.get('data') if isinstance(payload, dict) else None",
            "if not isinstance(rows, list): raise ValueError('LM Studio collection data is invalid')",
            "states = {}",
            "for row in rows:",
            "    model_id = row.get('id') if isinstance(row, dict) else None",
            "    state = row.get('state') if isinstance(row, dict) else None",
            "    if not isinstance(model_id, str) or not model_id or model_id in states or state not in ('loaded', 'not-loaded'):",
            "        raise ValueError('LM Studio collection row is invalid')",
            "    states[model_id] = state",
            "print(states.get(identifier, 'not-loaded'))",
        )
    )
    artifact_parser = "\n".join(
        (
            "import json",
            "import sys",
            "from pathlib import Path",
            "home, artifact, metadata = (Path(value) for value in sys.argv[1:4])",
            "size, key, source = int(sys.argv[4]), sys.argv[5], json.loads(sys.argv[6])",
            "artifact.relative_to(home)",
            "if not artifact.is_file() or any(part.is_symlink() for part in (artifact, *artifact.parents) if part != home and home in part.parents): raise ValueError('LM Studio artifact is unsafe')",
            "if artifact.stat().st_size != size or artifact.open('rb').read(4) != b'GGUF': raise ValueError('LM Studio artifact is incomplete')",
            "payload = json.loads(metadata.read_text(encoding='utf-8'))",
            "rows = payload.get('json') if isinstance(payload, dict) else None",
            "matching = [row for row in rows if isinstance(row, list) and len(row) == 2 and row[0] == key] if isinstance(rows, list) else []",
            "if len(matching) != 1 or not isinstance(matching[0][1], dict) or matching[0][1].get('source') != source: raise ValueError('LM Studio artifact provenance is invalid')",
        )
    )
    collection_command = shlex.join(("/usr/bin/python3", "-c", collection_parser))
    artifact_command = shlex.join(("/usr/bin/python3", "-c", artifact_parser))

    return (
        "model_artifact_present() {",
        f"  {artifact_command} \"$@\"",
        "}",
        "model_state() {",
        '  response=$(/usr/bin/curl --fail --silent --show-error --max-time 2 --max-filesize 1048576 "http://127.0.0.1:1234/api/v0/models") || return 1',
        f'  printf %s "$response" | {collection_command} "$1"',
        "}",
    )


def login_definition_current(home: Path, models: dict[str, ModelArtifact]) -> bool:
    expected = render_login_agent(home, models)
    for path, content, mode in zip(login_paths(home), expected, (0o644, 0o700), strict=True):
        if not contained_regular_file(path, home):
            return False
        try:
            if path.read_text(encoding="utf-8") != content or path.stat().st_mode & 0o777 != mode:
                return False
        except (OSError, UnicodeError):
            return False
    return True


def login_loaded(runtime: Runtime) -> bool | None:
    """Distinguish an absent job from an unreadable launchctl state."""

    outcome = runtime.run(("/bin/launchctl", "print", f"gui/{os.getuid()}/{LABEL}"), timeout_seconds=5)
    if outcome.ok:
        return True if "state =" in outcome.stdout and LABEL in outcome.stdout else None
    if not outcome.timed_out and "Could not find service" in outcome.stderr and not outcome.stderr_truncated:
        return False
    return None


def login_classification(runtime: Runtime, models: dict[str, ModelArtifact]) -> str:
    plist, helper = login_paths(runtime.home)
    loaded = login_loaded(runtime)
    if loaded is None:
        return "unknown"
    if not plist.exists() and not helper.exists() and not loaded:
        return "absent"
    if not login_definition_current(runtime.home, models):
        return "present_but_stale"
    return "present_already_current" if loaded else "present_not_loaded"


def install_login_agent(runtime: Runtime, models: dict[str, ModelArtifact]) -> bool:
    """Upsert and enable the singleton without unload or per-solet ownership."""

    classification = login_classification(runtime, models)
    if classification == "unknown":
        return False
    if classification == "present_already_current":
        return True
    if classification == "present_but_stale" and login_loaded(runtime) is True:
        # Replacing a loaded definition needs host-level coordination. Never
        # unload a shared job merely because this solet's setup runs again.
        return False
    _write_login_files(runtime, models)
    domain = f"gui/{os.getuid()}"
    if not runtime.run(("/bin/launchctl", "enable", f"{domain}/{LABEL}"), timeout_seconds=10).ok:
        return False
    if not runtime.run(("/bin/launchctl", "bootstrap", domain, str(login_paths(runtime.home)[0])), timeout_seconds=10).ok:
        return False
    return login_loaded(runtime) is True


def _write_login_files(runtime: Runtime, models: dict[str, ModelArtifact]) -> None:
    for path, content, mode in zip(login_paths(runtime.home), render_login_agent(runtime.home, models), (0o644, 0o700), strict=True):
        if not path.is_file() or path.read_text(encoding="utf-8") != content or path.stat().st_mode & 0o777 != mode:
            runtime.atomic_write(path, content, mode=mode)
