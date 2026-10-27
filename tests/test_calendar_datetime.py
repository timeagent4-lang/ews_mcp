"""Serialize real SDK datetimes without sentinel dates or seconds in offsets."""
from datetime import date
from zoneinfo import ZoneInfo
import unittest

from exchangelib import Attendee, CalendarItem, EWSDateTime, Mailbox
from calendar_operations import _event_detail, _event_time
from tool_support import iso_datetime, parse_datetime


class CalendarDateTimeTests(unittest.TestCase):
    def test_last_response_sentinels_are_null_for_all_attendee_groups(self):
        for day in (1, 2):
            attendee = Attendee(mailbox=Mailbox(email_address="a@example.com"), response_type="Accept",
                                last_response_time=EWSDateTime(1, 1, day, tzinfo=ZoneInfo("UTC")))
            event = CalendarItem(required_attendees=[attendee], optional_attendees=[attendee], resources=[attendee])
            result = _event_detail(event)
            for group in ("required_attendees", "optional_attendees", "resources"):
                self.assertIsNone(result[group][0]["last_response_time"])
                self.assertEqual(result[group][0]["response_type"], "Accept")

    def test_valid_historical_sdk_time_preserves_instant_in_rfc3339(self):
        value = EWSDateTime(1900, 1, 1, tzinfo=ZoneInfo("UTC"))
        serialized = iso_datetime(value)
        self.assertEqual(serialized, "1900-01-01T00:00:00+00:00")
        self.assertEqual(parse_datetime(serialized), value)

    def test_modern_local_time_and_all_day_dates_are_unchanged(self):
        value = EWSDateTime(2026, 10, 27, 1, 2, 3, tzinfo=ZoneInfo("UTC"))
        self.assertEqual(iso_datetime(value), "2026-10-27T09:02:03+08:00")
        self.assertEqual(_event_time(date(2026, 10, 27)), "2026-10-27")
