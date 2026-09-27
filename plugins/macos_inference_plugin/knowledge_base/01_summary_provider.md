# macOS Foundation Models summary provider

`macos_inference_plugin` is a separate `InferenceProvider` for macOS 27 Apple
Silicon and Python 3.13. It does not depend on or replace the source of
`default_inference_plugin`. Select it explicitly in a reviewed seed/transition
and bind `inference_service` to it for qualified local summary callers.

The framework owns planning, action parsing and routing. Autonomic completions
stay with the frontier `sys:autonomic` session or its durable deferred queue.
Do not configure a completion-provider fallback to this plugin. This plugin has no action
execution API. The required protocol method `propose_name` raises an explicit
unsupported-operation error; naming and other unqualified direct callers need
separate routing review. Generic action-output defaults are refused.

## Configuration and lifecycle

Materialize `plugins/macos_inference_plugin/src/macos_inference_plugin/resources/default_config.json`
from the installed package `macos_inference_plugin.resources` to the
solet's own `profile/config/plugins` directory as `macos_inference_plugin.json`. Every field is required;
configuration is validated without silently merging missing fields. The model
is `apple-system`; no endpoint, daemon or API key is required. The SDK pin is
`apple-fm-sdk==0.2.1`.

Preparation validates configuration and registers the plugin without probing the
model. Post-registration availability returns a typed warning with the actual
reason and a repair instruction. A VM can remain `DEVICE_NOT_ELIGIBLE` while
unrelated setup completes. Runtime requests fail explicitly when the model is
unavailable; warning is not inference success. A readiness check re-probes an
unavailable model so an asset/configuration repair can recover without a retry
thread. The plugin creates an isolated SDK session for each request, serializes
calls, cancels asynchronous generation on timeout, and releases the request
slot once that worker unwinds. A native call ignoring cancellation may outlive
the synchronous deadline; the slot stays held to prevent accumulating workers.
Queue wait and generation each have their configured timeout budget.

There is no persistent plugin-owned state, schema, credential, conversation
history, or cache to migrate/clear. Failed requests can be retried. A replaced
plugin instance is retired by the platform's reviewed lifecycle transition.
Platform context records remain platform-owned. Context clearing and automatic
compaction are disabled here; bounded explicit compaction is supported. The
context cache warming flag is constrained to false, `warm_cache` returns false
without calling the SDK, and model capabilities say warming is unsupported.
No discarded session is represented as a reusable warmed cache.

## Request and response contract

`generate_completion(InferenceRequest)` accepts text system/user/assistant
messages, finite temperature from 0 to 2, and positive output tokens capped at
2048. Prose uses `use_structured_output=False`. Guided output requires an
explicit JSON object schema; object titles and property order are adapted for
Apple's SDK. The result uses `data.result.completion`, `model`, `provider`,
`usage`, and explicit truncation metadata. Unsupported stop sequences, empty
output, output reaching its limit, refusal/schema/context failures, and missing
assets are typed failures.

The model's reported context size is authoritative. Instructions and prompt are
counted separately with the SDK; response space and 256 tokens are reserved.
Only `context_metadata.purpose=session_ledger_auto_summarize` permits a marked
head/recent-tail truncation. Other oversized requests fail and require caller
chunking; historical 32K–175K register fields are not silently supported. Usage
counts the retained input, while `original_input_tokens` records pre-truncation
size. Guided schemas can consume further native context; the fixed reserve is
not a guarantee for arbitrary schema sizes, and native context failure is
surfaced. Each request has fresh history, so warming never leaks between users.

The existing session-ledger auto-summarizer is a qualified prose caller. The
register's separate HTTP integration and its leading/trailing nonce contract
are not migrated by this plugin. Thinking and code-vetting direct callers are
unqualified until their owners review routing and size; this plugin does not
claim action-generation parity.

## Packaging and integration seams

The source wheel is at
`plugins/macos_inference_plugin/vendor/apple_fm_sdk-0.2.1-py3-none-macosx_27_0_arm64.whl`,
SHA-256 `e005f63d275935ed3fbdccff6b5ab4d185116f8cbca22e04a5dd5511a2c739e6`.
The installed Python plugin wheel embeds these identical bytes as an
`importlib.resources` resource of `macos_inference_plugin.vendor`. The outer
plugin is Python-only; the embedded SDK wheel carries the native platform tag
and `Root-Is-Purelib: false`. See `plugins/macos_inference_plugin/vendor/README.md`
for source/build provenance.

Installer/Manager must verify this digest and install/select the local SDK wheel
before resolving the plugin's required dependency. An unqualified index lookup
can choose Apple's source distribution and require Xcode. Seed configuration,
service bindings, existing-solet ownership/cutover, installer warning UI, shared
smoke registration, fresh VM installation and publication are separate units.
Standalone package tests prove none of those transitions or releases.

Run `plugins/macos_inference_plugin/tests/apple_fm_provider_smoke.py`,
`plugins/macos_inference_plugin/tests/provider_isolation_smoke.py` and
`plugins/macos_inference_plugin/tests/plugin_contract_smoke.py` for portable
behavioral checks. Run
`plugins/macos_inference_plugin/tests/installed_resources_smoke.py` with the
installed plugin directory on `PYTHONPATH`, outside the source tree.
`plugins/macos_inference_plugin/tests/physical_host_probe.py` is an explicit
real-model probe and fails on model absence; it is not an always-green fixture
or a required VM summary gate.
