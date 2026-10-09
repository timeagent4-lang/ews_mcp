"""Offline flag writes through the dispatcher and real confirmation store."""

import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import EWSDateTime, EWSTimeZone, Message

from confirmation import OperationStore
from flag_operations import FlagOperations
import mcp_server


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"
ORIGINAL_DUE = "2026-10-20T09:00:00+08:00"


class FlagConfirmationTests(unittest.TestCase):
    def setUp(self):
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        self.item = Message(
            id="flag-message", changekey="version-1", subject="Follow up",
            flag_status=2, task_complete=False,
            task_due_date=EWSDateTime(2026, 10, 20, 9, tzinfo=EWSTimeZone("Asia/Shanghai")),
        )
        self.client = FlagOperations()
        self.client.config = SimpleNamespace(email=MAILBOX)
        self.client._tool_item = Mock(return_value=self.item)
        self.saved = []

        def save(item, *, update_fields):
            self.saved.append({field: getattr(item, field) for field in update_fields})
            item.changekey = "version-2"
            return item

        for patcher in (
            patch.object(mcp_server, "get_store", return_value=self.store),
            patch.object(mcp_server, "resolve_identity_by_lanid",
                         return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(mcp_server, "initialize_outlook_client", return_value=self.client),
            patch.object(Message, "save", autospec=True, side_effect=save),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dispatch(self, **params):
        return mcp_server._dispatch(
            "set_message_flag", {**IDENTITY, **params}, None,
        ).structured_content

    def preview(self, **business):
        params = {"message_id": self.item.id, **business}
        pending = self.dispatch(**params)
        self.assertTrue(pending["ok"], pending)
        self.assertTrue(pending["confirmation_required"])
        self.assertEqual(self.saved, [])
        self.assertEqual(self.item.task_due_date.isoformat(), ORIGINAL_DUE)
        return params, pending

    def confirm(self, params, pending):
        receipt = self.dispatch(
            **params, operation_id=pending["operation_id"],
            confirm_token=pending["confirm_token"],
        )
        self.assertTrue(receipt["ok"], receipt)
        self.assertEqual(receipt["status"], "completed")
        self.assertEqual(len(self.saved), 1)
        return receipt

    def assert_receipt_round_trip(self, params, pending, receipt, expected_due):
        operation_id = pending["operation_id"]
        self.assertEqual(receipt["results"]["task_due_date"], expected_due)
        self.assertEqual(self.store.get(operation_id)["result"], receipt["results"])

        query = self.dispatch(operation_id=operation_id)
        self.assertTrue(query["ok"], query)
        self.assertEqual(query["results"], receipt["results"])

        replay = self.dispatch(
            **params, operation_id=operation_id,
            confirm_token=pending["confirm_token"],
        )
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["results"], receipt["results"])
        self.assertEqual(len(self.saved), 1)

    def test_complete_without_due_date_preserves_date_in_all_receipts(self):
        params, pending = self.preview(flag="complete")
        receipt = self.confirm(params, pending)

        self.assertEqual(self.item.task_due_date.isoformat(), ORIGINAL_DUE)
        self.assertNotIn("task_due_date", self.saved[0])
        self.assertEqual(self.item.flag_status, 1)
        self.assertTrue(self.item.task_complete)
        self.assert_receipt_round_trip(params, pending, receipt, ORIGINAL_DUE)

    def test_complete_with_due_date_replaces_date_in_all_receipts(self):
        params, pending = self.preview(
            flag="complete", due_date="2026-10-21T01:00:00Z",
        )
        receipt = self.confirm(params, pending)

        expected = "2026-10-21T09:00:00+08:00"
        self.assertEqual(self.item.task_due_date.isoformat(), expected)
        self.assertEqual(self.saved[0]["task_due_date"].isoformat(), expected)
        self.assert_receipt_round_trip(params, pending, receipt, expected)

    def test_clear_with_due_date_rejects_before_creating_preview_or_saving(self):
        for index, due_date in enumerate(("2026-10-30T09:00:00+08:00", "not-a-date")):
            with self.subTest(due_date=due_date):
                key = f"clear-with-date-{index}"
                result = self.dispatch(
                    message_id=self.item.id, flag="clear", due_date=due_date,
                    idempotency_key=key,
                )
                self.assertFalse(result["ok"], result)
                self.assertEqual(result["error_code"], "INVALID_PARAMS")
                self.assertIsNone(self.store.find_by_idempotency_key(key, mailbox=MAILBOX))
                self.assertEqual(self.saved, [])
                self.assertEqual(self.item.task_due_date.isoformat(), ORIGINAL_DUE)
                self.assertEqual(self.item.flag_status, 2)

    def test_clear_with_due_date_cannot_claim_an_existing_preview(self):
        params, pending = self.preview(flag="clear")
        operation_id = pending["operation_id"]
        result = self.dispatch(
            **params, due_date="2026-10-30T09:00:00+08:00",
            operation_id=operation_id, confirm_token=pending["confirm_token"],
        )

        self.assertFalse(result["ok"], result)
        self.assertEqual(result["error_code"], "INVALID_PARAMS")
        self.assertEqual(self.store.get(operation_id)["status"], "pending")
        self.assertEqual(self.saved, [])
        receipt = self.confirm(params, pending)
        self.assertIsNone(receipt["results"]["task_due_date"])

    def test_clear_without_due_date_removes_existing_date(self):
        params, pending = self.preview(flag="clear")
        receipt = self.confirm(params, pending)

        self.assertIsNone(self.item.task_due_date)
        self.assertIsNone(self.saved[0]["task_due_date"])
        self.assertEqual(self.item.flag_status, 0)
        self.assertFalse(self.item.task_complete)
        self.assert_receipt_round_trip(params, pending, receipt, None)

    def test_identity_and_operation_id_query_needs_no_exchange_connection(self):
        params, pending = self.preview(flag="complete")
        operation_id = pending["operation_id"]
        with patch.object(mcp_server, "initialize_outlook_client",
                          side_effect=AssertionError("Receipt queries must not connect to Exchange")):
            query = self.dispatch(operation_id=operation_id)
        self.assertFalse(query["ok"])
        self.assertEqual(query["status"], "pending")
        self.assertEqual(self.saved, [])

        receipt = self.confirm(params, pending)
        with patch.object(mcp_server, "initialize_outlook_client",
                          side_effect=AssertionError("Receipt queries must not connect to Exchange")):
            query = self.dispatch(operation_id=operation_id)
        self.assertTrue(query["ok"], query)
        self.assertEqual(query["results"], receipt["results"])
        self.assertEqual(len(self.saved), 1)

    def test_send_disabled_still_allows_confirmed_flag_update(self):
        with patch.dict(os.environ, {"EWS_MCP_SEND_ENABLED": "false"}):
            params, pending = self.preview(
                flag="flagged", due_date="2026-10-22T09:00:00+08:00",
            )
            receipt = self.confirm(params, pending)

        self.assertEqual(receipt["results"]["flag"], "flagged")
        self.assertEqual(self.item.task_due_date.isoformat(), "2026-10-22T09:00:00+08:00")
        self.assertFalse(self.item.task_complete)


if __name__ == "__main__":
    unittest.main()
