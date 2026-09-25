"""Reviewed LM Studio artifact identities and passive host observations."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import stat
import time
import urllib.error
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Literal

from .lm_studio_deadline import ServedDeadline, call_budget, check_deadline
from .setup_adapter_contract import JsonObject, JsonValue, evidence
from .setup_adapter_runtime import Runtime

BASE_URL = "http://127.0.0.1:1234/v1"
WATCH_INTERVAL_SECONDS = 5
# The vendor CLI's own stall timer fired at 84 s (R44 fresh round 2).
STALL_WINDOW_SECONDS = 90
# Digest reserve: 9,001,753,376 bytes hash in ~3.6 s at the 2.5 GB/s measured
# for _hash_model_stream; 60 s still covers a 150 MB/s cold-read guest disk.
VERIFY_RESERVE_SECONDS = 60
MODEL_REGISTRY = Path("plugins/github_midwife_plugin/knowledge_base/profile_templates/lm_studio_models.yaml")


@dataclass(frozen=True)
class ModelArtifact:
    """One exact download and its independently checked served identifier."""

    role: str
    api_identifier: str
    repository: str
    filename: str
    size_bytes: int
    sha256: str
    get_argv: tuple[str, ...]
    load_argv: tuple[str, ...]

    def path(self, home: Path) -> Path:
        return home / ".lmstudio/models" / self.repository / self.filename


def reviewed_models(target: Path) -> dict[str, ModelArtifact]:
    """Reject registry drift without making pre-venv provisioning depend on PyYAML.

    The reviewed registry intentionally has a tiny, fixed surface.  The exact
    artifact values remain in this module, and the source file must retain each
    of their canonical YAML lines.  This is deliberately fail-closed: an edit
    to a reviewed value needs a corresponding code review here rather than
    becoming a new command merely because a general YAML parser accepted it.
    """

    try:
        registry_lines = tuple(
            line.strip()
            for line in (target / MODEL_REGISTRY).read_text(encoding="utf-8").splitlines()
        )
    except OSError as exc:
        raise ValueError("LM Studio model registry is unreadable") from exc
    expected = (
        (
            "embeddings",
            "text-embedding-nomic-embed-text-v1.5-embedding",
            "gaianet/Nomic-embed-text-v1.5-Embedding-GGUF",
            "nomic-embed-text-v1.5.f16.gguf",
            274290560,
            # https://huggingface.co/gaianet/Nomic-embed-text-v1.5-Embedding-GGUF/blob/main/nomic-embed-text-v1.5.f16.gguf
            "f7af6f66802f4df86eda10fe9bbcfc75c39562bed48ef6ace719a251cf1c2fdb",
            "",
        ),
        (
            "inference",
            "qwen3-14b",
            "lmstudio-community/Qwen3-14B-GGUF",
            "Qwen3-14B-Q4_K_M.gguf",
            9001753376,
            # https://huggingface.co/lmstudio-community/Qwen3-14B-GGUF/blob/main/Qwen3-14B-Q4_K_M.gguf
            "712c0791d5124d3dd6d1e4968de1201207afeae49c6e10fbeb9c58fe00c58555",
            "@Q4_K_M",
        ),
    )
    result: dict[str, ModelArtifact] = {}
    for role, identifier, repository, filename, size, sha256, suffix in expected:
        source = f"https://huggingface.co/{repository}{suffix}"
        get_argv = ("get", source, "--gguf", "--yes")
        load_argv = ("load", identifier, "--gpu", "off")
        if role == "inference":
            load_argv += ("--context-length", "8192")
        load_argv += ("--yes",)
        required_lines = (
            f"{role}:",
            f"api_identifier: {identifier}",
            f"download_source: {source}",
            f"download_file: {filename}",
            f"size_bytes: {size}",
            "get_argv: [" + ", ".join(get_argv) + "]",
            "load_argv: ["
            + ", ".join(
                f'"{item}"' if item in {"off", "8192"} else item for item in load_argv
            )
            + "]",
        )
        if any(registry_lines.count(line) != 1 for line in required_lines):
            raise ValueError(f"LM Studio {role} registry differs from the reviewed artifact")
        result[role] = ModelArtifact(role, identifier, repository, filename, size, sha256, get_argv, load_argv)
    return result


def cli_path(home: Path) -> Path:
    """Use the vendor location regardless of the caller's PATH."""

    return home / ".lmstudio/bin/lms"


def cli_available(home: Path) -> bool:
    path = cli_path(home)
    return contained_regular_file(path, home) and os.access(path, os.X_OK)


def contained_regular_file(path: Path, home: Path) -> bool:
    """Reject symlink traversal in the vendor-owned path beneath the home."""

    try:
        path.relative_to(home)
        return path.is_file() and not any(part.is_symlink() for part in (path, *path.parents) if part != home and home in part.parents)
    except (OSError, ValueError):
        return False


def artifact_present(model: ModelArtifact, home: Path, deadline: ServedDeadline | None = None) -> bool:
    """Require reviewed bytes and provenance, without following the file symlink."""

    path = model.path(home)
    if not contained_regular_file(path, home):
        return False
    check_deadline(deadline)
    current = verified_file_identity(model, path, home, deadline)
    if current is None:
        return False
    try:
        metadata: object = json.loads((home / ".lmstudio/.internal/model-data.json").read_text(encoding="utf-8"))
        return (
            _provenance_matches(metadata, model)
            and contained_regular_file(path, home)
            and _file_identity(current) == _file_identity(path.stat())
        )
    except (OSError, json.JSONDecodeError):
        return False


def _reviewed_file_identity(model: ModelArtifact, path: Path, deadline: ServedDeadline | None = None) -> os.stat_result | None:
    """Return the stable file identity only after full-byte digest verification."""

    try:
        check_deadline(deadline)
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size != model.size_bytes:
                return None
            digest, bytes_read = _hash_model_stream(stream, deadline)
            after = os.fstat(stream.fileno())
        if bytes_read != model.size_bytes:
            return None
        current = path.stat()
        if _file_identity(before) != _file_identity(after) or _file_identity(after) != _file_identity(current):
            return None
        if digest != model.sha256:
            return None
        check_deadline(deadline)
        return current
    except OSError:
        return None


def _hash_model_stream(stream: BinaryIO, deadline: ServedDeadline | None) -> tuple[str, int]:
    check_deadline(deadline)
    if stream.read(4) != b"GGUF":
        return "", 0
    stream.seek(0)
    digest = hashlib.sha256()
    bytes_read = 0
    while True:
        check_deadline(deadline)
        chunk = stream.read(1024 * 1024)
        check_deadline(deadline)
        if not chunk:
            break
        digest.update(chunk)
        bytes_read += len(chunk)
    return digest.hexdigest(), bytes_read


def verified_file_identity(model: ModelArtifact, path: Path, home: Path, deadline: ServedDeadline | None = None) -> os.stat_result | None:
    """Verify an indexed destination against the same reviewed bytes as pulls."""

    return _reviewed_file_identity(model, path, deadline) if contained_regular_file(path, home) else None


def _file_identity(item: os.stat_result) -> tuple[int, int, int, int, int]:
    return item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns


def _provenance_matches(metadata: object, model: ModelArtifact) -> bool:
    rows = metadata.get("json") if isinstance(metadata, dict) else None
    if not isinstance(rows, list):
        return False
    key = f"{model.repository}/{model.filename}"
    matching = [row for row in rows if isinstance(row, list) and len(row) == 2 and row[0] == key]
    if len(matching) != 1 or not isinstance(matching[0][1], dict):
        return False
    owner, repository = model.repository.split("/", 1)
    expected = {"type": "huggingface", "owner": owner, "repo": repository, "file": model.filename}
    return matching[0][1].get("source") == expected


def partial_bytes(model: ModelArtifact, home: Path) -> int:
    """Report the vendor's receipted resumable partial without changing it."""

    path = model.path(home).with_name(f"downloading_{model.filename}.part")
    return path.stat().st_size if contained_regular_file(path, home) else 0


def download_observation(model: ModelArtifact, home: Path) -> tuple[int, bool]:
    """Observe the daemon's transfer by stat only: partial size, final at size.

    The vendor renames the partial onto the final path, so either stat can
    race that rename. A vanished file reads as absent, never as an error.
    """

    try:
        partial = partial_bytes(model, home)
    except OSError:
        partial = 0
    return partial, final_settle_mark(model, home) is not None


def final_settle_mark(model: ModelArtifact, home: Path) -> tuple[int, int] | None:
    """Size and mtime of a contained final file at the reviewed size; no digest."""

    path = model.path(home)
    try:
        current = path.stat() if contained_regular_file(path, home) else None
    except OSError:
        return None
    if current is None or current.st_size != model.size_bytes:
        return None
    return current.st_size, current.st_mtime_ns


def provenance_recorded(model: ModelArtifact, home: Path) -> bool:
    """Cheap provenance read for settling; ``artifact_present`` stays the verdict."""

    try:
        metadata: object = json.loads((home / ".lmstudio/.internal/model-data.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return _provenance_matches(metadata, model)


def resumable_partial(model: ModelArtifact, home: Path) -> bool:
    partial, _ = download_observation(model, home)
    return 0 < partial < model.size_bytes


def rewatchable(model: ModelArtifact, home: Path, deadline: float) -> bool:
    """A second vendor timeout is watched again only over a valid partial, no final file."""

    final = model.path(home)
    if final.exists() or final.is_symlink():
        return False
    return resumable_partial(model, home) and deadline - time.monotonic() >= VERIFY_RESERVE_SECONDS


def transfer_active(model: ModelArtifact, home: Path) -> bool:
    """Sample a contained partial twice; only an existing partial costs a sleep.

    Only growth, or a final file that appeared during the sample, counts.
    """

    before, final_before = download_observation(model, home)
    if not 0 < before < model.size_bytes:
        return False
    time.sleep(WATCH_INTERVAL_SECONDS)
    after, final_after = download_observation(model, home)
    return after > before or (final_after and not final_before)


@dataclass
class DownloadWatch:
    """Stat-only record of the daemon's transfer; never a digest per tick."""

    started: float
    first_bytes: int
    last_bytes: int
    last_progress: float
    ticks: int = 0
    final_mark: tuple[int, int] | None = None
    exit: Literal["settled", "stalled", "reserve", "unsettled"] = "stalled"
    ended: float = field(default=0.0)

    @property
    def elapsed_ms(self) -> int:
        return int((self.ended - self.started) * 1000)

    def evidence(self) -> JsonObject:
        return evidence(
            evidence_id="lm_studio_download_watch",
            kind="filesystem",
            status="observed",
            summary=f"LM Studio daemon transfer watched after the CLI exit: exit={self.exit}; ticks={self.ticks}.",
            observed=[f"exit={self.exit}", f"ticks={self.ticks}", f"first_bytes={self.first_bytes}", f"last_bytes={self.last_bytes}", f"elapsed_ms={self.elapsed_ms}"],
            expected="final artifact settles at the reviewed size before the budget reserve",
            source="target_runtime_lm_studio_download_stat",
        )


def watch_download(model: ModelArtifact, home: Path, deadline: float) -> DownloadWatch:
    """Poll the daemon's files until settled, stalled, or the verify reserve."""

    now = time.monotonic()
    partial, _ = download_observation(model, home)
    watch = DownloadWatch(started=now, first_bytes=partial, last_bytes=partial, last_progress=now)
    while True:
        if deadline - now < VERIFY_RESERVE_SECONDS:
            watch.exit = "unsettled" if watch.final_mark is not None else "reserve"
            break
        time.sleep(WATCH_INTERVAL_SECONDS)
        now = time.monotonic()
        watch.ticks += 1
        if _watch_tick(model, home, watch, now):
            watch.exit = "settled"
            break
        if now - watch.last_progress >= STALL_WINDOW_SECONDS:
            watch.exit = "stalled"
            break
    watch.ended = now
    return watch


def _watch_tick(model: ModelArtifact, home: Path, watch: DownloadWatch, now: float) -> bool:
    """Record progress; true only for a final file stable across a tick with provenance."""

    partial, _ = download_observation(model, home)
    mark = final_settle_mark(model, home)
    if mark is None:
        # A tick with neither file present is the rename gap, not a failure.
        watch.final_mark = None
        if partial > watch.last_bytes:
            watch.last_bytes, watch.last_progress = partial, now
        return False
    if mark == watch.final_mark:
        return provenance_recorded(model, home)
    watch.final_mark, watch.last_bytes, watch.last_progress = mark, model.size_bytes, now
    return False


def served_models(runtime: Runtime, *, timeout_seconds: int = 2) -> tuple[str, ...] | None:
    """Observe server availability only: v1 includes unloaded models with JIT."""

    try:
        status, payload = runtime.http_json(f"{BASE_URL}/models", timeout_seconds=timeout_seconds)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(rows, list):
        return None
    identifiers: list[str] = []
    for row in rows:
        identifier = row.get("id") if isinstance(row, dict) else None
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            return None
        identifiers.append(identifier)
    return tuple(identifiers)


@dataclass(frozen=True)
class LoadedObservation:
    status: Literal["loaded", "absent", "transport_unknown", "protocol_error"]


def observe_loaded(runtime: Runtime, identifier: str, deadline: ServedDeadline | None = None) -> LoadedObservation:
    """Distinguish passive transport lag from malformed or conflicting protocol."""
    timeout = call_budget(deadline, 2)
    try:
        status, payload = runtime.http_json("http://127.0.0.1:1234/api/v0/models", timeout_seconds=timeout)
    except urllib.error.HTTPError:
        return LoadedObservation("protocol_error")
    except (OSError, urllib.error.URLError):
        check_deadline(deadline)
        return LoadedObservation("transport_unknown")
    except (ValueError, http.client.HTTPException):
        return LoadedObservation("protocol_error")
    check_deadline(deadline)
    rows = payload.get("data") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(rows, list):
        return LoadedObservation("protocol_error")
    states = _loaded_states(rows)
    if states is None:
        return LoadedObservation("protocol_error")
    return LoadedObservation("loaded" if states.get(identifier) == "loaded" else "absent")


def model_loaded(runtime: Runtime, identifier: str) -> bool | None:
    """Strict one-shot exact v0 loaded ID; never borrow v1 discovery/JIT."""
    observed = observe_loaded(runtime, identifier)
    return None if observed.status in {"transport_unknown", "protocol_error"} else observed.status == "loaded"


def _loaded_states(rows: list[JsonValue]) -> dict[str, str] | None:
    states: dict[str, str] = {}
    for row in rows:
        model_id = row.get("id") if isinstance(row, dict) else None
        state = row.get("state") if isinstance(row, dict) else None
        if not isinstance(model_id, str) or not model_id or model_id in states or state not in ("loaded", "not-loaded"):
            return None
        states[model_id] = str(state)
    return states


def selected_roles(inputs: dict[str, JsonValue]) -> tuple[str, ...]:
    """Select only the already-reviewed implementation decisions."""

    roles: list[str] = []
    for role, decision in (("embeddings", "embeddings_implementation"), ("inference", "inference_implementation")):
        value = inputs.get(decision)
        if not isinstance(value, str):
            raise ValueError(f"LM Studio input {decision} is unresolved")
        if value == "lm_studio":
            roles.append(role)
    return tuple(roles)


def embedding_observation(runtime: Runtime, model: ModelArtifact, deadline: ServedDeadline | None) -> str:
    check_deadline(deadline)
    if deadline is not None:
        deadline.attempts += 1
    observed = observe_loaded(runtime, model.api_identifier, deadline).status
    if observed == "loaded" and not artifact_present(model, runtime.home, deadline):
        observed = "protocol_error"
    if deadline is not None:
        deadline.terminal_class = observed
    check_deadline(deadline)
    return observed
