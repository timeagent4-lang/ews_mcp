import unittest
from unittest.mock import Mock, patch

import httpx
from starlette.middleware import Middleware
from starlette.responses import JSONResponse

import mcp_server


class MCPHTTPAuthTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.reached = []

        async def downstream(scope, receive, send):
            self.reached.append(scope["path"])
            await JSONResponse({"reached": scope["path"]})(scope, receive, send)

        self.app = mcp_server.MCPKeyAuthMiddleware(downstream, "测试-key")

    async def request(self, path):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://test",
        ) as client:
            return await client.post(path, json={"method": "tools/list"})

    async def test_missing_wrong_duplicate_and_blank_keys_never_reach_mcp(self):
        for path in ("/mcp", "/mcp/", "/mcp?key=", "/mcp?key=wrong",
                     "/mcp?key=wrong&key=wrong", "/mcp?key=测试-key&key=wrong"):
            with self.subTest(path=path):
                response = await self.request(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.json(), {"error": "Invalid or missing API key"})
        self.assertEqual(self.reached, [])

    async def test_exact_unicode_key_allows_mcp_and_trailing_slash(self):
        for path in ("/mcp?key=测试-key", "/mcp/?key=测试-key"):
            self.assertEqual((await self.request(path)).status_code, 200)
        self.assertEqual(self.reached, ["/mcp", "/mcp/"])

    async def test_health_and_signed_download_routes_use_their_own_controls(self):
        for path in ("/healthz", "/downloads/identifier?expires=1&token=x"):
            self.assertEqual((await self.request(path)).status_code, 200)
        self.assertEqual(self.reached, ["/healthz", "/downloads/identifier"])

    async def test_health_payload_contains_no_mailbox_or_credentials(self):
        with patch.object(mcp_server, "resolve_identity_by_lanid") as oa, \
                patch.object(mcp_server, "OutlookClient") as outlook:
            response = await mcp_server.healthz(None)
        self.assertEqual(response.body, b'{"status":"ok","service":"ews_mcp","tool_count":28}')
        oa.assert_not_called()
        outlook.assert_not_called()


class MCPStartupTests(unittest.TestCase):
    def test_missing_key_refuses_startup(self):
        with patch.dict(mcp_server.os.environ, {"EWS_MCP_API_KEY": " "}), \
                patch.object(mcp_server, "configure_store"), \
                patch.object(mcp_server, "data_dir", return_value="fixture"), \
                patch.object(mcp_server.atexit, "register"), \
                patch.object(mcp_server.mcp, "run") as run:
            with self.assertRaisesRegex(ValueError, "EWS_MCP_API_KEY"):
                mcp_server.main()
        run.assert_not_called()

    def test_startup_installs_auth_and_disables_access_log(self):
        with patch.dict(mcp_server.os.environ, {"EWS_MCP_API_KEY": "fixture-key",
                         "EWS_MCP_HOST": "127.0.0.1", "EWS_MCP_PORT": "7805"}), \
                patch.object(mcp_server, "configure_store", return_value=Mock()), \
                patch.object(mcp_server, "data_dir", return_value="fixture"), \
                patch.object(mcp_server.atexit, "register"), \
                patch.object(mcp_server.mcp, "run") as run:
            mcp_server.main()
        kwargs = run.call_args.kwargs
        self.assertEqual(kwargs["transport"], "streamable-http")
        self.assertEqual(kwargs["path"], "/mcp")
        self.assertEqual(kwargs["uvicorn_config"], {"access_log": False})
        auth = kwargs["middleware"][0]
        self.assertIsInstance(auth, Middleware)
        self.assertIs(auth.cls, mcp_server.MCPKeyAuthMiddleware)
        self.assertEqual(auth.kwargs, {"api_key": "fixture-key"})


if __name__ == "__main__":
    unittest.main()
