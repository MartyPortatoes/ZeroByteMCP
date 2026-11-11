"""Fixed-operation HTTP client. Never a generic URL/method/command executor."""
from __future__ import annotations

import json
import re
from types import MappingProxyType
from urllib.parse import urlsplit

import httpx


class SafeError(RuntimeError):
    """An error safe to return to MCP clients without upstream payloads."""


OPERATIONS = MappingProxyType({
    "backups_list": ("GET", "/api/v1/backups", "read"),
    "backup_get": ("GET", "/api/v1/backups/{id}", "read"),
    "repositories_list": ("GET", "/api/v1/repositories", "read"),
    "repository_get": ("GET", "/api/v1/repositories/{id}", "read"),
    "repository_stats": ("GET", "/api/v1/repositories/{id}/stats", "read"),
    "volumes_list": ("GET", "/api/v1/volumes", "read"),
    "tasks_list": ("GET", "/api/v1/tasks", "read"),
    "task_get": ("GET", "/api/v1/tasks/{id}", "read"),
    "task_history": ("GET", "/api/v1/tasks/history", "read"),
    "backup_run": ("POST", "/api/v1/backups/{id}/run", "write"),
    "task_cancel": ("POST", "/api/v1/tasks/{id}/cancel", "write"),
    "repository_stats_refresh": ("POST", "/api/v1/repositories/{id}/stats/refresh", "write"),
    "repository_snapshots_refresh": ("POST", "/api/v1/repositories/{id}/snapshots/refresh", "write"),
})
KINDS = frozenset({"backup", "restore", "deleteSnapshots", "tagSnapshots", "doctor", "mirrorSync", "forget"})
OUTCOMES = frozenset({"running", "success", "warning", "error", "cancelled", "stale"})
MAX_RESPONSE_BYTES = 1_048_576


def checked_id(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value, flags=re.ASCII):
        raise SafeError("ID must contain 1–64 ASCII letters, digits, underscores, or hyphens.")
    return value


def checked_base_url(value: str, allow_insecure_http: bool = False) -> str:
    parts = urlsplit(value)
    if parts.scheme not in {"https", "http"} or not parts.hostname or parts.username or parts.password:
        raise SafeError("Configure a fixed HTTP(S) origin without embedded credentials.")
    if parts.path not in {"", "/"} or parts.query or parts.fragment:
        raise SafeError("API base URL must be an origin only, without path, query, or fragment.")
    if parts.scheme == "http" and not allow_insecure_http:
        raise SafeError("Plain HTTP is disabled. Explicit deployment approval is required to enable it.")
    return value.rstrip("/")


class ZerobyteClient:
    def __init__(self, base_url: str, api_key: str, *, transport=None, allow_insecure_http=False):
        self.base_url = checked_base_url(base_url, allow_insecure_http)
        if not api_key or "\n" in api_key or "\r" in api_key:
            raise SafeError("A valid API key is required.")
        self._api_key = api_key
        self.http = httpx.AsyncClient(
            base_url=self.base_url, headers={"X-API-Key": api_key, "Accept": "application/json", "Accept-Encoding": "identity"},
            follow_redirects=False, trust_env=False,
            timeout=httpx.Timeout(30, connect=5), transport=transport,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )

    async def close(self):
        await self.http.aclose()

    async def call(self, operation: str, *, id: str | None = None, query: dict | None = None):
        if operation not in OPERATIONS:
            raise SafeError("Operation is not in the reviewed allowlist.")
        method, path, _ = OPERATIONS[operation]
        if "{id}" in path:
            path = path.replace("{id}", checked_id(id))
        elif id is not None:
            raise SafeError("This operation does not accept an ID.")
        query = dict(query or {})
        allowed_query = {"kind", "outcome", "page"} if operation == "task_history" else {"kind"} if operation == "tasks_list" else set()
        if query.keys() - allowed_query:
            raise SafeError("Unexpected query parameter.")
        if "kind" in query and query["kind"] not in KINDS:
            raise SafeError("Unsupported task kind.")
        if "outcome" in query and query["outcome"] not in OUTCOMES:
            raise SafeError("Unsupported task outcome.")
        if "page" in query and (type(query["page"]) is not int or not 1 <= query["page"] <= 10000):
            raise SafeError("Page must be an integer between 1 and 10000.")
        try:
            async with self.http.stream(method, path, params=query) as response:
                if not 200 <= response.status_code < 300:
                    # Never reflect upstream headers, URL, body, or exception strings.
                    raise SafeError(f"Zerobyte returned HTTP {response.status_code} for {operation}. No automatic retry was made.")
                if response.headers.get("content-type", "").split(";", 1)[0].lower() != "application/json":
                    raise SafeError("Zerobyte returned a non-JSON response.")
                # Bound bytes before any decompression can allocate a larger payload.
                if response.headers.get("content-encoding", "identity").strip().lower() != "identity":
                    raise SafeError("Zerobyte returned an unsupported compressed response.")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > MAX_RESPONSE_BYTES:
                        raise SafeError("Zerobyte response exceeded the 1 MiB safety limit.")
                return json.loads(data)
        except httpx.TimeoutException:
            raise SafeError("Zerobyte request timed out. A write may have executed; check current state before retrying.") from None
        except httpx.RequestError:
            raise SafeError("Zerobyte request failed. A write may have executed; check current state before retrying.") from None
        except (ValueError, UnicodeDecodeError):
            raise SafeError("Zerobyte returned malformed JSON.") from None

    def scrub(self, value):
        """Last-resort removal of the configured credential even in a scalar field."""
        if isinstance(value, str):
            return value.replace(self._api_key, "[REDACTED]")[:512]
        if isinstance(value, list):
            return [self.scrub(v) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub(v) for k, v in value.items()}
        return value
