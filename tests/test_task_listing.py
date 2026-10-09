"""Offline task listing contracts with real SDK queries and restrictions."""

from datetime import date, timedelta
import inspect
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, EWSDate, Task
from exchangelib.folders import Root, Tasks
from exchangelib.services import FindItem
from exchangelib.version import Build, Version
from jsonschema import Draft202012Validator

from task_operations import TaskOperations
from tool_specs import SPECS
from tool_support import ToolOperationError


NS = {"t": "http://schemas.microsoft.com/exchange/services/2006/types"}


class OfflineTaskListingFixture(unittest.TestCase):
    def setUp(self):
        self.account = Mock(spec=Account)
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.account.protocol = Mock()
        self.folder = Tasks(root=Root(account=self.account), id="tasks", changekey="folder-key")
        self.ops = TaskOperations()
        self.ops.account = self.account
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.ops._tool_folder = Mock(return_value=self.folder)
        self.tasks = []
        self.requests = []
        self.account.fetch.side_effect = self.fetch
        patcher = patch.object(FindItem, "call", autospec=True, side_effect=self.find_items)
        patcher.start()
        self.addCleanup(patcher.stop)
        for method in ("save", "complete"):
            patcher = patch.object(Task, method, side_effect=AssertionError("Listing wrote a task"))
            patcher.start()
            self.addCleanup(patcher.stop)

    def task(self, subject, status=Task.NOT_STARTED):
        task = Task(
            account=self.account, folder=self.folder, id=subject, changekey="task-key",
            subject=subject, status=status,
            is_complete=status == Task.COMPLETED,
            due_date=EWSDate.from_date(date(2030, 1, 1) - timedelta(days=len(self.tasks))),
            percent_complete=100 if status == Task.COMPLETED else 0,
        )
        self.tasks.append(task)
        return task

    def find_items(self, service, *, restriction, order_fields, offset, max_items, **kwargs):
        # Only the external service is replaced. QuerySet validates fields and
        # constructs/slices its query, and the real restriction serializes to XML.
        xml = restriction.to_xml(version=self.account.version) if restriction else None
        self.requests.append({"restriction": xml, "offset": offset, "max_items": max_items})
        rows = self.tasks
        if xml is not None:
            equality = xml.find(".//t:IsEqualTo", NS)
            if equality is None and xml.tag.endswith("IsEqualTo"):
                equality = xml
            self.assertIsNotNone(equality)
            self.assertEqual(equality.find("t:FieldURI", NS).get("FieldURI"), "task:IsComplete")
            self.assertIn(equality.find(".//t:Constant", NS).get("Value"), ("0", "false"))
            rows = [task for task in rows if isinstance(task, Exception) or not task.is_complete]
        self.assertEqual(len(order_fields), 1)
        self.assertEqual(order_fields[0].field_path.field.name, "due_date")
        self.assertTrue(order_fields[0].reverse)
        # Fixture due dates already run descending, including any injected errors.
        rows = rows[offset:offset + max_items] if max_items is not None else rows[offset:]
        yield from (task if isinstance(task, Exception) else (task.id, task.changekey) for task in rows)

    def fetch(self, ids, **kwargs):
        by_id = {task.id: task for task in self.tasks if not isinstance(task, Exception)}
        for item in ids:
            yield item if isinstance(item, Exception) else by_id[item[0]]


class TaskListingSDKTests(OfflineTaskListingFixture):
    def test_completed_tasks_cannot_consume_default_limit(self):
        for index in range(50):
            self.task(f"done-{index}", Task.COMPLETED)
        self.task("open")
        result = self.ops.list_tasks()
        self.assertEqual([item["subject"] for item in result["items"]], ["open"])
        self.assertEqual({key: result[key] for key in ("incomplete_only", "offset", "limit", "has_more", "next_offset")},
                         {"incomplete_only": True, "offset": 0, "limit": 50, "has_more": False, "next_offset": None})
        self.assertIsNotNone(self.requests[0]["restriction"])
        self.assertEqual(self.requests[0]["max_items"], 51)

    def test_short_page_counts_incomplete_tasks_only(self):
        for index in range(8):
            self.task(f"done-{index}", Task.COMPLETED)
        for subject in ("open-1", "open-2", "open-3", "open-4", "open-5", "open-6", "open-7"):
            self.task(subject)
        result = self.ops.list_tasks(limit=10)
        self.assertEqual([item["subject"] for item in result["items"]],
                         ["open-1", "open-2", "open-3", "open-4", "open-5", "open-6", "open-7"])
        self.assertFalse(result["has_more"])
        self.assertIsNone(result["next_offset"])

    def test_offset_pages_incomplete_tasks_and_reports_lookahead(self):
        self.task("done", Task.COMPLETED)
        for subject in ("first", "second", "third", "fourth"):
            self.task(subject)
        result = self.ops.list_tasks(limit=2, offset=1)
        self.assertEqual([item["subject"] for item in result["items"]], ["second", "third"])
        self.assertEqual({key: result[key] for key in ("offset", "limit", "has_more", "next_offset")},
                         {"offset": 1, "limit": 2, "has_more": True, "next_offset": 3})
        self.assertEqual(self.requests[0]["offset"], 1)
        self.assertEqual(self.requests[0]["max_items"], 3)

    def test_all_incomplete_statuses_survive_multiple_pages(self):
        for subject, status in (("not-started", "NotStarted"), ("done-1", "Completed"),
                                ("progress", "InProgress"), ("waiting", "WaitingOnOthers"),
                                ("done-2", "Completed"), ("deferred", "Deferred")):
            self.task(subject, status)
        first = self.ops.list_tasks(limit=2)
        final = self.ops.list_tasks(limit=2, offset=first["next_offset"])
        self.assertEqual([item["status"] for item in first["items"]], ["NotStarted", "InProgress"])
        self.assertEqual([item["status"] for item in final["items"]], ["WaitingOnOthers", "Deferred"])
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 2)
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_offset"])
        self.assertTrue(all(not item["complete"] for item in first["items"] + final["items"]))

    def test_incomplete_only_false_pages_the_full_mixed_list(self):
        self.task("done", "Completed")
        self.task("open", "InProgress")
        self.task("waiting", "WaitingOnOthers")
        first = self.ops.list_tasks(incomplete_only=False, limit=2)
        final = self.ops.list_tasks(incomplete_only=False, limit=2, offset=first["next_offset"])
        self.assertEqual([item["subject"] for item in first["items"]], ["done", "open"])
        self.assertEqual([item["complete"] for item in first["items"]], [True, False])
        self.assertEqual([item["subject"] for item in final["items"]], ["waiting"])
        self.assertFalse(first["incomplete_only"])
        self.assertEqual(first["next_offset"], 2)
        self.assertIsNone(final["next_offset"])
        self.assertTrue(all(request["restriction"] is None for request in self.requests))

    def test_final_empty_and_out_of_range_pages_have_no_next_offset(self):
        for subject in ("one", "two", "three", "four"):
            self.task(subject)
        for offset, expected in ((2, ["three", "four"]), (3, ["four"]), (4, []), (100, [])):
            with self.subTest(offset=offset):
                result = self.ops.list_tasks(limit=2, offset=offset)
                self.assertEqual([item["subject"] for item in result["items"]], expected)
                self.assertEqual(result["offset"], offset)
                self.assertEqual(result["limit"], 2)
                self.assertFalse(result["has_more"])
                self.assertIsNone(result["next_offset"])
        self.tasks.clear()
        empty = self.ops.list_tasks()
        self.assertEqual(empty["items"], [])
        self.assertFalse(empty["has_more"])
        self.assertIsNone(empty["next_offset"])

    def test_existing_third_positional_argument_remains_limit(self):
        self.task("one")
        self.task("two")
        result = self.ops.list_tasks("tasks", True, 1)
        self.assertEqual([item["subject"] for item in result["items"]], ["one"])
        self.assertEqual(result["limit"], 1)
        self.assertEqual(result["offset"], 0)
        self.assertEqual(result["next_offset"], 1)

    def test_maximum_page_size_keeps_lookahead_and_final_remainder(self):
        for index in range(150):
            self.task(f"open-{index}")
        first = self.ops.list_tasks(limit=100)
        final = self.ops.list_tasks(limit=100, offset=first["next_offset"])
        self.assertEqual(len(first["items"]), 100)
        self.assertEqual(first["items"][0]["subject"], "open-0")
        self.assertEqual(first["items"][-1]["subject"], "open-99")
        self.assertTrue(first["has_more"])
        self.assertEqual(first["next_offset"], 100)
        self.assertEqual(len(final["items"]), 50)
        self.assertEqual(final["items"][0]["subject"], "open-100")
        self.assertEqual(final["items"][-1]["subject"], "open-149")
        self.assertFalse(final["has_more"])
        self.assertIsNone(final["next_offset"])
        self.assertEqual([request["max_items"] for request in self.requests], [101, 101])

    def test_nonpositive_limit_fails_before_folder_resolution(self):
        for limit in (-1, 0):
            with self.subTest(limit=limit):
                with self.assertRaises(ToolOperationError) as caught:
                    self.ops.list_tasks(limit=limit)
                self.assertEqual(caught.exception.code, "INVALID_PAGINATION")
        self.ops._tool_folder.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_invalid_pagination_types_and_bounds_fail_before_folder_resolution(self):
        for name, values in (("limit", (101, True, False, 1.0, 1.5, "1", None)),
                             ("offset", (-1, True, False, 0.0, 1.5, "0", None))):
            for value in values:
                with self.subTest(name=name, value=value):
                    with self.assertRaises(ToolOperationError) as caught:
                        self.ops.list_tasks(**{name: value})
                    self.assertEqual(caught.exception.code, "INVALID_PAGINATION")
        self.ops._tool_folder.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_folder_and_incomplete_only_rejected_before_query(self):
        for folder in ("inbox", "raw-folder-id", None):
            with self.subTest(folder=folder):
                with self.assertRaises(ToolOperationError) as caught:
                    self.ops.list_tasks(folder=folder)
                self.assertEqual(caught.exception.code, "INVALID_FOLDER")
        for value in (0, 1, "false", None):
            with self.subTest(incomplete_only=value):
                with self.assertRaises(ToolOperationError) as caught:
                    self.ops.list_tasks(incomplete_only=value)
                self.assertEqual(caught.exception.code, "INVALID_PARAMS")
        self.ops._tool_folder.assert_not_called()
        self.assertEqual(self.requests, [])

    def test_service_exception_inside_page_or_lookahead_propagates(self):
        for position in (0, 1, 2):
            with self.subTest(position=position):
                self.tasks.clear()
                self.task("one")
                self.task("two")
                error = RuntimeError("Exchange service rejected the item")
                self.tasks.insert(position, error)
                with self.assertRaises(RuntimeError) as caught:
                    self.ops.list_tasks(limit=2)
                self.assertIs(caught.exception, error)


class TaskListingSchemaTests(unittest.TestCase):
    def setUp(self):
        self.schema = SPECS["list_tasks"]["inputSchema"]
        self.validator = Draft202012Validator(self.schema)
        self.identity = {"lanid": "employee", "name": "员工"}

    def test_valid_pagination_and_schema_defaults_match_public_method(self):
        self.assertTrue(self.validator.is_valid(self.identity))
        for limit, offset in ((1, 0), (100, 200)):
            self.assertTrue(self.validator.is_valid({**self.identity, "limit": limit, "offset": offset}))
        parameters = inspect.signature(TaskOperations.list_tasks).parameters
        for name in ("folder", "incomplete_only", "limit", "offset"):
            self.assertEqual(self.schema["properties"][name]["default"], parameters[name].default)
        self.assertEqual(self.schema["properties"]["offset"]["type"], "integer")
        self.assertEqual(self.schema["properties"]["offset"]["minimum"], 0)
        self.assertEqual(self.schema["properties"]["limit"]["minimum"], 1)
        self.assertEqual(self.schema["properties"]["limit"]["maximum"], 100)

    def test_schema_rejects_bad_pagination_folder_and_boolean_arguments(self):
        for name, values in (("limit", (-1, 0, 101, True, False, 1.5, "1", None)),
                             ("offset", (-1, True, False, 1.5, "0", None)),
                             ("folder", ("inbox", "raw-folder-id", None)),
                             ("incomplete_only", (0, 1, "false", None))):
            for value in values:
                with self.subTest(name=name, value=value):
                    self.assertFalse(self.validator.is_valid({**self.identity, name: value}))


if __name__ == "__main__":
    unittest.main()
