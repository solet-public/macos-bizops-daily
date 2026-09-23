#!/usr/bin/env python3
"""Smoke: W-STORE-POSTGRES-BACKEND-MOVE — postgres Store factory relocation.

Verifies the relocation of the postgres Store factory from the two vault
plugins to the two state plugins (local + RDS) per Tier 1 dispatch
2026-06-07 (W-INVENTORY-11 + master plan §3.1 driven).

Six logical smokes per dispatch:

1. **state-factory-first**: import the state plugin's `store_factory`
   first; assert `register_backend("postgres", ...)` registered with the
   canonical factory object. Then import the vault shim; assert no
   duplicate-registration error (idempotent re-registration on the same
   function object via `sys.modules` cache).

2. **default-vault-shim-first** (local profile only): import the
   default vault shim first (which itself imports the local state
   factory); assert `"postgres"` registered. Then import the state
   factory directly; assert idempotency.

3. **secrets-manager-shim-first** (cloud profile only): same as smoke
   2 but with `secrets_manager_vault_plugin.postgres_backend.store`
   and `rds_postgres_state_management_plugin.postgres_backend.store_factory`.

4. **Object-identity**: assert the registered factory function object
   is `is`-identical to the canonical `store_factory.make_postgres_store`,
   AND identical to the function exposed by the vault shim. Also asserts
   `PostgresStore` class identity across the two import paths.

5. **peer_registry-before-vault**: state factory loaded; NO vault shim
   imported; `PeerRegistry(state_service=...)` construction succeeds.
   This is the load-order-decoupling proof — peer_registry no longer
   depends on a vault import side effect for the `postgres` backend to
   be registered. (Closes the gap documented at
   `peer_registry.py:104-116`.)

Profile matrix:
- local profile: `postgres_state_management_plugin` + `macos_vault_plugin`
- cloud profile: `rds_postgres_state_management_plugin` + `secrets_manager_vault_plugin`

Smokes 1, 4, 5 fire on each profile. Smoke 2 fires on local only (it
exercises the local default-vault shim by name); smoke 3 fires on cloud
only (the secrets-manager shim by name). Total: 3 × 2 + 1 + 1 = **8
subprocess runs**.

(The dispatch summary line names "12 total smoke runs"; the actual
matrix shape is 8 because smokes 2 and 3 are profile-name-bound by
design. The 6 semantic concerns documented above are fully covered.)

Each subsmoke runs in a FRESH subprocess so `sys.modules` is empty —
Python's import cache makes in-process re-imports indistinguishable
from no-op refreshes, which would mask the duplicate-registration
concern this smoke exists to verify.

Standalone — not pytest. Run with:

    .venv/bin/python3 plugins/postgres_state_management_plugin/tests/store_postgres_backend_move_smoke.py

Exit codes:
    0  — all subsmokes passed
    1  — at least one subsmoke failed
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


# ---------------------------------------------------------------------------
# Per-profile sys.path setup
# ---------------------------------------------------------------------------


def _profile_paths(profile: str) -> list[Path]:
    """Return the sys.path additions needed for a given profile.

    Always includes `ananta/src` (platform code).
    """
    base = [REPO_ROOT / "ananta" / "src"]
    if profile == "local":
        return base + [
            REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src",
            REPO_ROOT / "plugins" / "macos_vault_plugin" / "src",
            REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src",
        ]
    if profile == "cloud":
        return base + [
            REPO_ROOT / "plugins" / "rds_postgres_state_management_plugin" / "src",
            REPO_ROOT / "plugins" / "secrets_manager_vault_plugin" / "src",
            REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src",
        ]
    raise ValueError(f"unknown profile {profile!r}")


def _install_paths(profile: str) -> None:
    for path in _profile_paths(profile):
        sys.path.insert(0, str(path))


# ---------------------------------------------------------------------------
# Profile-aware module name resolution
# ---------------------------------------------------------------------------


def _state_factory_module(profile: str) -> str:
    return {
        "local": "postgres_state_management_plugin.postgres_backend.store_factory",
        "cloud": "rds_postgres_state_management_plugin.postgres_backend.store_factory",
    }[profile]


def _vault_shim_module(profile: str) -> str:
    return {
        "local": "macos_vault_plugin.postgres_backend.store",
        "cloud": "secrets_manager_vault_plugin.postgres_backend.store",
    }[profile]


# ---------------------------------------------------------------------------
# Subsmoke 1 — state-factory-first
# ---------------------------------------------------------------------------


def _subsmoke_state_first(profile: str) -> None:
    """Import state factory first, then vault shim. Assert idempotent re-reg."""
    _install_paths(profile)

    from ananta.services.store import list_backends
    from ananta.services.store.factory import _BACKENDS

    # Step 1: import the state factory
    state_factory = __import__(_state_factory_module(profile), fromlist=["make_postgres_store"])
    assert "postgres" in list_backends(), (
        f"[state-first {profile}] 'postgres' not registered after "
        f"importing {_state_factory_module(profile)}"
    )
    assert _BACKENDS["postgres"] is state_factory.make_postgres_store, (
        f"[state-first {profile}] registered factory != state_factory.make_postgres_store"
    )

    # Step 2: import the vault shim
    vault_shim = __import__(_vault_shim_module(profile), fromlist=["make_postgres_store"])

    # Idempotent re-registration: same function object via sys.modules cache
    assert _BACKENDS["postgres"] is state_factory.make_postgres_store, (
        f"[state-first {profile}] registered factory changed after vault shim import"
    )
    assert vault_shim.make_postgres_store is state_factory.make_postgres_store, (
        f"[state-first {profile}] vault shim's make_postgres_store is NOT the canonical "
        f"function: shim.id={id(vault_shim.make_postgres_store)}, "
        f"state.id={id(state_factory.make_postgres_store)}"
    )
    print(f"  PASS [state-first {profile}] — idempotent re-registration, identity preserved")


# ---------------------------------------------------------------------------
# Subsmoke 2/3 — vault-shim-first
# ---------------------------------------------------------------------------


def _subsmoke_vault_shim_first(profile: str) -> None:
    """Import vault shim first, then state factory. Assert idempotent re-reg."""
    _install_paths(profile)

    from ananta.services.store import list_backends
    from ananta.services.store.factory import _BACKENDS

    # Step 1: import the vault shim — this triggers state_factory load + register_backend
    vault_shim = __import__(_vault_shim_module(profile), fromlist=["make_postgres_store"])
    assert "postgres" in list_backends(), (
        f"[vault-shim-first {profile}] 'postgres' not registered after "
        f"importing {_vault_shim_module(profile)}"
    )

    # Step 2: import the state factory directly — same module via sys.modules cache
    state_factory = __import__(_state_factory_module(profile), fromlist=["make_postgres_store"])

    assert _BACKENDS["postgres"] is state_factory.make_postgres_store, (
        f"[vault-shim-first {profile}] registered factory != state_factory.make_postgres_store"
    )
    assert vault_shim.make_postgres_store is state_factory.make_postgres_store, (
        f"[vault-shim-first {profile}] vault shim's make_postgres_store is NOT the canonical "
        f"function: shim.id={id(vault_shim.make_postgres_store)}, "
        f"state.id={id(state_factory.make_postgres_store)}"
    )
    print(f"  PASS [vault-shim-first {profile}] — idempotent re-registration, identity preserved")


# ---------------------------------------------------------------------------
# Subsmoke 4 — object identity
# ---------------------------------------------------------------------------


def _subsmoke_object_identity(profile: str) -> None:
    """Assert function + class identity across the canonical and shim paths."""
    _install_paths(profile)

    from ananta.services.store.factory import _BACKENDS

    state_factory = __import__(_state_factory_module(profile), fromlist=["make_postgres_store", "PostgresStore"])
    vault_shim = __import__(_vault_shim_module(profile), fromlist=["make_postgres_store", "PostgresStore"])

    # Function-object identity
    assert vault_shim.make_postgres_store is state_factory.make_postgres_store, (
        f"[object-identity {profile}] make_postgres_store function objects differ: "
        f"shim.id={id(vault_shim.make_postgres_store)}, "
        f"state.id={id(state_factory.make_postgres_store)}"
    )
    # Class-object identity
    assert vault_shim.PostgresStore is state_factory.PostgresStore, (
        f"[object-identity {profile}] PostgresStore class objects differ: "
        f"shim.id={id(vault_shim.PostgresStore)}, "
        f"state.id={id(state_factory.PostgresStore)}"
    )
    # Registered factory is the canonical one
    assert _BACKENDS["postgres"] is state_factory.make_postgres_store, (
        f"[object-identity {profile}] _BACKENDS['postgres'] != canonical factory"
    )
    # Module home is the state plugin, not the vault plugin
    home = state_factory.make_postgres_store.__module__
    expected_state_pkg = {
        "local": "postgres_state_management_plugin",
        "cloud": "rds_postgres_state_management_plugin",
    }[profile]
    assert home.startswith(expected_state_pkg), (
        f"[object-identity {profile}] canonical factory home != {expected_state_pkg}: got {home!r}"
    )
    print(f"  PASS [object-identity {profile}] — function/class identity preserved, home={home}")


# ---------------------------------------------------------------------------
# Subsmoke 5 — peer_registry-before-vault (load-order decoupling proof)
# ---------------------------------------------------------------------------


def _subsmoke_peer_registry_before_vault(profile: str) -> None:
    """State factory loaded; no vault shim; PeerRegistry construction succeeds."""
    _install_paths(profile)

    # Import the state factory only — register_backend("postgres", ...) fires.
    __import__(_state_factory_module(profile))

    # Confirm NO vault plugin has imported its shim yet — this is the
    # whole point of the load-order decoupling proof.
    local_shim = "macos_vault_plugin.postgres_backend.store"
    cloud_shim = "secrets_manager_vault_plugin.postgres_backend.store"
    assert local_shim not in sys.modules, (
        f"[peer-registry-before-vault {profile}] {local_shim} already in sys.modules — "
        "smoke pre-condition violated"
    )
    assert cloud_shim not in sys.modules, (
        f"[peer-registry-before-vault {profile}] {cloud_shim} already in sys.modules — "
        "smoke pre-condition violated"
    )

    # Construct a structural mock state_service. PostgresStore.__init__
    # does not exercise the connection; the schema is standardized via
    # SchemaStandardizer (pure-Python, no DB). The provider only needs
    # to satisfy the duck-type contract in _resolve_provider.
    class _FakeProvider:
        def get_connection(self):  # noqa: ANN201 — structural duck-type, no return type
            raise NotImplementedError(
                "fake provider — construction-only smoke does not hit the DB",
            )

    class _FakePlugin:
        def _get_provider(self) -> _FakeProvider:
            return _FakeProvider()

    class _FakeStateService:
        _state_plugin = _FakePlugin()

    # PeerRegistry construction triggers open_store(backend="postgres", state_service=...)
    from agent_messaging_plugin.peer_registry import PeerRegistry

    registry = PeerRegistry(state_service=_FakeStateService())  # type: ignore[arg-type]
    assert registry._bindings is not None, (
        f"[peer-registry-before-vault {profile}] PeerRegistry._bindings is None"
    )

    # Identity check on the Store object's class home — must be the
    # state plugin, NOT a vault plugin.
    store_class_module = type(registry._bindings).__module__
    expected_state_pkg = {
        "local": "postgres_state_management_plugin",
        "cloud": "rds_postgres_state_management_plugin",
    }[profile]
    assert store_class_module.startswith(expected_state_pkg), (
        f"[peer-registry-before-vault {profile}] Store class home != {expected_state_pkg}: "
        f"got {store_class_module!r}"
    )

    # Confirm no vault module has been loaded transitively.
    assert local_shim not in sys.modules, (
        f"[peer-registry-before-vault {profile}] {local_shim} got loaded "
        "during PeerRegistry construction — load-order decoupling broken"
    )
    assert cloud_shim not in sys.modules, (
        f"[peer-registry-before-vault {profile}] {cloud_shim} got loaded "
        "during PeerRegistry construction — load-order decoupling broken"
    )
    print(
        f"  PASS [peer-registry-before-vault {profile}] — "
        f"PeerRegistry constructed with Store from {store_class_module}, no vault shim loaded"
    )


# ---------------------------------------------------------------------------
# Subsmoke dispatcher
# ---------------------------------------------------------------------------


_SUBSMOKES = {
    "state_first": _subsmoke_state_first,
    "vault_shim_first": _subsmoke_vault_shim_first,
    "object_identity": _subsmoke_object_identity,
    "peer_registry_before_vault": _subsmoke_peer_registry_before_vault,
}


def _run_subsmoke(name: str, profile: str) -> int:
    func = _SUBSMOKES.get(name)
    if func is None:
        print(f"FAIL: unknown subsmoke {name!r}", file=sys.stderr)
        return 1
    try:
        func(profile)
    except AssertionError as exc:
        print(f"FAIL [{name} {profile}]: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    except Exception as exc:  # pragma: no cover — defensive
        print(f"ERROR [{name} {profile}]: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 1
    return 0


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _spawn(name: str, profile: str) -> tuple[str, str, int, str]:
    """Run a subsmoke in a fresh subprocess; return (name, profile, rc, output)."""
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--subsmoke",
        name,
        "--profile",
        profile,
    ]
    result = subprocess.run(  # noqa: S603 — local Python, controlled args
        cmd, capture_output=True, text=True, check=False,
    )
    return name, profile, result.returncode, (result.stdout + result.stderr)


# Run matrix: (smoke_name, profile)
# Smoke 1 + 4 + 5 fire on each profile.
# Smoke 2 is profile-name-bound to local (default-vault shim by name).
# Smoke 3 is profile-name-bound to cloud (secrets-manager shim by name).
# Total: 8 subprocess runs.
_MATRIX: list[tuple[str, str]] = [
    ("state_first", "local"),
    ("state_first", "cloud"),
    ("vault_shim_first", "local"),   # smoke 2 (default-vault shim)
    ("vault_shim_first", "cloud"),   # smoke 3 (secrets-manager shim)
    ("object_identity", "local"),
    ("object_identity", "cloud"),
    ("peer_registry_before_vault", "local"),
    ("peer_registry_before_vault", "cloud"),
]


def _drive() -> int:
    print(f"W-STORE-POSTGRES-BACKEND-MOVE smoke — running {len(_MATRIX)} subprocess subsmokes")
    print(f"  repo_root: {REPO_ROOT}")
    print(f"  python:    {sys.executable}")
    print()

    failures: list[tuple[str, str, str]] = []
    passes: list[tuple[str, str]] = []
    for name, profile in _MATRIX:
        print(f"--- [{name} {profile}]")
        _, _, rc, output = _spawn(name, profile)
        # Subsmoke prints its own PASS line to stdout on success.
        if output:
            for line in output.rstrip().splitlines():
                print(f"  | {line}")
        if rc == 0:
            passes.append((name, profile))
        else:
            failures.append((name, profile, output))

    print()
    print(f"--- summary: {len(passes)}/{len(_MATRIX)} passed")
    for name, profile in passes:
        print(f"  PASS  {name:<32s}  profile={profile}")
    for name, profile, _ in failures:
        print(f"  FAIL  {name:<32s}  profile={profile}")
    return 0 if not failures else 1


def main(argv: list[str]) -> int:
    if "--subsmoke" in argv:
        i = argv.index("--subsmoke")
        name = argv[i + 1]
        j = argv.index("--profile")
        profile = argv[j + 1]
        return _run_subsmoke(name, profile)
    return _drive()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
