# Model Dispatch Policy

Tags: knowledge:tag:plugin_reference, knowledge:tag:dispatch_policy, knowledge:tag:model_assignment

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, evidence-category:operator-ruling, domain:agent-messaging, domain:managed-dispatch

Embedding Description: Current open dispatch-kind provenance, scope tags, selector receipt replay, retired state-schema model floor, and separation of Main-seat launcher choice from work-unit selection.

## Current contract

The source is `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
(`schema_version: 2`, `policy_version: model-dispatch-policy-v3`). It is read for each managed spawn. The historical `.v1.json` filename remains the consumer path. The schema contains only version, dispatch-kind provenance examples, and capability floors; the retired `orchestrator` field or any other extra top-level field refuses policy loading.
Its `dispatch_kinds` section is an empty object because dispatch kinds are
open provenance text, not a model-pair assignment map. The policy parser checks
its shape and rejects nonempty rule objects; it does not restrict callers to
keys in that section. Project Solet's `UnitDispatch.__post_init__` accepts
nonblank unit-kind text. All 16 kind values observed in the live register on
2026-09-24, including `test`, `implement`, `implementation`, `integration`,
`smoke-test`, and `build`, therefore pass as provenance. An additional
nonblank phase minted later also passes without a policy edit. Blank or
non-string values refuse; no case folding, aliasing, or score change occurs.

`spawn_session` and `dispatch_managed_work` require `dispatch_kind`.
`select_dispatch_tier` may omit it; when supplied, it uses the same nonblank
text rule. `reviewed_report_vendor` and `pair_id` are optional provenance
fields. A selector receipt records the required score, billing objective,
selected runtime/model/effort, policy version, and applied floors. Managed
spawn replays an enforced receipt for the exact tuple.

`select_dispatch_tier` also takes an optional `runtime` (`claude_code` or
`codex`). A coordinator that has chosen the runtime passes it so the cheapest
cell of the other runtime cannot dominate its choice out: the catalog is
filtered to that runtime before domination and selection, and the selector
stays cheapest-within-runtime with no runtime preference of its own. The
receipt carries the value as `runtime_constraint` (null when absent), replay
re-applies it, and a receipt without that key is refused; re-select to get one.

A flat-rate subscription pool (the Claude plan) starts `unknown` in the shipped
usage profile, and an unknown quota never excludes a cell: Claude cells are
selectable with no reading recorded. Only a known `exhausted` status excludes
one. An optional `record_allowance_pool_reading` (a percent of the window, the
instant it was read, when it expires, its source and recorder) lays a current
reading over the static profile; a reading showing no percent remaining makes
the pool exhausted until it expires, and a pool is exhausted even when another
pool it shares a cell with is unknown. A reading past its expiry no longer
counts and is named in any refusal, and nothing is ever inferred.
`read_allowance_pool_readings` lists them and `retract_allowance_pool_reading`
removes one.

## Flat-rate ranking: dispatch weights (iss_eef0812b)

Under a flat-rate plan the plan, not the API meter, is what is charged, yet
`relative` cost is only metered dollars divided by one catalog-wide constant, so
it ranks exactly as `metered_usd` does. A flat-rate profile may therefore declare
`dispatch_weights`: a `by_model_prefix` table, a mandatory `default_weight`, a
`basis` (`operator_policy` with a `ruling_id`, or `measured` with an
`evidence_ref`) and `declared_at`. The longest matching prefix wins. A weight is
an operator-declared dispatch preference, never a token or allowance conversion,
and the provider publishes none. The shipped Max 20x profile declares
`claude-sonnet` at 0.5 under `rul_29449a0b-3b9e-4251-bf87-4d295269a409`
(decision `dec_838f872f-af5f-46a5-abcd-7b8619d6e4d2`), default 1.0. The value is
a policy choice that counts Sonnet's lower list price twice on purpose; whether
Sonnet usage also draws the all-model pool is an unverified assumption.

The objective `allowance_weighted` ranks by `relative_cost_multiplier * weight`.
It is the default only when `runtime` is passed, a current flat-rate plan covers
it and that plan declares weights. It is plan-scoped: an explicit
`allowance_weighted` without such a runtime is `parameter_invalid`, and
unconstrained or Codex selections are never weighted, so allowance units are not
compared with dollars. A covering plan with no table keeps the metered-dollar
ranking and the answer carries a `warnings` line, which is not a refusal. The
answer's `cost_basis` names the plan, the table and the weight applied, and
`frontier`, `dominated` and `ladder` are in weighted units. The receipt keeps its
keys; `billing_objective` records the ranking, and replay recomputes it from the
profile, so editing a weight makes an in-flight receipt mismatch until the
dispatcher re-selects. Capability floors and a known-exhausted pool still
exclude cells before ranking.

Two refusals belong to the weighted path. With a runtime constraint the profile
is loaded before quota is read, so an unloadable or malformed profile (a bad
`dispatch_weights` table included) refuses `usage_economics_invalid`; an
unconstrained selection still refuses `quota_state_unknown` for the same fault.
And when two current flat-rate plans cover the same runtime the weights would be
ambiguous, so the selection also refuses `usage_economics_invalid` rather than
choosing one.

## Schema scope provenance

`scope_tags: ["state_schema"]` records that a dispatch touches state schema.
It is accepted as nonblank provenance and does not constrain the model pair.
The `state_schema` capability floor was retired under operator ruling
`rul_0c6ec7c7-70c6-442a-9695-3738efb61ccd`: `capability_floors` is now
empty, and schema identifiers in a brief do not apply a model-choice gate.
Selectors still require a fresh accepted catalog cell that clears the numeric
difficulty score. Policy loading requires a `capability_floors` object and
validates any configured future floor, but accepts an empty object and refuses
an attempted restoration of the retired `state_schema` model floor.

The ruling's remedy is a state-documentation link and mechanically assembled
linked information in each schema-touching work-unit brief. This policy change
does not implement brief assembly; `iss_25f79570-9b0d-48e7-a73f-e3a56242b181`
tracks retirement of the model floor in this candidate.

The landing companion `quality_gates/schema_init_gate.py` boots the real
`initialize_schemas` path against a candidate tree. A successful dispatch
selection does not replace that landing gate.

## Main-seat hook transition and failures

The Main seat chooses and configures its launcher model separately from the
work-unit selector, under operator-present seat authority. The fixed Claude
Main-seat model list is retired. The Claude Code project settings file (.claude/settings.json) no longer registers
`orchestrator_model_gate.py` for `PreToolUse` or `SessionStart`. That script is
temporarily callable as a no-op for active Claude processes retaining a cached
hook command; it makes no model or role-authorization decision. After relevant
seats confirm settings reload, a separate cleanup can remove the script and its
registered transition smoke. The server still authenticates managed-dispatch
role authority and replays selection receipts before host dispatch.

The dispatch ledger records kind, declared scope tags, and applied floors
(currently empty) with source and detail. A missing or blank spawn kind returns
`dispatch_kind_required`; a non-string kind returns
`dispatch_policy_violation`. Missing or malformed policy/profile data returns
`dispatch_policy_unavailable` or `dispatch_policy_invalid`. A blank or
non-string scope tag returns `scope_tag_invalid`. A forged, absent, or stale
selection receipt and a caller without the held coordinator role retain their
server-side refusals.

## References

- `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_dispatch_policy.py`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_capability_verbs.py`
- `plugin::agent_messaging_plugin::spawn_session`
- `plugin::agent_messaging_plugin::dispatch_managed_work`
- `plugin::agent_messaging_plugin::select_dispatch_tier`
- `quality_gates/schema_init_gate.py`
