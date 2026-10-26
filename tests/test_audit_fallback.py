"""Offline contracts for calls terminated before the audit middleware runs."""
import json
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from fastmcp import FastMCP
from mcp import types as mcp_types

from utils.audit import ToolAuditMiddleware, install_tool_audit_fallback


class AuditFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.logger = Mock()
        self.middleware = ToolAuditMiddleware(logger=self.logger)
        self.server = FastMCP("audit-fallback-contracts")
        self.request = mcp_types.CallToolRequest(
            method="tools/call",
            params=mcp_types.CallToolRequestParams(
                name="check_availability",
                arguments={
                    "lanid": "employee", "name": "员工", "interval_minutes": "30",
                    "params": {
                        "duration": 30, "attendees": ["person@example.com"],
                        "unknown": None, "confirm_token": "FALLBACK_TOKEN",
                    },
                },
            ),
        )
        self.expected_arguments = {
            "lanid": "employee", "name": "员工", "interval_minutes": "30",
            "params": {
                "duration": 30, "attendees": ["person@example.com"],
                "unknown": None,
                "confirm_token": {"redacted": True, "type": "string", "length": 14},
            },
        }
        self.error_result = mcp_types.ServerResult(mcp_types.CallToolResult(
            isError=True,
            content=[mcp_types.TextContent(type="text", text="rejected before dispatch")],
        ))

    def install(self, original_handler):
        self.server._mcp_server.request_handlers[mcp_types.CallToolRequest] = original_handler
        install_tool_audit_fallback(self.server, self.middleware)
        return self.server._mcp_server.request_handlers[mcp_types.CallToolRequest]

    def assert_snapshot(self, *, status, error_code):
        self.logger.info.assert_called_once()
        serialized = self.logger.info.call_args.args[0]
        event = json.loads(serialized)
        self.assertEqual(event["tool"], "check_availability")
        self.assertEqual(event["status"], status)
        self.assertEqual(event["error_code"], error_code)
        self.assertEqual(event["identity_assurance"], "not_verified")
        self.assertEqual(event["lanid"], "employee")
        self.assertEqual(event["name"], "员工")
        self.assertEqual(event["input_arguments"], self.expected_arguments)
        self.assertIn("input_summary", event)
        self.assertNotIn("FALLBACK_TOKEN", serialized)
        self.assertNotIn("MUTATED_TOKEN", serialized)

    async def test_error_result_keeps_snapshot_before_nested_handler_mutation(self):
        async def original_handler(request):
            request.params.arguments["lanid"] = "mutated-employee"
            nested = request.params.arguments["params"]
            nested["duration"] = 60
            nested["attendees"].append("added@example.com")
            nested["unknown"] = False
            nested["confirm_token"] = "MUTATED_TOKEN"
            request.params.arguments["interval_minutes"] = 30
            return self.error_result

        handler = self.install(original_handler)
        result = await handler(self.request)

        self.assertIs(result, self.error_result)
        self.assert_snapshot(status="failure", error_code="MCP_TOOL_ERROR")

    async def test_handler_exception_keeps_snapshot_and_propagates_original_exception(self):
        failure = ValueError("pre-dispatch failure")

        async def original_handler(request):
            request.params.arguments["params"]["attendees"].clear()
            request.params.arguments.clear()
            raise failure

        handler = self.install(original_handler)
        with self.assertRaises(ValueError) as raised:
            await handler(self.request)

        self.assertIs(raised.exception, failure)
        self.assert_snapshot(status="error", error_code="TOOL_CALL_ERROR")

    async def test_repeated_install_keeps_one_wrapper_and_one_record_per_call(self):
        received_requests = []

        async def original_handler(request):
            received_requests.append(request)
            return self.error_result

        handler = self.install(original_handler)
        install_tool_audit_fallback(self.server, self.middleware)
        self.assertIs(self.server._mcp_server.request_handlers[mcp_types.CallToolRequest], handler)

        for _ in range(2):
            self.logger.reset_mock()
            result = await handler(self.request)
            self.assertIs(result, self.error_result)
            self.assert_snapshot(status="failure", error_code="MCP_TOOL_ERROR")
        self.assertEqual(received_requests, [self.request, self.request])

    async def test_fallback_does_not_duplicate_record_emitted_by_real_middleware(self):
        async def call_next(context):
            context.message.arguments["params"]["duration"] = 60
            return self.error_result

        async def original_handler(request):
            context = SimpleNamespace(message=request.params, fastmcp_context=None)
            return await self.middleware.on_call_tool(context, call_next)

        handler = self.install(original_handler)
        result = await handler(self.request)

        self.assertIs(result, self.error_result)
        self.assert_snapshot(status="failure", error_code="MCP_TOOL_ERROR")


if __name__ == "__main__":
    unittest.main()
