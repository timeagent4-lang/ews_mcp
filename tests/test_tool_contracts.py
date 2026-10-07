"""真实 MCP 入口测试，仅替换 Exchange 网络边界。"""
import asyncio
import inspect
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from exchangelib.errors import ErrorAccessDenied, ErrorInvalidChangeKey, UnauthorizedError
from fastmcp import Client
from jsonschema import Draft202012Validator

import mcp_server as server
from outlook_client import OutlookClient
from tool_specs import SPECS, TOOL_NAMES
from tool_support import ToolOperationError

MAILBOX = {"mailbox": "employee@example.com"}
ENV = {"OUTLOOK_SERVER": "exchange.example.com", "OUTLOOK_ADMIN_LANID": "public", "OUTLOOK_ADMIN_PASSWORD": "fixture-public-password"}
EXPECTED = {
    "list_folders", "get_mailbox_overview", "find_message", "get_message", "get_thread", "get_attachment",
    "prepare_attachment_download", "list_flagged_messages", "create_draft", "update_draft", "send_draft",
    "delete_draft", "update_messages", "move_messages", "delete_messages", "set_message_flag",
    "list_contact_folders", "list_contacts", "find_people", "get_contact", "create_contact", "update_contact",
    "list_events", "get_event", "create_event", "update_event", "respond_to_event", "cancel_event", "get_server_status",
}


class SchemaTests(unittest.TestCase):
    def test_exact_tool_set_and_flat_credentials(self):
        self.assertEqual(set(TOOL_NAMES), EXPECTED)
        for name, spec in SPECS.items():
            schema = spec["inputSchema"]
            Draft202012Validator.check_schema(schema)
            self.assertIn("mailbox", schema["required"])
            self.assertIn("password", schema["properties"])
            self.assertNotIn("params", schema["properties"])
            self.assertFalse(set(schema["properties"]) & {"lanid", "name", "confirm", "operation_id", "confirm_token", "idempotency_key"})

    def test_business_parameters_match_real_methods(self):
        for name, spec in SPECS.items():
            signature = inspect.signature(getattr(OutlookClient, name)).parameters
            for field in set(spec["inputSchema"]["properties"]) - {"mailbox", "username", "password"}:
                self.assertIn(field, signature, (name, field))
            for field, param in signature.items():
                if field != "self" and param.default is inspect.Parameter.empty:
                    self.assertIn(field, spec["inputSchema"]["required"], (name, field))

    def test_contact_and_conditional_validation(self):
        def valid(tool, **params):
            return Draft202012Validator(SPECS[tool]["inputSchema"]).is_valid({**MAILBOX, **params})
        self.assertTrue(valid("create_contact", contact={"display_name": "测试", "notes": "备注", "physical_addresses": {"Home": {"street": "街道"}}}))
        self.assertFalse(valid("create_contact", contact={}))
        self.assertTrue(valid("update_contact", contact_id="c", contact={"phone_numbers": {"MobilePhone": ""}}))
        self.assertFalse(valid("update_contact", contact_id="c", contact={"unknown": "x"}))
        self.assertFalse(valid("create_draft", mode="reply"))
        self.assertTrue(valid("create_draft", mode="reply", reply_to="m"))
        self.assertFalse(valid("find_message", limit=True))
        self.assertFalse(valid("find_message", aqs="subject:x", is_unread=False))

    def test_native_subject_limits_reject_before_exchange(self):
        for tool, extra in (("create_draft", {}), ("update_draft", {"draft_id": "m"}),
                            ("create_event", {"start": "2026-10-07T10:00:00+08:00"}), ("update_event", {"event_id": "e"})):
            validator = Draft202012Validator(SPECS[tool]["inputSchema"])
            self.assertFalse(validator.is_valid({**MAILBOX, **extra, "subject": "x" * 256}), tool)
            self.assertTrue(validator.is_valid({**MAILBOX, **extra, "subject": "x" * 255}), tool)


class MCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        env = patch.dict(os.environ, ENV, clear=True)
        env.start()
        self.addCleanup(env.stop)
        client = patch.object(server, "OutlookClient")
        self.exchange = client.start()
        self.addCleanup(client.stop)

    async def call(self, tool="get_server_status", **params):
        async with Client(server.mcp) as client:
            return await client.call_tool(tool, {**MAILBOX, **params}, raise_on_error=False)

    def payload(self, result, status):
        value = result.structured_content
        self.assertEqual(value["status"], status)
        self.assertEqual(value["ok"], status == "success")
        self.assertEqual(result.is_error, status != "success")
        self.assertEqual(json.loads(result.content[0].text), value)
        self.assertTrue(value["request_id"])
        return value

    async def test_registry_and_success(self):
        async with Client(server.mcp) as client:
            self.assertEqual({t.name for t in await client.list_tools()}, EXPECTED)
        self.exchange.return_value.get_server_status.return_value = {"connected": True}
        self.payload(await self.call(), "success")

    async def test_invalid_params_do_not_connect_or_echo_secret(self):
        result = await self.call(password="pw!", operation_id="old")
        self.payload(result, "failed")
        self.assertNotIn("pw!", result.content[0].text)
        self.exchange.assert_not_called()

    async def test_personal_failure_never_falls_back(self):
        self.exchange.side_effect = UnauthorizedError("bad pw!")
        value = self.payload(await self.call(password="pw!"), "failed")
        self.assertEqual(value["error_code"], "AUTHENTICATION_FAILED")
        self.assertNotIn("pw!", json.dumps(value))
        self.assertEqual(self.exchange.call_count, 1)
        self.assertEqual(self.exchange.call_args.args[0].credential_mode, "personal")

    async def test_permission_and_conflict_keep_real_exchange(self):
        for exc in (ErrorAccessDenied("denied"), ErrorInvalidChangeKey("conflict")):
            self.exchange.return_value.get_server_status.side_effect = exc
            value = self.payload(await self.call(), "failed")
            self.assertEqual(value["exchange_code"], type(exc).__name__)

    async def test_batch_partial_failed_and_unknown(self):
        for rows, status in (
            ([{"success": True}, {"success": False, "status": "failed", "error_code": "ITEM_NOT_FOUND"}], "partial"),
            ([{"success": False, "status": "failed"}], "failed"),
            ([{"success": True}, {"success": False, "status": "unknown"}], "unknown"),
        ):
            self.exchange.return_value.delete_messages.return_value = {"results": rows}
            value = self.payload(await self.call("delete_messages", ids=["m"]), status)
            self.assertEqual(value["success_count"], sum(r["success"] for r in rows))
            self.assertEqual(value["unknown_count"], sum(r.get("status") == "unknown" for r in rows))

    async def test_contact_partial_and_unknown_keep_known_save(self):
        for status in ("partial", "unknown"):
            self.exchange.return_value.create_contact.side_effect = ToolOperationError("PHOTO_FAILED", "照片保存失败", status=status, results={"id": "saved-contact"})
            value = self.payload(await self.call("create_contact", contact={"display_name": "测试"}), status)
            self.assertEqual(value["results"]["id"], "saved-contact")

    async def test_business_receives_no_credentials(self):
        self.exchange.return_value.list_contacts.return_value = {"items": []}
        self.payload(await self.call("list_contacts", password="personal", username="u", folder_id="f"), "success")
        self.exchange.return_value.list_contacts.assert_called_once_with(folder_id="f")

    async def test_multi_source_all_failed_not_successful_empty(self):
        self.exchange.return_value.find_people.return_value = {"items": [], "status": "failed", "ok": False,
            "source_errors": {"contacts": {"error_code": "EXCHANGE_ACCESS_DENIED"}}}
        value = self.payload(await self.call("find_people", query="x"), "failed")
        self.assertEqual(value["source_errors"]["contacts"]["request_id"], value["request_id"])

    async def test_unknown_tool_and_return_log_share_request_id(self):
        with self.assertLogs(server.tool_audit.logger, level="INFO") as logs:
            result = await self.call("not_registered")
        value = self.payload(result, "failed")
        records = [json.loads(line.split(":", 2)[2]) for line in logs.output]
        self.assertEqual(len(records), 2)
        self.assertEqual([r["phase"] for r in records], ["start", "finish"])
        self.assertEqual({r["request_id"] for r in records}, {value["request_id"]})

    async def test_concurrent_mailboxes_have_independent_configuration(self):
        seen = []
        def connect(config):
            seen.append((config.email, config.lanid, config.password))
            return SimpleNamespace(get_server_status=lambda: {"mailbox": config.email})
        self.exchange.side_effect = connect
        async with Client(server.mcp) as client:
            results = await asyncio.gather(*[
                client.call_tool("get_server_status", {"mailbox": f"u{i}@example.com", "password": f"p{i}"}) for i in range(2)
            ])
        self.assertEqual(set(seen), {("u0@example.com", "u0@example.com", "p0"), ("u1@example.com", "u1@example.com", "p1")})
        self.assertNotEqual(results[0].structured_content["request_id"], results[1].structured_content["request_id"])
