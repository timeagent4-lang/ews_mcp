import io
import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from exchangelib import FileAttachment, ItemAttachment
from exchangelib.attachments import AttachmentId

from mail_operations import MailOperations
from tool_support import ToolOperationError


class StreamingAttachment(FileAttachment):
    @property
    def content(self):
        raise AssertionError("must not load complete attachment content")

    @property
    def fp(self):
        return io.BytesIO(b"attachment bytes")


class AttachmentMailTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.env = patch.dict(os.environ, {
            "EWS_MCP_DATA_DIR": self.tmp.name,
            "EWS_MCP_DOWNLOAD_ENABLED": "true",
            "EWS_MCP_PUBLIC_BASE_URL": "http://host.internal:7712",
            "EWS_MCP_DOWNLOAD_SECRET": "x" * 32,
        }, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.mail = MailOperations()
        self.mail.config = SimpleNamespace(email="employee@example.com")
        self.file = StreamingAttachment(name="报表.txt", attachment_id=AttachmentId(id="a1"), content_type="text/plain")

    def test_existing_save_flag_streams_and_retains_saved_path(self):
        with patch.object(self.mail, "_tool_item", return_value=SimpleNamespace(attachments=[self.file])):
            result = self.mail.get_attachment("m1", "a1", save=True)
        self.assertTrue(result["saved"])
        with open(result["saved_path"], "rb") as file:
            self.assertEqual(file.read(), b"attachment bytes")

    def test_prepare_uses_existing_scope_check_and_omits_server_path(self):
        with patch.object(self.mail, "_tool_item", return_value=SimpleNamespace(attachments=[self.file])) as scoped:
            result = self.mail.prepare_attachment_download("m1", "a1")
        scoped.assert_called_once_with("m1", only_fields=["attachments"])
        self.assertNotIn("saved_path", result)
        self.assertEqual(result["filename"], "报表.txt")
        self.assertIn(":7712/downloads/", result["download_url"])

    def test_wrong_attachment_or_denied_mail_does_not_save(self):
        for failure in (None, ToolOperationError("ITEM_OUT_OF_SCOPE", "denied")):
            with patch.object(self.mail, "_tool_item", return_value=SimpleNamespace(attachments=[self.file]), side_effect=failure), \
                    patch.object(self.mail, "_save_export") as save:
                with self.assertRaises(ToolOperationError):
                    self.mail.prepare_attachment_download("m1", "someone-elses-attachment")
                save.assert_not_called()

    def test_item_attachment_is_rejected(self):
        item = ItemAttachment(name="mail", attachment_id=AttachmentId(id="a1"))
        with patch.object(self.mail, "_tool_item", return_value=SimpleNamespace(attachments=[item])):
            with self.assertRaises(ToolOperationError) as cm:
                self.mail.prepare_attachment_download("m1", "a1")
        self.assertEqual(cm.exception.code, "ATTACHMENT_NOT_SUPPORTED")

    def test_disabled_download_does_not_fetch_mail(self):
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}), \
                patch.object(self.mail, "_tool_item") as scoped:
            with self.assertRaises(ToolOperationError) as cm:
                self.mail.prepare_attachment_download("m1", "a1")
        self.assertEqual(cm.exception.code, "DOWNLOAD_DISABLED")
        scoped.assert_not_called()
