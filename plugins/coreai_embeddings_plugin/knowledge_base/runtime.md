# Core AI Nomic embedding runtime

The plugin implements the synchronous `EmbeddingServiceInterface` on macOS 27.
Configure an absolute `asset_root` and `compute_preference` (`gpu`, the default,
or `cpu`). Install the separately pinned asset through the asset acquisition
API before preparation. Runtime preparation and inference never download assets.

## Lifecycle and failures

`initialize(config)` replaces configuration and closes the previous runtime.
`prepare_for_readiness()` verifies installed asset hashes, proves the compiled
cache with an out-of-process canary, loads the combined model, and performs a
real embedding probe before setting readiness. Missing or
corrupt assets leave the service unavailable, with a warning and repair text in
`get_runtime_status()`. Repair the asset/configuration and prepare again to retry.
`cleanup()` closes the runtime; repeated cleanup is safe and preparation can
reopen it. No database records or optional mutable persistence are introduced.

## Compiled-cache canary and self-repair

Core AI compiles each model specialization once and reuses the result from
`~/Library/Caches/coreai-cache/<os build>/<bundle id>/<model hash>/<spec key>/`
without verifying it. The model hash is the pinned `main.mlirb` sha256. An
unclean OS or disk stop (power loss, kernel panic, a forced VM stop) can leave a
torn `bnns.ir` there. Every later load then returns non-normalized vectors or
dies with SIGTRAP in `BNNSGraphContextExecute_v2`, and the damage survives
reboots because the cache key never changes. A crashed or killed process cannot
tear the cache; Core AI publishes a finished compile atomically.

Preparation therefore runs an out-of-process canary before the in-process load.
A child process (`python -m coreai_embeddings_plugin.cache_canary`) loads the
model with the same `compute_preference`, runs the readiness probe and validates
the output as finite and normalized. The canary is a separate process because a
SIGTRAP cannot be caught in process.

- Child succeeds: the in-process load proceeds. Measured healthy overhead on a
  Mac15,14 (M3 Ultra, macOS 27.0): median 0.42 s for `cpu` and 0.59 s for
  `gpu`, warm.
- Child dies by a crash signal (SIGTRAP, SIGILL, SIGSEGV, SIGBUS, SIGABRT or
  SIGFPE) or reports invalid output: the plugin takes the repair lock
  exclusively and reruns the canary. If the cache is still bad, it deletes
  only the pinned model's entries, `coreai-cache/<os build>/<bundle id>/<main.mlirb sha256>`,
  which covers both the GPU and the CPU specialization. It then recompiles
  once and reruns the canary. A recovery is logged as a warning.
- Canary fails again: preparation fails loudly with `status: error` and
  `last_error` "Core AI compiled cache invalid after purge and recompile:
  <reason>", logged at ERROR. There is no second rebuild and no silent fallback.
- Any other child failure (asset missing, a 45-second timeout, an unexpected
  exit, or a kill such as SIGKILL, SIGTERM or SIGINT) fails preparation with
  that reason and never purges the cache.

Every Python-based solet on a host shares this cache entry, and after the
unclean stop that tears it they all prepare at once. A lock file beside the
cache root, `coreai-cache.<key prefix>.canary.lock`, coordinates them. Canary
attempts hold it shared, so healthy boots still run in parallel. Repair holds it
exclusively, and its first step is the rerun, so a solet that waited while
another one repaired the cache finds it healthy and purges nothing
(`outcome: repaired_by_peer`). Waiting is bounded at two canary timeouts.

The purge walks the cache without following links. Before deleting anything it
refuses any symlink on the path to the model's entries, and any entry that
resolves outside the cache root, as a typed error. It never follows a link out
of the cache and never stops halfway on a bare `OSError`.

`get_runtime_status()` reports the last canary result under `cache_canary`:
`outcome` (`healthy`, `recovered` or `repaired_by_peer`), `seconds`, `first_attempt` and `purged`.

### VM rounds: shut the guest down from inside

End every VM round with an in-guest `sudo shutdown -h now`, then wait until
`tart list` shows the guest `stopped`. A bare `tart stop`, even with its default
30-second grace, was measured as an unclean power-off: the guest records the
session as `crash`. Forced stops tore this cache 4 of 4 times despite `sync`,
`F_FULLFSYNC` and the host `sync=full` disk mode. They also left a stale Homebrew
Postgres `postmaster.pid`. A graceful shutdown preserved the cache. The canary
repairs a torn cache on the next boot, but a clean shutdown keeps a round's
evidence about what it actually installed.

Generation fails with a typed ActionResult rather than returning fake vectors or
a partial batch. Metadata calls describe the supported model without asserting
availability; `list_models()` includes an explicit `available` flag. Static
`get_default_dimensions()` is usable during schema initialization.

## Input and output contract

Model ID: `nomic-ai/nomic-embed-text-v1.5`. Only nonempty text strings are accepted,
with at most 128 inputs per call. The pinned tokenizer includes special tokens
in the 2,048-token ceiling. Inputs exceeding that ceiling fail; they are never
silently truncated. Buckets are 256, 512, 1024 and 2048 tokens with explicit
padding masks. Output is one finite, normalized 768-dimensional vector per input.
Caller text and prefixes are preserved exactly. Discovery writes use
`clustering:` and discovery queries use `search_query:`. Ledger and ACT-R callers
also use raw text. The runtime never adds or strips these prefixes.

## Native execution

A single worker owns the async Core AI runtime so synchronous service calls also
work from callers with an active event loop. Calls are serialized. A native
operation exceeding 120 seconds poisons that runtime; repair/preparation creates
a new instance. Cancellation cannot forcibly interrupt an already executing
native call; its worker releases resources once the call returns.

GPU load or execution RuntimeError triggers a logged CPU-only retry. Diagnostics
separate configured preference, selected preference, and observed compute unit.
GPU evidence is the SDK profiler `call.MPSGraph` execution event. CPU evidence is
successful inference under `SpecializationOptions.cpu_only`. An unrecognized
GPU execution remains `unknown`, not an invented GPU observation.

Runtime dependencies are Core AI, tokenizers and NumPy; Torch and Transformers
are not imported or required. Asset conversion is a separate build-time task.

## Migration and outage boundary

This plugin does not rewrite service bindings, seeds, installers, stored vectors
or the old embedding plugin. Numeric and mixed-corpus retrieval evidence must be
reviewed before any no-reindex decision. The ledger has its own embedding drain;
discovery logs generation failures and ACT-R raises them. This plugin does not
promise durable replay for those direct callers. Installer warning behavior and
VM acceptance require separate integration validation.
