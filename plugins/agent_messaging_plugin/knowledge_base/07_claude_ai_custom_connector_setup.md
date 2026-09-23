# Cloud-Connector Setup — claude.ai + ChatGPT against a Cloud Solet

## When to use this

The operator wants to reach a cloud solet from an external MCP client
that uses OAuth 2.1: claude.ai web custom-connectors, Claude Desktop, and
(in development) ChatGPT custom-connectors. The transport is Streamable HTTP
MCP over HTTPS, authenticated by an OAuth-2.1-minted sealed-box bearer token.

Cloud solets sit behind an Application Load Balancer with an ACM-issued
public cert — TLS is ALB-native, no Caddy or mkcert involved. The OAuth 2.1
surface served by `agent_messaging_plugin/mcp_streamable/` is the
authentication layer. Local solets reachable from external MCP clients
need a tunnel (OpenAI secure tunnel or equivalent) on top — that's a
separate workstream, not this article.

For the daily Claude Code CLI + Codex CLI use case, this article does not
apply: those use the stdio MCP bridge (`python -m
agent_messaging_plugin.mcp_bridge`), not the Streamable HTTP transport.

## OAuth 2.1 endpoints exposed by the solet

| Method + path | Purpose |
|---|---|
| `GET /.well-known/oauth-authorization-server` | RFC 8414 metadata: issuer, token_endpoint, supported grant types (no `client_credentials` advertised), scopes. `registration_endpoint` is intentionally absent — Dynamic Client Registration is hard-disabled (Task #31). |
| `GET /.well-known/oauth-protected-resource` | MCP 2025-06-18 §authorization metadata: resource URL + authorization_servers. |
| `GET /authorize` | RFC 6749 §4.1.1 authorization endpoint, PKCE S256 mandatory. |
| `POST /oauth/token` | Accepts `authorization_code` (claude.ai / ChatGPT), `refresh_token` (rotation), and `client_credentials` (operator-created machine clients). Every grant verifies `operator_approved` AND that the requested grant is in the client's stored `grant_types`. |
| `POST /register` | Returns **404** — DCR is hard-disabled. Use the `oauth_client_register` verb to mint pre-registered clients. |
| `OPTIONS /api/v1/mcp/streamable` | CORS preflight for browser-driven clients. Returns 204 + headers. |

The `/api/v1/mcp/streamable` endpoint additionally emits
`WWW-Authenticate: Bearer ..., resource_metadata="<URL>"` on every 401 so
the connector validator can discover the authorization server during
recovery.

## Provisioning a client for claude.ai

1. Register an operator-approved client via the vault verb:

   ```
   process_call(
     process_key="service_interface::vault_service::oauth_client_register",
     arguments={
       "client_name": "claude-ai",
       "scopes": ["mcp:read", "mcp:write"],
       "redirect_uris": ["https://claude.ai/api/connectors/<slug>/callback"],
       "grant_types": ["authorization_code", "refresh_token"]
     }
   )
   ```

   The result payload contains `client_id` and a one-time `client_secret`.
   Copy them — the secret is only stored as a scrypt hash and never
   re-emitted by `oauth_client_list`. The minted client is automatically
   `operator_approved=True`.

2. In claude.ai's custom-connector form (Advanced Settings):

   | Field | Value |
   |---|---|
   | Server URL | `https://<name>.acute-focus.com/api/v1/mcp/streamable` |
   | Client ID | the `client_id` from step 1 |
   | Client Secret | the `client_secret` from step 1 |

3. claude.ai walks through `/authorize` (PKCE S256), redeems the code at
   `/oauth/token`, and uses the resulting access_token as
   `Authorization: Bearer <token>` on subsequent MCP requests. The access
   token has a 24h TTL; claude.ai rotates via refresh_token silently within
   the 30-day refresh window.

## Provisioning a client for ChatGPT

Same `oauth_client_register` shape, with `client_name="chatgpt"` and
ChatGPT's redirect URI in `redirect_uris`. The token endpoint and
authorization flow are identical — both consumers speak the same OAuth 2.1
spec.

## Token shape

The access_token returned by `/oauth/token` is a libsodium sealed box of
this JSON claim, base64url-encoded:

```json
{
  "agent_id":          "claude_phone",
  "agent_instance_id": "agi-oauth-<client_id>",
  "issued_at":         "<UTC ISO-8601>",
  "session_label":     "<client_name>",
  "scopes":            ["mcp:read", "mcp:write"]
}
```

`agent_instance_id` is derived deterministically from `client_id` so
repeated token fetches for the same client surface under one `peer_list`
row. The existing `BearerVerifier` accepts the token unchanged — OAuth
only wraps the *issuance*.

## Revoking a client

```
process_call(
  process_key="service_interface::vault_service::oauth_client_revoke",
  arguments={"client_id": "client-<32 hex>"}
)
```

Idempotent: returns `removed=0` when the client_id is already absent.
Existing tokens issued for that client remain valid until they fall
outside the skew window (5 min), so revocation is eventually consistent —
re-issue or wait for natural expiry.

## Smoking the OAuth surface

```
.venv/bin/python3 \
    plugins/agent_messaging_plugin/tools/oauth_surface_smoke.py \
    --url https://<name>.acute-focus.com \
    --client-id client-<32 hex> \
    --client-secret <secret>
```

Exercises seven cases:

1. `GET /.well-known/oauth-authorization-server`
2. `GET /.well-known/oauth-protected-resource`
3. `POST /oauth/token` (form body)
4. `POST /oauth/token` (HTTP Basic)
5. `POST /oauth/token` with bad secret → 401 invalid_client
6. `OPTIONS /api/v1/mcp/streamable` → 204 + CORS headers
7. `POST /api/v1/mcp/streamable` initialize with the OAuth token → 200 + `Mcp-Session-Id`

## Smoking the full Streamable HTTP MCP transport

```
.venv/bin/python3 \
    plugins/agent_messaging_plugin/tools/streamable_mcp_smoke.py \
    --url https://<name>.acute-focus.com
```

End-to-end transport smoke: initializes two sessions, exercises the full
tool surface, peer-sends a cross-session message (delivery is unconditional
since A4, 2026-08-04), and verifies the SSE channel delivers the wake
notification.

## What's NOT in this article

- **Stdio MCP bridge** for local Claude Code CLI + Codex CLI — uses
  `python -m agent_messaging_plugin.mcp_bridge`, not Streamable HTTP. See
  `02_platform_call_surface.md`.
- **Tunnel-based local-solet access** — OpenAI secure tunnel for
  ChatGPT and (eventually) Anthropic equivalent for claude.ai reaching a
  *local* solet. Separate workstream; verbs TBD.
- **Direct phone-on-Wi-Fi access via Caddy + mkcert + iOS trust profile** —
  retired 2026-06-15. That paradigm is superseded by Claude Code's `/rc`
  remote-control and Codex's mobile session-share, both of which route
  through Anthropic/OpenAI infrastructure without exposing the local
  solet to direct external HTTPS.
