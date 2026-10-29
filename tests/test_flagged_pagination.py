"""Flagged-mail pagination through real SDK queries and the public MCP tool."""

from datetime import datetime, timedelta
import inspect
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, EWSDateTime, Message
from exchangelib.folders import Inbox, Root
from exchangelib.services import FindItem
from exchangelib.version import Build, Version
from fastmcp import Client
from jsonschema import Draft202012Validator

from flag_operations import FLAG_FLAGGED, FlagOperations
from tool_specs import SPECS
from tool_support import LOCAL_TIMEZONE, ToolOperationError


class FlaggedListingFixture:
    def setUp(self):
        self.account = Mock(spec=Account)
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.account.protocol = Mock()
        self.folder = Inbox(root=Root(account=self.account), id="inbox", changekey="folder-key")
        self.ops = FlagOperations()
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.ops._tool_folder = Mock(return_value=self.folder)
        self.rows = []
        self.requests = []
        patcher = patch.object(FindItem, "call", autospec=True, side_effect=self.find_items)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.account.fetch.side_effect = self.fetch

    def message(self, number):
        received = EWSDateTime.from_datetime(
            datetime(2026, 10, 29, 12, tzinfo=LOCAL_TIMEZONE) - timedelta(minutes=number)
        )
        item = Message(account=self.account, folder=self.folder, id=f"flag-{number}",
                       changekey="v1", subject=f"Follow up {number}",
                       datetime_received=received, flag_status=FLAG_FLAGGED,
                       task_complete=False)
        self.rows.append(item)

    def find_items(self, service, *, offset, max_items, additional_fields, order_fields, **kwargs):
        self.requests.append({"offset": offset, "max_items": max_items})
        self.assertEqual(order_fields[0].field_path.field.name, "datetime_received")
        self.assertTrue(order_fields[0].reverse)
        selected = self.rows[offset:offset + max_items]
        for row in selected:
            yield row if additional_fields or isinstance(row, Exception) else (row.id, row.changekey)

    def fetch(self, ids, **kwargs):
        by_id = {row.id: row for row in self.rows if not isinstance(row, Exception)}
        for row in ids:
            yield row if isinstance(row, Exception) else by_id[row[0]]


class FlaggedPaginationTests(FlaggedListingFixture, unittest.TestCase):
    def test_101_flags_remain_reachable_across_maximum_size_pages(self):
        for number in range(101):
            self.message(number)
        first = self.ops.list_flagged_messages(limit=100)
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(first["items"][0]["id"], "flag-0")
        self.assertEqual(first["items"][-1]["id"], "flag-99")
        self.assertEqual({key: first.get(key) for key in ("offset", "limit", "has_more", "next_offset")},
                         {"offset": 0, "limit": 100, "has_more": True, "next_offset": 100})
        final = self.ops.list_flagged_messages(limit=100, offset=first["next_offset"])
        self.assertEqual([row["id"] for row in final["items"]], ["flag-100"])
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_offset"])
        self.assertEqual(final["offset"], 100)
        self.assertEqual(final["limit"], 100)
        self.assertEqual((final["folder"], final["status"]), ("inbox", "flagged"))
        self.assertEqual(self.requests, [{"offset": 0, "max_items": 101},
                                         {"offset": 100, "max_items": 101}])

    def test_exactly_full_final_page_does_not_offer_another_page(self):
        for number in range(4):
            self.message(number)
        result = self.ops.list_flagged_messages(limit=2, offset=2)
        self.assertEqual([row["id"] for row in result["items"]], ["flag-2", "flag-3"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])

    def test_empty_and_out_of_range_pages_keep_pagination_metadata(self):
        for count, offset in ((0, 0), (1, 1), (1, 100)):
            with self.subTest(count=count, offset=offset):
                self.rows.clear()
                for number in range(count):
                    self.message(number)
                result = self.ops.list_flagged_messages(limit=20, offset=offset)
                self.assertEqual(result["items"], [])
                self.assertEqual({key: result[key] for key in ("offset", "limit", "has_more", "next_offset")},
                                 {"offset": offset, "limit": 20, "has_more": False, "next_offset": None})

    def test_invalid_offsets_are_rejected_before_folder_access(self):
        for value in (-1, True, False, 0.0, 1.5, "0", None):
            with self.subTest(value=value):
                with self.assertRaises(ToolOperationError) as caught:
                    self.ops.list_flagged_messages(offset=value)
                self.assertEqual(caught.exception.code, "INVALID_PAGINATION")
        self.ops._tool_folder.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_service_errors_in_visible_rows_or_lookahead_propagate(self):
        for position in (0, 1, 2):
            with self.subTest(position=position):
                self.rows.clear()
                self.message(0)
                self.message(1)
                error = RuntimeError("item read failed")
                self.rows.insert(position, error)
                with self.assertRaises(RuntimeError) as caught:
                    self.ops.list_flagged_messages(limit=2)
                self.assertIs(caught.exception, error)

    def test_third_positional_argument_stays_limit(self):
        self.message(0)
        self.message(1)
        result = self.ops.list_flagged_messages("inbox", "all", 1)
        self.assertEqual([row["id"] for row in result["items"]], ["flag-0"])
        self.assertEqual({key: result.get(key) for key in ("folder", "status", "offset", "limit", "next_offset")},
                         {"folder": "inbox", "status": "all", "offset": 0, "limit": 1, "next_offset": 1})


class FlaggedPaginationSchemaTests(unittest.TestCase):
    def test_offset_is_additive_and_matches_method_default(self):
        schema = SPECS["list_flagged_messages"]["inputSchema"]
        validator = Draft202012Validator(schema)
        self.assertTrue(validator.is_valid({"lanid": "employee"}))
        self.assertTrue(validator.is_valid({"lanid": "employee", "offset": 100, "limit": 100}))
        self.assertEqual(schema["properties"]["offset"]["default"],
                         inspect.signature(FlagOperations.list_flagged_messages).parameters["offset"].default)
        for value in (-1, True, False, 1.5, "0", None):
            with self.subTest(value=value):
                self.assertFalse(validator.is_valid({"lanid": "employee", "offset": value}))


class FlaggedPaginationMCPTests(FlaggedListingFixture, unittest.IsolatedAsyncioTestCase):
    async def test_public_tool_pages_real_business_results_and_rejects_bad_offsets(self):
        import mcp_server

        for number in range(3):
            self.message(number)
        with patch.dict(os.environ, {"EWS_MCP_DATA_DIR": "", "EWS_MCP_UPLOAD_ENABLED": "false",
                                     "EWS_MCP_DOWNLOAD_ENABLED": "false"}), \
                patch.object(mcp_server, "resolve_identity_by_lanid", return_value=self.ops.config), \
                patch.object(mcp_server.OutlookConfig, "from_service_env", return_value=self.ops.config), \
                patch.object(mcp_server, "OutlookClient", return_value=self.ops), \
                patch.object(mcp_server.tool_audit, "logger", Mock()):
            async with Client(mcp_server.mcp) as client:
                listed = next(tool for tool in await client.list_tools() if tool.name == "list_flagged_messages")
                self.assertTrue(Draft202012Validator(listed.inputSchema).is_valid(
                    {"lanid": "employee", "offset": 1, "limit": 1}))
                result = await client.call_tool("list_flagged_messages",
                    {"lanid": "employee", "offset": 1, "limit": 1}, raise_on_error=False)
                self.assertFalse(result.is_error)
                self.assertEqual([row["id"] for row in result.structured_content["results"]], ["flag-1"])
                self.assertEqual(result.structured_content["next_offset"], 2)
                self.assertTrue(result.structured_content["has_more"])
                self.requests.clear()
                for value in (-1, True, False, "0"):
                    rejected = await client.call_tool("list_flagged_messages",
                        {"lanid": "employee", "offset": value}, raise_on_error=False)
                    self.assertTrue(rejected.is_error)
                    self.assertEqual(rejected.structured_content["error_code"], "INVALID_PAGINATION")
                self.assertEqual(self.requests, [])


if __name__ == "__main__":
    unittest.main()
