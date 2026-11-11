"""Credential-free transport, lifecycle, and projection regression tests."""
import asyncio
from contextlib import asynccontextmanager
import unittest

import httpx

from zerobyte_mcp.client import SafeError, ZerobyteClient
from zerobyte_mcp.projections import pick_numbers, record, records
from zerobyte_mcp.server import BearerGate, create_server

FAKE_KEY = "synthetic-upstream-key-never-valid-0123456789"
FAKE_TOKEN = "synthetic-mcp-token-never-valid-0123456789"
HEADERS = {
    "Authorization": "Bearer " + FAKE_TOKEN,
    "Accept": "application/json, text/event-stream",
    "MCP-Protocol-Version": "2025-03-26",
}


@asynccontextmanager
async def lifespan(app):
    """Drive the outer ASGI app, including its shutdown callback."""
    incoming, outgoing = asyncio.Queue(), asyncio.Queue()
    task = asyncio.create_task(app(
        {"type": "lifespan", "asgi": {"version": "3.0", "spec_version": "2.0"}},
        incoming.get, outgoing.put,
    ))
    await incoming.put({"type": "lifespan.startup"})
    startup = await asyncio.wait_for(outgoing.get(), timeout=5)
    if startup["type"] != "lifespan.startup.complete":
        await task
        raise AssertionError(startup)
    try:
        yield
    finally:
        await incoming.put({"type": "lifespan.shutdown"})
        shutdown = await asyncio.wait_for(outgoing.get(), timeout=5)
        await asyncio.wait_for(task, timeout=5)
        if shutdown["type"] != "lifespan.shutdown.complete":
            raise AssertionError(shutdown)


class HardeningTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.response = httpx.Response(200, json=[])

        async def handle(request):
            self.requests.append(request)
            return self.response

        self.client = ZerobyteClient("https://zerobyte.invalid", FAKE_KEY,
                                     transport=httpx.MockTransport(handle))
        self.mcp = create_server(self.client)
        self.app = BearerGate(self.mcp.streamable_http_app(), FAKE_TOKEN, self.client.close)

    async def asyncTearDown(self):
        await self.client.close()

    async def test_auth_is_required_for_every_http_path_and_method(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                    base_url="http://localhost") as http:
            for method in ["GET", "POST", "DELETE", "OPTIONS"]:
                for path in ["/mcp", "/mcp/", "/sse", "/api/v1/backups", "/"]:
                    with self.subTest(method=method, path=path):
                        response = await http.request(method, path)
                        self.assertEqual(response.status_code, 401)
            invalid = [
                [("Authorization", "Bearer " + "x" * len(FAKE_TOKEN))],
                [("Authorization", "Bearer " + FAKE_TOKEN), ("Authorization", "Bearer " + FAKE_TOKEN)],
                [("Authorization", "Bearer " + FAKE_TOKEN + " ")],
                [("Authorization", FAKE_TOKEN)],
            ]
            for headers in invalid:
                response = await http.post("/mcp", headers=headers, json={})
                self.assertEqual(response.status_code, 401)
        self.assertFalse(self.requests)

    async def test_arbitrary_browser_origin_is_refused(self):
        async with lifespan(self.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://localhost") as http:
                response = await http.post("/mcp", json={}, headers={**HEADERS, "Origin": "https://attacker.invalid"})
                self.assertEqual(response.status_code, 403)
                self.assertFalse(self.requests)

    async def test_stateless_requests_reuse_client_until_full_app_shutdown(self):
        req = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": "backups_list", "arguments": {}}}
        async with lifespan(self.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://localhost") as http:
                for _ in range(3):
                    response = await http.post("/mcp", json=req, headers=HEADERS)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["result"].get("isError", False))
                    self.assertFalse(self.client.http.is_closed)
                    self.assertNotIn("mcp-session-id", response.headers)
        self.assertTrue(self.client.http.is_closed)
        self.assertEqual(len(self.requests), 3)

    async def test_http_request_body_limit(self):
        async with lifespan(self.app):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),
                                        base_url="http://localhost") as http:
                response = await http.post("/mcp", json={"padding": "x" * 65536}, headers=HEADERS)
                self.assertEqual(response.status_code, 413)
                self.assertFalse(self.requests)

    async def test_malformed_json_errors_do_not_reflect_upstream_body(self):
        for content in [FAKE_KEY.encode(), b"\xff\xfe\xff", b'{"broken":']:
            self.response = httpx.Response(200, content=content,
                                           headers={"content-type": "application/json"})
            with self.assertRaises(SafeError) as ctx:
                await self.client.call("backups_list")
            self.assertEqual(str(ctx.exception), "Zerobyte returned malformed JSON.")
            self.assertNotIn(FAKE_KEY, str(ctx.exception))

    async def test_compressed_responses_are_rejected_before_consumption(self):
        class UnreadBody(httpx.AsyncByteStream):
            async def __aiter__(self):
                raise AssertionError("Compressed body must not be consumed")
                yield b""

        self.response = httpx.Response(200, stream=UnreadBody(), headers={
            "content-type": "application/json", "content-encoding": "gzip",
        })
        with self.assertRaisesRegex(SafeError, "unsupported compressed response"):
            await self.client.call("backups_list")
        self.assertEqual(self.requests[0].headers["accept-encoding"], "identity")

    async def test_drifted_numeric_fields_do_not_expose_text(self):
        source = {"total_size": "do not expose an upstream error or credential", "total_files": True,
                  "files_done": 12, "percent_done": 0.5, "seconds_remaining": None,
                  "compression_ratio": float("inf"), "total_bytes": float("nan")}
        self.assertEqual(pick_numbers(source, source), {
            "files_done": 12, "percent_done": 0.5, "seconds_remaining": None,
        })
        task = record({"progress": {"progress": source}}, "task")
        self.assertEqual(task["progress"], {"files_done": 12, "percent_done": 0.5, "seconds_remaining": None})

    async def test_nested_metadata_values_are_not_exposed(self):
        value = record({"name": {"password": "hidden"}, "status": ["hidden"],
                        "shortId": "safe", "config": {"password": "hidden"}}, "repository")
        self.assertEqual(value, {"shortId": "safe", "hasError": False})
        for bad in [None, "hidden", ["hidden"]]:
            with self.assertRaises(SafeError):
                record(bad, "repository")
        with self.assertRaises(SafeError):
            records([{}] * 1001, "repository")

    async def test_operation_key_is_not_exposed(self):
        value = record({"id": "task_1", "operationKey": "unreviewed arbitrary upstream string"}, "task")
        self.assertEqual(value, {"id": "task_1", "hasError": False})


if __name__ == "__main__":
    unittest.main()
