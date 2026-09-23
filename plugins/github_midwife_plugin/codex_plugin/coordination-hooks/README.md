# coordination-hooks for Codex

This is the stock-Codex sibling of the Claude coordination plugin. The current
increment contains:

- fixed `SessionStart` context reminders;
- the WS-4b.1 `PreToolUse` Bash Git-Controller gate;
- `context_status_reporter.py`, a background `Stop` reporter for native Codex
  context measurements;
- `inbox_consumer.py` (CDX-06, 2026-08-24), a SYNCHRONOUS `Stop` hook that
  checks the session's peer inbox for anything already queued and, if so,
  forces the turn to continue with a fixed nudge; plus its own honesty
  attestation, `report_inbox_consumption`.

The context reporter is bound with `"async": true`, as defined by the current
official Codex hook contract. Live stock-process execution remains a separate
acceptance leg. It reads the host-provided transcript, requires the hook
session/model to agree with native transcript identity, and uses only the latest
positive `token_count` after the most recent `context_compacted` boundary. It
reports the runtime's native provider, model, effort, effective context ceiling,
input tokens, cache counters, and source timestamp through
`report_context_status`. Immediately after compaction, when no positive
post-boundary reading exists yet, it deliberately reports nothing instead of
replaying a stale pre-compaction value.

## The peer-inbox consumer (CDX-06)

Root cause (the project backlog's CDX-06 entry): stock Codex had NO consumer
for the peer-inbox wake event at all — `peer_send` reported success
(`delivery=queued_watcher`) and the pane never turned. Measured live against
the actual codex-0.149.0 binary this fleet runs (isolated proof, a
dev-checkout workbench evidence report, not part of this shipped bundle):
an **async** Stop hook's `decision` output is discarded — Codex does not
wait for it, so an
async hook can report telemetry but can never gate or continue a turn. A
**synchronous** hook returning `{"decision": "block", "reason": "..."}` DOES
force Codex to continue the turn, feeding `reason` back as the next input —
confirmed live, nine consecutive forced continuations in one test run. So
`inbox_consumer.py` runs synchronously; `context_status_reporter.py` stays
async because it only reports and never needs to gate anything.

`inbox_consumer.py` uses `<solet-name> wake --max-wait 0` to observe only its
already-armed sidecar spool. The paired eight-second Stop registration covers
the two-second wake and report children plus startup margin. A pending result
continues the model turn; empty is reported as an empty observation; timeout,
exception, malformed output, and non-contract exits are unknown and do not
refresh the observation. Future delivery belongs to native qualified driving
and bounded reconciliation, not a parked Stop hook. The model reads actual
message content only in its continued turn.

Because this hook observes it, the wake-hook spool is RE-ARMED for Codex:
the hydration-rendered launcher no longer passes `--no-spool` to `<name>
watch`. (It was disabled by codex-0147-dead-spool-retirement, 2026-08-13,
specifically because nothing consumed it then — that premise no longer
holds.) A `spawn_session`-managed worker also still gets
`drive_on_delivery`'s driver-channel notice, independent of this plugin's
hooks entirely, for turn initiation while idle.

`report_inbox_consumption` is called after each known pending or empty
observation — never for unknown — so its `checked_at` remains an honest
successful-check timestamp. `session_inbox_consumption_status` reads it back with the same
`resolved=False`-never-defaulted-true contract `session_context_status`
established: a session whose consumer has never run is loudly distinguishable
from one that ran and found nothing, which is distinguishable from one that
ran and found something — so a `queued_watcher` delivery can never again be
mistaken for a delivery that was actually consumed.

An operator-launched (non-fleet) Codex session or a `host=operator` seat still
gets no per-turn peer-delivery notice from this hook (it is armed only when
`AGENT_INSTANCE_ID`/`AGENT_SESSION_ID`/`AGENT_WAKE_CLI` are all present —
expected, not an error): delivery remains durable but silent, the operator
drains `peer_inbox` themselves on their own next user/model turn, and the
`SessionStart` unread-coordination reminder above only fires at
startup/resume/`clear`, not per-turn.

The sibling hydration package now renders the stock-Codex launcher-owned
watcher, repo marketplace, and exact durable-inbox/paging instructions. Those
artifacts are outside this hook bundle so hydration can render deployment names
and paths without changing the plugin bytes. Their externally addressed live
acceptance, lifecycle matrix, and the patched-binary cutover remain WS-4b.4 and
WS-4b.6 retirement gates. Trust revocation is delivered as an explicit operator
action in Codex's supported `/hooks` surface; this package deliberately does
not rewrite Codex's private trust state behind that review boundary.

## Hook inventory

| event | script | behavior |
|---|---|---|
| `SessionStart` (`startup`, `resume`, `clear`) | `step_zero_reminder.js` | fixed project-orientation reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `check_messages_reminder.js` | fixed unread-coordination reminder |
| `SessionStart` (`startup`, `resume`, `clear`) | `role_binding_reminder.js` | fixed label-versus-role reminder |
| `PreToolUse` (`Bash` only) | `git_controller_gate.py` | opt-in Git-Controller mistake-prevention gate |
| `Stop` (background) | `context_status_reporter.py` | report the latest positive native context reading after the last compaction boundary |
| `Stop` (synchronous) | `inbox_consumer.py` | observe the already-queued wake spool; block/continue on pending; report known pending/empty observations only |

**Cadence ruling (2026-08-11):** `step_zero_reminder.js` and
`check_messages_reminder.js` moved from `UserPromptSubmit` to
`SessionStart` (`startup`, `resume`, `clear`) — both used to re-fire on
every prompt turn, accumulating one copy per turn in the transcript. This
changes CADENCE ONLY: the always-armed ruling for `step_zero_reminder.js`
below is unchanged and still enforced by `tests/manifest_consistency_smoke.py`'s
`EXPECTED` inventory (re-adding either reminder to a `UserPromptSubmit`
binding reds that check) and `tests/reminder_hooks_smoke.py`'s
`check_step_zero_fires_everywhere`.

**Tag-echo fix (2026-08-11, §41):** the cadence move initially shipped with
`step_zero_reminder.js` still hardcoding `"hookEventName":
"UserPromptSubmit"` — a host that validates the declared event name against
the firing event silently discards the output, so the reminder never landed
after the rebinding. `step_zero_reminder.js` now echoes stdin's
`hook_event_name` (two-value allowlist, like `check_messages_reminder.js`),
and `tests/reminder_hooks_smoke.py`'s `check_manifest_bound_events_echo`
derives each reminder's expected events from `hooks.json` itself, so a
hardcoded tag can never silently desync from the wiring again.

`step_zero_reminder.js` is unconditionally armed — installed means armed,
with no environment condition (§7 re-key, 2026-08-02; parity with the Claude
sibling's `2fb49dbf2`). `check_messages_reminder.js` keys on
`AGENT_SESSION_ID` — identity, not label, since the inbox it points at is
keyed on identity; the role-binding reminder keys on `AGENT_SESSION_LABEL`,
because the label is its content. Their `additionalContext` is compiled into
the scripts and is independent of the prompt, hook payload, role, label,
inbox state, and message content.

Every `additionalContext` string in this plugin is byte-fixed and generic.
Deployment-specific commands, process keys, and local tool guidance belong in
the project's native `AGENTS.md` instruction surface, which Codex loads without
introducing a second environment-authored prompt channel.

The `context_status_reporter.py` `Stop` binding remains background-only;
removing its `async` flag is a manifest-contract failure the smoke pins
(`context_status_reporter.py` is the ONLY entry required to carry
`"async": true` — every other `Stop` entry, including `inbox_consumer.py`,
must carry no `async` key at all). `inbox_consumer.py` is the deliberate
exception to "a synchronous hook would block the turn boundary": it has an
eight-second paired registration and never waits for future delivery.
Autonomous Codex workers also still get turn initiation
through `spawn_session`'s `drive_on_delivery`, which uses the host driver
channel and does not depend on either `Stop` hook. `inbox_consumer.py` arms
on the same three-variable fleet precondition
`context_status_reporter.py` already uses
(`AGENT_INSTANCE_ID`/`AGENT_SESSION_ID`/`AGENT_WAKE_CLI` all present); this
package does not itself assert which launch paths satisfy that precondition
today, only that a session which does gets the check and a session which
does not gets a clean no-op. Where it IS armed, the hook only ever nudges —
it never drains message CONTENT itself, the same discipline as the Claude
sibling's `wake_waiter.py`; the model (or the operator, on an interactive
session) still reads `peer_inbox` explicitly.

The Bash gate is separately opt-in through `GIT_CONTROLLER_NAME`. It authorizes
only from `AGENT_ROLE`; labels, session IDs, hook thread IDs, and runner identity
cannot grant controller authority. Native edit and delegation tools stay
unrouted until their live stock-Codex payloads are captured and reviewed.

## Repo-marketplace install and trust

The repository marketplace is `.agents/plugins/marketplace.json` at the clone
root. Its local source points directly to this directory, so the reviewed source
and marketplace source are one tree.

From this checkout, using the supported stock binary during validation:

```sh
/opt/homebrew/bin/codex plugin marketplace add /Users/alice/Workspace/solet --json
/opt/homebrew/bin/codex plugin add coordination-hooks@solet-development --json
```

Installation and enablement do not grant hook execution. Start a fresh stock
Codex process, open `/hooks`, inspect each command, matcher, and timeout, then
trust only the expected definitions. New or changed definitions remain inactive
until reviewed. Do not copy another host's `trusted_hash` values.

Use the same supported `/hooks` browser to revoke execution of one definition:
select its event, select the definition, and press Space or Enter so it is
unchecked. Stock Codex persists that definition as disabled without deleting
its reviewed hash, leaves unrelated definitions unchanged, and reports the
revoked definition as installed but inactive. Re-enable it only through the
same reviewed surface.

The seed lane must render its own repo marketplace identity and preserve the
same plugin bytes. This development marketplace does not substitute for that
seed integration or for the later installed-cache equality proof.

## Update/cachebuster procedure

Only the live Git-Controller may mutate files in the shared solet checkout. For a
reviewed local development update, that session runs the Codex plugin-creator
cachebuster helper against this plugin, reviews the resulting single version
suffix, and reinstalls from the existing marketplace:

```sh
python3 ~/.codex/skills/.system/plugin-creator/scripts/update_plugin_cachebuster.py \
  /Users/alice/Workspace/solet/plugins/github_midwife_plugin/codex_plugin/coordination-hooks
python3 deployment/scripts/codex_coordination_hooks_safe_install.py \
  --marketplace solet-development --bridge /Users/alice/.local/bin/solet-bridge
```

Do not run bare `codex plugin add` for this plugin while managed Codex lanes
are live. The safe-install wrapper queries the managed-session ledger, snapshots
the versioned cache, runs the stock install, and restores pruned prior versions
when a live Codex lane exists. This replaces the compat-symlink repair for
`iss_9959c013` and `iss_eb36b76d`; its report records every restored version.

The helper preserves the base version and replaces, rather than stacks, one
`+codex.<cachebuster>` suffix. Re-open `/hooks`: only definitions whose
normalized content changed should require fresh trust. A new Codex thread is the
pickup boundary after reinstall.

Do not hand-edit the marketplace during this update loop. Do not treat plugin
removal as trust revocation. Measured on stock Codex 0.141.0, uninstall keeps the
definition state: an enabled definition automatically becomes active after an
identical reinstall, while a definition disabled through `/hooks` remains
inactive after that same uninstall/reinstall cycle. The supported revocation
procedure is therefore: disable the intended definition in `/hooks`, verify its
event row reports the expected installed-versus-active count, and only then
remove the plugin if complete removal is also wanted. Do not automate this by
rewriting `config.toml`; stock Codex owns the parser and persistence semantics.

## Verification

The artifact carries its own offline suite:

```sh
python3 tests/run_all.py
```

The repository gate also registers each new `*_smoke.py`. Live untrusted and
trusted stock-process legs are recorded separately because direct script tests
cannot prove Codex trust enforcement.

Read [SECURITY.md](SECURITY.md) before trusting the hooks.
