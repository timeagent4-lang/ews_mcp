import io
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from exchangelib.util import post_ratelimited
from fastmcp import Client
import mcp_server


class ServiceLoggingTests(unittest.TestCase):
    def test_sdk_exception_does_not_log_auth_body_or_raw_error(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        protocol = SimpleNamespace(TIMEOUT=1, retry_policy=SimpleNamespace(back_off_until=None), retire_session=lambda s: None)
        session = SimpleNamespace(session_id="fixture", auth="private-login", get_adapter=lambda u: "fixture",
            post=Mock(side_effect=RuntimeError("private-password")))
        with self.assertRaises(RuntimeError):
            post_ratelimited(protocol, session, "https://exchange.example.com/EWS/Exchange.asmx", {}, b"<Body>private-body</Body>")
        for private in ("private-password", "private-login", "private-body", "Request XML"):
            self.assertNotIn(private, output.getvalue())


class SDKLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_protocol_requests_do_not_log_arguments_and_session_stays_usable(self):
        output = io.StringIO()
        root = logging.getLogger()
        handler = logging.StreamHandler(output)
        root.addHandler(handler)
        self.addCleanup(root.removeHandler, handler)
        previous_level = root.level
        root.setLevel(logging.DEBUG)
        self.addCleanup(root.setLevel, previous_level)
        app = mcp_server.mcp.http_app(path="/mcp", transport="streamable-http", json_response=True)
        headers = {"Accept": "application/json, text/event-stream"}
        secret, body = "pw!", "body!"
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}, clear=True):
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers) as client:
                    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                        "protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "offline-test", "version": "1"}}})
                    self.assertEqual(response.status_code, 200)
                    client.headers.update({"Mcp-Session-Id": response.headers["Mcp-Session-Id"], "Mcp-Protocol-Version": "2025-11-25"})
                    await client.post("/mcp", json={"jsonrpc": "2.0", "method": "notifications/initialized"})
                    for request_id, name in enumerate((None, 42, []), start=2):
                        with self.subTest(name=name):
                            response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
                                "name": name, "arguments": {"password": secret, "body": body}}})
                            self.assertEqual(response.status_code, 200)
                            self.assertEqual(response.json()["error"]["code"], -32602)
                            self.assertNotIn(secret, response.text)
                    response = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
                    self.assertEqual(len(response.json()["result"]["tools"]), 29)
        logs = output.getvalue()
        self.assertNotIn(secret, logs)
        self.assertNotIn(body, logs)
        self.assertIn("mcp_session_diagnostic", logs)
        root.warning("application diagnostic remains visible")
        self.assertIn("application diagnostic remains visible", output.getvalue())

    async def test_unknown_tool_name_does_not_leak_password_in_sdk_log(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("private-unknown-password", {"mailbox": "u@example.com", "password": "private-unknown-password"}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertNotIn("private-unknown-password", output.getvalue())
