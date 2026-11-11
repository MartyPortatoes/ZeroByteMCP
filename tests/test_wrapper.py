import json
import unittest

import httpx

from zerobyte_mcp.client import OPERATIONS, SafeError, ZerobyteClient, checked_base_url
from zerobyte_mcp.server import BearerGate, create_server

FAKE_KEY = "synthetic-upstream-key-never-valid-0123456789"
FAKE_TOKEN = "synthetic-mcp-token-never-valid-0123456789"
READ = {name for name, op in OPERATIONS.items() if op[2] == "read"}
WRITE = {name for name, op in OPERATIONS.items() if op[2] == "write"}


class WrapperTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.payload = {}
        self.status = 200
        self.content_type = "application/json"

        async def handle(request):
            self.requests.append(request)
            return httpx.Response(self.status, json=self.payload,
                headers={"content-type": self.content_type})

        self.client = ZerobyteClient("https://zerobyte.invalid", FAKE_KEY,
                                     transport=httpx.MockTransport(handle))
        self.mcp = create_server(self.client)

    async def asyncTearDown(self):
        await self.client.close()

    async def invoke(self, name, arguments=None):
        return await self.mcp.call_tool(name, arguments or {})

    async def test_catalog_is_one_combined_server(self):
        catalog = await self.mcp.list_tools()
        self.assertEqual({t.name for t in catalog}, READ | WRITE)
        self.assertEqual(len(READ), 9)
        self.assertEqual(len(WRITE), 4)
        for tool in catalog:
            self.assertEqual(tool.annotations.readOnlyHint, tool.name in READ)
            self.assertEqual(tool.annotations.destructiveHint, tool.name in WRITE)
            self.assertEqual(tool.annotations.openWorldHint, tool.name == "backup_run")
        self.assertFalse(self.requests)

    async def test_fixed_read_routes_and_queries(self):
        for name in READ:
            with self.subTest(name=name):
                if name in {"backups_list", "repositories_list", "volumes_list", "tasks_list"}:
                    self.payload = []
                elif name == "task_history":
                    self.payload = {"items": [], "page": 1, "totalItems": 0}
                elif name == "repository_get":
                    self.payload = {"repository": {"shortId": "repo_1", "status": "healthy"}}
                else:
                    self.payload = {"id": "synthetic_1"}
                arg = {"backup_get": "schedule_id", "repository_get": "repository_id",
                       "repository_stats": "repository_id", "task_get": "task_id"}.get(name)
                await self.invoke(name, {arg: "synthetic_1"} if arg else {})
                request = self.requests[-1]
                self.assertEqual(request.method, "GET")
                self.assertEqual(request.url.path, OPERATIONS[name][1].replace("{id}", "synthetic_1"))
                self.assertEqual(request.headers["x-api-key"], FAKE_KEY)
                self.assertEqual(request.url.host, "zerobyte.invalid")

    async def test_fixed_write_routes_no_payload_or_retry(self):
        for name in WRITE:
            with self.subTest(name=name):
                self.payload = {"taskId": "test_task", "status": "started", "count": 2}
                arg = "schedule_id" if name == "backup_run" else "task_id" if name == "task_cancel" else "repository_id"
                before = len(self.requests)
                await self.invoke(name, {arg: "synthetic_1"})
                request = self.requests[-1]
                self.assertEqual(request.method, "POST")
                self.assertEqual(request.url.path, OPERATIONS[name][1].replace("{id}", "synthetic_1"))
                self.assertEqual(request.content, b"")
                self.assertEqual(len(self.requests), before + 1)

    async def test_raw_configuration_and_errors_not_returned(self):
        self.payload = [{"id": 1, "shortId": "schedule_1", "name": "Synthetic",
            "backupWebhooks": {"pre": {"headers": ["Authorization: secret"]}},
            "customResticParams": ["--password-file=secret"],
            "lastBackupError": "secret credentials leaked upstream",
            "volume": {"name": "Synthetic", "config": {"password": "secret"}},
            "repository": {"name": "Synthetic", "config": {"secretAccessKey": "secret"}},
            "futureField": {"token": "secret"}}]
        output = str(await self.invoke("backups_list"))
        self.assertNotIn("secret", output)
        self.assertNotIn("backupWebhooks", output)
        self.assertNotIn("futureField", output)
        self.assertIn("hasError", output)

    async def test_credential_scrub_even_in_safe_field(self):
        self.payload = [{"name": FAKE_KEY, "shortId": "r"}]
        output = str(await self.invoke("repositories_list"))
        self.assertNotIn(FAKE_KEY, output)
        self.assertIn("REDACTED", output)

    async def test_task_outputs_exclude_input_logs_and_paths(self):
        self.payload = {"id": "task", "status": "running", "input": {"target": "/private"},
                        "error": "private", "result": {"output": "private"},
                        "progress": {"kind": "backup", "progress": {"percent_done": 0.5, "current_files": ["/private"]}}}
        output = str(await self.invoke("task_get", {"task_id": "task"}))
        self.assertNotIn("private", output)
        self.assertIn("percent_done", output)

    async def test_identifier_traversal_and_url_injection_rejected(self):
        for bad in ["../auth/api-keys", "x/y", "x%2Fy", "https://evil.invalid", "x?force=true", "x#y", "x\ny", "", "x" * 65]:
            with self.subTest(bad=bad), self.assertRaises(Exception):
                await self.invoke("backup_run", {"schedule_id": bad})
        self.assertFalse(self.requests)

    async def test_unknown_tools_and_operations_denied(self):
        for name in ["deleteSnapshot", "runForget", "restoreSnapshot", "http_request", "createApiKey", "future_new_tool"]:
            with self.subTest(name=name), self.assertRaises(Exception):
                await self.invoke(name)
        with self.assertRaises(SafeError):
            await self.client.call("http_request")
        self.assertFalse(self.requests)

    async def test_query_allowlist_and_validation(self):
        for kwargs in [{"query": {"path": "/etc/passwd"}}, {"query": {"page": -1}},
                       {"query": {"page": True}}, {"query": {"kind": "evil"}}]:
            with self.assertRaises(SafeError):
                await self.client.call("task_history", **kwargs)
        self.assertFalse(self.requests)
        self.payload = {"items": [], "page": 2}
        await self.invoke("task_history", {"page": 2, "kind": "backup", "outcome": "error"})
        self.assertEqual(dict(self.requests[-1].url.params), {"page": "2", "kind": "backup", "outcome": "error"})

    async def test_http_error_sanitized_and_never_retried(self):
        self.status, self.payload = 500, {"error": FAKE_KEY, "url": "https://secret.invalid"}
        with self.assertRaises(Exception) as ctx:
            await self.invoke("backup_run", {"schedule_id": "s"})
        self.assertNotIn(FAKE_KEY, str(ctx.exception))
        self.assertNotIn("secret.invalid", str(ctx.exception))
        self.assertEqual(len(self.requests), 1)

    async def test_redirect_not_followed(self):
        self.status = 302
        with self.assertRaises(Exception):
            await self.invoke("backup_get", {"schedule_id": "s"})
        self.assertEqual(len(self.requests), 1)

    async def test_non_json_refused(self):
        self.content_type = "text/html"
        with self.assertRaises(Exception):
            await self.invoke("backup_get", {"schedule_id": "s"})

    async def test_response_size_limit(self):
        self.payload = {"content": "x" * 1_100_000}
        with self.assertRaises(Exception):
            await self.invoke("backup_get", {"schedule_id": "s"})

    async def test_timeout_uncertainty_and_no_retry(self):
        count = 0
        async def handle(request):
            nonlocal count
            count += 1
            raise httpx.ReadTimeout(FAKE_KEY, request=request)
        client = ZerobyteClient("https://zerobyte.invalid", FAKE_KEY, transport=httpx.MockTransport(handle))
        try:
            with self.assertRaises(SafeError) as ctx:
                await client.call("backup_run", id="s")
            self.assertIn("may have executed", str(ctx.exception))
            self.assertNotIn(FAKE_KEY, str(ctx.exception))
            self.assertEqual(count, 1)
        finally:
            await client.close()

    async def test_base_url_safety(self):
        for url in ["http://zerobyte.invalid", "https://u:p@zerobyte.invalid", "file:///etc/passwd",
                    "https://zerobyte.invalid/api", "https://zerobyte.invalid?q=1", ""]:
            with self.subTest(url=url), self.assertRaises(SafeError):
                checked_base_url(url)
        self.assertEqual(checked_base_url("http://zerobyte.invalid", True), "http://zerobyte.invalid")

    async def test_streamable_http_auth_and_catalog(self):
        app = self.mcp.streamable_http_app()
        gate = BearerGate(app, FAKE_TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://localhost") as http:
                req = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                response = await http.post("/mcp", json=req)
                self.assertEqual(response.status_code, 401)
                headers = {"Authorization": "Bearer " + FAKE_TOKEN,
                           "Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-03-26"}
                response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "initialize", "params": {
                    "protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "synthetic", "version": "1"}}}, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                response = await http.post("/mcp", json=req, headers=headers)
                self.assertEqual(response.status_code, 200, response.text)
                names = {t["name"] for t in response.json()["result"]["tools"]}
                self.assertEqual(names, set(OPERATIONS))
                # Direct named tool call: unknown name denied without upstream traffic.
                response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "deleteSnapshot", "arguments": {}}}, headers=headers)
                self.assertTrue(response.json()["result"]["isError"])
                self.assertFalse(self.requests)
                self.payload = []
                response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "backups_list", "arguments": {}}}, headers=headers)
                self.assertFalse(response.json()["result"].get("isError", False), response.text)
                self.assertEqual(len(self.requests), 1)
                # A combined upstream intentionally permits known writes. Namespace split is external.
                self.payload = {"taskId": "synthetic_1", "status": "started"}
                response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "backup_run", "arguments": {"schedule_id": "synthetic_1"}}}, headers=headers)
                self.assertFalse(response.json()["result"].get("isError", False), response.text)
                self.assertEqual(self.requests[-1].method, "POST")

    async def test_http_host_rebinding_refused(self):
        app = self.mcp.streamable_http_app()
        gate = BearerGate(app, FAKE_TOKEN)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=gate), base_url="http://attacker.invalid") as http:
                response = await http.post("/mcp", json={}, headers={"Authorization": "Bearer " + FAKE_TOKEN})
                self.assertEqual(response.status_code, 421)
                self.assertFalse(self.requests)

    async def test_manifest_exactly_covers_tools(self):
        from pathlib import Path
        manifest = json.loads((Path(__file__).parents[1] / "deployment/namespace-manifest.json").read_text())
        self.assertEqual(set(manifest["namespaces"]["zerobyte-read"]), READ)
        self.assertEqual(set(manifest["namespaces"]["zerobyte-write"]), WRITE)
        self.assertFalse(READ & WRITE)


if __name__ == "__main__":
    unittest.main()
