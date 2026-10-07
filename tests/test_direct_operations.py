"""离线验证直接写入与 EWS 提交边界，仅模拟网络写入方法。"""

import importlib
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, PropertyMock, patch

from exchangelib import Account, Configuration, Credentials, FileAttachment, Folder, HTMLBody, Mailbox, Message
from exchangelib.transport import NTLM
from exchangelib.version import Build, Version
from exchangelib.services import DeleteItem
from exchangelib.errors import ErrorAccessDenied, ErrorCorruptData, ErrorInvalidChangeKey
from requests.exceptions import ReadTimeout

from tool_support import ToolOperationError


def offline_account(mailbox="user@example.com"):
    return Account(
        primary_smtp_address=mailbox, autodiscover=False,
        config=Configuration(server="offline.example.com", auth_type=NTLM,
                             credentials=Credentials("offline", "dummy"),
                             version=Version(build=Build(15, 1, 0, 0))),
    )


class DirectMailTests(unittest.TestCase):
    def setUp(self):
        try:
            module = importlib.import_module("write_operations")
        except ImportError as exc:
            self.fail(f"邮件业务模块应可独立加载：{exc}")
        self.ops = module.WriteOperations()
        self.ops.config = SimpleNamespace(email="user@example.com")
        self.ops.account = offline_account()
        self.folders = {name: Folder(id=name) for name in ("drafts", "sent", "inbox")}
        self.ops._tool_folder = Mock(side_effect=self.folders.__getitem__)
        self.message = Message(
            account=self.ops.account,
            id="m1", changekey="v1", is_draft=True,
            author=Mailbox(email_address=self.ops.config.email),
            parent_folder_id=Folder(id="drafts"),
            body="old", to_recipients=[Mailbox(email_address="recipient@example.com")],
        )
        self.ops._tool_item = Mock(return_value=self.message)

    def test_create_draft_directly_saves_from_target_mailbox(self):
        saved = Message(id="created", changekey="new-v")
        with patch.object(Message, "save", autospec=True, return_value=saved) as save:
            result = self.ops.create_draft(subject="subject", to_emails="recipient@example.com")
        self.assertTrue(result["created"])
        draft = save.call_args.args[0]
        self.assertEqual(draft.author.email_address, "user@example.com")
        self.assertEqual(draft.folder.id, "drafts")
        save.assert_called_once()

    def test_reply_preserves_html_type_and_native_reference(self):
        self.message.is_draft = False
        self.message.account = offline_account(self.ops.config.email)
        self.message.body = HTMLBody('<html><body><table><tr><td>old</td></tr></table></body></html>')
        self.message.author = Mailbox(email_address="other@example.com")
        saved = Message(id="reply", changekey="rv")
        with patch.object(type(self.message.create_reply(body="probe", subject="RE")), "save", autospec=True, return_value=saved) as save:
            result = self.ops.create_draft(mode="reply", reply_to="m1", body="<new>\nline")
        reply = save.call_args.args[0]
        self.assertIsInstance(reply.new_body, HTMLBody)
        self.assertEqual(str(reply.new_body), "&lt;new&gt;<br>line")
        self.assertEqual(reply.reference_item_id.id, "m1")
        self.assertEqual(reply.author.email_address, "user@example.com")
        self.assertEqual(result["id"], "reply")

    def test_html_prepend_keeps_original_markup_and_existing_inline_image(self):
        original = '<html><body style="x"><table><tr><td>old</td></tr></table><img src="cid:logo"></body></html>'
        self.message.body = HTMLBody(original)
        self.message.attachments = [FileAttachment(name="logo.png", content_id="logo", is_inline=True)]
        with patch.object(Message, "save") as save:
            self.ops.update_draft("m1", body='<p>new</p><img src="cid:logo">', body_format="html", body_action="prepend")
        self.assertEqual(str(self.message.body), original.replace('<body style="x">', '<body style="x"><p>new</p><img src="cid:logo">'))
        self.assertEqual(len(self.message.attachments), 1)
        save.assert_called_once_with(update_fields=["body"], conflict_resolution="NeverOverwrite")

    def test_missing_inline_reference_is_rejected_before_save(self):
        self.message.body = HTMLBody("<p>old</p>")
        with patch.object(Message, "save") as save:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.update_draft("m1", body='<img src="cid:missing">', body_format="html", body_action="prepend")
        self.assertEqual(cm.exception.code, "CID_ATTACHMENT_MISSING")
        save.assert_not_called()

    def test_send_directly_uses_target_sent_and_gate(self):
        with patch("write_operations.require_send") as gate, patch.object(Message, "send") as send:
            result = self.ops.send_draft("m1")
        self.assertTrue(result["sent"])
        send.assert_called_once_with(save_copy=True, copy_to_folder=self.folders["sent"])
        gate.assert_called_once()

    def test_sent_folder_lookup_failure_does_not_submit_send(self):
        self.ops._tool_folder.side_effect = lambda name: self.folders[name] if name != "sent" else (_ for _ in ()).throw(ReadTimeout("read"))
        with patch("write_operations.require_send"), patch.object(Message, "send") as send:
            with self.assertRaises(ReadTimeout):
                self.ops.send_draft("m1")
        send.assert_not_called()

    def test_send_timeout_is_unknown_and_not_retried(self):
        with patch("write_operations.require_send"), patch.object(Message, "send", side_effect=ReadTimeout("write")) as send:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.send_draft("m1")
        self.assertEqual(cm.exception.status, "unknown")
        self.assertIsInstance(cm.exception.__cause__, ReadTimeout)
        send.assert_called_once()

    def test_send_result_keeps_input_draft_id_when_sdk_clears_it(self):
        def complete_send(*args, **kwargs):
            self.message.id = None
        with patch("write_operations.require_send"), patch.object(Message, "send", side_effect=complete_send):
            result = self.ops.send_draft("m1")
        self.assertEqual(result["id"], "m1")

    def test_send_gate_blocks_before_submission(self):
        blocked = ToolOperationError("SEND_DISABLED", "blocked")
        with patch("write_operations.require_send", side_effect=blocked), patch.object(Message, "send") as send:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.send_draft("m1")
        self.assertEqual(cm.exception.status, "failed")
        send.assert_not_called()

    def test_sender_mismatch_is_rejected_without_public_fallback(self):
        self.message.author = Mailbox(email_address="public@example.com")
        with patch.object(Message, "send") as send:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.send_draft("m1")
        self.assertEqual(cm.exception.code, "DRAFT_AUTHOR_MISMATCH")
        send.assert_not_called()

    def test_send_rejection_is_failed_with_original_cause(self):
        with patch("write_operations.require_send"), patch.object(Message, "send", side_effect=ErrorAccessDenied("denied")) as send:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.send_draft("m1")
        self.assertEqual(cm.exception.status, "failed")
        self.assertIsInstance(cm.exception.__cause__, ErrorAccessDenied)
        send.assert_called_once()

    def test_draft_delete_only_moves_to_deleted_items(self):
        with patch.object(Account, "bulk_delete", return_value=[True]) as trash, patch.object(Message, "delete") as delete:
            result = self.ops.delete_draft("m1")
        self.assertTrue(result["deleted"])
        trash.assert_called_once_with(ids=[self.message], delete_type="MoveToDeletedItems", send_meeting_cancellations="SendToNone")
        delete.assert_not_called()
        self.assertEqual(result["delete_type"], "MoveToDeletedItems")

    def test_real_sdk_delete_success_is_not_reversed_by_trash_lookup_denial(self):
        self.message.account = offline_account()
        for tool in ("delete_draft", "delete_messages"):
            self.message.id = "m1"
            with self.subTest(tool=tool), patch.object(DeleteItem, "get", autospec=True, return_value=True), \
                    patch.object(DeleteItem, "call", autospec=True, return_value=iter([True])) as delete_call, \
                    patch.object(Account, "trash", new_callable=PropertyMock, side_effect=ErrorAccessDenied("trash denied")) as trash:
                try:
                    result = getattr(self.ops, tool)("m1" if tool == "delete_draft" else ["m1"])
                except ToolOperationError as exc:
                    self.fail(f"DeleteItem 已成功，不应因后续查文件夹变成 {exc.status}")
                if tool == "delete_messages":
                    self.assertTrue(result["results"][0]["success"])
                else:
                    self.assertTrue(result["deleted"])
                trash.assert_not_called()
                self.assertEqual(delete_call.call_args.kwargs["delete_type"], "MoveToDeletedItems")
                self.assertEqual(delete_call.call_args.kwargs["send_meeting_cancellations"], "SendToNone")

    def test_batch_delete_marks_read_failed_and_submitted_unknown_separately(self):
        self.ops._tool_item.side_effect = [ReadTimeout("read"), self.message]
        with patch.object(Account, "bulk_delete", side_effect=ReadTimeout("write")) as trash, patch.object(Message, "delete") as delete:
            result = self.ops.delete_messages(["read", "write"])
        self.assertEqual([r["status"] for r in result["results"]], ["failed", "unknown"])
        self.assertEqual([r["error_code"] for r in result["results"]], ["NETWORK_ERROR", "NETWORK_ERROR"])
        trash.assert_called_once()
        delete.assert_not_called()

    def test_batch_update_conflict_is_rejected_with_exchange_code(self):
        with patch.object(Message, "save", side_effect=ErrorInvalidChangeKey("changed")) as save:
            result = self.ops.update_messages(["m1"], set_read=True)
        row = result["results"][0]
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["error_code"], "VERSION_CONFLICT")
        self.assertEqual(row["exchange_code"], "ErrorInvalidChangeKey")
        save.assert_called_once_with(update_fields=["is_read"], conflict_resolution="NeverOverwrite")

    def test_batch_categories_preserves_unmentioned_categories(self):
        self.message.categories = ["existing", "remove"]
        with patch.object(Message, "save") as save:
            result = self.ops.update_messages(["m1"], categories_add=["new", "existing"], categories_remove=["remove"])
        self.assertEqual(self.message.categories, ["existing", "new"])
        self.assertEqual(result["results"][0]["status"], "success")
        save.assert_called_once_with(update_fields=["categories"], conflict_resolution="NeverOverwrite")

    def test_create_missing_saved_id_is_unknown(self):
        with patch.object(Message, "save", return_value=Message()):
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.create_draft()
        self.assertEqual(cm.exception.status, "unknown")

    def test_draft_native_field_validation_is_failed_before_save(self):
        with patch.object(Message, "save", side_effect=ValueError("subject too long")) as save:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.create_draft(subject="s" * 256)
        self.assertEqual(cm.exception.status, "failed")
        self.assertEqual(cm.exception.code, "INVALID_PARAMS")
        save.assert_not_called()

    def test_draft_update_native_field_validation_is_failed_before_save(self):
        with patch.object(Message, "save", side_effect=ValueError("subject too long")) as save:
            with self.assertRaises(ToolOperationError) as cm:
                self.ops.update_draft("m1", subject="s" * 256)
        self.assertEqual(cm.exception.status, "failed")
        save.assert_not_called()

    def test_move_missing_new_id_is_unknown(self):
        self.message.is_draft = False
        def clear_id(*args, **kwargs):
            self.message.id = None
        with patch.object(Message, "move", side_effect=clear_id):
            result = self.ops.move_messages(["m1"], to_folder="sent")
        self.assertEqual(result["results"][0]["status"], "unknown")


class DirectFlagTests(unittest.TestCase):
    def test_flag_list_preserves_yielded_exchange_error(self):
        ops = importlib.import_module("flag_operations").FlagOperations()
        fault = ErrorCorruptData("broken mail")
        folder = Mock()
        folder.filter.return_value.only.return_value.order_by.return_value = [fault]
        ops._tool_folder = Mock(return_value=folder)
        with self.assertRaises(Exception) as cm:
            ops.list_flagged_messages()
        self.assertIs(cm.exception, fault)

    def test_flag_timeout_is_unknown_after_one_save(self):
        try:
            ops = importlib.import_module("flag_operations").FlagOperations()
        except ImportError as exc:
            self.fail(f"旗标业务模块应可独立加载：{exc}")
        item = Message(id="m", changekey="v", subject="subject")
        ops._tool_item = Mock(return_value=item)
        with patch.object(Message, "save", side_effect=ReadTimeout("write")) as save:
            with self.assertRaises(ToolOperationError) as cm:
                ops.set_message_flag("m", "flagged")
        self.assertEqual(cm.exception.status, "unknown")
        self.assertEqual(item.flag_status, 2)
        self.assertEqual(save.call_args.kwargs["conflict_resolution"], "NeverOverwrite")
        save.assert_called_once()


if __name__ == "__main__":
    unittest.main()
