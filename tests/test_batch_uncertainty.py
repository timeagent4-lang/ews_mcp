"""A lost response after submission is not a safely retryable batch failure."""

import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Message
from exchangelib.errors import ErrorAccessDenied
from fastmcp import Client

from confirmation import OperationStore
import mcp_server
from write_operations import WriteOperations


class BatchUncertaintyTests(unittest.TestCase):
    def setUp(self):
        self.store = OperationStore(':memory:')
        self.addCleanup(self.store.close)
        self.ops = WriteOperations()
        self.ops.config = SimpleNamespace(email='employee@example.com')
        self.items = {name: Message(id=name, changekey='v1', subject=name, is_draft=False)
                      for name in ('saved', 'denied', 'lost')}
        self.ops._tool_item = lambda item_id, **kw: self.items[item_id]
        self.ops._tool_folder = lambda name: SimpleNamespace(id=name)
        self.submissions = []
        for patcher in (
            patch.object(mcp_server, 'get_store', return_value=self.store),
            patch.object(mcp_server, 'resolve_identity_by_lanid',
                         return_value=SimpleNamespace(email=self.ops.config.email)),
            patch.object(mcp_server, 'initialize_outlook_client', return_value=self.ops),
            patch.object(mcp_server.tool_audit, 'logger', Mock()),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    def dispatch(self, tool, **params):
        return mcp_server._dispatch(tool, {'lanid': 'employee', **params}, None).structured_content

    def confirm_args(self, tool, **business):
        preview = self.dispatch(tool, **business)
        self.assertEqual(preview['status'], 'pending', preview)
        return {**business, 'operation_id': preview['operation_id'], 'confirm_token': preview['confirm_token']}

    def save(self, item, **kwargs):
        if item.id == 'denied':
            raise ErrorAccessDenied('denied')
        self.submissions.append(item.id)
        item.changekey = 'v2'
        if item.id == 'lost':
            raise TimeoutError('response lost after save')

    def test_mixed_batch_unknown_survives_wire_query_and_replay_without_resubmission(self):
        async def run():
            business = {'ids': ['saved', 'denied', 'lost'], 'set_read': True}
            async with Client(mcp_server.mcp) as client:
                first = await client.call_tool('update_messages', {'lanid': 'employee', **business})
                preview = first.structured_content
                confirm = {**business, 'operation_id': preview['operation_id'],
                           'confirm_token': preview['confirm_token']}
                payloads = []
                for params in (confirm, {'operation_id': preview['operation_id']}, confirm):
                    response = await client.call_tool('update_messages', {'lanid': 'employee', **params},
                                                      raise_on_error=False)
                    self.assertTrue(response.is_error)
                    payload = response.structured_content
                    self.assertEqual(payload['status'], 'unknown', payload)
                    self.assertEqual((payload['success_count'], payload['failed_count'], payload['unknown_count']),
                                     (1, 1, 1))
                    self.assertIn('未知', payload['note'])
                    self.assertIn('仅确定未执行', payload['note'])
                    payloads.append(payload)
                self.assertEqual(payloads[0]['results'], payloads[1]['results'])
                self.assertEqual(payloads[0]['results'], payloads[2]['results'])
                rows = payloads[0]['results']['results']
                self.assertEqual([row['id'] for row in rows], ['saved', 'denied', 'lost'])
                self.assertEqual(rows[1]['status'], 'failed')
                self.assertEqual(rows[2]['status'], 'unknown')
        with patch.object(Message, 'save', autospec=True, side_effect=self.save):
            asyncio.run(run())
        self.assertEqual(self.submissions, ['saved', 'lost'])

    def test_lookup_timeout_before_submission_is_definite_failure(self):
        confirm = self.confirm_args('update_messages', ids=['saved'], set_read=True)
        with patch.object(self.ops, '_tool_item', side_effect=TimeoutError('read timed out')), \
                patch.object(Message, 'save', side_effect=AssertionError('must not submit')):
            receipt = self.dispatch('update_messages', **confirm)
        self.assertEqual(receipt['status'], 'failed', receipt)
        self.assertEqual(receipt['results']['results'][0].get('status'), 'failed')
        self.assertEqual(receipt.get('unknown_count'), 0)

    def test_move_applied_then_response_lost_is_unknown(self):
        confirm = self.confirm_args('move_messages', ids=['lost'], to_folder='sent')
        def move(item, **kwargs):
            self.submissions.append(item.id)
            item.id = 'new-id'
            raise TimeoutError('response lost after move')
        with patch.object(Message, 'move', autospec=True, side_effect=move):
            receipt = self.dispatch('move_messages', **confirm)
            replay = self.dispatch('move_messages', **confirm)
        self.assertEqual(receipt['status'], 'unknown', receipt)
        self.assertEqual(receipt['results'], replay['results'])
        self.assertEqual(receipt['unknown_count'], 1)
        self.assertEqual(self.submissions, ['lost'])
        self.assertEqual(self.items['lost'].id, 'new-id')

    def test_move_missing_result_id_is_unknown_even_without_transport_exception(self):
        confirm = self.confirm_args('move_messages', ids=['lost'], to_folder='sent')
        with patch.object(Message, 'move', autospec=True,
                          side_effect=lambda item, **kwargs: setattr(item, 'id', None)):
            receipt = self.dispatch('move_messages', **confirm)
        self.assertEqual(receipt['status'], 'unknown', receipt)
        self.assertEqual(receipt['results']['results'][0]['error_code'], 'MOVE_RESULT_UNKNOWN')


if __name__ == '__main__':
    unittest.main()
