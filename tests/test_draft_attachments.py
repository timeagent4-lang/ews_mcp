import base64
import hashlib
import io
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from PIL import Image
from exchangelib import Account, FileAttachment, HTMLBody, Mailbox, Message
from exchangelib.attachments import AttachmentId
from exchangelib.folders import Drafts
from exchangelib.properties import ParentFolderId
from exchangelib.services import CreateAttachment
from exchangelib.version import Build, Version

from attachment_uploads import AttachmentUploadStore
from config import UploadSettings
from confirmation import OperationStore
from tool_support import ToolOperationError, is_definite_rejection
from write_operations import WriteOperations


class DraftAttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OperationStore(str(Path(self.tmp.name) / 'operations.db'))
        self.addCleanup(self.store.close)
        self.uploads = AttachmentUploadStore(UploadSettings(root=self.tmp.name, enabled=True, base_url='http://host'), self.store)
        account = Mock(spec=Account)
        account.version = Version(build=Build(15, 2, 0, 0))
        account.protocol = Mock(version=account.version)
        account.primary_smtp_address = 'employee@example.com'
        self.ops = WriteOperations()
        self.ops.config = SimpleNamespace(email='employee@example.com')
        self.ops.account = account
        self.ops.requester_lanid = 'employee'
        self.ops.attachment_uploads = self.uploads
        self.folder = Drafts(id='drafts', changekey='folder-key')
        self.draft = Message(account=account, id='draft', changekey='v1',
            parent_folder_id=ParentFolderId(id='drafts'), is_draft=True,
            author=Mailbox(email_address='employee@example.com'), subject='Original',
            body=HTMLBody('<p>Original</p>'), to_recipients=[Mailbox(email_address='to@example.com')],
            cc_recipients=[Mailbox(email_address='cc@example.com')], bcc_recipients=[Mailbox(email_address='bcc@example.com')])
        self.ops._tool_item = Mock(return_value=self.draft)
        self.ops._tool_folder = Mock(return_value=self.folder)

    def ready(self, data):
        row = self.uploads.prepare(lanid='employee', mailbox='employee@example.com', filename='report.bin', size=len(data),
                                   sha256=hashlib.sha256(data).hexdigest())
        handle = self.uploads.begin_receive(row['upload_id'], row['upload_token'], len(data), worker_id='r')
        handle.path.write_bytes(data)
        self.uploads.publish_ready(handle, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return row['upload_id']

    def preview(self, upload_id, is_inline=False):
        return self.ops.attach_draft_attachment('draft', upload_id, is_inline=is_inline)['preview']['details']

    def execute(self, upload_id, details, is_inline=False):
        args = {'draft_id': 'draft', 'upload_id': upload_id, 'is_inline': is_inline}
        operation = self.store.create_attachment_preview(mailbox='employee@example.com', business_params=args, items=[],
            details=details, upload_snapshot=details['attachment'], canonical_draft_id='draft', expires_at=9999999999)
        execution = self.store.begin_draft_confirm(operation['operation_id'], operation['confirm_token'], args,
            mailbox='employee@example.com', lanid='employee', worker_id='w')
        self.ops.attachment_operation_id = execution.operation_id
        return self.ops.attach_draft_attachment(**args, confirm=True, expected_version=details['version'], expected_attachment=details['attachment'])

    def image(self, format='PNG', **kw):
        stream = io.BytesIO()
        Image.new('RGB', (2, 2), 'red').save(stream, format=format, **kw)
        return stream.getvalue()

    def test_attach_serializes_verified_bytes_and_preserves_existing_draft(self):
        old = FileAttachment(name='old.png', size=3, content_id='old', is_inline=True, attachment_id=AttachmentId(id='old-att'))
        self.draft.attachments = [old]
        data = self.image()
        upload_id = self.ready(data)
        details = self.preview(upload_id, True)
        before = (self.draft.body, self.draft.to_recipients, self.draft.cc_recipients, self.draft.bcc_recipients)
        captured = []
        def exchange(service, *, parent_item, items):
            captured.append(service.get_payload(parent_item=parent_item, items=items))
            return FileAttachment(attachment_id=AttachmentId(id='new-att', root_id='draft', root_changekey='v2'))
        with patch.object(CreateAttachment, 'get', autospec=True, side_effect=exchange), patch.object(Message, 'save', side_effect=AssertionError('must not save whole draft')):
            result = self.execute(upload_id, details, True)
        self.assertEqual(len(captured), 1)
        xml = captured[0]
        ns = {'m': 'http://schemas.microsoft.com/exchange/services/2006/messages', 't':'http://schemas.microsoft.com/exchange/services/2006/types'}
        self.assertEqual(xml.find('m:ParentItemId', ns).get('Id'), 'draft')
        self.assertEqual(base64.b64decode(xml.find('.//t:Content', ns).text), data)
        self.assertEqual(xml.find('.//t:ContentId', ns).text, details['attachment']['content_id'])
        self.assertIn(xml.find('.//t:IsInline', ns).text, ('true', '1'))
        self.assertEqual(result['attachment_id'], 'new-att')
        self.assertEqual(result['changekey'], 'v2')
        self.assertEqual(result['content_id'], details['attachment']['content_id'])
        self.assertEqual(before, (self.draft.body, self.draft.to_recipients, self.draft.cc_recipients, self.draft.bcc_recipients))
        self.assertIs(self.draft.attachments[0], old)

    def test_inline_rejects_spoofed_truncated_or_multiframe_image(self):
        apng = io.BytesIO()
        Image.new('RGB', (2,2)).save(apng, format='PNG', save_all=True, append_images=[Image.new('RGB', (2,2), 'red')])
        for data in (b'not image', b'<svg></svg>', self.image()[:45], apng.getvalue()):
            with self.subTest(data=data[:10]):
                with self.assertRaises(ToolOperationError) as caught: self.preview(self.ready(data), True)
                self.assertEqual(caught.exception.code, 'INVALID_INLINE_IMAGE')
        for format in ('PNG', 'JPEG'):
            self.assertEqual(self.preview(self.ready(self.image(format)), True)['attachment']['content_type'], 'image/'+('jpeg' if format=='JPEG' else 'png'))
        self.assertFalse(self.preview(self.ready(b'PDF bytes'))['attachment']['is_inline'])

    def test_preview_never_writes_and_version_change_blocks_attachment(self):
        upload_id = self.ready(b'abc')
        with patch.object(CreateAttachment, 'get', side_effect=AssertionError('preview wrote')):
            details = self.preview(upload_id)
            self.draft.body = HTMLBody('<p>changed</p>')
            with self.assertRaises(ToolOperationError) as caught: self.execute(upload_id, details)
            self.assertEqual(caught.exception.code, 'DRAFT_CHANGED')

    def test_attachment_incomplete_receipt_is_unknown(self):
        upload_id = self.ready(b'abc')
        details = self.preview(upload_id)
        with patch.object(Message, 'attach', return_value=None):
            with self.assertRaises(ToolOperationError) as caught: self.execute(upload_id, details)
        self.assertEqual(caught.exception.code, 'ATTACHMENT_RECEIPT_INCOMPLETE')
        self.assertFalse(is_definite_rejection(caught.exception))

    def test_draft_limits_include_existing_attachments_with_unknown_sizes_rejected(self):
        for size in (None, 20 * 1024 * 1024):
            self.draft.attachments = [FileAttachment(name='old', size=size)]
            with self.assertRaises(ToolOperationError): self.preview(self.ready(b'abc'))

    def test_inline_dimension_and_pixel_limits_and_cid_read_metadata(self):
        from mail_operations import _attachment_info
        for dimensions in ((8193, 1), (4001, 4000)):
            stream = io.BytesIO()
            Image.new('L', dimensions).save(stream, format='PNG')
            with self.assertRaises(ToolOperationError) as caught:
                self.preview(self.ready(stream.getvalue()), True)
            self.assertEqual(caught.exception.code, 'INVALID_INLINE_IMAGE')
        self.assertEqual(_attachment_info(FileAttachment(name='logo', content_id='actual-cid'))['content_id'], 'actual-cid')

    def test_only_employee_ordinary_drafts_can_receive_attachment(self):
        from exchangelib import CalendarItem
        upload_id = self.ready(b'abc')
        for item in (CalendarItem(id='event'), Message(id='inbox', is_draft=False, parent_folder_id=ParentFolderId(id='inbox'))):
            self.ops._tool_item.return_value = item
            with self.assertRaises(ToolOperationError): self.preview(upload_id)

    def test_send_rejects_body_recipients_attachment_and_legacy_preview_changes(self):
        for mutation in ('body','to_recipients','cc_recipients','bcc_recipients','attachments','changekey'):
            details = self.ops.send_draft('draft')['preview']['details']
            original = getattr(self.draft, mutation)
            if mutation == 'body': value = HTMLBody('x'*2001+'changed')
            elif mutation == 'attachments': value = [FileAttachment(name='added', size=1)]
            elif mutation == 'changekey': value = 'different'
            else: value = [Mailbox(email_address='different@example.com')]
            setattr(self.draft, mutation, value)
            with patch.object(Message, 'send', side_effect=AssertionError('old preview sent')):
                with self.assertRaises(ToolOperationError) as caught:
                    self.ops.send_draft('draft', confirm=True, expected_version=details['version'])
                self.assertEqual(caught.exception.code, 'DRAFT_CHANGED')
            setattr(self.draft, mutation, original)
        with self.assertRaises(ToolOperationError) as caught: self.ops.send_draft('draft', confirm=True)
        self.assertEqual(caught.exception.code, 'PREVIEW_VERSION_REQUIRED')
