import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exchangelib import EWSDateTime, Message
from jsonschema import Draft202012Validator

from flag_operations import FLAG_COMPLETE, FLAG_FLAGGED, FLAG_NONE, FlagOperations
from tool_specs import SPECS
from tool_support import LOCAL_TIMEZONE, ToolOperationError, iso_datetime


OLD_DUE = "2026-10-12T18:00:00+08:00"
NEW_DUE = "2026-10-15T18:00:00+08:00"


def ews_date(value):
    return EWSDateTime.from_datetime(
        datetime.fromisoformat(value).astimezone(LOCAL_TIMEZONE)
    )


class MessageFlagTests(unittest.TestCase):
    def setUp(self):
        self.item = Message(
            id="message-id", changekey="v1", subject="Follow up",
            flag_status=FLAG_FLAGGED, task_complete=False,
            task_start_date=ews_date("2026-10-10T09:00:00+08:00"),
            task_due_date=ews_date(OLD_DUE),
        )
        self.ops = FlagOperations()
        self.ops.config = SimpleNamespace(email="employee@example.invalid")
        self.ops._tool_item = Mock(return_value=self.item)
        save_patch = patch.object(Message, "save")
        self.save = save_patch.start()
        self.addCleanup(save_patch.stop)

    def test_complete_preserves_existing_due_date_in_receipt(self):
        start = self.item.task_start_date
        result = self.ops.set_message_flag("message-id", "complete", confirm=True)
        self.assertEqual(iso_datetime(self.item.task_due_date), OLD_DUE)
        self.assertEqual(result["task_due_date"], OLD_DUE)
        self.assertEqual(self.item.task_start_date, start)
        self.assertNotIn("task_due_date", self.save.call_args.kwargs["update_fields"])
        self.assertEqual(result["flag_status"], FLAG_COMPLETE)
        self.assertTrue(result["task_complete"])

    def test_complete_without_existing_due_date_returns_none(self):
        self.item.task_due_date = None
        result = self.ops.set_message_flag("message-id", "complete", confirm=True)
        self.assertIsNone(self.item.task_due_date)
        self.assertIsNone(result["task_due_date"])

    def test_complete_replaces_due_date_when_supplied(self):
        result = self.ops.set_message_flag(
            "message-id", "complete", due_date=NEW_DUE, confirm=True
        )
        self.assertEqual(iso_datetime(self.item.task_due_date), NEW_DUE)
        self.assertEqual(result["task_due_date"], NEW_DUE)
        self.assertIn("task_due_date", self.save.call_args.kwargs["update_fields"])

    def test_flagged_omission_clears_due_date_and_completion(self):
        self.item.flag_status = FLAG_COMPLETE
        self.item.task_complete = True
        self.item.flag_complete_time = ews_date(OLD_DUE)
        self.item.task_date_completed = ews_date(OLD_DUE)
        result = self.ops.set_message_flag("message-id", "flagged", confirm=True)
        self.assertEqual(result["flag_status"], FLAG_FLAGGED)
        self.assertFalse(self.item.task_complete)
        for field in ("task_due_date", "flag_complete_time", "task_date_completed"):
            self.assertIsNone(getattr(self.item, field))
        self.assertIsNone(result["task_due_date"])

    def test_clear_without_date_clears_all_task_dates(self):
        self.item.flag_complete_time = ews_date(OLD_DUE)
        self.item.task_date_completed = ews_date(OLD_DUE)
        result = self.ops.set_message_flag("message-id", "clear", confirm=True)
        self.assertEqual(result["flag_status"], FLAG_NONE)
        self.assertFalse(result["task_complete"])
        self.assertIsNone(result["task_due_date"])
        for field in ("task_start_date", "task_due_date", "flag_complete_time",
                      "task_date_completed", "task_status"):
            self.assertIsNone(getattr(self.item, field))
        self.save.assert_called_once()

    def test_clear_rejects_any_date_before_lookup_or_write(self):
        for confirm in (False, True):
            for due in (NEW_DUE, "not-a-date", ""):
                with self.subTest(confirm=confirm, due=due):
                    with self.assertRaises(ToolOperationError) as caught:
                        self.ops.set_message_flag(
                            "message-id", "clear", due_date=due, confirm=confirm
                        )
                    self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.ops._tool_item.assert_not_called()
        self.save.assert_not_called()
        self.assertEqual(iso_datetime(self.item.task_due_date), OLD_DUE)


class MessageFlagSchemaTests(unittest.TestCase):
    def setUp(self):
        self.validator = Draft202012Validator(SPECS["set_message_flag"]["inputSchema"])
        self.identity = {"lanid": "test123", "name": "Test User"}

    def test_clear_with_date_is_rejected_in_preview_and_confirmation(self):
        for control in ({}, {"operation_id": "operation", "confirm_token": "token"}):
            for due in (NEW_DUE, "not-a-date", ""):
                with self.subTest(control=control, due=due):
                    arguments = {
                        **self.identity, "message_id": "message-id", "flag": "clear",
                        "due_date": due, **control,
                    }
                    self.assertFalse(self.validator.is_valid(arguments))

    def test_valid_flag_date_combinations_and_receipt_query_remain_valid(self):
        for flag in ("flagged", "complete", "clear"):
            for control in ({}, {"operation_id": "operation", "confirm_token": "token"}):
                arguments = {
                    **self.identity, "message_id": "message-id", "flag": flag, **control,
                }
                self.validator.validate(arguments)
                if flag != "clear":
                    self.validator.validate({**arguments, "due_date": NEW_DUE})
        self.validator.validate({**self.identity, "operation_id": "operation"})


if __name__ == "__main__":
    unittest.main()
