"""The setup wizard offers LM Studio for inference but does not recommend it (chg_04232fcc, iss_f510b5c9).

r64 removes ``lm_studio`` from ``decisions.inference_implementation.recommended_option_refs``
and keeps it a supported, selectable option.  Drives the real Manager plan builder and
the real wizard prompt renderer (``static_decision_prompts``) over the shipped create-flow
contract, with the host measurement replaced by a fixed macOS 26 and macOS 27 reading:

* the inference prompt still lists ``lm_studio`` as a supported candidate;
* its rank is past every recommended option, and it sorts after the host's
  recommended option;
* no flow default selects inference on either host, so the choice stays the owner's;
* an explicit ``--decision inference_implementation=lm_studio`` is accepted.

Offline: no host command runs.  ``--contracts <dir>`` runs it against another contract
bundle (used for the red leg against the released 19350a8d bytes).  Run from the
repository root with
``.venv/bin/python3 solet_cli/tests/lm_studio_inference_not_recommended_wizard_smoke.py``.
"""

from __future__ import annotations

# ruff: noqa: E402
import argparse
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "solet_cli" / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))

from solet_manager import host_option_binding
from solet_manager.config import CreateConfig
from solet_manager.contracts import ContractBundle
from solet_manager.decision_resolution import static_decision_prompts
from solet_manager.host_platform import HostPlatform
from solet_manager.models import JsonValue
from solet_manager.plan_builder import SetupPlan, build_setup_plan
from solet_manager.release_lock import SeedLock

_KB = _ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_REVISION = "a" * 40
_HOSTS = (
    ("macOS 26", HostPlatform("26.4", "arm64"), "llama_cpp"),
    ("macOS 27", HostPlatform("27.0", "arm64"), "apple_foundation_models"),
)
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


def _reader(host: HostPlatform) -> Callable[[], HostPlatform]:
    return lambda: host


def _plan(bundle: ContractBundle, host: HostPlatform, selections: dict[str, JsonValue] | None = None) -> SetupPlan:
    with patch.object(host_option_binding, "read_host_platform", _reader(host)):
        return build_setup_plan(
            bundle=bundle,
            config=CreateConfig(name="wizard", target=Path("/tmp/wizard"), autostart=True),
            seed=_seed(),
            journal_path=Path("/tmp/wizard.json"),
            prospective_consents=True,
            decision_selections=selections,
            operation_stage_ids={"genesis"},
        )


def _inference_candidates(bundle: ContractBundle, host: HostPlatform, plan: SetupPlan) -> list[dict[str, JsonValue]]:
    with patch.object(host_option_binding, "read_host_platform", _reader(host)):
        prompts = static_decision_prompts(bundle, plan)
    prompt = next(
        cast(dict[str, JsonValue], item)
        for item in prompts
        if cast(dict[str, JsonValue], item)["id"] == "inference_implementation"
    )
    return cast(list[dict[str, JsonValue]], prompt["candidates"])


def _check_host(bundle: ContractBundle, name: str, host: HostPlatform, host_default: str) -> None:
    recommended = cast(list[str], bundle.decisions["inference_implementation"]["recommended_option_refs"])
    plan = _plan(bundle, host)
    _check(
        f"{name}: no flow default selects inference, so the wizard asks",
        "inference_implementation" in plan.unresolved_decisions,
        plan.unresolved_decisions,
    )
    candidates = _inference_candidates(bundle, host, plan)
    values = [str(item["value"]) for item in candidates]
    lm_studio = next((item for item in candidates if item["value"] == "lm_studio"), None)
    _check(f"{name}: the wizard offers lm_studio for inference", lm_studio is not None, values)
    assert lm_studio is not None
    metadata = cast(dict[str, JsonValue], lm_studio["metadata"])
    _check(f"{name}: lm_studio is offered as supported", metadata["availability"] == "supported", metadata)
    _check(
        f"{name}: lm_studio is ranked past every recommended option",
        cast(int, lm_studio["recommendation_rank"]) >= len(recommended),
        (lm_studio["recommendation_rank"], recommended),
    )
    _check(
        f"{name}: the host's recommended option ({host_default}) sorts ahead of lm_studio",
        host_default in values and values.index(host_default) < values.index("lm_studio"),
        values,
    )
    chosen = _plan(bundle, host, {"inference_implementation": "lm_studio"})
    _check(
        f"{name}: an explicit lm_studio inference choice is accepted",
        cast(dict[str, JsonValue], chosen.answers["decisions"]).get("inference_implementation") == "lm_studio",
        chosen.answers["decisions"],
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--contracts", type=Path, default=_KB)
    bundle = ContractBundle.load(source_revision=_REVISION, directory=parser.parse_args().contracts)
    recommended = cast(list[str], bundle.decisions["inference_implementation"]["recommended_option_refs"])
    _check("the inference decision does not recommend lm_studio", "lm_studio" not in recommended, recommended)
    for name, host, host_default in _HOSTS:
        _check_host(bundle, name, host, host_default)
    print(f"lm_studio_inference_not_recommended_wizard_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
