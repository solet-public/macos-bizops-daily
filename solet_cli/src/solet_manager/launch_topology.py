"""Manager-side launch-topology derivation from the registry-owned LaunchAgent plist.

Mirrors the seed's ``target_reconciliation.detect_topology`` rule exactly
(design section 7.1): an interpreter or argument under ``releases/current``
is a materialized supervisor, a direct ``-m ananta.cli`` launch is the legacy
direct vintage, and anything else is unsupported.  The seed re-derives the
same value from the same plist and refuses on disagreement, so this module
must never grow a vintage the seed does not know.
"""

from __future__ import annotations

import hashlib
import plistlib
from pathlib import Path
from typing import Final
from xml.parsers.expat import ExpatError

__all__ = [
    "LEGACY_DIRECT",
    "MATERIALIZED_SUPERVISOR",
    "PLIST_PARSE_ERRORS",
    "SUPPORTED_TOPOLOGIES",
    "UNSUPPORTED_TOPOLOGY",
    "derive_launch_topology",
    "launchagent_plist_path",
    "parse_plist",
    "plist_label",
    "plist_program_arguments",
    "plist_sha256",
]

LEGACY_DIRECT = "legacy_direct"
MATERIALIZED_SUPERVISOR = "materialized_supervisor"
UNSUPPORTED_TOPOLOGY = "unsupported_launch_topology"
SUPPORTED_TOPOLOGIES = frozenset({LEGACY_DIRECT, MATERIALIZED_SUPERVISOR})


def launchagent_plist_path(home: Path, label: str) -> Path:
    """The one plist the update may read for the instance, keyed by its inventory label."""
    return home / "Library" / "LaunchAgents" / f"{label}.plist"


def plist_sha256(raw: bytes) -> str:
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def plist_program_arguments(raw: bytes) -> tuple[str, ...]:
    parsed = _parsed(raw)
    arguments = parsed.get("ProgramArguments")
    if not isinstance(arguments, list):
        return ()
    return tuple(argument for argument in arguments if isinstance(argument, str))


def plist_label(raw: bytes) -> str:
    label = _parsed(raw).get("Label")
    return label if isinstance(label, str) else ""


def derive_launch_topology(raw: bytes) -> str:
    """Derive the launch vintage from the plist bytes; never raises on an unknown shape."""
    arguments = plist_program_arguments(raw)
    if not arguments:
        return UNSUPPORTED_TOPOLOGY
    interpreter = Path(arguments[0])
    if not interpreter.is_absolute():
        return UNSUPPORTED_TOPOLOGY
    parts = interpreter.parts
    if ("releases" in parts and "current" in parts) or any("releases/current" in argument for argument in arguments):
        return MATERIALIZED_SUPERVISOR
    if "-m" in arguments and "ananta.cli" in arguments:
        return LEGACY_DIRECT
    return UNSUPPORTED_TOPOLOGY


#: Every way ``plistlib.loads`` rejects bytes it cannot read.  This is the Manager's twin of the seed's ``target_reconciliation.PLIST_PARSE_ERRORS``:
#: the Manager runs on the host against any seed it previews, old ones included, and never imports seed code, so the list is kept here and a smoke
#: holds the two equal.  Enumerated from plistlib's source and a 60,000-mutation fuzz of XML and binary plists: ``ExpatError`` (truncated XML, a
#: mismatched tag), ``ValueError`` (a bad ``<integer>``, ``<real>`` or base64, a key with no value, invalid UTF-8), ``AttributeError`` (a bad
#: ``<date>``), ``InvalidFileException``, ``LookupError`` (an unknown encoding), ``OverflowError``, ``TypeError`` and ``RecursionError``.
PLIST_PARSE_ERRORS: Final[tuple[type[Exception], ...]] = (OSError, plistlib.InvalidFileException, ExpatError, ValueError, AttributeError, OverflowError, TypeError, LookupError, RecursionError)


def parse_plist(raw: bytes) -> dict[str, object] | None:
    """The plist's top-level dict, or ``None`` when the bytes are not a plist or its root is not a dict; never raises."""
    try:
        parsed = plistlib.loads(raw)
    except PLIST_PARSE_ERRORS:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parsed(raw: bytes) -> dict[str, object]:
    return parse_plist(raw) or {}
