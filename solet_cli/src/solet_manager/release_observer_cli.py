"""CLI routing for the Manager release-observer owner interface."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import cast

from solet_setup_contracts.release_observer_codec import ObserverContractError, decode_message
from solet_setup_contracts.release_observer_contract import (
    ExecutionRequest,
    ObservationResult,
    ReadObservationRequest,
)

from .models import CommandResult, ExitCode, JsonValue
from .release_observer import ManagerReleaseObserver, installed_manager_binding
from .release_observer_store import ObservationArchive
from .release_operation import ReleaseOperationStore


def add_release_proof_parser(commands: object) -> None:
    proof = commands.add_parser(
        "release-proof", help="Manager-owned release observation interface."
    )
    modes = proof.add_subparsers(dest="release_proof_command", required=True)
    modes.add_parser("capabilities", help="Print the packaged observer capability contract.")
    modes.add_parser("binding", help="Produce the installed Manager binding receipt.")
    execute = modes.add_parser("execute", help="Persist a pre-execution release-operation binding.")
    execute.add_argument("--request", type=Path, required=True)
    observe = modes.add_parser(
        "observe", help="Passive observation is available only through a qualified owner provider."
    )
    observe.add_argument("--request", type=Path, required=True)
    history = modes.add_parser("read-observation", help="Read an immutable archived observation.")
    history.add_argument("--request", type=Path, required=True)


def run_release_proof(
    args: object, state_dir: Path, seed_lock_path: Path | None = None
) -> CommandResult:
    mode = args.release_proof_command
    if mode == "capabilities":
        payload = _metadata()
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return _result(
            "capabilities",
            "succeeded",
            "Manager release observer capabilities.",
            {"contract": payload, "sha256": hashlib.sha256(encoded).hexdigest()},
        )
    if mode == "binding":
        metadata = (
            Path(__file__).with_name("released_metadata") / "release_observer_contract.v1.json"
        )
        binding = installed_manager_binding(
            state_dir=state_dir,
            capability_path=metadata,
            seed_lock_path=seed_lock_path,
        )
        return _result(
            "observer_binding",
            "succeeded",
            "Installed Manager observer binding receipt.",
            {"binding": cast(JsonValue, binding.to_dict())},
        )
    if mode == "execute":
        message = decode_message(args.request.read_bytes())
        if not isinstance(message, ExecutionRequest):
            raise ObserverContractError("malformed", "execute requires execution_request")
        bound = ReleaseOperationStore(state_dir / "release-proof").bind(message)
        return _result(
            "release_operation",
            "bound",
            "Release operation binding persisted before execution.",
            {"operation_id": bound.header.operation_id, "request_sha256": bound.digest},
        )
    if mode == "observe":
        # A CLI request cannot carry authoritative facts or select an executable.
        return _result(
            "release_observation",
            "unknown",
            "No qualified passive Manager owner provider is installed.",
            {},
            ExitCode.HUMAN_ACTION,
            "owner_provider_unavailable",
        )
    if mode == "read-observation":
        message = decode_message(args.request.read_bytes())
        if not isinstance(message, ReadObservationRequest):
            raise ObserverContractError(
                "malformed", "read-observation requires read_observation_request"
            )
        archive = ObservationArchive(state_dir / "release-proof")
        observer = ManagerReleaseObserver(archive, _unavailable_provider)
        result = observer.read(message)
        return _result(
            "read_observation",
            "succeeded",
            "Immutable manager observation read.",
            {"result": cast(JsonValue, result.to_dict())},
        )
    raise AssertionError(f"unhandled release proof mode: {mode}")


def _unavailable_provider(_: object) -> ObservationResult:
    raise ObserverContractError("unsupported_capability", "qualified owner provider is unavailable")


def _metadata() -> dict[str, JsonValue]:
    source = Path(__file__).with_name("released_metadata") / "release_observer_contract.v1.json"
    raw = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ObserverContractError(
            "malformed", "packaged Manager observer metadata is not an object"
        )
    payload = cast(dict[str, JsonValue], raw)
    if payload.get("schema_version") != "release_observer.v1" or payload.get("owner") != "manager":
        raise ObserverContractError("malformed", "packaged Manager observer metadata is invalid")
    return payload


def _result(
    kind: str,
    status: str,
    message: str,
    data: dict[str, JsonValue],
    code: ExitCode = ExitCode.OK,
    error_kind: str | None = None,
) -> CommandResult:
    return CommandResult(
        kind=kind,
        status=status,
        message=message,
        exit_code=code,
        error_kind=error_kind,
        data=data,
    )
