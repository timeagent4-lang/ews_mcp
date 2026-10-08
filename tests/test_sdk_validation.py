"""SDK 全局严格校验开关不能绕过本服务的安全参数错误响应。"""
import json
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import unittest


class SDKValidationTests(unittest.TestCase):
    def test_strict_sdk_setting_preserves_safe_errors_and_valid_calls(self):
        # 新进程让 SDK 在首次导入时真实读取环境开关，避免污染其他用例。
        script = textwrap.dedent("""
            import asyncio, json, logging
            from types import SimpleNamespace
            from unittest.mock import patch
            from fastmcp import Client
            import mcp_server as server
            logging.getLogger().setLevel(logging.CRITICAL)
            server.tool_audit.logger.disabled = True
            async def check():
                with patch.object(server, 'initialize_outlook_client', return_value=SimpleNamespace(
                    get_server_status=lambda: {'connected': True})) as connect:
                    async with Client(server.mcp) as client:
                        bad = await client.call_tool('get_server_status', {
                            'mailbox': 'u@example.com', 'password': 'pw!' * 1366}, raise_on_error=False)
                        good = await client.call_tool('get_server_status', {
                            'mailbox': 'u@example.com', 'password': 'valid-test-password'})
                    print(json.dumps({'error': bad.structured_content,
                        'text': bad.content[0].text, 'is_error': bad.is_error,
                        'success': good.structured_content, 'connections': connect.call_count}))
            asyncio.run(check())
        """)
        result = subprocess.run([sys.executable, "-c", script],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "FASTMCP_STRICT_INPUT_VALIDATION": "true", "EWS_MCP_DOWNLOAD_ENABLED": "false"},
            capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        value = json.loads(result.stdout)
        self.assertNotIn("pw!", value["text"])
        self.assertTrue(value["is_error"])
        self.assertEqual(value["error"]["error_code"], "INVALID_PARAMS")
        self.assertEqual(value["error"]["status"], "failed")
        self.assertTrue(value["error"]["request_id"])
        self.assertEqual(json.loads(value["text"]), value["error"])
        self.assertEqual(value["success"]["status"], "success")
        self.assertEqual(value["connections"], 1)
