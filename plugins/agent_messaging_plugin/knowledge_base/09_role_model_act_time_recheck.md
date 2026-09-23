# Role Model — Act-Time Ownership Re-Check and the No-Tombstone Invariant

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:role_model, knowledge:tag:identity_model, knowledge:tag:state_interface

Article Tags: planning-stage:agent-to-agent-coordination, planning-stage:server-side-internals, evidence-category:identity-model, evidence-category:invariant-contract, domain:agent-messaging, domain:inter-agent-routing

Embedding Description: The role-model v4 platform contract — why role resolution is advisory and a holder must re-check ownership at act time via holds_role, the load-bearing no-tombstone invariant that role bindings must be hard-deleted (a soft-delete tombstone permanently deadlocks the slot), and the race-safe claim/displace mechanic (write_state INSERT as the race primitive, non-completed ActionResult on a UNIQUE conflict, predicated compare-and-set on claim_epoch).

## Purpose

The first-class role model (design `workbench/2026-07-02_first_class_role_model_design.md`)
makes a discriminated `role_binding` state table the single resolution + ownership
authority for a role name. This article is the CONTRACT that consumers of that
authority must honor: how to read a role safely, why a released binding must be
hard-deleted, and how the race-safe claim path actually works. It is deliberately
short and load-bearing — building on a stale reading of any point here reintroduces
a real defect (a wrong-holder action, or a permanently stuck role slot).

The identity vocabulary is the same one used throughout this plugin's KB
(`03_inter_agent_messaging.md`, `08_mcp_bridge_troubleshooting.md`):

- `agent_instance_id` (`agi-*`) — minted fresh on every bridge-subprocess launch;
  NOT stable across a reconnect.
- `agent_session_id` — the STABLE per-logical-session key; survives a reconnect and
  the `agent_instance_id` rotation. This is the identity a role's ownership is keyed on.
- `session_label` — display-only, never a routing or authority key.

## Resolution is advisory — re-check ownership at ACT time (§5.0)

Resolving a role (`resolve_role_binding_v4`, and the routing behind
`peer_send_by_name`) answers exactly one question: *who holds role X right now.*
That answer is a snapshot. Between the moment a session resolves a role and the
moment it acts on that resolution, another session can claim or displace the
holder — so a routing or authority decision made at resolve time can already be
stale by act time.

The platform contract is therefore: **a role holder RE-CHECKS ownership at the
moment it acts on the authority the role confers**, not only when it was routed to.
The primitive is:

```
holds_role(state, name, agent_session_id) -> bool
```

It reads the live binding and compares the binding's stable `agent_session_id` to
the caller's own. Rules that make it safe:

- An empty `agent_session_id` is never an identity — it never matches (returns `False`).
- A vacant role returns `False` (nobody holds it, so neither does the caller).
- A session that has been DISPLACED sees `False` — this is the reference-abort: the
  displaced holder must not act on a role it no longer owns.
- A malformed binding row is NOT swallowed — it raises `RoleBindingMalformedError`
  so a genuine data fault surfaces rather than silently reading as "not held".

Use `resolve_*` to route; use `holds_role` to gate a privileged act. They are two
different questions and the gap between them is where staleness lives.

## The no-tombstone invariant (load-bearing)

A `role_binding` row MUST be hard-deleted on release (`delete_records` with
`soft_delete=False`). This is not a cleanliness preference — it is a correctness
invariant the claim path depends on:

- The claim's race primitive is an INSERT against the `external_id` UNIQUE
  constraint (`role:{name}`). The Postgres UNIQUE index **ignores `is_deleted`**.
- So a soft-delete tombstone (`is_deleted=1`) still occupies the UNIQUE slot: the
  first-claim INSERT conflicts on the DEAD row, while both the `is_deleted=0`
  re-read AND the `is_deleted=0` displace compare-and-set HIDE it.
- The result is a permanently deadlocked role: you cannot INSERT (the tombstone
  conflicts) and you cannot compare-and-set (the tombstone is filtered out). The
  slot is stuck with no live holder and no way to claim it.

Anything that can leave a soft-deleted `role_binding` row reintroduces this
deadlock: a release variant that forgets `soft_delete=False`, a bulk cleanup using
`delete_records`' default (which is a SOFT delete), or a migration that copies
`is_deleted=1` rows. Release hard-deletes; displace never deletes (it
compare-and-sets the holder in place); so in a healthy system a tombstone never
exists to begin with.

## The race-safe claim / displace mechanic (§5.1)

`claim_role_binding_v4` is a bounded compare-and-set loop:

1. **First claim** is a `write_state` INSERT. Exactly one concurrent first-claim wins
   the `external_id` UNIQUE race. Critically, on a UNIQUE conflict `write_state`
   **RETURNS a non-completed `ActionResult` — it does NOT raise**: the Postgres
   provider's raw `psycopg.UniqueViolation` is caught by `write_state`'s broad
   `except (psycopg.Error, …)` and converted to an error envelope. (The abstraction
   `UniqueViolationError` translated by the `open_store` Store surface is a
   DIFFERENT consumer path that `write_state` never touches.) So the first-claim
   inspects the raw `ActionResult` with `is_completed(...)` — NOT `require_completed`,
   which would raise on the expected conflict — and on a non-completed result
   RE-READS by `external_id` to disambiguate.

2. **Re-read disambiguation.** A live holder on re-read → displace. No row on re-read
   → the holder released in the race window (release hard-deletes) → retry the INSERT.
   A genuine query fault raises `StateOperationError` and propagates (fail loud). The
   bounded loop exhausting raises `RoleClaimContendedError` carrying the last write
   detail, so a PERSISTENT non-conflict fault (e.g. a not-null violation) is never
   masked as mere contention.

3. **Displace** is a predicated compare-and-set: read the current `claim_epoch` E,
   then `update_state WHERE external_id AND claim_epoch=E AND is_deleted=0 SET
   holder…, claim_epoch=E+1`. Rows-affected `1` = won; `0` = another session moved
   the epoch under us → re-read and retry. The displaced prior holder is captured
   per-attempt (tied to epoch E) so the caller notifies exactly the session it
   displaced, routed to that session's CURRENT bridge via its stable
   `agent_session_id`.

4. **Self-re-claim** (the live holder's `agent_session_id` equals the claimant's) is
   an IDEMPOTENT refresh, never a displace: it re-points the instance in place with
   NO epoch bump and NO handover notification. This is why a session that reconnects
   (rotated `agent_instance_id`, same `agent_session_id`) and re-claims its own role
   does not self-notify.

## Sourcing the session id on claim (REL-07)

`peer_claim_role`'s arguments do not carry `agent_session_id`. The claim MUST source
it from the CLAIMANT's own live `peer_binding` row (by `agent_instance_id`), never
from claim args — an empty `agent_session_id` on the binding makes the reconnect
self-refresh (`refresh_role_binding_cas`, keyed on the stable session id alone) match
nothing, so a claimed role's reroute can never heal until the holder re-claims
explicitly. A populated session id is what lets one predicated compare-and-set
re-point every role a reconnecting session holds.

## Status and slice-D migration flag

As of 2026-07-02 (slice-B, built + gate-green): the new `role_binding` table, the
v4 resolver, `claim_role_binding_v4`, `holds_role`, and the reverse-lookup
(`resolve_by_agent_session_id`) are built and smoked, but the LIVE resolution +
claim path still runs on the legacy `agent_role_binding` table. The atomic cutover
of the live callers to `role_binding` is the §9 migration (slice-D).

**Slice-D migration flag (do not lose this):** the migration MUST filter
`is_deleted=1` when copying rows from `agent_role_binding` into `role_binding`.
Copying a tombstone reintroduces the permanent-deadlock described above. Migrate
live rows only.
