"""Address-book-backed agent-role primitives.

Backs the ``peer_send_by_name`` / ``peer_claim_role`` / ``peer_release_role``
verbs documented in ``workbench/2026-05-29_address_book_driven_peer_addressing.md``.
Each role (e.g. ``Coordinator``, ``Architect``, ``Git-Controller``) is one
entry in the address book carrying four structured child entries —
agent_id, agent_instance_id, session_label, claimed_at — that together
let ``peer_send_by_name(name)`` resolve to the current routing target.

**Encoding choice (Coordinator ruling 2026-05-30, option C)**:
Architect's §4.4 design proposed a fresh ``address_type="agent_role"``
plus four named child ``field_type``s. The
``default_address_book_plugin`` schema has DB-level CHECK constraints
that reject both — ``address_type IN ('url', 'endpoint', 'service',
'api', 'database', 'file', 'custom')`` and ``field_type IN ('url',
'port', 'host', ..., 'custom', ...)`` (see
``plugins/default_address_book_plugin/src/default_address_book_plugin/schema.py``).
Rather than migrate the schema's CHECK constraints (which would risk a
``--hard-reset`` cost on every operator), we encode the design intent
within the existing-allowed values:

- ``ROLE_ADDRESS_TYPE = 'service'`` — a peer session IS a kind of
  service endpoint; semantically close enough.
- ``FIELD_TYPE_FOR_ROLE_FIELDS = 'custom'`` — the schema's catch-all;
  used for every one of the four role-bearing child entries.
- The child entry's ``description`` carries the actual semantic name
  (e.g. ``"agent_id: agent kind"``, ``"agent_instance_id: bridge UUID"``).
- ``ROLE_TAG = 'agent_role'`` is the discriminator that distinguishes
  role entries from plain service-endpoint entries.
  ``address_book_service::search(tag='agent_role')`` enumerates every
  role; ``resolve(name)`` finds a role by its unique name.
- ``ROLE_CLAIM_TAG_PREFIX = 'agent_role_claim:'`` continues to tag the
  auto-ingested memory records so they remain sweepable.

A future deliberate schema-migration dispatch can introduce a dedicated
``agent_role`` address_type + named field_types for semantic cleanliness;
the helper-module-as-encoding-boundary keeps that migration scoped to
swapping the two constants (callers don't need to change).

Why a separate module:
- Keeps plugin.py from growing past its 2052-LOC baseline. The role
  primitives are independent of the bridge / peer-registry surface so
  they live alongside them, not inside the plugin class.
- Easier unit smoke without spinning up the plugin: pass in a fake
  ``AddressBookServiceInterface`` and assert outcomes.

Why memory auto-ingest is NOT opted out at this layer:
The address-book service's ``register`` / ``update_entry`` interface
(``ananta/src/ananta/interfaces/address_book_service_interface.py``)
does not expose a per-call ``auto_ingest_to_memory`` parameter; the
flag lives on the implementation side
(``default_address_book_plugin.address_ops.register_impl``). Until the
interface grows the override (an operator-scope change), every
register/update call here will auto-ingest a memory record. We TAG
those records with ``agent_role_claim:<name>`` so an operator (or a
future scheduled janitor) can sweep them. Architect §3.4 + Coordinator
dispatch 2026-05-30 explicitly chose the tag approach over interface
extension for this dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .role_binding_store import ResolvedRole

if TYPE_CHECKING:
    from ananta.interfaces.address_book_service_interface import (
        AddressBookServiceInterface,
    )

ROLE_ADDRESS_TYPE = "service"
ROLE_TAG = "agent_role"
ROLE_CLAIM_TAG_PREFIX = "agent_role_claim:"

# Every role-bearing child entry uses 'custom' as its on-disk field_type
# because the address-book schema's CHECK constraint rejects fresh
# field_type strings. See module docstring §"Encoding choice" for why
# the four semantically distinct entries (agent_id, agent_instance_id,
# session_label, claimed_at) all share this on-disk field_type.
FIELD_TYPE_FOR_ROLE_FIELDS = "custom"

# Logical semantic names — used in the entry's ``description`` field to
# distinguish the four roles a child entry can play, and as the in-memory
# key by which the resolver indexes them.
_FIELD_AGENT_ID = "agent_id"
_FIELD_AGENT_INSTANCE_ID = "agent_instance_id"
_FIELD_SESSION_LABEL = "session_label"
_FIELD_CLAIMED_AT = "claimed_at"

# Marker prefix prepended to every role-bearing entry's description so the
# resolver can recover the semantic field name from the description string.
# Format: ``"<semantic_name>: <human-readable detail>"``. The resolver
# splits at the first colon to recover ``<semantic_name>``.
_DESCRIPTION_SEPARATOR = ": "

_FIELD_DESCRIPTIONS: dict[str, str] = {
    _FIELD_AGENT_ID: (
        f"{_FIELD_AGENT_ID}"
        f"{_DESCRIPTION_SEPARATOR}"
        "agent kind (e.g. 'claude_code', 'codex')"
    ),
    _FIELD_AGENT_INSTANCE_ID: (
        f"{_FIELD_AGENT_INSTANCE_ID}"
        f"{_DESCRIPTION_SEPARATOR}"
        "current bridge instance UUID for the role-holding session"
    ),
    _FIELD_SESSION_LABEL: (
        f"{_FIELD_SESSION_LABEL}"
        f"{_DESCRIPTION_SEPARATOR}"
        "display label as of the most recent claim or refresh"
    ),
    _FIELD_CLAIMED_AT: (
        f"{_FIELD_CLAIMED_AT}"
        f"{_DESCRIPTION_SEPARATOR}"
        "ISO-8601 UTC timestamp of the most recent claim or refresh"
    ),
}


class PeerRoleVacantError(Exception):
    """Raised when no ``agent_role`` entry exists for ``name``."""

    def __init__(self, name: str) -> None:
        self.name = name
        super().__init__(f"peer_role_vacant: no agent_role entry for {name!r}")


class PeerRoleAddressBookError(Exception):
    """Raised when address-book operations fail unexpectedly."""

    def __init__(self, op: str, message: str) -> None:
        self.op = op
        super().__init__(f"address_book {op} failed: {message}")


def resolve_role(
    address_book: AddressBookServiceInterface, name: str,
) -> ResolvedRole:
    """Look up the current routing target for ``name`` via the address book."""
    result = address_book.resolve(name)
    if not _is_success(result):
        raise PeerRoleVacantError(name)
    data = _result_data(result)
    entries = data.get("entries")
    if not isinstance(entries, list):
        raise PeerRoleAddressBookError(
            "resolve", f"missing 'entries' in resolve payload for {name!r}",
        )
    fields = _index_entries_by_field_type(entries)
    return ResolvedRole(
        name=name,
        agent_id=fields.get(_FIELD_AGENT_ID, ""),
        agent_instance_id=fields.get(_FIELD_AGENT_INSTANCE_ID, ""),
        session_label=fields.get(_FIELD_SESSION_LABEL, ""),
    )


def claim_or_refresh_role(
    address_book: AddressBookServiceInterface,
    *,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
    auto_create: bool = True,
) -> dict[str, Any]:
    """Claim a new role or refresh an existing role's routing fields.

    When the entry exists, every child entry is updated to match the
    new (agent_id, agent_instance_id, session_label, claimed_at) tuple.
    When the entry does not exist AND ``auto_create=True``, a new entry
    is registered. When the entry does not exist AND ``auto_create=False``
    (the bridge self-refresh path), ``PeerRoleVacantError`` is raised so
    the caller can no-op without unilateral creation.
    """
    existing_result = address_book.resolve(name)
    if _is_success(existing_result):
        return _refresh_existing_role(
            address_book=address_book,
            existing=_result_data(existing_result),
            name=name,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            session_label=session_label,
        )
    if not auto_create:
        raise PeerRoleVacantError(name)
    return _register_new_role(
        address_book=address_book,
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
    )


def release_role(
    address_book: AddressBookServiceInterface, name: str,
) -> dict[str, Any]:
    """Delete the ``agent_role`` entry for ``name``; returns its prior state."""
    existing_result = address_book.resolve(name)
    if not _is_success(existing_result):
        return {"released": False, "name": name, "reason": "not_found"}
    prior = _index_entries_by_field_type(
        _result_data(existing_result).get("entries") or [],
    )
    delete_result = address_book.delete(name)
    if not _is_success(delete_result):
        raise PeerRoleAddressBookError(
            "delete", _result_error_message(delete_result) or "unknown error",
        )
    return {
        "released": True,
        "name": name,
        "prior_agent_instance_id": prior.get(_FIELD_AGENT_INSTANCE_ID, ""),
    }


def _register_new_role(
    *,
    address_book: AddressBookServiceInterface,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
) -> dict[str, Any]:
    """First-time claim path: register a fresh agent_role entry."""
    timestamp = _now_iso()
    entries = _build_entry_payload(
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        session_label=session_label,
        claimed_at=timestamp,
    )
    tags = [ROLE_TAG, f"{ROLE_CLAIM_TAG_PREFIX}{name}"]
    result = address_book.register(
        name=name,
        address_type=ROLE_ADDRESS_TYPE,
        description=f"Agent-role routing entry for {name!r}.",
        entries=entries,
        tags=tags,
    )
    if not _is_success(result):
        raise PeerRoleAddressBookError(
            "register", _result_error_message(result) or "unknown error",
        )
    data = _result_data(result)
    return {
        "action": "registered",
        "name": name,
        "address_id": str(data.get("address_id", "")),
        "memory_id": str(data.get("memory_id", "")),
        "claimed_at": timestamp,
    }


def _refresh_existing_role(
    *,
    address_book: AddressBookServiceInterface,
    existing: Mapping[str, Any],
    name: str,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
) -> dict[str, Any]:
    """Existing-claim refresh path: update_entry on each child."""
    timestamp = _now_iso()
    targets: dict[str, str] = {
        _FIELD_AGENT_ID: agent_id,
        _FIELD_AGENT_INSTANCE_ID: agent_instance_id,
        _FIELD_SESSION_LABEL: session_label,
        _FIELD_CLAIMED_AT: timestamp,
    }
    existing_entries = existing.get("entries") or []
    updated_entry_ids: list[str] = []
    for entry in existing_entries:
        entry_id = _entry_id(entry)
        semantic = _semantic_field_from_description(_entry_description(entry))
        if entry_id is None or semantic not in targets:
            continue
        new_value = targets[semantic]
        if str(entry.get("value", "")) == new_value:
            continue
        result = address_book.update_entry(entry_id=entry_id, value=new_value)
        if not _is_success(result):
            raise PeerRoleAddressBookError(
                "update_entry",
                f"{semantic}: {_result_error_message(result) or 'unknown error'}",
            )
        updated_entry_ids.append(entry_id)
    return {
        "action": "updated",
        "name": name,
        "address_id": str(existing.get("id", "")),
        "updated_entry_ids": updated_entry_ids,
        "claimed_at": timestamp,
    }


def _build_entry_payload(
    *,
    agent_id: str,
    agent_instance_id: str,
    session_label: str,
    claimed_at: str,
) -> list[dict[str, str]]:
    """Render the 4-field child-entry payload for the address-book register call.

    Per the option (C) encoding (see module docstring): every entry
    ships with ``field_type=FIELD_TYPE_FOR_ROLE_FIELDS`` so the schema's
    CHECK constraint accepts it. The semantic-field discriminator
    lives in ``description``'s prefix; the resolver recovers it via
    :func:`_semantic_field_from_description`.
    """
    values: dict[str, str] = {
        _FIELD_AGENT_ID: agent_id,
        _FIELD_AGENT_INSTANCE_ID: agent_instance_id,
        _FIELD_SESSION_LABEL: session_label,
        _FIELD_CLAIMED_AT: claimed_at,
    }
    return [
        {
            "field_type": FIELD_TYPE_FOR_ROLE_FIELDS,
            "description": _FIELD_DESCRIPTIONS[semantic_field],
            "value": values[semantic_field],
        }
        for semantic_field in (
            _FIELD_AGENT_ID,
            _FIELD_AGENT_INSTANCE_ID,
            _FIELD_SESSION_LABEL,
            _FIELD_CLAIMED_AT,
        )
    ]


def _index_entries_by_field_type(entries: list[Any]) -> dict[str, str]:
    """Flatten the resolve-payload entries list to a {semantic_field: value} dict.

    Each child entry's on-disk ``field_type`` is the generic
    ``FIELD_TYPE_FOR_ROLE_FIELDS`` sentinel; the semantic field name
    (``agent_id``, ``agent_instance_id``, etc.) is recovered from the
    entry's ``description`` prefix. Entries that don't match the
    role-claim description pattern are silently skipped — a defensive
    guard for partial / hand-crafted address-book content.
    """
    indexed: dict[str, str] = {}
    for entry in entries:
        semantic = _semantic_field_from_description(_entry_description(entry))
        if not semantic:
            continue
        indexed[semantic] = str(_entry_value(entry))
    return indexed


def _semantic_field_from_description(description: str) -> str:
    """Recover the semantic field name from the entry's description prefix.

    The build path writes descriptions of the shape
    ``"<semantic_field>: <human detail>"`` (see ``_FIELD_DESCRIPTIONS``).
    Returns the matched semantic name when the prefix names one of the
    four known role fields; returns ``""`` otherwise.
    """
    if not description:
        return ""
    head, _, _ = description.partition(_DESCRIPTION_SEPARATOR)
    head = head.strip()
    if head in (
        _FIELD_AGENT_ID,
        _FIELD_AGENT_INSTANCE_ID,
        _FIELD_SESSION_LABEL,
        _FIELD_CLAIMED_AT,
    ):
        return head
    return ""


def _entry_description(entry: Any) -> str:
    """Return the entry's ``description`` if it has one."""
    if not isinstance(entry, Mapping):
        return ""
    raw = entry.get("description")
    return raw if isinstance(raw, str) else ""


def _entry_value(entry: Any) -> str:
    """Return the entry's ``value`` if it has one."""
    if not isinstance(entry, Mapping):
        return ""
    raw = entry.get("value", "")
    return raw if isinstance(raw, str) else ""


def _entry_id(entry: Any) -> str | None:
    """Return the entry's stable id, or ``None`` if the entry is malformed."""
    if not isinstance(entry, Mapping):
        return None
    raw = entry.get("id")
    if isinstance(raw, str) and raw:
        return raw
    return None


def _is_success(result: Mapping[str, Any] | None) -> bool:
    """True when an ``ActionResult`` indicates completion."""
    if not isinstance(result, Mapping):
        return False
    return str(result.get("action_status", "")) == "completed"


def _result_data(result: Mapping[str, Any]) -> Mapping[str, Any]:
    """Return the ``data`` field of an ``ActionResult``, or an empty mapping."""
    data = result.get("data")
    return data if isinstance(data, Mapping) else {}


def _result_error_message(result: Mapping[str, Any] | None) -> str | None:
    """Pull a human-readable error string off an ``ActionResult``."""
    if not isinstance(result, Mapping):
        return None
    err = result.get("error")
    if not isinstance(err, Mapping):
        return None
    msg = err.get("message")
    return msg if isinstance(msg, str) else None


def _now_iso() -> str:
    """ISO-8601 UTC ``claimed_at`` timestamp for the entry."""
    return datetime.now(UTC).isoformat()
