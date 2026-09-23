"""Passive manager observation composition and historical readback."""

# U2 remaining work: bind a qualified installed owner provider; construct
# SetupFacts from registry/transaction/frontier/attempt records; prove install
# provenance and T08/T10/T11/T16/T17 refusal sentinels.  Doctor positive facts
# remain blocked on persisted-result prerequisite iss_a9d970c5; preserve the
# explicit missing-observation behavior until that separate unit lands.

from __future__ import annotations

import socket
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from solet_setup_contracts.release_observer_codec import (
    ObserverContractError,
    observation_artifacts,
    validate_observation,
)
from solet_setup_contracts.release_observer_contract import (
    ArtifactRef,
    GitOID,
    ModuleBinding,
    ObservationResult,
    ObserverBinding,
    ObserveRequest,
    ReadAttestation,
    ReadObservationRequest,
    ReadObservationResult,
)

from .paths import ManagerPaths
from .registry import InstanceRegistry
from .release_lock import load_seed_lock
from .release_observer_store import ObservationArchive, publish_immutable_bytes
from .transaction import load_transaction

ObservationProvider = Callable[[ObserveRequest], ObservationResult]

_BINDING_MODULES = (
    "solet_manager.release_observer",
    "solet_manager.release_observer_store",
    "solet_manager.release_operation",
    "solet_manager.release_observer_cli",
)


def installed_manager_binding(
    *,
    state_dir: Path,
    capability_path: Path,
    seed_lock_path: Path | None = None,
) -> ObserverBinding:
    """Return a receipt-backed binding for these installed Manager bytes.

    The pin comes only from the installed immutable seed lock.  This does not
    infer a pin from a working tree, an actor, or a caller-supplied request.
    """

    lock_path = (
        Path(sys.prefix) / "share" / "solet" / "seed.lock.json"
        if seed_lock_path is None
        else seed_lock_path
    )
    seed = load_seed_lock(lock_path)
    modules = tuple(_module_binding(name) for name in _BINDING_MODULES)
    capability_bytes = capability_path.read_bytes()
    payload: dict[str, object] = {
        "schema_version": "manager_observer_binding_receipt.v1",
        "producer_id": "manager",
        "producer_generation": "manager-" + seed.commit[:12],
        "executable": str(Path(sys.executable).resolve(strict=True)),
        "interpreter": str(Path(sys.executable).resolve(strict=True)),
        "prefix": str(Path(sys.prefix).resolve(strict=True)),
        "modules": [
            {"name": item.name, "path": item.path, "sha256": item.sha256} for item in modules
        ],
        "release_pin": {"algorithm": "sha1", "value": seed.commit},
        "host_id": socket.gethostname(),
        "namespace": "host",
        "capability_contract_sha256": sha256(capability_bytes).hexdigest(),
    }
    receipt_path, receipt_sha256, receipt_size = publish_immutable_bytes(
        state_dir / "release-proof",
        category="installed-receipts",
        artifact_id="manager-binding",
        payload=payload,
    )
    return ObserverBinding(
        producer_id="manager",
        producer_generation="manager-" + seed.commit[:12],
        executable=str(Path(sys.executable).resolve(strict=True)),
        executable_sha256=_file_sha256(Path(sys.executable).resolve(strict=True)),
        interpreter=str(Path(sys.executable).resolve(strict=True)),
        prefix=str(Path(sys.prefix).resolve(strict=True)),
        modules=modules,
        package="manager",
        release_pin=GitOID("sha1", seed.commit),
        host_id=socket.gethostname(),
        namespace="host",
        capability_contract_sha256=sha256(capability_bytes).hexdigest(),
        installed_receipt=ArtifactRef(
            "manager-binding-" + receipt_sha256[:12],
            "file://" + str(receipt_path),
            receipt_sha256,
            receipt_size,
            "application/json",
            "manager",
        ),
    )


def _module_binding(name: str) -> ModuleBinding:
    module_path = Path(__import__(name, fromlist=["__file__"]).__file__ or "").resolve(strict=True)
    return ModuleBinding(name, str(module_path), _file_sha256(module_path))


def _file_sha256(path: Path) -> str:
    if not path.is_file():
        raise ObserverContractError(
            "missing_observation", "installed Manager provenance path is not a regular file"
        )
    return sha256(path.read_bytes()).hexdigest()


class ManagerReleaseObserver:
    """Publishes only a manager-owned provider result; never executes a probe."""

    def __init__(
        self,
        archive: ObservationArchive,
        provider: ObservationProvider,
        paths: ManagerPaths | None = None,
    ) -> None:
        self._archive = archive
        self._provider = provider
        self._paths = paths

    def observe(self, request: ObserveRequest) -> ObservationResult:
        if self._paths is not None:
            PassiveManagerState(self._paths).validate(request)
        result = self._provider(request)
        if result.request != request:
            raise ObserverContractError(
                "binding_mismatch", "owner result does not contain exact request"
            )
        # The U1 codec verifies every nested header, target and challenge.
        validate_observation(
            request, result, expected_binding=result.observer_binding, now=_utc_now(), previous=None
        )
        return self._archive.publish(result)

    def read(self, query: ReadObservationRequest) -> ReadObservationResult:
        observation = self._archive.read(query.observation_id)
        if observation.digest != query.observation_sha256:
            raise ObserverContractError("digest_mismatch", "historical observation digest differs")
        attestation = ReadAttestation(
            query.digest,
            observation.observation_id,
            observation.digest,
            observation.producer_id,
            observation.producer_generation,
            observation.observer_binding,
            _utc_now(),
            observation_artifacts(observation),
            query.transport_challenge,
        )
        return ReadObservationResult(query, observation, attestation)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PassiveManagerState:
    """Read-only Manager binding validation; never invokes doctor or a lock helper.

    DoctorFacts are deliberately refused until iss_a9d970c5 supplies the
    required persisted doctor-result artifact.  Reading doctor.py would run
    probes and write its journal, so it is not an observer input.
    """

    def __init__(self, paths: ManagerPaths) -> None:
        self._paths = paths

    def validate(self, request: ObserveRequest) -> None:
        records = InstanceRegistry(self._paths.registry_path).list()
        matching = [
            item for item in records if item.target == request.header.allocated_target.app_home
        ]
        if len(matching) != 1:
            raise ObserverContractError(
                "identity_mismatch", "request target has no unique Manager registry owner"
            )
        record = matching[0]
        transaction = load_transaction(self._paths.transaction_path(record.name))
        if transaction is None:
            raise ObserverContractError("missing_observation", "Manager transaction is absent")
        if (
            transaction.name,
            transaction.target,
            transaction.flow_id,
            transaction.flow_contract_digest,
        ) != (
            record.name,
            record.target,
            record.flow_id,
            record.flow_contract_digest,
        ):
            raise ObserverContractError(
                "identity_mismatch", "registry and canonical transaction differ"
            )
        if request.header.phase == "DOCTOR":
            raise ObserverContractError(
                "missing_observation",
                "persisted doctor result unavailable; prerequisite iss_a9d970c5",
            )
        if request.header.phase == "SETUP":
            complete = bool(transaction.completion) and all(
                item.value == "verified" for item in transaction.completion.values()
            )
            if transaction.status.value != "verified" or not complete:
                raise ObserverContractError(
                    "missing_observation", "canonical setup frontier is not fully verified"
                )
