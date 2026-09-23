Tags: seat, running_log, handoff, obligations, rotation, authorizations, coordinator, git_controller, durable_knowledge

Article Layer: 2

Embedding Description: What a seat's running log must contain across a rotation or handoff — the required section shape, and specifically the OBLIGATIONS slot for authorizations, rulings, and retirements a seat owes another party, written the moment the obligation is incurred rather than reconstructed at handoff time.

# Seat Running-Log Convention

A seat (a primary or driving session, a coordinator, Git-Controller, a long-running lane) keeps a running log across its own lifetime: a single growing file under `workbench/`, newest entry last, that lets the seat itself — and whichever session inherits the role after a rotation, restart, or `/clear` — reconstruct what happened without replaying the full conversation transcript.

**Before this article existed, this convention was transmitted only by example.** No durable article specified what a running log must contain; each seat wrote one because the previous seat's log looked a certain way, and the shape drifted informally from instance to instance. That gap was discovered measured (R1, 2026-08-17, below) rather than assumed — a repo-wide search for "running log" outside `workbench/` returned zero hits in `ananta/knowledge_bases/`, `plugins/*/knowledge_base/`, or `knowledge_bases/`. This article is the fix: the durable surface a fresh seat can read instead of inferring the shape from a predecessor's file.

---

## 1) Required sections

A seat running log is a single Markdown file, one per boot/handoff cycle (a fresh file per major version, named for the seat and the version it covers), with these sections, newest content last within each:

- **Boot cycle** — identity verified, hydration state, inbox state at boot, live peers/lanes and their briefs.
- **Measured findings** — anything the seat confirmed by evidence during this cycle (a wrong prior belief corrected, a defect reproduced, a design verified against the running system). Distinguish measured from assumed; mark assumptions as assumptions.
- **Landings** — what merged, by branch/commit/merge SHA, verified by ancestry (`git merge-base --is-ancestor`) and by content, not by a report alone.
- **OBLIGATIONS** — see §2. This is the slot that was missing before this article.
- **NEXT ACTION** — the concrete next step, for the seat's own continuation or for whoever inherits the role.

A section with nothing to report in a given cycle is simply omitted for that cycle, not filled with placeholder text.

## 2) The OBLIGATIONS slot

**Authorizations owed, rulings owed, retirements owed — written at CREATION time, the moment the obligation is incurred, not reconstructed at handoff time.**

The creation-time requirement is the entire point. An obligation recorded only when a seat is about to rotate or hand off is the same failure the log is meant to prevent, with one extra step: the seat still has to remember, under handoff pressure, everything it has come to owe since boot. A log that only records obligations issued (what the seat has already sent, decided, or granted) has no way to represent an obligation that exists but hasn't been acted on yet — there is nothing to omit from a handoff that was never written down as pending.

Write an OBLIGATIONS entry the instant the obligation exists, before doing anything else with it:

- **Authorization owed** — the seat has told another party (a peer, Git-Controller) that it will send a first-party authorization, but has not sent it yet. Record: what the authorization is for, who is owed, and the arm/message id once sent (which retires the entry).
- **Ruling owed** — the seat has been asked a question that requires its decision, and has not yet answered. Record: who asked, what the question is, by when if there's a deadline.
- **Retirement owed** — the seat has committed to closing something out (retiring a stale binding, releasing a held resource, tearing down a transient grant) and has not yet done so.

Each entry is retired in place (struck through or moved to a "retired this cycle" line) when fulfilled, not deleted silently — the retired trail is itself evidence that nothing was dropped on the floor.

### Motivating incident (R1, measured 2026-08-17)

`lane-release-pointer` reached gate-green with a commit request at Git-Controller at 03:53Z. The predecessor seat's approval for that request existed only as an in-conversation act — never sent as a first-party message Git-Controller could read in its own inbox. Git-Controller correctly refused the lane's relayed quotation of that approval (a relayed assent is never a substitute for a first-party authorization — see the git-controller-commit skill's pre-authorization exception). The seat then rotated without ever sending the authorization it owed. The lane escalated twice and eventually handed off with the work still blocked. The authorization was finally sent roughly **8 hours 20 minutes** after the work went gate-green, by the *next* seat, once it reconstructed what was owed.

The v9/v10 running logs in use at the time had a section for authorizations the seat had **issued** — a record of what it had done — and no section for authorizations the seat still **owed** — a record of what it still had to do. The seat could account for its past actions but had nowhere to carry an open commitment forward. The OBLIGATIONS slot above is the fix: a pending authorization is seat state, and seat state that lives only in a conversation dies with the session holding it.

### A worked example, the same day the slot was specified

On 2026-08-17, within hours of this slot being written, the same wave that
specified it produced its own instances of it — recorded at creation time,
not reconstructed after the fact:

- The seat deferred restarting a long-lived Git-Controller process (the R6
  incident above involves the same seat) rather than interrupt a live
  landing wave with a kill-and-relaunch. The deferral itself was written
  down as a retirement owed the moment it was decided, in the seat's own
  words: "I have recorded it in the seat's OBLIGATIONS as a retirement owed
  so it does not evaporate at my next rotation."
- A platform design (a new queue, built off this same slot's mechanical
  counterpart — see §4) was sent for a go/no-go and the requesting lane held
  rather than building ahead of an answer, rather than silently assuming
  approval. That is the OBLIGATIONS discipline applied at *design* grain,
  not only at *authorization* grain: state what you are waiting on, at the
  moment you start waiting, not when someone later asks where it went.

Neither entry was hypothetical or written up in hindsight. Both were on the
record as pending before they were resolved — which is the one test this
slot exists to pass, and the reason a slot for authorizations *issued* is
not the same slot as one for authorizations *owed*.

## 3) Why a log instance is not the convention

A seat's running-log file under `workbench/`, and every predecessor of it, is an **instance** of this convention, not the convention itself. Reading a predecessor's log tells a fresh seat what that predecessor happened to record; it does not tell the seat what it is *required* to record, and a section a predecessor omitted (because nothing happened to fall in it that cycle) reads, to an example-only reader, as a section that doesn't exist. This article is the durable reference; the workbench files remain the running record.

## 4) Related conventions

- `coordinator_dispatch_discipline.md` — the analogous discipline for a coordinator's outbound dispatches (declare `expected_path` / `expected_completion_signal` at dispatch time, not reconstructed at watchdog time). The OBLIGATIONS slot is the seat-level counterpart: declare what you owe at the moment you come to owe it.
- The Git-Controller commit procedure's peer pre-authorization exception — the consumer-side rule that makes a first-party authorization load-bearing: Git-Controller acts only on an authorization it holds directly in its own inbox, never on a peer's citation of one. An OBLIGATIONS entry for "authorization owed" exists precisely because of this rule — the obligation isn't discharged until the first-party message actually lands.
