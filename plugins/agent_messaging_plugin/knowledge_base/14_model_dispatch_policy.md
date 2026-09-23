# Model Dispatch Policy

Tags: knowledge:tag:plugin_reference, knowledge:tag:dispatch_policy, knowledge:tag:model_assignment

Article Layer: 1

Article Role: plugin_reference

Article Tags: planning-stage:agent-to-agent-coordination, evidence-category:operator-ruling, domain:agent-messaging, domain:managed-dispatch

Embedding Description: How the platform enforces the required Claude-orchestrator and Codex-Terra implementation dispatch policy at spawn time, during paired diagnosis/design sweeps, and at the main seat hook.

## Ruling

The accepted operator ruling is: “So from now on, we will be using Sonnet five
as our orchestrator. We will be assigning all work to Terra.” The enforcement
point is `plugin::agent_messaging_plugin::spawn_session`, not a plan or memory
note, because every managed dispatch passes that verb.

The declarative source is
`plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`. It is loaded
for every spawn and validated against model identities declared under
`plugins/agent_messaging_plugin/model_profiles/`; a missing, malformed, or
unknown policy model refuses rather than choosing a fallback.

| Dispatch kind | Required assignment |
| --- | --- |
| `diagnose` / `design` | Two producers from different vendors, sharing a `pair_id`, unless the active, ruling-cited `budget_vendor_override` relaxes pairing for that kind |
| `review` | codex `gpt-5.6-terra` or claude_code `claude-sonnet-5`, and normally the reviewer must be the other vendor than `reviewed_report_vendor`; an active, ruling-cited override may permit same-vendor review |
| `fix` | codex `gpt-5.6-terra` / `gpt-5.6-luna` / `gpt-5.3-codex`, or claude_code `claude-sonnet-5` / `claude-opus-5` / `claude-fable-5-1` (`claude-sonnet-5` added 2026-09-20 so a genuinely low-score, non-schema fix ticket has a cheap option, `iss_da9e5e67`) |
| `infrastructure` | Any runtime/model pair; it remains recorded on the ledger — but never below a capability floor (next section) |

## Capability floors — scope outranks dispatch kind

A capability floor is a minimum `(agent_runtime, model)` set keyed on WHAT the
work touches, not on which `dispatch_kind` a caller picked. The policy JSON
must declare `capability_floors` and must contain the `state_schema` floor;
deleting or renaming it makes every spawn refuse with
`dispatch_policy_invalid`, the same fail-closed shape as deleting the `fix`
row. It exists because two Sonnet-5 landings on consecutive days
(`iss_63d91ca9`, `iss_5f91287d`) redeclared platform-protected fields in a
`ColumnDefinition` — a defect no static gate checks — and crashed
`initialize_schemas` on every blue-green candidate boot; the ticket's raw
complexity score never captured that risk, and `dispatch_kind=infrastructure`
(`allow_any_pair`) was the route by which a cheap model reached it.

| Floor | Applies when | Allowed pairs |
| --- | --- | --- |
| `state_schema` | the caller declares `scope_tags: ["state_schema"]`, OR the workbench brief text contains any `brief_markers` entry (`get_schema_definitions`, `ColumnDefinition`, `TableSchema`, `SchemaDefinition`, `initialize_schemas`, `state-service table`, `state table`) | claude_code `claude-opus-5` or `claude-fable-5-1` |

The floor check runs first in `validate_spawn_dispatch` and never consults
`allow_any_pair`, so `infrastructure` cannot lower it; a declared tag is
honoured even when the brief is silent, and a brief marker applies the floor
even when the tag was omitted, so omission never lowers a floor either.
`spawn_session`, `dispatch_managed_work`, and `provision_role_session` accept
`scope_tags`; `select_dispatch_tier` accepts the same list and intersects the
floor with the kind's allowlist so it never recommends a pair the floor
forbids. `managed_session` records `scope_tags` (declared) and
`capability_floors` (every floor applied, with its `source` — `declared` or
`brief_marker` — and `detail`), so "which floor governed this model" is a
ledger read.

- `capability_floor_violation`: a floor applies and the pair is outside it.
- `scope_tag_unknown`: a `scope_tags` entry names no declared floor.
- `scope_tag_invalid`: a `scope_tags` entry is empty.

The companion landing-time gate, `quality_gates/schema_init_gate.py`, boots
the real `initialize_schemas` path against the candidate tree and refuses a
landing on any schema-standardizer violation regardless of which model built
it; the floor and the gate are the dispatch-time and landing-time halves of
the same prevention.

The `Main` orchestrator-role suffix may use only `claude-sonnet-5` or
`claude-fable-5`. The tracked seat hook reads the latest usage-bearing
assistant model from the same transcript source as the tracked rotation-watch
hook. An invalid seat model produces an
`ORCHESTRATOR_MODEL_VIOLATION` line and blocks only
`spawn_session` / `dispatch_managed_work` tool calls until the seat relaunches
with the stated `--model` flag.

## Spawn arguments and refusals

`dispatch_kind` is mandatory for `plugin::agent_messaging_plugin::spawn_session`
and `plugin::agent_messaging_plugin::dispatch_managed_work`. `pair_id` is
mandatory for `diagnose` and `design`; `reviewed_report_vendor` is mandatory
for `review` and must be `codex` or `claude_code`.

- `dispatch_kind_required`: no kind was supplied.
- `dispatch_policy_violation`: the pair is not allowed, a review is same-vendor,
  or a required pair/review field is absent.
- `dispatch_policy_unavailable`: the policy file cannot be read.
- `dispatch_policy_invalid`: the policy shape or its model-profile references
  are invalid.

`managed_session` records `dispatch_kind`, `reviewed_report_vendor`,
`pair_id`, `scope_tags`, and `capability_floors`. `session_sweep.py` sends a `dispatch_policy_unpaired` notice to the
spawning role when a live diagnose/design producer has no live other-vendor
partner with the same `pair_id` within ten minutes of spawn. An active
`budget_vendor_override` suppresses that notice only for the kinds it names.
The JSON object is optional but, when present, must contain a boolean `active`
and a non-empty `ruling_ids_by_kind` mapping of `diagnose`, `design`, and/or
`review` to full ruling IDs; malformed entries refuse policy loading. The
notice is a delivery reminder, not a synthetic second producer.

## Scope boundary

This policy does not create composite dispatch verbs. A later
`dispatch_diagnosis` or `dispatch_review` workflow can consume the three
ledger fields, policy JSON, and unpaired notice, but it must still explicitly
create and drive both producer/reviewer sessions.

## References

- `plugins/agent_messaging_plugin/model_dispatch_policy.v1.json`
- `plugins/agent_messaging_plugin/src/agent_messaging_plugin/model_dispatch_policy.py`
- `plugin::agent_messaging_plugin::spawn_session`
- `plugin::agent_messaging_plugin::dispatch_managed_work`
- `plugin::agent_messaging_plugin::select_dispatch_tier`
- `quality_gates/schema_init_gate.py`
