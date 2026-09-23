# Memory-Driven Scheduling (Wake-Ups, Not Payloads)
Article Layer: 1

This platform uses **memory-driven scheduling** for model-orchestrated follow-ups.

Instead of embedding action payloads inside a schedule, the scheduler stores only:

- **when** to wake up (`seconds` or `cron_expression`)
- **what memory to wake up** (`memory_tag`)

When the schedule fires, the platform retrieves memories tagged with `memory_tag` (via `memory_service::get_memories_by_tag`) and the model decides what to do next in the normal ReAct loop.

---

## Core Pattern

1. **Stash follow-up intent**: Store a short follow-up instruction as a memory addressed by `memory_tag`.
2. **Schedule a wake-up**: Create a wake-up that references the same `memory_tag`.
3. **Wake-Up Vertex**: When the wake-up fires, the recalled memory arrives as an observation at `process_results`.
4. **Decide**: The model chooses the next action (post a status update, check a job, execute a tool, reschedule, or terminate).

This keeps scheduling payloads small and avoids brittle nested schemas.

---

## Global Heartbeat (Always-On Liveness)

For a responsive assistant, it is often useful to have a **single global heartbeat** wake-up that keeps the system "awake" even when no user is actively chatting.

Key points:

- The heartbeat is **not bound to any one session**. If it stops, the assistant is effectively asleep.
- The heartbeat should be **low-noise**: wake up, observe queued follow-ups / job state, and decide whether any user-visible check-in is warranted.

Recommended conventions:

- Heartbeat schedule tag: `heartbeat:global`
- Heartbeat wake-up `memory_tag`: `heartbeat:global`
- Follow-up queue tag: `followup_queue:global`

Bootstrap policy:

- Start the global heartbeat **once after the first user interaction** (or on the first interaction after startup).
- If it falls off, the next user interaction can re-seed it.

If the platform exposes `service_interface::scheduling_service::ensure_global_heartbeat`, prefer calling that (idempotent).

### Variable Rate (Busy vs. Idle)

The global heartbeat does not need to run at a fixed cadence. A practical pattern is a small cadence ladder:

- Busy (in-flight jobs / user waiting): 1-2 minutes
- Warm (recent activity): 5 minutes
- Idle (nothing pending): 15-60 minutes

To keep the system simple and avoid duplicate heartbeats, adjust cadence by updating the *single* heartbeat schedule (rather than creating multiple competing schedules). An idempotent helper like `ensure_global_heartbeat(cadence_minutes=...)` is the preferred way to do this.

Otherwise, you can build the heartbeat with existing primitives:

1. Create/replace a canonical instruction memory:
   - `memory_service::upsert_memory_by_tag(tag="heartbeat:global", content="...")` (recommended), or
   - `memory_service::delete_memories_by_tag(tag="heartbeat:global")` then `memory_service::remember(..., tags=["heartbeat:global"])`
2. Create a cron wake-up:
   - `scheduling_service::create_cron_schedule(cron_expression="* * * * *", memory_tag="heartbeat:global", tags=["heartbeat:global"])`

The heart of the system remains model-driven: the schedule only wakes you up; the model decides what to do next.

---

## One-Step vs. Two-Step

There are two supported ways to stash follow-up intent:

### One-Step (Preferred)

Call `execute_in_seconds` with `seconds`, `memory_tag`, and `content`.

- The scheduling plugin stashes `content` as a memory tagged with `memory_tag`.
- Then it schedules the wake-up keyed by `memory_tag`.

This reduces tool calls and keeps the model’s output schema flat.

Limitations:

- The one-step stash uses only `tags=[memory_tag]`. If you need richer tags (for example `session:<id>`, `job:<id>`), use the two-step pattern.

### Two-Step (More Control)

Call `memory_service::remember` yourself (with any tags you want), then schedule a wake-up using only `memory_tag`.

Note: `memory_service::remember` does not replace by tag; reusing a tag can create multiple memories. Prefer unique tags, or explicitly delete old tagged memories before writing a replacement.

---

## Example: Async Job Check-In

### One-Step

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup:job-job_abc123",
    "content": "Follow-up: check job status for job_abc123. For any user-visible update, resolve the active IO namespace with query_process_registry, then invoke plugin::<namespace>::post_message; SESSION_AWARE binds the session, so never pass session_id. Use attachments or job_result_ref only when that namespace schema declares them. If still processing, send a brief status update and schedule another check in 60 seconds."
  }
}
```

### Two-Step

```json
{
  "process_key": "service_interface::memory_service::remember",
  "arguments": {
    "content": "Follow-up: check job status for job_abc123. For any user-visible update, resolve the active IO namespace with query_process_registry, then invoke plugin::<namespace>::post_message; SESSION_AWARE binds the session, so never pass session_id. Use attachments or job_result_ref only when that namespace schema declares them. If still processing, send a brief status update and schedule another check in 60 seconds.",
    "tags": ["followup:job-job_abc123", "job:job_abc123", "session:sess-abc123"]
  }
}
```

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup:job-job_abc123"
  }
}
```

---

## Naming And Cleanup

Use a **unique `memory_tag` per follow-up thread**, typically derived from stable identifiers:

- `followup:sess-<session_id>:<topic>`
- `followup:flow-<flow_id>`
- `followup:job-<job_id>`

Schedules are typically tagged with the same value, so cleanup is straightforward:

- Cancel wake-ups: `clear_scheduled_actions_by_tag(tag=<memory_tag>)`
- Clean up stale instruction memory: delete/forget memories with that tag when no longer needed

---

## What Not To Do

- Do not embed large action payloads in schedules for model-driven follow-ups.
- Do not spam the user with frequent, repetitive check-ins; use wake-ups to *observe state*, then decide whether a message is warranted.
