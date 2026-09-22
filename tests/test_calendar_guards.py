"""Real calendar guards with offline CalendarItem data and mocked write methods."""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Attendee, CalendarItem, Mailbox
from calendar_operations import CalendarOperations
from tool_support import ToolOperationError


class CalendarGuardTests(unittest.TestCase):
    def setUp(self):
        self.ops = CalendarOperations()
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.event = CalendarItem(id="e", changekey="v", subject="old", type="Single",
                                 organizer=Mailbox(email_address=self.ops.config.email))
        self.ops._calendar_item = Mock(return_value=self.event)

    def invoke(self, tool, confirm=False):
        extra = {"subject": "new"} if tool == "update_event" else (
            {"response": "accept"} if tool == "respond_to_event" else {})
        return getattr(self.ops, tool)("e", confirm=confirm, confirmation_id="token", **extra)

    def test_recurring_masters_rejected_for_preview_and_confirm(self):
        self.event.type = "RecurringMaster"
        with patch.object(CalendarItem, "save") as save, patch.object(CalendarItem, "cancel") as cancel, \
                patch.object(CalendarItem, "accept") as accept, patch.object(CalendarItem, "move_to_trash") as trash:
            for tool in ("update_event", "respond_to_event", "cancel_event"):
                for confirm in (False, True):
                    with self.subTest(tool=tool, confirm=confirm):
                        with self.assertRaises(ToolOperationError) as cm:
                            self.invoke(tool, confirm)
                        self.assertEqual(cm.exception.code, "RECURRING_MASTER_UNSUPPORTED")
            for method in (save, cancel, accept, trash):
                method.assert_not_called()

    def test_update_and_cancel_require_organizer(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        for tool in ("update_event", "cancel_event"):
            for confirm in (False, True):
                with self.assertRaises(ToolOperationError) as cm:
                    self.invoke(tool, confirm)
                self.assertEqual(cm.exception.code, "NOT_ORGANIZER")

    def test_organizer_cannot_respond_even_if_listed_as_attendee(self):
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address=self.ops.config.email))]
        for confirm in (False, True):
            with self.assertRaises(ToolOperationError) as cm:
                self.invoke("respond_to_event", confirm)
            self.assertEqual(cm.exception.code, "ORGANIZER_CANNOT_RESPOND")

    def test_response_requires_attendee_membership(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        self.event.my_response_type = "NoResponseReceived"
        for confirm in (False, True):
            with self.assertRaises(ToolOperationError) as cm:
                self.invoke("respond_to_event", confirm)
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

    def test_single_occurrence_and_exception_allow_organizer_preview(self):
        for event_type in ("Single", "Occurrence", "Exception"):
            self.event.type = event_type
            for tool in ("update_event", "cancel_event"):
                result = self.invoke(tool)
                self.assertTrue(result["confirmation_required"])

    def test_attendee_response_preview_and_confirm_preserve_send_gate(self):
        self.event.organizer = Mailbox(email_address="other@example.com")
        for group in ("required_attendees", "optional_attendees", "resources"):
            setattr(self.event, group, [Attendee(mailbox=Mailbox(email_address="EMPLOYEE@example.com"))])
            for event_type in ("Single", "Occurrence", "Exception"):
                self.event.type = event_type
                with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "accept") as accept:
                    self.assertTrue(self.invoke("respond_to_event")["confirmation_required"])
                    accept.assert_not_called()
                    gate.assert_not_called()
                    self.assertTrue(self.invoke("respond_to_event", True)["responded"])
                    gate.assert_called_once()
                    accept.assert_called_once_with()
            setattr(self.event, group, [])

    def test_personal_cancel_still_soft_deletes_without_send(self):
        with patch("calendar_operations.require_send") as gate, \
                patch.object(CalendarItem, "move_to_trash") as trash, patch.object(CalendarItem, "cancel") as cancel:
            self.assertTrue(self.invoke("cancel_event", True)["deleted"])
            trash.assert_called_once_with(send_meeting_cancellations="SendToNone")
            cancel.assert_not_called()
            gate.assert_not_called()

    def test_update_notification_and_meeting_cancel_preserve_send_controls(self):
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address="other@example.com"))]
        for notify in (False, True):
            self.event.subject = "old"
            with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "save") as save:
                self.ops.update_event("e", subject="new", notify_attendees=notify, confirm=True, confirmation_id="token")
                save.assert_called_once_with(update_fields=["subject"], send_meeting_invitations=(
                    "SendToAllAndSaveCopy" if notify else "SendToNone"))
                self.assertEqual(gate.call_count, int(notify))
        with patch("calendar_operations.require_send") as gate, patch.object(CalendarItem, "cancel") as cancel, \
                patch.object(CalendarItem, "move_to_trash") as trash:
            self.assertFalse(self.invoke("cancel_event", True)["deleted"])
            cancel.assert_called_once_with()
            gate.assert_called_once()
            trash.assert_not_called()


if __name__ == "__main__":
    unittest.main()
