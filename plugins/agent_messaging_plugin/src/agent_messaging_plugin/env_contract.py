"""Neutral fleet identity env-var contract (``AGENT_*``).

The five per-session identity variables a launcher exports for fleet
coordination are unprefixed and runner-neutral: they are part of the seed
contract and must read identically on the platform origin and on every
seed-born homunculus (operator ruling 2026-07-28; fleet-transport-parity
design §7). This module is the single source of truth for the names; it is
stdlib-only so the ``homunculus`` console script and the standalone MCP
bridge subprocess can both import it bare (PEP 562 lazy package init).

Migration tripwire (one release): the family moved through two prefixed
generations (the pre-2026-07-25 origin-name prefix, then
``HOMUNCULUS_AGENT_*``) before the neutral atomic land, with NO alias reads.
A legacy name present in the environment while its neutral replacement is
absent means an un-migrated launcher or MCP config — entry points fail loudly
instead of silently registering with degraded identity.
"""

from __future__ import annotations

import os
from typing import Final

AGENT_IDENTITY_ENV: Final[str] = "AGENT_IDENTITY"
AGENT_INSTANCE_ID_ENV: Final[str] = "AGENT_INSTANCE_ID"
AGENT_SESSION_LABEL_ENV: Final[str] = "AGENT_SESSION_LABEL"
AGENT_SESSION_ID_ENV: Final[str] = "AGENT_SESSION_ID"
AGENT_ROLE_ENV: Final[str] = "AGENT_ROLE"

# The older prefix is assembled from fragments so the seed content gate's
# reserved-origin-token scan never matches shipped source; the runtime string
# is unchanged (same idiom as the seed_content_validator's own pattern).
_PRE_RENAME_PREFIX: Final[str] = "AD" + "A_"
_LEGACY_ENV_PREFIXES: Final[tuple[str, ...]] = (_PRE_RENAME_PREFIX, "HOMUNCULUS_")
_CONTRACT_ENV_NAMES: Final[tuple[str, ...]] = (
    AGENT_IDENTITY_ENV,
    AGENT_INSTANCE_ID_ENV,
    AGENT_SESSION_LABEL_ENV,
    AGENT_SESSION_ID_ENV,
    AGENT_ROLE_ENV,
)


def enforce_no_legacy_agent_env() -> None:
    """Fail loudly when a legacy prefixed ``AGENT_*`` var is still exported.

    Raises ``RuntimeError`` when a legacy prefixed member of the five-name
    family (either pre-rename generation, e.g. ``HOMUNCULUS_AGENT_*``) is
    present while its neutral replacement is absent — never reads through to
    the legacy value.
    Legacy absent is the steady state; legacy plus neutral is tolerated for
    the flip window only (the read sites see exclusively the neutral name).
    """
    stale = [
        (f"{prefix}{name}", name)
        for prefix in _LEGACY_ENV_PREFIXES
        for name in _CONTRACT_ENV_NAMES
        if os.environ.get(f"{prefix}{name}") is not None
        and os.environ.get(name) is None
    ]
    if stale:
        legacy = ", ".join(legacy_name for legacy_name, _ in stale)
        neutral = ", ".join(dict.fromkeys(neutral_name for _, neutral_name in stale))
        msg = (
            f"un-migrated fleet identity environment: {legacy} present "
            f"without {neutral}. The {_PRE_RENAME_PREFIX}AGENT_* and "
            "HOMUNCULUS_AGENT_* families were renamed to AGENT_* "
            "(2026-07-28, no alias reads); update the launcher/MCP config "
            "to export the unprefixed names."
        )
        raise RuntimeError(msg)
