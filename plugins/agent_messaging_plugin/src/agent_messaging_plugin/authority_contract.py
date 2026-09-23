"""T2 authority-template (seat's design ruling, 2026-08-05; approved text
message arm-b79d254b9d1bd8c732e9398ae2901257) -- renders the fleet
delegation contract injected via ``--append-system-prompt`` at spawn, so a
worker's authority to act is anchored in a TRUSTED spawn surface (the
adapter's own process launch) rather than peer-channel prose, which is
untrusted-by-construction in the worker harness (the exact structural hold
the medium-effort builder trial's 4th stall named -- see
the 2026-08-05 usage-capture-ruling lane-history
note). Recon measurement (2026-08-05, scratch-only, three legs +
negative-control) confirmed ``--append-system-prompt`` text composes with
``--settings``/``--setting-sources``, genuinely lands in the worker-visible
system prompt, AND persists across ``/clear`` -- the delegation chain
survives context rotation with zero re-onboard prose needed.

The template file (``templates/authority_delegation_contract.txt``,
plugin-root-relative, versioned via git history, gated -- never hand-edited
outside a GC commit) is rendered with ``spawn_session``'s own fields. Uses
plain ``str.replace`` per named placeholder rather than ``str.format`` --
the template's own BINDING RULES text contains a literal
``{"op": "is_null"}`` JSON example, which ``str.format`` would misparse as
a field reference.

AUTHORITY paragraph revised (fleet-watch-transport-migration phase 2, slice
1+5, per the capability-tier guardrail redesign
the 2026-08-06 capability-tier guardrail redesign record
section 4): the original text instructed a worker it "must never request,
expect, or wait for" a human pane confirmation -- live-measured
(2026-08-05/06) to read, to a cautious model weighing a genuine capability
escalation, like the thing a hijacker would write, and to have plausibly
sharpened rather than prevented a hold on exactly such a request. Replaced
with an honest tier statement: tier-0 (routine lane work) needs no human
word per action; tier-1 (capability escalation -- minting an unattended
session, granting shell/bypass capability, terminating a live session, a
live-state deploy) is seat-native and NEVER asked of a worker at all, so a
worker never has to invoke the withheld clause it used to be told never to
invoke. A contract that never asks a worker to do what the floor forbids
never triggers the floor.
"""

from __future__ import annotations

import re
from pathlib import Path

_TEMPLATE_PATH = (
    Path(__file__).resolve().parents[2] / "templates" / "authority_delegation_contract.txt"
)

_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Z_][a-zA-Z0-9_]*\}")

_PLACEHOLDER_NAMES = (
    "agent_instance_id", "role_class", "lane_id", "brief_ref", "unit_id", "spawned_by_role",
    "repository_root",
)
_OPTIONAL_UNIT_ID_LINE = "UNIT ID: {unit_id}\n"


class UnresolvedPlaceholderError(ValueError):
    """A rendered contract still contains a ``{placeholder}`` -- the
    KB-ships-unrendered trap class: an unresolved placeholder must never
    ship into a live system prompt."""


def render_authority_delegation_contract(
    *,
    agent_instance_id: str,
    role_class: str,
    lane_id: str,
    brief_ref: str,
    spawned_by_role: str,
    unit_id: str = "",
    repository_root: str = "",
) -> str:
    """Renders the delegation contract for one spawn. Raises
    :class:`UnresolvedPlaceholderError` if any named placeholder in the
    template file's own text failed to substitute (e.g. a typo in the
    template) -- never silently ships a raw ``{placeholder}`` token into a
    worker's live system prompt."""
    template = _TEMPLATE_PATH.read_text()
    if not unit_id:
        template = template.replace(_OPTIONAL_UNIT_ID_LINE, "")
    values = {
        "agent_instance_id": agent_instance_id,
        "role_class": role_class,
        "lane_id": lane_id,
        "brief_ref": brief_ref,
        "unit_id": unit_id,
        "spawned_by_role": spawned_by_role,
        "repository_root": repository_root,
    }
    rendered = template
    for name in _PLACEHOLDER_NAMES:
        rendered = rendered.replace("{" + name + "}", values[name])
    leftover = _PLACEHOLDER_RE.search(rendered)
    if leftover is not None:
        raise UnresolvedPlaceholderError(
            f"authority_delegation_contract.txt rendered with an unresolved "
            f"placeholder {leftover.group()!r} -- refusing to return this "
            f"into a live system prompt",
        )
    return rendered


__all__ = ["UnresolvedPlaceholderError", "render_authority_delegation_contract"]
