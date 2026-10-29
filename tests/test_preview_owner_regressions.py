"""Public preview/owner regressions with real storage and HTTP uploads."""

import hashlib
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import httpx
from exchangelib import FileAttachment
from exchangelib.attachments import AttachmentId
from exchangelib.services import CreateAttachment
from fastmcp import Client

import mcp_server
import test_draft_attachments as fixtures


class PreviewOwnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        fixtures.DraftAttachmentTests.setUp(self)
        self.attachments_created = 0
        for patcher in (
            patch.dict(os.environ, {
                'EWS_MCP_DATA_DIR': self.tmp.name, 'EWS_MCP_UPLOAD_ENABLED': 'true',
                'EWS_MCP_DOWNLOAD_ENABLED': 'false', 'EWS_MCP_PUBLIC_BASE_URL': 'http://host',
            }),
            patch.object(mcp_server, 'get_store', return_value=self.store),
            patch('confirmation.get_store', return_value=self.store),
            patch.object(mcp_server, 'resolve_identity_by_lanid',
                         return_value=SimpleNamespace(email='employee@example.com')),
            patch.object(mcp_server.OutlookConfig, 'from_service_env', return_value=self.ops.config),
            patch.object(mcp_server, 'OutlookClient', return_value=self.ops),
            patch.object(mcp_server.tool_audit, 'logger', Mock()),
            patch.object(CreateAttachment, 'get', autospec=True, side_effect=self.create_attachment),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def create_attachment(self, service, *, parent_item, items):
        self.attachments_created += 1
        return FileAttachment(attachment_id=AttachmentId(
            id='attachment-' + str(self.attachments_created), root_id='draft',
            root_changekey='v' + str(self.attachments_created + 1)))

    async def call(self, client, tool, lanid='employee', **params):
        result = await client.call_tool(tool, {'lanid': lanid, **params}, raise_on_error=False)
        self.assertFalse(result.is_error, result.structured_content)
        return result.structured_content

    async def upload(self, client, lanid='employee', data=b'abc'):
        prepared = await self.call(client, 'prepare_attachment_upload', lanid,
                                   filename='fixture.bin', size=len(data),
                                   sha256=hashlib.sha256(data).hexdigest())
        row = prepared['results']
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=mcp_server.mcp.http_app()),
                                     base_url='http://host') as http:
            response = await http.put('/uploads/' + row['upload_id'], content=data,
                                      headers={'Authorization': 'Bearer ' + row['upload_token']})
        self.assertEqual(response.status_code, 200, response.text)
        return row['upload_id']

    async def test_case_and_domain_owner_survive_upload_confirm_and_receipt(self):
        async with Client(mcp_server.mcp) as client:
            for lanid in ('EMPLOYEE', r'DOMAIN\Employee'):
                with self.subTest(lanid=lanid):
                    upload_id = await self.upload(client, lanid)
                    mcp_server.resolve_identity_by_lanid.assert_called_with(lanid)
                    args = {'draft_id': 'draft', 'upload_id': upload_id}
                    preview = await self.call(client, 'attach_draft_attachment', lanid, **args)
                    completed = await self.call(client, 'attach_draft_attachment', 'employee', **args,
                                                operation_id=preview['operation_id'],
                                                confirm_token=preview['confirm_token'])
                    receipt = await self.call(client, 'attach_draft_attachment', lanid,
                                              operation_id=preview['operation_id'])
                    self.assertEqual(completed['status'], 'completed')
                    self.assertEqual(receipt['results'], completed['results'])
                    self.assertEqual(self.ops.requester_lanid, 'employee')
        self.assertEqual(self.attachments_created, 2)

    async def test_draft_preview_replay_preserves_original_shape_and_token(self):
        args = {'draft_id': 'draft', 'subject': 'Updated', 'idempotency_key': 'edit-key'}
        async with Client(mcp_server.mcp) as client:
            first = await self.call(client, 'update_draft', **args)
            self.assertEqual(first['preview']['preview']['items'][0]['draft_id'], 'draft')
            mcp_server.tool_audit.logger.reset_mock()
            with patch.object(self.ops, 'update_draft', side_effect=AssertionError('replayed business')):
                replay = await self.call(client, 'update_draft', **args)
        self.assertEqual(replay['preview'], first['preview'])
        self.assertEqual(replay['operation_id'], first['operation_id'])
        self.assertEqual(replay['confirm_token'], first['confirm_token'])
        events = [json.loads(call.args[0]) for call in mcp_server.tool_audit.logger.info.call_args_list]
        finished = next(event for event in events if event['event_type'] == 'tool.finished')
        self.assertEqual(finished.get('operation_id'), first['operation_id'])
        self.assertEqual(finished.get('operation_id_source'), 'verified')

    async def test_inline_preview_replay_preserves_cid_without_storage_fields(self):
        data = fixtures.DraftAttachmentTests.image(self)
        async with Client(mcp_server.mcp) as client:
            upload_id = await self.upload(client, data=data)
            args = {'draft_id': 'draft', 'upload_id': upload_id, 'is_inline': True,
                    'idempotency_key': 'inline-key'}
            first = await self.call(client, 'attach_draft_attachment', **args)
            details = first['preview']['preview']['details']
            self.assertTrue(details['attachment']['content_id'])
            self.assertNotIn('expires_at', details)
            with patch.object(self.ops, 'attach_draft_attachment', side_effect=AssertionError('replayed business')):
                replay = await self.call(client, 'attach_draft_attachment', **args)
        self.assertEqual(replay['preview'], first['preview'])
        self.assertEqual(replay['confirm_token'], first['confirm_token'])
        self.assertEqual(self.attachments_created, 0)


if __name__ == '__main__':
    unittest.main()
