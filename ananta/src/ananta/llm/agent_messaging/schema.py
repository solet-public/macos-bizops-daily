"""Schema definitions for durable agent thread/message persistence.

Two tables in namespace ``core``:

- ``core__agent_thread`` — per-thread state, owned by an originator
  (currently always an MCP bridge owned by ``agent_messaging_plugin``).
- ``core__agent_message`` — append-only, cursor-addressable messages.

Cursor allocation MUST be atomic (``UPDATE … RETURNING last_message_cursor``
inside ``state_service.transactional()``); see workbench doc §7.

Standard fields (``id``, ``created_at``, ``updated_at`` on the thread row)
are auto-added by the StateService metadata layer, matching the pattern
in ``plugins/discord_plugin/src/discord_plugin/schema.py``.
"""

from __future__ import annotations

from ananta.types.column_types import ColumnType
from ananta.types.schema_types import (
    ColumnDefinition,
    IndexDefinition,
    SchemaDefinition,
    TableSchema,
)

NAMESPACE = "core"
TABLE_AGENT_THREAD = "agent_thread"
TABLE_AGENT_MESSAGE = "agent_message"
TABLE_AGENT_ROLE_MESSAGE = "agent_role_message"
# REL-05: the direct-IMPORTANT-send outbox (one row per IMPORTANT direct
# peer_send). Sibling of ``agent_role_message`` — same delivered-bookkeeping
# shape, but the recipient instance is FIXED (Q3 key), so it carries no
# recipient_kind/recipient_key. Written + read ONLY through the state interface.
TABLE_AGENT_DIRECT_WAKE = "agent_direct_wake"
# Pull-surface boundary (workbench/2026-08-02_pull_surface_boundary_design_claude_d.md
# §1): one row per role, the durable server-side "covered through" mark that
# bounds the peer_inbox role section's DEFAULT drain. Co-located here (not the
# plugin's agent_messaging_plugin namespace) because it must be read INLINE on
# every peer_inbox call alongside agent_role_message, same-namespace.
TABLE_ROLE_COVERED_MARK = "role_covered_mark"
# Weak display receipts are intentionally separate from the strong handover
# mark above.  A receipt belongs to one concrete bridge instance and is never
# evidence that a successor consumed or covered a role.
TABLE_ROLE_READ_WATERMARK = "role_read_watermark"
TABLE_ROLE_READ_RECEIPT = "role_read_receipt"
TABLE_ROLE_READ_PAGE = "role_read_page"
TABLE_ROLE_READ_PAGE_ITEM = "role_read_page_item"

ID_PREFIX_THREAD = "agt"
ID_PREFIX_MESSAGE = "agm"
ID_PREFIX_ROLE_MESSAGE = "arm"
ID_PREFIX_DIRECT_WAKE = "adw"
ID_PREFIX_ROLE_COVERED_MARK = "arc"  # R7: collision-checked clear against every ID_PREFIX_* in the repo at build time.
ID_PREFIX_ROLE_READ_WATERMARK = "arr"
ID_PREFIX_ROLE_READ_RECEIPT = "arrc"
ID_PREFIX_ROLE_READ_PAGE = "arrp"
ID_PREFIX_ROLE_READ_PAGE_ITEM = "arri"

# REL-05 consumption + re-emit bookkeeping columns, shared verbatim across
# schema.py, the service drain/reconcile methods, the escalation reconciler, and
# the forwarder drain serializer — named ONCE here (the same anti-drift rationale
# as the META_KEY_* keys below) so the layers cannot drift on spelling.
#
# The BOOLEAN companion columns (``consumed`` / ``escalated``) are the
# equality-filterable predicates the state interface can express (it filters on
# equality only, exactly like the existing ``delivered`` bit); the ``*_at``
# timestamps are audit + the basis for the Python-side re-emit-window / cap
# comparisons the equality filter cannot do.
COL_CONSUMED = "consumed"
COL_CONSUMED_AT = "consumed_at"
COL_ESCALATED = "escalated"
COL_ESCALATED_AT = "escalated_at"
COL_ESCALATION_REASON = "escalation_reason"
# The two values written into ``COL_ESCALATION_REASON`` — named ONCE here
# (anti-drift) because BOTH the plugin reconciler (which WRITES them) and the
# core Fork-1a re-home CAS (which READS ``recipient_gone`` to reactivate an
# orphaned row when its recipient re-registers) depend on the exact spelling.
ESCALATION_REASON_CAP = "cap_reached"
ESCALATION_REASON_GONE = "recipient_gone"
COL_LAST_EMITTED_AT = "last_emitted_at"
COL_EMIT_COUNT = "emit_count"
# F3 (displacement correctness): the ROLE-row consumption stamp requires model
# activity from the instance the message was actually emitted to — recorded here
# on each ``/peer/delivered`` confirm. Direct rows are immune (fixed recipient).
COL_EMITTED_TO_AGENT_INSTANCE_ID = "emitted_to_agent_instance_id"
# QUIET-GAP capture (H2 fix): the recipient's ``last_model_activity_at`` AS OF the
# most recent emission, frozen per-emission on BOTH owed tables. Guard 2 reads this
# instead of the live ``prev_model_activity_at``, which slides forward on every model
# call and so could retire an emission's only consumable window after the fact.
COL_ACTIVITY_AT_EMISSION = "activity_at_emission"
# H1-role re-home: the STABLE agent_session_id of the instance the most recent
# emission went to, stamped beside COL_EMITTED_TO_AGENT_INSTANCE_ID. The role
# sibling of the direct table's ``recipient_agent_session_id`` — an instance id
# rotates on a genuine session restart, so a re-home needs a key that does not.
COL_EMITTED_TO_AGENT_SESSION_ID = "emitted_to_agent_session_id"

# recipient_kind domain values for core__agent_role_message.
RECIPIENT_KIND_ROLE = "role"
RECIPIENT_KIND_INSTANCE = "instance"

# Synthetic role-channel thread handle prefix ("role:{recipient_key}") —
# display-only, never dereferenced as a live thread. Named so readers that
# recover the role name from an inbox entry's thread_id (the watcher-ack
# consumption path) cannot drift from the writers that compose it.
ROLE_THREAD_PREFIX = "role:"

_RECIPIENT_KIND_VALUES = (RECIPIENT_KIND_ROLE, RECIPIENT_KIND_INSTANCE)

# Immutable transport provenance for ``core__agent_role_message`` envelopes.
# ``unknown`` is the safe default for rows written before this column existed:
# historical traffic must never be retroactively presented as stdio traffic.
SENDER_PRINCIPAL_KIND_OAUTH_CLIENT = "oauth_client"
SENDER_PRINCIPAL_KIND_STDIO_AGENT = "stdio_agent"
SENDER_PRINCIPAL_KIND_SYSTEM = "system"
SENDER_PRINCIPAL_KIND_UNKNOWN = "unknown"
_SENDER_PRINCIPAL_KIND_VALUES = (
    SENDER_PRINCIPAL_KIND_OAUTH_CLIENT,
    SENDER_PRINCIPAL_KIND_STDIO_AGENT,
    SENDER_PRINCIPAL_KIND_SYSTEM,
    SENDER_PRINCIPAL_KIND_UNKNOWN,
)

# v10 Control #5 role-delivery wire contract — the channel-event ``meta`` keys
# the server stamps on a role-addressed ``peer_message`` (peer_dispatch writer)
# and the bridge forwarder reads to recognise a role delivery and flip its
# ``delivered`` flag (forwarder reader). Defined ONCE here (this module is
# light + importable in the stdio-bridge subprocess) so the two layers cannot
# drift on the key spelling.
META_KEY_RECIPIENT_KIND = "recipient_kind"
META_KEY_RECIPIENT_KEY = "recipient_key"
META_KEY_DELIVERY_EXTERNAL_ID = "delivery_external_id"
# The persisted role ROW's ``created_at``, carried so a watcher can advance its
# ``role_high_water`` mark from a LIVE delivery using the same quantity the
# role-inbox section sorts and pages on. Without it the live path has no
# comparable timestamp: ``sent_at`` is a clock reading taken at meta-build time
# (a different quantity, and skipping ahead of a row loses it silently) and
# ``delivery_external_id`` is ``role:{key}:{message_id}`` — not time-ordered at
# all. Absent on a pre-deploy server; readers MUST treat "" as "cannot advance"
# and leave the mark alone rather than substituting one of the above.
META_KEY_ROLE_CREATED_AT = "role_created_at"
META_KEY_ROLE_ROW_ID = "role_row_id"

_THREAD_STATUS_VALUES = (
    "open",
    "queued",
    "running",
    "idle",
    "interrupted",
    "error",
    "closed",
)
_MESSAGE_ROLE_VALUES = ("originator", "agent", "system")
_MESSAGE_KIND_VALUES = ("message", "status", "result", "error", "artifact")


def _quoted_csv(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{v}'" for v in values)


def get_agent_messaging_schema() -> SchemaDefinition:
    """Return the schema definition for agent thread/message persistence.

    Tables:
    - ``agent_thread``: per-thread lifecycle, ownership, last-cursor counter
    - ``agent_message``: cursor-addressable append-only messages
    """
    thread = TableSchema(
        table_name=TABLE_AGENT_THREAD,
        id_prefix=ID_PREFIX_THREAD,
        description=(
            "Durable per-thread state for inter-agent messaging. "
            "Owned by an originator (today: an MCP bridge in "
            "agent_messaging_plugin)."
        ),
        columns={
            "originator_type": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Where the originator lives (e.g., 'mcp_bridge').",
            ),
            "originator_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Originator-side identifier (optional).",
            ),
            "originator_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Platform session id minted for the originating bridge."
                ),
            ),
            "originator_bridge_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Bridge id that owns this thread (for ownership checks)."
                ),
            ),
            "target_backend": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Logical backend name (e.g., 'codex', 'claude_code').",
            ),
            "recipient_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "For peer threads only: the recipient bridge's "
                    "agent_instance_id, used to disambiguate when "
                    "multiple instances of the same agent_id exist. "
                    "NULL for non-peer threads (target_backend NOT LIKE "
                    "'peer:%') and for legacy peer threads predating "
                    "multi-instance support. Also the find_peer_thread dedup "
                    "key — it stays instance-keyed BY DESIGN; inbox visibility "
                    "across instance rotation is a READ-side UNION (see "
                    "recipient_agent_session_id), never a re-key of this column."
                ),
            ),
            "recipient_agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "REL-08 read-side (Fork-1a): the recipient's STABLE "
                    "per-logical-session key, stamped at thread creation from the "
                    "recipient's live binding. The peer_inbox read UNIONs this "
                    "with recipient_agent_instance_id so a thread whose recipient "
                    "instance rotated on reconnect stays visible under the "
                    "successor — WITHOUT re-keying the dedup-load-bearing instance "
                    "column. NULL for non-peer + legacy pre-fix threads."
                ),
            ),
            "originator_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the originator's session_label at "
                    "thread-creation time (e.g. 'Coordinator', "
                    "'Architect'). NULL for non-peer threads (historical "
                    "backend-dispatch threads, opened via the now-retired "
                    "agent_thread_open — see the D3 dormant-head "
                    "retirement). Per "
                    "2026-05-31 Architect ruling §2: snapshot "
                    "semantics by design — live /rename on a session "
                    "with open peer threads does NOT update historical "
                    "rows; new threads pick up the new label."
                ),
            ),
            "originator_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the originator bridge's "
                    "agent_instance_id at thread-creation time. The "
                    "durable per-bridge UUID per the identity model in "
                    "03_inter_agent_messaging.md. NULL for non-peer "
                    "threads."
                ),
            ),
            "recipient_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Snapshot of the recipient's session_label at "
                    "thread-creation time. NULL for non-peer threads. "
                    "Sibling of recipient_agent_instance_id; this is "
                    "the human-readable label that powers operator "
                    "queries like 'show Coordinator <-> Architect "
                    "threads'."
                ),
            ),
            "target_plugin_name": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Concrete plugin satisfying the backend at thread-open."
                ),
            ),
            "title": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Optional short label.",
            ),
            "working_directory": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Filesystem root the backend should treat as cwd."
                ),
            ),
            "status": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"status IN ({_quoted_csv(_THREAD_STATUS_VALUES)})",
                description="Thread lifecycle state.",
            ),
            "backend_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Backend-generated session id (informational; "
                    "resume not used in first slice)."
                ),
            ),
            "active_action_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="action_id of the currently running turn, if any.",
            ),
            "active_flow_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="flow_id of the currently running turn, if any.",
            ),
            "last_message_cursor": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description=(
                    "Highest cursor allocated to a message in this thread. "
                    "Allocated atomically via UPDATE…RETURNING."
                ),
            ),
            "metadata": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                default="{}",
                description="Free-form per-thread metadata.",
            ),
            "closed_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When the thread transitioned to 'closed'.",
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_agent_thread_bridge",
                columns=["originator_bridge_id"],
            ),
            IndexDefinition(
                name="idx_agent_thread_status",
                columns=["status"],
            ),
            IndexDefinition(
                name="idx_agent_thread_active_action",
                columns=["active_action_id"],
            ),
            IndexDefinition(
                name="idx_agent_thread_peer_lookup",
                columns=["target_backend", "recipient_agent_instance_id"],
            ),
            IndexDefinition(
                # REL-08 read-side: the session-keyed inbox disjunct — threads for
                # one recipient session, churn-proof across instance rotation.
                name="idx_agent_thread_peer_session",
                columns=["target_backend", "recipient_agent_session_id"],
            ),
        ],
    )

    message = TableSchema(
        table_name=TABLE_AGENT_MESSAGE,
        id_prefix=ID_PREFIX_MESSAGE,
        description=(
            "Append-only, cursor-addressable messages inside a thread. "
            "Cursor is unique within a thread. "
            "EXCEPTION, ruled 2026-08-19 (GAU-06): event-type-scoped retention "
            "is permitted on MACHINE-GENERATED NOTICE THREADS, of which there "
            "is currently exactly one kind -- threads whose sender is the "
            "'system:rotation-notice' sentinel, where the writer hard-deletes "
            "its own oldest rows past a bound. The append-only guarantee for "
            "EVERY OTHER THREAD is unchanged: the exception is named, narrow "
            "and sentinel-scoped, and no retention path can reach agent or "
            "human coordination mail, because peer threads key on "
            "(sender_bridge_id, peer_instance) and the pruner is only ever "
            "handed the thread its own write just landed in. Cursors are "
            "REMOVED by a prune, never renumbered, so a client holding a "
            "pre-prune cursor still pages forward correctly."
        ),
        columns={
            "thread_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="FK to agent_thread.id.",
            ),
            "cursor": ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                description=(
                    "Per-thread monotonically increasing position. "
                    "Allocated atomically by the repository."
                ),
            ),
            "role": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"role IN ({_quoted_csv(_MESSAGE_ROLE_VALUES)})",
                description="Producer of the message.",
            ),
            "kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"kind IN ({_quoted_csv(_MESSAGE_KIND_VALUES)})",
                description="Payload kind.",
            ),
            "content": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                description=(
                    "List of typed parts (text-only in first slice)."
                ),
            ),
            "action_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "The action_id this message was produced by, if any "
                    "(historically the now-retired run_turn EDGE process's "
                    "action id on backend-dispatch threads)."
                ),
            ),
            "backend_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Backend session id reported at the time, if any.",
            ),
            "error": ColumnDefinition(
                type=ColumnType.JSON,
                description="Structured error payload for kind='error'.",
            ),
            "artifacts": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                default="[]",
                description="List of blob-only artifact references.",
            ),
            "metadata": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                default="{}",
                description=(
                    "Free-form per-message metadata "
                    "(e.g., assembled_prompt for originator turns)."
                ),
            ),
            "important": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "True iff the sender used the IMPORTANT marker (a "
                    "first-class projection of metadata.important). Gates the "
                    "silent peer-inbox: silent_only=True returns important=False "
                    "rows only; the public peer_inbox default is the catch-up "
                    "view that includes IMPORTANT messages. "
                    "Additive column (SQL-lockdown GAP-2): replaces the raw "
                    "metadata->>'important' JSONB predicate the state interface "
                    "cannot express."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                name="idx_agent_message_thread_cursor",
                columns=["thread_id", "cursor"],
                unique=True,
            ),
            IndexDefinition(
                name="idx_agent_message_thread_created",
                columns=["thread_id", "created_at"],
            ),
            IndexDefinition(
                name="idx_agent_message_action",
                columns=["action_id"],
                where="action_id IS NOT NULL",
            ),
        ],
    )

    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="Agent messaging — durable thread + message tables.",
        tables={
            TABLE_AGENT_THREAD: thread,
            TABLE_AGENT_MESSAGE: message,
        },
    )


def get_agent_role_message_schema() -> SchemaDefinition:
    """Return the schema for the authoritative role-addressed delivery store.

    ``core__agent_role_message`` (v10 Control #1) is the single authoritative
    delivery envelope for role-addressed peer messages — a sibling of
    ``core__agent_message`` written ONLY through the state-management
    interface (``upsert_state`` / ``query_ordered`` / ``update_state``),
    never raw SQL. One row per logical send, idempotent on the standard
    ``external_id`` conflict key (``{recipient_kind}:{recipient_key}:{message_id}``);
    ``delivered`` flips ``false → true`` when the holder's transport emits
    it. The standardizer auto-adds ``id`` (PK), ``external_id`` (UNIQUE),
    ``created_at`` / ``updated_at``, and ``is_deleted`` (INTEGER soft-delete
    flag); the business columns below carry the COMPLETE envelope so the
    role channel is threadless yet losslessly projectable to a
    ``PeerInboxEntry`` (the mandatory ``sender_agent_instance_id`` powers a
    targeted reply even from a meta-less native wake).
    """
    role_message = TableSchema(
        table_name=TABLE_AGENT_ROLE_MESSAGE,
        id_prefix=ID_PREFIX_ROLE_MESSAGE,
        description=(
            "Authoritative role-addressed delivery envelope. One row per "
            "logical role send, idempotent on external_id; delivered flag "
            "drives the at-least-once drain. Threadless (no agent_thread row)."
        ),
        columns={
            "recipient_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                check=f"recipient_kind IN ({_quoted_csv(_RECIPIENT_KIND_VALUES)})",
                description="'role' (role name) or 'instance' (agent_instance_id).",
            ),
            "recipient_key": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The role name (recipient_kind='role') or instance id. "
                    "The load-bearing durable addressing key — a role query "
                    "matches recipient_key only, so cross-kind takeover works."
                ),
            ),
            "message_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Stable per-logical-send id (minted once per send, not "
                    "per transport attempt) — the third external_id component."
                ),
            ),
            "sender_agent_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Sender's stable agent kind (provenance).",
            ),
            "sender_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Sender's per-bridge instance id. MANDATORY: native wake "
                    "delivers meta-less prose, so the receiver builds a "
                    "targeted reply from this embedded value."
                ),
            ),
            "sender_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Sender's human session label at send time (nullable).",
            ),
            "sender_principal_kind": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default=SENDER_PRINCIPAL_KIND_UNKNOWN,
                check=(
                    "sender_principal_kind IN "
                    f"({_quoted_csv(_SENDER_PRINCIPAL_KIND_VALUES)})"
                ),
                description=(
                    "Immutable sender transport provenance: oauth_client, "
                    "stdio_agent, system, or unknown for pre-provenance rows."
                ),
            ),
            "sender_transport_principal": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default="unknown",
                description=(
                    "Immutable authenticated transport principal, distinct from "
                    "the sender identity. Unknown is retained for legacy or "
                    "non-attested paths."
                ),
            ),
            "sender_identity_trust": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                default="unknown",
                description=(
                    "What the transport actually verified about the sender "
                    "identity; never an inferred authority level."
                ),
            ),
            "thread_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Synthetic role-channel handle 'role:{recipient_key}' — "
                    "informational/display only, NEVER dereferenced as a live "
                    "thread FK (the role channel is threadless by design)."
                ),
            ),
            "important": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "True iff the sender used the IMPORTANT marker — gates the "
                    "auto-emit drain (silent role messages are inbox-only)."
                ),
            ),
            "delivered": ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "False until the current holder's transport emits the "
                    "message; flipped true (idempotently) on delivery. The row "
                    "persists (audit) — never delete-to-prune the undelivered set."
                ),
            ),
            "content": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                description="The message body/envelope payload (list of typed parts).",
            ),
            # ---- REL-05 consumption-gated re-emit (Q5, additive) --------------
            COL_CONSUMED: ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "REL-05 (Q5): context-entry proof — flipped true when the "
                    "instance the row was emitted_to performs model-initiated "
                    "activity after the emission. The drain STOPS on this (not on "
                    "``delivered``, which stays the emission-bookkeeping bit). The "
                    "equality-filterable companion of ``consumed_at``. Additive "
                    "default=False; the F2 grandfather backfill sets it true on "
                    "pre-migration delivered=true history so the new predicate "
                    "cannot flood-re-emit ancient messages."
                ),
            ),
            COL_CONSUMED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When consumption was stamped (audit; NULL = still owed).",
            ),
            COL_LAST_EMITTED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "When the last emission was confirmed to the holder (NULL "
                    "until the first). The re-emit window is measured from here."
                ),
            ),
            COL_EMIT_COUNT: ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description=(
                    "Emissions so far (original + re-emits). The cap-3 stop "
                    "condition; compared in Python (the state filter is "
                    "equality-only). F2 backfill sets it to 1 on delivered=true "
                    "history."
                ),
            ),
            COL_EMITTED_TO_AGENT_INSTANCE_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "F3: the agent_instance_id the most recent emission was "
                    "confirmed to (recorded on /peer/delivered). The consumption "
                    "stamp requires model activity from THIS instance, so a "
                    "displaced-then-reclaimed role re-owes to the new holder. "
                    "NULL until first emission."
                ),
            ),
            COL_ACTIVITY_AT_EMISSION: ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "QUIET-GAP capture: the recipient's last_model_activity_at "
                    "AS OF the most recent emission, frozen on the row. Guard 2 "
                    "reads THIS rather than the live sliding pair, so the "
                    "quiet-gap test cannot drift after the fact. Refreshed on "
                    "every emission (original and each re-emit) so each emission "
                    "gets its own honest test. NULL = no recorded activity at "
                    "emission (or a pre-migration row) ⇒ the whole preceding "
                    "session was the quiet gap ⇒ guard 2 passes."
                ),
            ),
            COL_EMITTED_TO_AGENT_SESSION_ID: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "H1-role: the STABLE agent_session_id behind "
                    "emitted_to_agent_instance_id at the most recent emission. "
                    "The instance id rotates when a session restarts, which "
                    "stranded the consumption join on a dead id; this key does "
                    "not rotate, so the register-time re-home can re-point the "
                    "row onto the successor. NULL until first emission."
                ),
            ),
            # ---- RIDER-1 terminal-clear (mirrors the direct-wake escalation) ---
            # Before this, a capped-unconsumed role IMPORTANT went DORMANT
            # (consumed=false, emit_count=cap) and NEVER left the consumed=false
            # owed set — so ≥cap dormant rows for one role filled the oldest
            # limit-page of the drain and STARVED genuinely-owed newer rows behind
            # them (+ the sender got no terminal signal). Escalation flips
            # ``escalated`` so the row drops from the drain's equality filter
            # (kills the starvation) and the reconciler fires the sender terminal
            # signal (kills the silent-fail) — exactly as direct rows already do.
            COL_ESCALATED: ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "RIDER-1: terminal — a capped-unconsumed (or recipient_gone) "
                    "role IMPORTANT that the reconciler escalated. The drain filter "
                    "excludes it (``consumed=false AND escalated=false``) so it can "
                    "no longer clog the oldest limit-page and starve newer owed "
                    "rows. Additive default=False; existing rows inherit False on "
                    "ALTER (the correct non-escalated state — no re-owe, so NO data "
                    "backfill, unlike REL-05's consumed predicate change)."
                ),
            ),
            COL_ESCALATED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When escalation fired (audit; NULL until escalated).",
            ),
            COL_ESCALATION_REASON: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Why escalation fired: 'cap_reached' | 'recipient_gone'. "
                    "NULL until escalated."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                # The IMPORTANT-drain oldest-first composite scan + tie-break.
                # REL-05: ``consumed`` joins the scan (the drain now stops on
                # consumed, keeping delivered as the emission bit). RIDER-1:
                # ``escalated`` joins so terminal rows drop from the drain.
                name="idx_agent_role_message_drain",
                columns=[
                    "recipient_key", "important", "delivered", "escalated",
                    "created_at", "id",
                ],
            ),
            IndexDefinition(
                # The silent recent-N inbox scan + tie-break.
                name="idx_agent_role_message_inbox",
                columns=["recipient_key", "important", "created_at", "id"],
            ),
        ],
    )

    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="Agent messaging — authoritative role-addressed delivery store.",
        tables={TABLE_AGENT_ROLE_MESSAGE: role_message},
    )


def get_agent_direct_wake_schema() -> SchemaDefinition:
    """Return the schema for the direct-IMPORTANT-send outbox (REL-05).

    ``core__agent_direct_wake`` is the direct-send sibling of
    ``core__agent_role_message``: one row per IMPORTANT DIRECT ``peer_send``
    (silent sends never get rows — the marker discipline is IMPORTANT-only). It
    exists so a direct IMPORTANT send gets the SAME durable outbox + consumption-
    gated re-emit the role path has, rather than a second replayer. Written +
    read ONLY through the state interface (``upsert_state`` / ``query_ordered`` /
    ``update_state``) — no raw SQL, no ``core__agent_message`` projection.

    The row IS the deaf-wake census record: the pair (``emit_count``,
    ``consumed_at``) distinguishes never-drained (bridge dead), drained-but-deaf
    (Vector B), and consumed. Rows are terminal once ``consumed`` or
    ``escalated`` but are NEVER deleted in v1 (audit; a retention sweep is a
    shared follow-up). The standardizer auto-adds ``id`` (PK), ``external_id``
    (UNIQUE — the insured ``message_id``), ``created_at`` / ``updated_at`` /
    ``is_deleted``.
    """
    direct_wake = TableSchema(
        table_name=TABLE_AGENT_DIRECT_WAKE,
        id_prefix=ID_PREFIX_DIRECT_WAKE,
        description=(
            "Direct-IMPORTANT-send outbox with consumption-gated re-emit. One "
            "row per IMPORTANT direct peer_send, idempotent on "
            "external_id=message_id; the (emit_count, consumed_at) pair is the "
            "deaf-wake census record. Threadless; never delete-to-prune."
        ),
        columns={
            "message_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The persisted thread message this row insures — also the "
                    "external_id (one outbox row per logical send; a transport "
                    "retry of the same send collapses to one row)."
                ),
            ),
            "thread_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The peer thread the insured message belongs to.",
            ),
            "recipient_agent_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Recipient's agent kind (inbox-matching addressing).",
            ),
            "recipient_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Recipient's per-bridge instance id — the drain fence. It "
                    "ROTATES when the recipient session reconnects/restarts (the "
                    "REL-01 recipient_gone orphan class); the peer/register re-home "
                    "CAS re-points owed rows off a dead instance to the successor "
                    "via ``recipient_agent_session_id`` (Fork-1a). NB earlier docs "
                    "called this a FIXED, F3-immune key that 'never changes' — "
                    "falsified by the 2026-07-11 deaf-wake RCA (a full session "
                    "restart mints a new instance id under the same session)."
                ),
            ),
            "recipient_agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Fork-1a (REL-01/REL-07): the recipient's STABLE per-logical-"
                    "session key — survives ``agent_instance_id`` rotation across "
                    "reconnect/restart. Sourced from the recipient's live "
                    "peer_binding at send time (never caller args). The "
                    "peer/register re-home CAS keys on it to re-point every owed "
                    "row off a dead instance to the session's successor instance. "
                    "Nullable — a recipient without a session key (older client) "
                    "simply cannot re-home (degrades to today's behavior)."
                ),
            ),
            "sender_agent_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Sender's agent kind (escalation return path).",
            ),
            "sender_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Sender's per-bridge instance id (escalation return path).",
            ),
            "sender_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Sender's human session label at send time (nullable).",
            ),
            "sender_bridge_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Sender's bridge at send time — the escalation target (the "
                    "sender is the party with context to chase an unconsumed send)."
                ),
            ),
            "content": ColumnDefinition(
                type=ColumnType.JSON,
                not_null=True,
                description=(
                    "The already-marker-stripped delivered prose (list of typed "
                    "parts) — re-emitted byte-identical to the original wake."
                ),
            ),
            COL_LAST_EMITTED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "When the last emission was confirmed to the client (NULL "
                    "until the first). The re-emit window is measured from here."
                ),
            ),
            COL_EMIT_COUNT: ColumnDefinition(
                type=ColumnType.INTEGER,
                not_null=True,
                default=0,
                description="Emissions so far (original + re-emits); the cap-3 stop.",
            ),
            COL_ACTIVITY_AT_EMISSION: ColumnDefinition(
                type=ColumnType.DATETIME,
                description=(
                    "QUIET-GAP capture: the recipient's last_model_activity_at "
                    "AS OF the most recent emission, frozen on the row. Guard 2 "
                    "reads THIS rather than the live sliding pair, so the "
                    "quiet-gap test cannot drift after the fact. Refreshed on "
                    "every emission (original and each re-emit). NULL = no "
                    "recorded activity at emission (or a pre-migration row) ⇒ "
                    "the whole preceding session was the quiet gap ⇒ guard 2 "
                    "passes."
                ),
            ),
            COL_CONSUMED: ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "Context-entry proof — flipped true when the recipient "
                    "instance performs model-initiated activity after an "
                    "emission. The drain STOPS on this. Equality-filterable "
                    "companion of ``consumed_at``."
                ),
            ),
            COL_CONSUMED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When consumption was stamped (audit; NULL = still owed).",
            ),
            COL_ESCALATED: ColumnDefinition(
                type=ColumnType.BOOLEAN,
                not_null=True,
                default=False,
                description=(
                    "Terminal: the cap fired (or recipient_gone past the time "
                    "cap). No further re-emit. Equality-filterable companion of "
                    "``escalated_at``."
                ),
            ),
            COL_ESCALATED_AT: ColumnDefinition(
                type=ColumnType.DATETIME,
                description="When escalation fired (audit; NULL until escalated).",
            ),
            COL_ESCALATION_REASON: ColumnDefinition(
                type=ColumnType.TEXT,
                description=(
                    "Why escalation fired: 'cap_reached' | 'recipient_gone'. "
                    "NULL until escalated."
                ),
            ),
        },
        indexes=[
            IndexDefinition(
                # The drain scan: owed rows for one recipient instance,
                # oldest-first. consumed/escalated are the equality predicates;
                # the window/cap are Python post-filters.
                name="idx_agent_direct_wake_drain",
                columns=[
                    "recipient_agent_instance_id",
                    COL_CONSUMED,
                    COL_ESCALATED,
                    "created_at",
                    "id",
                ],
            ),
            IndexDefinition(
                # Fork-1a re-home CAS: the owed rows for one recipient SESSION,
                # re-pointed to the successor instance on peer/register reconnect
                # (recipient_agent_session_id is stable across instance rotation).
                name="idx_agent_direct_wake_rehome",
                columns=[
                    "recipient_agent_session_id",
                    COL_CONSUMED,
                    COL_ESCALATED,
                ],
            ),
        ],
    )
    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="Agent messaging — direct-IMPORTANT-send outbox (REL-05).",
        tables={TABLE_AGENT_DIRECT_WAKE: direct_wake},
    )


def role_covered_mark_external_id(recipient_key: str) -> str:
    """Deterministic UNIQUE conflict key — one row per role.

    Mirrors ``role_binding_external_id``'s ``"role:{name}"`` shape exactly
    (design §1), defined locally rather than imported from the plugin's
    ``role_binding.py`` — core does not depend on the plugin layer (the
    plugin depends on core, never the reverse); ``role_message_external_id``
    just above is the same locally-defined-but-same-shape pattern.
    """
    return f"role:{recipient_key}"


def get_role_covered_mark_schema() -> SchemaDefinition:
    """Pull-surface boundary durable mark (design §1, R1-R7 approved).

    One row per role: the attested ``(covered_created_at, covered_id)`` pair
    that bounds ``peer_inbox``'s role-section default drain. Written ONLY by
    ``plugin::agent_messaging_plugin::peer_mark_role_covered`` (a
    registered-route-only, model-initiated attestation — see that verb's own
    docstring for the identity fence), read inline by
    ``AgentMessagingService._query_role_page`` on every call. Monotonic:
    an attestation at or below the stored mark is a no-op.
    """
    role_covered_mark = TableSchema(
        table_name=TABLE_ROLE_COVERED_MARK,
        id_prefix=ID_PREFIX_ROLE_COVERED_MARK,
        description=(
            "Durable per-role 'covered through' mark bounding the peer_inbox "
            "role section's default drain (pull-surface boundary design §1)."
        ),
        columns={
            "recipient_key": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="The role name this mark bounds.",
            ),
            "covered_created_at": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "normalize_sort_value form of the attested row's OWN "
                    "created_at — never a caller-asserted value (§2)."
                ),
            ),
            "covered_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "The attested row's OWN id — paired with "
                    "covered_created_at for the tie-safe (created_at, id) "
                    "comparison the merge/cursor logic already uses."
                ),
            ),
            "covered_message_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Audit: which arm-... message_id was attested.",
            ),
            "attested_by_agent_instance_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description=(
                    "Server-sourced from the calling route's own peer_binding "
                    "(R1) — never caller-supplied."
                ),
            ),
            "attested_by_agent_session_id": ColumnDefinition(
                type=ColumnType.TEXT,
                not_null=True,
                description="Server-sourced stable session id at attestation time.",
            ),
            "attested_by_session_label": ColumnDefinition(
                type=ColumnType.TEXT,
                description="Audit readability only; nullable best-effort.",
            ),
            "attested_at": ColumnDefinition(
                type=ColumnType.DATETIME,
                not_null=True,
                description="When this attestation was stamped.",
            ),
        },
        indexes=[
            IndexDefinition(
                # One row per role; point lookups only (§1) — no range scan.
                name="idx_role_covered_mark_lookup",
                columns=["recipient_key"],
            ),
        ],
    )
    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="Agent messaging — pull-surface boundary durable mark.",
        tables={TABLE_ROLE_COVERED_MARK: role_covered_mark},
    )


def get_role_read_schema() -> SchemaDefinition:
    """Schemas for two-phase, per-instance display receipts.

    The page/item pair freezes exactly what was returned before a caller can
    acknowledge it.  Only immutable receipt rows are suppression authority;
    the watermark is an indexed diagnostic and monotonic performance hint.
    """
    watermark = TableSchema(
        table_name=TABLE_ROLE_READ_WATERMARK,
        id_prefix=ID_PREFIX_ROLE_READ_WATERMARK,
        description="Per-instance diagnostic role display watermark.",
        columns={
            "recipient_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "agent_instance_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "read_created_at": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "read_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "read_message_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "observed_at": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
        },
        indexes=[IndexDefinition(name="idx_role_read_watermark_lookup", columns=["recipient_key", "agent_instance_id"])],
    )
    receipt = TableSchema(
        table_name=TABLE_ROLE_READ_RECEIPT,
        id_prefix=ID_PREFIX_ROLE_READ_RECEIPT,
        description="Immutable exact row receipt after receiver-side display acknowledgement.",
        columns={
            "recipient_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "agent_instance_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_row_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_message_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_created_at": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "acknowledged_at": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
        },
        indexes=[IndexDefinition(name="idx_role_read_receipt_lookup", columns=["recipient_key", "agent_instance_id", "role_row_id"])],
    )
    page = TableSchema(
        table_name=TABLE_ROLE_READ_PAGE,
        id_prefix=ID_PREFIX_ROLE_READ_PAGE,
        description="Pending or acknowledged opaque role display page.",
        columns={
            "token_hash": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "agent_session_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "agent_instance_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "held_role_scope_hash": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "include_important": ColumnDefinition(type=ColumnType.BOOLEAN, not_null=True),
            "expected_item_count": ColumnDefinition(type=ColumnType.INTEGER, not_null=True),
            "item_digest": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "status": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "issued_at": ColumnDefinition(type=ColumnType.DATETIME, not_null=True),
            "acked_at": ColumnDefinition(type=ColumnType.DATETIME),
        },
        indexes=[IndexDefinition(name="idx_role_read_page_token", columns=["token_hash"])],
    )
    item = TableSchema(
        table_name=TABLE_ROLE_READ_PAGE_ITEM,
        id_prefix=ID_PREFIX_ROLE_READ_PAGE_ITEM,
        description="Immutable exact role envelope rows belonging to an issued page.",
        columns={
            "page_external_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "recipient_key": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_row_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_message_id": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
            "role_created_at": ColumnDefinition(type=ColumnType.TEXT, not_null=True),
        },
        indexes=[IndexDefinition(name="idx_role_read_page_item_page", columns=["page_external_id"])],
    )
    return SchemaDefinition(
        namespace=NAMESPACE,
        version="1.0.0",
        description="Agent messaging per-instance role display receipts.",
        tables={
            TABLE_ROLE_READ_WATERMARK: watermark,
            TABLE_ROLE_READ_RECEIPT: receipt,
            TABLE_ROLE_READ_PAGE: page,
            TABLE_ROLE_READ_PAGE_ITEM: item,
        },
    )


__all__ = [
    "COL_CONSUMED",
    "COL_ACTIVITY_AT_EMISSION",
    "COL_EMITTED_TO_AGENT_SESSION_ID",
    "COL_CONSUMED_AT",
    "COL_EMITTED_TO_AGENT_INSTANCE_ID",
    "COL_EMIT_COUNT",
    "COL_ESCALATED",
    "COL_ESCALATED_AT",
    "COL_ESCALATION_REASON",
    "COL_LAST_EMITTED_AT",
    "ESCALATION_REASON_CAP",
    "ESCALATION_REASON_GONE",
    "ID_PREFIX_DIRECT_WAKE",
    "ID_PREFIX_MESSAGE",
    "ID_PREFIX_ROLE_COVERED_MARK",
    "ID_PREFIX_ROLE_READ_PAGE",
    "ID_PREFIX_ROLE_READ_PAGE_ITEM",
    "ID_PREFIX_ROLE_READ_RECEIPT",
    "ID_PREFIX_ROLE_READ_WATERMARK",
    "ID_PREFIX_ROLE_MESSAGE",
    "ID_PREFIX_THREAD",
    "META_KEY_DELIVERY_EXTERNAL_ID",
    "META_KEY_RECIPIENT_KEY",
    "META_KEY_RECIPIENT_KIND",
    "META_KEY_ROLE_ROW_ID",
    "NAMESPACE",
    "RECIPIENT_KIND_INSTANCE",
    "RECIPIENT_KIND_ROLE",
    "ROLE_THREAD_PREFIX",
    "SENDER_PRINCIPAL_KIND_OAUTH_CLIENT",
    "SENDER_PRINCIPAL_KIND_STDIO_AGENT",
    "SENDER_PRINCIPAL_KIND_SYSTEM",
    "SENDER_PRINCIPAL_KIND_UNKNOWN",
    "TABLE_AGENT_DIRECT_WAKE",
    "TABLE_AGENT_MESSAGE",
    "TABLE_AGENT_ROLE_MESSAGE",
    "TABLE_AGENT_THREAD",
    "TABLE_ROLE_COVERED_MARK",
    "TABLE_ROLE_READ_PAGE",
    "TABLE_ROLE_READ_PAGE_ITEM",
    "TABLE_ROLE_READ_RECEIPT",
    "TABLE_ROLE_READ_WATERMARK",
    "get_agent_direct_wake_schema",
    "get_agent_messaging_schema",
    "get_agent_role_message_schema",
    "get_role_covered_mark_schema",
    "get_role_read_schema",
    "role_covered_mark_external_id",
]
