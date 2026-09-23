# Cross-Solet Sealed-Box Secret Transfer

## When to use this

Use the sealed-box protocol when one solet needs to hand a credential
(API key, OAuth token, bot secret, etc.) to another solet and the
plaintext must never enter the agent's context, transcript, MCP log, or
any other caller-reachable surface.

The canonical case is bootstrapping a fresh cloud solet: it boots
empty, the operator's home solet has the keys, and the four processes
documented here move them across without ever passing through the
agent's context window.

Do not use this protocol for storing your own secrets locally — use
`store` / `store_from_env` / `store_from_file` / `store_from_kv_file` /
`store_from_keychain` for that. The transfer protocol is for moving
secrets between separate vault instances.

## The protocol

The transfer is three calls plus an idempotent bootstrap, all driven by
the operator agent. Plaintext exists only inside the two vault entries (at
rest, protected by each solet's vault substrate — the macOS Keychain
locally, AWS Secrets Manager + KMS in the cloud) and momentarily inside the
sender's `export_encrypted` and the recipient's `import_encrypted` process
frames.

```
Bootstrap (each solet, one time, idempotent):
    solet.vault.ensure_encryption_keypair()
        -> { created: bool, public_key: <base64 X25519 pubkey> }

Recipient publishes its public key:
    recipient.vault.get_public_key()
        -> { public_key: "<base64 X25519 pubkey>" }

Sender seals the secret for the recipient:
    sender.vault.export_encrypted(
        secret_name="anthropic_api_key",
        recipient_pubkey="<recipient pubkey from above>",
        recipient_identifier="kepler")
        -> { ciphertext: "<base64 sealed box>",
             plaintext_fingerprint: "sha256:abc123..." }

Recipient unseals and stores:
    recipient.vault.import_encrypted(
        name="anthropic_api_key",
        ciphertext="<from above>",
        sender_identifier="newton")
        -> { ok: true,
             plaintext_fingerprint: "sha256:abc123..." }
```

## Confirming intact transfer

The `plaintext_fingerprint` returned by `export_encrypted` and
`import_encrypted` is `sha256(plaintext)` truncated to 16 hex chars,
prefixed with `sha256:`. It is one-way and short; safe to log, print,
and compare. After step 4 the agent compares the two fingerprints; an
exact match means the transfer is byte-for-byte identical to what the
sender exported.

A mismatch means the transfer was tampered with or the wrong ciphertext
landed in the wrong place. Delete the imported secret and re-run from
step 3.

## What this protects against

- **Agent context leakage.** The full agent transcript can be published
  and the secret is still safe — the only string the agent ever holds is
  base64 ciphertext.
- **MCP transport interception.** If TLS to the bridge is somehow MITM'd
  the attacker sees ciphertext, not plaintext.
- **Recipient impersonation by an attacker.** Only the recipient's
  private key (never exposed) can decrypt the ciphertext.
- **CloudWatch / audit log leakage.** The audit record carries
  fingerprints and peer identifiers; no plaintext or ciphertext.

## What this does not protect against in v1

- **Sender impersonation.** Sealed boxes are anonymous-sender by design.
  Anyone with the recipient's public key can seal an arbitrary
  ciphertext; the recipient accepts it as long as it decrypts. If
  sender-authentication matters in your deployment, wait for v2
  (Ed25519 sender signatures) or gate access at the MCP bearer-token
  layer.
- **Per-secret export ACL.** v1 lets the sender export anything it has
  in its vault. v2 adds an `exportable_to=<recipient pubkey list>`
  metadata field on each secret.

See `workbench/2026-05-17_homunculus_aws_deployment_plan.md`
§13 for the broader cloud-deployment context that motivated this
protocol.

## Audit log

Every `export_encrypted` and `import_encrypted` call appends one row to
the Postgres-backed `default_vault_plugin__secret_transfer_audit` table,
written through the platform `Store` abstraction. The schema lives in
`plugins/macos_vault_plugin/src/macos_vault_plugin/schema.py` under
`get_secret_transfer_audit_schema()` and is installed on first boot via
the standard `install_plugin_schema` lifecycle.

| Column | Example | Notes |
|---|---|---|
| direction | `"export"` or `"import"` | which side of the transfer |
| secret_name | `"anthropic_api_key"` | the local vault key |
| peer_identifier | `"kepler"` | free-text identifier the caller supplied; nullable |
| peer_pubkey_fingerprint | `"sha256:..."` | sha256 of the peer pubkey, truncated |
| plaintext_fingerprint | `"sha256:..."` | one-way fingerprint, same value returned to the caller; empty string on early-failure paths |
| status | `"success"` or `"error"` | |
| error_message | `null` or short token | populated only on failure; never plaintext |
| created_at | ISO-8601 | auto-injected by the Store backend |
| updated_at | ISO-8601 | auto-injected by the Store backend |
| id | `"sta-..."` | auto-generated row id |

Query the audit log directly:

```sql
SELECT direction, secret_name, peer_identifier, plaintext_fingerprint,
       status, error_message, created_at
FROM default_vault_plugin__secret_transfer_audit
ORDER BY created_at DESC LIMIT 50;
```

The audit table carries fingerprints and identifiers only — never
plaintext, never ciphertext.

## Anti-patterns

- **Never log the plaintext.** Not at DEBUG, not "temporarily." If you
  need to debug a transfer, compare fingerprints.
- **Never return the plaintext from `export_encrypted` or
  `import_encrypted`.** The fingerprint is the only way the calling
  agent confirms transfer integrity.
- **Never store the X25519 private key in a plugin attribute.** Read it
  from the vault each time it is needed and let it leave scope at
  function exit. A leaked in-memory reference outlives the function and
  shows up in debuggers, crash dumps, and traceback locals.
- **Never give the keypair its own special storage.** Both halves are
  stored as ordinary vault entries under
  `identity__encryption_private_key` and `identity__encryption_public_key`
  and ride the same at-rest path as every other secret (the macOS Keychain
  locally; AWS Secrets Manager in the cloud).

## See also

- `plugins/macos_vault_plugin/knowledge_base/articles/secret_transfer_protocol_design.md` —
  protocol design and threat model.
- `plugins/macos_vault_plugin/tests/secret_transfer_protocol_smoke.py` —
  end-to-end verification of the four verbs against a running solet via MCP.
- `workbench/2026-05-17_homunculus_aws_deployment_plan.md` §13 —
  the architectural context (why cloud-bootstrap needs this).
