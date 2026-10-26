"""Audit the actual tool input without logging credentials or message content."""

import io
import json
import logging
from types import SimpleNamespace
import unittest

from fastmcp.tools.tool import ToolResult

from utils.audit import ToolAuditMiddleware


class AuditArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def record(self, arguments, downstream=None):
        output = io.StringIO()
        logger = logging.Logger("audit-arguments-test")
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger=logger)
        context = SimpleNamespace(
            message=SimpleNamespace(name="check_availability", arguments=arguments),
            fastmcp_context=None,
        )

        async def call_next(context):
            if downstream is not None:
                downstream(context.message.arguments)
            return ToolResult(content="done", structured_content={"ok": True})

        await middleware.on_call_tool(context, call_next)
        serialized = output.getvalue()
        self.assertEqual(len(serialized.splitlines()), 1)
        event = json.loads(serialized)
        self.assertIn("input_arguments", event)
        return event, serialized

    async def test_records_explicit_values_and_preserves_missing_defaults(self):
        event, _ = await self.record({
            "lanid": "employee", "name": "员工", "start": "2026-10-12T14:00:00+08:00",
            "end": "2026-10-12T15:00:00+08:00", "attendees": ["person@example.test"],
            "duration": 30, "limit": "10", "is_unread": False, "extra": None,
        })
        self.assertEqual(event["input_arguments"], {
            "lanid": "employee", "name": "员工", "start": "2026-10-12T14:00:00+08:00",
            "end": "2026-10-12T15:00:00+08:00", "attendees": ["person@example.test"],
            "duration": 30, "limit": "10", "is_unread": False, "extra": None,
        })
        self.assertNotIn("interval_minutes", event["input_arguments"])

    async def test_snapshots_input_before_downstream_mutates_nested_arguments(self):
        arguments = {"attendees": ["first@example.test"], "params": {"duration": 30}}

        def mutate(params):
            params["attendees"].append("later@example.test")
            params["params"]["duration"] = 60
            params["interval_minutes"] = 30

        event, _ = await self.record(arguments, mutate)
        self.assertEqual(event["input_arguments"], {
            "attendees": ["first@example.test"], "params": {"duration": 30},
        })

    async def test_recursively_redacts_credentials_and_private_content(self):
        event, serialized = await self.record({
            "params": {
                "confirm_token": "CONFIRM_SECRET",
                "nested": [{"Password": "PASSWORD_SECRET", "body": "敏感正文"}],
            },
            "apiKey": "API_SECRET", "authorization": "Bearer AUTH_SECRET",
            "idempotency_key": "IDEMPOTENCY_SECRET", "subject": "SUBJECT_SECRET",
            "internal_reply": "INTERNAL_SECRET", "external_reply": "EXTERNAL_SECRET",
            "message": "MESSAGE_SECRET", "content_base64": "BASE64_SECRET",
            "confirm_token": None,
            "query": "同事",
        })
        for secret in (
            "CONFIRM_SECRET", "PASSWORD_SECRET", "API_SECRET", "AUTH_SECRET",
            "IDEMPOTENCY_SECRET", "SUBJECT_SECRET", "INTERNAL_SECRET",
            "EXTERNAL_SECRET", "MESSAGE_SECRET", "BASE64_SECRET", "敏感正文",
        ):
            self.assertNotIn(secret, serialized)
        snapshot = event["input_arguments"]
        self.assertIn("confirm_token", snapshot["params"])
        self.assertEqual(snapshot["params"]["nested"][0]["body"], {
            "redacted": True, "type": "string", "length": 4,
        })
        self.assertIsNone(snapshot["confirm_token"])
        self.assertEqual(snapshot["query"], "同事")

    async def test_redacts_signed_urls_and_bearer_values_in_unexpected_fields(self):
        _, serialized = await self.record({
            "unexpected": "download https://files.example.test/downloads/abc?expires=1&token=SIGNED_SECRET",
            "other": "Bearer BEARER_SECRET",
        })
        self.assertNotIn("SIGNED_SECRET", serialized)
        self.assertNotIn("BEARER_SECRET", serialized)

    async def test_multiline_input_stays_one_json_log_record(self):
        event, _ = await self.record({"query": "甲\n乙\r丙\u0085丁\u2028戊\u2029己"})
        self.assertEqual(event["input_arguments"]["query"], "甲\n乙\r丙\u0085丁\u2028戊\u2029己")

    async def test_unpaired_surrogates_in_values_and_keys_are_safe_to_log(self):
        arguments = {"query": "prefix\ud800suffix", "field\udfff": "value"}
        event, serialized = await self.record(arguments)
        self.assertEqual(event["input_arguments"], arguments)
        serialized.encode("utf-8")

    async def test_redacts_singular_attachment_content(self):
        event, serialized = await self.record({
            "params": {"attachment": {"data": "ATTACHMENT_BYTES_SENTINEL"}},
        })
        self.assertNotIn("ATTACHMENT_BYTES_SENTINEL", serialized)
        self.assertEqual(event["input_arguments"]["params"]["attachment"], {
            "redacted": True, "type": "object", "length": 1,
        })

    async def test_malformed_identity_fields_cannot_exceed_input_budget(self):
        event, serialized = await self.record({
            "lanid": ["x" * 512] * 100, "name": {"a": "y" * 512},
        })
        snapshot = event["input_arguments"]
        self.assertTrue(snapshot["truncated"])
        self.assertLessEqual(len(json.dumps(snapshot).encode("utf-8")), 16384)
        self.assertLess(len(serialized.encode("utf-8")), 20000)

    async def test_oversized_input_is_bounded_and_marked_as_truncated(self):
        event, serialized = await self.record({"ids": ["标识" * 1000] * 1000})
        snapshot = event["input_arguments"]
        self.assertLessEqual(len(json.dumps(snapshot, ensure_ascii=False).encode("utf-8")), 16384)
        self.assertIn("truncated", json.dumps(snapshot))
        self.assertLess(len(serialized.encode("utf-8")), 20000)


if __name__ == "__main__":
    unittest.main()
