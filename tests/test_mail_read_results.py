"""Offline regressions for search lookahead errors and empty conversations."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock

from exchangelib.errors import ErrorAccessDenied

from mail_operations import MailOperations
from tool_support import ToolOperationError


def folder_with_rows(rows):
    folder = MagicMock()
    folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.side_effect = (
        lambda page: rows[page]
    )
    return folder


def row(item_id, sent="2026-09-22T09:00:00+08:00"):
    return SimpleNamespace(id=item_id, datetime_sent=sent,
                           datetime_received=None, datetime_created=None)


class MailReadResultTests(unittest.TestCase):
    def setUp(self):
        self.mail = MailOperations()
        self.mail._tool_item = Mock(side_effect=lambda item_id, **kw: row(item_id))
        self.mail._message_dict = Mock(side_effect=lambda item, **kw: {"id": item.id})

    def test_search_raises_original_exception_in_page_or_lookahead_before_item_reads(self):
        for position in (0, 1, 2):
            with self.subTest(position=position):
                failure = ErrorAccessDenied("denied")
                rows = [row("a"), row("b"), row("c")]
                rows[position] = failure
                self.mail._tool_folder = Mock(return_value=folder_with_rows(rows))
                with self.assertRaises(ErrorAccessDenied) as caught:
                    self.mail.find_message(limit=2)
                self.assertIs(caught.exception, failure)
                self.mail._tool_item.assert_not_called()

    def test_search_empty_and_normal_pages_keep_next_offset(self):
        for count, offset, expected, next_offset in (
            (0, 0, [], None), (2, 0, ["a", "b"], None),
            (3, 0, ["a", "b"], 2), (3, 2, ["c"], None),
        ):
            with self.subTest(count=count, offset=offset):
                self.mail._tool_folder = Mock(return_value=folder_with_rows(
                    [row(name) for name in ("a", "b", "c")][:count]))
                result = self.mail.find_message(limit=2, offset=offset)
                self.assertEqual(result, {"items": [{"id": name} for name in expected],
                                          "next_offset": next_offset})

    def setup_thread(self, inbox, sent):
        source = SimpleNamespace(conversation_id=SimpleNamespace(id="conversation"))
        self.mail._tool_item.side_effect = lambda item_id, **kw: source if item_id == "source" else row(item_id)
        def folder(name):
            value = {"inbox": inbox, "sent": sent}[name]
            if isinstance(value, Exception):
                raise value
            return value
        self.mail._tool_folder = Mock(side_effect=folder)

    def test_empty_thread_with_both_folders_accessible_is_success(self):
        self.setup_thread(folder_with_rows([]), folder_with_rows([]))
        result = self.mail.get_thread("source")
        self.assertEqual(result, {"conversation_id": "conversation", "items": [],
                                  "coverage": {"inbox": "ok", "sent": "ok"},
                                  "partial": False, "next_offset": None})

    def test_empty_thread_with_one_accessible_folder_is_partial_success(self):
        for unavailable in ("inbox", "sent"):
            with self.subTest(unavailable=unavailable):
                folders = {"inbox": folder_with_rows([]), "sent": folder_with_rows([])}
                folders[unavailable] = ErrorAccessDenied("denied")
                self.setup_thread(**folders)
                result = self.mail.get_thread("source")
                self.assertEqual(result["items"], [])
                self.assertIsNone(result["next_offset"])
                self.assertTrue(result["partial"])
                self.assertEqual(result["coverage"][unavailable], "denied")
                self.assertEqual(set(result["coverage"].values()), {"ok", "denied"})

    def test_thread_with_neither_folder_accessible_still_fails(self):
        self.setup_thread(ErrorAccessDenied("denied"), RuntimeError("unreachable"))
        with self.assertRaises(ToolOperationError) as caught:
            self.mail.get_thread("source")
        self.assertEqual(caught.exception.code, "THREAD_FOLDERS_UNAVAILABLE")

    def test_thread_merges_and_paginates_as_before(self):
        self.setup_thread(folder_with_rows([row("a"), row("c")]), folder_with_rows([row("b")]))
        for offset, expected, next_offset in ((0, ["a", "b"], 2), (2, ["c"], None), (3, [], None)):
            with self.subTest(offset=offset):
                result = self.mail.get_thread("source", offset=offset, limit=2)
                self.assertEqual(result["items"], [{"id": name} for name in expected])
                self.assertEqual(result["next_offset"], next_offset)
                self.assertFalse(result["partial"])

    def test_query_failure_is_not_converted_to_an_empty_success(self):
        folder = folder_with_rows([])
        failure = ErrorAccessDenied("query denied after folder resolution")
        folder.filter.side_effect = failure
        self.setup_thread(folder, folder_with_rows([]))
        with self.assertRaises(ErrorAccessDenied) as caught:
            self.mail.get_thread("source")
        self.assertIs(caught.exception, failure)


if __name__ == "__main__":
    unittest.main()
