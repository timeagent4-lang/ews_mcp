"""Meeting metadata through real SDK read shapes, XML parsing and MCP output."""

import asyncio
import json
import os
import tempfile
import unittest
from types import MethodType, SimpleNamespace
from unittest.mock import Mock, patch
from xml.sax.saxutils import escape

from exchangelib import Account
from exchangelib.errors import ErrorAccessDenied
from exchangelib.folders import Drafts, Inbox, Root, SentItems
from exchangelib.services.common import EWSService
from exchangelib.version import Build, Version
from fastmcp import Client
from requests import Response

from confirmation import OperationStore
from mail_operations import MailOperations
from test_tool_contracts import load_server
from tool_support import ToolOperationError


NS = {"m": "http://schemas.microsoft.com/exchange/services/2006/messages",
      "t": "http://schemas.microsoft.com/exchange/services/2006/types"}
INVITE_FIELDS = {"calendar:Start", "calendar:End", "calendar:IsAllDayEvent",
                 "calendar:Location", "calendar:Organizer", "meetingRequest:MeetingRequestType"}


class MeetingMailReadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.account = Mock(spec=Account)
        self.account.primary_smtp_address = "employee@example.com"
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.account.protocol = Mock(version=self.account.version, RETRY_WAIT=1)
        self.account.fetch = MethodType(Account.fetch, self.account)
        self.account._consume_item_service = MethodType(Account._consume_item_service, self.account)
        root = Root(account=self.account, id="root")
        folders = {name: cls(root=root, id=name) for name, cls in
                   (("inbox", Inbox), ("drafts", Drafts), ("sent", SentItems))}
        self.ops = MailOperations()
        self.ops.account = self.account
        self.ops.config = SimpleNamespace(email=self.account.primary_smtp_address)
        self.ops._tool_folder = lambda name: folders[name]
        self.kind = "MeetingRequest"
        self.item_class = "IPM.Schedule.Meeting.Request"
        self.parent = "inbox"
        self.linked_id = "calendar-event"
        self.start = "2026-10-31T00:00:00Z"
        self.end = "2026-10-31T00:30:00Z"
        self.all_day = False
        self.location = "Room A"
        self.failure = None
        self.requests = []
        for patcher in (
            patch.dict(os.environ, {"EWS_MCP_DATA_DIR": self.tmp.name,
                                   "EWS_MCP_UPLOAD_ENABLED": "false", "EWS_MCP_DOWNLOAD_ENABLED": "false"}),
            patch.object(EWSService, "_get_response", autospec=True, side_effect=self.response),
            patch("requests.sessions.Session.send", side_effect=AssertionError("Network is offline")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def response(self, service, payload, api_version):
        name = service.SERVICE_NAME
        self.assertIn(name, ("GetItem", "FindItem"))  # No calendar lookup or write service.
        uris = {node.get("FieldURI") for node in payload.findall(".//t:FieldURI", NS)}
        self.requests.append((name, uris))
        if self.kind != "MeetingRequest":
            self.assertFalse(uris & INVITE_FIELDS, "Requested invitation fields on another item type")
        if self.kind == "Message":
            self.assertFalse(any(uri.startswith("meeting:") for uri in uris))
        if self.failure and "meeting:AssociatedCalendarItemId" in uris:
            raise self.failure
        fields = {
            "item:ParentFolderId": f'<t:ParentFolderId Id="{self.parent}"/>',
            "item:ItemClass": f"<t:ItemClass>{self.item_class}</t:ItemClass>",
            "item:Subject": "<t:Subject>Invitation</t:Subject>",
            "item:Body": "<t:Body BodyType=\"Text\">abcdefghij</t:Body>",
            "item:HasAttachments": "<t:HasAttachments>false</t:HasAttachments>",
            "item:DateTimeReceived": "<t:DateTimeReceived>2026-10-28T01:00:00Z</t:DateTimeReceived>",
            "calendar:Start": f"<t:Start>{self.start}</t:Start>",
            "calendar:End": f"<t:End>{self.end}</t:End>",
            "calendar:IsAllDayEvent": f"<t:IsAllDayEvent>{str(self.all_day).lower()}</t:IsAllDayEvent>",
            "calendar:Location": f"<t:Location>{escape(self.location)}</t:Location>" if self.location is not None else "",
            "calendar:Organizer": "<t:Organizer><t:Mailbox><t:Name>Organizer</t:Name><t:EmailAddress>organizer@example.com</t:EmailAddress></t:Mailbox></t:Organizer>",
            "meeting:HasBeenProcessed": "<t:HasBeenProcessed>true</t:HasBeenProcessed>",
            "meeting:IsOutOfDate": "<t:IsOutOfDate>false</t:IsOutOfDate>",
            "meetingRequest:MeetingRequestType": "<t:MeetingRequestType>NewMeetingRequest</t:MeetingRequestType>",
        }
        if self.linked_id:
            fields["meeting:AssociatedCalendarItemId"] = f'<t:AssociatedCalendarItemId Id="{escape(self.linked_id)}" ChangeKey="event-key"/>'
        item = f'<t:{self.kind}><t:ItemId Id="mail" ChangeKey="key"/>' + "".join(fields.get(uri, "") for uri in uris) + f"</t:{self.kind}>"
        items = f"<t:Items>{item}</t:Items>"
        body = (f'<m:RootFolder IncludesLastItemInRange="true" TotalItemsInView="1">{items}</m:RootFolder>'
                if name == "FindItem" else f"<m:Items>{item}</m:Items>")
        xml = f'''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="{NS['m']}" xmlns:t="{NS['t']}"><s:Body><m:{name}Response><m:ResponseMessages><m:{name}ResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>{body}</m:{name}ResponseMessage></m:ResponseMessages></m:{name}Response></s:Body></s:Envelope>'''
        response = Response()
        response.status_code = 200
        response._content = xml.encode()
        response._content_consumed = True
        return response

    def test_invitation_details_include_exact_link_and_times_without_calendar_lookup(self):
        result = self.ops.get_message("mail")
        self.assertEqual(result.get("item_class"), "IPM.Schedule.Meeting.Request")
        self.assertEqual(result.get("start"), "2026-10-31T08:00:00+08:00")
        self.assertEqual(result.get("end"), "2026-10-31T08:30:00+08:00")
        self.assertIs(result.get("is_all_day"), False)
        self.assertEqual(result.get("location"), "Room A")
        self.assertEqual(result.get("organizer"), {"name": "Organizer", "email_address": "organizer@example.com"})
        self.assertEqual(result.get("associated_calendar_item_id"), "calendar-event")
        self.assertIs(result.get("has_been_processed"), True)
        self.assertIs(result.get("is_out_of_date"), False)
        self.assertEqual(result.get("meeting_request_type"), "NewMeetingRequest")
        self.assertEqual(result["attachments"], [])
        self.assertEqual(result["body"], "abcdefghij")
        self.assertEqual(len(self.requests), 2)
        self.assertTrue(INVITE_FIELDS <= self.requests[-1][1])

    def test_search_identifies_type_without_expanding_meeting_details(self):
        for include_body in (True, False):
            with self.subTest(include_body=include_body):
                self.requests.clear()
                result = self.ops.find_message(query="Invitation", include_body=include_body)
                self.assertIsNone(result["next_offset"])
                row = result["items"][0]
                self.assertEqual(row.get("item_class"), "IPM.Schedule.Meeting.Request")
                self.assertEqual("body" in row, include_body)
                self.assertNotIn("start", row)
                self.assertFalse(any(uris & INVITE_FIELDS for _, uris in self.requests))
                self.assertEqual(len(self.requests), 3)  # FindItem plus the two scoped GetItem calls.

    def test_other_item_types_request_only_their_own_fields(self):
        for kind, item_class in (("Message", "IPM.Note"),
                                 ("MeetingResponse", "IPM.Schedule.Meeting.Resp.Pos"),
                                 ("MeetingCancellation", "IPM.Schedule.Meeting.Canceled")):
            with self.subTest(kind=kind):
                self.kind, self.item_class = kind, item_class
                result = self.ops.get_message("mail", body_offset=2, max_body_chars=4)
                self.assertEqual(result.get("item_class"), item_class)
                self.assertEqual(result["body"], "cdef")
                self.assertEqual(result["next_body_offset"], 6)
                self.assertNotIn("start", result)
                if kind == "Message":
                    self.assertNotIn("associated_calendar_item_id", result)
                else:
                    self.assertEqual(result.get("associated_calendar_item_id"), "calendar-event")

    def test_missing_link_stays_null_and_parent_scope_is_still_enforced(self):
        self.linked_id = None
        self.location = None
        result = self.ops.get_message("mail")
        self.assertIn("associated_calendar_item_id", result)
        self.assertIsNone(result["associated_calendar_item_id"])
        self.assertIsNone(result["location"])
        self.assertEqual(result.get("start"), "2026-10-31T08:00:00+08:00")
        self.requests.clear()
        self.parent = "another-mailbox-folder"
        with self.assertRaises(ToolOperationError) as caught:
            self.ops.get_message("mail")
        self.assertEqual(caught.exception.code, "ITEM_OUT_OF_SCOPE")
        self.assertEqual(len(self.requests), 1)

    def test_all_day_invitation_serializes_sdk_values_in_local_timezone(self):
        self.all_day = True
        for start, end, expected_start, expected_end in (
            ("2026-10-30T16:00:00Z", "2026-10-31T16:00:00Z", "2026-10-31T00:00:00+08:00", "2026-11-01T00:00:00+08:00"),
            # MeetingRequest parses date-only XML into local midnight datetimes.
            ("2026-10-31", "2026-11-01", "2026-10-31T00:00:00+08:00", "2026-11-01T00:00:00+08:00"),
        ):
            with self.subTest(start=start):
                self.start, self.end = start, end
                result = self.ops.get_message("mail")
                self.assertTrue(result["is_all_day"])
                self.assertEqual(result["start"], expected_start)
                self.assertEqual(result["end"], expected_end)

    def test_existing_scoped_reads_do_not_expand_or_expose_meeting_details(self):
        item = self.ops._tool_item("mail", only_fields=["body"])
        result = self.ops._message_dict(item)
        self.assertEqual(result["body"], "abcdefghij")
        self.assertNotIn("start", result)
        self.assertNotIn("item_class", result)
        self.assertFalse(any(uris & INVITE_FIELDS for _, uris in self.requests))

    def test_meeting_field_read_errors_are_not_silently_converted_to_missing_data(self):
        self.failure = ErrorAccessDenied("metadata read denied")
        with self.assertRaises(ErrorAccessDenied):
            self.ops.get_message("mail")

    def test_mcp_json_and_structured_results_both_include_meeting_metadata(self):
        server = load_server()
        store = OperationStore(":memory:")
        self.addCleanup(store.close)
        async def call():
            async with Client(server.mcp) as client:
                return await client.call_tool("get_message", {"lanid": "employee", "message_id": "mail"}, raise_on_error=False)
        with patch.object(server, "get_store", return_value=store), \
                patch.object(server, "initialize_outlook_client", return_value=self.ops):
            result = asyncio.run(call())
        self.assertFalse(result.is_error)
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        self.assertEqual(result.structured_content["results"].get("associated_calendar_item_id"), "calendar-event")
