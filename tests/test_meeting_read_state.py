"""Meeting read-state writes through SDK SOAP and persisted confirmation receipts."""

from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, CalendarItem, Message
from exchangelib.errors import ErrorAccessDenied
from exchangelib.items.calendar_item import MeetingCancellation, MeetingRequest, MeetingResponse
from exchangelib.properties import ParentFolderId
from exchangelib.services.common import EWSService
from exchangelib.version import Build, Version
from requests import Response

from confirmation import OperationStore
from test_tool_contracts import load_server
from tool_support import ToolOperationError
from write_operations import WriteOperations


NS = {"m": "http://schemas.microsoft.com/exchange/services/2006/messages",
      "t": "http://schemas.microsoft.com/exchange/services/2006/types"}
MEETING_TYPES = (MeetingRequest, MeetingResponse, MeetingCancellation)


class MeetingReadStateTests(unittest.TestCase):
    def setUp(self):
        self.account = Mock(spec=Account)
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.account.protocol = Mock(version=self.account.version, RETRY_WAIT=1)
        self.ops = WriteOperations()
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.items = {}
        self.ops._tool_item = Mock(side_effect=lambda item_id, **kw: self.lookup(item_id))
        self.ops._tool_folder = Mock()
        self.payloads = []
        self.denied_id = None
        for patcher in (
            patch.object(EWSService, "_get_response", autospec=True, side_effect=self.response),
            patch("requests.sessions.Session.send", side_effect=AssertionError("Network is offline")),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lookup(self, item_id):
        item = self.items[item_id]
        if isinstance(item, Exception):
            raise item
        return item

    def item(self, cls, item_id="mail"):
        item = cls(account=self.account, id=item_id, changekey="before", subject="Meeting",
                   parent_folder_id=ParentFolderId(id="inbox"))
        self.items[item_id] = item
        return item

    def response(self, service, payload, api_version):
        self.assertEqual(service.SERVICE_NAME, "UpdateItem")  # No send, RSVP, move or calendar write.
        self.assertEqual(payload.get("MessageDisposition"), "SaveOnly")
        self.assertEqual(payload.get("SendMeetingInvitationsOrCancellations"), "SendToNone")
        self.assertEqual(payload.get("SuppressReadReceipts"), "true")
        self.payloads.append(payload)
        item_id = payload.find(".//t:ItemId", NS).get("Id")
        if item_id == self.denied_id:
            raise ErrorAccessDenied("write denied")
        result = Response()
        result.status_code = 200
        result._content = f'''<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" xmlns:m="{NS['m']}" xmlns:t="{NS['t']}"><s:Body><m:UpdateItemResponse><m:ResponseMessages><m:UpdateItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode><m:Items><t:Message><t:ItemId Id="{item_id}" ChangeKey="after"/></t:Message></m:Items></m:UpdateItemResponseMessage></m:ResponseMessages></m:UpdateItemResponse></s:Body></s:Envelope>'''.encode()
        result._content_consumed = True
        return result

    def assert_read_only_update(self, payload, value):
        updates = payload.find(".//t:Updates", NS)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].tag, f"{{{NS['t']}}}SetItemField")
        self.assertEqual(updates[0].find("t:FieldURI", NS).get("FieldURI"), "message:IsRead")
        self.assertIn(updates[0].find(".//t:IsRead", NS).text,
                      (str(value).lower(), str(int(value))))
        self.assertEqual(len(updates[0][1]), 1)

    def test_read_and_unread_supported_for_each_meeting_type(self):
        for cls in MEETING_TYPES:
            for value in (True, False):
                with self.subTest(cls=cls.__name__, value=value):
                    item = self.item(cls)
                    before = len(self.payloads)
                    result = self.ops.update_messages([item.id], set_read=value, confirm=True)
                    self.assertTrue(result["results"][0]["success"], result)
                    self.assertEqual(item.changekey, "after")
                    self.assertEqual(len(self.payloads), before + 1)
                    self.assert_read_only_update(self.payloads[-1], value)

    def test_meeting_categories_or_missing_read_change_stay_rejected(self):
        for cls in MEETING_TYPES:
            self.item(cls)
            for params in ({}, {"categories_add": ["tag"]},
                           {"set_read": True, "categories_add": []},
                           {"set_read": False, "categories_remove": ["tag"]}):
                with self.subTest(cls=cls.__name__, params=params):
                    result = self.ops.update_messages(["mail"], confirm=True, **params)
                    self.assertEqual(result["results"][0]["error_code"], "NOT_MAIL_MESSAGE")
        self.assertEqual(self.payloads, [])

    def test_mixed_batch_preserves_order_and_per_item_failures(self):
        self.item(Message, "ordinary")
        self.item(MeetingRequest, "invite")
        self.item(MeetingResponse, "denied")
        self.item(CalendarItem, "calendar")
        self.items["outside"] = ToolOperationError("ITEM_OUT_OF_SCOPE", "项目不属于当前员工允许访问的文件夹。")
        self.denied_id = "denied"
        ids = ["ordinary", "outside", "invite", "denied", "calendar"]
        result = self.ops.update_messages(ids, set_read=True, confirm=True)["results"]
        self.assertEqual([r["id"] for r in result], ids)
        self.assertEqual([r["success"] for r in result], [True, False, True, False, False])
        self.assertEqual(result[1]["error_code"], "ITEM_OUT_OF_SCOPE")
        self.assertEqual(result[3]["exchange_code"], "ErrorAccessDenied")
        self.assertEqual(result[4]["error_code"], "NOT_MAIL_MESSAGE")
        self.assertEqual(len(self.payloads), 3)

    def test_ordinary_categories_and_meeting_move_delete_limits_unchanged(self):
        item = self.item(Message)
        item.categories = ["old"]
        result = self.ops.update_messages(["mail"], categories_add=["new"], categories_remove=["old"], confirm=True)
        self.assertTrue(result["results"][0]["success"])
        self.assertEqual(item.categories, ["new"])
        self.assertEqual(self.payloads[-1].find(".//t:FieldURI", NS).get("FieldURI"), "item:Categories")
        self.payloads.clear()
        for cls in MEETING_TYPES:
            self.item(cls)
            for result in (self.ops.move_messages(["mail"], "sent", confirm=True),
                           self.ops.delete_messages(["mail"], confirm=True)):
                self.assertEqual(result["results"][0]["error_code"], "NOT_MAIL_MESSAGE")
        self.assertEqual(self.payloads, [])

    def test_dispatch_preview_confirmation_receipt_and_replay(self):
        server = load_server()
        store = OperationStore(":memory:")
        self.addCleanup(store.close)
        self.item(MeetingRequest)
        self.item(MeetingCancellation, "cancel")
        def dispatch(**params):
            return server._dispatch("update_messages", {"lanid": "employee", **params}, None).structured_content
        with patch.object(server, "get_store", return_value=store), \
                patch.object(server, "resolve_identity_by_lanid", return_value=SimpleNamespace(email=self.ops.config.email)), \
                patch.object(server, "initialize_outlook_client", return_value=self.ops):
            business = {"ids": ["mail", "cancel"], "set_read": True}
            preview = dispatch(**business)
            self.assertEqual(preview["status"], "pending", preview)
            self.assertEqual(self.payloads, [])
            confirm = {**business, "operation_id": preview["operation_id"], "confirm_token": preview["confirm_token"]}
            receipt = dispatch(**confirm)
            self.assertEqual(receipt["status"], "completed", receipt)
            self.assertTrue(all(r["success"] for r in receipt["results"]["results"]))
            self.assertEqual(dispatch(operation_id=preview["operation_id"])["results"], receipt["results"])
            self.assertTrue(dispatch(**confirm)["replayed"])
            self.assertEqual(len(self.payloads), 2)
