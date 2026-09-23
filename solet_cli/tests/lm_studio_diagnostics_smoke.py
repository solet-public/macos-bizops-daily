"""Passive LM Studio diagnosis and declared operation timeout/projection controls."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "solet_cli/src"))
sys.path.insert(0, str(ROOT / "plugins/github_midwife_plugin/tests"))

from setup_flow_schema_smoke import _projection_transaction  # noqa: E402
from solet_manager.condition_evaluator import condition_matches  # noqa: E402
from solet_manager.contracts import ContractBundle, target_contract_directory  # noqa: E402
from solet_manager.doctor_lm_studio_census import collect_lm_studio_advisories  # noqa: E402
from solet_manager.errors import ContractError, StateConflictError  # noqa: E402
from solet_manager.lm_studio_diagnostics import inspect_lm_studio, selected_lm_studio_roles  # noqa: E402
from solet_manager.models import InstanceRecord, JsonValue  # noqa: E402
from solet_manager.operation_records import operation_request  # noqa: E402
from solet_manager.plan_builder import _planned_operation  # noqa: E402
from solet_manager.probe_input_projection import probe_public_inputs  # noqa: E402
from solet_manager.probe_input_retention import retain_probe_inputs  # noqa: E402
from solet_manager.transaction import Transaction  # noqa: E402


def fixture(target: Path) -> None:
    config = target / "profile/config"
    (config / "plugins").mkdir(parents=True)
    (config / "service_bindings.json").write_text('{"embedding_service":"openai_embeddings_plugin","inference_service":"none"}')
    (config / "plugins/openai_embeddings_plugin.json").write_text('{"base_url":"http://127.0.0.1:1234/v1","model":"text-embedding-nomic-embed-text-v1.5-embedding"}')


def check_passivity(target: Path, home: Path) -> None:
    fixture(target)
    before = {path: path.read_bytes() for path in target.rglob("*") if path.is_file()}
    with patch("subprocess.run", side_effect=AssertionError("passive inspection executed a command")):
        checks = inspect_lm_studio(target, home=home, model_reader=lambda: {"text-embedding-nomic-embed-text-v1.5-embedding": "not-loaded"})
    assert len(checks) == 6 and not any("inference" in check.check_id for check in checks)
    assert next(check for check in checks if check.check_id.endswith("embeddings_loaded")).status.value == "failed"
    assert {path: path.read_bytes() for path in target.rglob("*") if path.is_file()} == before
    assert not home.exists()
    assert selected_lm_studio_roles(target) == ("embeddings",)


def check_advisory(target: Path) -> None:
    record = cast(InstanceRecord, SimpleNamespace(target=str(target)))
    absent = collect_lm_studio_advisories(record, reader=lambda: (113, "", "Could not find service"))
    unknown = collect_lm_studio_advisories(record, reader=lambda: (-1, "", "permission denied"))
    present = collect_lm_studio_advisories(record, reader=lambda: (0, "local.solet.lm-studio = {\n state = not running\n}", ""))
    assert absent[0]["status"] == "warn" and unknown[0]["status"] == "unknown"
    assert present[0]["status"] == "verified" and present[0]["blocking"] is False


def check_timeout_and_projection() -> None:
    directory = ROOT / "plugins/github_midwife_plugin/knowledge_base"
    flow = json.loads((directory / "macos_setup_flow.json").read_text())
    bundle = ContractBundle.load(source_revision="f" * 40, directory=directory)
    transaction = _projection_transaction(flow)
    decisions = transaction.answers["decisions"]
    inputs = transaction.answers["public_inputs"]
    for operation_id, timeout in (("pull_lm_studio_embedding_model", 900), ("pull_lm_studio_inference_model", 900), ("start_lm_studio_server", 300)):
        operation = _planned_operation(bundle, "system_dependencies", operation_id, inputs, decisions)
        applied = operation_request(transaction, bundle, operation, phase="apply", probe_purpose=None, approval="sha256:" + "c" * 64, attempt=1)
        probed = operation_request(transaction, bundle, operation, phase="probe", probe_purpose="preview", approval=None, attempt=1)
        assert applied.timeout_seconds == timeout and probed.timeout_seconds == 30
        for probe_id in operation.postcondition_probe_ids:
            projected = probe_public_inputs(transaction, bundle.probes[probe_id]["probe_ref"])
            assert projected == {"embeddings_implementation": "lm_studio", "inference_implementation": "lm_studio", "lm_studio_base_url": "http://127.0.0.1:1234/v1"}


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="lm-diagnostics-") as temporary:
        target = Path(temporary) / "target"
        check_passivity(target, Path(temporary) / "unwritten-home")
        check_advisory(target)
        check_inactive_projection(Path(temporary) / "projection")
    check_timeout_and_projection()
    check_selection()
    check_retention_boundaries()
    print("lm_studio_diagnostics_smoke: passive named checks, conditional inference, advisory unknown, timeout and probe projection passed")
    return 0


def _assert_projection_refused(transaction: Transaction) -> None:
    try:
        probe_public_inputs(transaction, "setup::lm_studio.cli_available")
    except (ContractError, StateConflictError):
        return
    raise AssertionError("unresolved LM Studio carrier or URL was accepted")


def check_inactive_projection(target: Path) -> None:
    directory = target_contract_directory(target)
    shutil.copytree(ROOT / "plugins/github_midwife_plugin/knowledge_base", directory)
    bundle = ContractBundle.load(source_revision="f" * 40, directory=directory)
    transaction = replace(
        _projection_transaction(bundle.flow), target=str(target),
        flow_contract_digest=bundle.contract_digest,
    )
    decisions: dict[str, JsonValue] = {"setup_profile": "free", "embeddings_implementation": "lm_studio"}
    inputs: dict[str, JsonValue] = {"lm_studio_base_url": "http://localhost:1234/v1"}
    transaction = replace(transaction, answers={"decisions": decisions, "public_inputs": inputs})
    before = json.dumps(transaction.answers, sort_keys=True)
    for suffix in ("cli_available", "server_ready", "embedding_artifact_present", "embedding_model_served", "login_agent_valid", "jit_disabled"):
        projected = probe_public_inputs(transaction, f"setup::lm_studio.{suffix}")
        assert projected == {
            "embeddings_implementation": "lm_studio", "inference_implementation": "none", **inputs,
        }
    assert json.dumps(transaction.answers, sort_keys=True) == before
    for profile in ("macos-bizops", "custom"):
        _assert_projection_refused(replace(transaction, answers={"decisions": {**decisions, "setup_profile": profile}, "public_inputs": inputs}))
    for key in ("embeddings_implementation", "inference_implementation"):
        for invalid in (None, "", False):
            _assert_projection_refused(replace(transaction, answers={"decisions": {**decisions, key: invalid}, "public_inputs": inputs}))
    for invalid_inputs in ({}, {"lm_studio_base_url": None}, {"lm_studio_base_url": ""}, {"lm_studio_base_url": False}):
        _assert_projection_refused(replace(transaction, answers={"decisions": decisions, "public_inputs": invalid_inputs}))
    _assert_projection_refused(replace(transaction, answers={"decisions": {"embeddings_implementation": "lm_studio"}, "public_inputs": inputs}))
    print("inactive LM Studio projection: 6 valid routes, 13 unresolved controls, unchanged answers passed")


def check_retention_boundaries() -> None:
    bundle = ContractBundle.load(source_revision="f" * 40, directory=ROOT / "plugins/github_midwife_plugin/knowledge_base")
    reviewed = {"public_inputs": {"lm_studio_base_url": "http://localhost:1234/v1", "unused": "discard"}, "resolution_evidence": [{"id": "lm_studio_base_url", "source": "flow_default", "summary": "http://localhost:1234/v1"}]}
    active = {"embeddings_implementation": "lm_studio", "inference_implementation": "none"}
    retained = {}
    assert retain_probe_inputs(bundle, active, reviewed, retained) == reviewed["resolution_evidence"]
    assert retained == {"lm_studio_base_url": "http://localhost:1234/v1"}
    inactive = {}
    assert retain_probe_inputs(bundle, {"embeddings_implementation": "other", "inference_implementation": "none"}, reviewed, inactive) == [] and inactive == {}
    missing = {}
    assert retain_probe_inputs(bundle, active, {"public_inputs": {}}, missing) == [] and missing == {}
    try:
        retain_probe_inputs(bundle, active, {"public_inputs": reviewed["public_inputs"]}, {})
    except ContractError:
        return
    raise AssertionError("unreviewed stored input was retained")


def check_selection() -> None:
    flow = json.loads((ROOT / "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json").read_text())
    operations = flow["operations"]
    for embedding in ("lm_studio", "other"):
        for inference in ("lm_studio", "other"):
            decisions = {"embeddings_implementation": embedding, "inference_implementation": inference}
            active = {name for name, definition in operations.items() if definition["operation_ref"].startswith("setup::lm_studio.") and condition_matches(definition["required_when"], decisions)}
            assert ("pull_lm_studio_embedding_model" in active) == (embedding == "lm_studio")
            assert ("load_lm_studio_inference_model" in active) == (inference == "lm_studio")
            assert ("install_lm_studio_login_agent" in active) == ("lm_studio" in (embedding, inference))


if __name__ == "__main__":
    raise SystemExit(main())
