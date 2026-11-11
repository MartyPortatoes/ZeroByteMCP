# Zerobyte MCP for MetaMCP

**Status:** a locally tested integration candidate, not full API coverage or proof of
production backup recoverability. The synthetic test suite does not establish live
API compatibility or deployed MetaMCP enforcement. This project is not an official
ZeroByte release and has not been submitted to the upstream maintainers.

One combined MCP server named `zerobyte` exposes both read and write tools. Attach that
same upstream to `zerobyte-read` and `zerobyte-write` MetaMCP namespaces and classify
individual tools. There are not separate read-only and read/write upstream servers.

## Implementation

- Official Python MCP SDK's `mcp.server.fastmcp.FastMCP`, pinned to `mcp==1.29.0`.
  This is the SDK's FastMCP, not the separately distributed `fastmcp` package.
- Streamable HTTP at `/mcp`; one bearer-protected upstream.
- Explicit typed tools and a fixed method/path registry. No arbitrary URL, method,
  HTTP body, shell, Restic-command, or OpenAPI auto-import tool.
- A fixed API origin and server-held `X-API-Key`. Credentials are read from approved
  secret files only at process startup, never accepted as tool arguments.
- Positive response-field projections omit connection configurations, webhook headers
  and bodies, raw error/log strings, task inputs, file listings and backup data.
- 5-second connect / 30-second request timeout, four connections, 1 MiB response limit,
  1,000-item list cap, explicit pagination, no redirect following or automatic retries.
- Compressed upstream responses are refused before body consumption, preventing
  decompression from bypassing the response-size limit.
- Strict resource IDs reject path traversal and URL/query injection.
- Bearer auth and DNS-rebinding Host validation apply to the HTTP endpoint.
- The configured upstream API key is scrubbed from projected string output as defense
  in depth. This is not a general guarantee that arbitrary user-entered labels contain
  no sensitive information.

## Tool catalog

### Read namespace: 9 tools

| Tool | Zerobyte operation | Meaning |
|---|---|---|
| `backups_list` | `GET /api/v1/backups` | Stored schedule metadata, last/next run and status |
| `backup_get` | `GET /api/v1/backups/{shortId}` | One stored schedule |
| `repositories_list` | `GET /api/v1/repositories` | Stored repository inventory and health |
| `repository_get` | `GET /api/v1/repositories/{shortId}` | One stored repository |
| `repository_stats` | `GET /api/v1/repositories/{shortId}/stats` | Stored stats, possibly stale or empty |
| `volumes_list` | `GET /api/v1/volumes` | Stored source-volume status |
| `tasks_list` | `GET /api/v1/tasks` | Active tasks and numeric progress |
| `task_get` | `GET /api/v1/tasks/{taskId}` | Status of one active/recent in-memory task |
| `task_history` | `GET /api/v1/tasks/history` | One page of persisted history |

### Write namespace: 4 tools

| Tool | Zerobyte operation | Consequence |
|---|---|---|
| `backup_run` | `POST /api/v1/backups/{shortId}/run` | Starts an existing schedule, including its configured webhooks, retention/pruning and mirrors |
| `task_cancel` | `POST /api/v1/tasks/{taskId}/cancel` | Requests cancellation; work can be left incomplete |
| `repository_stats_refresh` | `POST /api/v1/repositories/{shortId}/stats/refresh` | Runs repository I/O and updates stored statistics |
| `repository_snapshots_refresh` | `POST /api/v1/repositories/{shortId}/snapshots/refresh` | Invalidates and refreshes the snapshot cache using Restic |

These tools being available does not authorize executing them. Client confirmation
policy still applies to the specific resource and consequences. In particular, a manual
backup can cause retention pruning, so it is not a harmless write canary.

The wrapper intentionally does not add a self-attested `confirm=true` argument. Such an
argument would not be an authorization boundary. Tool annotations are also hints only.

## Exact coverage and omissions

`deployment/api-coverage.json` enumerates all **97 operations in the generated v0.44.0
client SDK**, with exact methods and paths: **13 implemented, 84 omitted**. This count
describes that SDK, not every possible route exposed by the application or auth library.

Omissions include schedule creation/edit/deletion; retention/forget; snapshot listing,
file listing, tagging, deletion, restore and dump; repository creation/edit/deletion,
doctor, unlock and arbitrary execution; source-volume creation/edit/deletion, mount,
unmount and connection checks; filesystem browsing; mirror management; notification
configuration/testing; auth/API-key/user/organization/SSO operations; configuration
export/import; recovery-key download; remote-agent enrollment; and unbounded SSE feeds.

The read tools support audit coverage of stored schedules, source/repository status,
active work and persisted run outcomes. The write subset supports authorized reruns,
cancellation and cache/stat refresh. **It is not sufficient for full backup repair or
recoverability proof.** It cannot restore a file, fix host filesystem permissions,
edit a broken schedule, repair/unlock a repository, or establish a restore canary.
Those need further explicitly scoped tool implementation and approval for actual use.

### Why not import every GET as read-only?

Version-matched source review found:

- Backup GET payloads contain webhook configuration and nested storage configuration.
  Raw JSON would expose fields outside normal audit needs.
- Snapshot listing/detail GETs invoke Restic and populate caches when not cached.
- Volume detail GET probes mounted storage. The wrapper uses stored volume listing instead.
- Snapshot dump GET exports backup file contents.

HTTP verbs alone are not the permission classification. The initial read catalog is
stored operational metadata, not a promise that the entire request has zero incidental
effects. For example, authentication can update API-key usage metadata.

## Namespace boundary and known MetaMCP limits

`deployment/namespace-manifest.json` is a **review manifest**, not an undocumented import
API payload. It records the exact 9/4 split. The same upstream serves all 13 tools.

MetaMCP v2.4.22's filter checks both discovery and invocation. Explicit INACTIVE mappings
are rejected by its call middleware. Its implementation also **allows missing mappings,
unparseable names, unresolved servers, and some lookup errors**. Therefore:

1. Persist and explicitly classify all 13 tools in both namespaces. In the read namespace,
   every write tool must be INACTIVE; in the write namespace, every read tool must be INACTIVE.
2. Validate real invocation filtering, not just the advertised catalog.
3. Require a complete catalog/mapping review before upgrades. This wrapper does not
   automatically expose new Zerobyte routes, but a later wrapper release can add tools.
4. Do not claim endpoint API keys are scoped merely because their variable names differ.
   Verify a read credential cannot reach the write endpoint if credential separation is required.
5. An upstream bearer credential grants access to both tool groups. Anyone able to call
   the upstream directly can bypass the namespace split. Keep it unexposed to clients.

The ZeroByte API key itself inherits its user's organization-backed operation powers.
The reviewed v0.44 backup controller uses `requireAuth`, not a read-only API-key scope.
Calling the key “RO” would not restrict it. A compromised wrapper or stolen upstream key
can reach operations outside the curated MCP catalog. The requested namespace split
is retained; it is not represented as upstream credential-level isolation.

## Container deployment

The `Dockerfile` uses the official Python image pinned to a content digest:

```text
python:3.12-slim@sha256:4840d514c583a4d3d8cad3940e68dee6f0f6f3635c12fcd0a3d74c21a5b93a54
```

Build the image from this repository, then review and pin the resulting runtime image
before production deployment:

```sh
docker build -t local/zerobyte-mcp:0.1.0 .
```

`deployment/compose.yaml` is an example for an existing private Docker network shared
with MetaMCP. It publishes no host ports, mounts no Docker socket or backup data, drops
all Linux capabilities and runs as UID/GID 10001 with a read-only root filesystem.
Adjust the example network name to your installation, rather than creating or changing
network access implicitly.

Supply these deployment settings through your approved configuration/secret manager:

- `ZEROBYTE_BASE_URL`: a fixed ZeroByte origin, such as `https://zerobyte.example.com`.
  No paths, query parameters or embedded credentials are accepted.
- `ZEROBYTE_API_KEY_SECRET_FILE`: host path to the already-approved ZeroByte API-key file.
- `MCP_BEARER_SECRET_FILE`: host path to the already-approved MCP bearer-token file.
  The bearer must be at least 32 ASCII non-whitespace characters.
- `ZEROBYTE_ALLOW_INSECURE_HTTP`: defaults to `false`. Use TLS or an encrypted route.
  Explicitly setting `true` sends the API key in cleartext over HTTP.

The template mounts secrets as `/run/secrets/zerobyte_api_key` and
`/run/secrets/zerobyte_mcp_bearer`; ensure UID 10001 can read them. Do not commit secret
files, paste credentials into chat/logs, or place their contents in Compose YAML.
Credential creation, ongoing access and endpoint changes require appropriate operator
approval. This repository does not create or rotate credentials.

Application startup is `python -m zerobyte_mcp.server`. The container listens on
`0.0.0.0:8000` and the example internal MCP URL is `http://zerobyte-mcp:8000/mcp`.
The example Host allowlist contains `zerobyte-mcp:8000`; adjust it only to the actual
approved destination. Without container settings, startup defaults to loopback.

The MetaMCP-to-wrapper bearer link in this example is HTTP over a private Docker
bridge. Keep it on the trusted host/network or use TLS. Anyone with the upstream bearer
can call all 13 tools, bypassing the namespace split.

### Before deployment

1. Recheck the running ZeroByte revision against the v0.44.0 adapter and review API drift.
2. Build/test the image, review and lock transitive dependencies, perform a dependency
   vulnerability audit, validate secret-file readability and validate Compose on the host.
3. Verify the approved network route and authenticated API compatibility without running
   consequential writes. Synthetic fixtures do not replace these checks.
4. Back up the relevant MetaMCP configuration through an approved administrative route.
5. Register one upstream, then persist and classify every tool in both namespaces before
   exposing authenticated endpoints. See the exact namespace review manifest.

### Deployment acceptance tests

1. Wrapper without/wrong bearer: 401. Correct bearer: MCP initialize and tools/list succeed.
2. Direct wrapper catalog: exactly 13 reviewed names; no resources/prompts containing secrets.
3. MetaMCP read endpoint: exactly 9 names; write endpoint: exactly 4 names. Names should
   have MetaMCP's `zerobyte__` prefix, verified from live discovery rather than assumed.
4. Call each known write name through the read endpoint with an invalid ID such as `!`.
   Require MetaMCP's **inactive-tool denial**, not just a schema error. A bypass would
   reach the wrapper's ID validator but cannot send an API request. This checks name-based
   invocation filtering without starting a real backup or cancellation.
5. Unknown tool name: rejected. Read credentials against write endpoint: test tools/list,
   not a write. Report any cross-endpoint access instead of inferring key scoping.
6. Run a representative read canary and inspect only projected output. Verify stored
   status/time semantics against ZeroByte UI; old tasks may require history rather than task_get.
7. Test production writes only after separate approval for an appropriate real operation.
   Never use backup_run as an automatic integration canary.
8. Recheck existing MetaMCP endpoints. Rollback removes only the new registration,
   namespace/endpoint entries and wrapper container; revoke newly created keys if retired.

## Development and local validation

Python 3.12 or newer is required. The package pins the top-level runtime dependencies
in `pyproject.toml`; transitive dependencies are not yet locked.

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e .
PYTHONPATH=src python -m unittest discover -s tests -v
PYTHONPATH=src python -m compileall -q src tests
```

The current synthetic suite contains **28 tests**. Transport tests use `httpx.MockTransport`
against reserved `.invalid` domains with never-valid fixture credentials. Streamable
HTTP is exercised in-process using ASGI. No live credentials are needed.
A dependency-free regression checks the template's quoted, single-mount `tmpfs`
flow list and required options; it does not validate full Compose YAML or runtime.

Coverage includes combined catalog/annotations, fixed routes, output projections,
IDs/queries, unknown tools, credential redaction, timeout/no-retry behavior, redirects,
non-JSON/oversize/compressed responses, bearer authentication, Host/Origin rejection,
duplicate-auth/body limits, shutdown lifecycle, initialize/list/call and namespace
manifest coverage. See `VALIDATION.md` for the exact verification scope and limitations.

## License and upstream status

No project license has been selected. No open-source license grant is implied by this
repository. Choose a license and review dependency license obligations before public
release or submission to the ZeroByte maintainers. References to upstream projects
identify compatibility and implementation sources, not endorsement.

## Primary sources

- [Version-matched ZeroByte SDK and API paths](https://github.com/nicotsx/zerobyte/blob/a1b6d0cd623312d4e3be3216c643d8f6943270b9/app/client/api-client/sdk.gen.ts)
- [Version-matched API-key authentication middleware](https://github.com/nicotsx/zerobyte/blob/a1b6d0cd623312d4e3be3216c643d8f6943270b9/app/server/modules/auth/auth.middleware.ts)
- [Version-matched backup controller](https://github.com/nicotsx/zerobyte/blob/a1b6d0cd623312d4e3be3216c643d8f6943270b9/app/server/modules/backups/backups.controller.ts)
- [Version-matched repository service](https://github.com/nicotsx/zerobyte/blob/a1b6d0cd623312d4e3be3216c643d8f6943270b9/app/server/modules/repositories/repositories.service.ts)
- [Version-matched volume service](https://github.com/nicotsx/zerobyte/blob/a1b6d0cd623312d4e3be3216c643d8f6943270b9/app/server/modules/volumes/volume.service.ts)
- [MetaMCP v2.4.22 invocation filter](https://github.com/metatool-ai/metamcp/blob/v2.4.22/apps/backend/src/lib/metamcp/metamcp-middleware/filter-tools.functional.ts)
- [Official Python MCP SDK](https://github.com/modelcontextprotocol/python-sdk)

