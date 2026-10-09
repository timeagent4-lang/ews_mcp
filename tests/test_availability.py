"""Offline free/busy facts contract, with real SDK payload serialization."""

import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from exchangelib.properties import FreeBusyView
from exchangelib.services import GetUserAvailability
from exchangelib.version import Build, Version
from jsonschema import Draft202012Validator

from availability_operations import AvailabilityOperations
from tool_specs import SPECS
from tool_support import ToolOperationError


START = "2026-10-09T09:00:00+08:00"
END = "2026-10-09T10:00:00+08:00"
NS = {"t": "http://schemas.microsoft.com/exchange/services/2006/types"}


class AvailabilityFactsTests(unittest.TestCase):
    def setUp(self):
        self.version = Version(build=Build(15, 2, 0, 0))
        self.ops = AvailabilityOperations()
        self.ops.account = SimpleNamespace(protocol=SimpleNamespace(version=self.version))
        self.payloads = []

    def query(self, responses, *, start=START, end=END, attendees=None, **kwargs):
        # The SDK constructs the actual request and parses actual response XML;
        # only the boundary that would contact Exchange is replaced.
        elements = [
            FreeBusyView(view_type="MergedOnly", merged=response.merged).to_xml(version=self.version)
            if isinstance(response, FreeBusyView) else response
            for response in responses
        ]

        def offline_transport(service, payload):
            self.payloads.append(payload)
            return iter(elements)

        with patch.object(GetUserAvailability, "_get_elements", offline_transport):
            return self.ops.check_availability(
                start, end, ["a@example.com"] if attendees is None else attendees,
                **kwargs,
            )

    def test_default_returns_facts_without_selection_fields(self):
        result = self.query([FreeBusyView(merged="00")])
        self.assertEqual(result, {
            "window_start": START,
            "window_end": END,
            "timezone": "Asia/Shanghai",
            "interval_minutes": 30,
            "attendees": [{
                "email": "a@example.com", "status": "free",
                "intervals": [{"start": START, "end": END, "status": "free"}],
            }],
        })
        signature = inspect.signature(self.ops.check_availability)
        self.assertIn("interval_minutes", signature.parameters)
        self.assertNotIn("duration", signature.parameters)

    def test_each_ews_status_is_preserved_and_unknown_dominates_summary(self):
        result = self.query(
            [FreeBusyView(merged="01234")], end="2026-10-09T11:30:00+08:00",
        )
        attendee = result["attendees"][0]
        self.assertEqual(attendee["status"], "unknown")
        self.assertIn("intervals", attendee)
        self.assertEqual(attendee["intervals"], [
            {"start": START, "end": "2026-10-09T09:30:00+08:00", "status": "free"},
            {"start": "2026-10-09T09:30:00+08:00", "end": END, "status": "tentative"},
            {"start": END, "end": "2026-10-09T10:30:00+08:00", "status": "busy"},
            {"start": "2026-10-09T10:30:00+08:00", "end": "2026-10-09T11:00:00+08:00", "status": "oof"},
            {"start": "2026-10-09T11:00:00+08:00", "end": "2026-10-09T11:30:00+08:00", "status": "unknown"},
        ])
        for merged in ("01", "02", "03"):
            with self.subTest(merged=merged):
                self.assertEqual(self.query([FreeBusyView(merged=merged)])["attendees"][0]["status"], "busy")

    def test_adjacent_equal_statuses_coalesce_without_losing_busy_boundaries(self):
        attendee = self.query(
            [FreeBusyView(merged="002200")], end="2026-10-09T12:00:00+08:00",
        )["attendees"][0]
        self.assertIn("intervals", attendee)
        self.assertEqual(attendee["intervals"], [
            {"start": START, "end": END, "status": "free"},
            {"start": END, "end": "2026-10-09T11:00:00+08:00", "status": "busy"},
            {"start": "2026-10-09T11:00:00+08:00", "end": "2026-10-09T12:00:00+08:00", "status": "free"},
        ])

    def test_final_partial_bucket_reports_only_its_actual_span(self):
        for end, merged, expected in (
            ("2026-10-09T09:15:00+08:00", "0", [
                {"start": START, "end": "2026-10-09T09:15:00+08:00", "status": "free"},
            ]),
            ("2026-10-09T10:15:00+08:00", "220", [
                {"start": START, "end": END, "status": "busy"},
                {"start": END, "end": "2026-10-09T10:15:00+08:00", "status": "free"},
            ]),
        ):
            with self.subTest(end=end):
                attendee = self.query([FreeBusyView(merged=merged)], end=end)["attendees"][0]
                self.assertIn("intervals", attendee)
                self.assertEqual(attendee["intervals"], expected)

    def test_malformed_missing_and_wrong_length_data_make_whole_attendee_unknown(self):
        for response in (
            None, RuntimeError("mailbox unavailable"), FreeBusyView(),
            FreeBusyView(merged=""), FreeBusyView(merged="0"),
            FreeBusyView(merged="000"), FreeBusyView(merged="2220"),
            FreeBusyView(merged="0x"), FreeBusyView(merged="05"),
        ):
            with self.subTest(response=response):
                self.assertEqual(self.query([response])["attendees"], [{
                    "email": "a@example.com", "status": "unknown",
                    "intervals": [{"start": START, "end": END, "status": "unknown"}],
                }])

    def test_response_count_mismatch_invalidates_mapping_for_every_attendee(self):
        for responses in ([], [FreeBusyView(merged="00")], [FreeBusyView(merged="00")] * 3):
            with self.subTest(count=len(responses)):
                result = self.query(responses, attendees=["a@example.com", "b@example.com"])
                self.assertEqual(result["attendees"], [
                    {"email": email, "status": "unknown", "intervals": [
                        {"start": START, "end": END, "status": "unknown"},
                    ]} for email in ("a@example.com", "b@example.com")
                ])

    def test_normalized_duplicate_mailboxes_are_queried_once_in_first_seen_order(self):
        result = self.query(
            [FreeBusyView(merged="44"), FreeBusyView(merged="00")],
            attendees=[" A@Example.COM ", "b@example.com", "a@example.com"],
        )
        self.assertEqual([item["email"] for item in result["attendees"]], ["a@example.com", "b@example.com"])
        self.assertEqual([item["status"] for item in result["attendees"]], ["unknown", "free"])
        self.assertEqual(self.payloads[-1].findall(".//t:Email/t:Address", NS)[0].text, "a@example.com")
        self.assertEqual([node.text for node in self.payloads[-1].findall(".//t:Email/t:Address", NS)],
                         ["a@example.com", "b@example.com"])

    def test_shanghai_timezone_and_utc_inputs_generate_equivalent_complete_ews_payloads(self):
        local = self.query([FreeBusyView(merged="00")])
        utc = self.query([FreeBusyView(merged="00")],
                         start="2026-10-09T01:00:00Z", end="2026-10-09T02:00:00Z")
        self.assertEqual(local, utc)
        for payload in self.payloads:
            self.assertEqual(payload.find("t:TimeZone/t:Bias", NS).text, "-480")
            for name in ("StandardTime", "DaylightTime"):
                transition = payload.find(f"t:TimeZone/t:{name}", NS)
                self.assertIsNotNone(transition)
                self.assertEqual({node.tag.rsplit("}", 1)[-1]: node.text for node in transition}, {
                    "Bias": "0", "Time": "00:00:00", "DayOrder": "0", "Month": "0", "DayOfWeek": "Sunday",
                })
            self.assertEqual(payload.find(".//t:TimeWindow/t:StartTime", NS).text, START)
            self.assertEqual(payload.find(".//t:TimeWindow/t:EndTime", NS).text, END)
            self.assertEqual(payload.find(".//t:MergedFreeBusyIntervalInMinutes", NS).text, "30")
            self.assertEqual(payload.find(".//t:RequestedView", NS).text, "MergedOnly")

    def test_interval_bounds_serialize_and_return_the_selected_sampling_resolution(self):
        for minutes, merged in ((5, "0" * 12), (1440, "0")):
            with self.subTest(minutes=minutes):
                result = self.query([FreeBusyView(merged=merged)], interval_minutes=minutes)
                self.assertEqual(result["interval_minutes"], minutes)
                self.assertEqual(result["attendees"][0]["intervals"], [
                    {"start": START, "end": END, "status": "free"},
                ])
                self.assertEqual(self.payloads[-1].find(".//t:MergedFreeBusyIntervalInMinutes", NS).text, str(minutes))

    def test_invalid_interval_and_attendee_counts_fail_before_transport(self):
        for value in (None, True, False, 0, -1, 1, 4, 1441, 30.0, "30"):
            with self.subTest(interval_minutes=value):
                with self.assertRaises(ToolOperationError) as caught:
                    self.query([], interval_minutes=value)
                self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        for attendees in ([], [f"u{i}@example.com" for i in range(101)]):
            with self.subTest(attendees=len(attendees)):
                with self.assertRaises(ToolOperationError):
                    self.query([], attendees=attendees)
        self.assertEqual(self.payloads, [])

    def test_invalid_time_range_remains_a_specific_public_error(self):
        for end in (START, "2026-10-09T08:59:00+08:00"):
            with self.subTest(end=end):
                with self.assertRaises(ToolOperationError) as caught:
                    self.query([], end=end)
                self.assertEqual(caught.exception.code, "INVALID_TIME_RANGE")
        self.assertEqual(self.payloads, [])


class AvailabilityPublicSchemaTests(unittest.TestCase):
    def test_sampling_parameter_and_attendee_count_validation(self):
        schema = SPECS["check_availability"]["inputSchema"]
        properties = schema["properties"]
        self.assertIn("interval_minutes", properties)
        self.assertNotIn("duration", properties)
        self.assertEqual(properties["interval_minutes"]["default"], 30)
        validator = Draft202012Validator(schema)
        base = {"lanid": "employee", "name": "Employee", "start": START, "end": END,
                "attendees": ["a@example.com"]}
        for minutes in (5, 30, 1440):
            with self.subTest(valid_minutes=minutes):
                self.assertTrue(validator.is_valid({**base, "interval_minutes": minutes}))
        for minutes in (True, False, 0, 4, 1441, "30", 30.5):
            with self.subTest(invalid_minutes=minutes):
                self.assertFalse(validator.is_valid({**base, "interval_minutes": minutes}))
        self.assertTrue(validator.is_valid(base))
        self.assertFalse(validator.is_valid({**base, "duration": 30}))
        self.assertFalse(validator.is_valid({**base, "attendees": []}))
        self.assertTrue(validator.is_valid({**base, "attendees": [f"u{i}@example.com" for i in range(100)]}))
        self.assertFalse(validator.is_valid({**base, "attendees": [f"u{i}@example.com" for i in range(101)]}))


if __name__ == "__main__":
    unittest.main()
