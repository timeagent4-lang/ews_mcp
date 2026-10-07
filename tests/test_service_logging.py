import io
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

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
    async def test_unknown_tool_name_does_not_leak_password_in_sdk_log(self):
        output = io.StringIO()
        handler = logging.StreamHandler(output)
        logging.getLogger().addHandler(handler)
        self.addCleanup(logging.getLogger().removeHandler, handler)
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("private-unknown-password", {"mailbox": "u@example.com", "password": "private-unknown-password"}, raise_on_error=False)
        self.assertTrue(result.is_error)
        self.assertNotIn("private-unknown-password", output.getvalue())
