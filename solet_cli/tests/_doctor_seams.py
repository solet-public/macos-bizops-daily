"""Run the real ``InstallationDoctor.run`` with only its seed-unrelated collaborators held still (r61).

The installation doctor loads the create's contract bundle, runs its completion probes and calls sixteen
advisory collectors, several of which reach launchctl or the installed keg.  The seed-tree smokes
(iss_a81b79e3, iss_9cd4359a) need the real ``run`` -- which record it reads, which verification it
reports, what it hands the vintage and release-identity censuses -- and none of the rest.  So:

- ``_doctor_context`` returns the fixture's real create transaction and no bundle;
- ``run_completion_probes`` answers one verified check;
- the vintage census (``collect_doctor_advisories``) is a spy recording the record it was handed;
- the release-identity census is the caller's wrapper, or quiet;
- every other collector is quiet.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import patch

from solet_manager import doctor as doctor_module
from solet_manager.models import InstanceRecord, JsonValue
from solet_manager.paths import ManagerPaths
from solet_manager.transaction import Transaction, load_transaction

__all__ = ["doctor_seams", "no_advisories"]

type Collector = Callable[..., list[JsonValue]]

_QUIET_COLLECTORS = (
    "collect_residue_advisories",
    "collect_blue_green_advisories",
    "collect_seed_integrity_advisories",
    "collect_secret_exposure_advisories",
    "collect_genesis_marker_advisories",
    "collect_postgres_pin_advisories",
    "collect_python_interpreter_advisories",
    "collect_python_on_request_advisories",
    "collect_router_identity_advisories",
    "collect_credential_copy_advisories",
    "collect_plugin_version_skew_advisories",
    "collect_lm_studio_advisories",
    "collect_terminal_return_key_advisories",
    "collect_inference_qualification_advisories",
    "collect_inference_probe_advisories",
)


def no_advisories(*_args: object, **_kwargs: object) -> list[JsonValue]:
    return []


@contextmanager
def doctor_seams(vintage_records: list[InstanceRecord], release_identity: Collector = no_advisories) -> Iterator[None]:
    def context(paths: ManagerPaths, name: str, _record: InstanceRecord, _target: Path) -> tuple[Transaction, None, None]:
        transaction = load_transaction(paths.transaction_path(name))
        if transaction is None:
            raise AssertionError("the fixture lost its create transaction")
        return transaction, None, None

    def probes(_bundle: object, transaction: Transaction, _registry: object, _paths: ManagerPaths) -> tuple[Transaction, list[JsonValue]]:
        return transaction, [{"id": "doctor", "status": "verified"}]

    def vintage(record: InstanceRecord, _transaction: Transaction) -> list[JsonValue]:
        vintage_records.append(record)
        return []

    with ExitStack() as stack:
        stack.enter_context(patch.object(doctor_module, "recover_contract_reconciliation", no_advisories))
        stack.enter_context(patch.object(doctor_module, "_doctor_context", context))
        stack.enter_context(patch.object(doctor_module, "run_completion_probes", probes))
        stack.enter_context(patch.object(doctor_module, "collect_doctor_advisories", vintage))
        stack.enter_context(patch.object(doctor_module, "collect_release_identity_advisories", release_identity))
        for name in _QUIET_COLLECTORS:
            stack.enter_context(patch.object(doctor_module, name, no_advisories))
        yield
