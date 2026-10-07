"""Verify blocking EWS boundaries do not stall MCP HTTP or mix request contexts."""
import asyncio
import inspect
import json
import os
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import anyio
import httpx
from exchangelib.errors import ErrorAccessDenied
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.middleware import Middleware

import mcp_server as server
from utils.audit import current_request_id


ENV = {"OUTLOOK_SERVER": "offline.example.com", "EWS_MCP_DOWNLOAD_ENABLED": "false"}


class MCPConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)

    async def test_health_responds_while_exchange_call_is_blocked(self):
        started, release = threading.Event(), threading.Event()
        timer = threading.Timer(2, release.set)

        def status():
            started.set()
            release.wait(3)
            return {"connected": True}

        app = server.mcp.http_app(path="/mcp", transport="streamable-http", json_response=True,
            middleware=[Middleware(server.MCPKeyAuthMiddleware, api_key="fixture-key")])

        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), **kwargs)

        transport = StreamableHttpTransport("http://test/mcp?key=fixture-key", httpx_client_factory=factory)
        with patch.object(server, "OutlookClient", return_value=SimpleNamespace(get_server_status=status)):
            async with app.router.lifespan_context(app):
                async with Client(transport) as client:
                    timer.start()
                    task = asyncio.create_task(client.call_tool("get_server_status",
                        {"mailbox": "user@example.com", "password": "personal-secret"}))
                    try:
                        did_start = await asyncio.to_thread(started.wait, 3)
                        async with factory(base_url="http://test") as http:
                            response = await http.get("/healthz")
                        stalled = release.is_set()
                    finally:
                        release.set()
                        timer.cancel()
                        result = await task
        timer.join()
        self.assertTrue(did_start)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(stalled, "health only responded after the EWS call finished")
        self.assertEqual(result.structured_content["status"], "success")

    async def test_overlapping_calls_keep_credentials_errors_and_audit_context_isolated(self):
        barrier = threading.Barrier(2, timeout=2)
        connections = []

        def connect(config):
            connections.append((config.email, config.lanid, config.password))

            def status():
                barrier.wait()
                if config.email == "a@example.com":
                    raise ErrorAccessDenied("rejected " + config.password)
                return {"mailbox": config.email, "seen_request_id": current_request_id()}

            return SimpleNamespace(get_server_status=status)

        with patch.object(server, "OutlookClient", side_effect=connect), self.assertLogs(server.tool_audit.logger, level="INFO") as logs:
            async with Client(server.mcp) as client:
                results = await asyncio.gather(*[
                    client.call_tool("get_server_status", {"mailbox": mailbox, "password": password}, raise_on_error=False)
                    for mailbox, password in (("a@example.com", "secret-A"), ("b@example.com", "secret-B"))
                ])
        payloads = [result.structured_content for result in results]
        self.assertEqual(payloads[0]["status"], "failed")
        self.assertEqual(payloads[0].get("exchange_code"), "ErrorAccessDenied")
        self.assertEqual(payloads[1]["status"], "success")
        self.assertEqual(payloads[1]["results"]["mailbox"], "b@example.com")
        self.assertEqual(payloads[1]["results"]["seen_request_id"], payloads[1]["request_id"])
        self.assertNotEqual(payloads[0]["request_id"], payloads[1]["request_id"])
        self.assertEqual(set(connections), {("a@example.com", "a@example.com", "secret-A"), ("b@example.com", "b@example.com", "secret-B")})
        records = [json.loads(line.split(":", 2)[2]) for line in logs.output]
        self.assertEqual(len(records), 4)
        for mailbox, payload in zip(("a@example.com", "b@example.com"), payloads):
            own = [record for record in records if record["mailbox"] == mailbox]
            self.assertEqual({record["phase"] for record in own}, {"start", "finish"})
            self.assertEqual({record["request_id"] for record in own}, {payload["request_id"]})
        for secret in ("secret-A", "secret-B"):
            self.assertNotIn(secret, json.dumps(payloads) + "".join(logs.output))

    async def test_request_scope_cancellation_waits_for_started_worker_outcome(self):
        started, release = threading.Event(), threading.Event()
        timer = threading.Timer(2, release.set)
        holder, outcomes, calls = {}, [], []

        def status():
            calls.append("started")
            started.set()
            release.wait(3)
            calls.append("completed")
            return {"connected": True}

        async def invoke():
            with anyio.CancelScope() as scope:
                holder["scope"] = scope
                result = server._make_handler("get_server_status")(
                    mailbox="user@example.com", password="personal-secret")
                if inspect.isawaitable(result):
                    result = await result
                outcomes.append(result.structured_content)

        with patch.object(server, "OutlookClient", return_value=SimpleNamespace(get_server_status=status)):
            timer.start()
            task = asyncio.create_task(invoke())
            try:
                self.assertTrue(await asyncio.to_thread(started.wait, 3))
                holder["scope"].cancel()
                await asyncio.sleep(0)
                self.assertFalse(task.done(), "cancellation discarded or finished the still-running EWS call")
                self.assertEqual(calls, ["started"])
            finally:
                release.set()
                timer.cancel()
                await task
        timer.join()
        self.assertEqual(calls, ["started", "completed"])
        self.assertEqual(len(outcomes), 1)
        self.assertEqual(outcomes[0]["status"], "success")
