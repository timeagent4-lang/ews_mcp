"""Real calendar guards with offline CalendarItem data and mocked write methods."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from exchangelib import Account, Attendee, CalendarItem, EWSDateTime, Folder, Mailbox
from exchangelib.errors import ErrorAccessDenied, ErrorCorruptData, ErrorInvalidRecipients
from exchangelib.services import DeleteItem
from requests.exceptions import ReadTimeout
from calendar_operations import CalendarOperations
from tool_support import ToolOperationError
from tests.test_direct_operations import offline_account


class CalendarGuardTests(unittest.TestCase):
    def setUp(self):
        self.ops = CalendarOperations()
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.event = CalendarItem(account=offline_account(), id="e", changekey="v", subject="old", type="Single",
                                 start=EWSDateTime(2026, 10, 8, 9), end=EWSDateTime(2026, 10, 8, 10),
                                 organizer=Mailbox(email_address=self.ops.config.email))
        self.ops._calendar_item = Mock(return_value=self.event)

    def invoke(self, tool):
        extra = {"subject": "new"} if tool == "update_event" else (
            {"response": "accept"} if tool == "respond_to_event" else {})
        return getattr(self.ops, tool)("e", **extra)

    def test_recurring_masters_rejected_before_write(self):
        self.event.type = "RecurringMaster"
        with patch.object(CalendarItem, "save") as save, patch.object(CalendarItem, "cancel") as cancel, \
                patch.object(CalendarItem, "accept") as accept, patch.object(Account, "bulk_delete", return_value=[True]) as trash:
            for tool in ("update_event", "respond_to_event", "cancel_event"):
                with self.subTest(tool=tool):
                    with self.assertRaises(ToolOperationError) as cm:
                        self.invoke(tool)
                    self.assertEqual(cm.exception.code, "RECURRING_MASTER_UNSUPPORTED")
            for method in (save, cancel, accept, trash):
                method.assert_not_called()

    def test_update_and_cancel_require_organizer(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        for tool in ("update_event", "cancel_event"):
            with self.assertRaises(ToolOperationError) as cm:
                self.invoke(tool)
            self.assertEqual(cm.exception.code, "NOT_ORGANIZER")

    def test_organizer_cannot_respond_even_if_listed_as_attendee(self):
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address=self.ops.config.email))]
        with self.assertRaises(ToolOperationError) as cm:
            self.invoke("respond_to_event")
        self.assertEqual(cm.exception.code, "ORGANIZER_CANNOT_RESPOND")

    def test_response_requires_attendee_membership(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        self.event.my_response_type = "NoResponseReceived"
        with self.assertRaises(ToolOperationError) as cm:
            self.invoke("respond_to_event")
        self.assertEqual(cm.exception.code, "NOT_ATTENDEE")

    def test_unknown_event_type_is_not_a_clear_single_or_occurrence(self):
        self.event.type = None
        for tool in ("update_event", "respond_to_event", "cancel_event"):
            with self.assertRaises(ToolOperationError) as cm:
                self.invoke(tool)
            self.assertEqual(cm.exception.code, "INVALID_EVENT_TYPE")

    def test_organizer_response_marker_also_blocks_response(self):
        self.event.organizer = None
        self.event.my_response_type = "Organizer"
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address=self.ops.config.email))]
        with self.assertRaises(ToolOperationError) as cm:
            self.invoke("respond_to_event")
        self.assertEqual(cm.exception.code, "ORGANIZER_CANNOT_RESPOND")

    def test_single_occurrence_and_exception_allow_direct_organizer_write(self):
        for event_type in ("Single", "Occurrence", "Exception"):
            self.event.type = event_type
            for tool in ("update_event", "cancel_event"):
                self.event.subject = "old"
                with patch.object(CalendarItem, "save") as save, patch.object(Account, "bulk_delete", return_value=[True]) as trash:
                    result = self.invoke(tool)
                self.assertEqual(save.call_count + trash.call_count, 1)
                self.assertEqual(result["id"], "e")

    def test_attendee_direct_response_preserves_send_gate(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        for group in ("required_attendees", "optional_attendees", "resources"):
            setattr(self.event, group, [Attendee(mailbox=Mailbox(email_address="EMPLOYEE@example.com"))])
            for event_type in ("Single", "Occurrence", "Exception"):
                self.event.type = event_type
                with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "accept") as accept:
                    self.assertTrue(self.invoke("respond_to_event")["responded"])
                    gate.assert_called_once()
                    accept.assert_called_once_with()
            setattr(self.event, group, [])

    def test_personal_cancel_still_soft_deletes_without_send(self):
        with patch("calendar_operations.require_send") as gate, \
                patch.object(Account, "bulk_delete", return_value=[True]) as trash, patch.object(CalendarItem, "cancel") as cancel:
            self.assertTrue(self.invoke("cancel_event")["deleted"])
            trash.assert_called_once_with(ids=[self.event], delete_type="MoveToDeletedItems", send_meeting_cancellations="SendToNone")
            cancel.assert_not_called()
            gate.assert_not_called()

    def test_update_notification_and_meeting_cancel_preserve_send_controls(self):
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address="other@example.com"))]
        for notify in (False, True):
            self.event.subject = "old"
            with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "save") as save:
                self.ops.update_event("e", subject="new", notify_attendees=notify)
                save.assert_called_once_with(update_fields=["subject"], conflict_resolution="NeverOverwrite", send_meeting_invitations=(
                    "SendToAllAndSaveCopy" if notify else "SendToNone"))
                self.assertEqual(gate.call_count, int(notify))
        with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "cancel") as cancel, \
                patch.object(Account, "bulk_delete", return_value=[True]) as trash:
            self.assertFalse(self.invoke("cancel_event")["deleted"])
            cancel.assert_called_once_with()
            gate.assert_called_once()
            trash.assert_not_called()

    def test_cancel_response_delivery_failure_stays_unknown(self):
        self.event.is_meeting = True
        with patch("calendar_operations.require_send"), patch.object(CalendarItem, "cancel", side_effect=ErrorInvalidRecipients("delivery")) as cancel, \
                patch.object(Account, "bulk_delete", return_value=[True]) as trash:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.cancel_event("e")
        self.assertEqual(cm.exception.status, "unknown")
        self.assertEqual(cm.exception.code, "CANCEL_OUTCOME_UNKNOWN")
        self.assertIsInstance(cm.exception.__cause__, ErrorInvalidRecipients)
        cancel.assert_called_once()
        trash.assert_not_called()

    def test_cancel_permission_rejection_is_failed_without_retry(self):
        self.event.is_meeting = True
        with patch("calendar_operations.require_send"), patch.object(CalendarItem, "cancel", side_effect=ErrorAccessDenied("denied")) as cancel:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.cancel_event("e")
        self.assertEqual(cm.exception.status, "failed")
        cancel.assert_called_once()

    def test_update_response_and_soft_delete_timeouts_are_unknown(self):
        for tool, write_method in (("update_event", "save"), ("respond_to_event", "accept"), ("cancel_event", "bulk_delete")):
            self.event.subject = "old"
            self.event.organizer = Mailbox(email_address=self.ops.config.email)
            self.event.required_attendees = []
            if tool == "respond_to_event":
                self.event.organizer = Mailbox(email_address="other@example.com")
                self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address=self.ops.config.email))]
            with self.subTest(tool=tool), patch("calendar_operations.require_send"), \
                    patch.object(Account if write_method == "bulk_delete" else CalendarItem, write_method, side_effect=ReadTimeout("write")) as write:
                with self.assertRaises(ToolOperationError) as cm:
                    self.invoke(tool)
                self.assertEqual(cm.exception.status, "unknown")
                self.assertIsInstance(cm.exception.__cause__, ReadTimeout)
                write.assert_called_once()

    def test_create_event_timeout_is_unknown_and_missing_response_is_unknown(self):
        self.ops.account = None
        self.ops._tool_folder = Mock(return_value=Folder(id="calendar"))
        for failure in (ReadTimeout("write"), None):
            with self.subTest(failure=type(failure).__name__), patch.object(CalendarItem, "save", side_effect=failure) as save:
                with self.assertRaises(ToolOperationError) as cm:
                    self.ops.create_event(subject="subject", start="2026-10-08T09:00:00+08:00", end="2026-10-08T10:00:00+08:00")
                self.assertEqual(cm.exception.status, "unknown")
                save.assert_called_once()

    def test_create_event_reads_calendar_before_submission(self):
        self.ops.account = None
        self.ops._tool_folder = Mock(side_effect=ReadTimeout("read"))
        with patch.object(CalendarItem, "save") as save:
            with self.assertRaises(ReadTimeout):
                self.ops.create_event(subject="subject", start="2026-10-08T09:00:00+08:00")
        save.assert_not_called()

    def test_real_sdk_personal_delete_does_not_lookup_trash_after_success(self):
        with patch.object(DeleteItem, "get", autospec=True, return_value=True), \
                patch.object(DeleteItem, "call", autospec=True, return_value=iter([True])) as delete_call, \
                patch.object(Account, "trash", new_callable=PropertyMock, side_effect=ErrorAccessDenied("trash denied")) as trash:
            result = self.ops.cancel_event("e")
        self.assertTrue(result["deleted"])
        self.assertEqual(result["id"], "e")
        trash.assert_not_called()
        self.assertEqual(delete_call.call_args.kwargs["delete_type"], "MoveToDeletedItems")

    def test_update_subject_length_is_failed_before_save(self):
        with patch.object(CalendarItem, "save", side_effect=ValueError("subject too long")) as save:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.update_event("e", subject="s" * 256)
        self.assertEqual(cm.exception.status, "failed")
        self.assertEqual(cm.exception.code, "INVALID_PARAMS")
        save.assert_not_called()

    def test_list_events_preserves_yielded_exchange_error(self):
        fault = ErrorCorruptData("broken event")
        calendar = Mock()
        calendar.view.return_value.order_by.return_value = [fault]
        self.ops._tool_folder = Mock(return_value=calendar)
        with self.assertRaises(Exception) as cm:
            self.ops.list_events("2026-10-08T09:00:00+08:00", "2026-10-08T10:00:00+08:00")
        self.assertIs(cm.exception, fault)

    def test_calendar_item_type_and_scope_guard_is_preserved(self):
        self.ops = CalendarOperations()
        self.ops._tool_item = Mock(return_value=Mailbox(email_address="user@example.com"))
        with self.assertRaises(ToolOperationError) as cm:
            self.ops.get_event("e")
        self.assertEqual(cm.exception.code, "INVALID_EVENT")
        self.assertEqual(self.ops._tool_item.call_args.kwargs["folder_names"], ("calendar",))


if __name__ == "__main__":
    unittest.main()
