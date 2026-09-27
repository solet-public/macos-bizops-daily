# Setup security permissions and consents

This file is generated from `macos_setup_flow.json`; do not edit it by hand.
Manifest schema version: `1`.
Source setup-flow schema version: `1`.

It lists the declared setup permission, consent, and authentication-flow surface only.
An item with no declared System Settings pane or denial behavior says so explicitly.

## Permissions

### Background items (`background_items_permission`)

- WHAT: Background items
- WHY: Allow the reviewed main per-user LaunchAgent at ~/Library/LaunchAgents/local.solet.<name>.plist to run after login.
- WHEN: Always required.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: General > Login Items & Extensions
- GRANT ACTOR: user

### Blue-green router background item (`blue_green_router_background_items_permission`)

- WHAT: Blue-green router background item
- WHY: Allow the reviewed blue-green router LaunchAgent at ~/Library/LaunchAgents/local.solet.<name>.router.plist to bootout, bootstrap, kickstart, and run after login.
- WHEN: When `setup_profile` equals `macos-bizops`.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: General > Login Items & Extensions
- GRANT ACTOR: user

### Claude Code plugin configuration (`claude_plugin_configuration_permission`)

- WHAT: Claude Code plugin configuration
- WHY: Allow the selected Claude Code CLI to register the repository marketplace, install the coordination plugin, and read ~/.claude/plugins/installed_plugins.json for verification.
- WHEN: When `coding_agents` contains `claude_code`.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: Privacy & Security > Files and Folders
- GRANT ACTOR: user

### Claude Code session files (`claude_session_files_permission`)

- WHAT: Claude Code session files
- WHY: Read only the user-approved Claude projects, history, and tasks roots for ingestion.
- WHEN: When `session_sources` contains `claude_code_local`.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: Privacy & Security > Files and Folders
- GRANT ACTOR: user

### Codex plugin configuration (`codex_plugin_configuration_permission`)

- WHAT: Codex plugin configuration
- WHY: Allow the selected Codex CLI to register the repository marketplace and install the coordination plugin in its user-owned configuration.
- WHEN: When `coding_agents` contains `codex`.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: Privacy & Security > Files and Folders
- GRANT ACTOR: user

### Codex session files (`codex_session_files_permission`)

- WHAT: Codex session files
- WHY: Read only the user-approved Codex session roots for ingestion.
- WHEN: When `session_sources` contains `codex_local`.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: Privacy & Security > Files and Folders
- GRANT ACTOR: user

### macOS Keychain (`keychain_permission`)

- WHAT: macOS Keychain
- WHY: Store the platform vault passphrase and provider-owned client sessions without placing secrets in the repository.
- WHEN: Always required.
- WHAT DENIAL DOES: block
- SYSTEM SETTINGS PANE: Not applicable (no System Settings path declared).
- GRANT ACTOR: user

### LM Studio background item (`lm_studio_background_items_permission`)

- WHAT: LM Studio background item
- WHY: Allow the host-shared LM Studio service to start after login.
- WHEN: Always required.
- WHAT DENIAL DOES: disable_capability
- SYSTEM SETTINGS PANE: General > Login Items & Extensions
- GRANT ACTOR: user

## Consents

### Confirm Apple Intelligence readiness (`apple_ai_attestation`)

- WHAT: Confirm Apple Intelligence readiness
- WHY: Confirm Apple Intelligence is enabled and its current terms and attestations are accepted on this macOS 27 host.
- WHEN: When `inference_implementation` equals `apple_foundation_models`.
- WHAT DENIAL DOES: Decline state: `blocked`. Apple Foundation Models cannot be selected until the host attestations are accepted.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Run after login (`background_service_consent`)

- WHAT: Run after login
- WHY: Authorize the reviewed main per-user LaunchAgent and background item.
- WHEN: When `autostart` equals `enabled`.
- WHAT DENIAL DOES: Decline state: `declined`. The solet remains manual-start only.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Install the Claude Code coordination plugin (`claude_plugin_install_consent`)

- WHAT: Install the Claude Code coordination plugin
- WHY: Authorize Claude Code to register the repository marketplace, install the coordination plugin, and read its installed-plugin registry for verification.
- WHEN: When `coding_agents` contains `claude_code`.
- WHAT DENIAL DOES: Decline state: `declined`. Claude Code remains installed without the repository coordination plugin or fresh-session hook proof.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Index Claude Code session history (`claude_session_ingestion_consent`)

- WHAT: Index Claude Code session history
- WHY: Authorize read-only ingestion of the displayed Claude projects, history, and tasks roots. No source files will be moved, deleted, or modified.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Claude history remains local and unindexed.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Configure Codex MCP access (`codex_mcp_configuration_consent`)

- WHAT: Configure Codex MCP access
- WHY: Authorize the Genesis append to ~/.codex/config.toml that registers this solet as a Codex MCP server.
- WHEN: Always required.
- WHAT DENIAL DOES: Decline state: `blocked`. Genesis does not run because every profile installs the named no-MCP launcher and its Codex MCP configuration together.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Install the Codex coordination plugin (`codex_plugin_install_consent`)

- WHAT: Install the Codex coordination plugin
- WHY: Authorize Codex to register the repository marketplace and install the selected coordination plugin.
- WHEN: When `coding_agents` contains `codex`.
- WHAT DENIAL DOES: Decline state: `declined`. Codex remains installed without the repository coordination plugin or fresh-session hook proof.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Index Codex session history (`codex_session_ingestion_consent`)

- WHAT: Index Codex session history
- WHY: Authorize read-only ingestion of the displayed Codex session and archived-session roots. No source files will be moved, deleted, or modified.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Codex history remains local and unindexed.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Authorize Google Workspace account (`google_workspace_account_consent`)

- WHAT: Authorize Google Workspace account
- WHY: Review the five requested Google scopes and authorize the selected account.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Google Workspace remains not connected.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Authorize Jira write and delete capability (`jira_destructive_access_consent`)

- WHAT: Authorize Jira write and delete capability
- WHY: Acknowledge that the Jira token inherits user permissions and the plugin includes permanent deletion.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Jira remains not connected.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Run LM Studio after login (`lm_studio_background_service_consent`)

- WHAT: Run LM Studio after login
- WHY: Authorize the shared LM Studio login job and explicit model loading.
- WHEN: When (`embeddings_implementation` equals `lm_studio`) or (`inference_implementation` equals `lm_studio`).
- WHAT DENIAL DOES: Decline state: `blocked`. LM Studio provisioning remains blocked until the background-service change is approved.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Authorize Marketo write, delete, and merge capability (`marketo_destructive_access_consent`)

- WHAT: Authorize Marketo write, delete, and merge capability
- WHY: Review the selected Marketo API Role and acknowledge delete and merge capabilities.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Marketo remains not connected.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Install shell integration (`shell_modification_consent`)

- WHAT: Install shell integration
- WHY: Authorize the reviewed PATH and launcher block in the selected shell startup file.
- WHEN: Always required.
- WHAT DENIAL DOES: Decline state: `blocked`. The installation cannot claim fresh-shell command or hook readiness.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Authorize Snowflake role (`snowflake_write_access_consent`)

- WHAT: Authorize Snowflake role
- WHY: Review the selected Snowflake role and acknowledge that run_statement can write wherever that role permits.
- WHEN: Optional; no required_when condition is declared.
- WHAT DENIAL DOES: Decline state: `dormant`. Snowflake remains not connected.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

### Install system dependencies (`system_change_consent`)

- WHAT: Install system dependencies
- WHY: Review and authorize the exact package, service, database-role, and filesystem changes in the generated plan.
- WHEN: Always required.
- WHAT DENIAL DOES: Decline state: `blocked`. Setup remains at the reviewed-plan checkpoint and performs no system mutation.
- SYSTEM SETTINGS PANE: Not applicable (consent, not a macOS permission entry).
- GRANT ACTOR: operator (explicit assent)

## Authentication flows

### External PostgreSQL credentials (`external_postgres_credentials`)

- WHAT: External PostgreSQL credentials
- WHY: Repeatable on-demand connection credentials; zero configured connections is a normal steady state.
- WHEN: No required_when condition is declared; starts through `configure_external_postgres`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Google Workspace OAuth (`google_workspace_oauth`)

- WHAT: Google Workspace OAuth
- WHY: Browser OAuth for Gmail, Drive, Docs, Sheets, and Slides using a local loopback or web callback selected by deployment topology.
- WHEN: No required_when condition is declared; starts through `configure_google_workspace`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Jira API token (`jira_api_token`)

- WHAT: Jira API token
- WHY: Jira account email plus API token; no OAuth callback.
- WHEN: No required_when condition is declared; starts through no declared operation.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Marketo LaunchPoint credentials (`marketo_client_credentials`)

- WHAT: Marketo LaunchPoint credentials
- WHY: OAuth client-credentials flow backed by a Marketo API Role, API User, and LaunchPoint service.
- WHEN: No required_when condition is declared; starts through `configure_marketo`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Salesforce CLI browser login (`salesforce_cli_login`)

- WHAT: Salesforce CLI browser login
- WHY: The standalone sf CLI performs browser login and owns the resulting credential in its auth store or macOS Keychain.
- WHEN: No required_when condition is declared; starts through `configure_salesforce`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Schwab browser OAuth (`schwab_oauth`)

- WHAT: Schwab browser OAuth
- WHY: Browser OAuth using a registered HTTPS callback whose final redirect is replayed to the local HTTP listener.
- WHEN: No required_when condition is declared; starts through `configure_schwab`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_each_renewal).

### Snowflake RSA key pair (`snowflake_rsa`)

- WHAT: Snowflake RSA key pair
- WHY: 4096-bit RSA key-pair authentication; the selected user and role define the effective authorization boundary.
- WHEN: No required_when condition is declared; starts through `configure_snowflake`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).

### Zuora OAuth client credentials (`zuora_client_credentials`)

- WHAT: Zuora OAuth client credentials
- WHY: OAuth client-credentials flow scoped to the tenant selected by base URL.
- WHEN: No required_when condition is declared; starts through `configure_zuora`.
- WHAT DENIAL DOES: Not declared by this authentication-flow entry.
- SYSTEM SETTINGS PANE: Not applicable (authentication flow, not a macOS permission entry).
- GRANT ACTOR: operator (authentication presence: required_once).
