"""Focused pg_hba SCRAM-oracle checks used by the adapter contract smoke."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

Check = Callable[[object, str], None]


def _completed() -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], 0, "", "")


def _no_op_runner(
    _command: list[str], **_kwargs: object,
) -> subprocess.CompletedProcess[str]:
    return _completed()


def check_pg_hba_scram_oracle(root: Path, *, check: Check, now: datetime) -> None:
    """Prove comments, transport-specific rules, and trust cannot satisfy SCRAM."""

    from bootstrap_adapter.models import AdapterError, AdapterRuntime, RolePolicyObservation
    from bootstrap_adapter.postgres import (
        _default_scram_lines,
        _inspect_pg_hba,
        apply_postgres_configuration,
        atomic_write_scram_hba,
    )

    commented_scram = root / "commented-pg_hba.conf"
    commented_scram.write_text(
        "\n".join(f"# {line}" for line in _default_scram_lines())
        + "\nlocal all all trust\n"
        + "host all all 127.0.0.1/32 trust\n"
        + "host all all ::1/128 trust\n"
    )
    commented_scram.chmod(0o600)
    check(
        _inspect_pg_hba(commented_scram) == (True, True, False),
        "RED-FIRST pg_hba oracle ignores commented-out SCRAM strings",
    )
    atomic_write_scram_hba(commented_scram)
    check(
        _inspect_pg_hba(commented_scram) == (True, True, True),
        "RED-FIRST comment-only SCRAM no longer makes the writer skip a real fix",
    )

    unverified_scram = root / "unverified-pg_hba.conf"
    unverified_scram.write_text("local all all trust\n")
    unverified_scram.chmod(0o600)
    runtime = AdapterRuntime(
        run=_no_op_runner,
        which=lambda _name: None,
        now=lambda: now,
        name="adaptertest",
        target=root,
    )
    policy = RolePolicyObservation(
        False, False, None, None, False, None, False, False,
        unverified_scram, True, True, False,
    )
    with patch("bootstrap_adapter.postgres.atomic_write_scram_hba"):
        try:
            apply_postgres_configuration(
                runtime, policy, [{"id": "postgres.edit_pg_hba_scram"}]
            )
        except AdapterError as exc:
            check(
                "verification failed after apply" in str(exc),
                "RED-FIRST no-op pg_hba write is rejected by the post-write re-read",
            )
        else:
            check(False, "pg_hba apply did not verify its written result")

    preempted_scram = root / "preempted-pg_hba.conf"
    preempted_scram.write_text(
        "local all all trust\n"
        "host all all 127.0.0.1/32 trust\n"
        "host all all ::1/128 trust\n"
        + "\n".join(_default_scram_lines())
        + "\n"
    )
    preempted_scram.chmod(0o600)
    check(
        _inspect_pg_hba(preempted_scram) == (True, False, False),
        "RED-FIRST pg_hba oracle rejects first-match trust before SCRAM",
    )

    ssl_only_scram = root / "ssl-only-scram-pg_hba.conf"
    ssl_only_scram.write_text(
        "local all all scram-sha-256\n"
        "hostssl all all 127.0.0.1/32 scram-sha-256\n"
        "hostssl all all ::1/128 scram-sha-256\n"
        "hostnossl all all 127.0.0.1/32 trust\n"
        "hostnossl all all ::1/128 trust\n"
    )
    ssl_only_scram.chmod(0o600)
    check(
        _inspect_pg_hba(ssl_only_scram) == (True, False, False),
        "RED-FIRST hostssl SCRAM cannot verify passwordless hostnossl traffic",
    )

    for connection_type in ("hostgssenc", "hostnogssenc"):
        gss_preempted_scram = root / f"{connection_type}-preempted-pg_hba.conf"
        gss_preempted_scram.write_text(
            "local all all scram-sha-256\n"
            f"{connection_type} all all 127.0.0.1/32 trust\n"
            "hostssl all all 127.0.0.1/32 scram-sha-256\n"
            "hostnossl all all 127.0.0.1/32 scram-sha-256\n"
            "hostssl all all ::1/128 scram-sha-256\n"
            "hostnossl all all ::1/128 scram-sha-256\n"
        )
        gss_preempted_scram.chmod(0o600)
        check(
            _inspect_pg_hba(gss_preempted_scram) == (True, False, False),
            f"RED-FIRST {connection_type} trust cannot hide behind later hostssl SCRAM",
        )
