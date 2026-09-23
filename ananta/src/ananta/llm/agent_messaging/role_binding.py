"""Cross-layer contract for the ``agent_role_binding`` collection (v10).

The binding's ``TableSchema`` is DECLARED and REGISTERED in the plugin
(``agent_messaging_plugin/schema.py``, in the plugin's own namespace) because
it is plugin-owned — the analog of ``peer_binding`` and the sole writable
resolution + compare-and-set authority for role ownership (Control #2).

But the v10 role-inbox section reads it from core:
:meth:`AgentMessagingService.list_silent_for_roles` enumerates the roles a
holder currently holds via ``query_state`` on this table. The state interface
is namespace-per-call, so core legitimately reads a plugin-namespaced table
(one ``peer_inbox`` call touches both the ``core`` and the plugin namespace —
intentional, per the v10 design).

These constants are the SINGLE source of truth both layers import, so the
core read key and the plugin-declared schema can never drift. Core imports the
namespace / table / column names it reads; the plugin imports the same names to
declare the schema and (in Control #2) to write claims + run the CAS refresh.
"""

from __future__ import annotations

# Namespace is the plugin's own name (PLUGIN_NAME), matching ``peer_binding``.
AGENT_ROLE_BINDING_NAMESPACE = "agent_messaging_plugin"
TABLE_AGENT_ROLE_BINDING = "agent_role_binding"

# Business columns (the standardizer auto-adds id / external_id / created_at /
# updated_at / is_deleted). One row per role; ``claimed_at`` is a DISTINCT
# business column (the CAS self-refresh writes it explicitly — it is NOT folded
# into the standard ``updated_at``).
COL_ROLE = "role"
COL_AGENT_ID = "agent_id"
COL_AGENT_INSTANCE_ID = "agent_instance_id"
COL_AGENT_SESSION_ID = "agent_session_id"
COL_SESSION_LABEL = "session_label"
COL_CLAIMED_AT = "claimed_at"

# Non-unique index name for the enumeration read (one row per role, queried by
# the holding instance on every inbox call). Perf only — a seq-scan is correct.
INDEX_AGENT_ROLE_BINDING_INSTANCE = "idx_agent_role_binding_instance"


def role_binding_external_id(role: str) -> str:
    """Deterministic UNIQUE conflict key — one row per role.

    ``external_id = "role:{role}"`` is the standard platform-UNIQUE field, so
    single-row-per-role is enforced without a custom primary key. Both the
    explicit claim (``upsert_state`` with ``conflict_columns=['external_id']``)
    and the resolution read key off this value. Shared by the v4 ``role`` entity
    AND ``role_binding`` tables below (same value, distinct tables).
    """
    return f"role:{role}"


# ---------------------------------------------------------------------------
# Role-model v4 — the first-class ``role`` ENTITY + discriminated ``role_binding``
# tables (same ``agent_messaging_plugin`` namespace). FRESH tables, NOT an
# evolve of ``agent_role_binding``: ``role_binding``'s holder identity is
# discriminated, so ``agent_instance_id`` / ``agent_session_id`` are NULLABLE (a
# provider holder leaves them NULL) — a shape ``agent_role_binding`` (NOT NULL)
# cannot adopt in place. The live cutover of readers/writer from
# ``agent_role_binding`` to ``role_binding`` is the migration (design §9); these
# constants are the shared read/write contract for the new tables (same
# single-source-of-truth split as the ``agent_role_binding`` block above).
# ---------------------------------------------------------------------------

# The first-class role ENTITY (extensible + discoverable; NEVER on the resolve
# hot path — §4.1/§4.3). ``external_id = "role:{role}"`` UNIQUE.
TABLE_ROLE = "role"
COL_ORIGIN = "origin"          # 'user' | 'system' — reserved-namespace provenance (§6)
COL_DESCRIPTION = "description"
COL_PROPERTIES = "properties"  # JSON hang-point (P7) — extensible, no ALTER per property
COL_MEMORY_ID = "memory_id"    # the ingested memory row (§7); nullable (best-effort)

# ``origin`` domain values.
ROLE_ORIGIN_USER = "user"
ROLE_ORIGIN_SYSTEM = "system"

# Reserved SYSTEM-slot keyspace (§6). A system slot's identity is a platform
# CODE CONSTANT living behind this prefix — the no-role-name-literal rule scopes
# to USER names only (§6/§D.2). User-facing claim verbs reject any name in this
# prefix, so user and system identities occupy DISJOINT ``external_id`` keyspaces
# (collision is structurally impossible). ``origin`` is set from the prefix check.
SYSTEM_ROLE_PREFIX = "sys:"
# The canonical session-filled system slot: the organism's inference-of-last-resort
# (INF-01). ``holder_kind='session'``, ``owner_plugin=null`` → ungated, assigned by
# the §D.9 auto-assignment policy (INF-01 lane), NOT the general ``peer_claim_role``.
SYS_AUTONOMIC_SLOT = f"{SYSTEM_ROLE_PREFIX}autonomic"


def is_system_role(name: str) -> bool:
    """True iff ``name`` is in the reserved system-slot keyspace (``sys:`` prefix)."""
    return name.startswith(SYSTEM_ROLE_PREFIX)


# Fleet session-management Phase B (§2) — the R4 role-class taxonomy. A first-class
# column on the ``role`` row (class attaches to the NAME, not the binding —
# offices-vs-callsigns, C4), validated at claim time in the plugin's claim gate. Lives
# here (not only in the plugin) alongside ``COL_ORIGIN``/``is_system_role`` because
# core's ``list_silent_for_roles``-style enumeration reads share this same contract
# module as their single source of truth for the ``role`` row shape (AMEND 1a).
COL_ROLE_CLASS = "role_class"

ROLE_CLASS_PRIMARY = "primary"
ROLE_CLASS_PRINCIPAL = "principal"
ROLE_CLASS_PROJECT = "project"
ROLE_CLASS_EPHEMERAL = "ephemeral"
ROLE_CLASS_CHAT = "chat"

# Existing (pre-Phase-B) rows backfill to this class (§3.1) — the default a bare
# ``project`` lane role predates this design under.
ROLE_CLASS_DEFAULT = ROLE_CLASS_PROJECT

ROLE_CLASSES = (
    ROLE_CLASS_PRIMARY,
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    ROLE_CLASS_EPHEMERAL,
    ROLE_CLASS_CHAT,
)

# The reserved ``primary``-shape name suffix (design §2: "<solet>-Main",
# e.g. any name of that shape). Dawn ruling (2026-08-03, Q1): peer_claim_role is
# enforce-by-class ONLY, never class-assignment — a fresh mint always stamps
# ROLE_CLASS_DEFAULT ('project'), so a pre-D4 claim of a reserved-pattern name
# would otherwise squat it as an ordinary project role. The ONE claim-path
# guard that IS D1 scope: refuse a FRESH MINT (no pre-existing role row) of a
# reserved-pattern name with 'reserved_role_name' — a string-pattern check,
# nothing more. A name whose role row ALREADY exists (legislated via
# agent_messaging_plugin's legislate_role governance-act verb, D4, entirely
# outside D1) is unaffected — this guards MINTING, not claiming an
# already-legislated primary seat.
RESERVED_PRIMARY_NAME_SUFFIX = "-Main"


def is_reserved_primary_name(name: str) -> bool:
    """True iff ``name`` matches the reserved primary-seat pattern
    (``<solet>-Main``). Distinct from :func:`is_system_role`: that one is
    a DISJOINT keyspace rejected outright for ANY claim; this one only guards
    a FRESH MINT (§3.1 — "validated against class at claim time... NOT
    keyspace-rejected... claimable-with-displacement through the normal
    path" once legislated)."""
    return name.endswith(RESERVED_PRIMARY_NAME_SUFFIX)

# The discriminated BINDING authority (Control-#2 continuity; sole writable
# resolution + CAS authority). Reuses COL_ROLE / COL_AGENT_INSTANCE_ID /
# COL_AGENT_SESSION_ID / COL_CLAIMED_AT — the two identity columns are NULLABLE
# (session holders only; NULL for providers).
TABLE_ROLE_BINDING = "role_binding"
COL_HOLDER_KIND = "holder_kind"          # discriminator (CODE semantics, NOT a role name)
COL_HOLDER_IDENTITY = "holder_identity"  # JSON, typed-parsed per holder_kind on resolve (§4.6)
COL_CLAIM_EPOCH = "claim_epoch"          # predicated-CAS token for claim/displace (§5.1)

# ``holder_kind`` domain values (code constants, never user names).
HOLDER_KIND_SESSION = "session"
HOLDER_KIND_INFERENCE_PROVIDER = "inference_provider"

# Non-unique index for the reverse-lookup / drain-fence read on the new table.
INDEX_ROLE_BINDING_INSTANCE = "idx_role_binding_instance"


__all__ = [
    "AGENT_ROLE_BINDING_NAMESPACE",
    "COL_AGENT_ID",
    "COL_AGENT_INSTANCE_ID",
    "COL_AGENT_SESSION_ID",
    "COL_CLAIMED_AT",
    "COL_CLAIM_EPOCH",
    "COL_DESCRIPTION",
    "COL_HOLDER_IDENTITY",
    "COL_HOLDER_KIND",
    "COL_MEMORY_ID",
    "COL_ORIGIN",
    "COL_PROPERTIES",
    "COL_ROLE",
    "COL_ROLE_CLASS",
    "COL_SESSION_LABEL",
    "HOLDER_KIND_INFERENCE_PROVIDER",
    "HOLDER_KIND_SESSION",
    "INDEX_AGENT_ROLE_BINDING_INSTANCE",
    "INDEX_ROLE_BINDING_INSTANCE",
    "RESERVED_PRIMARY_NAME_SUFFIX",
    "ROLE_CLASSES",
    "ROLE_CLASS_CHAT",
    "ROLE_CLASS_DEFAULT",
    "ROLE_CLASS_EPHEMERAL",
    "ROLE_CLASS_PRIMARY",
    "ROLE_CLASS_PRINCIPAL",
    "ROLE_CLASS_PROJECT",
    "ROLE_ORIGIN_SYSTEM",
    "ROLE_ORIGIN_USER",
    "SYSTEM_ROLE_PREFIX",
    "SYS_AUTONOMIC_SLOT",
    "TABLE_AGENT_ROLE_BINDING",
    "TABLE_ROLE",
    "TABLE_ROLE_BINDING",
    "is_reserved_primary_name",
    "is_system_role",
    "role_binding_external_id",
]
