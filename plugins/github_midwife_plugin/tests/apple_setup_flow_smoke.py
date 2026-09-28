"""Fake-world proof for the Apple setup branch and its closed adapter."""

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import uuid
from enum import Enum
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from jsonschema import Draft7Validator

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT / "plugins" / "github_midwife_plugin" / "src"))
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))

from github_midwife_plugin import apple_setup_adapter as apple  # noqa: E402
from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import (  # noqa: E402
    AdapterRequest,
    JsonObject,
    JsonValue,
)
from github_midwife_plugin.setup_adapter_runtime import CommandOutcome  # noqa: E402
from solet_manager.condition_evaluator import condition_matches  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.plan_builder import selected_operation_ids  # noqa: E402


class FakeWorld:
    def __init__(self, target: Path) -> None:
        self.home = target / "home"
        self.version = "27.0"
        self.architecture = "arm64"
        self.model = "VirtualMac2,1"
        self.calls: list[tuple[str, ...]] = []
        self.writes: list[Path] = []
        self.process_outcomes: dict[str, CommandOutcome] = {}

    def run(
        self,
        argv: tuple[str, ...],
        *,
        timeout_seconds: int,
        cwd: Path | None = None,
        extra_env: dict[str, str] | None = None,
        input_text: str | None = None,
        output_limit: int = 4096,
    ) -> CommandOutcome:
        del timeout_seconds, cwd, extra_env, input_text, output_limit
        self.calls.append(argv)
        if len(argv) == 4 and argv[1] == "call":
            assert argv[2] in self.process_outcomes, f"unreviewed process: {argv[2]}"
            return self.process_outcomes[argv[2]]
        values: dict[tuple[str, ...], str] = {
            ("/usr/bin/sw_vers", "-productVersion"): self.version,
            ("/usr/bin/uname", "-m"): self.architecture,
            ("/usr/sbin/sysctl", "-n", "hw.model"): self.model,
        }
        assert argv in values, f"unreviewed command: {argv}"
        return CommandOutcome(0, False, 1, values[argv] + "\n", "")

    def http_json(
        self,
        url: str,
        *,
        timeout_seconds: int,
        payload: JsonObject | None = None,
    ) -> tuple[int, JsonValue]:
        raise AssertionError(f"Apple route attempted an endpoint: {url}")

    def atomic_write(self, path: Path, content: str, *, mode: int) -> None:
        assert mode == 0o600
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        self.writes.append(path)


def _request(target: Path, reference: str, phase: str = "probe") -> AdapterRequest:
    return AdapterRequest(
        request_id=str(uuid.uuid4()),
        operation_id="apple_test",
        operation_ref=reference,
        phase=phase,
        probe_purpose="pre_apply" if phase == "probe" else None,
        attempt=1,
        name="apple-test",
        target=target,
        flow_source_revision="a" * 40,
        answers_fingerprint="sha256:" + "b" * 64,
        approval_fingerprint=None,
        dry_run=phase == "probe",
        timeout_seconds=30,
        public_inputs={},
    )


def _status(value: JsonObject) -> str:
    return cast(str, value["checkpoint_status"])


def _digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _asset_fixture(target: Path) -> str:
    asset = target / "profile/data/model-assets/nomic-embed-text-v1.5"
    contents = {
        "model.aimodel/main.mlirb": b"small fake model",
        "model.aimodel/main.hash": b"fake hash",
        "model.aimodel/metadata.json": b"{}",
        "tokenizer.json": b"{}",
        "LICENSE-2.0.txt": b"fixture license",
        "NOTICE.txt": b"fixture notice",
    }
    pins: dict[str, dict[str, str | int]] = {}
    for relative, content in contents.items():
        path = asset / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        pins[relative] = {"sha256": _digest(content), "size_bytes": len(content)}
    manifest = {"model_id": "nomic-ai/nomic-embed-text-v1.5", "files": pins}
    source = target / apple._ASSET_MANIFEST
    source.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(manifest, sort_keys=True).encode()
    source.write_bytes(content)
    return _digest(content)


def _flow_branch() -> None:
    kb = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
    flow = json.loads((kb / "macos_setup_flow.json").read_text())
    schema = json.loads((kb / "setup_flow.schema.json").read_text())
    assert not list(Draft7Validator(schema).iter_errors(flow))
    _flow_options(flow)
    _flow_conditions(flow)
    _launchagent_ordering(flow)
    _selected_plan_branches(kb)


def _flow_options(flow: dict[str, Any]) -> None:
    embedding = flow["decisions"]["embeddings_implementation"]["option_source"]["options"]["coreai"]
    inference = flow["decisions"]["inference_implementation"]["option_source"]["options"]["apple_foundation_models"]
    for option in (embedding, inference):
        activated = json.dumps(option["activates"])
        assert "lm_studio" not in activated and "qwen" not in activated
        assert "openai_embeddings_plugin" not in activated
    assert flow["decisions"]["embedding_model"]["required_when"]["value"] == "lm_studio"
    assert flow["decisions"]["inference_model"]["required_when"]["value"] == "lm_studio"
    assert "apple_ai_attestation" in inference["activates"]["consent_refs"]


def _flow_conditions(flow: dict[str, Any]) -> None:
    for operation_id in ("configure_lm_studio_embeddings", "configure_lm_studio_inference"):
        assert flow["operations"][operation_id]["required_when"]["value"] == "lm_studio"
    assert "coreai_asset_verified" in flow["stages"]["models"]["exit_probe_refs"]
    assert "apple_model_availability" in flow["stages"]["models"]["exit_probe_refs"]
    _embedding_flow_gate(flow)


def _embedding_flow_gate(flow: dict[str, Any]) -> None:
    exits = flow["stages"]["models"]["exit_probe_refs"]
    assert exits.index("coreai_embedding_request_succeeds") > exits.index("router_ready")
    assert flow["probes"]["coreai_embedding_request_succeeds"]["required_when"]["value"] == "coreai"
    assert flow["probes"]["embedding_request_succeeds"]["required_when"]["value"] == "lm_studio"
    assert "coreai_embedding_request_succeeds" in flow["completion"]["required_probe_refs"]
    coreai = flow["decisions"]["embeddings_implementation"]["option_source"]["options"]["coreai"]
    assert "coreai_embedding_request_succeeds" in coreai["verification_probe_refs"]
    assert "embedding_request_succeeds" not in coreai["verification_probe_refs"]
    for selected, expected in (("coreai", True), ("lm_studio", False)):
        assert condition_matches(
            flow["probes"]["coreai_embedding_request_succeeds"]["required_when"],
            {"embeddings_implementation": selected},
        ) is expected
        assert condition_matches(
            flow["probes"]["embedding_request_succeeds"]["required_when"],
            {"embeddings_implementation": selected},
        ) is not expected


def _launchagent_ordering(flow: dict[str, Any]) -> None:
    """The asset probe precedes the LaunchAgent only on the Core AI branch."""
    stages = flow["stages"]
    assert stages["genesis"]["sequence"] < stages["models"]["sequence"]
    assert "run_genesis" in stages["genesis"]["operation_refs"]
    models = stages["models"]["operation_refs"]
    assert models.index("configure_coreai_embeddings") < models.index("install_launchagent")
    coreai = flow["operations"]["configure_coreai_embeddings"]
    assert coreai["required_when"] == {
        "decision_ref": "embeddings_implementation", "operator": "equals", "value": "coreai"
    }
    assert coreai["idempotency"]["precondition_probe_refs"] == ["coreai_asset_verified"]
    launchagent = flow["operations"]["install_launchagent"]
    assert "required_when" not in launchagent
    # Operation-owned probes run without their required_when, so a Core AI probe
    # here would block the LM Studio LaunchAgent; the adapter gate owns it instead.
    for key in ("precondition_probe_refs", "postcondition_probe_refs"):
        for probe_id in launchagent["idempotency"][key]:
            assert "coreai" not in json.dumps(flow["probes"][probe_id].get("required_when"))


def _selected_plan_branches(kb: Path) -> None:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=kb)
    selected = selected_operation_ids(bundle, {
        "setup_profile": "macos-bizops",
        "embeddings_implementation": "coreai",
        "inference_implementation": "apple_foundation_models",
    })
    assert {"configure_coreai_embeddings", "configure_apple_inference"} <= selected
    assert not any("lm_studio" in item for item in selected)
    legacy = selected_operation_ids(bundle, {
        "setup_profile": "custom",
        "embeddings_implementation": "lm_studio",
        "inference_implementation": "lm_studio",
    })
    assert {"configure_lm_studio_embeddings", "configure_lm_studio_inference"} <= legacy
    assert "configure_coreai_embeddings" not in legacy
    assert "configure_apple_inference" not in legacy
    for embeddings in ("coreai", "lm_studio"):
        autostarted = selected_operation_ids(bundle, {
            "setup_profile": "custom",
            "embeddings_implementation": embeddings,
            "inference_implementation": "lm_studio",
            "autostart": "enabled",
        })
        assert {"run_genesis", "install_launchagent"} <= autostarted, embeddings
        assert ("configure_coreai_embeddings" in autostarted) is (embeddings == "coreai")


def _host_checks(target: Path, world: FakeWorld) -> None:
    request = _request(target, "setup::apple.host_eligible")
    assert _status(apple.host_eligible(request, world)) == "verified"
    world.version = "26.6"
    assert _status(apple.host_eligible(request, world)) == "blocked"
    world.version = "28.1"  # iss_f7937801: a later macOS is not refused
    assert _status(apple.host_eligible(request, world)) == "verified"
    world.version = "27.0"
    world.architecture = "x86_64"
    assert _status(apple.host_eligible(request, world)) == "blocked"
    world.architecture = "arm64"


def _asset_and_config(target: Path, world: FakeWorld) -> None:
    asset_request = _request(target, "setup::apple.asset_verified")
    config_ref = "setup::apple.configure_coreai_embeddings"
    assert _status(apple.asset_verified(asset_request, world)) == "blocked"
    assert _status(setup_adapter.dispatch_request(_request(target, config_ref, "apply"), world)) == "blocked"
    manifest_digest = _asset_fixture(target)
    with patch.object(apple, "_ASSET_MANIFEST_SHA256", manifest_digest):
        _asset_integrity(target, world, asset_request)
        _coreai_config_state(target, world, config_ref)


def _asset_integrity(target: Path, world: FakeWorld, request: AdapterRequest) -> None:
    tokenizer = target / "profile/data/model-assets/nomic-embed-text-v1.5/tokenizer.json"
    tokenizer.write_bytes(b"corrupt")
    assert _status(apple.asset_verified(request, world)) == "blocked"
    tokenizer.write_bytes(b"{}")
    assert _status(apple.asset_verified(request, world)) == "verified"


def _coreai_config_state(target: Path, world: FakeWorld, reference: str) -> None:
    with patch.object(setup_adapter, "provision_lm_studio", side_effect=AssertionError("LM Studio invoked")):
        assert _status(setup_adapter.dispatch_request(_request(target, reference), world)) == "pending"
        assert _status(setup_adapter.dispatch_request(_request(target, reference, "apply"), world)) == "applied"
    config = target / "profile/config/plugins/coreai_embeddings_plugin.json"
    data = json.loads(config.read_text())
    assert data == {"asset_root": str(target / "profile/data/model-assets"), "compute_preference": "gpu"}
    assert _status(apple.embedding_config_valid(_request(target, "setup::apple.embedding_config_valid"), world)) == "verified"
    config.write_text('{"asset_root":"/wrong", "compute_preference":"gpu"}')
    assert _status(setup_adapter.dispatch_request(_request(target, reference, "apply"), world)) == "blocked"
    assert json.loads(config.read_text())["asset_root"] == "/wrong"


def _inference_config(target: Path, world: FakeWorld) -> None:
    reference = "setup::apple.configure_inference"
    assert _status(setup_adapter.dispatch_request(_request(target, reference, "apply"), world)) == "blocked"
    source = target / apple._INFERENCE_SOURCE
    source.parent.mkdir(parents=True, exist_ok=True)
    content = '{"model":"apple-system","max_tokens":1024,"context.model_context_tokens":8192}\n'
    source.write_text(content)
    with patch.object(apple, "_INFERENCE_SOURCE_SHA256", _digest(content.encode())):
        assert _status(setup_adapter.dispatch_request(_request(target, reference), world)) == "pending"
        assert _status(setup_adapter.dispatch_request(_request(target, reference, "apply"), world)) == "applied"
        assert _status(apple.inference_config_valid(_request(target, "setup::apple.inference_config_valid"), world)) == "verified"
        config = target / "profile/config/plugins/macos_inference_plugin.json"
        assert "base_url" not in config.read_text()
        config.write_text('{"model":"wrong"}')
        assert _status(setup_adapter.dispatch_request(_request(target, reference, "apply"), world)) == "blocked"


class _Reason(Enum):
    DEVICE_NOT_ELIGIBLE = 1
    APPLE_INTELLIGENCE_NOT_ENABLED = 0


def _availability(target: Path, world: FakeWorld) -> None:
    request = _request(target, "setup::apple.model_availability")

    class Model:
        available = True
        reason = _Reason.DEVICE_NOT_ELIGIBLE
        context_size = 8192

        def is_available(self) -> tuple[bool, _Reason | None]:
            return self.available, None if self.available else self.reason

    with patch.dict(sys.modules, {"apple_fm_sdk": SimpleNamespace(SystemLanguageModel=Model)}):
        assert _status(apple.model_availability(request, world)) == "verified"
        Model.available = False
        warning = apple.model_availability(request, world)
        assert _status(warning) == "verified"
        assert cast(list[dict[str, object]], warning["evidence"])[0]["status"] == "warning"
        world.model = "Mac15,1"
        assert _status(apple.model_availability(request, world)) == "blocked"
        world.model = "VirtualMac2,1"
        Model.reason = _Reason.APPLE_INTELLIGENCE_NOT_ENABLED
        assert _status(apple.model_availability(request, world)) == "verified", "iss_9b396043: a warning, not a block"


def _process_result(value: dict[str, object]) -> CommandOutcome:
    return CommandOutcome(0, False, 1, json.dumps({"result": value}), "")


def _embedding_behavior(target: Path, world: FakeWorld) -> None:
    _metadata_red_control(target, world)
    vector_key = "service_interface::embedding_service::generate_embeddings"
    model = "nomic-ai/nomic-embed-text-v1.5"
    manifest_digest = _asset_fixture(target)
    config = target / "profile/config/plugins/coreai_embeddings_plugin.json"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(apple.coreai_config_text(target), encoding="utf-8")
    binding = target / "profile/config/service_bindings.json"
    binding.write_text(json.dumps({"embedding_service": "coreai_embeddings_plugin"}))
    failed = _apple_probe(target, world, manifest_digest, {
        "success": False, "action_status": "error", "error": {"code": "runtime_failed"}
    })
    assert _status(failed) == "blocked", "real inference failure must block metadata-green control"
    vector = [0.0] * 768
    passed = _apple_probe(target, world, manifest_digest, {
        "success": True,
        "action_status": "completed",
        "error": None,
        "data": {"result": {"embeddings": [vector], "dimension": 768, "model": model}},
    })
    assert _status(passed) == "verified", "one finite selected-provider vector qualifies"
    vector_calls = [call for call in world.calls if len(call) == 4 and call[1] == "call" and call[2] == vector_key]
    assert vector_calls
    assert json.loads(vector_calls[-1][3]) == {
        "inputs": ["Apple embedding readiness probe"], "model": model, "input_type": "text"
    }
    _invalid_embedding_vectors(target, world, manifest_digest, vector, model)
    binding.write_text(json.dumps({"embedding_service": "openai_embeddings_plugin"}))
    assert _status(_apple_probe(target, world, manifest_digest, {
        "success": True, "action_status": "completed", "error": None,
        "data": {"result": {"embeddings": [vector], "dimension": 768, "model": model}}
    })) == "blocked"


def _metadata_red_control(target: Path, world: FakeWorld) -> None:
    from github_midwife_plugin import installation_doctor as doctor

    metadata_key = "service_interface::embedding_service::get_embedding_dimension"
    model = "nomic-ai/nomic-embed-text-v1.5"
    world.process_outcomes[metadata_key] = _process_result({
        "success": True,
        "action_status": "completed",
        "error": None,
        "data": {"result": {"dimension": 768, "model": model}},
    })
    metadata = doctor._generic_process(
        _request(target, "service_interface::embedding_service.get_embedding_dimension"), world
    )
    assert _status(metadata) == "verified", "red control: static metadata is green"


def _apple_probe(target: Path, world: FakeWorld, manifest_digest: str, value: dict[str, object]) -> JsonObject:
    world.process_outcomes["service_interface::embedding_service::generate_embeddings"] = _process_result(value)
    with patch.object(apple, "_ASSET_MANIFEST_SHA256", manifest_digest):
        return setup_adapter.dispatch_request(
            _request(target, "setup::apple.embedding_request_succeeds"), world
        )


def _invalid_embedding_vectors(
    target: Path, world: FakeWorld, manifest_digest: str, vector: list[float], model: str
) -> None:
    for invalid in ([], [vector[:-1]], [vector, vector], [[float("nan")] * 768],
                    [[float("inf")] * 768], [[True] * 768]):
        malformed = _apple_probe(target, world, manifest_digest, {
            "success": True,
            "action_status": "completed",
            "error": None,
            "data": {"result": {"embeddings": invalid, "dimension": 768, "model": model}},
        })
        assert _status(malformed) == "blocked", f"malformed vector passed: {invalid[:1]}"
    for dimension, returned_model in ((767, model), (768, "wrong-model")):
        mismatch = _apple_probe(target, world, manifest_digest, {
            "success": True, "action_status": "completed", "error": None,
            "data": {"result": {"embeddings": [vector], "dimension": dimension, "model": returned_model}},
        })
        assert _status(mismatch) == "blocked"


def main() -> None:
    _flow_branch()
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        world = FakeWorld(target)
        _host_checks(target, world)
        _asset_and_config(target, world)
        _inference_config(target, world)
        _availability(target, world)
        assert not any("lm_studio" in " ".join(call).lower() for call in world.calls)
        assert len(world.writes) == 2
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory)
        _embedding_behavior(target, FakeWorld(target))
    print(
        "apple_setup_flow_smoke: Apple vector behavior, flow gates, asset/config, VM warning, "
        "Core AI LaunchAgent flow ordering, and LM Studio isolation pass"
    )


if __name__ == "__main__":
    main()
