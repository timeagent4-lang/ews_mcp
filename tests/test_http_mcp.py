"""实际 Streamable HTTP 入口和 query key，使用 ASGI 传输。"""
import json
import os
import unittest
from unittest.mock import patch

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from starlette.middleware import Middleware

import mcp_server as server


class HTTPMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.app = server.mcp.http_app(path="/mcp", transport="streamable-http", json_response=True,
            middleware=[Middleware(server.MCPKeyAuthMiddleware, api_key="fixture-key")])

    async def test_missing_bad_and_duplicate_keys_rejected_health_allowed(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test") as client:
            for path in ("/mcp", "/mcp?key=wrong", "/mcp?key=fixture-key&key=fixture-key", "/mcp/?key=wrong"):
                self.assertEqual((await client.post(path)).status_code, 401)
            result = await client.get("/healthz")
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()["tool_count"], 29)

    async def test_tools_and_failure_round_trip_through_streamable_http(self):
        def factory(**kwargs):
            return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), **kwargs)
        transport = StreamableHttpTransport("http://test/mcp?key=fixture-key", httpx_client_factory=factory)
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}, clear=True):
            async with self.app.router.lifespan_context(self.app):
                async with Client(transport) as client:
                    self.assertEqual(len(await client.list_tools()), 29)
                    result = await client.call_tool("get_server_status", {"mailbox": "u@example.com", "confirm": True}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["status"], "failed")
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
