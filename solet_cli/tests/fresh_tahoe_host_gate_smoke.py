"""Fresh setup choices follow this Mac's host profile (iss_3a2a74ea, rul_c11cf191, rul_5cc2910c).

Drives the real Manager plan builder over the shipped create-flow contract with
the host measurement replaced by a fixed reading:

* macOS 26.x on arm64 defaults embeddings to llama.cpp, offers llama.cpp and not
  Apple Foundation Models for summaries, and refuses an explicit Apple choice
  with a reason naming the Mac's version;
* macOS 27.x on arm64 keeps the Apple-native default and refuses llama.cpp;
* a Mac that cannot be measured refuses the choice instead of guessing;
* a recorded answer is never re-judged, so no measurement is taken;
* the create flow's host profiles equal the existing-install flow's, so fresh
  create and update gate on the same thresholds (r52's measurement and table).

Offline: no host command runs.  Run from the repository root with
``.venv/bin/python3 solet_cli/tests/fresh_tahoe_host_gate_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import json
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
for _path in (_ROOT / "solet_cli" / "src",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from solet_manager import host_option_binding
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle
from solet_manager.errors import ContractError, HostPlatformUnknownError
from solet_manager.host_platform import HostPlatform, HostPlatformError
from solet_manager.models import JsonValue
from solet_manager.plan_builder import SetupPlan, build_setup_plan
from solet_manager.release_lock import SeedLock

_KB = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_REVISION = "a" * 40
_TAHOE = HostPlatform("26.4", "arm64")
_GOLDEN_GATE = HostPlatform("27.0", "arm64")
_CHECKS: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    if not condition:
        raise AssertionError(f"FAIL: {label}: {detail}")
    _CHECKS.append(label)


def _seed() -> SeedLock:
    return SeedLock(
        repository="example/seed",
        release_tag="v1.0.0",
        commit=_REVISION,
        tree_hash="b" * 40,
        archive_sha256="c" * 64,
        profile="macos-bizops",
    )


def _reader(host: HostPlatform | None, calls: list[str]) -> Callable[[], HostPlatform]:
    def read() -> HostPlatform:
        calls.append("measured")
        if host is None:
            raise HostPlatformError("sw_vers exited 1")
        return host

    return read


def _plan(
    bundle: ContractBundle,
    host: HostPlatform | None,
    *,
    selections: dict[str, JsonValue] | None = None,
    recorded: dict[str, JsonValue] | None = None,
) -> tuple[SetupPlan, list[str]]:
    calls: list[str] = []
    with patch.object(host_option_binding, "read_host_platform", _reader(host, calls)):
        plan = build_setup_plan(
            bundle=bundle,
            config=CreateConfig(name="host-gate", target=Path("/tmp/host-gate"), autostart=True),
            seed=_seed(),
            journal_path=Path("/tmp/host-gate.json"),
            prospective_consents=True,
            decision_selections=selections,
            recorded_answers=recorded,
            operation_stage_ids={"genesis"},
        )
    return plan, calls


def _decisions(plan: SetupPlan) -> dict[str, JsonValue]:
    return cast(dict[str, JsonValue], plan.answers["decisions"])


def _refusal(bundle: ContractBundle, host: HostPlatform, selections: dict[str, JsonValue]) -> str:
    try:
        _plan(bundle, host, selections=selections)
    except ContractError as exc:
        return str(exc)
    return ""


def _offered(bundle: ContractBundle, host: HostPlatform, decision_id: str) -> set[str]:
    bound = host_option_binding.host_bound(bundle, {"setup_profile": "macos-bizops"}, {}, read=_reader(host, []))
    source = cast(dict[str, JsonValue], bound.decisions[decision_id]["option_source"])
    options = cast(dict[str, dict[str, JsonValue]], source["options"])
    return {option_id for option_id, option in options.items() if option.get("availability") == "supported"}


def _check_host_profiles_shared() -> None:
    create = json.loads((_KB / "macos_setup_flow.json").read_text(encoding="utf-8"))
    update = json.loads((_KB / "existing_install_flow.json").read_text(encoding="utf-8"))
    _check(
        "fresh create and update gate on the same host profiles",
        create.get("host_profiles") == update.get("host_profiles") and bool(create.get("host_profiles")),
        (create.get("host_profiles"), update.get("host_profiles")),
    )


def _check_tahoe(bundle: ContractBundle) -> None:
    plan, calls = _plan(bundle, _TAHOE)
    decisions = _decisions(plan)
    _check("macOS 26 is measured for a pending choice", calls != [], calls)
    _check("macOS 26 defaults embeddings to llama.cpp", decisions.get("embeddings_implementation") == "llama_cpp", decisions)
    offered = _offered(bundle, _TAHOE, "inference_implementation")
    _check(
        "macOS 26 offers llama.cpp and not Apple Foundation Models for summaries",
        "llama_cpp" in offered and "apple_foundation_models" not in offered,
        offered,
    )
    _check("macOS 26 does not offer Core AI embeddings", "coreai" not in _offered(bundle, _TAHOE, "embeddings_implementation"))
    chosen, _ = _plan(bundle, _TAHOE, selections={"inference_implementation": "llama_cpp"})
    _check(
        "macOS 26 accepts llama.cpp summaries alongside llama.cpp embeddings",
        _decisions(chosen).get("inference_implementation") == "llama_cpp"
        and _decisions(chosen).get("embeddings_implementation") == "llama_cpp",
        _decisions(chosen),
    )
    genesis = next(item for item in chosen.operations if item.operation_ref == "genesis::solet.run")
    _check(
        "genesis receives both llama.cpp implementation decisions",
        genesis.public_inputs.get("embeddings_implementation") == "llama_cpp"
        and genesis.public_inputs.get("inference_implementation") == "llama_cpp",
        genesis.public_inputs,
    )
    for decision_id, option_id in (
        ("embeddings_implementation", "coreai"),
        ("inference_implementation", "apple_foundation_models"),
    ):
        refusal = _refusal(bundle, _TAHOE, {decision_id: option_id})
        _check(
            f"macOS 26 refuses an explicit {option_id} with the Mac's version",
            "not available on this Mac" in refusal and "macOS 26.4" in refusal and "macOS 27" in refusal,
            refusal,
        )


def _check_golden_gate(bundle: ContractBundle) -> None:
    plan, _ = _plan(bundle, _GOLDEN_GATE)
    _check("macOS 27 keeps the Core AI default", _decisions(plan).get("embeddings_implementation") == "coreai", _decisions(plan))
    offered = _offered(bundle, _GOLDEN_GATE, "inference_implementation")
    _check(
        "macOS 27 offers Apple Foundation Models and not llama.cpp",
        "apple_foundation_models" in offered and "llama_cpp" not in offered,
        offered,
    )
    refusal = _refusal(bundle, _GOLDEN_GATE, {"embeddings_implementation": "llama_cpp"})
    _check("macOS 27 refuses llama.cpp embeddings", "not available on this Mac" in refusal and "Apple-native" in refusal, refusal)


def _check_unmeasurable(bundle: ContractBundle) -> None:
    try:
        _plan(bundle, None)
    except HostPlatformUnknownError as exc:
        _check("an unmeasurable Mac refuses the choice", "could not be measured" in str(exc), str(exc))
        return
    _check("an unmeasurable Mac refuses the choice", False, "the plan was built without a measurement")


def _check_recorded_not_rejudged(bundle: ContractBundle) -> None:
    recorded: dict[str, JsonValue] = {
        "public_inputs": {},
        "decisions": {
            "setup_profile": "macos-bizops",
            "embeddings_implementation": "coreai",
            "inference_implementation": "apple_foundation_models",
        },
    }
    plan, calls = _plan(bundle, None, recorded=recorded)
    _check("recorded answers take no measurement", calls == [], calls)
    _check(
        "recorded Apple answers survive on any Mac",
        _decisions(plan).get("embeddings_implementation") == "coreai"
        and _decisions(plan).get("inference_implementation") == "apple_foundation_models",
        _decisions(plan),
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision=_REVISION, directory=_KB)
    _check_host_profiles_shared()
    _check_tahoe(bundle)
    _check_golden_gate(bundle)
    _check_unmeasurable(bundle)
    _check_recorded_not_rejudged(bundle)
    print(f"fresh_tahoe_host_gate_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
