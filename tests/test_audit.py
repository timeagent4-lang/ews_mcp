"""Exercise the real audit middleware and FastMCP transport without Exchange."""

import asyncio
import io
import json
import logging
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastmcp import Client, FastMCP
from fastmcp.tools.tool import ToolResult

from utils import audit


class AuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.logger = logging.Logger("audit-test", level=logging.INFO)
        self.logger.addHandler(logging.StreamHandler(self.output))
        self.middleware = audit.ToolAuditMiddleware(logger=self.logger)

    def records(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    async def test_source_errors_record_codes_without_private_messages(self):
        await self.invoke(payload={"ok": False, "status": "partial", "results": [],
            "source_errors": {"gal": {"status": "failed", "error_code": "EXCHANGE_ACCESS_DENIED", "exchange_code": "ErrorAccessDenied", "message": "private-body"}}})
        finish = self.records()[1]
        self.assertEqual(finish["source_errors"][0]["source"], "gal")
        self.assertEqual(finish["source_errors"][0]["exchange_code"], "ErrorAccessDenied")
        self.assertNotIn("private-body", self.output.getvalue())

    async def invoke(self, *, tool="get_message", arguments=None, payload=None, raised=None):
        context = SimpleNamespace(
            message=SimpleNamespace(name=tool, arguments=arguments or {}),
            fastmcp_context=None,
        )

        async def call_next(_context):
            if raised is not None:
                raise raised
            return ToolResult(content="result", structured_content=payload or {
                "ok": True, "status": "success", "results": {},
            })

        return await self.middleware.on_call_tool(context, call_next)

    async def test_one_start_and_finish_with_the_same_request_id(self):
        await self.invoke(arguments={"mailbox": " USER@example.com ", "message_id": "ews-id"})
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual([row["phase"] for row in rows], ["start", "finish"])
        self.assertEqual(rows[0]["request_id"], rows[1]["request_id"])
        self.assertTrue(rows[0]["request_id"])
        self.assertEqual(rows[0]["mailbox"], "user@example.com")
        self.assertEqual(rows[0]["credential_mode"], "delegate")
        self.assertEqual(rows[0]["object_ids"], {"message_id": "ews-id"})
        self.assertEqual(rows[1]["status"], "success")
        self.assertGreaterEqual(rows[1]["duration_ms"], 0)

    async def test_partial_and_unknown_are_preserved_with_batch_counts(self):
        for status in ("partial", "unknown", "failed"):
            with self.subTest(status=status):
                self.output.seek(0)
                self.output.truncate()
                await self.invoke(tool="update_messages", payload={
                    "ok": False, "status": status, "error_code": "BATCH_PARTIAL_FAILURE",
                    "exchange_code": "ErrorAccessDenied",
                    "results": {"results": [
                        {"id": "one", "success": True},
                        {"id": "two", "success": False, "status": "failed"},
                        {"id": "three", "success": False, "status": "unknown"},
                    ]},
                })
                final = self.records()[-1]
                self.assertEqual(final["status"], status)
                self.assertEqual(final["success_count"], 1)
                self.assertEqual(final["failed_count"], 1)
                self.assertEqual(final["unknown_count"], 1)
                self.assertEqual(final["exchange_code"], "ErrorAccessDenied")

    async def test_read_partial_is_not_logged_as_full_success(self):
        await self.invoke(tool="get_thread", payload={
            "results": [], "partial": True, "coverage": {"sent": {"ok": False}},
        })
        self.assertEqual(self.records()[-1]["status"], "partial")

    async def test_batch_errors_log_codes_without_private_error_messages(self):
        await self.invoke(tool="delete_messages", payload={
            "ok": False, "status": "partial", "error_code": "BATCH_PARTIAL_FAILURE",
            "results": {"results": [
                {"id": "one", "success": True},
                {"id": "two", "success": False, "status": "failed",
                 "error_code": "ErrorAccessDenied", "exchange_code": "ErrorAccessDenied",
                 "message": "private upstream body", "exchange_message": "private login"},
            ]},
        })
        final = self.records()[-1]
        self.assertIn("item_errors", final)
        self.assertEqual(final["item_errors"], [{
            "id": "two", "status": "failed", "error_code": "ErrorAccessDenied",
            "exchange_code": "ErrorAccessDenied",
        }])
        self.assertNotIn("private upstream body", self.output.getvalue())
        self.assertNotIn("private login", self.output.getvalue())

    async def test_unrecognized_result_status_cannot_interrupt_a_tool_return(self):
        try:
            await self.invoke(payload={"ok": False, "status": [], "results": None})
        except TypeError:
            self.fail("Malformed result metadata interrupted the audit instead of logging a failed result")
        self.assertEqual(len(self.records()), 2)
        self.assertEqual(self.records()[-1]["status"], "failed")

    async def test_unicode_ids_still_produce_two_single_line_json_records(self):
        await self.invoke(arguments={
            "mailbox": "user@example.com", "message_id": "id\nline\u0085\u2028\u2029",
        })
        self.assertEqual(len(self.output.getvalue().splitlines()), 2)
        self.assertEqual(self.records()[-1]["object_ids"]["message_id"], "id\nline\u0085\u2028\u2029")

    async def test_password_presence_matches_credential_mode_without_trimming(self):
        for password, mode in ((None, "delegate"), ("", "delegate"), ("  ", "personal")):
            with self.subTest(password=password):
                self.output.seek(0)
                self.output.truncate()
                await self.invoke(arguments={"mailbox": "user@example.com", "password": password})
                self.assertEqual(self.records()[-1]["credential_mode"], mode)

    async def test_contact_values_and_credentials_never_enter_logs(self):
        arguments = {
            "mailbox": "user@example.com", "username": "private-login", "password": "p!",
            "contact_id": "contact-id", "folder_id": "folder-id", "query": "private-query",
            "contact": {
                "display_name": "private-name", "notes": "private-notes\nsecond line",
                "phone_numbers": {"MobilePhone": "private-number"},
                "photo": {"filename": "private-photo.jpg", "content": "private-base64"},
                "untrusted-key": "private-value",
            },
        }
        await self.invoke(tool="update_contact", arguments=arguments)
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["credential_mode"], "personal")
        self.assertIn("contact.notes", rows[-1]["modified_fields"])
        self.assertIn("contact.phone_numbers.MobilePhone", rows[-1]["modified_fields"])
        text = self.output.getvalue()
        for value in ("private-login", "p!", "private-query", "private-name", "private-notes",
                      "private-number", "private-photo.jpg", "private-base64", "untrusted-key"):
            self.assertNotIn(value, text)

    async def test_unhandled_exception_is_logged_without_its_message(self):
        with self.assertRaises(RuntimeError):
            await self.invoke(raised=RuntimeError("short-password and private body"))
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[-1]["status"], "failed")
        self.assertEqual(rows[-1]["exception_type"], "RuntimeError")
        self.assertNotIn("short-password", self.output.getvalue())
        self.assertNotIn("private body", self.output.getvalue())

    async def test_request_scope_is_reused_and_secrets_reset_after_exit(self):
        self.assertTrue(callable(getattr(audit, "request_scope", None)))
        with patch.dict(os.environ, {
            "OUTLOOK_ADMIN_PASSWORD": "env-password", "OUTLOOK_ADMIN_LANID": "service-login",
            "EWS_MCP_API_KEY": "short-key",
        }):
            self.assertIsNone(audit.current_request_id())
            with audit.request_scope({
                "mailbox": "user@example.com", "username": "private-login", "password": "p!",
                "body": "private-body", "query": "private-query", "subject": "private-subject",
                "message": "private-message", "contact": {"physical_addresses": {
                    "Home": {"street": "private-address"},
                }},
            }):
                request_id = audit.current_request_id()
                self.assertTrue(request_id)
                for value in ("p!", "private-login", "env-password", "service-login", "short-key",
                              "private-body", "private-query", "private-subject", "private-message",
                              "private-address"):
                    self.assertIn(value, audit.request_secrets())
                self.assertNotIn("user@example.com", audit.request_secrets())
                with audit.request_scope({}):
                    self.assertEqual(audit.current_request_id(), request_id)
            self.assertIsNone(audit.current_request_id())
            self.assertEqual(audit.request_secrets(), ())

    def make_server(self):
        server = FastMCP("real-audit-tests")
        server.add_middleware(self.middleware)

        @server.tool(name="get_message")
        async def get_message(mailbox: str, message_id: str, username: str = None, password: str = None):
            await asyncio.sleep(0)
            return ToolResult(content="ok", structured_content={
                "ok": True, "status": "success", "results": {"id": message_id},
                "request_id": getattr(audit, "current_request_id", lambda: None)(),
            })

        audit.install_tool_audit_fallback(server, self.middleware)
        return server

    async def test_real_fastmcp_response_and_logs_share_id_without_duplicates(self):
        server = self.make_server()
        async with Client(server) as client:
            result = await client.call_tool("get_message", {
                "mailbox": "user@example.com", "message_id": "ews-id", "password": "p!",
            })
        rows = self.records()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["request_id"], rows[1]["request_id"])
        self.assertEqual(rows[0]["request_id"], result.structured_content["request_id"])
        self.assertNotIn("p!", self.output.getvalue())
        self.assertIsNone(audit.current_request_id())

    async def test_unknown_tool_and_schema_rejection_have_two_logs(self):
        server = self.make_server()
        async with Client(server) as client:
            for name, arguments in (
                ("unregistered_tool", {"mailbox": "user@example.com", "password": "p!"}),
                ("get_message", {"mailbox": "user@example.com"}),
            ):
                with self.subTest(tool=name):
                    self.output.seek(0)
                    self.output.truncate()
                    result = await client.call_tool(name, arguments, raise_on_error=False)
                    self.assertTrue(result.is_error)
                    rows = self.records()
                    self.assertEqual(len(rows), 2)
                    self.assertEqual([row["phase"] for row in rows], ["start", "finish"])
                    self.assertEqual(rows[0]["request_id"], rows[1]["request_id"])
                    self.assertEqual(rows[1]["status"], "failed")
                    self.assertNotIn("p!", self.output.getvalue())

    async def test_concurrent_requests_do_not_share_ids_or_secrets(self):
        server = self.make_server()

        async def invoke(mailbox, password):
            async with Client(server) as client:
                return await client.call_tool("get_message", {
                    "mailbox": mailbox, "message_id": "ews-id", "password": password,
                })

        first, second = await asyncio.gather(
            invoke("first@example.com", "first-private"),
            invoke("second@example.com", "second-private"),
        )
        rows = self.records()
        self.assertEqual(len(rows), 4)
        self.assertNotEqual(first.structured_content["request_id"], second.structured_content["request_id"])
        for result, mailbox in ((first, "first@example.com"), (second, "second@example.com")):
            pair = [row for row in rows if row["request_id"] == result.structured_content["request_id"]]
            self.assertEqual([row["phase"] for row in pair], ["start", "finish"])
            self.assertTrue(all(row["mailbox"] == mailbox for row in pair))
        self.assertNotIn("first-private", self.output.getvalue())
        self.assertNotIn("second-private", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
