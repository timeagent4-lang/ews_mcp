"""Calendar confirmation versions through the real dispatcher and receipt store."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, Attendee, CalendarItem, Mailbox
from exchangelib.services.common import EWSService
from exchangelib.version import Build, Version
from requests import Response

from calendar_operations import CalendarOperations
from confirmation import OperationStore
from test_tool_contracts import load_server


MAILBOX = "employee@example.com"
BUSINESS = {
    "update_event": {"event_id": "event", "subject": "Updated subject"},
    "respond_to_event": {"event_id": "event", "response": "accept"},
    "cancel_event": {"event_id": "event"},
}


class CalendarConfirmationStateTests(unittest.TestCase):
    def setUp(self):
        self.server = load_server()
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        self.ops = CalendarOperations()
        self.ops.config = SimpleNamespace(email=MAILBOX)
        self.event = CalendarItem(
            id="event", changekey="v1", subject="Approved subject", type="Single",
            organizer=Mailbox(email_address=MAILBOX),
        )
        self.ops._calendar_item = Mock(return_value=self.event)
        self.effects = []
        for patcher in (
            patch.object(self.server, "get_store", return_value=self.store),
            patch.object(self.server, "resolve_identity_by_lanid",
                         return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(self.server, "initialize_outlook_client", return_value=self.ops),
            patch("calendar_operations.require_send"),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        for method in ("save", "cancel", "accept", "move_to_trash"):
            def effect(event, _method=method, **kwargs):
                self.effects.append((_method, kwargs))
            patcher = patch.object(CalendarItem, method, autospec=True, side_effect=effect)
            patcher.start()
            self.addCleanup(patcher.stop)

    def prepare_role(self, tool):
        self.event.changekey = "v1"
        self.event.subject = "Approved subject"
        self.event.organizer = Mailbox(email_address=(
            "organizer@example.com" if tool == "respond_to_event" else MAILBOX
        ))
        self.event.required_attendees = (
            [Attendee(mailbox=Mailbox(email_address=MAILBOX))]
            if tool == "respond_to_event" else []
        )

    def dispatch(self, tool, **control):
        return self.server._dispatch(
            tool, {"lanid": "employee", **BUSINESS[tool], **control}, None,
        ).structured_content

    def confirm(self, tool, pending):
        return self.dispatch(tool, operation_id=pending["operation_id"],
                             confirm_token=pending["confirm_token"])

    def test_previews_persist_the_version_without_writing(self):
        for tool in BUSINESS:
            with self.subTest(tool=tool):
                self.prepare_role(tool)
                pending = self.dispatch(tool)
                self.assertEqual(pending["status"], "pending")
                record = self.store.get(pending["operation_id"])
                self.assertEqual(record["details"].get("version"),
                                 {"id": "event", "changekey": "v1"})
        self.assertEqual(self.effects, [])

    def test_unchanged_versions_execute_once_and_receipts_replay(self):
        for tool, method in (("update_event", "save"), ("respond_to_event", "accept"),
                             ("cancel_event", "move_to_trash")):
            with self.subTest(tool=tool):
                self.prepare_role(tool)
                self.effects.clear()
                pending = self.dispatch(tool)
                receipt = self.confirm(tool, pending)
                self.assertEqual(receipt["status"], "completed", receipt)
                self.assertEqual([effect[0] for effect in self.effects], [method])
                self.assertEqual(self.confirm(tool, pending)["status"], "completed")
                self.assertEqual(len(self.effects), 1)

    def test_unchanged_field_values_still_produce_a_confirmable_versioned_preview(self):
        self.event.subject = "Updated subject"
        pending = self.dispatch("update_event")
        self.assertEqual(pending["status"], "pending")
        result = self.confirm("update_event", pending)
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["results"]["updated_fields"], [])
        self.assertEqual(self.effects, [])

    def test_changed_versions_are_known_pre_submission_failures(self):
        for tool in BUSINESS:
            with self.subTest(tool=tool):
                self.prepare_role(tool)
                pending = self.dispatch(tool)
                self.event.changekey = "v2"
                self.event.subject = "Changed after approval"
                result = self.confirm(tool, pending)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["error_code"], "EVENT_CHANGED")
                self.assertEqual(self.store.get(pending["operation_id"])["status"], "failed")
        self.assertEqual(self.effects, [])

    def test_changed_item_identity_is_rejected_even_with_the_same_change_key(self):
        pending = self.dispatch("cancel_event")
        self.event.id = "another-event"
        result = self.confirm("cancel_event", pending)
        self.assertEqual(result.get("error_code"), "EVENT_CHANGED", result)
        self.assertEqual(self.effects, [])

    def test_legacy_pending_previews_without_a_version_require_a_new_preview(self):
        for tool in BUSINESS:
            with self.subTest(tool=tool):
                self.prepare_role(tool)
                record = self.store.create_preview(
                    tool=tool, mailbox=MAILBOX, action=tool,
                    business_params=BUSINESS[tool], items=[], details={},
                )
                result = self.confirm(tool, record)
                self.assertEqual(result["status"], "failed", result)
                self.assertEqual(result["error_code"], "PREVIEW_VERSION_REQUIRED")
        self.assertEqual(self.effects, [])

    def test_appointment_preview_cannot_become_a_meeting_cancellation(self):
        pending = self.dispatch("cancel_event")
        self.assertEqual(self.store.get(pending["operation_id"])["details"]["delivery"], "delete")
        self.event.changekey = "v2"
        self.event.required_attendees = [
            Attendee(mailbox=Mailbox(email_address="new-recipient@example.com")),
        ]
        result = self.confirm("cancel_event", pending)
        self.assertEqual(result.get("error_code"), "EVENT_CHANGED", result)
        self.assertEqual(self.effects, [])


class CalendarUpdateConflictPolicyTests(unittest.TestCase):
    def test_update_soap_refuses_to_overwrite_a_concurrent_change(self):
        account = Mock(spec=Account)
        account.version = Version(build=Build(15, 2, 0, 0))
        account.protocol = Mock(version=account.version, RETRY_WAIT=1)
        event = CalendarItem(
            account=account, id="event", changekey="v1", subject="Original",
            type="Single", organizer=Mailbox(email_address=MAILBOX),
        )
        ops = CalendarOperations()
        ops.config = SimpleNamespace(email=MAILBOX)
        ops._calendar_item = Mock(return_value=event)
        server = load_server()
        store = OperationStore(":memory:")
        self.addCleanup(store.close)
        payloads = []

        def response(service, payload, api_version):
            payloads.append(payload)
            result = Response()
            result.status_code = 200
            result._content = b'''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"
                xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages"
                xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><s:Body>
                <m:UpdateItemResponse><m:ResponseMessages><m:UpdateItemResponseMessage ResponseClass="Success">
                <m:ResponseCode>NoError</m:ResponseCode><m:Items><t:CalendarItem>
                <t:ItemId Id="event" ChangeKey="v2"/></t:CalendarItem></m:Items>
                </m:UpdateItemResponseMessage></m:ResponseMessages></m:UpdateItemResponse>
                </s:Body></s:Envelope>'''
            result._content_consumed = True
            return result

        with patch.object(EWSService, "_get_response", autospec=True, side_effect=response), \
                patch("requests.sessions.Session.send", side_effect=AssertionError("Network is offline")), \
                patch.object(server, "get_store", return_value=store), \
                patch.object(server, "resolve_identity_by_lanid", return_value=SimpleNamespace(email=MAILBOX)), \
                patch.object(server, "initialize_outlook_client", return_value=ops):
            params = {"lanid": "employee", **BUSINESS["update_event"]}
            pending = server._dispatch("update_event", params, None).structured_content
            result = server._dispatch("update_event", {
                **params, "operation_id": pending["operation_id"],
                "confirm_token": pending["confirm_token"],
            }, None).structured_content
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(result["results"]["changekey"], "v2")
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0].get("ConflictResolution"), "NeverOverwrite")


if __name__ == "__main__":
    unittest.main()
