"""A missing llama.cpp model is a warning, never a blocked setup (rul_18bd93a3, iss_3a2a74ea).

Proves, offline:

* ``models_present`` stays verified with a warning for an absent model, reports
  the bytes of a partial download, and passes only a file at its pinned size and
  SHA-256 (a tiny fixture model under a fixture pin, never the real GGUF);
* ``services_current`` stays verified with a warning while a loaded server is
  still fetching or loading its model;
* the service operation finishes ``applied`` when the embeddings server is not
  yet serving, after its bounded wait;
* the flow declares the model probe as a readiness probe with no remediation,
  so nothing waits on the download.
"""

from __future__ import annotations

# ruff: noqa: E402
import dataclasses
import hashlib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

_REPO = Path(__file__).resolve().parents[3]
_MIDWIFE = _REPO / "plugins" / "github_midwife_plugin"
for _path in (_MIDWIFE / "src", Path(__file__).resolve().parent):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from github_midwife_plugin import llama_cpp_setup as llama
from github_midwife_plugin.setup_adapter_contract import JsonObject
from llama_cpp_fixture_support import FixtureRuntime, request

_FIXTURE_BYTES = b"GGUF fixture model bytes, not a real model\n"
_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _evidence(result: JsonObject) -> dict[str, JsonObject]:
    return {cast(str, item["id"]): item for item in cast(list[JsonObject], result["evidence"])}


def _fixture_registry() -> llama.LlamaRegistry:
    """The shipped registry with its embeddings model repinned to fixture bytes."""
    registry = llama.load_registry()
    embeddings = registry.roles["embeddings"]
    model = dataclasses.replace(
        embeddings.model, size_bytes=len(_FIXTURE_BYTES), sha256=hashlib.sha256(_FIXTURE_BYTES).hexdigest()
    )
    roles = {**registry.roles, "embeddings": dataclasses.replace(embeddings, model=model)}
    return dataclasses.replace(registry, roles=roles)


def _check_models_present(root: Path) -> None:
    home, target = root / "home", root / "target"
    runtime = FixtureRuntime(home)
    reference = "setup::llama_cpp.models_present"
    absent = llama.models_present(request(target, reference, phase="probe"), runtime)
    items = _evidence(absent)
    _check("absent models still verify", absent["checkpoint_status"] == "verified" and absent["error_kind"] is None, absent)
    _check("each absent model is a warning",
           {item["status"] for item in items.values()} == {"warning"} and len(items) == 2, items)
    _check("the warning names the rule and says the download resumes on its own",
           "rul_18bd93a3" in str(absent["repair"]) and "resumes on its own" in str(absent["repair"]), absent["repair"])

    registry = _fixture_registry()
    model = registry.roles["embeddings"].model
    with patch.object(llama, "load_registry", lambda: registry):
        partial = model.path(home).with_name(model.filename + ".partial")
        partial.parent.mkdir(parents=True)
        partial.write_bytes(_FIXTURE_BYTES[:7])
        halfway = _evidence(llama.models_present(request(target, reference, phase="probe"), runtime))
        observed = halfway["llama_cpp_embeddings_model"]["observed"]
        _check("a partial download reports its bytes as a warning",
               halfway["llama_cpp_embeddings_model"]["status"] == "warning" and observed == ["bytes=7", "verified=false"], observed)

        partial.rename(model.path(home))
        model.path(home).write_bytes(_FIXTURE_BYTES[:-1] + b"!")
        tampered = _evidence(llama.models_present(request(target, reference, phase="probe"), runtime))
        _check("a full-size file with the wrong SHA-256 is not accepted",
               tampered["llama_cpp_embeddings_model"]["status"] == "warning", tampered["llama_cpp_embeddings_model"])

        model.path(home).write_bytes(_FIXTURE_BYTES)
        present = _evidence(llama.models_present(request(target, reference, phase="probe"), runtime))
        _check("the pinned model passes its readback",
               present["llama_cpp_embeddings_model"]["status"] == "passed"
               and present["llama_cpp_summaries_model"]["status"] == "warning", present)


def _check_services_waiting(root: Path) -> None:
    home, target = root / "wait-home", root / "wait-target"
    runtime = FixtureRuntime(home)
    applied = llama.install_services(request(target, "setup::llama_cpp.install_services", phase="apply"), runtime)
    _check("the services install while no model is served yet", applied["checkpoint_status"] == "applied", applied)
    current = llama.services_current(request(target, "setup::llama_cpp.services_current", phase="probe"), runtime)
    items = _evidence(current)
    _check("loaded services still fetching their models verify with warnings",
           current["checkpoint_status"] == "verified" and {item["status"] for item in items.values()} == {"warning"}, current)
    runtime.serving.add(llama.load_registry().roles["embeddings"].port)
    serving = _evidence(llama.services_current(request(target, "setup::llama_cpp.services_current", phase="probe"), runtime))
    _check("a serving role passes while the other keeps its warning",
           serving["llama_cpp_embeddings_service"]["status"] == "passed"
           and serving["llama_cpp_summaries_service"]["status"] == "warning", serving)


def _check_flow() -> None:
    flow = cast(dict[str, Any], json.loads((_MIDWIFE / "knowledge_base" / "macos_setup_flow.json").read_text(encoding="utf-8")))
    probe = flow["probes"]["llama_cpp_models_present"]
    _check("the model probe is readiness evidence with no remediation",
           probe["level"] == "readiness" and "remediation_operation_refs" not in probe, probe)
    operations = [ref for ref, row in flow["operations"].items() if "llama_cpp_models_present" in json.dumps(row.get("idempotency", {}))]
    _check("no operation waits on the model download", operations == [], operations)


def main() -> int:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _check_models_present(root)
        _check_services_waiting(root)
    _check_flow()
    print(f"llama_cpp_missing_model_warning_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
