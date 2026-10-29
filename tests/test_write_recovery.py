"""Recovery contracts with the real dispatcher, SQLite store and SDK drafts."""

import os
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Message
from exchangelib.errors import ErrorIrresolvableConflict

import mcp_server
import test_draft_attachments as draft_fixtures
from confirmation import OperationStore
from tool_support import ToolOperationError, is_definite_rejection


class WriteRecoveryTests(unittest.TestCase):
    def setUp(self):
        draft_fixtures.DraftAttachmentTests.setUp(self)
        for patcher in (
            patch.dict(os.environ, {
                'EWS_MCP_DATA_DIR': self.tmp.name,
                'EWS_MCP_PREVIEW_TTL_SECONDS': '1800',
                'EWS_MCP_UPLOAD_ENABLED': 'true',
                'EWS_MCP_PUBLIC_BASE_URL': 'http://host',
            }),
            patch.object(mcp_server, 'get_store', side_effect=lambda: self.store),
            patch.object(mcp_server, 'resolve_identity_by_lanid',
                         return_value=SimpleNamespace(email='employee@example.com')),
            patch.object(mcp_server.OutlookConfig, 'from_service_env', return_value=self.ops.config),
            patch.object(mcp_server, 'OutlookClient', return_value=self.ops),
            patch.object(mcp_server.tool_audit, 'logger', Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dispatch(self, tool='update_draft', **params):
        return mcp_server._dispatch(tool, {'lanid': 'employee', **params}, None).structured_content

    def preview_update(self):
        params = {'draft_id': 'draft', 'subject': 'Updated'}
        preview = self.dispatch(**params)
        self.assertEqual(preview['status'], 'pending', preview)
        return {**params, 'operation_id': preview['operation_id'], 'confirm_token': preview['confirm_token']}

    def restart(self):
        self.store = OperationStore(self.store.db_path)
        self.addCleanup(self.store.close)

    def save_successfully(self, item, **kwargs):
        item.changekey = 'v2'
        return item

    def test_definite_update_conflict_releases_draft_for_a_new_preview(self):
        params = self.preview_update()
        with patch.object(Message, 'save', side_effect=ErrorIrresolvableConflict('concurrent edit')):
            result = self.dispatch(**params)
        self.assertEqual(result['status'], 'failed', result)
        self.assertEqual(result['exchange_code'], 'ErrorIrresolvableConflict')
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)
        replacement = self.preview_update()
        with patch.object(Message, 'save', autospec=True, side_effect=self.save_successfully):
            result = self.dispatch(**replacement)
        self.assertEqual(result['status'], 'completed', result)

    def test_update_timeout_remains_unknown_and_keeps_draft_isolated_after_restart(self):
        params = self.preview_update()
        with patch.object(Message, 'save', side_effect=TimeoutError('response lost')):
            result = self.dispatch(**params)
        self.assertEqual(result['status'], 'unknown', result)
        self.restart()
        replay = self.dispatch(**params)
        self.assertEqual(replay['status'], 'unknown', replay)
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 1)
        replacement = self.preview_update()
        self.assertEqual(self.dispatch(**replacement)['error_code'], 'DRAFT_BUSY')

    def test_pending_preview_survives_restart_and_executes_once(self):
        params = self.preview_update()
        self.restart()
        self.assertEqual(self.store.get(params['operation_id'])['status'], 'pending')
        with patch.object(Message, 'save', autospec=True, side_effect=self.save_successfully) as save:
            completed = self.dispatch(**params)
            replay = self.dispatch(**params)
        self.assertEqual(completed['status'], 'completed', completed)
        self.assertEqual(replay['status'], 'completed', replay)
        self.assertEqual(save.call_count, 1)

    def test_expired_pending_preview_remains_unexecuted_after_restart(self):
        params = self.preview_update()
        with self.store.transaction() as db:
            db.execute("UPDATE operations SET created_at='2000-01-01T00:00:00+00:00' WHERE operation_id=?",
                       (params['operation_id'],))
        self.restart()
        with patch.object(Message, 'save', side_effect=AssertionError('expired write submitted')):
            result = self.dispatch(**params)
        self.assertEqual(result['error_code'], 'PREVIEW_EXPIRED', result)
        self.assertEqual(self.store.get(params['operation_id'])['status'], 'pending')

    def test_executing_update_still_becomes_unknown_after_restart(self):
        params = self.preview_update()
        self.store.begin_draft_confirm(params['operation_id'], params['confirm_token'],
            {'draft_id': 'draft', 'subject': 'Updated'}, mailbox='employee@example.com',
            lanid='employee', worker_id='worker')
        self.restart()
        self.assertEqual(self.store.get(params['operation_id'])['status'], 'unknown')
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 1)

    def test_pending_attachment_reference_still_protects_file_after_restart(self):
        upload_id = draft_fixtures.DraftAttachmentTests.ready(self, b'abc')
        params = {'draft_id': 'draft', 'upload_id': upload_id}
        preview = self.dispatch('attach_draft_attachment', **params)
        self.assertEqual(preview['status'], 'pending', preview)
        self.restart()
        record = self.store.get(preview['operation_id'])
        self.assertEqual(record['status'], 'pending')
        ref = self.store._conn.execute('SELECT kind FROM operation_upload_refs WHERE operation_id=?',
                                      (preview['operation_id'],)).fetchone()
        self.assertIsNotNone(ref)
        self.assertEqual(ref['kind'], 'preview')
        with self.store.transaction() as db:
            db.execute('UPDATE attachment_uploads SET retained_until=? WHERE upload_id=?', (time.time() - 1, upload_id))
        self.uploads.operations = self.store
        self.uploads.cleanup()
        self.assertTrue(self.uploads._path(upload_id).exists())
        execution = self.store.begin_draft_confirm(preview['operation_id'], preview['confirm_token'],
            {**params, 'is_inline': False}, mailbox='employee@example.com', lanid='employee', worker_id='worker')
        verified = self.uploads.open_verified(upload_id, lanid='employee', mailbox='employee@example.com',
                                              operation_id=execution.operation_id)
        self.assertEqual(verified.content, b'abc')

    def test_missing_move_receipt_is_an_unknown_outcome(self):
        self.assertFalse(is_definite_rejection(ToolOperationError('MOVE_RESULT_UNKNOWN', 'missing moved ID')))


if __name__ == '__main__':
    unittest.main()
