# Homebrew Install Troubleshooting Runbook — Driving `solet create` Past Its Stops

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for installing a solet on a Mac through the Homebrew path (brew install solet-public/tap/solet, then solet create), explaining the preview-then-approve loop and its exit codes, the nine setup stages in order, how to read the manager's JSON when a stage stops (message, repair, error_kind, decision_errors, unresolved_actions, probe statuses), the automated LM Studio provisioning operations in system_dependencies (pinned installer, JIT disabled, exact artifacts, explicit CPU loading and shared login job), and recovery for older releases with manual provisioning, what it means when an LM Studio model download sits stuck at 0% forever with no error while the server and daemon both report healthy (an unpinned installer build that cannot transfer bytes, not a network problem, fixed by pinning the installer version), where the instance, transaction journal, install-state projection, logs and LaunchAgent live, stage-by-stage recovery for PostgreSQL, pgvector, cask installs, stale Keychain names and crash-looping services, the 24 GB memory minimum, and the rule never to mix the manager path with the bootstrap.py path mid-run.

## When to use this runbook

Use this when a solet is being created on a Mac through the Homebrew path
(`brew install solet-public/tap/solet` followed by `solet create <name>`) and
a `solet create` pass has stopped, refused, or asked for something. It is
written to the coding agent driving the install, the same audience as the
genesis section of the seed README, and applies equally to Claude Code and
Codex. It does not cover the seed-native `bootstrap.py` path (that is the
seed README's Genesis section), updating a live solet (the seed-update
runbook), or the operator-environment work after creation (the hydration
runbook).

## Hard requirements to confirm before the first command

- macOS 13 or newer, Apple Silicon or Intel, with Homebrew installed from
  brew.sh. The manager never installs Homebrew.
- **24 GB of memory or more.** This is the supported minimum, because the
  solet runs a local inference model. An 8 GB or 16 GB machine fails at the
  models stage in a way that looks like a model or manager defect. Check with
  `sysctl -n hw.memsize` (bytes) before starting and say so plainly if the
  machine is under the floor.
- About 15 GB of free disk for the two local models and the instance.
- An account that can install Homebrew casks (LM Studio, Claude Code, Codex).

## Before `solet create`: the Homebrew trust check

Homebrew 6 refuses to load a formula from a third-party tap until it is
trusted: `brew install solet-public/tap/solet` or `brew info` stops with
`Refusing to load formula solet-public/tap/solet from untrusted tap
solet-public/tap. Run brew trust ...`. That is Homebrew policy, not a
formula defect. Trust the one formula, never the whole tap, and retry:

```console
brew tap solet-public/tap
brew trust --formula solet-public/tap/solet
brew install solet-public/tap/solet
```

`brew trust --help` documents the current flags if they differ. Older
Homebrew versions have no trust store and install directly. Record which
case the machine was in; it decides whether an upgrade later needs the same
step.

## The loop: preview, approve, repeat

`solet create` is not a single run. Each pass is one of two things:

1. **Preview**: `solet create <name> --dry-run --json` probes the machine,
   works out what the next stage needs, and prints a plan carrying an
   `approval_fingerprint` (`sha256:…`) that identifies exactly that plan.
2. **Apply**: `solet create <name> --yes --approval-fingerprint <value> --json`
   applies the approved plan and nothing else. If the machine changed since
   the preview, it refuses (`probe_drift`) and asks for a fresh preview.

Exit codes are a contract: `0` success, `1` failed, `2` invalid invocation,
`3` awaiting a human action. **Exit 3 with `"status": "awaiting_user"` is the
normal end of an apply pass**, not a failure. Read `message` and `repair`:
they say whether the stage completed (preview again) or needs an input.
Continue until a preview reports the flow complete.

Decisions the flow cannot make itself come back as `decision_prompts`, each
with `candidates`. Answer with `--decision <id>=<value>` on the next preview.
Always run `--json`; the human-readable rendering drops fields you need.

The stages, in sequence, with what each installs or verifies:

| Stage | What happens | Exit probes |
|---|---|---|
| `preflight` | git checkout valid, Python 3.13 present | `python_version_valid` |
| `decision_review` | resolve profile, implementations, coding agents, topology, autostart | `decisions_resolved` |
| `system_dependencies` | Homebrew, Python, instance venv, Codex and Claude Code clients, Node, PostgreSQL, pgvector, tmux | `postgres_ready`, `postgres_role_policy_valid`, `pgvector_ready` |
| `genesis` | create the instance, database role, Keychain entries, `<name>` command, shell integration | `genesis_artifacts_valid`, fresh-shell PATH and Python probes |
| `models` | discover and qualify the embedding and inference models, install the LaunchAgent | `embedding_request_succeeds`, `launchagent_running`, `router_ready` |
| `coding_agents` | install the solet's Claude Code and Codex plugins | `coding_agent_plugins_visible`, `fresh_session_hooks_active`, `peer_identity_valid` |
| `optional_accounts` | business connectors, deferred to first use by default | connector validity probes |
| `session_sources` | register which local coding-agent histories may be indexed | `session_sources_retrievable` |
| `completion` | end-to-end verification | `knowledge_retrieval_succeeds`, `install_state_projection_matches` |

## Claude Code newlines in a managed tmux seat

This applies when the `execution_topology` decision is **Managed fleet**. A
normal Return submits a Claude Code prompt. Newlines require a modified Return
chord, and two independent host settings must preserve that chord:

1. tmux must contain both of these lines in `~/.tmux.conf`:

   ```tmux
   set -s extended-keys on
   set -as terminal-features "xterm*:extkeys"
   ```

2. iTerm2 must send Option as Escape. Setup offers a managed dynamic profile
   named **Solet Claude Code Return keys** with **Option Key Sends = Esc+ (2)**.
   Approve that preview, restart iTerm2, then select that profile before
   opening a new managed seat.

The tmux spawn path sets the same server options for every managed worker, so
the user configuration also covers manually started tmux servers. Run
`solet doctor <name> --json` after setup: its advisories report the tmux block
and the iTerm2 dynamic profile separately; an unreadable artifact is
`unknown`, never a pass.

With both settings, **Option+Return** sends Escape then Return, which Claude
Code treats as a newline. **Shift+Return** also requires iTerm2 CSI u modifier
reporting or the mapping installed by Claude Code `/terminal-setup`; do not
assume it works merely because Option+Return does. On any terminal where a
modified Return chord is unavailable, type **backslash then Return** as the
portable newline fallback.

## Where everything lives

- `~/Solets/<name>`: the instance, holding the seed clone with its own
  `README.md`, `AGENTS.md`, `CLAUDE.md` and this knowledge base.
- `~/.local/state/solet/transactions/<name>.json`: the create transaction
  journal, every stage, action and probe result so far. Read it before
  speculating about what ran.
- `~/Solets/<name>/.solet/install-state.json`: the installed-state
  projection `solet doctor` reads.
- `~/Solets/<name>/profile/data/logs/`: the solet's own logs once genesis
  has run; the newest file is the one to read when the service will not stay
  up.
- LaunchAgent label `local.solet.<name>`: `launchctl print
  gui/$(id -u)/local.solet.<name>` shows state, run count and last exit.
- `solet status <name> --json`, `solet doctor <name> --json`,
  `solet list --json`: what the manager believes and verifies.

## Automated LM Studio provisioning

When either existing implementation choice selects local LM Studio, setup
runs seven operations at the end of `system_dependencies`, after `install_tmux`:
install the CLI, start the server with JIT disabled, pull and explicitly load
the embedding model, pull and explicitly load the inference model, then
register the shared login job. Each model pair runs only for its selected
implementation. The later `models` stage discovers identifiers and configures
the selected services after this bootstrap has finished.

The installer pins `llmster 0.0.23-1`. The required artifacts are the
274,290,560-byte Nomic f16 file (served as
`text-embedding-nomic-embed-text-v1.5-embedding`) and the
9,001,753,376-byte Qwen3 14B Q4_K_M file (served as `qwen3-14b`). Both load
explicitly with `--gpu off`; Qwen uses `--context-length 8192`. JIT is
prohibited: setup preserves existing HTTP settings, sets
`justInTimeModelLoading` to `false`, restarts the server to reload the file,
and checks the value again. A downloaded model cannot serve until explicitly
loaded. `/v1/models` proves server availability; bounded `/api/v0/models`
checks must show `state: loaded` for each exact selected identifier. An
unrecognized response is unknown and blocks completion.

Each pull has a 900-second apply deadline. A timeout records a pending,
retry-safe operation and the observed partial bytes. Preserve `.part` files;
review a new dry run and resume with its fingerprint. Completed artifacts
are retained and checked against exact size, filename and source metadata.

The background consent authorizes the host-shared
`local.solet.lm-studio` job, with its helper under
`~/Library/Application Support/Solet/LM Studio/`. It explicitly loads the
complete selected artifacts at login. Solet abandon never unloads or removes
this shared service. A stale loaded definition requires host-level
coordination; setup reports it rather than replacing a running shared job.
`solet doctor <name> --json` reports a separate login-item advisory; unreadable
state is unknown, and does not change completion verification.
`solet inspect <name> --json` reads named LM Studio conditions without starting
the daemon or loading a model.

## Older releases: manual LM Studio recovery

On a fresh Mac using a release without the seven operations, the preview stops at `models` with `error_kind:
decisions_required`, both `embedding_model` and `inference_model` reporting
`model_discovery_failed` and zero candidates, and four unresolved actions
(`configure_lm_studio_embeddings`, `configure_lm_studio_inference`,
`install_launchagent`, `models:entry:decisions_resolved`). The decisions are
discovered from a running LM Studio server, and **those older releases have no
operation that installs LM Studio or downloads models.** That is the expected
stop, not a broken install. Provision by hand, then re-preview:

1. Install LM Studio's headless service, **pinned to `llmster 0.0.23-1`**.
   This is the route measured end to end on clean 24 GB machines and it
   needs no window:

   ```console
   curl -fsSL https://lmstudio.ai/install.sh -o /tmp/lms_install.sh
   sed 's/^APP_VERSION="[^"]*"/APP_VERSION="0.0.23-1"/' /tmp/lms_install.sh > /tmp/lms_install_pinned.sh
   sh /tmp/lms_install_pinned.sh --quiet
   ```

   **Do not run the plain `curl … | sh` one-liner unpinned.** As of
   2026-09-09 that command serves `llmster 0.0.24-1`, and on that build
   `lms get` transfers **zero bytes forever** while reporting healthy — no
   error, no exit, the daemon and server both start fine, `GET /v1/models`
   answers normally, and the pull's spinner just sits at `0.00%`. It looks
   exactly like a slow or stalled network, but the network is fine; it is
   this one build of the installed client that cannot fetch models. Measured
   across four independent runs: `0.0.23-1` completed the 9 GB inference-model
   pull cleanly on r26 and r27, `0.0.24-1` transferred 0 bytes across 8
   attempts over 90 minutes on r28. Filed as `iss_58331946`. The vendor
   installer has no `--version` flag; `APP_VERSION` is a hard assignment on
   line 27 of the fetched script, so pinning means rewriting that line before
   running it, as above. If a pull you started before reading this is stuck
   at `0.00%` for more than two attempts, stop retrying and inspect the
   installed version. Preserve the `.part` file under `~/.lmstudio/models/`
   when correcting the installer pin; retry the identical download command.

   The installer places `lms` at `~/.lmstudio/bin/lms`, off PATH; use the full
   path or add the `export PATH` line it prints. The desktop app route
   (`brew install --cask lm-studio`, open it once, `~/.lmstudio/bin/lms
   bootstrap`) reaches the same command but its bundled `lms` has exited 1 on
   machines without an interactive session, so prefer the installer when an
   agent is driving.
   Before starting the server, set `justInTimeModelLoading` to JSON `false`
   in `~/.lmstudio/.internal/http-server-config.json`, preserving other keys.
   Start the daemon and server using the absolute CLI path, patch the
   materialized settings again if needed, then explicitly stop and start
   the server so it reloads the setting. Read back `false` before proceeding.
   Missing or malformed settings must be resolved before loading models.

2. **Before downloading anything, know the trap.** As soon as the server
   starts, `GET /v1/models` already lists `text-embedding-nomic-embed-text-v1.5`.
   That is LM Studio's bundled build and it is the wrong one: it satisfies a
   substring check and produces different vectors with no fail-loud signal.
   The required identity carries the `-embedding` suffix and is about 274 MB;
   the bundled one lacks the suffix and is about 84 MB. Check size and suffix,
   never just that "nomic" appears.
3. Download the exact builds:

   ```console
   ~/.lmstudio/bin/lms get "https://huggingface.co/gaianet/Nomic-embed-text-v1.5-Embedding-GGUF" --gguf --yes
   ~/.lmstudio/bin/lms get "https://huggingface.co/lmstudio-community/Qwen3-14B-GGUF@Q4_K_M" --gguf --yes
   ```

   The inference target is Qwen3 14B at Q4_K_M (about 9 GB); the
   `owner/repo@QUANT` URL form selects the quantization. A pull this size
   can fail at the very start with `Download failed: Timed-out. Please try
   to resume.`; running the identical command again resumes from the partial
   file. Measure progress by the partial file's size in
   `~/.lmstudio/models/`, not by the spinner line, which a captured stream
   renders stale. Do not substitute the 30B model the older profile template
   names; it does not fit a 24 GB machine alongside everything else.
4. Load both and prove they are served. `~/.lmstudio/bin/lms ls` must show
   `text-embedding-nomic-embed-text-v1.5-embedding` at roughly 274 MB and the
   Qwen3 14B entry. Load them explicitly:

   ```console
   ~/.lmstudio/bin/lms load text-embedding-nomic-embed-text-v1.5-embedding --gpu off --yes
   ~/.lmstudio/bin/lms load qwen3-14b --gpu off --context-length 8192 --yes
   ```

   `lms` derives the served identifier from the download source by dropping
   the owner and the `-GGUF` / `@QUANT` tokens, so the inference model is
   served as `qwen3-14b`, not as the Hugging Face path. Measured on a 24 GB
   machine: the embeddings load in about 15 s at 262 MiB, Qwen3 14B in about
   30 s at 8.4 GiB resident, which is why the 24 GB floor is not
   conservative. `curl -s http://localhost:1234/api/v0/models` must show
   `state: loaded` for the exact required ids: pick
   `text-embedding-nomic-embed-text-v1.5-embedding` and `qwen3-14b`; the
   un-suffixed nomic id is the bundled wrong build from step 2. If the server
   stops answering, `lms server start` again; with the desktop app, quit and
   reopen it first.
5. Re-preview. The two decisions now carry candidates. Select the
   `-embedding` suffixed nomic entry and the `qwen3-14b` entry explicitly on
   the preview, then apply that preview's fingerprint:

   ```console
   solet create <name> --dry-run --json \
     --decision embedding_model=<candidate id> \
     --decision inference_model=<candidate id>
   solet create <name> --yes --approval-fingerprint <sha256 from that preview> --json
   ```

6. Make the server survive a reboot. In LM Studio's settings enable the
   option that runs the local server (its headless service) at login; the
   exact wording varies by version. Nothing those older releases install does that
   for you, and the solet's own LaunchAgent starts on login regardless, so
   without it the solet is up before its models are.

## Recovery by stage

**`preflight` / `decision_review`.** No Homebrew, a Python other than 3.13
on PATH, or a target directory that already exists. `solet create --target
<dir>` refuses a non-empty directory on purpose; choose another name or move
the directory. Never delete a directory you did not create.

**`system_dependencies`.** PostgreSQL is installed and started through
Homebrew, then pgvector is installed after the server is up. If a probe
still reports `postgres_ready` or `pgvector_ready` false: `brew services
list`, `psql -U $(whoami) -d postgres -c 'select 1'`, `brew list --versions
pgvector`; start the service Homebrew installed (`brew services start
postgresql@<version>`) and re-preview. The Codex and Claude Code clients are
casks; if the cask step is refused, install them yourself with `brew install
--cask claude-code codex` under the operator's approval and re-preview.

**`genesis`.** Genesis refuses a fresh instance paired with leftover Keychain
entries from an earlier attempt under the same name, so the newborn does not
crash-loop under launchd. Retrying a name means deleting the Keychain items
for service `<name>-vault` (account `master-key`) and any `<name>.<plugin>`
services first, with the operator's approval, or picking a new name. A
service that starts and exits shows a growing run count and a non-zero last
exit in `launchctl print`; read the newest log under `profile/data/logs/`.

**`models`.** The section above. `model_discovery_failed` means nothing
answered on `http://localhost:1234`; zero candidates with a running server
means the models are absent or not the expected builds.

**`coding_agents`, `session_sources`, `completion`.** These run the solet's
own hydration steps and end-to-end checks. They have had less clean-machine
coverage than the earlier stages. On a stop, keep the full preview JSON and
the transaction journal, follow `repair`, and if that does not clear it, file
the two files with the report (see the upstream feedback runbook).

## Rules that keep an install recoverable

- One route from the start. The Homebrew path (`solet create`) and the
  seed-native path (`git clone` plus `bootstrap.py`) materialize the same seed
  but keep different state. Never run `bootstrap.py` inside `~/Solets/<name>`
  to push a stalled `solet create` forward, and never start `solet create`
  against a directory `bootstrap.py` already used.
- Never join `brew install` and `solet create` with `&&`. Homebrew installs
  only the manager; `solet create` previews and asks.
- Stop and ask the operator before installing casks, editing Keychain items,
  or deleting anything. Divergent host state is reported as `awaiting_user`
  precisely so a human decides.
- Snapshot or note the state before a manual repair, so the report can say
  exactly what was changed by hand and what the manager did.

## After `completion`

The hydration runbook takes over: named Claude Code session launcher, shell
integration, the `<name>` command-line client, and the deployment report
card. Search this knowledge base for "hydration runbook operator environment
setup" once the solet is up, or read
`plugins/github_midwife_plugin/knowledge_base/01_hydration_runbook.md`
directly beforehand.

## Reporting what stopped you

File issues on the seed repository (`solet-public/macos-bizops`) with
`solet --version`, the exact command, and the full JSON it printed. A precise
report with the transaction journal attached is the fastest route to a fix;
pull requests are not accepted.
