"""Immutable, manager-owned release-observation archive.

The archive deliberately has no dependency on Manager transaction helpers: those
helpers may create paths or take write locks.  This module only publishes its
own receipts with exclusive creation and reads committed bytes back verbatim.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from solet_setup_contracts.release_observer_codec import (
    ObserverContractError,
    decode_message,
    encode_message,
)
from solet_setup_contracts.release_observer_contract import ObservationResult


def publish_immutable_bytes(
    root: Path,
    *,
    category: str,
    artifact_id: str,
    payload: dict[str, object],
) -> tuple[Path, str, int]:
    """Publish one canonical manager-owned receipt, refusing different replay."""

    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    directory = root / category
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = directory / f"{artifact_id}-{digest}.json"
    try:
        fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        if destination.read_bytes() != encoded:
            raise ObserverContractError(
                "request_conflict", "immutable manager receipt bytes differ"
            ) from exc
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    return destination, digest, len(encoded)


class ObservationArchive:
    """Append-only archive keyed by observation id and canonical request bytes."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def publish(self, result: ObservationResult) -> ObservationResult:
        encoded = encode_message(result)
        directory = self._root / "observations"
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        destination = directory / f"{result.observation_id}.json"
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError as exc:
            prior = self.read(result.observation_id)
            if encode_message(prior) != encoded:
                raise ObserverContractError(
                    "request_conflict", "observation id has different canonical bytes"
                ) from exc
            return prior
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            try:
                destination.unlink(missing_ok=True)
            finally:
                raise
        return result

    def read(self, observation_id: str) -> ObservationResult:
        source = self._root / "observations" / f"{observation_id}.json"
        try:
            value = decode_message(source.read_bytes())
        except FileNotFoundError as exc:
            raise ObserverContractError(
                "missing_observation", "manager observation is absent"
            ) from exc
        if not isinstance(value, ObservationResult) or value.observation_id != observation_id:
            raise ObserverContractError(
                "duplicate_different", "archive record is not its requested observation"
            )
        return value
