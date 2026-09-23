# Timing Heuristics (Human-Like Latency, Check-Ins, and Cadence)
Article Layer: 2

This is a pragmatic reference for choosing *when* to send acknowledgements, status updates, reminders, and heartbeat cadences.

These are heuristics, not rigid rules. Prefer evidence (observed job state, known timings) over guessing.

---

## Interaction Latency (HCI)

In interactive systems, people quickly assume silence means failure. A useful mental model:

- **Immediate**: if you can respond essentially right away, just answer.
- **Noticeable delay**: if you expect a non-trivial delay (or do not know the ETA), acknowledge quickly and set expectations.
- **Attention drift**: beyond roughly ~10 seconds without any new information, users often disengage. Plan at least one check-in for long-running work.

In a tool-driven assistant, this typically means:

- Send an acknowledgement when starting work that may take time.
- For async jobs, send a brief "still running" update if the job is still not complete after a meaningful interval.

---

## Check-In Scheduling (Mechanical Baseline)

### Async job polling (example ladder)

Use an escalating backoff to avoid spam:

- First check: ~30-60s after enqueue (many jobs finish quickly)
- If still running: next check in 60s
- If still running: next check in 2m
- If still running: next check in 5m (and/or ask whether to keep waiting)

Only message the user when:

- There is new information (completed, error, meaningful milestone), or
- The user is likely to wonder whether the system is stuck (time-based threshold), or
- The ETA expectation has changed.

### Waiting-for-user reminders ("impatience")

If you asked a blocking question:

- First nudge: 10-30 minutes (depending on urgency and channel norms)
- Second nudge: 2-24 hours (only if it is genuinely important)
- Then stop, or explicitly say you will wait for their reply.

Before nudging, check whether the user has replied recently.

---

## Variable-Rate Heartbeat (One Global Wake-Up)

A single global heartbeat helps liveness and responsiveness. A cadence ladder is usually enough:

- **Busy** (in-flight work / user waiting): every 1-2 minutes
- **Warm** (recent activity): every 5 minutes
- **Idle** (nothing pending): every 15-60 minutes

Use hysteresis to avoid thrashing:

- Only speed up when urgent work appears (or a user is waiting).
- Only slow down after a sustained idle window (for example: 2-3 consecutive idle ticks).

The heartbeat wake-up should be low-noise: wake, observe queued follow-ups, decide whether a user-visible message is warranted, and adjust cadence as needed.

---

## Learning Timing From Experience

If your environment has consistent performance characteristics, store timing notes as memory. Use them for better ETAs and check-in intervals. Keep them lightweight:

- process key + rough duration + hardware/context
- a suggested ETA range
- a suggested first check-in time

Example:

- "CosyVoice2 TTS: ~70s to produce ~10s WAV on this host. Use ETA 1-2 minutes. First check-in at 60s."

