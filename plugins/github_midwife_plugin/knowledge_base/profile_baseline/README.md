# `profile_baseline/` — checked-in per-plugin config templates

Genesis has no parent solet to read per-plugin config baselines from
(`macos_midwife_plugin`'s `config_generator.py` reads a *running parent's*
`profile/config/plugins/`; genesis has none). These files are the
plugin-owned substitute: static config for the `macos-free-solet`
allowlist, checked into the repo instead of read from a parent at
materialize time.

**Static fields only** — per the same discipline `initialization/profiles/cloud.yaml`
documents for its `plugin_config_overrides`: "heavy overrides are a smell
that the profile is wrong." Per-solet / per-installation fields are
injected by the materialize step (`config_materialize.py`, Slice D), NOT
checked in here — mirroring `config_generator.py`'s existing
`write_postgres_config`/`write_pgvector_config` pattern, which loads a
baseline and then overrides only the fields that vary per newborn.

## Per-solet fields the materialize step (Slice D) MUST inject

| File | Field(s) | Injected value |
|---|---|---|
| `postgres_state_management_plugin.json` | `schema` | the solet name (mirrors `write_postgres_config`) |
| `postgres_state_management_plugin.json` | `password` | never checked in — seeded by Slice C (`credential_seed.py`) straight to the vault Keychain substrate, never written to a config file |
| `pgvector_service_plugin.json` | `db_schema`, `host`, `port` | mirrors `write_pgvector_config`, which adds these keys even when the baseline doesn't have them |
| `default_knowledge_plugin.json` | `knowledge_base_root` | `<clone_root>/knowledge_bases` — absolute, depends on where the user cloned the repo; the plugin has no built-in default and raises `knowledge_base_root not configured` if the key is absent, so Slice D MUST set it (fail-loud otherwise, by design) |

## `default_inference_plugin.json` — preserved existing local inference

Baseline selection is plugin-membership-derived (`seed_resolver`), so this
file materializes ONLY into profiles whose allowlist includes
`default_inference_plugin` (`macos-samantha-solet` among the supported fresh
profiles; the free profile stays declared-VACANT per INF-03). This file retains
the existing LM Studio and Qwen settings. Samantha's direct inference callers
need separate qualification before its binding can move to Apple Foundation
Models. Existing solets and this baseline are not silently reconfigured.

## `macos_inference_plugin.json` — Apple summaries in BizOps

The macOS 27 BizOps profile selects the new Apple Foundation Models plugin
for qualified summaries. This static baseline copies all fields from the
separately frozen plugin's packaged default config resource (SHA-256
`507745a254cb526a0a6bd19b98b47bc38049086772cee6c0dde7fbd619828d68`).
It selects `apple-system`, a 1,024-token output limit, an 8,192-token context,
and disables warming, automatic compaction, and clear. It carries no local
server address or API key. Autonomous planning remains session-primary and no
local-provider completion fallback is enabled here. Runnable proof still
requires the separately owned plugin source to be composed with this profile.

## llama.cpp on macOS 26 — the same two baselines, pointed at loopback

A fresh macos-bizops create on a Mac below macOS 27 selects llama.cpp for
embeddings and summaries (iss_3a2a74ea). Genesis resolves the template's
`implementation_plugins` table, so `openai_embeddings_plugin` and
`default_inference_plugin` replace the two Apple plugins in the roster and
their baselines here materialize. Setup's `models` stage then points them at
the solet's loopback llama.cpp servers:

- `default_inference_plugin.json` receives the llama.cpp `base_url`, the
  `qwen3-8b` model alias and an 8,192-token `context.model_context_tokens`;
  every other key keeps this baseline's value.
- `openai_embeddings_plugin` reads its endpoint from the `openai_embeddings`
  address-book entry, not from this file. Setup adds that entry, with the
  2,048-token `max_input_tokens` budget, to the address-book seed.

The values come from
`plugins/github_midwife_plugin/knowledge_base/profile_templates/llama_cpp_models.yaml`,
never from this directory.

## `coreai_embeddings_plugin` asset configuration

Every supported fresh macOS 27 profile selects the Core AI embedding plugin.
Its runtime requires an absolute `asset_root` that names a separately verified
installed asset. This directory depends on the clone/install location and
cannot be expressed in a static baseline or the current `${SOLET_NAME}`
override substitution. The matching asset-root materialization and seed-lock
recipe are separate scoped work; until they are implemented, these profiles
are source candidates and cannot prove a runnable fresh install.

## Allowlisted plugins with NO file here — and why

Not every plugin in `macos-free-solet.yaml`'s allowlist needs a
baseline file. The reference environment's live `profile/config/plugins/`
already omits config for several of
these, and boots fine — an absent `<plugin>.json` is a fully supported
state (`ConfigManager.get_plugin_config`: "the override file holds only
operator deviations"; a plugin's own `plugin.yaml` `config:` schema
defaults, or its `get_default_config()`, fill the gap).

- `macos_vault_plugin` — no JSON config at all; its "config" is the
  Keychain master-key material generated at credential-seed time
  (Slice C), never a checked-in template.
- `default_address_book_plugin` — `plugin.yaml` declares `config: {}`.
- `actr_memory_plugin` — its one field (`enable_scheduled_operations`)
  already defaults to `true` in `plugin.yaml`; shipping `{"enable_scheduled_operations": true}`
  here would be redundant.
- `default_thinking_plugin` — owns no model path (DEP-01 Phase-2b):
  internal reasoning routes through `inference_service` via the
  autonomic lane, and the only config field (`system_prompt_path`)
  already defaults to `config/prompts/thinking_system_prompt.md`.
  The reference live config carries `base_url`/`model`/`inference_backend`/
  `max_tokens`/`temperature` fields that are NOT read anywhere in the
  plugin's current source (verified by grep) — dead config from an
  earlier architecture, not reproduced here. Shipping those fields
  would misleadingly imply genesis requires a local thinking-capable
  LM Studio model, which contradicts the zero-heavy-local-model
  framing of the free profile.
- `default_scheduling_plugin` — `get_default_config()` returns `{}`.
- `platform_health_plugin` — no config override at all.
- `agent_messaging_plugin` — every value in the reference live config either
  matches the plugin's `plugin.yaml` default exactly, or is one of the
  origin environment's opt-in deviations for external-connector setup
  (`streamable_enabled: true`, an `oauth_resource_aliases` entry naming
  a specific tunnel). Genesis's base profile has no tunnel and no
  registered OAuth client, so the plugin's own defaults — streamable
  transport OFF, no OAuth resource aliases, bridge host `127.0.0.1` —
  are exactly right; overriding anything here would either leak
  operator-specific data (the tunnel alias) or needlessly widen the
  local attack surface for a feature (the streamable HTTP transport)
  genesis's stdio-bridge MCP connect path doesn't use. Connector setup
  (which would enable `streamable_enabled` + register an OAuth client)
  is an opt-in step layered on top, not part of genesis.
- `github_midwife_plugin` — doesn't exist as installable code yet
  (Slice G); presumed to need no plugin config of its own once built,
  since its state (credentials, paths) is owned by dedicated modules,
  not generic plugin config.

## Public-safe

None of these files contain the operator's home-directory path or any
other operator-identity path. `postgres_state_management_plugin.json` keeps
only static connection defaults; genesis injects per-solet database,
schema, and user values from the selected profile at materialization time.
