"""Refuse an imported solet at the v1 verbs, naming the Manager verb that does serve it (iss_33637918)."""

from __future__ import annotations

from .errors import InstanceImportedError
from .maintenance_inventory import read_maintenance_inventory_v2
from .paths import ManagerPaths
from .registry import InstanceRegistry

# Every v1 verb that resolves a name through the create registry (``InstanceRegistry.require`` / ``get``).
IMPORTED_REFUSED_VERBS = frozenset({"status", "start", "doctor", "attest", "reconcile-contract", "reconcile-adapter", "reconcile-identity"})


def refuse_imported(paths: ManagerPaths, verb: str, name: str | None) -> None:
    """Raise when ``name`` is owned by the v2 inventory alone; read-only, so it sits before any state write."""
    if verb not in IMPORTED_REFUSED_VERBS or name is None or InstanceRegistry(paths.registry_path).get(name) is not None:
        return
    row = next((item for item in read_maintenance_inventory_v2(paths.maintenance_inventory_path) if item.name == name), None)
    if row is None:
        return
    if verb == "start":
        repair = (
            "The Manager has no start verb for an imported solet; launchd starts it: "
            f"`launchctl kickstart -k gui/$(id -u)/{row.service_identity.launchagent_label}`, then `{name} health`."
        )
    else:
        repair = f"Run `solet-manager doctor {name}` (`solet-manager update {name} --dry-run` for updates)."
    raise InstanceImportedError(f"{name!r} was imported, not created by `solet create`; `solet {verb}` reads only create records", repair=repair)
