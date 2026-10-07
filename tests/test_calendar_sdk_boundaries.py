"""保留真实 CalendarView、会议回复模型与提交边界的离线回归。"""
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, PropertyMock, patch

from exchangelib import Account, Attendee, CalendarItem, EWSDateTime, EWSTimeZone, Mailbox
from exchangelib.errors import ErrorCorruptData
from exchangelib.folders import Calendar, Root, SentItems
from exchangelib.services import CreateItem, FindItem
from exchangelib.util import MNS, create_element
from requests.exceptions import ReadTimeout

from calendar_operations import CalendarOperations
from tool_support import ToolOperationError, error_details
from tests.test_direct_operations import offline_account


class CalendarPaginationTests(unittest.TestCase):
    def setUp(self):
        self.account = offline_account()
        self.folder = Calendar(root=Root(account=self.account), id="calendar")
        self.ops = CalendarOperations()
        self.ops._tool_folder = Mock(return_value=self.folder)
        # 服务端顺序故意混乱；提前限制 CalendarView 会丢掉真正靠前的事件。
        self.events = [CalendarItem(account=self.account, id=str(hour), changekey="v", type="Single",
                                   start=EWSDateTime(2026, 10, 8, hour, tzinfo=EWSTimeZone("Asia/Shanghai")))
                       for hour in (12, 10, 8, 9, 11)]
        self.payloads = []

    def pages(self, service, payload_func, kwargs, expected_message_count):
        payload = payload_func(**kwargs)
        self.payloads.append(payload)
        view = payload.find(f"{{{MNS}}}CalendarView")
        maximum = view.get("MaxEntriesReturned")
        events = self.events[:int(maximum)] if maximum else self.events
        page = create_element("m:RootFolder")
        items = create_element("t:Items")
        for event in events:
            elem = create_element("t:CalendarItem")
            elem.append(create_element("t:ItemId", attrs={"Id": event.id, "ChangeKey": event.changekey}))
            items.append(elem)
        page.append(items)
        return [(page, None)]

    def fetch(self, account, ids, **kwargs):
        indexed = {event.id: event for event in self.events}
        return iter(indexed[item_id] for item_id, changekey in list(ids))

    def test_sorted_pages_look_ahead_including_last_full_page_and_out_of_range(self):
        with patch.object(FindItem, "_get_pages", autospec=True, side_effect=self.pages), \
                patch.object(Account, "fetch", autospec=True, side_effect=self.fetch), \
                patch("requests.Session.request", side_effect=AssertionError("不允许真实网络")):
            for offset, limit, ids, more in ((0, 1, ["8"], True), (1, 1, ["9"], True),
                                           (2, 2, ["10", "11"], True), (3, 2, ["11", "12"], False),
                                           (4, 1, ["12"], False), (5, 1, [], False)):
                with self.subTest(offset=offset, limit=limit):
                    result = self.ops.list_events("2026-10-08", "2026-10-09", offset=offset, limit=limit)
                    self.assertEqual([event["id"] for event in result["items"]], ids)
                    self.assertEqual(result["has_more"], more)
                    self.assertEqual((result["offset"], result["limit"]), (offset, limit))
        self.assertTrue(self.payloads)
        for payload in self.payloads:
            self.assertIsNone(payload.find(f"{{{MNS}}}IndexedPageItemView"))
            self.assertIsNone(payload.find(f"{{{MNS}}}CalendarView").get("MaxEntriesReturned"))

    def test_lookahead_exchange_error_cannot_be_hidden_as_next_page(self):
        fault = ErrorCorruptData("lookahead failed")
        calendar = Mock()
        calendar.view.return_value.order_by.return_value = [self.events[0], fault]
        self.ops._tool_folder = Mock(return_value=calendar)
        with self.assertRaises(ErrorCorruptData) as cm:
            self.ops.list_events("2026-10-08", "2026-10-09", limit=1)
        self.assertIs(cm.exception, fault)


class CalendarSubmissionBoundaryTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.Session.request", side_effect=AssertionError("不允许真实网络"))
        network.start()
        self.addCleanup(network.stop)
        self.account = offline_account("employee@example.com")
        self.ops = CalendarOperations()
        self.ops.config = SimpleNamespace(email=self.account.primary_smtp_address)
        self.event = CalendarItem(account=self.account, id="event", changekey="version", type="Single",
                                  start=EWSDateTime(2026, 10, 8, 9), end=EWSDateTime(2026, 10, 8, 10))
        self.ops._calendar_item = Mock(return_value=self.event)
        self.sent = SentItems(root=Root(account=self.account), id="target-sent")
        self.ops._tool_folder = Mock(return_value=self.sent)

    def invoke(self, response):
        if response == "cancel":
            self.event.organizer = Mailbox(email_address=self.ops.config.email)
            self.event.is_meeting = True
            return self.ops.cancel_event("event", message="comment")
        self.event.organizer = Mailbox(email_address="organizer@example.com")
        self.event.required_attendees = [Attendee(mailbox=Mailbox(email_address=self.ops.config.email))]
        return self.ops.respond_to_event("event", response=response, message="comment")

    def test_sent_read_timeout_is_failed_with_no_submission(self):
        for response in ("accept", "tentative", "decline", "cancel"):
            with self.subTest(response=response), patch("calendar_operations.require_send"), \
                    patch.object(Account, "sent", new_callable=PropertyMock, side_effect=ReadTimeout("sent read")), \
                    patch.object(CreateItem, "_chunked_get_elements", autospec=True) as submit:
                self.ops._tool_folder = Mock(side_effect=ReadTimeout("sent read"))
                with self.assertRaises(Exception) as cm:
                    self.invoke(response)
                self.assertEqual(error_details(cm.exception)["status"], "failed")
                self.ops._tool_folder.assert_called_once_with("sent")
                submit.assert_not_called()

    def test_native_replies_use_target_sent_and_exact_event_reference_without_implicit_lookup(self):
        models = {"accept": "AcceptItem", "tentative": "TentativelyAcceptItem", "decline": "DeclineItem", "cancel": "CancelCalendarItem"}
        for response, model in models.items():
            with self.subTest(response=response), patch("calendar_operations.require_send"), \
                    patch.object(Account, "sent", new_callable=PropertyMock, side_effect=AssertionError("implicit sent lookup")) as implicit, \
                    patch.object(CreateItem, "_chunked_get_elements", autospec=True, return_value=iter([True])) as submit:
                self.ops._tool_folder.reset_mock()
                self.invoke(response)
                implicit.assert_not_called()
                self.ops._tool_folder.assert_called_once_with("sent")
                kwargs = submit.call_args.kwargs
                self.assertIs(kwargs["folder"], self.sent)
                self.assertEqual(kwargs["message_disposition"], "SendAndSaveCopy")
                reply = list(kwargs["items"])[0]
                self.assertEqual(type(reply).__name__, model)
                self.assertEqual((reply.reference_item_id.id, reply.reference_item_id.changekey), ("event", "version"))
                self.assertEqual(str(reply.body), "comment")

    def test_real_submission_timeout_stays_unknown_with_no_retry(self):
        for response in ("accept", "tentative", "decline", "cancel"):
            with self.subTest(response=response), patch("calendar_operations.require_send"), \
                    patch.object(Account, "sent", new_callable=PropertyMock, return_value=self.sent), \
                    patch.object(CreateItem, "_chunked_get_elements", autospec=True, side_effect=ReadTimeout("submit")) as submit:
                with self.assertRaises(ToolOperationError) as cm:
                    self.invoke(response)
                self.assertEqual(cm.exception.status, "unknown")
                self.assertIsInstance(cm.exception.__cause__, ReadTimeout)
                submit.assert_called_once()

    def test_personal_appointment_does_not_resolve_sent(self):
        self.event.organizer = Mailbox(email_address=self.ops.config.email)
        self.ops._tool_folder = Mock(side_effect=AssertionError("unneeded sent read"))
        with patch.object(Account, "bulk_delete", return_value=[True]) as delete, patch("calendar_operations.require_send") as gate:
            result = self.ops.cancel_event("event")
        self.assertTrue(result["deleted"])
        self.ops._tool_folder.assert_not_called()
        gate.assert_not_called()
        self.assertEqual(delete.call_args.kwargs["delete_type"], "MoveToDeletedItems")
