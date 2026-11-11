"""One combined FastMCP upstream; MetaMCP provides the requested namespace split."""
from __future__ import annotations

import hmac
import logging
import os
from pathlib import Path
from typing import Annotated, Literal

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import Field
from starlette.responses import JSONResponse
import uvicorn

from .client import ZerobyteClient, SafeError, OPERATIONS
from .projections import pick, pick_numbers, record, records, STATS

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$", strict=True)]
TaskKind = Literal["backup", "restore", "deleteSnapshots", "tagSnapshots", "doctor", "mirrorSync", "forget"]
Outcome = Literal["running", "success", "warning", "error", "cancelled", "stale"]


def create_server(client: ZerobyteClient, *, allowed_hosts: list[str] | None = None) -> FastMCP:
    mcp = FastMCP(
        "zerobyte", host="0.0.0.0", streamable_http_path="/mcp", stateless_http=True,
        json_response=True, max_request_body_size=65536,
        instructions="Curated Zerobyte metadata and operator tools. MetaMCP must separate read/write tool catalogs. Tool annotations are hints, not authorization. Confirm consequential operations under the client's policy.",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts or ["localhost", "localhost:*", "127.0.0.1", "127.0.0.1:*", "zerobyte-mcp:8000"],
            allowed_origins=[],
        ),
    )

    def tool(name, description):
        read = OPERATIONS[name][2] == "read"
        return mcp.tool(name=name, description=description,
            annotations=ToolAnnotations(readOnlyHint=read, destructiveHint=not read,
                                        idempotentHint=read, openWorldHint=name == "backup_run"))

    @tool("backups_list", "List stored backup schedule status/times. Returns projected metadata, not webhook headers, storage credentials, raw errors, or backup contents.")
    async def backups_list() -> dict:
        return {"items": client.scrub(records(await client.call("backups_list"), "backup"))}

    @tool("backup_get", "Read stored metadata for one existing backup schedule. Does not run it.")
    async def backup_get(schedule_id: Id) -> dict:
        return client.scrub(record(await client.call("backup_get", id=schedule_id), "backup"))

    @tool("repositories_list", "List repository metadata and stored health. Does not run Restic or return connection configuration.")
    async def repositories_list() -> dict:
        return {"items": client.scrub(records(await client.call("repositories_list"), "repository"))}

    @tool("repository_get", "Read stored repository metadata. Storage credentials and raw errors are omitted.")
    async def repository_get(repository_id: Id) -> dict:
        result = await client.call("repository_get", id=repository_id)
        if not isinstance(result, dict) or "repository" not in result:
            raise SafeError("Unexpected upstream response shape.")
        return client.scrub(record(result["repository"], "repository"))

    @tool("repository_stats", "Read stored repository statistics; may be stale or empty. Does not refresh statistics or prove recoverability.")
    async def repository_stats(repository_id: Id) -> dict:
        return client.scrub(pick_numbers(await client.call("repository_stats", id=repository_id), STATS))

    @tool("volumes_list", "List stored volume metadata without probing storage, browsing files, or exposing connection configuration.")
    async def volumes_list() -> dict:
        return {"items": client.scrub(records(await client.call("volumes_list"), "volume"))}

    @tool("tasks_list", "List active tasks and projected numeric progress. Task input, raw errors, and file paths are omitted.")
    async def tasks_list(kind: TaskKind | None = None) -> dict:
        q = {"kind": kind} if kind else {}
        return {"items": client.scrub(records(await client.call("tasks_list", query=q), "task"))}

    @tool("task_get", "Read current status and numeric progress for one task. Useful before retrying an uncertain write.")
    async def task_get(task_id: Id) -> dict:
        return client.scrub(record(await client.call("task_get", id=task_id), "task"))

    @tool("task_history", "Read one page of persisted task history. Raw log messages are omitted; no automatic pagination.")
    async def task_history(page: Annotated[int, Field(ge=1, le=10000, strict=True)] = 1,
                           kind: TaskKind | None = None, outcome: Outcome | None = None) -> dict:
        q = {"page": page}
        if kind: q["kind"] = kind
        if outcome: q["outcome"] = outcome
        result = await client.call("task_history", query=q)
        out = pick_numbers(result, ["page", "pageSize", "totalItems", "totalPages"])
        out["items"] = records(result.get("items"), "history")
        return client.scrub(out)

    @tool("backup_run", "Start an EXISTING backup schedule. May execute configured webhooks, retention/pruning and mirrors. Requires authorization for that schedule and its configured effects. Never automatically retry an uncertain result.")
    async def backup_run(schedule_id: Id) -> dict:
        return client.scrub(pick(await client.call("backup_run", id=schedule_id), ["taskId", "status"]))

    @tool("task_cancel", "Request cancellation of one running task. Can leave a backup/restore incomplete. Requires authorization for that task; no automatic retry.")
    async def task_cancel(task_id: Id) -> dict:
        return client.scrub(pick(await client.call("task_cancel", id=task_id), ["status"]))

    @tool("repository_stats_refresh", "Run repository statistics work and update stored stats. Potentially expensive storage I/O. Requires authorization; no automatic retry.")
    async def repository_stats_refresh(repository_id: Id) -> dict:
        return client.scrub(pick_numbers(await client.call("repository_stats_refresh", id=repository_id), STATS))

    @tool("repository_snapshots_refresh", "Clear and refresh the snapshot cache using Restic. Performs storage I/O; does not itself delete snapshots. Requires authorization; no automatic retry.")
    async def repository_snapshots_refresh(repository_id: Id) -> dict:
        return client.scrub(pick_numbers(await client.call("repository_snapshots_refresh", id=repository_id), ["count"]))

    return mcp


class BearerGate:
    """Protect the entire HTTP upstream. It does not distinguish RO/RW callers."""
    def __init__(self, app, token: str, on_shutdown=None):
        if len(token) < 32 or not token.isascii() or any(c.isspace() for c in token):
            raise SafeError("MCP bearer token must be at least 32 ASCII non-whitespace characters.")
        self.app, self.expected = app, ("Bearer " + token).encode("ascii")
        self.on_shutdown = on_shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            try:
                await self.app(scope, receive, send)
            finally:
                if self.on_shutdown:
                    await self.on_shutdown()
            return
        if scope["type"] == "http":
            headers = [v for k, v in scope.get("headers", []) if k.lower() == b"authorization"]
            if len(headers) != 1 or not hmac.compare_digest(headers[0], self.expected):
                await JSONResponse({"error": "Unauthorized"}, status_code=401)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def read_secret_file(name: str) -> str:
    path = os.environ.get(name)
    if not path:
        raise SafeError(f"{name} must point to an approved secret file.")
    try:
        data = Path(path).read_text().strip()
    except (OSError, UnicodeError):
        raise SafeError(f"Cannot read the secret file configured by {name}.") from None
    if not data:
        raise SafeError(f"The secret file configured by {name} is empty.")
    return data


def main():
    # Required settings, no default production destination and no import-time secret use.
    base_url = os.environ.get("ZEROBYTE_BASE_URL", "")
    client = ZerobyteClient(base_url, read_secret_file("ZEROBYTE_API_KEY_FILE"),
        allow_insecure_http=os.environ.get("ZEROBYTE_ALLOW_INSECURE_HTTP") == "true")
    token = read_secret_file("MCP_BEARER_TOKEN_FILE")
    hosts = [h.strip() for h in os.environ.get("MCP_ALLOWED_HOSTS", "localhost,localhost:*,127.0.0.1,127.0.0.1:*,zerobyte-mcp:8000").split(",") if h.strip()]
    app = BearerGate(create_server(client, allowed_hosts=hosts).streamable_http_app(), token, client.close)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    # Explicit uvicorn host binding, rather than relying on an ignored environment variable.
    uvicorn.run(app, host=os.environ.get("MCP_HOST", "127.0.0.1"),
                port=int(os.environ.get("MCP_PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
