# Release notes

Newest release first. Earlier releases follow below the divider.

## 2026-09-27 — r48: stable macos-bizops on the Apple-native stack

**Solet Manager manager-v0.1.0-r48.** This is a full seed build, published
twice from the same landed source commit: first to
`solet-public/macos-bizops-daily`, with manager artifacts in
dwestgate/homebrew-tap-validate; then, after a fresh macOS 27 VM round, as a
fresh build to stable `solet-public/macos-bizops` — the first stable-channel
cut since r43 (r44 through r47 published to `-daily` only) — with manager
artifacts in solet-public/homebrew-tap, the public distribution route.

Requires macOS 27 on Apple-silicon hardware with Apple Intelligence
available. If Apple Intelligence is off or its model is still downloading,
the solet installs and runs, and summaries stay degraded until it is
available (warn-only, self-recovering). No other OS versions are supported.

Changes since r46 (source `af39a9f45`):

- Fresh macOS 27 genesis now builds `apple-fm-sdk` from its vendored wheel
  instead of its sdist: `profile_install` resolves the vendored wheel first
  via `--find-links`, avoiding a `SwiftToolingError` build failure that
  otherwise required Xcode.
- `coreai_embeddings_plugin` never became ready after a fresh boot:
  `initialize()` invalidates the readiness `prepare_for_readiness()` set
  during Phase-1 startup, and nothing re-prepared it. Both boot and a plugin
  config reload now re-prepare readiness correctly.
- `solet create`'s preview gate no longer deadlocks at the models stage. It
  now attempts the Core AI asset acquisition instead of waiting on it, and a
  blocked LaunchAgent install is excused when a runnable peer operation is
  set up to clear that exact same block — closing a deadlock the Core AI
  LaunchAgent deferral could trigger even though the prior
  precondition-remediation excuse never covered it.
- The Homebrew keg now ships its own rendered release manifest (previously a
  build-time-only artifact), so a Manager-only reissue can read
  `allow_manager_seed_skew` from an installed keg instead of finding nothing
  there.
- Seed assembly no longer fails closed on undeclared fixture dependencies:
  `apple_setup_flow_smoke`'s dependency on the Core AI and Apple FM
  per-plugin config files and `selected_inference_startup_smoke`'s
  cross-bundle transition are now declared in the seed's shipped-smoke
  contract.
- Two service-interface verbs, `inference_service::qualify` and
  `thinking_service::resume_thinking_completion`, declared required
  arguments their own schema never supplied, so neither was reachable by any
  real call path. Both are fixed, and a new class-wide AST check catches the
  pattern going forward.
- The session ledger's away-summary lookup is now a bounded read: an
  unbounded scan over a conversation group's SYSTEM events could trip the
  state service's row cap deterministically on a long-running session with a
  large sibling event set. Periodic auto-summarize failures are now logged
  visibly instead of retrying silently forever.
- Homebrew existing-install inspection's seed-lock hash moves out of the
  wheel-bundled catalog — which could never validly carry it, since that
  hash is only known after the release's own seed lock is rendered — into a
  separate release-rendered catalog file the installed keg ships.
- Documentation correction (this entry's own change): the shipped
  Apple-native profiles' actual default — Core AI Nomic embeddings, Apple
  Foundation Models inference — is now what the hydration runbook, the
  profile-templates KB article, and the seed README template describe. Each
  previously stated or implied that LM Studio / `openai_embeddings_plugin`
  was still the default, or that its models needed manual provisioning.

Fresh-guest installation and the existing-install update path are separate
post-publish operations. This release does not claim a fresh-guest install,
an update of an existing solet, `solet doctor`, or E3 result.

Known limits: the r45 and r46 limits remain — Core AI embeddings and Apple
Foundation Models require macOS 27 on Apple Silicon; Apple Foundation Models
also needs an Apple Intelligence-eligible host; choosing LM Studio embeddings
is not supported in the shipped macOS profiles; an update of an existing
solet has not been re-measured end to end on a clean guest; there is no
automated supported manager-binary upgrade path; and there is no
abandon/change decision command for a blocked transaction.

---

## 2026-09-27 — r47: Manager reissue of the daily seed with install and update fixes

**Solet Manager manager-v0.1.0-r47.** This is an automated publish_release
Manager-only re-release (reissue of seed release
release-2026-09-27-0a0388214130), with manager artifacts in
dwestgate/homebrew-tap-validate, built from the landed source commit
containing this entry. The solet-public/macos-bizops-daily seed is unchanged
from r46: same seed release tag, same sealed commit, no new seed content.

Changes since r46:

- The Manager no longer carries a fixed existing-install seed-lock hash in its
  wheel. That hash must match the release's own rendered seed lock, which
  cannot be known when the Manager source is committed, so r46 carried a value
  that matched no published lock and refused every existing-install update. The
  hash now lives in a separate catalog file rendered with the release after the
  seed lock is written and verified, and the Homebrew keg ships it under
  share/solet/.
- The Manager's existing-install inspection and import accept a Homebrew keg
  whose installed wheel has no RECORD file. A Homebrew-poured keg omits
  RECORD, so r46 refused inspection and import on a real install. The
  ownership, symlink and write-bit checks still apply; a RECORD that is present
  is still verified against the resource; more than one RECORD is still
  refused.
- `solet create` no longer deadlocks in preview at the models stage on
  macOS 27: its preview gate now attempts the Core AI model acquisition instead
  of waiting on it.

Fresh-guest installation and the existing-install update path are separate
post-publish operations. This release does not claim a fresh-guest install,
an update of an existing solet, solet doctor, or E3 result.

Known limits: the r46 limits remain. Core AI embeddings and Apple Foundation
Models require macOS 27 on Apple Silicon; Apple Foundation Models also needs
an Apple Intelligence-eligible host.

---

## 2026-09-27 — r46: Apple-native local inference on macOS 27

**Solet Manager manager-v0.1.0-r46.** This is an automated publish_release cut
to solet-public/macos-bizops-daily, with manager artifacts in
dwestgate/homebrew-tap-validate, built from the landed source commit
containing this entry.

Changes since r45:

- New `coreai_embeddings_plugin`: Nomic embed text v1.5, converted to Apple
  Core AI, runs in the solet's own process on macOS 27, on the GPU with a CPU
  fallback. It replaces the OpenAI-compatible local-server embeddings in the
  macOS profiles, and setup now selects it by default. The model (about
  277 MB) is downloaded by setup from a pinned release asset and verified
  against its SHA-256 before use. A valid copy already installed is reused
  without a download; a failed download or a corrupt installed copy stops
  setup with repair text instead of continuing.
- New `macos_inference_plugin`: Apple Foundation Models summaries on eligible
  macOS 27 hosts. It replaces LM Studio as the local summary provider in the
  macos-bizops profile. Planning and other frontier work stay with the frontier
  session.
- Setup does not start the solet's LaunchAgent until the pinned Core AI model
  is verified. Until then the LaunchAgent install reports blocked with repair
  text and Genesis records the deferral as a warning, never as a started
  service.
- Setup checks for macOS 27 on Apple Silicon when model setup begins, before
  any Apple-native model is configured or downloaded.
- Fresh macOS profiles materialize the model folder under the solet's own
  data directory.
- An intentionally vacant inference binding now starts as vacant instead of
  failing startup; an explicit but broken binding still fails closed.

Fresh-guest installation is a separate post-publish operation. This release
does not claim a fresh-guest install, solet doctor, or E3 result.

Known limits: Apple Foundation Models needs an Apple Intelligence-eligible
host; on an ineligible Mac or virtual machine, summaries are unavailable and
setup warns rather than fails. Core AI embeddings require macOS 27. Choosing
LM Studio embeddings is not supported in the shipped macOS profiles in this
release: they no longer carry the OpenAI-compatible embeddings plugin, and
their LaunchAgent still waits for the Core AI model. The limits disclosed in
r43 to r45 otherwise remain.

---

## 2026-09-25 — r45: doctor, LM Studio and messaging fixes

**Solet Manager manager-v0.1.0-r45.** This is an automated publish_release cut
to solet-public/macos-bizops-daily, with manager artifacts in
dwestgate/homebrew-tap-validate, built from the landed source commit
containing this entry.

Changes since r44:

- The Manager's doctor runtime identity check reads only the top-level
  launchd job state and PID, so a healthy LaunchAgent whose venv Python
  resolves through the Homebrew framework no longer fails, and nested
  coalition blocks are no longer miscounted as duplicate job states. A job
  that is waiting but still reports a PID still fails closed.
- Manager contracts and LM Studio readiness are reconciled, and an LM Studio
  model pull is judged by the daemon's observed state rather than the pull
  command's own output.
- The published r43 seed is recorded as the predecessor for existing-install
  transitions.
- Peer messaging routes no longer run on the solet's event loop, removing
  multi-second stalls and the mass reconnects they caused.
- A managed dispatch mints its work-register unit atomically.

Fresh-guest installation is a separate post-publish operation. This release
does not claim a fresh-guest install, solet doctor, or E3 result.

Known limits: the r43 existing-install upgrade router fix is not in this
release and follows in a later one. The limits disclosed in r43 and r44
otherwise remain: an update of an existing Solet has not been re-measured end
to end on a clean guest; there is no automated supported manager-binary
upgrade path; and there is no abandon/change decision command for a blocked
transaction.

---

## 2026-09-23 — r44: first automated release cut

**Solet Manager `manager-v0.1.0-r44`.** This is the first automated
`publish_release` cut to `solet-public/macos-bizops-daily`, with manager
artifacts in `dwestgate/homebrew-tap-validate`, built from the landed source
commit containing this entry.

Fresh-guest installation is a separate post-publish operation. This release
does not claim a fresh-guest install, `solet doctor`, or E3 result.

The known limits disclosed in r43 remain unless later landed evidence proves
otherwise: an update of an existing Solet has not been re-measured end to end
on a clean guest; there is no automated supported manager-binary upgrade path;
and there is no abandon/change decision command for a blocked transaction.

---

## 2026-09-19 — r43: existing-Solet import and update ships; ten install-blocker fixes

**Solet Manager `manager-v0.1.0-r43`** (`solet 0.1.0_28`) is pinned to public
seed release `release-2026-09-19-ef2ea8fce869` of `solet-public/macos-bizops`,
built from source pin `ef2ea8fce86918234d3e4281050b64c7efcb3d8e`. It was
validated end to end on a fresh 50 GB macOS guest: every required setup
checkpoint completed, `solet doctor` verified 24/24 checks, and the
target-local health probe reported healthy with the manager's own
registration in agreement. The published seed's artifact-keyed born-clone
gate passed cleanly (635 total, 620 passed, 15 environment-dependent skips,
zero failures).

### What is in it

- **New: existing-Solet import and update.** The manager can now inspect,
  classify, and import a pre-manager solet (one born from an earlier seed via
  `bootstrap.py`, not created by `solet create`), and can update an
  already-manager-created solet to a newer seed release in place. This spans
  provenance verification, a v2 maintenance inventory, an atomic
  Manager-only enrollment path, an `update` CLI with fast-forward apply,
  update runtime and cutover with hardened adapter protocols, dual-contract
  doctor/resume/promotion coverage, and a cold-host/real-style fixture matrix
  plus Homebrew import/upgrade lifecycle smoke coverage. **What this release's
  own clean-guest validation proves:** a fresh install completes cleanly
  end-to-end (doctor 24/24, health probe healthy) on the exact shipped code.
  **What it does not yet prove:** a real update-of-an-existing-solet has not
  been exercised end-to-end on a clean guest by this release's own validation
  ladder — the code path is extensively covered by unit and fixture-based
  tests (cold-host and real-style scenarios, both landed under independent
  review), but a live guest walk of install-then-update is a fast-follow, not
  yet re-measured here. Until that re-measurement lands, treat the *Reinstalling
  on a machine that had a solet* section below as still authoritative for what
  to expect in practice.
- **Ten install-blocker fixes**, each independently landed and gate-verified
  during r41/r42's own validation attempts: SeedLock resume-identity
  comparison; session-source naming migration; session-ledger boot-source
  registration; postgres readiness classification; `corrupt_state`
  out-of-band recovery reconciliation; identity-gate binary-file tolerance;
  cask-executable PATH-restriction resolution (two separate rounds);
  Homebrew dry-run version-item parsing; budget/timeout raises for
  `install_lm_studio` and `embedding_request_succeeds`; and a resume/preview
  permission preflight.
- **Fleet/docs housekeeping** (retirement-handoff convention, fleet
  liveness/progress joseki cards) — no functional effect on the install/update
  surface, shipped as-is.

### What is NOT in it, stated plainly

- **A real update-of-an-existing-solet walk has not been re-measured on a
  clean guest by this release.** See above — the code ships and is
  extensively tested at the unit/fixture level, but this release's own
  clean-guest ladder validated a fresh install only, per explicit discretion
  not to hold a ready, green fresh-install publish on a supplementary check.
  Follow the *Reinstalling on a machine that had a solet* section below until
  that walk is published.
- The previous release's outstanding limits still apply unless explicitly
  fixed above: no automated supported manager upgrade path for the manager
  binary itself, no abandon/change decision command for a blocked
  transaction.

Read the tap README first (https://github.com/solet-public/homebrew-tap) for
the current install guide, known gaps, and reinstall guidance.

## 2026-09-16 — r41: internal tooling only, retroactive disclosure

**Solet Manager `manager-v0.1.0-r41`** (`solet 0.1.0_27`) was published to
`solet-public/macos-bizops` and `solet-public/homebrew-tap` on 2026-09-16,
built from source pin `5c5aec1965befd824b71ad66458ee70025c2d2a7` ("require
explicit Project Solet register CLI"). This entry is added retroactively on
2026-09-19, during the r43 cut, after r43's own predecessor-conflict check
found this publish live on GitHub with no corresponding release note. r41's
source commit is internal Project Solet register tooling with **no
user-facing manager or install-surface change** — this is presumably why no
customer-facing note was written for it at the time. Recorded here so the
release history stays honest: every published round gets an entry, even one
that changes nothing an operator would notice.

## 2026-09-15 — r40: diagnosable genesis recovery and persistent-autostart proof

**Solet Manager `manager-v0.1.0-r40`** (`solet 0.1.0_26`) is pinned to
public seed release `release-2026-09-15-da73ac6cc1e4` of
`solet-public/macos-bizops`, built from source pin
`da73ac6cc1e4106864293faa766b301d91c750ca`. It was validated end to end on a
fresh 24 GB macOS guest: every required setup checkpoint completed, `solet
doctor` verified 24/24 checks, and target-local bridge health was healthy.
The published seed's artifact-keyed born-clone gate passed 586 of 601 checks;
the remaining 15 were classified environment-dependent skips, with no failures
or missing entries.

### What is in it

- **Fixed: genesis now preserves its actionable failure diagnostic.** An
  interrupted or inconsistent setup journal no longer becomes an opaque
  `corrupt_state` failure; r40 reports the underlying condition so the
  documented, narrowly-scoped repair can be chosen. This does not make it
  safe to overwrite a prior instance's state or Keychain entries.
- **Fixed: LaunchAgent readiness proves persistence as well as liveness.**
  The manager requires both a healthy launchd service and its persistent plist
  on disk before calling autostart reboot-ready. A manually loaded but
  non-persistent job can no longer pass that acceptance claim.
- **Fresh-install validation uses the released r40 payload.** The manager,
  Formula revision 26, public seed, and transport bytes were bound and checked
  together before the clean-guest ladder ran; the final transaction verified
  every required completion probe.

### What is NOT in it, stated plainly

- **Updating an existing solet is not supported or proven.** It remains round
  two. A stale transaction, instance registry, or Keychain vault can still
  block or misdirect a reused instance name; follow the tap README's
  *Reinstalling on a machine that had a solet* sweep before reusing one.
- **Known install-time transient:** `iss_b7b01bcc` tracks pgvector dependency
  reconciliation that can leave the formula absent while the extension remains
  registered. If completion reports a vector-library load failure, run `brew
  install pgvector` and resume the same reviewed `solet create` transaction;
  do not drop or recreate the extension.
- The previous release's outstanding limits still apply unless explicitly
  fixed above: no automated supported manager upgrade path, no abandon/change
  decision command for a blocked transaction, and no manager adoption of
  bootstrap-era solets.

Read the tap README first (https://github.com/solet-public/homebrew-tap) for
the current install guide, known gaps, and reinstall guidance.

## 2026-09-15 — Cold first boot reaches active color; installing on a fresh Mac is validated end to end

**This release fixes a cold-first-boot defect that could leave a brand-new
install never reaching an active router color inside its startup-readiness
window**, and is the first release whose fresh-install path is validated end
to end on a clean 24 GB virtual machine — through `solet create`, `solet
doctor` and `solet` health — after two earlier releases (r37, r38) were
measured against a base VM that turned out to silently resume a stale
transaction from an older seed, so their fresh-install ladders never actually
ran the code under test. The install path itself is unchanged from the
2026-09-08 release:

```console
brew install solet-public/tap/solet
solet create <name>
```

Read the tap's README first (https://github.com/solet-public/homebrew-tap);
it carries the full stage-by-stage guide and the current *Known gaps* list.
The same guidance ships in this tree as
`plugins/github_midwife_plugin/knowledge_base/09_homebrew_install_troubleshooting_runbook.md`.

### What is in it

- **Solet Manager `manager-v0.1.0-r39`** (`solet 0.1.0_25`), pinned to seed
  release `release-2026-09-15-0551603bd674`.
- **Fixed: a cold first boot now registers active before any
  inference-dependent work runs** (`iss_7f240dbe`, commit `0551603bd`). The
  embeddings plugin's readiness probe could crash-loop against a
  not-yet-serving LM Studio, and grammar pre-warm plus knowledge-base rebuild
  both ran before router registration and competed with it, so a cold
  install_launchagent restart could miss the 120 second startup-readiness
  budget entirely. That work now runs as deferred background work after
  registration. Measured on the validation run below: 1.145 seconds from
  restart to active color.
- **Fixed: the inference qualification probe is advisory, not a hard
  blocker** (`iss_2e36a62e`, commit `257df2cb8`, shipped in r38). A slow or
  failing `qwen3-14b` probe answer no longer stops the install with
  `decision_qualification_failed`.
- **Fixed: `install_launchagent` no longer refuses its own flow-declared
  inputs at the adapter gate** (`iss_d0f9e899`, commit `3f8774534`).
- **Fixed: managed tmux-hosted Claude Code seats now preserve newline key
  chords** (`iss_42ba8b2c`). The spawn path enables tmux extended-key
  reporting (`extended-keys` and `xterm*:extkeys`); setup offers the matching
  `~/.tmux.conf` block and an iTerm2 dynamic profile where Option+Return sends
  Escape then Return. Restart iTerm2 and select **Solet Claude Code Return
  keys** before opening a new seat. Shift+Return additionally needs iTerm2 CSI
  u reporting or Claude Code's `/terminal-setup` mapping. If neither chord is
  available, type backslash followed by Return for a newline.
- **Fresh-install validation is real again.** The base VM clone used for the
  r37 and r38 ladders carried a stale manager transaction file pinned to an
  older seed, so `solet create` silently resumed that old transaction instead
  of installing fresh — both releases' "the fix works on a clean machine"
  claims were built on a guest that was never actually running the new code.
  r39 is measured on a guest independently verified clean of that residue
  (and two further residue classes found while chasing it down: a stale
  `~/.config/solet` instance registry, and stale login-Keychain vault items
  from a prior install of the same name) before `brew install` ran. On that
  guest: `brew install` 15 s, `genesis` 43 s, `models` 0.4 s after selection,
  `coding_agents` 3 s, completion probes 15 s, `solet doctor` verified,
  bridge healthy.

### What is NOT in it, stated plainly

- **Updating an existing solet to r39 is not supported and not proven.**
  That is round two. `solet create <name>` on a machine that once held a
  solet of that name can silently do the wrong thing instead of failing
  cleanly: it can resume a stale transaction pinned to the old seed
  (`iss_50db7d43`), refuse with `state_conflict` against a stale instance
  registry (`iss_6e520a72`), or have genesis refuse to re-birth the name over
  leftover login-Keychain vault entries while the manager drops the failing
  command's own stderr (`iss_74d70ef6`). The tap README's new *Reinstalling
  on a machine that had a solet* section gives the manual sweep to run before
  trying a name again; there is no automated or supported upgrade path yet.
- **The `models` stage still needs LM Studio and both models provisioned by
  hand on a machine with neither present.** An automated `install_lm_studio`
  operation exists in the manager now (commit `43c6ae5c5`), but r39's own
  validation guest had LM Studio already present in the base image, so that
  automation is not yet independently proven from a bare machine.
- Everything else in the previous release's "What is NOT in it" still
  applies unless listed as fixed above: LM Studio is not started at login by
  anything this release installs, a blocked `solet create` still cannot be
  abandoned or have its decisions changed on the same name, `doctor`'s
  declared expectation values are documentation rather than independently
  evaluated, and bootstrap-era solets are not adopted by the manager.

### Updating an existing solet from this release

Solets created from an earlier seed with `bootstrap.py` keep updating through
the seed-update runbook in their own knowledge base (fast-forward pull,
restart); the manager still does not adopt them. A solet created by the
manager itself has no supported update path in this release — see *What is
NOT in it* above.

## 2026-09-08 — The Homebrew install path: `brew install solet-public/tap/solet`, then `solet create`

**This release is the install path.** Since the 2026-08-20 release the seed
has gained a global manager, `solet`, installed from a public Homebrew tap,
and a declared setup flow that the manager drives one stage at a time with a
preview you approve before anything is written. New installs no longer start
from a clone and a bootstrap script; they start from two commands:

```console
brew install solet-public/tap/solet
solet create <name>
```

Read the tap's README first (https://github.com/solet-public/homebrew-tap).
It is the install guide, the stage-by-stage troubleshooting reference, and
the list of what this release does not do yet. The same guidance ships in
this tree as `plugins/github_midwife_plugin/knowledge_base/09_homebrew_install_troubleshooting_runbook.md`,
searchable once the solet is up.

### What is in it

- **Solet Manager `manager-v0.1.0-r25`** (`solet 0.1.0_24`), pinned to this
  seed release. `solet create` previews and applies the install one stage at
  a time (`preflight`, `decision_review`, `system_dependencies`, `genesis`,
  `models`, `coding_agents`, `optional_accounts`, `session_sources`,
  `completion`), records a transaction journal, and refuses to apply a plan
  the machine has drifted away from. `solet status`, `solet doctor`, `solet
  list`, `solet inspect`, `solet start` and the reconcile verbs come with it.
- **The setup-flow contract ships in the seed** (`macos_setup_flow.json` and
  its schemas under `plugins/github_midwife_plugin/knowledge_base/`). The
  previous public seed did not carry it, so the manager's `doctor` could not
  verify an install made from it.
- **Install-order fixes found on clean machines this week:** the models
  stage runs before genesis, so genesis no longer fails for want of an
  inference server; pgvector is installed after PostgreSQL is running instead
  of before; sibling stage preconditions are full predicates rather than
  subsets of their postconditions; the LaunchAgent status parser handles a
  service that never exited; the genesis MCP stanza is idempotent;
  stage-boundary fingerprints pin the evidence they were computed from; the
  Codex client cask installs; a shell-substitution bypass in the shipped git
  policy is closed.
- **Platform fixes since 2026-08-20:** peer-message delivery and role-inbox
  drain pacing, bridge reconnect cursor fencing, tmux drive truncation, and
  the messaging-reliability work listed in the commit history.

### What is NOT in it, stated plainly

- **The `models` stage stops on a fresh Mac** with `model_discovery_failed`
  until LM Studio is installed, the two models are downloaded, and the local
  server is running. The manager does not do that yet. The runbook gives the
  exact commands and the one trap (LM Studio's bundled nomic model is the
  wrong build and looks right); it is a ten-minute manual step.
- **LM Studio is not started at login** by anything this release installs.
- **Stages after `models`** have had less clean-machine coverage.
- **Existing solets** created by cloning an earlier seed and running
  `bootstrap.py` keep updating through the seed-update runbook (fast-forward
  pull, restart). The manager does not adopt them, by design, and the
  self-directed "check the seed and converge onto the Homebrew path" update
  is not yet proven; do not attempt it on a solet you depend on.
- **The machine needs 24 GB of memory or more.** That is the supported
  minimum; smaller machines fail at the models stage.

### Updating an existing solet from this release

Unchanged: `git pull --ff-only` in the clone, restart, wait for startup to
finish. The complete procedure is the seed-update runbook in your solet's
knowledge base.

## 2026-08-20 — A fresh install can do work again, and the hook errors on a stock Mac are gone

**This release exists because two defects together made a correctly-installed
solet unable to act.** Both were reported by people installing, not found by us.
If you have hit either, nothing was wrong with your machine.

### ⚠ Two steps are required to RECEIVE this release. A pull is not enough.

Read this part even if you read nothing else. Neither step is automatic, and
neither announces itself if you skip it — you will simply keep experiencing the
old behaviour on an install that reports itself up to date.

**1. Re-run the hydration runbook's Step 2.** Your launcher
(`client/bin/claude-<name>`) was rendered ONCE, when your solet was born. A
`git pull` updates the template it was rendered from and never touches the
rendered file. Since this release's operator-visible change IS a launcher
change, pulling alone leaves you byte-identical to before.

**2. Uninstall and reinstall the `coordination-hooks` plugin.** The plugin runs
from a version-keyed cache copy. This release bumps it to `0.7.0`, but a version
bump only ARMS a refresh — neither `claude plugin install` on an
already-installed plugin nor `claude plugin update` re-copies changed files;
both report success without touching the cache. Only a genuine
uninstall-then-install moves the bytes.

### Your sessions no longer stop to ask permission for every action

The launcher used to pass no permission bypass, ever, and the only opt-in shipped
in an optional fleet-coordination file most installs never render. The practical
effect was that a single operator running a single session had no supported route
to an unattended one, short of adopting a whole multi-session apparatus to answer
a question none of it was about.

`claude-<name>` now starts with permission prompts off. If you want them back for
a session you intend to supervise:

```bash
SOLET_PERMISSION_PROMPTS=1 claude-<name>
```

The Codex launcher is unchanged and still passes no bypass of any kind.

One thing worth knowing, because its error message names neither cause: the
launcher takes positional arguments (`label`, `model`, `effort`) and forwards no
flags. `claude-<name> --permission-mode bypassPermissions` is therefore read as a
session named `--permission-mode` running a model called `bypassPermissions`. Use
the environment variable.

### The coordination hooks no longer fail on every single action

If your machine's `python3` was older than 3.11 — which is the default on a stock
macOS, where it is 3.9 — every tool call produced a hook error. The hooks are
written for a newer Python and used one function that only exists from 3.11
onward, and they were being started with whichever `python3` your PATH happened
to resolve first. Eight of the twenty shipped hook modules could not even load.

Each hook now checks the interpreter before it loads anything that would fail,
and steps aside quietly when it cannot run. Your sessions work either way.

**But quietly is not the same as fine.** On an interpreter below 3.11 the
coordination features — the heartbeat, rotation notices, memory capture — are
inert. They are not broken and they will not warn you; they simply do nothing.
If you want them, install a Python 3.11 or newer and make sure it is what
`python3` resolves to. The hydration runbook's Step 3 now verifies this in a
fresh login shell and reports what it finds, so an install that is degraded says
so instead of looking healthy.

### A hook that reported its own normal state as a problem

Separately: one hook wrote a diagnostic line on every tool call whenever a
session was not part of a coordinated fleet. That is the ordinary state of an
ordinary session, so the message was noise on a correctly-configured machine —
and noise on every action is how people learn to stop reading hook output. It is
silent now. Genuinely anomalous conditions still report.

---

## 2026-08-19 — Alarms that leave a record, a context notice that survives its own delivery, and a stalled gauge you can see

**One behaviour change worth knowing about, and it is deliberate.** A command in
the shipped coordination hooks that used to do nothing quietly now fails with a
message when it is asked to do nothing. Everything else in this release is
additive: alarms that could fire into silence now leave a durable record, a
condition that could not be detected at all now can be, and several notices stop
asserting things they never measured. No envelope shrinks, and no stored row
needs rewriting.

Update with the standard short form: pull fast-forward only, restart, and wait
for startup to finish. The seed update runbook carries the complete procedure.

### A context-rotation notice now survives the delivery that used to consume it

When a session crosses a rotation threshold, the platform tells that session so
directly. That notice was only ever a queue entry on the session's own event
channel. A queue entry is consumed by whoever drains it, and it leaves nothing
addressable behind, so for the one population the notice exists to serve — a
session running unattended, whose channel is drained by machinery rather than
read by a person — the notice could be taken off the queue and never seen.

The notice is now written as a durable message first and surfaced second, and
the order is the guarantee rather than a detail: surfacing first would let an
interruption between the two steps leave a notice that was displayed and then
lost. The persisted copy outlives the drain and stays readable from that
session's own inbox afterwards, so a notice that was delivered while nobody was
looking can still be found later.

The durable copy deliberately does not travel the same path an ordinary peer
message takes. That path marks everything it sends as important, wraps the text
in an envelope that presents the session's own measurement as mail from someone
else, and wakes the recipient through an adapter that raises when a recipient
has no live attachment — which would let a single dead binding fault the pass
for every other session in it. The measurement is now written as what it is: the
session's own reading, in its own words, counted per session, with a sweep that
keeps going when one recipient is unreachable.

### A notice that fired on the fraction now says so, instead of telling you to keep working

Rotation urgency is measured on two axes, an absolute token count and a share of
the model's own context ceiling, and the notice fires when either one is
actionable. On a small ceiling the share can cross while the absolute band is
still comfortable — and in exactly that case the notice printed the absolute
band alone, so a message typed as a rotation notice arrived reading, in effect,
keep working.

Every notice now carries the reason it fired: both axes agreeing, the absolute
band alone, or the share alone, each stated with the numbers behind it. The
reason is passed down from the decision that already made it rather than
recomputed where it is printed, so the sentence cannot drift away from the rule
it describes.

### One due condition now reaches you once, not up to three times

Four independent legs can notice that a session is due, each latching against
its own store, and none of them can see the others. A single condition could
therefore reach a steward three times and a session-level reader twice.

The watch leg now stands down whenever its own report to the platform completed,
because the platform's own legs then cover the same condition. This is deferral
rather than suppression: the stand-down never consumes the delivery record, so a
platform that stops answering makes the very next unthrottled tick fire
normally. It is additionally conditioned on the band being actionable, because
the platform being reachable does not establish that the platform is saying
this.

### The boot-cost figure the notices reason from was re-measured, and it had moved

The rotation policy subtracts an estimate of what a session costs before it does
any work of its own. That estimate was re-measured on a current session
generation by the same method that produced the original, with the older
transcript re-run as a control to prove the method still reproduces its own
earlier result exactly. It had drifted upward by about a third.

The drift is not the hook set: the boot payload moved barely at all over three
days, while the rehydration prompt that runs at pickup moved by half. The
provenance note now names the pickup prompt as a re-measurement trigger and
requires both components to be recorded separately, so the next drift is
attributable instead of merely visible. Test probes that were fixed numbers
sitting either side of the estimate are now derived from it — at the new value
those literals had ended up on the wrong side of their own thresholds, where the
assertions would have stayed green while no longer testing their subject.

### A gauge that STOPPED is now detected, not just one that was never written

The coverage check asked whether a session's context gauge row existed. A live
session sat for over an hour with a row that existed and had simply stopped
changing, and nothing surfaced it, because a frozen row reads as coverage.

There is now a separate check for the frozen case, and it deliberately does not
key on either clock alone. Both the heartbeat and the gauge reporter run on the
same completed tool call, so a session that is merely idle writes neither row
and its gauge goes stale with nothing wrong. The new check keys on the
divergence between the two: a session whose heartbeat is advancing while its
gauge is not. It carries its own wording and its own latch, and it releases that
latch when the condition relapses.

The same reasoning fixed a bound that had the opposite failure. Eligibility to
receive a rotation notice was gated on the gauge clock, so a session whose gauge
had arrested dropped out of the notifiable population after an hour — it stopped
being told to rotate at exactly the point it had been running longest without
being told. Eligibility is now read from the session's own liveness deadline.

### A bounded history behind the gauge, so a freeze is diagnosable after it ends

A single current reading cannot distinguish a gauge that stopped from a session
that went quiet, and by the time anyone looks the evidence is usually gone. The
platform now keeps a bounded series of past readings per session and exposes it
for reading, so the shape of a freeze — when it started, what the last moving
value was, whether it resumed — can be reconstructed after the fact instead of
having to be caught live.

### Every gauge alarm now leaves a durable, attributable record

Gauge alarms existed only as in-memory events. Nothing persisted them, the only
reader removed what it read, no surface was keyed on the kind of alarm, and the
notify path returned early when no steward was bound. The consequence is the one
that matters: an alarm that fired into the void and an alarm that never fired
were the same silence.

Each alarm is now recorded where it is raised, and the record is not conditional
on a steward being bound — having nobody to tell is written down as an outcome
rather than taken as a reason to stop. The record carries the delivery outcome
as a stated result rather than a bare success flag, the thresholds as they were
measured at the moment the alarm fired, and the identity of the release that
raised it, taken from the running code's own tree rather than from whatever the
reader believes is deployed. A new read path returns these records without
consuming them.

Alongside it there is now a canary: a synthetic subject that reports through the
real write path, a bounded and audited way to disturb it, and a verifier that
judges both edges against the durable record — that the detector fires when it
should, and stays quiet when it should. When the detector is not deployed at
all, the verifier abstains rather than accusing, because absence of the
instrument is not evidence about the thing it measures.

### Coverage alarms no longer fire at a worker that has simply not booted yet

The startup grace before a session was expected to have reported was five
minutes. The worst measured time from spawn to first reading, across several
observations of real managed workers, is nearer eight — the gap is structural,
because a spawned worker goes through dispatch and an acknowledgement turn
before its first working turn, and only a working turn produces a reading. The
grace is now ten minutes, with the measurement named in a test so it cannot
quietly drift back under the evidence.

The better half of the fix is that the check no longer rests on a wall clock at
all, since a wall clock can only ever guess at someone else's dispatch latency.
It now reads whether a heartbeat has landed since the session went live, which
is direct evidence that the session is completing tool calls. That reading is
three-valued on purpose: a session with no heartbeat window at all returns
unknown, because the absence of the window is not evidence about the beat.

The alarm text changed with it. It used to open by asserting that the session's
hooks were running — an inference about a row it had not read — and to name a
likeliest cause it had never measured, then close by ruling out the explanation
that turned out to be correct. It now leads with what was measured, how long the
session has been live with no reading against the stated grace, and diagnoses
only as far as that evidence reaches.

### Clearing a session now reports what actually happened to it

The verb that clears a session reported success on the basis of having sent the
instruction, not on the basis of the session having been cleared — so a pane
that was mid-turn, and therefore never cleared, produced a success. It now
reports the effect: what the session's state was before, what it is after, and
whether the clear actually took. The companion status verb also accepts either
of the two session identifier forms in circulation, instead of resolving only
one and reporting nothing for the other.

### An installed copy of a hook is now checked by content, not by version number

A hook exists in two places: the copy carried in the repository and the copy an
installation actually runs from its own cache. Both declared the same version
while differing by more than a hundred lines, and a version comparison reports
agreement on exactly that state. The check now compares content at the version
both trees declare. Only a matching version is compared, so a deliberately
retained older installation cannot raise a false alarm, and an installation that
is simply absent produces a stated skip rather than a silent pass.

### The shipped memory hooks no longer swallow an interleaved write

The hooks that write local memory edits back to the platform list what is
pending, send it, and then mark that batch as sent. The marking step re-read the
journal to its current end rather than binding to the batch it had just listed,
so anything captured in between was marked as sent without ever having been
sent, and was then invisible to every later run.

The marking step is now bound to the listed batch. This is the behaviour change
named at the top: asking to mark a batch as sent when nothing was freshly listed
used to do nothing at all and report success, and now fails with a message
saying so, because the two situations need to be told apart.

The same fix has been applied to the vendored copy of those hooks that ships
with the coordination-hooks plugin, which had been left on the old behaviour.
The two trees were confirmed to be the same vintage before the fix was carried
across, differing only in wording, so this is the identical repair rather than a
reimplementation of it.

### Document checks now walk shipped executables, not only shipped prose

The check that catches references to files a seed never ships walked shipped
documentation only. A reference living in the docstring of a shipped source file
was invisible to it by construction, and there was a live instance. The walk now
covers shipped sources as well, narrowed to references into the planning
directory, which is never shipped under any profile and therefore has no
per-profile ambiguity. Deliberately narrower than checking every reference in
every source file: a trial run of the broader form surfaced a large number of
findings belonging to an unrelated class, and mixing the two would have buried
both.

The one live instance is fixed. The pre-existing references the widened walk
surfaced are registered as tracked debt through the same allowlist and baseline
mechanism the earlier document checks already use, and a first pass has since
rewritten a substantial share of them from references into plain prose, with no
functional change to any of the files involved. The remainder stays tracked
rather than silently tolerated.

Two smaller repairs travel with it. The bundle verifier now writes its own
verdict as part of verifying, instead of leaving that to a separate call a
runner had to remember — a forgotten call produced no baseline, which then read
as a passing comparison rather than a missing one. And the test double that
stands in for the database now enforces the declared shape of the gauge table,
so a column that was never declared fails in the test suite rather than only
against a live database.

## 2026-08-18 (second update) — A stall alarm that can actually fire, and a bridge registration that refuses to start broken

**One narrow breaking change, and it is deliberate.** A managed spawn that was
previously accepted while being silently unusable now refuses to start, with an
error naming what to fix. The other item is a correction to a health verdict
that could not report the condition it existed to report. No envelope shrinks,
and no stored row needs rewriting.

Update with the standard short form: pull fast-forward only, restart, and wait
for startup to finish. The seed update runbook carries the complete procedure.

### The action-path stall verdict no longer suppresses itself

The health report publishes a derived verdict for whether the action dispatch
path has stopped, alongside the two numbers behind it: how long it has been
since a poll cycle completed, and how many rows were waiting at that last
cycle. The verdict required both to be true at once — a stale poll age, and
work waiting to be done.

The second half of that pair defeats the first. The waiting-work figure is
written only by the very poller whose death the verdict exists to detect, so a
freeze that begins while the queue happens to be empty pins that figure at zero
for as long as the freeze lasts. The condition could then never be met, and the
report kept saying healthy: stale age, empty queue, not stalled. That was
observed live on a running deployment, not inferred from reading the code.

The verdict is now the stale poll age alone. This does not reintroduce a false
alarm on a quiet deployment, and the reason is structural rather than a matter
of tuning: the poll timestamp is stamped at the end of every cycle, including
cycles that found nothing to do, so an idle-but-alive poller keeps it fresh
indefinitely. An age past the threshold therefore already means that cycles
stopped completing, not that nothing was queued. The waiting-work figure is
still published as corroborating context; it no longer gates the answer.

If you have anything keying on that verdict, expect it to begin reporting the
condition it was always meant to report. Nothing that previously reported a
stall stops doing so, and the threshold is unchanged.

### A managed bridge spawn now refuses rather than registering a binding nobody can confirm

When a managed spawner starts a message-bridge subprocess for a session, it
injects a stable instance identifier so the registry replaces that session's
binding in place across reconnects instead of accreting a new one each time. It
does not pass a session identifier alongside it, and the subprocess cannot
recover one from its own environment — that environment belongs to the
long-running service, not to the session being spawned.

The registration went ahead anyway, with the session identifier left empty. The
row it wrote looks present and is unusable: the check that asks whether a
session holds a role, and the call that claims one, both resolve through that
field, so neither can ever confirm anything for that instance. A session in
that state is told it holds no role while its messages route to it correctly —
a self-check that structurally cannot agree with what is actually happening.

That combination is now refused before anything is written, with an error that
names the missing carrier and what has to supply it. This is the breaking half
of the release: a spawner that omits the session identifier fails at startup
instead of appearing to work. The failure is the point, because the previous
outcome was a registration that could not be diagnosed from either side of it.

The deliberate empty-identifier path for a genuinely interactive bridge, started
by hand rather than by a managed spawner, is untouched. The two cases are told
apart by whether an instance identifier was injected, which only a managed
spawner does. The read side and the preserve-on-empty registration behaviour
are unchanged as well: this closes the fault where it was created rather than
compensating for it where it was noticed.

## 2026-08-18 — Rotation notices that fire when they are needed, a sweep that reports a healthy tick, and shipped-document checks you can run before committing

**Additive for every configuration.** A notice that could stay silent exactly
when it was most needed now fires; a sweep line that said nothing on a healthy
tick now says something; and checks that could only run against an assembled
bundle can now also run against a checkout. No envelope shrinks, and nothing
that worked before stops working.

Update with the standard short form: pull fast-forward only, restart, and wait
for startup to finish. The seed update runbook carries the complete procedure.

### A rotation-due notice now fires on either axis, and names the one that fired

Rotation urgency is measured two ways: an absolute token count, and a fraction
of the model's own context ceiling. The notice fired on the fraction alone, so a
session sitting in the most urgent absolute band could still be told it was not
due. On a large ceiling that is not a corner case — the absolute bands are
reached long before half the window is gone, so the most urgent band this policy
has could coexist with a not-due reading.

The predicate is now the union of the two axes: the notice fires when the
absolute band is actionable or the fraction threshold is crossed, and it names
which axis fired, so the reading is something you can act on rather than a bare
flag.

The new predicate is a strict superset of the one it replaces. No session that
would have been served a notice stops receiving one, and no stored row needs
rewriting. Narrow-ceiling models gain the most: on a small window the first
actionable absolute band arrives well past that model's own halfway point, so
the previous rule was strictly later there.

### The rotation-surface sweep now reports a healthy tick instead of staying silent

A tick on which every leg was healthy produced no output at all. That made
"never ran", "ran and found nobody", and "ran, and every leg was healthy" into
byte-identical silences. The all-healthy case is not an edge case: sessions
quiet for an hour are excluded from the surface by design, so it is the ordinary
overnight state of a small deployment.

Each tick now emits a line naming every leg and its result, and the empty case
prints the denominators it measured over and says the leg ran and found no
eligible subject. Legs that faulted are named too — a summary built only from
the legs that succeeded would shrink silently and reproduce the same defect one
level up.

### The shipped-document checks now run before a commit, not only at seal time

The citation check and the reserved-identity scan both took an assembled bundle,
so neither could run until a bundle existed. "Run the shipped-document checks
before handing off" was therefore an instruction that could not be followed.

Both now also run against what a checkout would ship, derived from the same
manifest the assembler reads, through the same grammars the seal-time checks
use. Nothing about how a citation is extracted, classified, resolved, or
tolerated is restated in a second place: a check with two implementations is two
different checks wearing one name, and the day they disagree is the day the
weaker one is believed.

Citations are measured per profile, because which plugins a bundle selects
decides what its citations resolve against; the reserved-identity scan is
measured over the union, because it has no such census. The tolerated-count
comparison blocks on drift in either direction, and the tolerance file itself
stays untouched — what keeps the count where it is should be remediation, not
silencing. A born clone ships this check but not the assembler, so it detects
that it has nothing to mint and prints a declared skip instead of failing.

## 2026-08-17 (second update) — Worker paths that survive a deploy, a preflight that checks the code it is about to run, and lifetimes that surface

**One breaking change, and it is narrow.** On a host whose organisation-managed
Claude Code policy strips hooks from non-plugin sources, tmux-hosted spawns that
previously succeeded now fail. That is the first item below, with the flag that
opts back in. Everything else is additive for standard configurations.

Envelopes grow fields — supersets of their previous shapes — in four places: the
cutover preflight probe, the headless host driver's `capability_report()`, the
tmux host driver's `capability_report()`, and `session_context_status`. Two new
notice kinds appear on the platform sweep's tick: `ttl_overdue_notice`, addressed
to a session's steward, and `rotation_self_notice`, addressed to the measured
session itself on its own bridge. Consumers doing exact-shape equality on any of
those must update. Update with the standard short form: `git pull --ff-only`,
restart, wait for startup to finish (`05_seed_update_runbook.md` is the complete
procedure).

This is the second release of the day. It is mostly repair work, and two of
the items below repair things the *previous* release announced — the notes
say so explicitly rather than re-announcing them as new.

### A spawn that would produce a hookless worker now fails on the tmux host too (#8)

**This is the one breaking change in this release, and it is worth reading even
if you think it does not apply to you.**

An organisation-managed Claude Code policy that lists `hooks` inside
`strictPluginOnlyCustomization` strips hooks from every non-plugin source. The
platform injects its worker hooks through exactly such a source — the
`--settings` blob each spawn driver passes to Claude Code — so on such a host a
spawned worker runs none of them. It never registers, never heartbeats, and never
captures its session mapping, while leaving a ledger row that looks perfectly
alive.

A refusal for precisely this already shipped: the 2026-08-15 notes announced that
"a spawn preflight refuses a host whose managed policy strips hooks," naming no
host. It was wired into the **headless** driver only. The tmux driver kept
spawning. If you read that sentence as covering your tmux spawns, it did not, and
this is the correction.

That gap mattered more than the fix that closes it. tmux is the swap-durable
host — the one this platform's own spawn documentation tells you to prefer for
any worker expected to outlive a release — so the refusal was absent from the
host most likely to be carrying long-lived workers.

Both drivers now run the same preflight. **On an affected host, a tmux spawn that
succeeded before this update fails after it**, with `host_cannot_spawn` naming the
policy file and the offending key. If that is your configuration and you want the
spawn anyway, the escape hatch is unchanged from the headless host: set
`degraded_hooks_acknowledged` on the spawn. It is accepted, it is logged loudly as
a degraded spawn, and it is recorded on the ledger row — you get the hookless
worker, and the choice stays legible afterwards from either surface.

If you have no managed policy, or your managed policy does not list `hooks` under
that key, nothing changes for you: the preflight finds nothing and the spawn
proceeds exactly as before.

### A managed policy that switches your permission denies off now says so (#17)

The same managed policy file carries a sibling key,
`allowManagedPermissionRulesOnly`. Set to `true`, it makes Claude Code honour only
the permission rules written inside the managed file itself: user-level and
project-level allow/ask/deny rules do not apply at all, in any permission mode.
Every `permissions.deny` this deployment ships — the launcher overlay, both spawn
drivers' `--settings` blobs, and the checkout's own project-scope settings — is
therefore dropped before permission evaluation on such a host. Nothing local said
so. The key was already sitting in the object the hooks preflight parsed; the
detector built for that report was one sibling key short of seeing this one.

Both drivers now warn at spawn time, naming the policy file, the key, and the
exact deny list that is inert on that host. Both `capability_report()`s answer the
same question without spawning anything: `permission_denies_operative` is `false`
when a policy voids the rules, and `permission_policy_path` names the file that
did it. The deny list itself is reported alongside them, under `permission_denies`
on the headless host and `permission_denies_default` on tmux — the names differ
because the tmux list varies per spawn, so that report can only honestly describe
the default rather than claim to know what a given spawn will deny. The policy is
re-read on every call rather than cached at construction: a managed file can be
pulled remotely at any time, and a cached answer would go stale in the one
direction that reports a lost guardrail as still present.

**It warns; it does not refuse — deliberately the opposite posture from the hooks
key, and the asymmetry is the point.** A hooks-stripped worker never registers and
never heartbeats, so that spawn produces nothing usable at all and refusing it
costs you nothing. A permissions-inert worker registers, heartbeats, and does its
work; refusing it would trade your entire fleet capability for a guardrail that
was never load-bearing, on a policy you very likely have no authority to change.
There is deliberately **no** `degraded_permissions_acknowledged` flag either: an
opt-in that every single spawn on an affected machine would always have to pass
goes reflexive within a day and stops carrying information.

Stated plainly, because it governs how much this is worth to you:
`permissions.deny` is a hygiene guardrail, not a security control. A deny that a
local policy file can switch off was never a boundary. The defect being fixed here
is the **silence**, not a bypass.

### A session is now told its own context size

The context gauge was measured continuously and surfaced only when an operator
typed. A session running on its own was therefore never shown its own number, and
the one surface that would have told it was silent exactly when it mattered. Two
context overruns had been recorded as discipline failures; they were delivery
failures.

A third leg on the sweep's rotation surface now delivers a session's own
measurement to that session, as a `rotation_self_notice` on its own bridge. It is
a separate event kind from the steward-facing `ttl_overdue_notice` and
`rotation_due_notice` rather than a reuse of either, because those are written in
the third person about somebody else's session and this one is written in the
second person about yours — sharing a name would leave anyone filtering on it
unable to tell which they were reading.

Two properties of it are load-bearing rather than incidental:

- **It notices; it never acts.** The leg appends to the session's bridge and does
  not drive the session's host. Driving injects a turn, and no automated surface
  belongs in the injection path for a context clear. The notice therefore surfaces
  at the session's next natural boundary instead of interrupting work in flight.
- **It keys on the capacity band, not on the rotation-due threshold.** Those two
  are not the same line: on a large context ceiling the due threshold sits well
  above where the bands saturate, which left a wide window in which a session was
  demonstrably too full and nothing said anything. Repetition of the same band to
  the same session is floored so it cannot become chatter, but crossing into a new
  band notifies on the very next tick — a band change is new information, and
  delaying it would reintroduce, in miniature, the reporting lag the leg exists to
  remove.

**`session_context_status` grows one field, `agent_session_id`.** It carries the
session's stable id so a caller can *reach* the session rather than merely
describe it — which is what the notice above needs in order to arrive at a worker
whose bridge is held under a different id from the one its gauge row is keyed on.
A `null` there means the row was written by a reporter that predates the column —
**not** that the session has no bridge. Those two readings must not be collapsed:
the first is a coverage gap that heals by itself as reporters upgrade, and the
second would be a live routing failure worth waking somebody for. The column is
nullable by design; that tri-state is the whole point of it, and it is also what
makes the migration a non-event on an existing installation.

### Long-running workers survive a deploy — completing what the last release started

The previous release said worker CLI paths were expressed through the
deployment's atomically-swapped `current` symlink so they survive a cutover.
That was true of the rewrite and not true of the result: all four spawn
adapters resolved the path **once, in their constructor**, and stored it. The
answer is a function of mutable filesystem state — what `current` points at —
so caching it froze whatever the filesystem happened to say at process
startup.

The failure needs no error to occur and produces none. If a platform process
starts inside the window between a release being materialized and cutover
flipping `current` onto it, the resolver correctly *refuses* to rewrite (a
worker must never be silently redirected onto a version its spawner is not
running) — and the cached refusal then applies to every worker that process
ever spawns. Measured: a worker spawned nearly three hours after the flip
still carried a versioned path, while a fresh resolution in the same release
returned the stable one. Because a deploy reaps old releases and every
consumer of those values degrades silently by design, the next cutover would
have dangled every long-running worker's wake path at once, quietly.

Resolution now happens **per spawn**. The resolver ranks two candidates rather
than picking a strategy, because naive re-resolution is not strictly better
than the cache: after a cutover away from the running release plus a reap, a
fresh resolution returns the construction-time versioned path — one that has
just been deleted — and the cache survived precisely that case. So: prefer the
fresh answer; fall back to the stabilized one only when the fresh answer no
longer stats as an executable and the fallback does; if neither is usable,
return the fresh value so every caller's existing degradation contract is
unchanged. All four adapters were fixed together — they carried the identical
defect, and fixing only the one that was measured would have left three dead.

### The deploy preflight validates with the code it is about to run

The zero-downtime cutover preflight validated the deployment root manifest
using code the **outgoing** process had resolved at its own last start. So it
refused demonstrably valid manifests immediately after an update — that is,
it was self-defeating in exactly the situation the code-pickup path exists
for. Reproduced byte-for-byte against a pre-rename package, including a footer
current code cannot emit.

The check now runs inside the fresh-source probe, under the **candidate**
release's own interpreter with fresh modules — the mechanism already built for
this defect class for the plugin-manifest preflight, which this gate had never
been migrated onto. It had to *move* rather than be re-pointed, because the
candidate does not exist until the swap materializes it, which happens after
the old call site.

Refusal classification is deliberately unchanged: a drift refusal stays a
plain failure with a drift reason code, and is **not** reclassified as a probe
failure — the core routes on that status to roll manifest bytes back, and
root-manifest drift is a property of the deployment root rather than of the
bytes being applied. The probe envelope gains `capabilities` and `checks_run`,
and the runner discriminates on that **positive self-assertion, never on
absence**, so "this probe predates the contract" (permitted, logged as
degraded) stays distinguishable from "advertised the check but did not run it"
(refused loudly). Additive in both directions across a version-mixed cutover.

**Disclosed limitation — this fix cannot govern the cutover that carries it.**
The outgoing side drives a cutover, and the outgoing side is the release you
are replacing. So on the **first** cut after taking this update, the *old*
in-process gate runs, with exactly the stale-code problem described above; the
new gate takes effect on the **second** cut. The hazard is one of
interpretation and it runs both ways: a green first cut does not demonstrate
that the new gate works, because it will not have run; and a refusal on the
first cut is not evidence that this update broke your deploy — it is the old
gate's last appearance, and the correct response is to cut again rather than
to roll back.

### Sessions that overrun their declared lifetime now say so

A spawned session's requested time-to-live was recorded at spawn and then
**nothing ever read it** — three touch points, zero readers. A declared
lifetime that no surface enforces is a comment. The platform sweep now reads
it and reports overruns as a latched notice on its regular tick.

- It keys on the frozen expiry, **not** on the liveness deadline. The
  liveness deadline is re-armed by ordinary activity, so keying on it would
  make TTL structurally unreachable for exactly the sessions that overrun —
  a healthy, chatty session re-arms forever. A session that requested no TTL
  is skipped: no TTL is not an expiry.
- **It notices; it never reaps.** Auto-retirement was considered and refused
  on in-repo precedent — an earlier sweep leg stopped reaping observed-alive
  rows for the same reason. The notice latches on its own latch, never a
  shared one, so a session can be TTL-overdue *and* rotation-due *and* dark
  without any of those suppressing the others.
- **The gauge-coverage alarm stopped crying wolf.** That leg had no age
  predicate, so every spawn wave manufactured one false "the reporting path
  is failing silently" alarm per freshly-spawned session. It now has a
  startup grace and states the observed mismatch instead of asserting a
  diagnosis. It fails *toward* reporting: a row whose age cannot be read gets
  no grace, because a grace is an exception to an alarm and must require
  positive evidence.
- **A notice whose own message had a bug used to vanish.** Both notice legs
  composed their prose *inside* the try block guarding delivery, so a defect
  in the message was caught, logged as a delivery fault, and the notice
  disappeared. Prose is now composed outside that block. This was found by a
  test mutation that survived — because the error it should have raised was
  being swallowed.

### An authorization now outlives the session that was holding it

A pending authorization was seat state that lived nowhere durable: if the
session holding it rotated without writing it down, the obligation was simply
gone. This release adds a durable held-authorization queue — a table, a state
layer, and three verbs to record, list and retire entries — so a refusal that
is waiting on a first-party confirmation is recorded at the moment of refusal
and retired when the confirmation arrives. There is no silent expiry;
staleness stays visible through the creation timestamp rather than being
cleaned up behind your back.

**Disclosed gap — the verbs ship, the procedure that calls them does not.**
The queue landed platform-complete: table, verbs, and their process
definitions are all in this release. The caller side is a *procedure* — prose
that an agent follows when it declines a request it cannot verify first-party
— and that procedure lives in a repository-local skill file that this seed
does not ship. The seed ships exactly two skill templates, and a
git-controller-commit template is not among them; the manifest carries no
skills or commands directory at all. So on an adopter's machine these verbs
arrive as capability with no shipped procedure of any kind that calls them.
They are usable directly, and they are not yet wired into anything you
receive. Packaging that procedure is not in this release.

### A born clone installs the gate toolchain it needs

The publication gate births a throwaway clone of a candidate seed and runs its
shipped test register inside it. That throwaway's environment never installed
the gate toolchain, so the shipped complexity and maintainability wrappers
failed to import in **every** born clone. The gate's birth step now installs
the declared toolchain requirements file and fails loudly if that install
fails, and the smoke that exercises those wrappers had its
tolerate-the-absence guard removed — the toolchain is now asserted in-clone
rather than skipped. The requirements are declared separately from platform
runtime dependencies, deliberately: the platform boots without them, and
conflating the two is what hid this.

### The rename skill refuses a standalone label patch

Reported by an adopter, and the report was about a proposal rather than an
outage — which is the useful kind to catch. Their supervising agent observed
that the git-mutation gate reads only a local session-label file, and proposed
patching that file directly to satisfy the gate after a role claim's transport
had failed. That produces a session which *looks* like the claimed role to the
gate while holding none of the actual protection: the platform-side single-
holder claim is skipped entirely, so two sessions could hold the same name
with nothing refusing.

Both shipping copies of the rename skill now state the prohibition explicitly
at the local-label step: patching the label is the last lock-step action of a
**completed** platform claim, never a standalone act, and a failed claim is
answered by the skill's existing guidance — surface the error and stop, do not
reach the label step anyway. The two copies were confirmed to be the complete
shipping footprint by a whole-repository search for the skill's own body text,
correcting an earlier claim that a third vendored copy existed.

### The update runbook now says how you learn a release exists

The seed-update runbook opened by assuming you already knew a newer release
had been published, with no step anywhere saying how you would find that out.
A subscribe instruction did exist, but it was framed around feedback
notifications and buried past the update sequence rather than presented as
this runbook's trigger.

There is now a "How you learn a new release exists" section immediately after
"When to use this runbook": subscribe to the seed repository's releases
(Watch → Custom → Releases); a re-mint publishes as a dated release carrying
its notes; that notification is the trigger. The moved-home interaction is
wired directly into the step where it bites — an existing subscription does
**not** follow a repository move and goes silent from the old location the
moment the move takes effect, so re-subscribing at the new home is part of
that step rather than an afterthought.

### Smaller items

- **A seat's running log has a stated convention.** What such a log must
  contain previously existed only by example, which is how an obligation
  recorded in one goes missing when the session rotates. It is now written
  down as a knowledge-base article.
- **A gate invocation pins its environment.** One shipped gate step ran a
  smoke whose negative control cleared the current environment variable but
  not its pre-rename predecessor, so a stale variable left over in a shell
  could fail the gate even when the current one was set correctly. Both
  shipping copies of that invocation now clear the legacy variable
  explicitly. Traced to the single affected step by mechanism rather than
  applied blanket-wise.

### What this release covers

Not everything in this release's range is described above. The omissions are
named here rather than quietly dropped, so you can tell an absence from an
oversight.

**A repair you never needed.** Work in this range fixed a fault that existed
only between two publications and never reached a released seed. Describing it
would tell you about a problem your installation never had, and send you hunting
for symptoms that cannot be there. You receive the working version as the first
version.

**Changes that reach you as nothing at all.** The notes' own commits, which
cannot describe themselves; and a documentation landing confined to a directory
this seed never copies, which ships you zero bytes.

**A change that does reach you, and fixes something that was broken.** A setup
step in the Schwab hydration guidance used to point at a script that this seed
does not ship — so the instruction resolved for its author and resolved to
nothing for you. It now states the requirement directly: seed the client secret
by reading it at an interactive prompt, never through a command an agent
composes, so the value reaches no echo, no argv and no shell history. If you
ever followed that step and could not find the file it named, that is why.

The range itself is measured from the commit the **previously published** seed
was assembled from, not from the platform release that happened to be running
while this one was built. Those are different boundaries, and the running
release's would have re-announced work you already received in the previous
update.

---

## 2026-08-17 — Rotation notices, deploy-proof worker paths, and session listings that page

No breaking changes for standard configurations. Context-status rows and the
sweep's event stream gain new fields and event kinds (supersets of their
previous shapes); consumers doing exact-shape equality on either must update.
Update with the standard short form: `git pull --ff-only`, restart, wait for
startup to finish (`05_seed_update_runbook.md` is the complete procedure).

### Sessions are told when their context needs rotation

Long-lived agent sessions used to discover an over-budget context only by
symptom. The platform sweep now measures two conditions on its regular tick
and says so:

- **Rotation-due notices**: a session whose context gauge crosses its
  rotation band gets a notice composed onto the sweep tick, **latched** — one
  notice at condition onset, re-armed only when the condition clears, with an
  honest bound of at most one repeat per platform restart. A wiring guard
  fails loudly if the notice rider is present but unwired, so the feature
  cannot silently un-ship.
- **Gauge-coverage notices**: a live session with no context-gauge row at all
  is itself reported — absence of the measurement is a condition, not a blind
  spot.
- **The operator seat is covered from the other side.** The one session the
  sweep's delivery path cannot reach gets the same rotation-due notice from a
  prompt-submit hook in the coordination-hooks plugin (now 0.5.9). The two
  halves share provenance checks so a notice always names which surface
  produced it.
- **The gauge itself got honest**: each context-status row now names which of
  five hook surfaces wrote it (checkout, vendored, release, cache, unknown)
  and carries prompt-cache state, so rotation economics are measured rather
  than guessed. A collapsed "unknown" surface from earlier releases resolves
  into its real writer.

### Long-running workers survive deploys

Worker spawn used to pin the wake CLI and its registration sidecar to a path
inside a **versioned release directory**. Blue-green deploys reap old
releases, so every deploy silently broke every standing worker's wake path —
no error in any log, the worker just went quiet. The worker CLI path is now
expressed through the deployment's atomically-swapped `current` symlink at
the single resolution choke point all surfaces inherit, left **unresolved**
so it survives every future cutover, and the rewrite refuses during a
mid-cutover version skew rather than guessing. This applies at spawn time:
workers spawned before this release keep their versioned paths until
respawned. A knowledge-base article ships the full contract, including how
to read a live process's environment to confirm which form of the path it
actually holds. Separately, the spawned solet subprocess's PATH now carries
the wake CLI's own directory, closing a silent fail-open.

### Session listings page instead of refusing

On mature deployments, `list_sessions` read whole tables and collided with
the 10,000-row read cap introduced in the previous release — refusing loudly
with no filters, and on `source_kind` filters too. All fleet and
session-source membership reads now page or bound themselves, and the census
counts its two count-only tables instead of walking them. The read cap keeps
its job; the callers stopped deserving to hit it.

### Scheduler correctness across time zones and restarts

Cron triggers are evaluated in UTC, and a trigger whose fire time fell inside
a platform-down window now defers to its next window instead of erroring
permanently at startup. A time-convention knowledge-base article ships with
two worked incidents.

### Gates that tell the truth

The quality-gate suite now covers its own directory with the same linters it
enforces; a gate that crashes is reported as a crash rather than rendered as
a violation count; the secrets scanner no longer dies on a tracked absolute
or escaping symlink; and a contributor-listing verb serializes its datetime
fields with explicit offsets at the edge seam.

### Content and identity hygiene

Two shipped files carried reserved origin-identity tokens in prose; both are
scrubbed, and the census that catches the class ran clean on both profiles at
this mint. Shipped process-guidance citations that pointed at unshipped
working files now cite process keys. The dual-home seed release procedure
itself is now deterministic, so updates to both published homes of this seed
are one procedure rather than two improvisations.

---

## 2026-08-15 — Payload safety bounds, migration repairs, and workers that fail loud

No breaking changes for standard configurations; one health endpoint's
response body is a superset of its previous shape (details below). Update
with the standard short form: `git pull --ff-only`, restart, wait for
startup to finish (`05_seed_update_runbook.md` is the complete procedure).

### Oversized action payloads can no longer freeze the platform

An action payload large enough to spend hours inside the JSON parser could
wedge the entire action queue while `health` kept answering healthy — the
dispatch half of the platform froze, the messaging half stayed up, and
nothing said so. Four guards close the class:

- **A 16 MiB byte bound on action parameters**, enforced before anything
  parses: at enqueue, measured on the serialization every action already
  passes through, and again at claim, as a raw length check on the stored
  text. An oversized row found at claim time fails only itself; the rest of
  its batch dispatches normally.
- **`read_state` is capped at 10,000 rows** across all storage backends. A
  read that would exceed the cap is refused loudly rather than silently
  truncated — an over-large result is an error, never a prefix — with an
  explicit `unbounded=True` opt-in for callers that genuinely need more.
- **`GET /api/v1/bridge/health` now reports the action path.** The body
  gains an `action_path` block, and `status` may read `degraded` when work
  is waiting but the dispatcher has stopped claiming — exactly the
  signature that was previously invisible. The HTTP status code is
  unchanged (200), so code-only probes are unaffected; a consumer doing
  exact-body equality on the old shape must update.
- **A size-aware orphan reaper.** Rows stuck in `processing` well past any
  plausible dispatch age are failed with a legible reason when they exceed
  the byte bound, and returned to the queue only when they are small enough
  to be safe — an oversized orphan is never requeued to wedge the platform
  a second time.

One caller needed repair to live with the new cap, and the repair ships in
this same release: the memory store's tag reads scan the whole memory table
and filter client-side, and on a mature deployment that table exceeds the
cap (knowledge-base articles are memories). Those deliberate whole-table
reads now carry the explicit `unbounded=True` opt-in, and — the deeper fix —
every memory-store read now fails loudly on an error result instead of
rendering it as an empty result set. Before this repair, a refused read was
indistinguishable from "no memories exist", which is exactly how our own
first deploy of the cap failed its boot-time identity verification; the
error-swallowing predates this release, the cap merely exposed it.

### Router socket ownership — the rename-migration crash loop (#4)

Root cause of the silent crash loop reported after a solet rename: an
overlapping router restart (`bootout` + `bootstrap`, or `kickstart -k`,
which overlaps by design) briefly runs two routers against one socket path,
and the outgoing router unconditionally deleted the socket the incoming
router had just bound. The management socket is now owned by inode: stop
unlinks only a socket it still owns; start probes the path, waits out an
answering incumbent for a bounded window, and refuses loudly only past it;
and the platform child's readiness wait connects and asks instead of
testing bare path existence, naming the failure states apart. The
migration script also closes the race from its end with a bounded wait for
the old launchd label to clear before re-bootstrapping, and backfills
`StandardOutPath`/`StandardErrorPath` onto solet-owned launch plists that
predate log redirection — so a future failure of this shape is loud
instead of logless.

### Migration completeness (#5, #6)

`migrate_to_solet.py` now migrates the Claude Code MCP server environment:
`HOMUNCULUS_*` keys inside `mcpServers.*.env` in `~/.claude.json` are
renamed to their `SOLET_*` forms, idempotently, refusing fail-safe while
any Claude Code process is running, since Claude Code holds that file in
memory and rewrites it on exit (#5). A new `--scan-stale` mode is a
report-only sweep for surviving old-name references in user-authored docs,
config, and memory — three categories (historical, live-process-state,
fixable), never auto-rewriting anything, because a memory fact's filename,
slug, and links must move together (#6). The update runbook's Step 3a
carries the post-apply sequence for all of it.

### Spawned workers: named at birth, loud when deaf (#13, #8)

- A spawned worker's local session name now defaults to its durable role
  name, and the tmux spawn path passes the name through to the CLI — so a
  spawned Git-Controller is recognized by the git gate at birth, without a
  rename round-trip (#13). Spawning never claims the durable role binding
  as a side effect, and a second spawn for a role whose local name is
  already held live is refused loudly, naming the incumbent. A dead
  holder stays cheaply claimable — crash succession is deliberate.
- The silence half of #8: a worker whose registration hooks were stripped
  used to sit in `spawning` indefinitely, because the sweep found its host
  process alive and kept extending its deadline. A registration watchdog
  now stamps and loudly reports any row still unregistered past a bound,
  independent of host liveness, and a spawn preflight refuses a host whose
  managed policy strips hooks (`strictPluginOnlyCustomization` including
  `hooks`), failing open and loud on an unreadable policy file. The
  capability half — delivering worker hooks as plugin hooks so they
  survive that policy — awaits the probe results on #8.

### Heredoc bodies are data, not commands

The git-mutation guard tokenized heredoc body content as command tokens, so
prose about git inside a heredoc — documentation, commit-message drafts,
JSON payloads — could be blocked as a banned invocation. Heredoc bodies
are now treated as data in every shipped copy of the guard, and
`coordination-hooks` is bumped to 0.5.6 so previously installed copies
actually pick the fix up rather than staying dormant on the old version.

### Corrections

- The self-deployment plugin's socket-teardown documentation no longer
  claims asyncio unlinks the socket path on close; only the bind path was
  ever unguarded, and the inode-ownership fix above is complete on its own.

Issues answered by this release: #4, #5, #6 (closing round #3) and #13
(closing round #10). The capability probe for #8 and the reproduction for
#9 remain open on their issues.

## 2026-08-14 — Content hygiene, fleet-worker reliability, and a default deny for blocking choice prompts

No breaking changes and no migration steps. Update with the standard short
form: `git pull --ff-only`, restart, wait for startup to finish (the seed
update runbook, `05_seed_update_runbook.md`, is the complete procedure).

**Where this release lives.** The seed's canonical home is the new repository
announced in the previous release. As a transition courtesy this update is
also published at the previous home, which still faces archival — if you have
not re-pointed your clone's origin yet (previous release, Step 2a), do it with
this update.

### Content hygiene across shipped knowledge and examples

Shipped knowledge bases, process descriptions, code docstrings, and test
fixtures have been swept of creative-era terminology and off-domain examples.
One rename is behavior-visible if you consume audio quality reports (profiles
carrying the audio plugins): `evaluate_audio_quality`'s context-hint key family
is now the `modulation_posture` family — `modulation_posture`,
`primary_modulation_mechanism`, `secondary_modulation_mechanisms` — renamed
consistently across code, process documentation, and fixtures. Callers passing
the previous key names get no error — the hint simply no longer matches — so
check any stored evaluation contexts against the currently documented keys
along with the pull.

### Blocking structured-choice prompts are now denied by default

Claude Code's `AskUserQuestion` tool renders a multiple-choice picker that
holds its session until a human answers; its auto-continue timeout defaults to
`"never"`. In unattended, worker, or peer-driven sessions that is a silent
stall — the session looks idle while it waits for a click nobody knows it
wants. The session launcher now passes a small settings overlay
(`<clone>/client/claude-session-overlay.json`) that denies the tool by default.
Overrides, in order of reach: `SOLET_ALLOW_ASKUSERQUESTION=1` for one attended
launch; remove the overlay flag from your rendered launcher to make the picker
your standing default; or keep the tool and set `askUserQuestionTimeout` in
your own settings so unanswered dialogs eventually continue. Doctrine and
rationale: the fleet-launcher session-configuration article
(`24_operator_communication/06`).

### A lighter default local model for new births

Newborn solets now default to `qwen/qwen3-14b` as the local inference model
(previously `qwen/qwen3-30b-a3b-2507`), with the request cap matched to its
32k context window. Any equivalent ~14B-class instruct model served by your
local endpoint works — set your preferred model id in the inference plugin's
config after birth. Existing deployments are untouched: this changes only
what a fresh birth materializes from the profile baseline.

### Background connector jobs — where the result goes

Connector writes and exports (Google Workspace, Snowflake, external Postgres,
and similar) dispatch as background jobs: the call returns
`{"job_id": ..., "status": "queued"}` immediately, and the finished result is
delivered as a message to a live listening session. A session that dispatched
through the plain `<name> call` CLI holds no listener, so it fetches the
finished payload itself with the `get_latest_job` process — filter by plugin
and verb; a completed job's `result` field carries the payload (a created
sheet's URL, an export's destination). The generated `CLAUDE.md` /
`AGENTS.md` bootstrap files and the Google Workspace plugin's knowledge base
now carry the exact command shape, so sessions learn this without being told.

### The GitHub CLI joins the hydration tool ladder

The `/feedback` skill files upstream feedback through the repository's issue
forms via `gh issue create --web`, so the GitHub CLI was a real dependency
with no install step. Hydration now probes for `gh` (Step 1), offers
`brew install gh` under your normal approval flow (Step 2), and verifies it
(final checklist). Declining is recorded and tolerated; the skill re-states
the dependency at first use instead of failing silently.

### Spawned fleet workers register reliably

Workers spawned through `spawn_session` now register on the watch transport
automatically (measured ~0.6 s after their ledger row), with absolute CLI
paths through both adapters and a message spool from the first moment. The
session reaper now observes actual process liveness before reaping a spawning
row, applies bounded patience, and emits an explicit notice instead of
silently killing a live worker whose registration was lost to transport
churn. The paste-stability wait in the tmux and codex driver channels is
baseline-gated, closing a class where a slow-rendering paste could confirm
"stability" on the pre-send screen and fire Enter before the text existed.

### Retrieval verification: a runner, a revived daily audit, and a seal gate

- The knowledge plugin's retrieval-audit verb now measures every KB article's
  `.retrieval_test.yaml` companion claims against the live index — a
  post-deploy instrument, not a commit gate. (The origin also carries a thin
  CLI runner over the same discovery; that script is maintainer tooling and is
  not part of the seed.)
- The daily retrieval audit is repaired and running again, now distinguishing
  stale process keys and legacy-key claims from genuine content drift.
- Sealing now runs an assemble-time cited-path gate: a shipped document that
  cites a path the bundle does not carry fails the mint instead of shipping a
  dead instruction. Known-tolerated citations live in a reviewed allowlist
  that ships with the gate.

### Operations knowledge

The joseki catalog gains cards for scoped landings, managed-worker dispatch,
and the full remint-and-respond release cycle; the maintenance-verbs article
gains a seat self-rotation card, and the fleet-launcher article carries the
measured procedure (and its traps) for a session clearing its own context via
a delegated helper. The SQL access gate now resolves its canonical
configuration when invoked bare, removing a phantom-red class from fully-bare
invocations.

---

## 2026-08-13 — BREAKING: the homunculus→solet rename, a new feedback channel, and the seed's new home

**This is the final release published at this repository.** Everything from
here is published at the seed's new home. The move is the last section of this
entry, and it needs one command from you.

### BREAKING — every live identifier is renamed, and a migration step is mandatory

The platform's own name for a deployment changed from `homunculus` to `solet`,
and the rename reaches every live identifier. There are **no compatibility
aliases**, by design: an alias layer here would have to survive in every
launchd plist, shell function, and config key indefinitely, and a rename that
half-works is worse than one that fails loudly.

What renames:

| Surface | Was | Is now |
|---|---|---|
| CLI console script | `homunculus …` | `solet …` (`solet call`, `solet health`) |
| Environment family | `HOMUNCULUS_*` | `SOLET_*` (`SOLET_NAME` foremost) |
| Root manifest key | `homunculus_name:` | `solet_name:` |
| launchd labels | `local.homunculus.<name>` | `local.solet.<name>` |
| Lifecycle verbs | `birth_homunculus`, `teardown_homunculus`, `provision_homunculus` | `birth_solet`, `teardown_solet`, `provision_solet` |
| Entry-point flag | `--homunculus` | `--solet` |

Result envelopes, error codes, and process JSON follow the same rename.

**The migration is mandatory and it is not optional bookkeeping.** A code pull
does not rewrite your LaunchAgent plists, launcher shell functions, venv
console script, or root manifest — that is deployment state, and it still
carries the old names after you pull. The platform refuses to boot when it
finds the old environment or manifest keys, on purpose, with an error naming
the step. Run this BEFORE restarting, dry run first:

    <clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py
    <clone>/.venv/bin/python3 <clone>/deployment/scripts/migrate_to_solet.py --apply

It is idempotent — re-running reports already-migrated pieces and changes
nothing. Full procedure, including the residual config-key guard to run after
the platform is back, is Step 3a of the seed update runbook
(`05_seed_update_runbook.md`).

**A note on that script, because it affects what "update" means for you.** The
migration script was not included in the shipped file set of any earlier
release — it existed only in the maintainers' own checkout. An adopter updating
across the rename boundary would have hit the refused boot, been pointed at
Step 3a, and found no such file in their clone, with no documented recovery.
This release ships the script. If you are reading these notes before updating,
you are ahead of that problem and there is nothing extra to do; the sequence
above is the whole story.

**What does NOT change — do not "fix" these.** Each of these looks like a
missed rename and is not:

- **The messaging wire route `notifications/homunculus/peer_message` and the
  channel `source="homunculus"` attribute.** These are protocol names on a
  wire with two independent implementations. Renaming them is a versioned
  protocol change, deliberately deferred — not an oversight. Changing them
  locally will break your messaging.
- **AWS-side resource names** — load balancer, bucket, registry, IAM, and tag
  values keep their existing spellings. Renaming deployed cloud resources is a
  migration with its own blast radius, out of scope for a rename release.
- **History, in every form.** Ledger rows, memories, existing tag values,
  persisted enum values, dated working notes, and past release entries keep
  their original spellings. History is a record of what was true, and
  rewriting it to match current vocabulary would make it a worse record.

Knowledge-base articles were renamed in place. Because a rename is a delete
plus an add, the startup staleness check cannot see the deletion half — the
update runbook's Step 6 re-install with a negative-search verification applies
to this release.

### Closing items you raised

**The `SessionStart` reminder that silently never fired — closed, fixed,
and the class killed.** A cadence change rebound the knowledge-base-first
reminder from `UserPromptSubmit` to `SessionStart` in both runtime variants'
wiring, but the scripts kept emitting a hardcoded `UserPromptSubmit` event
name. Claude Code rejects hook output whose declared event name mismatches the
firing event, and it reports that rejection at debug level only — so the
reminder silently never landed on any session start, in either runtime, while
every test stayed green because the suites asserted the stale literal as the
expected value. The defect was rendered in its own assertion.

This came in as adopter feedback, with a before/after `claude --debug -p`
capture showing exactly one `SessionStart` hook failing every time, plus a
proposed diff and a recommendation to derive the expected event names from the
wiring rather than restating them. All of it was correct, and the
recommendation was adopted as the actual fix shape. Canonicalized upstream in
`392e93130`: the reminder now echoes the event name from the hook's own stdin,
and both suites gained a check that derives every reminder's expected events
from `hooks.json` itself and fails if an emitted tag desyncs from the wiring —
so the assertion can no longer drift away from the configuration the way it
did here. Verified by deliberate red-mutation in both runtimes, not by
observing green. Thank you for the report; a defect that is invisible from the
origin and only visible on a real install is precisely the kind we cannot find
without you.

**The hydration runbook's retrieval self-test — closed.** The 2026-08-12
entry acknowledged that 7 of the hydration runbook's retrieval queries no
longer reached its articles at the expected rank, and stated that the entry
would close when the repair landed. It has landed: all 7 queries pass, and the
repair was checked against the neighbouring runbooks to confirm it did not
demote any of their ranks in the process. The runbook's steps were always
correct to follow; this was search reaching them, and it now does.

**The two intermediate re-mints.** The releases dated 2026-08-12 in this file —
the coordination-hooks 0.5.4 documentation-drift fix and the removal of the
origin's creative working corpus from the shipped thinking knowledge bases —
shipped without a direct reply to the deployments whose reports prompted them.
Those entries are the formal disposition; nothing about them is still open on
our side. If you raised something in that window that you cannot find answered
in this file, it fell through — re-raise it by number through the channel
below and it will be treated as open, not as a duplicate.

### The feedback channel changed shape

Feedback is now filed as **GitHub issues through the repository's issue
forms**, rather than as a pull request carrying a numbered document. Design
proposals are the exception and still travel as a pull request.

> **Superseded 2026-08-15 — read this before acting on the paragraph above.**
> The design-proposal exception is gone. **The repository accepts no pull
> requests and no patches at all**; an RFC-shaped design proposal is now a
> **feature-request issue carrying the design in its body**. The reason is what
> a solet does to the machine that installs it — provisions a database, writes
> keychain credentials, installs a background service, edits the shell — which
> makes merging unreviewed code a supply-chain path into every downstream
> install. Bug reports remain wanted; Apache-2.0 still lets you fork and change
> your own clone freely. See `CONTRIBUTING.md`. The entry above is left as
> written because a changelog records what a release said at the time, and
> rewriting it would erase the change this note describes.

What did not change: rounds are still `Part N`, items are still `§N.M`, the
four item classes are the same, and the evidence and content rules are the
same. To move: instead of writing one document per round and opening a pull
request, open the repository's issue chooser and file one issue per item using
the form for its class — the forms ask for the command, the observed output,
the expected behavior, and the release you measured against, so the evidence
you were already writing now has named fields. A multi-item round gets a parent
issue with the items attached as sub-issues; a single-item round is just the
one issue. Answers arrive as issue closures, and fixes land with `Fixes #N` so
the commit is reachable from your item. **Subscribe to the new repository's
releases** — that is now how you learn a round was answered, and how you learn
a release exists at all. The rewritten `07_upstream_feedback_runbook.md` in
this release is the full procedure.

### coordination-hooks 0.5.5

coordination-hooks 0.5.5 (Claude Code plugin): the session-start knowledge
reminder now states the two-source sequence — the platform knowledge base
first, then the working directory's own docs — rather than presenting them as
alternatives; neither source replaces the other. New test legs pin the
sequencing claim and the source order, and were red-mutation verified. The
Codex twin carries the same sequencing literal (build
`0.1.0+codex.20260814002958`), without the async clause because its CLI lookup
blocks by design. The 0.5.4→0.5.5 version bump is what makes installed copies
refresh: after pulling this update, run `claude plugin uninstall
coordination-hooks@<your-marketplace>` then `claude plugin install
coordination-hooks@<your-marketplace>`, and verify the `installPath` printed in
`~/.claude/plugins/installed_plugins.json` exists on disk (the update runbook's
three-outcome table explains why an in-place update without the uninstall can
silently keep stale bytes).

### THE MOVE — this repository is retired after this release

Going forward the seed lives at:

    https://github.com/solet-public/macos-bizops

The new repository was seeded with this repository's history before this
release published there, so the two share a common ancestor. Your move is one
command, not a re-birth: nothing your deployment has become — database,
memories, knowledge, credentials, LaunchAgents — is affected, and your next
update is an ordinary fast-forward pull.

Take this release's update from here FIRST (it published to both repositories),
then re-point:

    git -C <clone> remote get-url origin                    # note your current URL
    git -C <clone> remote set-url origin https://github.com/solet-public/macos-bizops
    git -C <clone> remote get-url origin                    # confirm it took
    git -C <clone> fetch origin                             # prove the new home is reachable

If that `fetch` fails with an access or not-found error, put your old URL back
and talk to whoever administers your deployment's access — access to the new
home is granted per-deployment. A clone pointed at a repository it cannot read
has no update path at all, which is worse than where you started.

**If you skip this, nothing breaks today and you stop receiving updates
forever.** A clone still pointed here will report "already up to date" on every
future pull, which is indistinguishable from "there have been no new releases."
That is the failure this section exists to prevent. Full procedure, including
the end-of-update verification, is Step 2a of `05_seed_update_runbook.md`.

---

## 2026-08-12 — the seed no longer ships the origin's creative working corpus, and deletion-only KB updates get an explicit re-ingestion step

**What was removed, and why.** The platform's thinking plugin
(`default_thinking_plugin`) doubles as a working store for authored
thinking artifacts, and the origin's own store had accumulated its
pre-product creative-domain corpus — composition designs, sketch
packets, dated working plans, WBS specifications, and one legacy plan
template. None of that is product surface for a deployment, but the seed
shipped it wholesale (91 of the thinking KB's 93 files), and the two
indexed slices (`plans/`, `wbs/`) went straight into every newborn's
retrieval. The seed manifest now excludes the corpus: the KB
registrations and the generic thinking system prompt still ship, the
artifact directories start empty exactly as a newborn's own store
should, and nothing else in the plugin changed.

**If you update an existing deployment, one extra step.** This is a
deletion-only KB change, and the startup re-index cannot see one: it
re-indexes when a surviving file changed, and here no surviving file
changed. After pulling and restarting, re-install the two affected
knowledge bases (idempotent; drops each KB's chunks and re-indexes from
the files on disk), then verify with a negative search — full procedure
in the seed update runbook (`05_seed_update_runbook.md`, Step 6, fourth
stale copy):

    solet call service_interface::knowledge_service::install '{"name": "thinking_plans"}'
    solet call service_interface::knowledge_service::install '{"name": "plan_templates"}'

Born-fresh deployments need nothing: the corpus was never load-bearing
for any shipped behavior, and no shipped test reads it.

## 2026-08-12 — coordination-hooks 0.5.4: the README caught up with the bounded wake wait, and doc/argv drift is now a tested class

**Docs-only release — no hook behavior changes.** The 2026-08-09
bounded-wait fix changed the idle-wake waiter's invocation from
`$AGENT_WAKE_CLI wake` to `$AGENT_WAKE_CLI wake --max-wait <seconds>`
(bounded so the harness can stamp an idle session at all; a delivery still
wakes immediately). `SECURITY.md` was updated with that fix; `README.md`
was not — it kept describing "the single fixed argument `wake`" and never
named the `AGENT_WAKE_MAX_WAIT_S` override. For a plugin whose documents
ARE the security review, a doc describing an argv the code doesn't run is
a defect even when the code is right. Both README passages now describe
the real argv and the override variable.

**What prevents the class.** The manifest smoke gains
`check_docs_name_the_bounded_wake_argv`: it derives the waiter's argv
tokens from the source literal and fails unless every prose surface names
them (and refuses the stale unbounded form outright) — the same
derive-from-the-wiring pattern as the 0.5.2 hotfix's
`check_manifest_bound_events_echo`, which ended the hardcoded-event-tag
class the same way. Plugin version 0.5.4; existing installs pick it up
with `claude plugin update coordination-hooks@<marketplace>` (the plugin
cache is version-keyed — an un-updated install keeps the stale README, and
its hooks were already correct).

**The Codex variant carried the same class twice over — both fixed.** Its
`wake_waiter.js` still spawned the unbounded `["wake"]` argv: the
2026-08-09 bounded-wait fix had only reached the Claude variant, so a
quiet Codex session's Stop hook still blocked on the CLI's ~23.9h default.
And its README described an arming rule ("`FLEET_TRANSPORT` is exactly
`watch`") that the source and its own arm-matrix smoke retired on
2026-08-06 (unset and empty also arm). The bounded wait is now ported —
same compiled-in default, same `AGENT_WAKE_MAX_WAIT_S` override contract,
announced fallback on a malformed value — both documents describe the real
argv and arming rule, and the smokes pin the bounded argv at source and
behavior level. Fresh cachebuster version; re-add the plugin and re-trust
the changed definitions per the Codex README.

**Known issue, acknowledged rather than left for you to find.** The
hydration runbook's (`01_hydration_runbook.md`) retrieval self-test is red
on master: 7 of its queries no longer retrieve the runbook's articles at
the expected rank, A/B-verified as pre-existing drift rather than
something the 2026-08-11/12 releases introduced. The runbook's content and
steps remain correct to follow; the gap is in search reaching them. A
repair lane is queued, and this entry closes when it lands.

---

## 2026-08-11 — the feedback channel is now official, and your deployment gets a report card

**Upstream feedback has a shipped runbook now.** *(Historical: the
pull-request delivery described here was replaced by GitHub issue forms on
2026-08-13, and pull requests were declined entirely on 2026-08-15. The item
conventions below survive both changes unchanged.)* Adopters invented the
pattern this release formalizes — pull requests of dated, numbered feedback
documents against the seed repository — and it worked well enough that it is
now the documented channel, with the conventions that made it work stated
explicitly: monotonically numbered parts with per-item numbering so every
item is individually answerable, defect/question/feature-request/closure
classification, evidence discipline (the command, the output, the release
measured against), an outbound content gate (no personal or employer
identifiers, credentials, or business records — ever), outcome-first feature
requests, and "is this a deliberate no?" as the sanctioned way to re-raise
an unanswered item. Release notes — this file — are the primary response
surface: items you raise land here when they land. The runbook is
`plugins/github_midwife_plugin/knowledge_base/07_upstream_feedback_runbook.md`.

**New: the deployment report card.** A canonical definition of a
fully-deployed solet — four tiers, each component with the evidence
command that proves it configured — and the operator-facing card your
driving agent produces against it: what is set up, what remains, what each
remaining item would give you, and one recommended next step. Delivering the
card is now the mandatory close-out of hydration, re-run after every update
and whenever you ask "is everything set up?". The definition lives in
`plugins/github_midwife_plugin/knowledge_base/08_deployment_report_card.md`.

**Honest reframing: the session ledger was never optional, and our own docs
said otherwise.** Both session-source plugins' hydration guidance described
an empty session ledger as "a fully supported steady state", and ledger
setup was not a numbered step in the hydration ladder at all — which is
exactly how deployments end up with no cross-session memory and nothing
visibly broken. The hydration runbook now carries session-ledger ingestion
as its own core, consent-gated step (Step 4d, covering every coding agent
you use, verified by retrieval rather than registration), the guidance
files state plainly that consent-gated is not the same as optional, and a
declined ingestion stays visible on the report card instead of silently
normalizing. The same applies to tmux worker hosting for fleets: a
tmux-hosted worker survives platform updates, a headless one does not, and
the report card now says so.

**Also: the session-start memory block no longer loads twice.** Deployments
wired both ways — the checkout's own memory-passthrough hook plus the
installed coordination-hooks plugin, which is the `setup_clone.sh` default —
paid the full HYDRATE/DRAIN instruction block twice on every session start,
citing two divergent sibling-script paths. Both copies now carry a
checkout-copy-wins guard: the plugin copy stays silent in a project whose
checkout carries its own emitter, and still emits everywhere else (born
clones without the local wiring are unaffected). Plugin version 0.5.3;
existing installs pick it up with `claude plugin update
coordination-hooks@<marketplace>` (the plugin cache is version-keyed — an
un-updated install keeps double-emitting indefinitely).

---

## 2026-08-11 (hotfix) — the cadence release left the KB-first reminder silently undelivered; fixed, both runtimes

**What was broken.** The cadence release below moved the reminders to
`SessionStart` but left `step_zero_reminder`'s own emitted `hookEventName`
hardcoded to the old `UserPromptSubmit`, in both runtime variants. Claude
Code discards hook output whose declared event name does not match the event
that fired — and the rejection is visible at debug level only — so the
knowledge-base-first reminder stopped landing entirely: installed no longer
meant delivered, the exact silent-absence class its always-armed ruling
exists to prevent. Found live in the origin fleet and reported independently
by an adopter (with the debug-level rejection line) within the same day.

**The fix.** Both scripts now read `hook_event_name` off stdin and echo it
back, exactly like their `check_messages_reminder` siblings, defaulting to
their own `SessionStart` binding. The Claude plugin is version 0.5.2 —
existing installs pick it up with `claude plugin update
coordination-hooks@<marketplace>` (the plugin cache is version-keyed, so an
un-updated install keeps the broken 0.5.1 copy indefinitely). The Codex
variant carries a fresh cachebuster suffix; re-add the plugin and re-trust
the changed definitions per its README.

**Why the suites missed it, and what now prevents the class.** Both reminder
smoke suites asserted the stale literal as the expected default — the defect
was rendered in the test's own assertion. Each suite now carries
`check_manifest_bound_events_echo`, which derives every reminder's expected
events from `hooks.json` itself and asserts the emitted tag matches, so a
hardcoded tag that desyncs from the wiring is a named red in both runtimes.

---

## 2026-08-11 — reminder-hook cadence + managed Codex worker runtime

**The coordination-hooks reminders now fire once per session, not once per
prompt — both runtimes.** The Step Zero and check-messages reminders were
bound to every prompt submission; every copy also accumulates in the session
transcript, so long sessions carried dozens of duplicates in context. Both
reminders now fire at session start (and again on resume and on a context
clear), in the Claude and Codex plugin variants alike. The always-armed
property is unchanged — installed still means armed, with no environment
gating; only the cadence moved. If you review or ship these hooks downstream,
the per-prompt binding is gone from both `hooks.json` files, and re-adding one
is a named failing mutation in the shipped smokes.

**New capability: managed Codex workers (opt-in).** `spawn_session` gains
`agent_runtime` (`claude_code`, the default, or `codex`) — the fleet's session
lifecycle (spawn / drive / clear / compact / terminate) now runs Codex CLI
workers on both host drivers, with a fresh-backend-thread translation for
context clears and the same ledger contract as Claude workers. Two honest
bounds: provider selection is rejected for Codex workers (fail-loud), and
**a Codex spawn currently requires an explicit Codex-valid `model`** — an
omitted model falls through to a Claude-vocabulary default that the Codex
runtime rejects at first inference with a hard API error (measured on a live
spawn; the fix — per-runtime model defaults — is queued for the next update).
Nothing changes for any `claude_code` path — the capability is inert unless
you pass `agent_runtime="codex"`.

---

## 2026-08-10 (second update) — response to adopter feedback Parts 36–40

Five feedback parts arrived after this morning's release; every defect they
reported is fixed in this update, and the capability they requested is
accepted with its design complete. All four fix packages below were verified
with named failing mutations (the fix reverted reproduces the exact reported
failure), and the born-clone items against a born-clone-shaped fixture rather
than a development checkout.

**The vault-read envelope bug is fixed — bearer tokens survive restarts now
(§36.2).** The bridge's bearer-token HMAC signing key was silently re-minted
on every restart because the read path checked for a `status` field the vault
backend never returns, so an existing key was never recognized. Any deployment
with Streamable-HTTP MCP bearer auth enabled was invalidating every
outstanding client token on every boot — including, we measured after your
report, the origin deployment itself. A single vault-read seam now keys on the
envelope the vault actually returns, a malformed envelope fails loud instead
of reading as a miss, and the shipped smoke's fake vault returns the real
envelope shape so this class cannot false-clean again. The audit you asked for
ran costume-aware across the tree: one more same-class hit fixed, ten
lookalike sites swept and documented benign.

**Born clones can spawn workers on every host driver (§36.3, both drivers).**
`verify_config` required `.mcp.json` to exist even for watch-transport workers
that receive an inline empty MCP config and never read the file — so the very
first spawn on a fresh clone refused. Both the headless and tmux drivers (the
latter carried the identical check, found in the same audit) now require the
file only when the resolved transport is `mcp`, and the refusal message states
exactly what satisfies it.

**Your tmux third-party-provider fix is canonicalized (§39.1 / §40.1).**
Taken exactly as you field-verified it — the inert dev-channels flag is
omitted rather than waited on; with the flag absent the confirmation
expect-loop is never entered, and the first-party path is byte-for-byte
unchanged. Two deliberate bounds, stated honestly: the predicate keys on the
effective spawn environment (rather than a provider argument our tree does not
carry yet), and its marker set is exactly the one with live evidence behind
it — your verified Bedrock spawn. Other third-party provider families get
their markers when the accepted §36.1 registry lands, sourced from vendor
documentation rather than inference; the code comment names that extension
path. The headless driver keeps the flag with a documented rationale (it has
no confirm loop, so the flag is inert there with no failure mode).

**Both LaunchAgent plist renderers now emit a PATH (§39.2 / §40.2).** A
launchd daemon inherits the bare system PATH, so Homebrew-installed binaries
(tmux first among them) were invisible even when correctly installed, and the
recovery required exactly the hand-edit-a-live-plist procedure your report
described as ugly — it was. Both the self-deployment and genesis-time
renderers now write a deterministic PATH with the Homebrew locations ahead of
the system defaults.

**A born clone can run its own commit gate (§37 / §38).** The gate
orchestrator hard-referenced two paths the seed never shipped
(`deployment/scripts/check_gate_toolchain.sh` and the root `pyproject.toml`)
with no existence guard — a raw traceback before a single gate ran. Both are
now guarded with clean fail-loud messages AND shipped in the seed. The root
cause you ran down in Part 38 is fixed as scoped there: the gate toolchain
(`ruff`/`pyright`/`radon`) lives in a deliberately-mandatory `ananta[gate]`
extras group the birth-time provisioner never installed; it now installs that
group specifically, without sweeping in any plugin's deliberately
absence-tolerant extras.

**Per-spawn provider selection is accepted upstream (§36.1) — design
complete, implementation scheduled.** Your layering survives review intact:
the shim resolves, the verb stays secret-free, strip-then-set, credentials
vault-resolved and never persisted. The accepted design generalizes it to a
declarative registry covering the six vendor-documented provider families
with an operator extension path, validates model identity per provider
family, and persists the provider name (never credentials) so a restarted
worker keeps its provider. Two findings from review you may want locally in
the meantime: `-e VAR=""` on tmux sets rather than unsets (the canonical
version uses a real env unset), and a restarted worker under your variant
silently reverts to the daemon's provider.

**Queued, not in this mint (§36.4):** the three Marketo asks (vendor error
code surfacing, in-plugin retry on idempotent long-window reads, documented
per-verb `row_limit` ceilings) are filed with the existing Marketo package to
land together.

---

## 2026-08-10 (first update) — multi-session self-management

**Update your solet.** This release adds the multi-session
self-management capability described below — your solet can now
spawn, monitor, hand off work between, and safely retire other agent
sessions of itself, with an operating manual (the maintenance-verbs joseki
cards) that ships in the same update. If you've been running a single
always-on session, updating is what makes the fleet capability available
to it; nothing about your current setup breaks if you don't use it yet.
The seed's own update runbook (`plugins/github_midwife_plugin/knowledge_base/05_seed_update_runbook.md`,
or its plain-language companion `06_seed_update_operator_guide.md`) walks
an already-running solet through picking this up — point a coding
agent at it and ask it to "update me."

This is the first release-notes package shipped with this seed. Previous
updates carried no equivalent document, which meant a deliberate scope
decision and a plain omission looked identical to anyone reading from
outside — this document exists to close that gap, going forward as a
standing practice, not just for this release.

## Why you should update

Four real defects are described below. Two are already fixed in this
release; two remain open, each disclosed with its exact scope. Two of the
four were found through actual use of this platform outside its original
environment — not discovered internally — which is itself worth knowing:
real use surfaces real defects that review alone did not catch. Separately,
one change below alters how you should interpret your own gate output going
forward, even though nothing in your own setup changes — worth reading
regardless of whether you act on anything else here.

## What changed

**A cloned seed now receives a working `.gitignore`.** The birth step that
writes `.gitignore` sat behind a check for whether a `.git` directory
already existed, intended to avoid clobbering an existing setup — but a
seed obtained by cloning its repository *always* already has `.git`, so
that check was always true and the file was never written, for every
adopter who got the seed the normal way. The write now happens regardless
of which shape of tree it's running against, and an existing `.gitignore`
is still never overwritten. **If you update an existing clone, the file
will arrive untracked** — genesis must never touch your git history, so
it writes the file to disk without staging or committing it; seeing a new
untracked `.gitignore` after updating is correct, not a symptom of
anything. Commit it yourself when convenient. If you already have your
own `.gitignore`, this change does not touch it. Without this fix,
`__pycache__` directories, your `.venv`, and other runtime state stay
unfiltered from git's view — one `git add -A` away from being committed
alongside the credential and vault material a proper `.gitignore` is
supposed to keep out.

**The hydration template now states Step Zero as a first action, before
its first use.** The template previously referenced "Step Zero" three
times without ever defining it, and stated the knowledge-base-search
expectation in a way that read as advisory — easy to read past under
time pressure, including for tasks that look like plain code or config
work, or where the answer feels already known. A short block now opens
the template, naming Step Zero before anything else references it and
stating plainly that acting from prior assumptions, source reading, web
search, or MCP tools before searching the solet is a defect, not a
shortcut. If you've been carrying a local edit to this template to
strengthen the same instruction, you can drop it — diff first if you want
to confirm nothing else in your local version is worth keeping.

**Gate output now tells you the difference between "passed" and "declined
to run."** Previously, a smoke test that detected a required tool or
service was missing — an absent type-checker, an unavailable optional
dependency — printed its own line saying so, exited 0, and was counted as
a pass at every level: the per-entry line, the aggregate total, and the
process exit code. The text disclosing the skip was captured and then
discarded unless the smoke also failed outright. **If you have ever read
an `N/M passed` figure from this platform's gate output, some unknown
fraction of that N may have been declined smokes, not passing ones, and
nothing in the output told you which.** The runner now reports passed,
skipped, and failed as three distinct outcomes, with a dedicated exit
code for a disclosed skip and its own count in the summary line, plus an
optional strict mode to treat any skip as a hard failure for callers that
want zero tolerance. Re-read your gate output after updating; a skip is
now visibly a skip. Nothing needs to be redone for a prior run — the
number wasn't wrong, it just meant less than it looked like it meant, and
now the meaning is legible.

**Multi-agent session management** — spawning, monitoring, and
coordinating other agent sessions from your own — gained substantial new
capability in this release: session lifecycle tracking through a durable
state machine, dispatching follow-up work into an already-running
session rather than only at spawn time, formal handoff of a durable named
role from one session to another, two ways to run a spawned session
unattended (fully non-interactive, or driving a real interactive
terminal), a registered-presence message transport as the new default for
newly spawned sessions, automatic rotation for a session approaching its
context limit, and per-session token usage accounting. None of this
affects a solet that never spawns other sessions.

**Session-registration behavior clarified — a registration and a live
watcher process are different things.** A durable role registration
(a name like "the session currently speaking for X") can outlive the
process that claimed it; a live watcher process is what actually holds
the delivery route. Specifically: arming a second watcher *while one is
genuinely running* is refused, naming the running process. Arming when
*no watcher process is alive* — even though a registration row still
exists — is permitted, and re-points the delivery binding to the new
arm. That second case is the intended reconnect path, not a missed
refusal: a successful arm is itself evidence nothing was actually holding
the lock. If you're addressing sessions by a registered role name and
something seems unresponsive, check whether a watcher process is actually
alive, not just whether the role is claimed — those are two different,
both-legitimate states.

**Session-ledger data-integrity fixes.** Deterministic handling of two
rows describing the same external event (previously outcome depended on
read order), a repair path for a source that produced duplicate records,
a way to safely disable a source without deleting its history, and a
declared natural key on the event table so a given external event can't
be recorded twice even under concurrent ingestion.

**Postgres connections can now write, if the credential you registered is
allowed to.** Previously every Postgres connection this platform opened was
hard read-only — no write verb existed at all, regardless of what your
database credential could do. That blanket restriction is reversed: one new
verb, `run_statement`, opens a connection without the read-only flag and
executes whatever statement you give it. What it can actually do there is
entirely your own database's decision — this plugin performs no
write-permission check of its own; your Postgres GRANTs are the only
control plane. If you want a connection that can never write no matter what,
register it under a read-only database role; that boundary now lives in
your database, not in this platform. Every existing read verb is completely
unaffected — reads still open strictly read-only connections, exactly as
before. **Snowflake gets the same reversal in this release**, one new verb
`run_statement`, same design: no plugin-side write-permission check, your
registered role's own grants are the entire control plane. One structural
difference from Postgres worth knowing if you rely on it: Snowflake has no
session-level read-only connection flag at all, for either read or write
verbs — Postgres's read verbs structurally refuse a write at the server
even for an over-privileged credential, Snowflake's do not (the boundary
there is entirely which grants your registered role has, for both read and
write verbs alike). **Two things about the Snowflake write verb are
disclosed as open, not fixed by this release** — see Known Issues below.
Salesforce, Zuora, and Jira were already write-capable before this release
and are unaffected either way.

**Internal hygiene: identifying information removed from source and test
files.** A number of source comments, docstrings, and test fixtures
across several plugins previously cited internal review references by an
identifier scoped to a specific deployment, or carried that deployment's
operator's personal name, email, or account handle as literal values.
All of it has been removed or replaced with synthetic placeholders where
a realistic-looking value was needed for a test to remain meaningful.
None of this shipped in any previously-sealed release. No behavior change
results, except one environment-variable rename and one default-value
removal, both confined to test-only or fallback code paths.

**The coordination-hooks plugin previously required `node` for four of its
five hooks; this is now fixed and independently re-verified by this
lane against this checkout's own actually-installed plugin copy, not just
the source tree.** As disclosed in an earlier revision of this document,
this platform names Python 3.13 as its only dependency but four reminder
hooks (a knowledge-base-first prompt and a check-your-messages prompt on
every submitted prompt, a check-your-messages prompt and a role-binding
prompt at the start of every session, and an opt-in idle-wake waiter at
the end of a turn) were invoked via `node` regardless — on a node-less
machine those four silently did not run, worst of all for the idle-wake
waiter, whose correct steady-state behavior (wait quietly, no output) is
indistinguishable from that failure. **All four have since been ported to
`python3`** and the old `.js` originals removed (source commit
`1dc804b00`, 2026-08-08). Re-verified independently by this worker two
ways: (1) direct read of the shipped `hooks.json` — every one of its five
hook entries now invokes `python3`, none `node`; (2) a byte-level check
against this checkout's own **installed Claude Code plugin cache** at
`~/.claude/plugins/cache/<marketplace>/coordination-hooks/0.3.0/hooks/hooks.json`
— the actual file the plugin loader executes, not the source tree — which
confirms all five commands there are `python3` too. The landing commit
itself flagged an owed live at-path verification before this seed's next
mint; this check is exactly that verification, for this machine, and it
passes. If you update from an older install, this fix reaching your own
running copy still depends on the plugin-cache refresh step in the seed
update runbook (the cache is version-keyed and does not update on its
own) — that mechanism is unchanged by this fix, only the defect it used
to carry.

**The coordination-hooks plugin gained two more hooks, then a fourth
capability set, in this release (`0.4.0` then `0.4.1`).** `0.4.0` vendors
a heartbeat and a rotation-due watcher directly into the plugin, both
wired as `PostToolUse` hooks; `0.4.1` — the same commit — additionally
vendors the memory-passthrough capture/session-context pair and a
new origin-resolution ladder for deciding which solet a session's
memory writes belong to. All of this is part of this release's
self-management enablement work — see "Update your solet" above and
the seed's own update runbook for the exact hook list and the
`SOLET_NAME` export this ladder's first rung depends on. **Not yet
tested end-to-end on a fresh adopter install** — see Known Issues below.

**Salesforce, Schwab market data, Jira, and Snowflake's read verbs moved
onto an async dispatch/completion shape (no verb behavior change).**
Fourteen verbs across these four plugins (Salesforce: all 9 real-I/O
verbs; Schwab: `get_options_chain`; Jira: 7 verbs plus `jql_search`,
`list_comments`, and `test_connection`; Snowflake: 7 read verbs) now
return a `{job_id, status: "queued"}` envelope in milliseconds instead of
blocking on the underlying network call, with the actual work completing
on a background worker thread and the result delivered through the same
completion channel every other async verb on this platform already uses.
This is a dispatch-shape change only — access control, output contracts
(TSV export limits, inline-vs-file delivery), and what each verb actually
does are all unchanged; if you call these verbs programmatically rather
than through an agent that already handles the async envelope, you will
need to poll for completion instead of getting an inline result.

**A packaging gap that kept the coordination-hooks plugin undiscoverable
in a freshly-cloned seed is fixed.** The plugin's own source shipped
correctly via this seed's normal birth mechanism, but the root-level
marketplace registration file Claude Code needs to discover and install
it (`.claude-plugin/marketplace.json`) was never included in the seed's
own copy manifest — a fresh clone had the plugin's code but no way for
Claude Code to find it. Fixed by adding it to the manifest's copy list;
no code path changed. The same commit also bumps the plugin from `0.3.0`
to `0.3.1` to pick up an earlier real behavioral change (the bounded
idle-wake wait) that had landed without its own version bump — an
already-installed `0.3.0` would otherwise have kept running the old
unbounded wait indefinitely, with no local signal anything was stale.

**Memory-head curation gained two new verbs.** `generate_curation_report`
surfaces activation-ranked demotion candidates from the ambient memory
index; `reinforce_by_slug` wires a cite into a reinforcement of the memory
it cited. Both follow the same async dispatch shape described above. Every
demotion decision from a curation report is still a human/seat judgment
call — there is no automatic trimming.

## Known issues — disclosed, not yet fixed

**Two gate-register entries cannot pass on a freshly-born clone.** The
shipped quality-gate register contains two entries that each read a local
path (a Claude Code session-surface path, and a plugin-marketplace path)
this platform's own root-strictness contract declares a born clone may
never have — both are origin-only by construction. **What to expect from
your own gate run:** measured end-to-end on a real, freshly-born clone of
the currently published seed, 247 of 249 register entries pass, and the
two failures are exactly these entries. Anything else failing is worth
investigating; these two specifically are not a broken install. A
publication-time check that would catch this class before a seed ships
has been designed and measured; it is not built yet, and is sequenced
behind other in-progress work. No date is promised.

**The `marketo_plugin` ships in this seed but is not runtime-dispatchable
on any machine that doesn't add it to its own live platform manifest.**
This is a per-adopter manifest/credentials fact, not something this seed
can decide for you at ship time — the plugin's code, tests, and knowledge
base are all present and correct; whether a given solet can actually
call it depends on that solet's own manifest. If you try a Marketo
verb and get a not-found error, check your manifest before assuming
something is broken.

**Two Jira verbs were not migrated onto this release's async dispatch
shape: `add_attachment` and `download_attachment`.** An earlier
blocking-I/O inventory pass classified both as "clean" (non-blocking) by
source-scanning for a recognized I/O pattern; that scan's own bounded
trace missed a blob-write code path in both verbs, a disclosed
false-negative in the scan itself, not a defect in this release's
migration work. Both verbs are unaffected otherwise and continue to work
exactly as before — this only means they were not part of this release's
async-shape work and remain on the prior synchronous path.

**The Snowflake write verb (`run_statement`, this release) has two open
questions, not yet answered by measurement.** First, Snowflake's own
support for a `RETURNING`-equivalent clause depends on the target object
and has not been characterized here — the connector will roll back rather
than silently commit-and-discard if such a clause produces rows with no
export path given, but which statements actually produce rows this way
is not documented. Second, **this release ships with no live write smoke
against a real Snowflake account** — every connection this platform
currently uses is pinned to a read-only role by the operator's own
registration, so a live write was never exercised end-to-end, only
tested against a fake client. If you register a write-capable role and
use `run_statement`, you are the first live exercise of that path.

**Neither the `0.4.0`/`0.4.1` coordination-hooks vendoring nor the
memory-passthrough origin-resolution ladder has a live adopter-install
test as of this release.** Both were verified against this checkout's own
source and (for `0.3.0`) an installed plugin-cache copy — see "What
changed" above — but not against a fresh clone going through the full
install → hydrate → first-launch path end to end. Mitigations already in
place: every hook in this stack degrades to a fail-silent no-op rather
than crashing a session when its arming environment variable is unset
(see this release's own `SECURITY.md` Configuration surface section), the
existing parity smokes cover the vendored code directly, and this
release's update runbook includes an explicit post-update hook-verification
step (see "After updating: verify your hooks actually fire" in the
runbook). None of that substitutes for an actual fresh-install run; treat
this stack as freshly landed rather than battle-tested until one happens.

## How to safely update

1. **Check you're starting from a healthy, unmodified clone.** Confirm
   your solet responds normally, and note that files like
   `AGENTS.md`, `CLAUDE.md`, and any `client/` directory showing as
   untracked or modified in `git status` is normal — those are generated
   for your own machine and an update never touches them directly.
2. **Pull, fast-forward only, never merge or rebase.** A refusal to
   fast-forward means your history has diverged from the seed's — stop
   and ask, rather than forcing it; this is not a routine occurrence.
3. **Most updates need no dependency install.** Only if you're told a
   specific update adds a new component do you need an editable-install
   step for it; running that step when it isn't needed is harmless.
4. **Restart, preferring a zero-downtime swap over a bare restart** when
   your profile has a local blue-green router available; otherwise a
   plain restart is always safe.
5. **Wait for startup to fully settle before your first query afterward.**
   Anything that looks alarming immediately after a restart — a brief
   "no active color" response through a router, for instance — is a known
   transient state that clears itself within seconds; re-probe rather than
   assuming something broke.
6. **Check for a newly-untracked `.gitignore` at the root** and commit it
   when convenient (see "What changed" above) — expected, not a symptom.
7. **Refresh anything that lives outside the platform's own restart path.**
   A restart makes the platform run new code; it does not, by itself,
   refresh an already-open connection from an external client, nor an
   installed Claude Code plugin's cached copy of shipped hooks. If an
   update specifically changes those surfaces, it will say so, and the
   fix is a fresh client session plus an explicit plugin refresh, not
   another restart.
8. **Verify.** A normal health check, plus a knowledge search that
   returns content matching this release's notes, confirms you're
   actually running what you think you're running. Re-reading your gate
   output after this update, now that skips are visible, is also worth
   doing once.

The full agent-executable version of this procedure — the one a
clean-context Claude Code session can run end to end — lives in this
seed's own knowledge base (search for "seed update runbook") and is kept
current independently of this document.
