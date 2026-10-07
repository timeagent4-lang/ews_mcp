import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock
from mail_operations import MailOperations
from tool_support import ToolOperationError

class SearchBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.mail = MailOperations()
        self.folder = MagicMock()
        self.folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.return_value = []
        self.mail._tool_folder = Mock(return_value=self.folder)

    def test_aqs_alone_uses_positional_exchange_filter(self):
        self.mail.find_message(aqs="subject:hello", folder="sent")
        self.folder.filter.assert_called_once_with("subject:hello")
        self.mail._tool_folder.assert_called_once_with("sent")

    def test_keyword_combines_with_structured_filters(self):
        self.mail.find_message(query="hello", is_unread=False, has_attachments=True)
        self.folder.filter.assert_called_once_with(subject__contains="hello", is_read=True, has_attachments=True)

    def test_mixed_aqs_rejected_before_exchange(self):
        for extra in ({"query": "x"}, {"is_unread": False}, {"has_attachments": False},
                      {"since": "2026-01-01"}, {"until": "2026-01-02"}):
            with self.assertRaises(ToolOperationError):
                self.mail.find_message(aqs="subject:x", **extra)
        self.folder.filter.assert_not_called()
