#!/usr/bin/env python3
# pyright: reportMissingImports=false
"""Reach candidate9 positive and all four candidate8 repair arms."""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import sys
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory

sealed_candidate = os.environ.get("SOLET_SEALED_SUBJECT_CANDIDATE")
if sealed_candidate:
    sys.path.insert(0, str(Path(sealed_candidate) / "solet_cli/src"))

import solet_manager.preservation_membership as membership  # noqa: E402

BEFORE = b'{"service_interface":"default"}\n'
AFTER = b'{"local_self_deployment_service":"macos_self_deployment_plugin","service_interface":"default"}\n'
THIRD = b'{"service_interface":"stale"}\n'
OPERATION = "repair_service_bindings_v1"
_checks = 0


def check(condition: bool, label: str, observed: object = None) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(f"{label}; observed={observed!r}")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def fixture(raw: str, suffix: str = "one") -> tuple[str, Path, Path, dict[str, str]]:
    base = Path(raw).resolve()
    name = f"owned-candidate9-{suffix}"
    root = base / f"subject-{suffix}"
    target = root / "profile/config/service_bindings.json"
    target.parent.mkdir(parents=True)
    target.write_bytes(BEFORE)
    (root / "root_manifest.yaml").write_text(
        f"schema_version: 1\nsolet_name: {name}\n", encoding="utf-8"
    )
    manager = base / f"manager-{suffix}"
    write_json(
        manager / f"state/transactions/{name}.json",
        {"name": name, "operation_id": "birth-op", "status": "failed", "target": str(root)},
    )
    write_json(
        manager / "config/preserved_roots.json",
        {"preserved_roots": [], "schema_version": 1},
    )
    return name, root, target, {
        "APP_HOME": str(root / "profile"),
        "SOLET_HOME": str(manager),
        "SOLET_NAME": name,
    }


def effects(target: Path) -> membership.RepairEffectSet:
    return membership.RepairEffectSet(
        paths=(target,),
        logical_identities=membership.REQUIRED_LOGICAL_IDENTITIES,
        expected_before_sha256=sha(BEFORE),
        expected_after_sha256=sha(AFTER),
        replacement_bytes=AFTER,
    )


def resolver(_name: str, _root: Path) -> tuple[str, ...]:
    return tuple(f"process:{index}" for index in range(7))


def plan(name: str, root: Path, target: Path, environ: dict[str, str]) -> membership.OwnershipPlan:
    return membership.build_preservation_snapshot(
        environ=environ,
        target_name=name,
        target_root=root,
        effects=effects(target),
        runtime_resolver=resolver,
        operation=OPERATION,
    )


def act(
    planned: object,
    acknowledgement: str,
    name: str,
    root: Path,
    target: Path,
    environ: dict[str, str],
    chosen: membership.RepairEffectSet | None = None,
) -> membership.OwnershipPlan:
    return membership.revalidate_preservation_snapshot(
        planned,
        acknowledgement=acknowledgement,
        environ=environ,
        target_name=name,
        target_root=root,
        effects=effects(target) if chosen is None else chosen,
        runtime_resolver=resolver,
        operation=OPERATION,
    )


def reason(action: object) -> str:
    try:
        assert callable(action)
        action()
    except membership.ProtectionRefusedError as exc:
        return exc.reason_code
    raise AssertionError("expected protection refusal")


def positive_and_replay() -> None:
    with TemporaryDirectory(prefix="candidate9-positive-") as raw:
        name, root, target, environ = fixture(raw)
        collateral = root / "profile/config/preexisting.json"
        collateral.write_bytes(b"keep\n")
        planned = plan(name, root, target, environ)
        check(len(planned.acknowledgement_token) >= 40, "plan mints random acknowledgement")
        record = Path(environ["SOLET_HOME"]) / f"state/repair_plans/available/{planned.plan_id}.json"
        check(record.is_file(), "plan persists canonical record")
        returned = act(planned, planned.acknowledgement_token, name, root, target, environ)
        check(returned == planned, "act returns persisted canonical plan")
        check(target.read_bytes() == AFTER, "owned CAS writes exact replacement")
        check(collateral.read_bytes() == b"keep\n", "preexisting collateral remains unchanged")
        check(not record.exists(), "act atomically consumes available record")
        replay = reason(lambda: act(planned, planned.acknowledgement_token, name, root, target, environ))
        check(replay in {"plan_token_unavailable", "plan_stale"}, "token cannot replay", replay)


def forged_token_arm() -> None:
    with TemporaryDirectory(prefix="candidate9-forged-") as raw:
        name, root, target, environ = fixture(raw)
        planned = plan(name, root, target, environ)
        fake = "A" * 43
        forged = replace(
            planned,
            plan_id=sha(fake.encode()),
            acknowledgement_token=fake,
        )
        observed = reason(lambda: act(forged, fake, name, root, target, environ))
        check(observed == "plan_token_unavailable", "forged token reaches act and refuses", observed)
        check(target.read_bytes() == BEFORE, "forged token cannot mutate")


def substituted_plan_arm() -> None:
    with TemporaryDirectory(prefix="candidate9-substitute-") as raw:
        name, root, target, environ = fixture(raw)
        planned = plan(name, root, target, environ)
        unrelated = root / "profile/config/already-present.json"
        unrelated.write_bytes(b"present\n")
        substituted = replace(
            planned,
            effect_path=str(unrelated),
            expected_after_sha256=sha(unrelated.read_bytes()),
        )
        observed = reason(
            lambda: act(substituted, planned.acknowledgement_token, name, root, target, environ)
        )
        check(observed == "plan_payload_mismatch", "substituted plan reaches act and refuses", observed)
        check(target.read_bytes() == BEFORE, "substituted plan cannot false-succeed")


def wrong_artifact_arm() -> None:
    with TemporaryDirectory(prefix="candidate9-wrong-artifact-") as raw:
        name, root, target, environ = fixture(raw)
        wrong = root / "profile/config/not_service_bindings.json"
        wrong.write_bytes(BEFORE)
        chosen = membership.RepairEffectSet(
            paths=(wrong,),
            logical_identities=("service:unrelated",),
            expected_before_sha256=sha(BEFORE),
            expected_after_sha256=sha(AFTER),
            replacement_bytes=AFTER,
        )
        observed = reason(
            lambda: membership.build_preservation_snapshot(
                environ=environ,
                target_name=name,
                target_root=root,
                effects=chosen,
                runtime_resolver=resolver,
                operation=OPERATION,
            )
        )
        check(observed == "effect_target_mismatch", "wrong artifact is rejected", observed)
        check(wrong.read_bytes() == BEFORE and target.read_bytes() == BEFORE, "wrong artifact cannot mutate")


def stale_arm() -> None:
    with TemporaryDirectory(prefix="candidate9-stale-") as raw:
        name, root, target, environ = fixture(raw)
        planned = plan(name, root, target, environ)
        target.write_bytes(THIRD)
        observed = reason(lambda: act(planned, planned.acknowledgement_token, name, root, target, environ))
        check(observed == "plan_stale", "stale preimage reaches act and refuses", observed)
        check(target.read_bytes() == THIRD, "stale refusal preserves bytes")
        again = reason(lambda: act(planned, planned.acknowledgement_token, name, root, target, environ))
        check(again == "plan_token_unavailable", "failed act still consumes one-shot token", again)


def main() -> None:
    positive_and_replay()
    forged_token_arm()
    substituted_plan_arm()
    wrong_artifact_arm()
    stale_arm()
    signature = inspect.signature(membership.revalidate_preservation_snapshot)
    check("mutate" not in signature.parameters, "act exposes no opaque mutation callback")
    check(not {"yes", "force", "assume_yes"} & set(signature.parameters), "act exposes no blanket confirmation")
    print(f"preservation_membership_smoke: PASS ({_checks} checks, four act-time arms reached)")


if __name__ == "__main__":
    main()
