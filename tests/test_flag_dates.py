"""验证邮件旗标状态切换后的截止日期与实际保存字段。"""
import unittest
from unittest.mock import Mock, patch

from exchangelib import EWSDateTime, EWSTimeZone, Message
from flag_operations import FlagOperations
from tests.test_direct_operations import offline_account


class FlagDateTests(unittest.TestCase):
    def setUp(self):
        self.ops = FlagOperations()
        self.old_due = EWSDateTime(2026, 10, 20, 9, tzinfo=EWSTimeZone("Asia/Shanghai"))
        self.item = Message(account=offline_account(), id="mail", changekey="version",
                            task_due_date=self.old_due, flag_status=2, task_complete=False)
        self.ops._tool_item = Mock(return_value=self.item)

    def test_complete_without_due_preserves_and_returns_existing_date(self):
        for existing in (self.old_due, None):
            with self.subTest(existing=existing), patch.object(Message, "save", autospec=True) as save:
                self.item.task_due_date = existing
                result = self.ops.set_message_flag("mail", "complete")
                self.assertEqual(result["task_due_date"], existing.isoformat() if existing else None)
                self.assertEqual(self.item.task_due_date, existing)
                self.assertEqual((result["flag_status"], result["task_complete"]), (1, True))
                self.assertNotIn("task_due_date", save.call_args.kwargs["update_fields"])
                self.assertEqual(save.call_args.kwargs["conflict_resolution"], "NeverOverwrite")
                save.assert_called_once()

    def test_explicit_date_and_reset_transitions_remain_consistent(self):
        supplied = "2026-10-22T10:00:00+08:00"
        for state, date, expected_due, status, complete in (
                ("complete", supplied, supplied, 1, True),
                ("flagged", supplied, supplied, 2, False),
                ("flagged", None, None, 2, False),
                ("clear", supplied, None, 0, False),
                ("clear", None, None, 0, False)):
            with self.subTest(state=state, date=date), patch.object(Message, "save", autospec=True) as save:
                self.item.task_due_date = self.old_due
                result = self.ops.set_message_flag("mail", state, due_date=date)
                self.assertEqual(result["task_due_date"], expected_due)
                self.assertEqual(self.item.task_due_date.isoformat() if self.item.task_due_date else None, expected_due)
                self.assertEqual((result["flag_status"], result["task_complete"]), (status, complete))
                self.assertIn("task_due_date", save.call_args.kwargs["update_fields"])
                save.assert_called_once()
