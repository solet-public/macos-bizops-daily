"""Red-first Manager-seam controls for verified Qwen indexing and exact IDs."""

from __future__ import annotations

import errno
import json
import os
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/src"))
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/tests"))

from github_midwife_plugin import lm_studio_deadline, lm_studio_models, lm_studio_provisioning  # noqa: E402
from github_midwife_plugin.lm_studio_deadline import ServedDeadline  # noqa: E402
from github_midwife_plugin.lm_studio_index import LOCAL_SOURCE_REF_KEY, VerifiedLocalSource  # noqa: E402
from github_midwife_plugin.lm_studio_models import artifact_present, cli_path  # noqa: E402
from github_midwife_plugin.setup_adapter import LocalSourceResolver, dispatch_request  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject, JsonValue  # noqa: E402
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from lm_studio_provisioning_smoke import FixtureRuntime, outcome, request, write_artifact  # noqa: E402

INDEX_REF = "setup::lm_studio.ensure_index_inference"
INDEX_PROBE = "setup::lm_studio.inference_model_indexed"
PULL_REF = "setup::lm_studio.pull_inference"
LOAD_REF = "setup::lm_studio.load_inference"
SERVED_PROBE = "setup::lm_studio.inference_model_served"
MODEL_KEY = "solet-verified/qwen3-14b@q4_k_m"
ALIAS_RELATIVE = "solet-verified/Qwen3-14B/Qwen3-14B-Q4_K_M.gguf"


class IndexRuntime(FixtureRuntime):
    def __init__(self, home: Path) -> None:
        super().__init__(home)
        self.server = True
        self.index_rows: list[JsonObject] = []
        self.loaded_by_id: dict[str, str] = {}
        self.other_loaded_rows: list[JsonObject] = []
        self.index_payload: str | None = None
        self.import_error: str | None = None
        self.import_dry_run_error: str | None = None
        self.import_without_row = False
        self.change_after_dry_run = False
        self.change_after_index_read = False
        self.dry_run_text: str | None = None
        self.import_count = 0
        self.load_count = 0
        binary = cli_path(home)
        binary.parent.mkdir(parents=True, exist_ok=True)
        binary.write_text("fixture executable\n")
        binary.chmod(0o700)
        write_artifact(home, self.models["inference"])

    @property
    def source(self) -> Path:
        return self.models["inference"].path(self.home)

    @property
    def alias(self) -> Path:
        return self.home / ".lmstudio/models" / ALIAS_RELATIVE

    def _row(self, *, key: str = MODEL_KEY, path: str = ALIAS_RELATIVE) -> JsonObject:
        return {"type": "llm", "modelKey": key, "path": path, "sizeBytes": self.models["inference"].size_bytes}

    def _lms(self, args: tuple[str, ...], timeout: int) -> CommandOutcome:
        del timeout
        if args == ("ls", "--llm", "--json"):
            return self._list_models()
        if args == ("ps", "--json"):
            return self._loaded_models()
        if args and args[0] == "import":
            return self._import_model(args)
        return self._load_model(args)

    def _list_models(self) -> CommandOutcome:
        payload = self.index_payload if self.index_payload is not None else json.dumps(self.index_rows)
        if self.change_after_index_read:
            self.source.write_bytes(self.source.read_bytes()[:-1] + b"X")
            self.change_after_index_read = False
        return outcome(text=payload)

    def _loaded_models(self) -> CommandOutcome:
        rows = [*self.other_loaded_rows, *({**self._row(), "identifier": identifier, "modelKey": key} for identifier, key in self.loaded_by_id.items())]
        return outcome(text=json.dumps(rows))

    def _import_model(self, args: tuple[str, ...]) -> CommandOutcome:
        assert str(self.source) in args and "--hard-link" in args and "--user-repo" in args, args
        assert "solet-verified/Qwen3-14B" in args, args
        if "--dry-run" in args:
            return self._dry_run_model()
        if self.import_error is not None:
            return outcome(code=1, error=self.import_error)
        self.alias.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.link(self.source, self.alias)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                return outcome(code=1, error="cross-device hard link")
            raise
        self.import_count += 1
        if not self.import_without_row:
            self.index_rows.append(self._row())
        return outcome()

    def _dry_run_model(self) -> CommandOutcome:
        if self.import_dry_run_error is not None:
            return outcome(code=1, error=self.import_dry_run_error)
        if self.change_after_dry_run:
            self.source.write_bytes(self.source.read_bytes()[:-1] + b"X")
            self.change_after_dry_run = False
        text = self.dry_run_text or f"Would create a hard link to {self.alias}\nBut not actually doing it because of --dry-run\n"
        return outcome(error=text)

    def _load_model(self, args: tuple[str, ...]) -> CommandOutcome:
        assert args and args[0] == "load", args
        assert args == ("load", MODEL_KEY, "--gpu", "off", "--context-length", "8192", "--identifier", "qwen3-14b", "--yes"), args
        self.load_count += 1
        self.loaded_by_id["qwen3-14b"] = MODEL_KEY
        return outcome()

    def http_json(self, url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        assert timeout_seconds <= 2 and payload is None
        if url.endswith("/api/v0/models"):
            return 200, {"data": [{"id": identifier, "state": "unexpected" if self.unknown else "loaded"} for identifier in self.loaded_by_id]}
        return super().http_json(url, timeout_seconds=timeout_seconds, payload=payload)


def call(runtime: IndexRuntime, reference: str, *, phase: str = "probe") -> JsonObject:
    with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
        return dispatch_request(request(reference, phase=phase), runtime)


def refused(runtime: IndexRuntime, reference: str = INDEX_REF) -> None:
    result = call(runtime, reference, phase="apply")
    assert result["error_kind"] not in {"adapter_missing", "adapter_protocol_error"}, result
    assert result["checkpoint_status"] in {"blocked", "failed", "pending"}, result
    assert result["timed_out"] is False, result
    assert runtime.import_count == 0 and runtime.load_count == 0


def check_positive_and_restart(runtime: IndexRuntime) -> None:
    _check_positive_index(runtime)
    _check_positive_load(runtime)
    _check_repeat_and_restart(runtime)


def _check_positive_index(runtime: IndexRuntime) -> None:
    pending = call(runtime, INDEX_REF)
    assert pending["checkpoint_status"] == "pending" and pending["planned_actions"], pending
    assert str(runtime.source) in str(pending["planned_actions"])
    assert str(runtime.alias) in str(pending["planned_actions"])
    indexed = call(runtime, INDEX_REF, phase="apply")
    assert indexed["checkpoint_status"] == "applied", indexed
    assert [item["id"] for item in indexed["evidence"]] == [
        "lm_studio_inference_index_binding",
        "lm_studio_index_command_0",
        "lm_studio_index_command_1",
        "lm_studio_index_command_2",
    ], indexed
    assert "file_identity=" in str(indexed["evidence"][0]), indexed
    assert runtime.import_count == 1
    assert runtime.source.stat().st_ino == runtime.alias.stat().st_ino
    assert call(runtime, INDEX_PROBE)["checkpoint_status"] == "verified"


def _check_positive_load(runtime: IndexRuntime) -> None:
    runtime.other_loaded_rows = [{"type": "embedding", "modelKey": "nomic-embed-text-v1.5", "path": "other/nomic.gguf", "sizeBytes": 7, "identifier": "nomic-embed-text-v1.5"}]
    assert call(runtime, LOAD_REF)["checkpoint_status"] == "pending"
    loaded = call(runtime, LOAD_REF, phase="apply")
    assert loaded["checkpoint_status"] == "applied", loaded
    before = len(runtime.commands)
    assert call(runtime, SERVED_PROBE)["checkpoint_status"] == "verified"
    assert [command[1:] for command in runtime.commands[before:]] == [("ls", "--llm", "--json"), ("ps", "--json"), ("ls", "--llm", "--json")]
    assert runtime.load_count == 1


def _check_repeat_and_restart(runtime: IndexRuntime) -> None:
    assert call(runtime, INDEX_REF, phase="apply")["checkpoint_status"] == "applied"
    assert call(runtime, LOAD_REF, phase="apply")["checkpoint_status"] == "applied"
    assert (runtime.import_count, runtime.load_count) == (1, 1)
    runtime.loaded_by_id.clear()  # daemon restart retains the durable index
    assert call(runtime, INDEX_PROBE)["checkpoint_status"] == "verified"
    assert call(runtime, LOAD_REF, phase="apply")["checkpoint_status"] == "applied"
    assert runtime.load_count == 2 and runtime.loaded_by_id == {"qwen3-14b": MODEL_KEY}


def check_source_controls(runtime: IndexRuntime) -> None:
    runtime.source.unlink()
    refused(runtime)
    write_artifact(runtime.home, runtime.models["inference"])
    runtime.source.write_bytes(runtime.source.read_bytes()[:-1])
    refused(runtime)
    write_artifact(runtime.home, runtime.models["inference"])
    runtime.source.write_bytes(runtime.source.read_bytes()[:-1] + b"X")
    refused(runtime)
    write_artifact(runtime.home, runtime.models["inference"])
    foreign = runtime.source.with_name("foreign.gguf")
    runtime.source.rename(foreign)
    runtime.source.symlink_to(foreign)
    refused(runtime)


def check_index_controls(runtime: IndexRuntime) -> None:
    runtime.index_payload = "{broken"
    refused(runtime)
    runtime.index_payload = None
    runtime.index_rows = [runtime._row(), runtime._row()]
    refused(runtime)
    runtime.index_rows = [runtime._row(path="../../escape.gguf")]
    refused(runtime)
    runtime.index_rows = [{**runtime._row(), "type": "embedding"}]
    refused(runtime)
    runtime.index_rows = []
    runtime.alias.parent.mkdir(parents=True, exist_ok=True)
    runtime.alias.write_bytes(b"GGUFwrong")
    refused(runtime)
    runtime.alias.unlink()
    runtime.import_without_row = True
    result = call(runtime, INDEX_REF, phase="apply")
    assert result["error_kind"] not in {"adapter_missing", "adapter_protocol_error"}, result
    assert result["checkpoint_status"] != "applied", result
    assert len(result["evidence"]) == 3, result


def check_drift_after_index_read(runtime: IndexRuntime) -> None:
    runtime.change_after_index_read = True
    changed = call(runtime, INDEX_REF, phase="apply")
    assert changed["error_kind"] == "lm_studio_index_source_changed", changed
    assert runtime.import_count == 0


def check_import_controls(runtime: IndexRuntime) -> None:
    runtime.import_dry_run_error = "dry-run unavailable"
    refused(runtime)
    runtime.import_dry_run_error = None
    runtime.dry_run_text = f"Would move the file to {runtime.alias}\nBut not actually doing it because of --dry-run\n"
    refused(runtime)
    runtime.dry_run_text = "Unrecognized import preview shape\n"
    refused(runtime)
    runtime.dry_run_text = None
    runtime.import_error = "cross-device hard link"
    refused(runtime)
    runtime.import_error = None
    with patch("os.link", side_effect=OSError(errno.EXDEV, "cross-device hard link")):
        refused(runtime)
    runtime.change_after_dry_run = True
    refused(runtime)


def check_local_handoff(runtime: IndexRuntime) -> None:
    model = runtime.models["inference"]
    (runtime.home / ".lmstudio/.internal/model-data.json").unlink()
    assert not artifact_present(model, runtime.home)
    revision = "local-review-revision-1"
    pull = request(PULL_REF, phase="apply")
    pull = replace(pull, public_inputs={**pull.public_inputs, LOCAL_SOURCE_REF_KEY: revision})
    refused_without_resolver = dispatch_request(pull, runtime)
    assert refused_without_resolver["error_kind"] == "lm_studio_local_source_invalid", refused_without_resolver
    source = VerifiedLocalSource(
        origin="user_approved_local",
        role="inference",
        repository=model.repository,
        filename=model.filename,
        api_identifier=model.api_identifier,
        expected_size_bytes=model.size_bytes,
        expected_sha256=model.sha256,
        observed_size_bytes=model.size_bytes,
        observed_sha256=model.sha256,
        canonical_source_path=runtime.source,
        staged_path=runtime.source,
        transaction_revision=revision,
        flow_revision=pull.flow_source_revision,
        approval_fingerprint=pull.approval_fingerprint,
    )

    class Resolver:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        def resolve(self, ref: str, received: object) -> VerifiedLocalSource:
            self.calls.append((ref, received.operation_ref))
            return source

    resolver = Resolver()
    with patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
        applied = dispatch_request(pull, runtime, local_source_resolver=resolver)
        assert applied["checkpoint_status"] == "applied", applied
        assert runtime.import_count == 0 and runtime.load_count == 0
        index = replace(pull, operation_ref=INDEX_REF)
        assert dispatch_request(index, runtime, local_source_resolver=resolver)["checkpoint_status"] == "applied"
        loaded = replace(pull, operation_ref=LOAD_REF, public_inputs={**pull.public_inputs, **request(LOAD_REF, phase="apply").public_inputs})
        assert dispatch_request(loaded, runtime, local_source_resolver=resolver)["checkpoint_status"] == "applied"
        assert runtime.import_count == 1 and runtime.load_count == 1
        _check_local_handoff_refusals(runtime, pull, source, resolver)
    assert resolver.calls == [(revision, PULL_REF), (revision, INDEX_REF), (revision, LOAD_REF), (revision, PULL_REF)]


def _check_local_handoff_refusals(
    runtime: IndexRuntime,
    pull: AdapterRequest,
    source: VerifiedLocalSource,
    resolver: LocalSourceResolver,
) -> None:
    stale = replace(source, approval_fingerprint="sha256:" + "d" * 64)

    class StaleResolver:
        def resolve(self, ref: str, received: AdapterRequest) -> VerifiedLocalSource:
            del ref, received
            return stale

    rejected = dispatch_request(pull, runtime, local_source_resolver=StaleResolver())
    assert rejected["error_kind"] == "lm_studio_local_source_invalid", rejected
    runtime.source.write_bytes(runtime.source.read_bytes()[:-1] + b"X")
    changed = dispatch_request(pull, runtime, local_source_resolver=resolver)
    assert changed["error_kind"] == "lm_studio_local_source_invalid", changed


def check_identifier_controls(runtime: IndexRuntime) -> None:
    assert call(runtime, INDEX_REF, phase="apply")["checkpoint_status"] == "applied"
    runtime.import_count = 0
    runtime.loaded_by_id[MODEL_KEY] = MODEL_KEY
    before = len(runtime.commands)
    conflict = call(runtime, LOAD_REF)
    assert conflict["error_kind"] == "identifier_conflict" and conflict["planned_actions"], conflict
    assert [command[1:] for command in runtime.commands[before:]] == [("ls", "--llm", "--json"), ("ps", "--json")]
    refused(runtime, LOAD_REF)
    runtime.loaded_by_id = {"qwen3-14b": "other/model@q4"}
    before = len(runtime.commands)
    refused(runtime, LOAD_REF)
    assert [command[1:] for command in runtime.commands[before:]] == [("ls", "--llm", "--json"), ("ps", "--json")]
    runtime.loaded_by_id.clear()
    runtime.other_loaded_rows = [{"type": "embedding", "modelKey": MODEL_KEY, "path": ALIAS_RELATIVE, "sizeBytes": runtime.models["inference"].size_bytes, "identifier": "another-id"}]
    refused(runtime, LOAD_REF)


def check_unknown_v0(runtime: IndexRuntime) -> None:
    assert call(runtime, INDEX_REF, phase="apply")["checkpoint_status"] == "applied"
    assert call(runtime, LOAD_REF, phase="apply")["checkpoint_status"] == "applied"
    runtime.unknown = True
    seen = [False]
    clock_reads = [0]
    original_http = runtime.http_json
    original_hash = lm_studio_models._hash_model_stream

    def now() -> int:
        if seen[0]:
            clock_reads[0] += 1
            return 1_024_900_000_000 if clock_reads[0] == 1 else 1_025_000_000_000
        return 1_000_000_000_000

    def http(url: str, *, timeout_seconds: int, payload: JsonObject | None = None) -> tuple[int, JsonValue]:
        response = original_http(url, timeout_seconds=timeout_seconds, payload=payload)
        if url.endswith("/api/v0/models"):
            seen[0] = True
        return response

    def hash_stream(stream: BinaryIO, deadline: ServedDeadline | None) -> tuple[str, int]:
        assert not seen[0], "hash after malformed v0"
        return original_hash(stream, deadline)

    before = len(runtime.commands)
    with patch.object(lm_studio_deadline.time, "monotonic_ns", side_effect=now), patch.object(runtime, "http_json", side_effect=http), patch.object(lm_studio_models, "_hash_model_stream", side_effect=hash_stream), patch.object(lm_studio_provisioning, "reviewed_models", return_value=runtime.models):
        checked = dispatch_request(replace(request(SERVED_PROBE, timeout=30), probe_purpose="stage_exit"), runtime)
    assert checked["checkpoint_status"] == "blocked" and checked["error_kind"] == "lm_studio_served_protocol_error", checked
    assert checked["retry_safe"] is False and clock_reads[0] >= 2
    assert [command[1:] for command in runtime.commands[before:]] == [("ls", "--llm", "--json"), ("ps", "--json")]
    assert any("terminal_class=lm_studio_served_protocol_error" in str(item.get("observed")) for item in checked["evidence"]), checked


def main() -> int:
    checks = (check_positive_and_restart, check_source_controls, check_index_controls, check_drift_after_index_read, check_import_controls, check_identifier_controls, check_unknown_v0, check_local_handoff)
    failures: list[str] = []
    for check in checks:
        with tempfile.TemporaryDirectory(prefix="lm-studio-index-") as temporary:
            runtime = IndexRuntime(Path(temporary))
            try:
                check(runtime)
            except (AssertionError, ValueError) as exc:
                failures.append(f"{check.__name__}: {exc}")
    if failures:
        print("lm_studio_index_smoke: RED", *failures, sep="\n")
        return 1
    print("lm_studio_index_smoke: deadline, index, exact identifier, restart and refusal controls passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
