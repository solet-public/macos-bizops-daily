"""Setup acquires the pinned Core AI asset, and never reports an unverified success.

A small real archive stands in for the 277 MB release. The pins are patched at
the same seams the acquisition unit tests use, the download is served by an
injected opener, and ``urlopen`` is replaced for the whole run so no case can
reach the network.

Run directly::

    .venv/bin/python3 plugins/github_midwife_plugin/tests/apple_coreai_acquisition_smoke.py
"""

from __future__ import annotations

import hashlib
import io
import json
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import cast
from unittest.mock import patch
from urllib.error import URLError
from urllib.request import Request

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "coreai_embeddings_plugin" / "src"))

from coreai_embeddings_plugin.assets import acquisition, archive  # noqa: E402
from github_midwife_plugin import apple_setup_adapter as apple  # noqa: E402
from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest, JsonObject  # noqa: E402
from github_midwife_plugin.setup_operations import coreai_autostart_deferral  # noqa: E402

_REFERENCE = "setup::apple.acquire_coreai_asset"
_CHECKS = 0


def _check(condition: object, label: str) -> None:
    global _CHECKS
    _CHECKS += 1
    if not condition:
        raise AssertionError(label)


class _Response:
    def __init__(self, data: bytes, *, status: int = 200) -> None:
        self.status = status
        self.headers: Mapping[str, str] = {"Content-Length": str(len(data))}
        self._stream = io.BytesIO(data)

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None

    def read(self, size: int = -1) -> bytes:
        return self._stream.read(size)


class _Opener:
    """Serve the fixture archive, or fail the way a broken network does."""

    def __init__(self, archive_bytes: bytes) -> None:
        self.archive_bytes = archive_bytes
        self.requests: list[str] = []
        self.failure: str | None = None

    def __call__(self, request: Request, timeout: int) -> _Response:
        self.requests.append(request.full_url)
        _check(timeout == 30, "acquisition keeps its bounded socket timeout")
        if self.failure == "offline":
            raise URLError("fixture network unreachable")
        if self.failure == "http":
            return _Response(b"", status=503)
        return _Response(self.archive_bytes)


class _NoHost:
    """A runtime that refuses every host command; acquisition needs none."""

    home = Path("/nonexistent-home")

    def run(self, argv: tuple[str, ...], **_: object) -> object:
        raise AssertionError(f"acquisition ran a host command: {argv}")

    def http_json(self, url: str, **_: object) -> object:
        raise AssertionError(f"acquisition called an endpoint: {url}")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        raise AssertionError(f"acquisition wrote through the runtime: {path}")


def _request(target: Path, phase: str, reference: str = _REFERENCE) -> AdapterRequest:
    return AdapterRequest(
        request_id=str(uuid.uuid4()),
        operation_id="acquire_coreai_asset",
        operation_ref=reference,
        phase=phase,
        probe_purpose="pre_apply" if phase == "probe" else None,
        attempt=1,
        name="coreai-acquire",
        target=target,
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=phase == "probe",
        timeout_seconds=30,
        public_inputs={},
    )


def _status(value: JsonObject) -> tuple[str, object]:
    return cast(str, value["checkpoint_status"]), value.get("error_kind")


@contextmanager
def _fixture_pins(root: Path) -> Iterator[tuple[bytes, dict[str, object]]]:
    """Patch the pins to a small real archive and yield it with its v1 manifest."""
    contents = {
        "model.aimodel/main.mlirb": b"fixture-graph-bytes",
        "model.aimodel/main.hash": b"fixture-graph-hash",
        "model.aimodel/metadata.json": b'{"license":"Apache-2.0"}',
        "tokenizer.json": b'{"tokens":["a","b"]}',
    }
    pins = tuple(
        acquisition.FilePin(pin.relative_path, pin.release_name, hashlib.sha256(contents[pin.relative_path]).hexdigest(), len(contents[pin.relative_path]))
        for pin in acquisition.FILE_PINS
    )
    with ExitStack() as stack:
        for module in (acquisition, archive):
            stack.enter_context(patch.object(module, "FILE_PINS", pins))
        model, tokenizer = root / "source.aimodel", root / "source-tokenizer.json"
        model.mkdir()
        for relative, data in contents.items():
            (tokenizer if relative == "tokenizer.json" else model / Path(relative).name).write_bytes(data)
        stream = io.BytesIO()
        archive.stream_archive(model, tokenizer, stream)
        archive_bytes = stream.getvalue()
        archive_pin = acquisition.FilePin("archive", "fixture-nomic.tar", hashlib.sha256(archive_bytes).hexdigest(), len(archive_bytes))
        stack.enter_context(patch.object(acquisition, "ARCHIVE_PIN", archive_pin))
        manifest: dict[str, object] = {
            "schema_version": 1, "model_id": acquisition.MODEL_ID, "source_url": acquisition.SOURCE_URL,
            "upstream_revision": acquisition.UPSTREAM_REVISION, "variant": acquisition.VARIANT,
            "license": acquisition.LICENSE, "release_repository": acquisition.RELEASE_REPOSITORY,
            "release_tag": "fixture-model-v1",
            "archive": {"release_name": archive_pin.release_name, "sha256": archive_pin.sha256, "size_bytes": archive_pin.size_bytes},
            "files": {
                pin.relative_path: {"sha256": pin.sha256, "size_bytes": pin.size_bytes}
                for pin in (*pins, acquisition.LICENSE_PIN, acquisition.NOTICE_PIN)
            },
        }
        yield archive_bytes, manifest


def _seed_target(target: Path, manifest: dict[str, object]) -> None:
    """Install the pinned declaration and a Core AI roster, as Genesis leaves them."""
    source = target / apple._ASSET_MANIFEST
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps(manifest, sort_keys=True), encoding="utf-8")
    roster = target / "profile/config/manifest.yaml"
    roster.parent.mkdir(parents=True)
    roster.write_text("plugins:\n  - coreai_embeddings_plugin\n", encoding="utf-8")


def _flow_contract() -> None:
    flow = json.loads((_ROOT / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json").read_text())
    operation = flow["operations"]["acquire_coreai_asset"]
    precedent = flow["operations"]["pull_lm_studio_embedding_model"]
    models = flow["stages"]["models"]["operation_refs"]
    _check(
        operation["operation_ref"] == _REFERENCE and operation["runner"] == "hydration"
        and operation["required_when"] == {"decision_ref": "embeddings_implementation", "operator": "equals", "value": "coreai"},
        "acquisition runs only on the Core AI branch through the target adapter",
    )
    _check(
        (operation["requires_confirmation"], operation["consent_refs"])
        == (precedent["requires_confirmation"], precedent["consent_refs"]),
        "acquisition shares the LM Studio model download's confirmation and consent",
    )
    _check(
        operation["idempotency"] == {"mode": "probe_then_apply", "precondition_probe_refs": ["coreai_asset_verified"], "postcondition_probe_refs": ["coreai_asset_verified"]}
        and "acquire_coreai_asset" in flow["probes"]["coreai_asset_verified"]["remediation_operation_refs"],
        "a blocked asset probe remediates through acquisition instead of stopping it",
    )
    _check(
        models.index("acquire_coreai_asset") < models.index("configure_coreai_embeddings") < models.index("install_launchagent"),
        "acquisition precedes Core AI configuration and the LaunchAgent",
    )
    component = flow["components"]["coreai_nomic_asset"]
    _check(
        component["lifecycle"] == "managed_by_setup" and component["install_operation_refs"] == ["acquire_coreai_asset"],
        "the pinned asset component is managed by setup",
    )
    _check(_REFERENCE in setup_adapter_handlers(), "the adapter registry routes the acquisition reference")


def setup_adapter_handlers() -> dict[str, object]:
    from github_midwife_plugin.setup_operations import operation_handlers

    return cast(dict[str, object], operation_handlers())


def _failure_matrix(target: Path, opener: _Opener) -> None:
    runtime = _NoHost()
    _check(_status(apple.acquire_coreai_asset(_request(target, "probe"), runtime)) == ("pending", None), "an absent asset is a planned download")
    _check(not opener.requests and not (target / "profile/data/model-assets").exists(), "the probe phase never downloads")
    _check(coreai_autostart_deferral(target) is not None, "the LaunchAgent is held before acquisition")
    for failure in ("offline", "http"):
        opener.failure = failure
        outcome = apple.acquire_coreai_asset(_request(target, "apply"), runtime, opener=opener)
        _check(_status(outcome) == ("blocked", "coreai_asset_download_failed"), f"{failure} download is blocked, never success")
        _check(_status(apple.asset_verified(_request(target, "probe"), runtime))[0] == "blocked", f"{failure} leaves the asset unverified")
        _check(coreai_autostart_deferral(target) is not None, f"{failure} keeps the LaunchAgent held")
    opener.failure = None
    with patch.object(acquisition, "urlopen", side_effect=URLError("offline host")):
        dispatched = setup_adapter.dispatch_request(_request(target, "apply"), runtime)
    _check(_status(dispatched) == ("blocked", "coreai_asset_download_failed"), "the dispatched default opener reports offline as blocked")


def _success_and_reuse(target: Path, opener: _Opener) -> None:
    runtime = _NoHost()
    before = len(opener.requests)
    _check(_status(apple.acquire_coreai_asset(_request(target, "apply"), runtime, opener=opener)) == ("applied", None), "a fresh download applies")
    _check(len(opener.requests) == before + 1 and opener.requests[-1].endswith("/fixture-model-v1/fixture-nomic.tar"), "one download from the pinned release")
    _check(_status(apple.asset_verified(_request(target, "probe"), runtime))[0] == "verified", "the downloaded asset verifies against every pin")
    _check(coreai_autostart_deferral(target) is None, "the LaunchAgent is released only after verification")

    def forbidden(request: Request, timeout: int) -> _Response:
        raise AssertionError("a valid installed asset must be reused without network")

    for phase in ("probe", "apply"):
        _check(_status(apple.acquire_coreai_asset(_request(target, phase), runtime, opener=forbidden))[0] == "verified", f"{phase} reuses the valid asset")


def _corrupt_and_tampered(target: Path, opener: _Opener) -> None:
    runtime = _NoHost()
    tokenizer = target / "profile/data/model-assets/nomic-embed-text-v1.5/tokenizer.json"
    tokenizer.write_bytes(b"x" * tokenizer.stat().st_size)
    before = len(opener.requests)
    for phase in ("probe", "apply"):
        outcome = apple.acquire_coreai_asset(_request(target, phase), runtime, opener=opener)
        _check(_status(outcome) == ("blocked", "coreai_asset_corrupt"), f"{phase} refuses a corrupt installed asset")
    _check(len(opener.requests) == before and tokenizer.read_bytes() == b"x" * tokenizer.stat().st_size, "a corrupt asset is neither fetched over nor repaired silently")
    _check(coreai_autostart_deferral(target) is not None, "a corrupt asset holds the LaunchAgent again")
    manifest = target / apple._ASSET_MANIFEST
    manifest.write_text(manifest.read_text(encoding="utf-8").replace("fixture-model-v1", "other-tag-v1"), encoding="utf-8")
    tokenizer.parent.parent.joinpath("nomic-embed-text-v1.5").rename(tokenizer.parent.parent / "quarantined")
    outcome = apple.acquire_coreai_asset(_request(target, "apply"), runtime, opener=opener)
    _check(_status(outcome) == ("blocked", "coreai_asset_manifest_invalid") and len(opener.requests) == before, "a tampered declaration cannot redirect the download")


def main() -> int:
    _flow_contract()
    with tempfile.TemporaryDirectory() as raw, patch.object(acquisition, "urlopen", side_effect=AssertionError("real network")):
        root = Path(raw)
        with _fixture_pins(root) as (archive_bytes, manifest):
            target = root / "target"
            _seed_target(target, manifest)
            digest = hashlib.sha256((target / apple._ASSET_MANIFEST).read_bytes()).hexdigest()
            opener = _Opener(archive_bytes)
            with patch.object(apple, "_ASSET_MANIFEST_SHA256", digest):
                _failure_matrix(target, opener)
                _success_and_reuse(target, opener)
                _corrupt_and_tampered(target, opener)
    print(f"apple_coreai_acquisition_smoke OK: {_CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
