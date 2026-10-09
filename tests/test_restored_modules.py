"""Offline checks for employee modules recovered from intranet source photos."""

import importlib
import os
import subprocess
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exchangelib import EWSDate, Task
from exchangelib.properties import FreeBusyView
from tool_support import ToolOperationError


class RestoredImportTests(unittest.TestCase):
    def test_real_server_import_has_all_employee_mixins(self):
        result = subprocess.run(
            [sys.executable, "-B", "-c", "import mcp_server"],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


class AvailabilityTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("availability_operations")
        self.ops = self.module.AvailabilityOperations()
        self.ops.account = SimpleNamespace(protocol=object())

    def query(self, responses):
        with patch.object(self.module, "GetUserAvailability") as service:
            service.return_value.call.return_value = iter(responses)
            return self.ops.check_availability(
                "2026-10-09T09:00:00+08:00", "2026-10-09T10:00:00+08:00",
                ["a@example.com", "b@example.com"],
            )

    def test_missing_nodata_and_misaligned_responses_never_claim_free(self):
        for responses in (
            [], [FreeBusyView(merged="00")],
            [FreeBusyView(merged="00"), FreeBusyView(merged="44")],
            [FreeBusyView(merged="00"), None],
        ):
            with self.subTest(responses=responses):
                result = self.query(responses)
                self.assertFalse(result["mutually_free_slot_possible"])
                self.assertIsNone(result["suggested_slot"])

    def test_free_window_suggests_first_bounded_slot(self):
        result = self.query([FreeBusyView(merged="00"), FreeBusyView(merged="00")])
        self.assertTrue(result["mutually_free_slot_possible"])
        self.assertEqual(result["suggested_slot"], {
            "start": "2026-10-09T09:00:00+08:00",
            "end": "2026-10-09T09:30:00+08:00", "duration_minutes": 30,
        })

    def test_lazy_exchange_failure_gets_public_error_and_keeps_cause(self):
        original = ConnectionError("offline")

        def broken_response():
            raise original
            yield  # noqa: unreachable -- models the SDK's lazy response iterator

        with patch.object(self.module, "GetUserAvailability") as service:
            service.return_value.call.return_value = broken_response()
            with self.assertRaises(ToolOperationError) as caught:
                self.ops.check_availability("2026-10-09T09:00", "2026-10-09T10:00", ["a@example.com"])
        self.assertEqual(caught.exception.code, "AVAILABILITY_UNAVAILABLE")
        self.assertIs(caught.exception.__cause__, original)


class OofTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("oof_operations")
        self.ops = self.module.OofOperations()
        self.ops.config = SimpleNamespace(email="a@example.com")
        self.ops.account = SimpleNamespace(protocol=object(), primary_smtp_address="a@example.com")

    def test_preview_does_not_submit_or_enable_external_audience_by_default(self):
        with patch.object(self.module, "SetUserOofSettings") as service:
            result = self.ops.set_oof(True, internal_reply="Internal only")
        self.assertTrue(result["confirmation_required"])
        self.assertIsNone(result["preview"]["items"][0]["external_audience"])
        service.assert_not_called()

    def test_confirmed_enable_preserves_send_gate(self):
        with patch.dict(os.environ, {"EWS_MCP_SEND_ENABLED": "false"}), \
                patch.object(self.module, "SetUserOofSettings") as service:
            with self.assertRaises(ToolOperationError) as caught:
                self.ops.set_oof(True, internal_reply="Internal only", confirm=True)
        self.assertEqual(caught.exception.code, "SEND_DISABLED")
        service.assert_not_called()


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("task_operations")
        self.ops = self.module.TaskOperations()
        self.ops.config = SimpleNamespace(email="a@example.com")

    def test_create_preview_formats_task_dates_without_write(self):
        with patch.object(Task, "save") as save:
            result = self.ops.create_task("Follow up", start_date="2026-10-09", due_date="2026-10-10")
        self.assertEqual(result["preview"]["items"][0]["start_date"], "2026-10-09")
        self.assertEqual(result["preview"]["items"][0]["due_date"], "2026-10-10")
        save.assert_not_called()

    def test_update_preview_does_not_mutate_task_and_confirm_updates_status(self):
        task = Task(id="task-id", changekey="v1", subject="Follow up", status=Task.NOT_STARTED)
        self.ops._tool_item = Mock(return_value=task)
        with patch.object(Task, "save") as save:
            preview = self.ops.update_task("task-id", complete=True, due_date="2026-10-10")
            self.assertTrue(preview["confirmation_required"])
            self.assertEqual(task.status, Task.NOT_STARTED)
            save.assert_not_called()
            result = self.ops.update_task("task-id", complete=True, due_date="2026-10-10", confirm=True)
        self.assertTrue(result["complete"])
        self.assertEqual(task.due_date, EWSDate(2026, 10, 10))
        save.assert_called_once_with(update_fields=["status", "percent_complete", "due_date"])


if __name__ == "__main__":
    unittest.main()
