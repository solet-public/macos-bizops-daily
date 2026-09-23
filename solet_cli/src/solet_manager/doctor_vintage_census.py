"""Passive manager-versus-target doctor advisories.

These checks deliberately supplement, rather than join, the target's pinned
completion contract.  A target may legitimately be internally consistent
while belonging to an older release; that is useful diagnosis information, not
grounds to refuse an otherwise valid doctor result.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import cast

from .contracts import contract_digest, discover_contract_directory
from .doctor_check_results import unknown as _unknown
from .doctor_check_results import verified as _verified
from .doctor_check_results import warn as _warn
from .errors import ContractError, SourceError
from .models import InstanceRecord, JsonValue
from .release_lock import load_seed_lock
from .transaction import Transaction

LaunchctlRunner = Callable[[tuple[str, ...]], tuple[int, str, str]]

_SEED_LOCK_NAME = "seed.lock.json"
_LAUNCHCTL_STATE = re.compile(r"(?m)^\s*state\s*=\s*([^\n]+?)\s*$")
_LAUNCHCTL_LAST_EXIT = re.compile(
    r"(?m)^\s*last exit (?:code|status)\s*=\s*([^\n]+?)\s*$"
)
_LAUNCHCTL_RUNS = re.compile(r"(?m)^\s*(?:runs|run count)\s*=\s*([^\n]+?)\s*$")
_LAUNCHCTL_NEVER_EXITED = "(never exited)"


def collect_doctor_advisories(
    record: InstanceRecord,
    transaction: Transaction,
    *,
    manager_seed_lock_path: Path | None = None,
    release_contract_directory: Path | None = None,
    launchctl_runner: LaunchctlRunner | None = None,
) -> list[JsonValue]:
    """Return report-only checks comparing manager and target persisted state."""

    seed_lock_path = (
        Path(sys.prefix) / "share" / "solet" / _SEED_LOCK_NAME
        if manager_seed_lock_path is None
        else manager_seed_lock_path
    )
    runner = _run_launchctl if launchctl_runner is None else launchctl_runner
    labels = _declared_launchagent_labels(record, transaction)
    observations = [_launchctl_observation(label, runner) for label in labels]
    return [
        _seed_vintage_advisory(record, seed_lock_path),
        _contract_digest_advisory(record, release_contract_directory),
        _background_item_advisory(labels, observations),
        _boot_history_advisory(observations),
    ]


def _seed_vintage_advisory(record: InstanceRecord, seed_lock_path: Path) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {
        "manager_seed_commit": None,
        "manager_seed_tree_hash": None,
    }
    observed: dict[str, JsonValue] = {
        "instance_seed_commit": record.seed_commit,
        "instance_seed_tree_hash": record.seed_tree_hash,
    }
    try:
        manager_seed = load_seed_lock(seed_lock_path)
    except SourceError as exc:
        return _unknown(
            "doctor::manager_seed_vintage_v1",
            "The manager seed lock could not be read, so target vintage is unknown.",
            expected,
            observed,
            str(seed_lock_path),
            "manager_seed_lock_unreadable",
            str(exc),
        )
    expected = {
        "manager_seed_commit": manager_seed.commit,
        "manager_seed_tree_hash": manager_seed.tree_hash,
    }
    if (
        record.seed_commit == manager_seed.commit
        and record.seed_tree_hash == manager_seed.tree_hash
    ):
        return _verified(
            "doctor::manager_seed_vintage_v1",
            "Target seed identity matches the installed manager seed.",
            expected,
            observed,
            str(seed_lock_path),
        )
    return _warn(
        "doctor::manager_seed_vintage_v1",
        "Target seed identity differs from the installed manager seed.",
        expected,
        observed,
        str(seed_lock_path),
        "manager_seed_vintage_mismatch",
        "Inspect `solet reconcile-adapter` availability before refreshing target-local code.",
    )


def _contract_digest_advisory(
    record: InstanceRecord,
    release_contract_directory: Path | None,
) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {"release_flow_contract_digest": None}
    observed: dict[str, JsonValue] = {
        "instance_flow_contract_digest": record.flow_contract_digest,
    }
    try:
        directory = (
            discover_contract_directory(None)
            if release_contract_directory is None
            else release_contract_directory
        )
        release_digest = contract_digest(directory)
    except ContractError as exc:
        return _unknown(
            "doctor::persisted_contract_digest_v1",
            "The manager release contract could not be read, so contract vintage is unknown.",
            expected,
            observed,
            "manager_release_contract",
            "release_contract_unreadable",
            str(exc),
        )
    expected = {"release_flow_contract_digest": release_digest}
    if record.flow_contract_digest == release_digest:
        return _verified(
            "doctor::persisted_contract_digest_v1",
            "Target persisted contract digest matches the manager release contract.",
            expected,
            observed,
            str(directory),
        )
    return _warn(
        "doctor::persisted_contract_digest_v1",
        "Target persisted contract digest differs from the manager release contract.",
        expected,
        observed,
        str(directory),
        "persisted_contract_digest_mismatch",
        "Review `solet reconcile-contract --dry-run` for this managed instance.",
    )


def _background_item_advisory(
    labels: tuple[str, ...],
    observations: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {
        "declared_labels": [cast(JsonValue, label) for label in labels],
        "declared_count": len(labels),
    }
    observed_count = sum(item["present"] is True for item in observations)
    observed: dict[str, JsonValue] = {
        "labels": cast(JsonValue, observations),
        "observed_count": observed_count,
    }
    if any(item["query_status"] == "unknown" for item in observations):
        return _unknown(
            "doctor::background_item_cardinality_v1",
            "One or more declared background items could not be inspected.",
            expected,
            observed,
            "launchctl:print",
            "background_item_status_unreadable",
            "launchctl output did not provide a usable item status.",
        )
    if observed_count == len(labels):
        return _verified(
            "doctor::background_item_cardinality_v1",
            "Every declared background item is installed.",
            expected,
            observed,
            "launchctl:print",
        )
    return _warn(
        "doctor::background_item_cardinality_v1",
        "Installed background-item count differs from the declared set.",
        expected,
        observed,
        "launchctl:print",
        "background_item_cardinality_mismatch",
        "Inspect the declared LaunchAgent labels and their installation receipts.",
    )


def _boot_history_advisory(
    observations: list[dict[str, JsonValue]],
) -> dict[str, JsonValue]:
    expected: dict[str, JsonValue] = {"maximum_clean_boot_attempts": 1}
    observed: dict[str, JsonValue] = {"labels": cast(JsonValue, observations)}
    readable = [item for item in observations if item["query_status"] == "readable"]
    if len(readable) != len(observations):
        return _unknown(
            "doctor::launchagent_boot_history_v1",
            "LaunchAgent boot history is unavailable for one or more declared items.",
            expected,
            observed,
            "launchctl:print",
            "launchagent_boot_history_unreadable",
            "launchctl output did not provide boot-history fields for every declared item.",
        )
    retried = [item for item in readable if isinstance(item["runs"], int) and item["runs"] > 1]
    if not retried:
        return _verified(
            "doctor::launchagent_boot_history_v1",
            "Declared LaunchAgents have no repeated observed boot attempts.",
            expected,
            observed,
            "launchctl:print",
        )
    return _warn(
        "doctor::launchagent_boot_history_v1",
        "A declared LaunchAgent is running after more than one observed boot attempt.",
        expected,
        observed,
        "launchctl:print",
        "launchagent_retried_boot",
        (
            "Review LaunchAgent logs for the earlier boot attempt; "
            "this advisory does not refuse doctor."
        ),
    )


def _declared_launchagent_labels(
    record: InstanceRecord,
    transaction: Transaction,
) -> tuple[str, ...]:
    labels = [f"local.solet.{record.name}"]
    profile = transaction.answers.get("setup_profile")
    if profile == "macos-bizops":
        labels.append(f"local.solet.{record.name}.router")
    return tuple(labels)


def _launchctl_observation(label: str, runner: LaunchctlRunner) -> dict[str, JsonValue]:
    code, output, error = runner(
        ("/bin/launchctl", "print", f"gui/{os.getuid()}/{label}")
    )
    if code != 0:
        return {
            "label": label,
            "present": False,
            "query_status": "readable",
            "launchctl_exit_code": code,
            "runs": None,
            "state": None,
            "last_exit_code": None,
            "query_error": error or None,
        }
    state = _field(_LAUNCHCTL_STATE, output)
    runs = _integer_field(_LAUNCHCTL_RUNS, output)
    last_exit = _last_exit_field(state, runs, output)
    if state is None or last_exit is None or runs is None:
        return {
            "label": label,
            "present": True,
            "query_status": "unknown",
            "launchctl_exit_code": code,
            "runs": runs,
            "state": state,
            "last_exit_code": last_exit,
            "query_error": "launchctl output omitted a required status field",
        }
    return {
        "label": label,
        "present": True,
        "query_status": "readable",
        "launchctl_exit_code": code,
        "runs": runs,
        "state": state,
        "last_exit_code": last_exit,
        "query_error": None,
    }


def _field(pattern: re.Pattern[str], output: str) -> str | None:
    match = pattern.search(output)
    return None if match is None else match.group(1)


def _integer_field(pattern: re.Pattern[str], output: str) -> int | None:
    value = _field(pattern, output)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _last_exit_field(state: str | None, runs: int | None, output: str) -> int | str | None:
    value = _field(_LAUNCHCTL_LAST_EXIT, output)
    if value == _LAUNCHCTL_NEVER_EXITED and state == "running" and runs == 1:
        return value
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _run_launchctl(command: tuple[str, ...]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 125, "", str(exc)
    return result.returncode, result.stdout, result.stderr
