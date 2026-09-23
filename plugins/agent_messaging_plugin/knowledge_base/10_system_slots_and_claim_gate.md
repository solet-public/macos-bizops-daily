# System Slots — Reserved Keyspace, Declaration Registry, and the §6.1 Claim Gate

Article Layer: 1

Article Role: plugin_reference

Tags: knowledge:tag:plugin_reference, knowledge:tag:agent_messaging, knowledge:tag:role_model, knowledge:tag:system_slots, knowledge:tag:authorization

Article Tags: planning-stage:agent-to-agent-coordination, planning-stage:server-side-internals, evidence-category:identity-model, evidence-category:authorization-contract, domain:agent-messaging, domain:inter-agent-routing

Embedding Description: The role-model v4 system-slot substrate — the reserved sys: keyspace that keeps platform capability slots disjoint from operator-chosen user role names, the slot-declaration registry (plugin-owned vs session-filled), and the peer_claim_role claim gate that rejects sys:* from user-facing callers, verifies a plugin-owned slot against the server-built CallContext principal, and hands session-filled slots (sys:autonomic) to the INF-01 auto-assignment lane.

## Purpose

A SYSTEM slot is a capability slot the *platform* declares — as opposed to a USER
role name ("Coordinator-Day", "Architect"), which is operator-chosen and arbitrary.
The first (and today only) system slot is `sys:autonomic`, the organism's
inference-of-last-resort (INF-01). This article is the contract for how system
slots are named, declared, and claimed — and specifically how the claim gate keeps
a bridge session from hijacking a platform capability.

Slice-C builds this substrate (§6/§6.1 of the first-class role model +
`workbench/2026-07-02_inf01_autonomic_inference_spec.md` §D.2/§D.6). The
auto-assignment *lifecycle* for `sys:autonomic` (vacancy-fill, succession) is
INF-01's lane (§D.9), not this article — see
`workbench/2026-07-03_inf01_slicec_seam_boundary.md`.

## The reserved `sys:` keyspace (why a prefix, not just an origin column)

System-slot identities live behind a reserved prefix, `SYSTEM_ROLE_PREFIX = "sys:"`
(so `sys:autonomic`). The prefix is collision-proofing chosen over an `origin`
column alone: a column records provenance but does not *prevent* a user from
choosing a colliding name. Because the general claim verb rejects any name in the
reserved prefix, user identities and system identities occupy **disjoint
`external_id` keyspaces** — a collision is structurally impossible. The
no-role-name-literal rule (roles are never hardcoded in code) scopes to USER names
only; a system slot's identity is deliberately a platform CODE CONSTANT
(`SYS_AUTONOMIC_SLOT`), imported by both the declaration and its consumers.

`is_system_role(name)` is the one-line predicate (`name.startswith("sys:")`).

## The declaration registry

Each system slot is declared as `SystemSlotDeclaration(slot_name, owner_plugin,
holder_kind)` in a platform registry fixed at import (startup). The registry key is
the `slot_name`, so a duplicate declaration is structurally impossible.
`validate_system_slot_declarations()` runs at plugin readiness and fails loud on a
malformed declaration (key ≠ slot_name, a non-`sys:` name, an unknown
`holder_kind`) — a declaration bug is a startup-blocking error, never a silently
tolerated state.

The declaration's `owner_plugin` + `holder_kind` are the **claim-gate
discriminator** — no separate `fill_kind` field is needed:

- **plugin-owned** (`owner_plugin` set) — a future capability slot owned by a
  specific plugin. Only that plugin may claim it. (No production plugin-owned slot
  exists yet; the gate machinery is complete and smoke-covered via a fixture.)
- **session-filled** (`owner_plugin=None`, e.g. `sys:autonomic`) — a slot filled by
  a live bridge session, assigned by the INF-01 §D.9 auto-assignment policy, NEVER
  the general claim verb.

## The §6.1 claim gate (on `peer_claim_role`)

`peer_claim_role` is the general, bridge-facing claim verb (`/rename` runs it). Its
gate, evaluated before any claim work, is `evaluate_system_slot_claim(name,
call_context)`:

- **not a `sys:` name** → the claim proceeds normally (an ordinary user role).
- **a `sys:` name not in the registry** → rejected (`system_slot_claim_denied`).
- **session-filled** (`sys:autonomic`) → rejected: it is assigned by §D.9
  auto-assignment, not claimable via this user-facing verb.
- **plugin-owned** → allowed ONLY when the principal is the declared owner
  (`principal_kind == 'plugin'` AND `calling_plugin == owner_plugin`); any other
  principal (a different plugin, an operator, or a missing context) is rejected —
  the gate FAILS CLOSED.

### The principal is server-built, never caller-supplied

The gate's authority to allow a plugin-owned claim rests entirely on the
`CallContext`, and that context must be **unspoofable**. It is not read from the
caller's `params`; it is the SERVER-BUILT context the action processor lifts into
`state`:

`_execute_plugin_method` builds a fresh `state` dict server-side and lifts
`state["call_context"] = self._build_call_context(action)` — reusing the same
vault-hardened `_build_call_context` that derives the principal from the
routing-table-resolved `source_plugin` (a plugin caller → `for_plugin`) or the
authenticated bridge principal (else operator/external). The verb reads
`state.get("call_context")`, never `params`, so a caller cannot forge ownership of
another plugin's slot.

This is the EDGE realization of the ratified "opt into `requires_call_context`"
intent. `requires_call_context` is a `@service_interface_process` mechanism that
injects `call_context` as a kwarg on the service path; EDGE `@platform_process`
verbs use the `(params, state)` convention with server-side state-lifts, so the
same server-built-principal property is realized by the state-lift above (the same
shape as the sibling inference-vertex identity lift on that path). The security
property is identical; the mechanism matches the path it lives in.

## No-vacant-release

`peer_release_role` rejects any `sys:` name (`system_slot_release_denied`). A system
slot is only ever RE-BOUND (a claim that atomically replaces the holder), never
released to vacant — a vacant system slot strands its capability (a vacant
`sys:autonomic` strands the organism's inference-of-last-resort). The binding-STATE
boot invariant (a session-filled slot LOUD-WARNs when zero sessions are live; a
plugin-filled slot fails boot when unbindable) rides the INF-01 autonomic readiness
lane (§D.9) — slice-C provides the declaration-INTEGRITY check that precedes it.

## Where INF-01 plugs in

INF-01 imports `SYS_AUTONOMIC_SLOT`, resolves the holder via
`resolve_role_binding_v4` (typed — it discriminates a session holder from a provider
holder off `holder_kind`), and its §D.9 register/unregister hooks call the slice-B
`claim_role_binding_v4` compare-and-set primitive to fill/succeed the slot, selecting
the newest LIVE session over the open bridge set (`BridgeSessionManager.list_active()`
— never the stale-inclusive `peer_binding` row set). System slots are otherwise
ordinary roles: same binding table, same predicated-CAS (§5.1), same both-party
notifications (§5.4).

## The client half — `provides_inference` on every register POST

INF-01's server hooks only ever see a session as autonomic-eligible when its
register POST carries `provides_inference: true`. The field lives on the
`/api/v1/bridge/{bridge_id}/peer/register` body (default `false`,
`http_routes.py`); when true, the route populates the per-instance
`SessionInferenceProvider` sidecar and Trigger-1 runs its vacancy-fill. When
absent or false, Trigger-1 short-circuits to `not_provider` and the register
leaves the slot untouched — a fleet whose clients never send the field keeps
`sys:autonomic` vacant forever and every organism turn DEFERs to the durable
queue (the 2026-07-10 chronic boot signature; fixed that day).

The stdio MCP bridge (`mcp_bridge/__main__.py` + `forwarder.py`) therefore
declares the capability on EVERY register surface — the auto-registration at
bridge open, the reconnect re-register (the sidecar entry does not survive a
reconnect, so re-assertion is load-bearing, §D.9 reconnect-survival part 2),
and the manual `peer_register` relabel (a relabel must not demote the session
to non-provider).

The declaration is capability-honest, keyed on the host agent kind: only
`claude_code` bridges send `true`. A Codex holder would be deaf — the patched
Codex CLI consumes ONLY `notifications/solet/peer_message`
(`forwarder._notification_method_for`), so vertex forwards and
`inference_completion_request` events emitted on the claude-channel method
would never reach the Codex session, parking organism turns until the
serve-timeout sweep requeues them. If Codex ever gains a channel-event sink,
widen the predicate in `mcp_bridge/__main__.py::_run` deliberately.
