import hashlib
import builtins
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from confirmation import OperationStore, ConfirmationError
from tool_support import ToolOperationError
import config


class UploadStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.operations = OperationStore(str(Path(self.tmp.name) / 'operations.db'))
        self.addCleanup(self.operations.close)

    def store(self, **kw):
        from attachment_uploads import AttachmentUploadStore
        settings = config.UploadSettings(root=self.tmp.name, enabled=True,
                                         base_url='http://host:7712', **kw)
        return AttachmentUploadStore(settings, self.operations)

    def prepare(self, store, data=b'abc', **kw):
        return store.prepare(lanid='employee', mailbox='employee@example.com',
                             filename=kw.pop('filename', 'report.txt'), size=len(data),
                             sha256=hashlib.sha256(data).hexdigest(), **kw)

    def ready(self, store, data=b'abc'):
        row = self.prepare(store, data)
        handle = store.begin_receive(row['upload_id'], row['upload_token'], len(data), worker_id='receiver')
        with open(handle.part_path, 'xb') as output:
            output.write(data)
        os.replace(handle.part_path, handle.path)
        store.publish_ready(handle, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return row

    def preview(self, store, row, **kw):
        snapshot = store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com').metadata
        return self.operations.create_attachment_preview(
            mailbox='employee@example.com', business_params={'draft_id': 'd', 'upload_id': row['upload_id'], 'is_inline': False},
            items=[], details={'version': {'changekey': 'v'}, 'attachment': snapshot,
                               'canonical_draft_id': 'd', 'requester_lanid': 'employee'},
            upload_snapshot=snapshot, canonical_draft_id='d', expires_at=kw.get('expires_at', 9999999999),
        )

    def test_prepare_reserves_declared_bytes_and_zero_byte_file_slot(self):
        store = self.store(max_files=1, max_cache_bytes=10, max_bytes=10)
        row = self.prepare(store, b'', filename='C:\\private\\report.txt')
        self.assertEqual(row['filename'], 'report.txt')
        self.assertEqual(row['size'], 0)
        self.assertNotIn(row['upload_token'], str(list(self.operations._conn.execute('SELECT * FROM attachment_uploads'))))
        with self.assertRaises(ToolOperationError) as caught:
            self.prepare(store, b'')
        self.assertEqual(caught.exception.code, 'UPLOAD_CACHE_FULL')

    def test_token_is_bound_to_one_upload_and_ready_is_immutable(self):
        from attachment_uploads import UploadHTTPError
        store = self.store()
        first, second = self.prepare(store), self.prepare(store)
        with self.assertRaises(UploadHTTPError) as caught:
            store.begin_receive(second['upload_id'], first['upload_token'], 3, worker_id='r')
        self.assertEqual(caught.exception.status_code, 403)
        row = self.ready(store)
        with self.assertRaises(UploadHTTPError) as caught:
            store.begin_receive(row['upload_id'], row['upload_token'], 3, worker_id='r')
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com').content, b'abc')

    def test_owner_and_actual_digest_are_checked(self):
        store = self.store()
        row = self.ready(store)
        with self.assertRaises(ToolOperationError):
            store.open_verified(row['upload_id'], lanid='other', mailbox='employee@example.com')
        file = next((Path(self.tmp.name) / 'uploads').glob('*.bin'))
        file.write_bytes(b'xyz')
        with self.assertRaises(ToolOperationError) as caught:
            store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com')
        self.assertEqual(caught.exception.code, 'UPLOAD_CHANGED')

    def test_concurrent_quota_reservations_cannot_overbook(self):
        store = self.store(max_cache_bytes=3, max_bytes=3)
        results = []
        def reserve():
            try:
                self.prepare(store)
                results.append('ok')
            except ToolOperationError as exc:
                results.append(exc.code)
        threads = [threading.Thread(target=reserve) for _ in range(2)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertCountEqual(results, ['ok', 'UPLOAD_CACHE_FULL'])

    def test_preview_execution_and_quarantine_protect_file(self):
        store = self.store()
        row = self.ready(store)
        operation = self.preview(store, row)
        self.assertEqual(store.claim_cleanup(now=9999999998), [])
        execution = self.operations.begin_draft_confirm(operation['operation_id'], operation['confirm_token'],
            {'draft_id': 'd', 'upload_id': row['upload_id'], 'is_inline': False},
            mailbox='employee@example.com', lanid='employee', worker_id='w')
        self.assertEqual(store.claim_cleanup(now=99999999999), [])
        self.operations.finish_draft_operation(execution, 'unknown', {'error_code': 'ATTACHMENT_FAILED_OR_UNKNOWN'})
        self.assertEqual(store.claim_cleanup(now=99999999999), [])

    def test_failed_retry_expires_at_original_deadline_and_cannot_rebind(self):
        store = self.store()
        row = self.ready(store)
        operation = self.preview(store, row)
        args = {'draft_id': 'd', 'upload_id': row['upload_id'], 'is_inline': False}
        execution = self.operations.begin_draft_confirm(operation['operation_id'], operation['confirm_token'], args,
            mailbox='employee@example.com', lanid='employee', worker_id='w')
        self.operations.finish_draft_operation(execution, 'failed', {}, definitely_not_applied=True)
        with patch('confirmation.time.time', return_value=10000000000):
            with self.assertRaises(ConfirmationError) as caught:
                self.operations.begin_draft_confirm(operation['operation_id'], operation['confirm_token'], args,
                    mailbox='employee@example.com', lanid='employee', worker_id='w')
        self.assertEqual(caught.exception.code, 'PREVIEW_EXPIRED')
        with self.assertRaises(ToolOperationError):
            self.preview(store, row)

    def test_atomic_preview_rollback_leaves_no_operation_or_reference(self):
        store = self.store()
        row = self.ready(store)
        self.preview(store, row)
        count = self.operations._conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0]
        with self.assertRaises(ToolOperationError):
            self.preview(store, row)
        self.assertEqual(self.operations._conn.execute('SELECT COUNT(*) FROM operations').fetchone()[0], count)

    def test_restart_does_not_publish_interrupted_receive(self):
        store = self.store()
        row = self.prepare(store)
        handle = store.begin_receive(row['upload_id'], row['upload_token'], 3, worker_id='r')
        Path(handle.path).write_bytes(b'abc')
        other = OperationStore(self.operations.db_path)
        self.addCleanup(other.close)
        from attachment_uploads import AttachmentUploadStore
        restarted = AttachmentUploadStore(store.settings, other)
        with self.assertRaises(ToolOperationError):
            restarted.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com')
        for deleted in restarted.claim_cleanup(now=99999999999):
            Path(deleted.path).unlink(missing_ok=True)
            Path(deleted.part_path).unlink(missing_ok=True)
            restarted.finish_cleanup(deleted)
        self.assertFalse(Path(handle.path).exists())

    def test_delete_failure_keeps_reserved_quota_until_finish(self):
        store = self.store(max_files=1)
        self.ready(store)
        deletion = store.claim_cleanup(now=99999999999)[0]
        with self.assertRaises(ToolOperationError): self.prepare(store)
        Path(deletion.path).unlink()
        store.finish_cleanup(deletion)
        self.prepare(store)

    def test_completed_receipt_survives_file_cleanup(self):
        store = self.store()
        row = self.ready(store)
        operation = self.preview(store, row)
        args = {'draft_id': 'd', 'upload_id': row['upload_id'], 'is_inline': False}
        execution = self.operations.begin_draft_confirm(operation['operation_id'], operation['confirm_token'], args,
            mailbox='employee@example.com', lanid='employee', worker_id='w')
        self.operations.finish_draft_operation(execution, 'completed', {'attachment_id': 'a'})
        for deletion in store.claim_cleanup(now=99999999999):
            Path(deletion.path).unlink(missing_ok=True)
            store.finish_cleanup(deletion)
        self.assertEqual(self.operations.get(operation['operation_id'])['result'], {'attachment_id': 'a'})

    def test_different_drafts_are_independent_and_restart_preserves_unknown_fence(self):
        operations = []
        for draft in ('first', 'second'):
            record = self.operations.create_preview(tool='send_draft', mailbox='employee@example.com', action='send_draft',
                business_params={'draft_id':draft}, details={'canonical_draft_id':draft})
            execution = self.operations.begin_draft_confirm(record['operation_id'], record['confirm_token'], {'draft_id':draft},
                mailbox='employee@example.com', lanid='employee', worker_id=draft)
            operations.append((record, execution))
        self.assertEqual(self.operations._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 2)
        other = OperationStore(self.operations.db_path)
        self.addCleanup(other.close)
        self.assertTrue(all(other.get(record['operation_id'])['status'] == 'unknown' for record, _ in operations))
        self.assertEqual(other._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 2)

    def test_upload_settings_reject_unbounded_preview_and_invalid_limits(self):
        for extra in ({'preview_ttl_seconds':0}, {'base_url':'http://user:secret@host'}, {'max_bytes':-1}, {'max_cache_bytes':1}):
            with self.subTest(extra=extra):
                with self.assertRaises(ValueError):
                    config.UploadSettings(**{'root':self.tmp.name,'enabled':True,'base_url':'http://host',**extra})

    def test_actual_verification_read_is_pinned_against_cleanup(self):
        store = self.store()
        row = self.ready(store)
        entered, release = threading.Event(), threading.Event()
        results = []
        class Reader:
            def __init__(self, *args, **kw): self.source = builtins.open(*args, **kw)
            def __enter__(self): return self
            def __exit__(self, *args): self.source.close()
            def fileno(self): return self.source.fileno()
            def read(self, size):
                entered.set()
                if not release.wait(2): raise TimeoutError('test did not release reader')
                return self.source.read(size)
        def verify():
            results.append(store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com').content)
        with patch('attachment_uploads.open', side_effect=Reader, create=True):
            worker = threading.Thread(target=verify)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                self.assertEqual(store.claim_cleanup(now=99999999999), [])
            finally:
                release.set()
                worker.join(3)
        self.assertEqual(results, [b'abc'])
        self.assertEqual(len(store.claim_cleanup(now=99999999999)), 1)
