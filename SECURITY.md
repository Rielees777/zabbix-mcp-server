# Security Policy

## Reporting a Vulnerability

If you discover a security vulnerability in Zabbix MCP Server, please report it responsibly.

**Do not open a public GitHub issue for security vulnerabilities.**

Instead, contact us directly:

- **Email:** [info@initmax.com](mailto:info@initmax.com)
- **Subject:** `[SECURITY] Zabbix MCP Server — <brief description>`

We will acknowledge your report within 48 hours and work with you on a fix.

## Security Considerations

### Secrets

- All configuration comes from `ZABBIX_MCP_*` environment variables (or files in a secrets directory); there is no config file holding credentials on disk
- Credentials - Zabbix API tokens and passwords, the MCP bearer token, the SMTP password - are held as pydantic `SecretStr` values: they are masked in `repr`, log lines and validation errors
- In Kubernetes the pipeline injects them from HashiCorp Vault as environment variables (see [`.gitlab-ci.yml`](.gitlab-ci.yml) and [`helm/values-prod.yaml`](helm/values-prod.yaml)). The server never contacts Vault itself, so it needs no Vault credentials of its own
- Settings are read once at start-up; rotate a secret by restarting the pods
- Keep `.env` out of version control (`.gitignore` and `.dockerignore` already exclude it) and out of images

### MCP Client Authentication

- One static bearer token (`ZABBIX_MCP_SERVER__AUTH_TOKEN`) protects the HTTP and SSE endpoints; it is compared in constant time
- `ZABBIX_MCP_SERVER__AUTH_TOKEN_EXPIRES_AT` makes the token temporary: after that moment every request is rejected
- There are no per-client scopes: the token opens every tool enabled by `TOOLS` / `DISABLED_TOOLS`. Run separate instances (separate token, tool list and Zabbix credentials) for separate audiences
- `action_confirm` only accepts a confirmation token prepared by the same bearer token
- `raw_json=true` (drops the prompt-injection marker from responses) is refused unless the operator sets `ZABBIX_MCP_SERVER__ALLOW_RAW_JSON=true`
- Without a token the server is unauthenticated; the start-up log warns when it is bound to anything but localhost

### Zabbix Users Are Not Exposed

No tool reads or changes Zabbix users, user groups, user directories, roles, API tokens or MFA methods, and `zabbix_raw_api_call` refuses every method of those objects (`user.*`, `usergroup.*`, `userdirectory.*`, `role.*`, `token.*`, `mfa.*`, matched case-insensitively), so they cannot be reached by the MCP client even with a Zabbix API token that is allowed to. Global authentication settings (`authentication.get/update`) and user macros remain available. Other `*.get` tools can still show user ids that Zabbix returns as part of their own objects (for example `userid` in an action's operations).

### Zabbix Credentials

Zabbix API tokens inherit the permissions of their Zabbix user - use a dedicated, least-privilege user per server.

Zabbix 5.0 and 5.2 have no API tokens (introduced in 5.4) and are out of upstream support. For those two releases only, a Zabbix server entry may authenticate with `USERNAME`/`PASSWORD` via `user.login` - which means the MCP server holds a **user password** instead of a revocable, scoped token. This is a deliberate, operator-chosen security risk and the whole path is best effort: it is not in our test matrix. The fences:

- It has to be switched on explicitly with `LEGACY_AUTH=true`; a `USERNAME`/`PASSWORD` pair without the flag is a configuration error, never a silent fallback.
- `LEGACY_AUTH` and `API_TOKEN` on the same entry is refused, so an entry carries exactly one credential.
- **Version lock:** on connect the server reads `apiinfo.version` and refuses legacy login against Zabbix 5.4 or newer, so the flag cannot be used to avoid tokens on a release that has them.
- The `zabbix-utils` version gate (refuses < 6.0) is relaxed only for entries marked `LEGACY_AUTH`, never for token authentication.
- Every start logs a `WARNING` per legacy entry naming the Zabbix version.

### Network Security

- The server binds to `127.0.0.1` (localhost) by default - not accessible from the network
- If you bind to `0.0.0.0`, always configure the bearer token to protect the endpoint
- Native TLS support - set `TLS_CERT_FILE` and `TLS_KEY_FILE`, or terminate TLS at a reverse proxy / Ingress
- IP allowlist - set `ALLOWED_HOSTS` to restrict access to specific IPs or CIDR ranges
- CORS control - set `CORS_ORIGINS` to restrict which web origins may access the server; omit to disable CORS entirely
- `RATE_LIMIT` protects the Zabbix API from being overwhelmed (default: 300 calls/minute per client)

### Origin / Host validation (DNS rebinding protection)

Per the MCP spec (2025-11-25 and later), the server can reject requests whose `Origin` or `Host` header does not match the operator-declared allowlist (returns HTTP 403 / 421 respectively). This blocks DNS rebinding attacks against an MCP endpoint reachable from a browser context.

**Recommended minimum configuration for production**: set `ZABBIX_MCP_SERVER__PUBLIC_URL` to the externally-reachable URL of the server, e.g. `https://mcp.example.com`. The host is auto-added to the Host allowlist, the origin to the Origin allowlist, and DNS-rebinding protection flips on.

For additional origins set `ZABBIX_MCP_SERVER__ALLOWED_ORIGINS`, e.g. `https://app.example.com,https://office.example.com:*`.

When `PUBLIC_URL`, `ALLOWED_ORIGINS` and `ALLOWED_HOSTS` are all unset on a non-localhost bind, protection stays off and the server logs a startup warning pointing here.

### Read-Only Mode

- Zabbix servers are `READ_ONLY=true` by default
- This blocks all write operations (create, update, delete, execute) at the MCP server level, including via the `zabbix_raw_api_call` tool
- Two-step action approval (`action_prepare` + `action_confirm`) for write operations - 5-minute confirmation window, and the read-only flag is checked again at confirmation
- Set `READ_ONLY=false` only on servers where you explicitly need write access

### File Access

- The `source_file` feature (for `configuration.import`) is disabled by default
- To enable it, set `ZABBIX_MCP_SERVER__ALLOWED_IMPORT_DIRS` to specific directories from which files may be read
- Path traversal is blocked - only files within configured directories are accessible, validated with `Path.is_relative_to()`
- Symlinks are judged by where they resolve, not by being links: the path is resolved first, the containment check runs on the resolved target, the file is opened with `O_NOFOLLOW` (so a symlink swapped in as the final path component after the check is refused), and `fstat` then insists on a regular file (a directory, FIFO or device under an allowed directory is never read). This narrows the check-to-open window rather than closing it: a directory symlink introduced higher up the path in that window would still be followed, and hard links are invisible to resolution. Both require write access to a directory under `ALLOWED_IMPORT_DIRS`, which is why those directories should be writable only by the operator
- Report templates are the ones built into the package and render in a `SandboxedEnvironment`, which prevents server-side template injection

### Report Delivery (v1.35+)

`report_generate` can hand a finished PDF to a resource link, the filesystem, or a mailbox instead of returning it inline. All three are reachable from an AI client, so all three are fenced by the operator.

**Resource links** (`zabbix://reports/<id>`) hold the PDF in memory between the tool call and `resources/read`. The id is a random 122-bit token (uuid4 hex), not a filename or a sequence number, so links cannot be guessed or enumerated. Reads go through the same authenticated MCP endpoint as every other request - a link is not a public URL. The store is bounded by `ZABBIX_MCP_REPORTING__LINK_TTL` (default 1 h, max 24 h) and `ZABBIX_MCP_REPORTING__LINK_MAX_REPORTS` (default 20, oldest evicted first), so a client cannot pin memory by generating reports it never fetches.

**Download URLs** (`GET /reports/<id>.pdf`, v1.36+, on by default) publish the same report at an ordinary URL so a person can open it in a browser. This is a **capability URL**: the same random 122-bit id (uuid4) is the only credential, and the route is deliberately unauthenticated because an AI client hands the link to a human who has no bearer token. That trade is bounded by the link lifetime (`LINK_TTL`), the store cap, and the fact that one id yields exactly one report. Responses carry `Content-Disposition: attachment`, `Cache-Control: no-store, private`, `Referrer-Policy: no-referrer` (so the id does not leak through a `Referer` header) and `X-Content-Type-Options: nosniff`. Set `ZABBIX_MCP_REPORTING__DOWNLOAD_URLS=false` to serve the MCP resource link only.

A URL is only built from an address somebody explicitly vouched for: `ZABBIX_MCP_SERVER__PUBLIC_URL`, or `X-Forwarded-Host` / `-Proto` sent by a peer listed in `ZABBIX_MCP_SERVER__TRUSTED_PROXIES`. Nothing is inferred - not from the local bind, and not from a bare `Host` header. Both would resolve to loopback in the very deployment where a link matters (a server bound to `127.0.0.1` behind a proxy; with no host allowlist configured the MCP SDK also rejects any non-loopback `Host` outright), and a remote user handed `http://127.0.0.1:8080/reports/<id>.pdf` would send a live report id to whatever answers on their own machine. A forwarded value is accepted only as a bare authority, so no header can smuggle a path, a query or a second origin into the link; the *last* element of a forwarded chain is taken, since a proxy that appends leaves the client's own value in front. No URL is emitted on stdio transport either - there is no HTTP listener.

The route is served outside the MCP endpoint's `TransportSecuritySettings`, so it carries no `Host` / `Origin` validation and no bearer requirement. Unless `ZABBIX_MCP_SERVER__CORS_ORIGINS` is configured - which wraps the whole application, this route included - it emits no CORS headers, so a hostile page cannot read a response even if it guessed an id. Serving these links over plaintext HTTP on a non-loopback bind logs a startup warning - the id travels as a bearer credential and the PDF is unencrypted in transit. Because the download route is served by the MCP backend, a reverse proxy that forwards an explicit list of paths must include `/reports/`.

**Filesystem** delivery is refused unless `ZABBIX_MCP_REPORTING__OUTPUT_DIR` names an absolute, existing directory. The AI client cannot supply a path or a filename: the name is generated server-side from report type, host group and timestamp, sanitised to `[A-Za-z0-9._-]`, and the resolved target is asserted to stay inside the configured directory before the write. Files are written `0640`.

**Email** delivery is refused unless `ZABBIX_MCP_REPORTING__EMAIL__ENABLED` is set **and** the recipient matches `ALLOWED_RECIPIENTS`. The allowlist is mandatory - the settings loader refuses to enable email without it, because an LLM that can pick an arbitrary recipient is an exfiltration channel for monitoring data. Entries are exact addresses or `*@domain` globs; `*` disables the restriction entirely and should be a deliberate choice. Attachments are capped at 25 MB and SMTP failures are reported without echoing credentials. The SMTP password is a secret setting (`SecretStr`): it comes from the environment or Vault and is masked in logs and error messages.

## Supported Versions

| Version | Supported |
|---|---|
| 2.0 (latest) | Yes |
| 1.39 | Yes |
| 1.38 | Yes |
| 1.37.4 | Yes |
| 1.37.3 | Yes |
| 1.37.2 | Yes - upgrade, see advisory below |
| 1.37.1 | Yes |
| 1.37 | Yes |
| 1.36.2 | Yes |
| 1.36.1 | Yes |
| 1.36 | Yes |
| 1.35 | Yes |
| 1.34 | Yes |
| 1.33 | Yes |
| 1.32 | Yes |
| 1.31 | Yes |
| 1.30 | Yes |
| < 1.30 | No |

Versions older than 1.30 no longer receive fixes. Note that 2.0 changed how the server is configured (environment variables instead of `config.toml`, no admin portal) - see the changelog before upgrading.
