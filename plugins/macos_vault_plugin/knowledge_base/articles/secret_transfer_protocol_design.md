# Sealed-box secret transfer between solets

## Goal

Move a secret value from one solet's vault to another's vault. The
plaintext must never enter the LLM context. The transfer must be
auditable.

## Primitive

libsodium's **anonymous-sender sealed boxes** (`crypto_box_seal`), via
the `pynacl` library. Recipient has an X25519 keypair. Sender encrypts
with the recipient's public key. Only the recipient's private key can
decrypt. The recipient cannot identify the sender from the ciphertext
alone — that's the "anonymous-sender" property.

## Keypair lifecycle

Each solet, on first boot, generates an X25519 keypair and stores
both halves in its own vault under:

- `vault://identity/encryption_keypair/private` (32 bytes, NEVER leaves the vault)
- `vault://identity/encryption_keypair/public` (32 bytes, durable identity)

The public key is the durable identity of the solet. If it changes,
every sender who has the old key needs the new one before they can send
again. So treat key rotation as a rare, deliberate event with explicit
notification to peers.

## Flow

Three MCP calls, orchestrated by the agent driving the transfer:

```
1. agent → recipient.vault.get_public_key()
       returns: { "public_key": "<base64-X25519-pub>" }

2. agent → sender.vault.export_encrypted(
       secret_name="anthropic_api_key",
       recipient_pubkey="<base64-X25519-pub>")
       returns: { "ciphertext": "<base64-sealed-box>",
                  "plaintext_fingerprint": "sha256:abc123…" }

3. agent → recipient.vault.import_encrypted(
       name="anthropic_api_key",
       ciphertext="<base64-sealed-box>")
       returns: { "ok": true,
                  "plaintext_fingerprint": "sha256:abc123…" }
```

The agent compares the two fingerprints. Match → the transfer landed
intact. The agent never saw the plaintext.

## Where plaintext exists

Only in three places, ever:

1. The sender's vault row (encrypted at rest by Postgres / RDS).
2. The sender's `export_encrypted` process memory during the call.
3. The recipient's vault row (encrypted at rest), after `import_encrypted`.

Plaintext **never** crosses a process boundary. The MCP responses
contain only public keys, ciphertext, and fingerprints — all
cryptographically safe to be in any log, any context window, any
transcript.

## What this protects against

- **Agent context leakage.** The agent's full transcript can be made
  public and the secret is still safe.
- **MCP-transport interception.** Even if HTTPS to the bridge is
  somehow MITM'd, the attacker sees only ciphertext.
- **CloudWatch logs / audit trails.** The export process logs the
  operation (recipient pubkey, secret name, timestamp, fingerprint) but
  not the plaintext.
- **Recipient impersonation.** Only the recipient's private key can
  decrypt; no impersonator can extract the plaintext from the
  ciphertext.

## What this does not protect against (yet)

- **Sender impersonation.** Sealed boxes are anonymous-sender by
  design. Anyone with kepler's public key can encrypt something. If the
  agent says "kepler, import this ciphertext I claim is from the origin solet,"
  kepler accepts it. To fix, add a separate Ed25519 identity-signing
  keypair on the sender side and a `sender_signature` field in
  `export_encrypted` / `import_encrypted`. Recipient verifies the
  signature against the claimed sender's known identity pubkey before
  importing. ~3 extra lines per side. See "v2" below.

- **Unauthorized export by the sender.** Sealed boxes don't decide what
  *should* be exported — only the bearer-token-authenticated MCP
  session controls that. For finer access control: a per-secret ACL.
  `vault://secrets/anthropic_api_key` carries `exportable_to=["kepler_pub_abc...",
  …]`. `export_encrypted` checks the ACL before encrypting; refuses
  with a clear error if the recipient isn't on the list.

## Audit trail

Every `export_encrypted` and `import_encrypted` call records an audit
entry. **Where the entry lives depends on which assignment ships
first** (see the assignment doc's FAQ for the decision tree): a
`Store`-backed table if the unified Store abstraction landed first, a
Python-logger structured record otherwise.

Field list (identical either way):

| field | example | notes |
|---|---|---|
| direction | `"export"` or `"import"` | which side of the transfer |
| secret_name | `"anthropic_api_key"` | the name in the local vault |
| peer_identifier | `"kepler"` | free-text from `recipient_identifier` / `sender_identifier`; may be null |
| peer_public_key_fingerprint | `sha256:abc123...` | sha256 of the peer pubkey, truncated — **not** the full pubkey |
| plaintext_fingerprint | `sha256:def456...` | already returned by the process; one-way |
| status | `"success"` or `"error"` | |
| error_message | `null` or short message | populated only on failure; no plaintext or ciphertext |
| timestamp | `2026-05-18T22:34:56Z` | UTC, ISO-8601 |
| caller_agent | optional, from MCP bearer-token metadata | identifies which agent initiated the call |

**What is never written here:** the plaintext secret. **Probably not
written:** the ciphertext (large, not useful for queryable history),
the full peer pubkey (use the fingerprint instead). Only deviate from
"fingerprint only" if a specific operator workflow requires it — and
document the deviation.

## Versioning

**v1** (this design): sealed boxes only. Recipient-authenticated,
sender-anonymous. Suitable for cases where the bearer token + the
MCP session are the trust boundary.

**v2** (planned): add Ed25519 sender signatures. Recipient verifies
the sender's identity before importing. Required if we ever federate
beyond a single trust domain (e.g., a solet owned by a different
operator).

**v3** (speculative): post-quantum migration. Hybrid X25519 + ML-KEM.
Out of scope for now.
