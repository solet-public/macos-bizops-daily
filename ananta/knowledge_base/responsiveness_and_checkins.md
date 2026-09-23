# Responsiveness, Status Updates, and Check-Ins
Article Layer: 2

Silence feels like failure in chat-based systems. When a request involves tool execution, async jobs, or multi-step work, it is often helpful to keep the requester oriented with short acknowledgements, time expectations, and periodic check-ins.

This guidance is designed for a ReAct-style loop: communicate progress as you observe it, prefer evidence over guessing, and use scheduling when you need follow-ups without blocking.

---

## 1) When To Check In (What To Say)

For user-visible communication (acknowledgements, progress, completion, errors, clarification), resolve the active IO plugin namespace with `query_process_registry`, then invoke `plugin::<namespace>::post_message` with `message` and any namespace-specific arguments from its schema. `SESSION_AWARE` binds the session from context: never pass `session_id`. Keep messages short and concrete.

### Latency Perception Heuristics (HCI)

Humans interpret silence as failure. A simple, practical timing model:

- If you can respond essentially immediately, just answer.
- If you expect meaningful delay (or you are unsure of the ETA), acknowledge quickly and set expectations.
- If the user is likely to wait more than ~10 seconds without any new information, plan for at least one check-in.

You do not need perfect timing. The goal is to avoid long, unexplained silence.

### Situations Where A Check-In Helps

- **Non-trivial latency**: You expect tool execution to take noticeable time (or you don't know the ETA).
- **Async jobs**: The work runs in the background; the user may wait tens of seconds or minutes.
- **Multi-step workflows**: There are distinct milestones the user will care about.
- **Waiting on a dependency**: External service, indexing, retries, backoff.

### What A Good Status Message Contains

- What you are doing (one sentence)
- What will happen next (one sentence)
- A rough ETA *or* when you'll check back (one sentence)

Example snippets (adapt to context):

- "Starting on this now. It may take a minute or two; I'll update you if it's still running after about a minute."
- "Working on it now: generating the audio first, then applying the effect, then delivering the final WAV."
- "Still running (waiting on synthesis to finish). I'll check back in about a minute."
- "Done. Here is the audio."

### Avoid

- Sending a single large message only at the end when the user has been waiting
- Repeating the same check-in too frequently
- Pretending work is happening when it hasn't started
- Putting metadata trailers or embedded JSON into the resolved `plugin::<namespace>::post_message` action's `arguments.message` (the platform appends metadata trailers on persistence)
- Passing `session_id`; the `SESSION_AWARE` action binds it from context

---

## 2) How To Do It (Scheduling Patterns)

Use `service_interface::scheduling_service::*` when you want the platform to execute follow-up actions later (even while other work is running). This is useful for check-ins, heartbeats, and timeouts.

The current architecture is **memory-driven scheduling**:

- Scheduling does **not** embed action definitions in the schedule payload.
- A schedule is a timer + a **memory address** (`memory_tag`).
- When the schedule fires, the platform runs a tag lookup (`memory_service::get_memories_by_tag`) and the result arrives as a normal observation at a `process_results` vertex.
- The model reads the recalled memory and decides what to do next (post a message, check a job, execute a tool, reschedule, or terminate).

This avoids complex nested “scheduled action definitions” and keeps the ReAct loop intact: *observe → decide → act*.

### Naming And Cleanup

Use a **unique `memory_tag`** per follow-up thread (typically based on session/job/flow), e.g.:

- `followup:sess-abc123:tts`
- `followup:flow-abc123`
- `followup:job-job_abc123`

Schedules are typically tagged with the same value so you can cancel them via `clear_scheduled_actions_by_tag(tag=<memory_tag>)`.

Note: `memory_service::remember` creates a new memory record; it does not “upsert/replace by tag”. If you reuse the same `memory_tag`, you may accumulate multiple memories with that tag. Prefer unique tags, or explicitly clean up old tagged memories (for example, delete by tag) before writing a replacement.

### One-Time Check-In (execute_in_seconds)

Preferred (one-step): stash the follow-up instructions and schedule the wake-up in a single call by providing both `content` and `memory_tag`.

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup:sess-abc123:tts",
    "content": "Follow-up: check CosyVoice2 TTS job status. For any user-visible update, resolve the active IO namespace with query_process_registry, then invoke plugin::<namespace>::post_message; SESSION_AWARE binds the session, so never pass session_id. Use attachments or job_result_ref only when that namespace schema declares them. If still processing, send a brief status update and schedule another check in 60 seconds."
  }
}
```

Two-step (more control): if you need richer tags on the follow-up memory, create it yourself via `memory_service::remember` and then schedule a wake-up using only `memory_tag`.

### Conditional Check-Ins (Poll Then Decide)

Memory-driven scheduling naturally supports “don’t spam unless needed”:

- Wake up.
- Check current state (e.g., `job_service::get_latest_job`).
- If still running, decide whether to post a status message.
- Reschedule the next wake-up with the same `memory_tag`.

This keeps user-facing messages conditional on evidence, not hardcoded timers.

### Periodic Heartbeat (create_cron_schedule)

For longer-running work, a heartbeat can keep the user confident the system is alive. Use a cron wake-up against a `memory_tag`, and let the model decide what (if anything) to post each time.

```json
{
  "process_key": "service_interface::scheduling_service::create_cron_schedule",
  "arguments": {
    "cron_expression": "*/2 * * * *",
    "memory_tag": "followup:sess-abc123:heartbeat"
  }
}
```

When the underlying work completes, clear the heartbeat:

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_actions_by_tag",
  "arguments": {
    "tag": "followup:sess-abc123:heartbeat"
  }
}
```

### Timeout With Fallback

If a task might hang or take too long, store a timeout policy in memory and schedule a wake-up for it.

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 300,
    "memory_tag": "followup:sess-abc123:timeout"
  }
}
```

Cleanup when done:

```json
{
  "process_key": "service_interface::scheduling_service::clear_scheduled_actions_by_tag",
  "arguments": {
    "tag": "followup:sess-abc123:timeout"
  }
}
```

### (Optional) Remembering Typical Durations

If you observe reliable timings for specific processes in a specific environment, you can store a lightweight timing note as memory for better future ETAs:

```json
{
  "process_key": "service_interface::memory_service::remember",
  "arguments": {
    "content": "Timing note (2026-02-27): cosyvoice2_tts_plugin::synthesize_speech_from_string took ~70s to produce ~10s WAV on this host. Use ~1-2 minutes as a rough ETA for similar requests."
  }
}
```

Then use `service_interface::memory_service::recall` later to retrieve similar timing notes when estimating.

---

## 3) Variable-Rate Heartbeat (One Global Wake-Up)

If you want the system to feel “alive” while work is in progress, use a single global heartbeat wake-up and vary its cadence based on workload.

Properties:

- **Single schedule**: there should be exactly one heartbeat schedule (tag `heartbeat:global`) at a time.
- **Variable cadence**: speed up when there is in-flight work or a user is waiting; slow down when idle.
- **Model-driven decisions**: the heartbeat wake-up only wakes you; the model decides what (if anything) to post.

Recommended cadence ladder (tune to taste):

- **Busy** (user waiting / async jobs running): every 1-2 minutes
- **Warm** (recent activity, no urgent work): every 5 minutes
- **Idle** (no pending follow-ups): every 15-60 minutes

Avoid thrash: only change cadence when crossing a threshold (hysteresis), not on every tick.

Mechanically, prefer an idempotent primitive like `scheduling_service::ensure_global_heartbeat(cadence_minutes=...)` to create/normalize the single schedule.

---

## 4) Always-On "Wake Up" Loop (Batching Follow-Ups)

If you want the system to feel responsive even during long waits, you can run a lightweight, always-on wake-up loop that periodically wakes up, checks what's in-flight, and decides whether to send a user-visible update.

This can be model-driven (not hardcoded routing) by combining:
- `scheduling_service::execute_in_seconds` to schedule the next wake-up
- a "queue" of follow-ups stored as memories (tagged items describing what's running, what to monitor, and when to check back)

### Bootstrap Policy (Once After First Interaction)

The wake-up loop should be bootstrapped **once after the first user interaction** (or on first interaction after startup), not re-created for every task.

Rationale: if the wake-up loop is global infrastructure, it prevents \"silence feels like failure\" without forcing every individual task to schedule its own follow-up.

Recommended conventions:

- Heartbeat schedule tag: `heartbeat:global`
- Heartbeat wake-up memory_tag: `heartbeat:global`
- Follow-up queue tag: `followup_queue:global`

If the platform exposes `service_interface::scheduling_service::ensure_global_heartbeat`, prefer calling it (idempotent).
Otherwise, create a recurring cron wake-up (or seed a self-rescheduling tick) exactly once, then rely on it.

### Important Constraint (Why Memory-Driven Scheduling Exists)

Nested “schedule these N actions later” payloads tend to produce large schemas and brittle model behavior.
Memory-driven scheduling keeps the schedule payload small and pushes intelligence back into the normal ReAct loop.

### Pattern: Self-Rescheduling Tick

1. **Seed**: schedule an initial tick a short time in the future (e.g., 60 seconds).
2. **Tick action**: list or recall memories tagged as "pending follow-ups" for the current session.
3. **Decision**: if there is nothing pending, schedule the next tick farther out; if there are multiple pending items or long-running jobs, schedule the next tick sooner and optionally schedule a check-in message.

### Example: Seed A Tick That Wakes The Follow-Up Queue

This schedules a wake-up that retrieves the follow-up queue memory (by tag). The lookup result will be presented back to the model at a `process_results` vertex, where it can decide what to do and when to schedule the next tick.

```json
{
  "process_key": "service_interface::scheduling_service::execute_in_seconds",
  "arguments": {
    "seconds": 60,
    "memory_tag": "followup_queue:session:sess-abc123"
  }
}
```

### Suggested Memory Shape (Human-Readable)

Store a short memory per in-flight item, tagged for grouping. Example content:

- "Follow-up: waiting on cosyvoice2 TTS job job-abc123 for output 'samantha_assertion' (started 2026-02-27T13:03Z). If no completion in ~2 minutes, send a brief status update."

Example tags (pick a consistent scheme):
- `followup_queue`
- `session:sess-abc123`
- `job:job-abc123`

This allows a tick to list `tag=followup_queue` and decide what to do.

---

## 5) "Impatience" For Missing User Replies (Gentle Reminders)

If you asked a blocking clarification question and the user goes silent, you can schedule a reminder. This should be low-noise:

- Only remind if the question is blocking and time-sensitive.
- Prefer a single nudge, then back off (do not spam).
- If the user replies, cancel the reminder / clear the follow-up memory.

Implementation pattern:

1. When you ask the user a blocking question, store a follow-up instruction memory tagged for that session (e.g., `followup:sess-abc123:waiting_for_user`) describing:
   - what question you are waiting on
   - when to remind (e.g., 10 minutes)
   - how to back off (e.g., if no reply after 1 hour, stop)
2. Either rely on the global heartbeat to notice it is due, or schedule a one-time wake-up via `execute_in_seconds`.
3. Before reminding, check evidence (for example: session activity stats) so you do not remind after the user has already replied.

### Avoid

- Very frequent ticks when nothing is happening (wastes compute)
- Spamming the user with repetitive check-ins
- Scheduling an unbounded number of reminders without tags/cleanup
