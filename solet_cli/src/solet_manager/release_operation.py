"""Manager-owned binding for a release-proof execution request."""

from __future__ import annotations

import os
from pathlib import Path

from solet_setup_contracts.release_observer_codec import ObserverContractError, decode_message, encode_message
from solet_setup_contracts.release_observer_contract import ExecutionRequest


class ReleaseOperationStore:
    """Persists a pre-invocation binding without deriving identities from journals."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def bind(self, request: ExecutionRequest) -> ExecutionRequest:
        encoded = encode_message(request)
        directory = self._root / "release-operations"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / f"{request.header.operation_id}.json"
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            existing = self.read(request.header.operation_id)
            if encode_message(existing) != encoded:
                raise ObserverContractError("request_conflict", "operation id already has different binding") from exc
            return existing
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        return request

    def read(self, operation_id: str) -> ExecutionRequest:
        try:
            value = decode_message((self._root / "release-operations" / f"{operation_id}.json").read_bytes())
        except FileNotFoundError as exc:
            raise ObserverContractError("missing_observation", "release operation binding is absent") from exc
        if not isinstance(value, ExecutionRequest) or value.header.operation_id != operation_id:
            raise ObserverContractError("duplicate_different", "stored operation binding is malformed")
        return value
