# Scheduling Service Reference
Article Layer: 1

The scheduling service provides two types of scheduled execution:
- **Recurring** schedules via cron expressions
- **One-time** delayed execution after a specified number of seconds

All scheduled wake-ups execute independently of the current inference chain. They fire asynchronously through the action queue.

---

## Execution Modes

Both scheduling operations require exactly one execution mode:

1. `action_definitions`: a non-empty list of syntactically valid canonical
   `{process_key, arguments}` objects. Registration validates entry shape and
   the scheduled-action result-processor policy; it does not promise that a
   named process exists. Execution resolves each `process_key` against processes
   registered at fire time. Use this mode for work that must happen, including
   scheduled peer notifications.
2. `memory_tag`: a terminal
   `service_interface::memory_service::get_memories_by_tag` read. The fetched
   content is written to the scheduled action's result row; no model turn or
   downstream action starts. Use this only when that terminal read is the whole
   intent.

Supplying both modes or neither mode fails loudly. `actions` remains an internal
legacy alias, while `action_definitions` is the discoverable public field.

---

## create_cron_schedule

Create a recurring schedule using a cron expression. The schedule fires repeatedly until cancelled.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `cron_expression` | string | Yes | Standard 5-field cron expression (UTC) |
| `action_definitions` | list | One mode required | Non-empty, syntactically valid `{process_key, arguments}` actions |
| `memory_tag` | string | One mode required | Terminal memory read; does not start a model turn |
| `label` | string | No | Human-readable label |
| `tags` | list | No | Tags for grouping (used by `clear_scheduled_actions_by_tag`) |

### Cron Expression Format

```
minute (0-59)
hour (0-23)
day of month (1-31)
month (1-12)
day of week (0-6, 0=Sunday)
```

**Common expressions:**

| Expression | Meaning |
|-----------|---------|
| `* * * * *` | Every minute |
| `*/5 * * * *` | Every 5 minutes |
| `0 * * * *` | Every hour (at minute 0) |
| `0 9 * * *` | Daily at 9:00 AM UTC |
| `0 9 * * 1-5` | Weekdays at 9:00 AM UTC |
| `0 0 * * 0` | Weekly on Sunday at midnight |
| `0 */6 * * *` | Every 6 hours |

### Example: Scheduled Peer Notification Every 15 Minutes

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "*/15 * * * *",
    "label": "Coordinator status reminder",
    "tags": ["coordination:status-reminder"],
    "action_definitions": [
      {
        "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
        "arguments": {
          "name": "Coordinator",
          "content": "Scheduled reminder: review active scheduler work."
        }
      }
    ]
  }
}
```

The scheduler invokes `peer_send_by_name` directly. No Claude or Codex
inference is required when the schedule fires. The peer message is the action
itself, not a prompt asking a seat to perform some other scheduled action.

### Example: Scheduled Mechanizable Joseki

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "0 6 * * 1",
    "label": "Weekly platform quality sweep",
    "action_definitions": [
      {
        "process_key": "service_interface::thinking_service::run_joseki",
        "arguments": {
          "joseki_key": "run_platform_quality_gates",
          "bindings": {},
          "label": "Scheduled platform quality sweep"
        }
      }
    ]
  }
}
```

Scheduler submission works for registered, mechanizable cards such as the
closed-world deterministic `run_platform_quality_gates` card. `run_joseki`
validates the card and bindings at execution time; this example does not imply
that inference-bearing or otherwise non-mechanizable cards are admitted.

---

## ensure_global_heartbeat

Ensure the platform has a single global heartbeat wake-up scheduled (tag convention: `heartbeat:global`).

This is an idempotent helper for liveness and responsiveness:

- If no heartbeat exists, it creates one.
- If duplicates exist, it normalizes back to a single schedule.
- If a heartbeat exists with the same cadence, it returns the existing schedule.

The helper currently uses the terminal `memory_tag` mode. It can maintain the
scheduled read/touch, but it does not create a model turn or cause a model to
act on the fetched content.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `cadence_minutes` | integer | No | Desired cadence in minutes (default: 5) |
| `tag` | string | No | Schedule tag to use (default: `heartbeat:global`) |
| `memory_tag` | string | No | Memory tag to wake up (default: same as tag) |

### Example: Ensure a 5-minute global heartbeat

```json
{
  "process_key": "service_interface::scheduling_service::ensure_global_heartbeat",
  "arguments": {
    "cadence_minutes": 5
  }
}
```

---

## get_schedules_by_tag

List schedules matching a tag. Use this for introspection (detecting missing heartbeats, duplicates, or stray per-job check-ins) without querying raw tables directly.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tag` | string | Yes | Tag to match |

### Example: Check whether a global heartbeat is present

```json
{
  "process_key": "service_interface::scheduling_service::get_schedules_by_tag",
  "arguments": {
    "tag": "heartbeat:global"
  }
}
```

---

## execute_in_seconds

Schedule one-time direct action execution or a terminal memory read after a
delay. Exactly one execution mode is required.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `seconds` | integer | Yes | Delay in seconds (must be > 0) |
| `action_definitions` | list | One mode required | Non-empty, syntactically valid `{process_key, arguments}` actions |
| `memory_tag` | string | One mode required | Terminal memory read; does not start a model turn |
| `content` | string | No | Content to store before a terminal memory-tag read; invalid in action-definition mode |
| `label` | string | No | Human-readable label |
| `tags` | list | No | Tags for grouping and cancellation |

Content is valid only with `memory_tag`. The plugin validates the complete
mode/content/action request and the scheduled-action policy before any memory or
schedule write. If valid `content` and `memory_tag` are both provided, it then
stashes the instructions as a memory automatically (tagged with `memory_tag`)
before creating the wake-up. If you omit `content`, the system assumes the
memory was already stashed (two-step pattern). Supplying `content` with
`action_definitions` fails with
`default_scheduling_plugin.parameter_error` rather than being ignored.

`execute_in_seconds` rejects inference-bearing scheduled actions before
persistence, using the same scheduled-action validator as recurring
registration and both restoration paths.

Note: the one-step stash uses only `tags=[memory_tag]`. If you need richer tags (for example `session:<id>` or `job:<id>`), use the two-step pattern.

### Returns

| Field | Type | Description |
|-------|------|-------------|
| `schedule_id` | string | Unique schedule identifier |
| `message` | string | Confirmation message |
| `run_at` | string | ISO 8601 timestamp when the wake-up will run |

### Example: One-Time Scheduled Peer Notification

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "label": "One-minute coordinator reminder",
    "action_definitions": [
      {
        "process_key": "plugin::agent_messaging_plugin::peer_send_by_name",
        "arguments": {
          "name": "Coordinator",
          "content": "Scheduled reminder: inspect the pending job."
        }
      }
    ]
  }
}
```

### Example: Terminal Memory Read

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "memory:recency-touch"
  }
}
```

The lookup result stays on the completed action row. This example does not
notify a peer, resume a seat, or start inference.

---

## clear_scheduled_action

Cancel a specific scheduled job.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `schedule_id` | string | Yes | The schedule ID returned by create/execute |

### Example

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_action",
  "arguments": {
    "schedule_id": "sch_abc123"
  }
}
```

---

## clear_scheduled_actions_by_tag

Cancel all scheduled jobs matching a tag. This is the preferred way to clean up recurring schedules.

### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `tag` | string | Yes | Tag used when schedules were created |

### Example

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_actions_by_tag",
  "arguments": {
    "tag": "followup:sess-abc123:tts"
  }
}
```

---

## Common Patterns

### Pattern: Deterministic Progress Check

Schedule the check verb itself in `action_definitions`. If the verb must notify
a peer, either make that notification part of the deterministic verb or schedule
`peer_send_by_name` directly. Do not use `memory_tag` as instructions for a
model; no model receives that result at fire time.

### Pattern: Timeout with Fallback

Schedule the deterministic fallback action in case the primary task takes too
long:

```
Step 1: execute_in_seconds(seconds=300, action_definitions=[{"process_key": "<fallback EDGE/EDGE_SINK verb>", "arguments": {"job_id": "<id>"}}])
Step 2: ... execute primary task ...
Step 3: clear_scheduled_actions_by_tag(tag="followup:timeout:<id>")  // Cancel if task completed in time
```

---

## Schedule Lifecycle

| Status | Description |
|--------|-------------|
| `scheduled` | Awaiting execution |
| `running` | Currently executing |
| `completed` | Successfully completed (one-time only) |
| `cancelled` | Manually cancelled via clear operations |
| `error` | Execution failed |
| `paused` | Temporarily paused |

---

## Important Notes

- All cron expressions use **UTC** timezone
- The `state` parameter is **auto-injected** -- do not set it manually. It carries `session_id` and `flow_id` for proper context routing
- Scheduled wake-ups execute **asynchronously** and independently of the current inference chain
- Provide exactly one of `action_definitions` or `memory_tag`
- `action_definitions` execute directly; `memory_tag` is a terminal read and starts no model turn
- One-time schedules (`execute_in_seconds`) auto-complete after execution
- Recurring schedules (`create_cron_schedule`) continue until explicitly cancelled
- **Always tag your schedules** so they can be bulk-cancelled with `clear_scheduled_actions_by_tag`
- Schedules persist across service restarts
