# Core AI Nomic embedding runtime

The plugin implements the synchronous `EmbeddingServiceInterface` on macOS 27.
Configure an absolute `asset_root` and `compute_preference` (`gpu`, the default,
or `cpu`). Install the separately pinned asset through the asset acquisition
API before preparation. Runtime preparation and inference never download assets.

## Lifecycle and failures

`initialize(config)` replaces configuration and closes the previous runtime.
`prepare_for_readiness()` verifies installed asset hashes, loads the combined
model, and performs a real embedding probe before setting readiness. Missing or
corrupt assets leave the service unavailable, with a warning and repair text in
`get_runtime_status()`. Repair the asset/configuration and prepare again to retry.
`cleanup()` closes the runtime; repeated cleanup is safe and preparation can
reopen it. No database records or optional mutable persistence are introduced.

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
