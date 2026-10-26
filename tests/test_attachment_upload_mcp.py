import asyncio
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import sqlite3
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from fastmcp import Client
from exchangelib import Message, FileAttachment
from exchangelib.attachments import AttachmentId
from exchangelib.services import CreateAttachment

import test_draft_attachments as draft_fixtures
import mcp_server


class AttachmentMCPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Reuse only the fixture initializer, not another imported TestCase's tests.
        draft_fixtures.DraftAttachmentTests.setUp(self)
        self.calls = 0
        self.audits = Mock()
        for p in (patch.dict(os.environ, {'EWS_MCP_DATA_DIR': self.tmp.name, 'EWS_MCP_UPLOAD_ENABLED':'true',
                    'EWS_MCP_DOWNLOAD_ENABLED':'false','EWS_MCP_PUBLIC_BASE_URL':'http://host','EWS_MCP_SEND_ENABLED':'true'}),
                  patch.object(mcp_server, 'get_store', return_value=self.store),
                  patch('confirmation.get_store', return_value=self.store),
                  patch.object(mcp_server, 'resolve_identity_by_lanid', return_value=SimpleNamespace(email='employee@example.com')),
                  patch.object(mcp_server.OutlookConfig, 'from_service_env', return_value=self.ops.config),
                  patch.object(mcp_server, 'OutlookClient', return_value=self.ops),
                  patch.object(mcp_server.tool_audit, 'logger', self.audits),
                  patch.object(CreateAttachment, 'get', autospec=True, side_effect=self.exchange)):
            p.start()
            self.addCleanup(p.stop)

    def exchange(self, service, *, parent_item, items):
        self.calls += 1
        return FileAttachment(attachment_id=AttachmentId(id='new-att', root_id='draft', root_changekey='v'+str(self.calls+1)))

    async def call(self, client, name, **kw):
        return await client.call_tool(name, {'lanid':'employee', **kw}, raise_on_error=False)

    async def upload(self, client, data=b'abc'):
        result = await self.call(client, 'prepare_attachment_upload', filename='report.txt', size=len(data), sha256=hashlib.sha256(data).hexdigest())
        self.assertFalse(result.is_error, result)
        row = result.structured_content['results']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.mcp.http_app()), base_url='http://host') as http:
            response = await http.put('/uploads/'+row['upload_id'], headers={'Authorization':'Bearer '+row['upload_token']}, content=data)
            self.assertEqual(response.status_code, 200, response.text)
        return row

    async def test_prepare_uses_oa_without_exchange_and_actual_schema(self):
        async with Client(mcp_server.mcp) as client:
            tools = await client.list_tools()
            self.assertEqual(len(tools), 30)
            self.assertIn('attach_draft_attachment', {t.name for t in tools})
            with patch.object(mcp_server, 'initialize_outlook_client', side_effect=AssertionError('prepare connected Exchange')):
                await self.upload(client)
            invalid = await self.call(client, 'attach_draft_attachment', draft_id='draft', upload_id='u', expected_attachment={})
            self.assertTrue(invalid.is_error)

    async def test_preview_confirm_receipt_and_replay_attach_once(self):
        async with Client(mcp_server.mcp) as client:
            row = await self.upload(client)
            args = {'draft_id':'draft','upload_id':row['upload_id']}
            preview = await self.call(client, 'attach_draft_attachment', **args)
            self.assertFalse(preview.is_error, preview)
            self.assertEqual(self.calls, 0)
            pending = preview.structured_content
            confirm = {**args,'operation_id':pending['operation_id'],'confirm_token':pending['confirm_token']}
            result = await self.call(client, 'attach_draft_attachment', **confirm)
            self.assertFalse(result.is_error, result)
            self.assertEqual(result.structured_content['status'], 'completed')
            self.assertEqual(result.structured_content['results']['attachment_id'], 'new-att')
            self.ops.attachment_uploads.cleanup()
            with patch.object(self.ops.attachment_uploads, 'open_verified', side_effect=AssertionError('receipt read file')):
                receipt = await self.call(client, 'attach_draft_attachment', operation_id=pending['operation_id'])
                replay = await self.call(client, 'attach_draft_attachment', **confirm)
            self.assertEqual(receipt.structured_content['status'], 'completed')
            self.assertEqual(replay.structured_content['status'], 'completed')
            self.assertEqual(self.calls, 1)

    async def test_oa_failure_and_cross_owner_cannot_prepare_or_attach(self):
        async with Client(mcp_server.mcp) as client:
            row = await self.upload(client)
            with patch.object(mcp_server, 'resolve_identity_by_lanid', side_effect=PermissionError('no')):
                result = await self.call(client, 'prepare_attachment_upload', filename='a', size=0, sha256=hashlib.sha256(b'').hexdigest())
                self.assertTrue(result.is_error)
            result = await client.call_tool('attach_draft_attachment', {'lanid':'other','draft_id':'draft','upload_id':row['upload_id']}, raise_on_error=False)
            self.assertTrue(result.is_error)
            self.assertEqual(self.calls, 0)

    async def test_send_preview_then_attachment_invalidates_old_confirmation(self):
        async with Client(mcp_server.mcp) as client:
            preview = await self.call(client, 'send_draft', draft_id='draft')
            self.assertFalse(preview.is_error, preview)
            pending = preview.structured_content
            self.draft.attachments = [FileAttachment(name='external', size=3)]
            with patch.object(Message, 'send', side_effect=AssertionError('stale send')):
                result = await self.call(client, 'send_draft', draft_id='draft', operation_id=pending['operation_id'], confirm_token=pending['confirm_token'])
            self.assertEqual(result.structured_content['error_code'], 'DRAFT_CHANGED')

    async def attach_preview(self, client, row, draft_id='draft', **extra):
        args = {'draft_id':draft_id, 'upload_id':row['upload_id'], **extra}
        preview = await self.call(client, 'attach_draft_attachment', **args)
        self.assertFalse(preview.is_error, preview)
        p = preview.structured_content
        return {**args,'operation_id':p['operation_id'],'confirm_token':p['confirm_token']}

    async def test_unknown_blocks_new_operation_and_replays_without_files(self):
        async with Client(mcp_server.mcp) as client:
            row = await self.upload(client)
            params = await self.attach_preview(client, row)
            with patch.object(CreateAttachment, 'get', side_effect=TimeoutError('lost result')) as boundary:
                result = await self.call(client, 'attach_draft_attachment', **params)
                self.assertEqual(result.structured_content['error_code'], 'ATTACHMENT_FAILED_OR_UNKNOWN')
                self.assertEqual(self.store.get(params['operation_id'])['status'], 'unknown')
                repeated = await self.call(client, 'attach_draft_attachment', **params)
                self.assertEqual(repeated.structured_content['status'], 'unknown')
                self.assertEqual(boundary.call_count, 1)
            second = await self.upload(client)
            another = await self.attach_preview(client, second)
            denied = await self.call(client, 'attach_draft_attachment', **another)
            self.assertEqual(denied.structured_content['error_code'], 'DRAFT_BUSY')
            for file in (Path(self.tmp.name)/'uploads').glob('*.bin'): file.unlink()
            receipt = await self.call(client, 'attach_draft_attachment', operation_id=params['operation_id'])
            self.assertEqual(receipt.structured_content['status'], 'unknown')
            other = await client.call_tool('attach_draft_attachment', {'lanid':'other','operation_id':params['operation_id']}, raise_on_error=False)
            self.assertEqual(other.structured_content['error_code'], 'OPERATION_SCOPE_MISMATCH')

    async def test_receipt_persistence_failure_does_not_release_execution(self):
        async with Client(mcp_server.mcp) as client:
            params = await self.attach_preview(client, await self.upload(client))
            with patch.object(self.store, 'finish_draft_operation', side_effect=sqlite3.OperationalError('disk full')):
                result = await self.call(client, 'attach_draft_attachment', **params)
            self.assertEqual(result.structured_content['error_code'], 'OPERATION_UNKNOWN')
            self.assertEqual(self.store.get(params['operation_id'])['status'], 'executing')
            self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 1)
            repeated = await self.call(client, 'attach_draft_attachment', **params)
            self.assertEqual(repeated.structured_content['status'], 'executing')
            self.assertEqual(self.calls, 1)

    async def test_different_tools_and_aliases_share_one_canonical_draft_claim(self):
        entered, release = threading.Event(), threading.Event()
        def blocking(**kw):
            entered.set()
            if not release.wait(3): raise TimeoutError('fixture not released')
            return self.exchange(None, **kw)
        async with Client(mcp_server.mcp) as client:
            params = await self.attach_preview(client, await self.upload(client), draft_id='alias')
            send = (await self.call(client, 'send_draft', draft_id='other-alias')).structured_content
            update = (await self.call(client, 'update_draft', draft_id='other-alias', subject='updated')).structured_content
            with patch.object(CreateAttachment, 'get', side_effect=blocking):
                first = asyncio.create_task(self.call(client, 'attach_draft_attachment', **params))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    repeated = await self.call(client, 'attach_draft_attachment', **params)
                    self.assertEqual(repeated.structured_content['status'], 'executing')
                    denied = await self.call(client, 'send_draft', draft_id='other-alias', operation_id=send['operation_id'], confirm_token=send['confirm_token'])
                    self.assertEqual(denied.structured_content['error_code'], 'DRAFT_BUSY')
                    denied = await self.call(client, 'update_draft', draft_id='other-alias', subject='updated', operation_id=update['operation_id'], confirm_token=update['confirm_token'])
                    self.assertEqual(denied.structured_content['error_code'], 'DRAFT_BUSY')
                    self.assertEqual(self.ops.attachment_uploads.claim_cleanup(now=99999999999), [])
                finally:
                    release.set()
                    result = await first
            self.assertEqual(result.structured_content['status'], 'completed')
            self.assertEqual(self.calls, 1)

    async def test_prepare_invalid_filename_is_redacted_before_validation(self):
        async with Client(mcp_server.mcp) as client:
            result = await self.call(client, 'prepare_attachment_upload', filename='C:\\SENSITIVE_DIR\\report.txt', size='bad', sha256='a'*64)
            self.assertTrue(result.is_error)
        output = '\n'.join(str(c.args) for c in self.audits.info.call_args_list)
        self.assertNotIn('SENSITIVE_DIR', output)

    async def test_same_idempotency_key_cannot_replay_a_different_upload(self):
        async with Client(mcp_server.mcp) as client:
            first, second = await self.upload(client), await self.upload(client)
            await self.attach_preview(client, first, idempotency_key='same')
            denied = await self.call(client, 'attach_draft_attachment', draft_id='draft', upload_id=second['upload_id'], idempotency_key='same')
            self.assertEqual(denied.structured_content['error_code'], 'CONFIRMATION_MISMATCH')
            self.assertEqual(self.calls, 0)

    async def test_completed_confirmation_replay_does_not_require_sdk_capacity(self):
        async with Client(mcp_server.mcp) as client:
            params = await self.attach_preview(client, await self.upload(client))
            completed = await self.call(client, 'attach_draft_attachment', **params)
            self.assertEqual(completed.structured_content['status'], 'completed')
            slots = mcp_server._ATTACHMENT_SLOTS
            slots.acquire()
            slots.acquire()
            try:
                repeated = await self.call(client, 'attach_draft_attachment', **params)
                self.assertEqual(repeated.structured_content.get('status'), 'completed')
            finally:
                slots.release()
                slots.release()
            self.assertEqual(self.calls, 1)

    async def test_sealed_bytes_changed_after_preview_never_reach_exchange(self):
        async with Client(mcp_server.mcp) as client:
            row = await self.upload(client)
            params = await self.attach_preview(client, row)
            (Path(self.tmp.name)/'uploads'/(row['upload_id']+'.bin')).write_bytes(b'xyz')
            result = await self.call(client, 'attach_draft_attachment', **params)
            self.assertEqual(result.structured_content['error_code'], 'UPLOAD_CHANGED')
            self.assertEqual(self.store.get(params['operation_id'])['status'], 'failed')
            self.assertEqual(self.calls, 0)

    async def test_old_send_preview_without_version_is_refused_through_mcp(self):
        old = self.store.create_preview(tool='send_draft', mailbox='employee@example.com', action='send_draft',
            business_params={'draft_id':'draft'}, details={'canonical_draft_id':'draft'})
        async with Client(mcp_server.mcp) as client:
            with patch.object(Message, 'send', side_effect=AssertionError('legacy preview sent')):
                result = await self.call(client, 'send_draft', draft_id='draft', operation_id=old['operation_id'], confirm_token=old['confirm_token'])
        self.assertEqual(result.structured_content['error_code'], 'PREVIEW_VERSION_REQUIRED')

