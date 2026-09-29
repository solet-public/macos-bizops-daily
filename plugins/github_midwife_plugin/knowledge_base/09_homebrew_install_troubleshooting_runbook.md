# Homebrew Install Troubleshooting Runbook — Driving `solet create` Past Its Stops

Tags: knowledge:tag:planning_reference

Article Layer: 2

Article Role: operations_runbook

Article Tags: planning-stage:solet-lifecycle, evidence-category:operations-runbook, domain:local-solet, domain:client-deployment, consumer_profile:both

Embedding Description: Agent-facing runbook for installing a solet on a Mac through the Homebrew path (HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 solet-public/tap/solet, then solet create), explaining the preview-then-approve loop and its exit codes, the nine setup stages in order, how to read the manager's JSON when a stage stops (message, repair, error_kind, decision_errors, unresolved_actions, probe statuses), the automated LM Studio provisioning operations in system_dependencies (pinned installer, JIT disabled, exact artifacts, explicit CPU loading and shared login job), how a fresh macos-bizops create on macOS 26 (Tahoe) runs Homebrew llama.cpp instead of the Apple-native stack (host-profile gate, two loopback llama-server login services, pinned GGUF models with SHA-256 readback, a missing model as a warning, the 2048-token embedding budget, measured speed and memory, swapping the summaries model), and recovery for older releases with manual provisioning, what it means when an LM Studio model download sits stuck at 0% forever with no error while the server and daemon both report healthy (an unpinned installer build that cannot transfer bytes, not a network problem, fixed by pinning the installer version), where the instance, transaction journal, install-state projection, logs and LaunchAgent live, stage-by-stage recovery for PostgreSQL, pgvector, cask installs, stale Keychain names and crash-looping services, the 24 GB memory minimum, and the rule never to mix the manager path with the bootstrap.py path mid-run.

## When to use this runbook

Use this when a solet is being created on a Mac through the Homebrew path
(`HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 solet-public/tap/solet` followed by `solet create <name>`) and
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
trusted: the install command or `brew info` stops with
`Refusing to load formula solet-public/tap/solet from untrusted tap
solet-public/tap. Run brew trust ...`. That is Homebrew policy, not a
formula defect. Trust the one formula, never the whole tap, and retry:

```console
brew tap solet-public/tap
brew trust --formula solet-public/tap/solet
HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 solet-public/tap/solet
```

`brew trust --help` documents the current flags if they differ. Older
Homebrew versions have no trust store and install directly. Record which
case the machine was in; it decides whether an upgrade later needs the same
step.

## Before `brew install`: the Python the Manager builds on

From r61 the Manager formula does not depend on `python@3.13` and never
installs or upgrades it. It builds its venv on the first of these that
reports Python 3.13, in this order:

1. `/opt/homebrew/opt/python@3.13/bin/python3.13`
2. `/usr/local/bin/python3.13`
3. `/Library/Frameworks/Python.framework/Versions/3.13/bin/python3.13`

Install with exactly this command:

```console
HOMEBREW_NO_INSTALL_UPGRADE=1 brew install python@3.13 solet-public/tap/solet
```

Naming `python@3.13` installs it when the Mac has none and marks it installed
on request. `HOMEBREW_NO_INSTALL_UPGRADE=1` leaves an existing `python@3.13` at
its version. When one is installed, Homebrew prints `Error: python@3.13
<version> is already installed` (or a `Warning:` that it is already installed
and up to date), exits 0, and installs the Manager anyway. That line is
expected: do not treat it as a failure and do not run `brew upgrade
python@3.13` because of it.

If the install stops with `No Python 3.13 found (looked in: ...)`, none of the
three paths answered. The same message names the command to run; it is the
install command above, which installs `python@3.13` first. Confirm with
`brew list --versions python@3.13` (one line) and the Manager with
`solet --version`.

Other solets on the same Mac are unaffected: a solet whose `.venv` links
`python@3.13` keeps its exact interpreter, so its Keychain credentials keep
working. Before r61, installing the Manager upgraded that Python, and any such
solet was refused on every credential read (`-25293`, "make sure executable
is signed with codesign util") at its next restart, because the Keychain ACL
of an ad-hoc-signed binary pins its exact code-directory hash. A `brew upgrade`
you run yourself can still move `python@3.13`; `brew pin python@3.13` prevents
that and no longer blocks the Manager. To recover a solet already affected, in
a logged-in GUI session (not SSH) read each of its Keychain items once under
its current interpreter and answer **Always Allow**, not Allow. After
`solet-manager import`, `solet doctor <name>` reports
`doctor::python_interpreter_drift_v1` for a venv created under one
`python@3.13` version that now resolves to another; it does not see a
same-version revision bump.

### Upgrading the Manager later: mark `python@3.13` on request first

A Manager installed before r61 left `python@3.13` recorded only as its
dependency (`installed_on_request` is false in its receipt). From r61 the
formula no longer depends on it, so `brew autoremove`, which `brew upgrade`
runs in its periodic cleanup, would delete the interpreter every solet
`.venv` links. Every upgrade therefore starts with the receipt flag:

```console
brew tab --installed-on-request python@3.13 && brew upgrade solet
```

`brew tab` changes only that flag: it installs and upgrades nothing, and
running it again is harmless. It fails only if `python@3.13` is not installed,
and that stops the `&&` before the upgrade. A failure here means the Python
was removed from the machine: run `brew install python@3.13` (which also marks
it installed on request), then run the pair again. `solet-manager create` and
`update` run the same flag command as a planned, previewed step, and
`solet-manager doctor <name>` reports `doctor::python_installed_on_request_v1`
with reason `python_not_installed_on_request` and this exact command until the
flag is set.

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
`solet inspect --target <path> --json` reads named LM Studio conditions without
starting the daemon or loading a model. It is an active probe that may run
binaries inside the target, not a passive read; for a read-only classification
of an existing solet use `solet-manager inspect`.

## macOS 26 (Tahoe): llama.cpp for a fresh macos-bizops create

The Apple-native choices need macOS 27 on Apple silicon: Core AI embeddings
and Apple Foundation Models summaries. Setup measures the Mac with `sw_vers`
and `uname -m` against the same host profiles the update flow uses. On a Mac
below them it withholds both Apple choices and offers llama.cpp. On macOS 26
the embeddings choice defaults to llama.cpp, and summaries offer llama.cpp in
place of Apple Foundation Models. An explicit Apple selection is refused with
the Mac's version in the message. If the Mac cannot be measured, setup stops
the choice instead of guessing. A choice already recorded is never re-judged,
so a solet keeps what it was created with after the Mac is upgraded.

Why embeddings use llama.cpp on macOS 26 in this release: Core AI has never
run on macOS 26, so the Core AI embeddings floor is macOS 27 (rul_5cc2910c,
iss_c6abab0d). The llama.cpp embeddings serve the same Nomic v1.5 model that
LM Studio serves, so vectors stay in the same space.

What setup does when llama.cpp is selected (macos-bizops only):

- `system_dependencies` installs the Homebrew `llama.cpp` formula only if
  `llama-server` is absent.
- `models` writes and loads one host-shared login service per selected role,
  both listening only on `127.0.0.1`:
  - `local.solet.llama-server.summaries` on port 18180, serving Qwen3 8B
    Q4_K_M from `lmstudio-community/Qwen3-8B-GGUF` as `qwen3-8b`, with one
    8192-token slot and reasoning off;
  - `local.solet.llama-server.embeddings` on port 18181, serving
    `nomic-embed-text-v1.5.f16.gguf` as `nomic-embed-text-v1.5`, with a
    2048-token context.
- Setup points `default_inference_plugin` and `openai_embeddings_plugin` at
  those servers. The embeddings entry declares a 2048-token input budget, and
  the plugin counts every input with the server's own `/tokenize`, so long
  text is split to fit and never truncated.
- Genesis installs those two plugins in place of the Apple ones, so no
  macOS 27-only package is installed.
- The solet does not start at genesis. Its autostart is deferred
  (`deferred_llama_cpp_config_pending`) until `models` writes the
  `openai_embeddings` address-book entry. `install_launchagent` then starts
  it, the same way a Core AI solet waits for its pinned asset.

Models: `plugins/github_midwife_plugin/knowledge_base/profile_templates/llama_cpp_models.yaml`
pins each model by Hugging Face revision, size and SHA-256.

- Before serving, each service fetches its model from that revision into
  `~/Library/Application Support/Solet/llama.cpp/models/<repository>/<revision>/`.
  It accepts the file only at the pinned size and SHA-256, and then writes a
  `.verified` stamp.
- A download resumes from its `.partial` file after any failure; launchd
  retries the service every 60 seconds.
- A model that is still downloading, or missing, is a **warning**, never a
  blocked stage (rul_18bd93a3). `llama_cpp_models_present` reports the bytes
  present so far.
- Setup waits up to ten minutes for the embeddings server, which the solet
  needs first. The 5 GB summaries model finishes in the background.
- To seed a Mac without downloading, copy the exact file into that directory
  before setup. The service verifies it the same way.

Swapping the summaries model is a one-line change to `model:` under
`roles.summaries` in the registry, for example `qwen3-4b-q4_k_m`, which is
already pinned there.

Measured on an M3 Ultra (256 GB) with Homebrew llama.cpp 0.5.0 (build 11146),
through the rendered services on macOS 27:

| Service | Ready | Speed | Memory |
|---|---|---|---|
| Summaries | 13.6 s on first start, including the SHA-256 of 5.0 GB | prompt 1,037 tokens/s (1,580 tokens), generation 88.8 tokens/s | RSS 6.0 GiB, footprint 1.5 GB |
| Embeddings | 1.0 s on first start, including verification; 0.6 s after | a 2,048-token input in 0.20 s; a 40,500-character input split into 5 windows, all embedded | RSS 619 MiB, footprint 578 MB |

Expect lower speeds on smaller Apple silicon. A macOS 26 guest has not yet
run this path end to end.

Where things live and how to look:

- `launchctl print gui/$(id -u)/local.solet.llama-server.embeddings` (or
  `.summaries`) shows whether a service is loaded and its last exit.
- Logs are in `~/Library/Application Support/Solet/llama.cpp/logs/<role>.stderr.log`.
- `curl -s http://127.0.0.1:18181/health` answers `{"status":"ok"}` once the
  embeddings model is serving; port 18180 is summaries.

Like the LM Studio job, these services are shared by every solet on the Mac.
No solet removes them or their models. A definition that differs from the
reviewed one is rewritten and reloaded by the next setup. If
`llama_cpp_service_gui_session_required` stops the stage, log into the Mac's
desktop as the setup account and preview again. Homebrew publishes
`llama.cpp` bottles for Apple silicon; an Intel Mac would build it from
source and is not covered by this path.

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

**`models`.** The LM Studio section above. `model_discovery_failed` means nothing
answered on `http://localhost:1234`; zero candidates with a running server
means the models are absent or not the expected builds. On llama.cpp, see
the macOS 26 section: a model warning clears on its own once the download
verifies, and a service stop names its `launchctl` step in `repair`.

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
pull requests are not accepted. The upstream feedback runbook has the filing
path that works for an ordinary account with no repository access.
