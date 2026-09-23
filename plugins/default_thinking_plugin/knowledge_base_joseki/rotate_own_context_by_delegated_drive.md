# Rotate Own Context (Delegated Drive Withdrawn)

Article Layer: 2

Article Role: joseki_catalog

Article Tags: planning-stage:always, evidence-category:joseki, domain:session-management, domain:platform-operations


JOSEKI_KEY: rotate_own_context_by_delegated_drive
DESCRIPTION: Clear and resume the context of a session that has no manager above it, with NO helper agent in the injection path — that mechanism is withdrawn after two independent, correct refusals. Checkpoints durable state, writes a resume-handoff file, then either the operator types the two lines themselves (the default when they are at the keyboard) or the session runs a deterministic two-injection script detached (when they are not). Also carries the economic question of whether to rotate at all: measured thresholds against a measured post-rotation prefix, not a fraction-of-window tripwire. The key still reads `by_delegated_drive` because it is a routing address, not a claim; the mechanism it names is history. NOT for managed workers: a tmux-hosted worker is rotated by its manager with clear_session plus drive_session.
EMBEDDING_DESCRIPTION: Clear your own conversation context and pick the work back up when you are the top session and nobody else can clear you, and decide first whether clearing is even worth it. Save what is in flight to a handoff file, then either have the operator type the clear command and a short pickup prompt themselves, or run a small script that types exactly those two things and nothing else. Do not send another agent to do it — that was tried and correctly refused twice, because a spawned worker typing into the operator's own seat is acting on consent it cannot verify. Covers the measured rotation thresholds, the cost of the context a clear re-writes, and the injection races: a script can type into the operator's live composer, and a clear can swallow a queued pickup prompt. For a session with a manager above it, use the worker rotation verbs instead.

## Withdrawn: the delegated helper drive

An earlier version of this card dispatched a helper agent to drive the seat's
terminal. **That path is withdrawn.** It was refused twice, and both refusals
were correct:

- **2026-08-15** — a dispatched helper declined, on the grounds that a spawned
  worker injecting keystrokes into the operator-present seat is a seat-native
  capability act, and that the operator's authorization for it, having reached
  the helper *relayed through the seat*, is evidence the helper cannot verify.
- **2026-08-16T16:02:49Z** — `lane-rotate-helper-0816` read its brief and this
  card, found the 08-15 refusal already recorded, and stopped without touching
  the pane, adding that its brief conflicted with the more recent
  deterministic-script resolution. Ratified; helper terminated 16:04Z.

A well-aligned worker *should* refuse an unverifiable consent claim, so this is
not a briefing defect to be written around. The defect was putting an agent
with judgment into the injection path at all. The 2026-08-13 helper rotation
did succeed, and that is not a counter-argument: one success against a
principled refusal is not a working mechanism, it is a mechanism that happened
not to be tested.

The JOSEKI_KEY still reads `rotate_own_context_by_delegated_drive`. A key is an
address, not a promise; renaming it would strand every reference for no gain.
Read the name as a tombstone.

## Input Contract

- A session whose context must be cleared and which no other session manages
- A measured context reading (`session_context_status`), and whether the prompt
  cache is warm — the rotation decision is economic before it is mechanical
- Whether the operator is at the keyboard; this selects the mechanism
- A drained memory projection and current workbench records — the clear
  destroys everything not written down
- Bindings: target_surface_selector, handoff_path, pickup_prompt
  (`helper_model` is REMOVED — there is no helper)

## Output Contract

- A decision, on measured numbers, that rotating is economic right now
- A resume-handoff file naming what is in flight, the read order, and the
  single first action
- A cleared session whose durable identity and role binding are intact
  (neither is re-minted by a clear)
- The fresh context VERIFIED to be actively processing the pickup prompt, not
  idle

## Sequence

[ ] 1. Decide whether to rotate at all, on measured numbers
    a) Read occupancy with `plugin::agent_messaging_plugin::session_context_status`; on `resolved: false`, stop and report the error verbatim rather than estimating [agent-executed: an unmeasurable gauge is not a low reading]
    b) Apply the economic policy — WARM cache: under 150K keep working; 150-200K rotate at the next natural task boundary; over 200K at the first safe checkpoint; over 300K immediately, finishing only the in-flight tool action. COLD cache (over 60 min idle, or over 5 min while in usage overage): rotate whenever context exceeds H [agent-executed: the unit is the MODEL CALL, not the operator prompt]

[ ] 2. Reach a safe checkpoint before anything is destroyed
    a) Drain pending memory write-through, bring workbench records current, and confirm no peer is holding for a go-signal only this session can send [agent-executed: the clear is irreversible for anything held only in context]

[ ] 3. Write the resume handoff file
    a) Record what is in flight, the read order for a fresh context, and the ONE first action [agent-executed: a workbench file, which never ships]

[ ] 4. Confirm the operator's consent directly, to this session
    a) Consent relayed through a third party is exactly what the helpers refused to act on; a script cannot verify it either — it simply cannot notice, which is why the check lives here [agent-executed: this is the step the withdrawn mechanism could not perform]

[ ] 5. Select the mechanism by whether the operator is at the keyboard
    RESULT_PROCESSOR_KIND: deterministic_continuation
    a) Operator PRESENT — the operator types the two lines themselves: zero race, zero extra tokens, no capability question. This is the default [agent-executed: on 2026-08-16 a detached script's injected clear landed inside the operator's live composer]
    b) Operator ABSENT — run the deterministic two-injection script, DETACHED [agent-executed: a foreground run deadlocks, because the session's own turn must end before the queued clear can execute]

[ ] 6. The script resolves the target surface under a 0/1/N gate
    a) Match the target by its role variable via the terminal's scripting API; refuse loudly on zero or multiple matches [agent-executed: refusal BY CONSTRUCTION, the only kind that is deterministic — an ambiguous match must never be guessed, the wrong pane gets cleared]

[ ] 7. The script injects the clear, submitting as separate sends
    a) Capture any non-empty composer content from the screen first (relay it, never silently discard it), then send a space, then a kill-line control (C-u), then the literal clear command, then its carriage return, then a SECOND carriage return, with gaps between sends [agent-executed: a leading bare return is BANNED — measured 2026-08-28 SUBMITTING a stranded remote-control draft as a prompt; the space suppresses ghost-suggestion text and realizes composer state, and the kill-line empties all real content, which is why the capture precedes it; a newline inside the text does not submit; and a slash command's first return can be consumed by its autocomplete menu as "accept selection" — a single return after the command was measured (2026-08-27) leaving it accepted but unsubmitted in the composer]

[ ] 8. The script settles, then injects the pickup prompt
    a) Wait for the turn to end and the clear to execute before sending the pickup, then send its carriage return separately [agent-executed: sending both together lets the clear swallow the queued pickup — and the wait must OBSERVE the executed clear, never trust a timer: a blind settle sleep was measured twice (2026-08-27/28) typing the pickup while the clear was still pending, so the pane committed the pickup first and the clear then wiped it]

[ ] 9. Verify the fresh session is PROCESSING, and record the rotation
    a) Confirm the fresh context is actively working the pickup prompt, not merely cleared; if deaf, the operator retypes it [agent-executed: cleared-and-idle is a failure, not a success — and with no helper there is no transcript to capture, so the fresh session records the rotation itself]

## Expected Step Count

9 steps.

## Binding Guidance

- There is no `helper_model` binding. If a brief still asks for one, the brief
  predates the withdrawal and should be corrected, not satisfied.
- Bind `pickup_prompt` SHORT and make it point at `handoff_path` rather than
  carrying the state inline. A long injection collapses into a paste chip and
  may not submit as typed.
- Bind `target_surface_selector` to a role-tagged variable rather than a
  session id captured earlier — ids go stale, and the 0/1/N gate is what makes
  a stale or duplicated match refuse instead of clearing the wrong pane.
- Bind `handoff_path` under `workbench/`, which never ships in a seed bundle,
  and put nothing in it that identifies a person, an employer, or a credential.
- The two-injection mechanics SHIP: `agent_messaging_plugin`'s
  `seat_rotation_helper.py` is the deterministic console one-shot — live
  `user.role` pane resolution under a 0/1/N gate, the capture-then-clear leg
  of step 7 (composer capture into the envelope, then space, kill-line, the
  clear command, return, return), a poll-settle on a POSITIVE cleared-state
  signature, then the pickup and its own return.
  Prefer it. Deliberately not a `@platform_process` verb: it must outlive the
  seat's own turn, which a synchronous verb call cannot. One measured limit
  of the positive signature (2026-08-28): the bare empty-composer prompt
  glyph also renders on a MID-TURN screen, so the signature only proves the
  clear on a pane that was IDLE when the clear was sent — fire rotation at a
  turn boundary, never from inside a running turn.
- **What ships is the CONTRACT, not the host driver.** That module holds the
  `iterm2` bindings as optional — absent bindings are recorded at import and
  re-raised only at the one step that actually drives a pane — because the
  distribution arrives with `iterm2_coding_agent_management_plugin`, which the
  `macos-bizops` profile excludes. So an adopter on a headless box inherits the
  ordering contract and the gates, and must supply a host driver for their own
  surface, or use the operator-present path. Cite the shipping module by its
  process surface; a checkout-local script is never the thing to hand an
  adopter, and one that used to sit in `workbench/` has since been deleted
  precisely because a card citing it went dead in a clone without saying so.

## Coherence Obligations

- **Rotating is a decision before it is a procedure.** H — the post-rotation
  prefix a clear re-writes — is MEASURED at 110,702 tokens: boot payload 42,873
  plus incremental rehydration 67,829. The boot payload is re-paid on every
  clear, so it belongs inside H rather than beside it. Break-even carries the
  1-hour-TTL cache-WRITE premium, since a clear re-writes the whole prefix at
  roughly 2x base input: clearing wins when `C > H + 20H/N`.
- **The superseded 0.6-of-window tripwire and the "~80k token-equivalents per
  prompt" arithmetic are WITHDRAWN.** They priced carriage per PROMPT when the
  billable unit is the model CALL, and they leaned on an estimated H that
  measurement put roughly 50% low.
- **The policy binds only if the session SELF-CHECKS.** A threshold nobody
  reads is not a threshold. Check at every landing relay and task boundary. The
  operator noticing first is a policy failure, not a save — on 2026-08-16 a
  seat rotated at 559K against a >300K rule, operator-flagged.
- **No agent in the injection path.** Not a workaround, a ruling. A session
  cannot clear itself directly, so the mechanism uses either the operator's own
  hands or a script with no capacity to decide anything.
- **The clear destroys everything held only in context.** Steps 2-3 are not
  formalities: anything not drained to the memory store or written to workbench
  is gone, and a checkpoint skipped here surfaces as a fresh session
  confidently redoing landed work.
- **Cleared is not resumed.** A cleared interactive session sits idle until
  someone types a turn, with no error anywhere — so a rotation that stops after
  the clear stalls silently and indefinitely. The pickup IS that turn, and
  step 9 verifies PROCESSING rather than presence.
- **The clear can consume its own follow-up.** Settling between the two
  injections is what prevents it; without the wait, executing the clear
  swallows the queued pickup and leaves a deaf session.
- **A script can race the operator's own keystrokes.** The 2026-08-16 composer
  incident is why the script path is operator-ABSENT only.
- **Identity survives; do not re-claim reflexively.** A clear does not mint a
  new instance id and does not release a held role. Measure identity after the
  clear if it matters, but a reflexive re-registration is itself an eviction
  risk.
- **Never claim self-clearing is impossible.** That claim has been made and
  been wrong. What is impossible is delegating it to something that can decide.

## Next Joseki

Determined by the handoff file's stated first action — the point of the
rotation is that the work continues, so the fresh context resumes the
programme it was in rather than starting a new one.

## Repair Joseki

Explicitly absent as a card. Each step fails in place and is re-runnable: an
ambiguous surface match refuses before touching anything, a clear that did not
take is re-injected after the queue drains, and a deaf fresh session is
recovered by one re-typed pickup. The only unrecoverable failure is a skipped
checkpoint, and its repair is not a procedure — it is reconstructing lost state
from the workbench and the memory store.
