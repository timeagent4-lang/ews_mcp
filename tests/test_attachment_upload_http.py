import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import httpx
from fastmcp import FastMCP

from config import UploadSettings
from confirmation import OperationStore
from attachment_uploads import AttachmentUploadStore


class UploadHTTPTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from attachment_uploads import register_upload_route
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.operations = OperationStore(str(Path(self.tmp.name) / 'operations.db'))
        self.addCleanup(self.operations.close)
        self.store = AttachmentUploadStore(UploadSettings(root=self.tmp.name, enabled=True, base_url='http://host'), self.operations)
        self.server = FastMCP('upload-test')
        register_upload_route(self.server)
        self.patches = [patch('attachment_uploads.get_upload_store', return_value=self.store),
                        patch.dict(os.environ, {'EWS_MCP_UPLOAD_ENABLED': 'true'})]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.http_app()), base_url='http://host')
        self.addAsyncCleanup(self.client.aclose)

    def prepare(self, data=b'abc'):
        return self.store.prepare(lanid='employee', mailbox='employee@example.com', filename='a.txt',
                                  size=len(data), sha256=hashlib.sha256(data).hexdigest())

    def headers(self, row, **extra):
        return {'Authorization': 'Bearer ' + row['upload_token'], 'Content-Length': str(row['size']), **extra}

    async def test_put_authentication_and_length_contract(self):
        row = self.prepare()
        url = '/uploads/' + row['upload_id']
        self.assertEqual((await self.client.put(url, content=b'abc')).status_code, 403)
        self.assertEqual((await self.client.put(url, headers={'Authorization': 'Bearer wrong'}, content=b'abc')).status_code, 403)
        self.assertEqual((await self.client.post(url, content=b'abc')).status_code, 405)
        headers = [('Authorization', 'Bearer '+row['upload_token'])]*2
        self.assertEqual((await self.client.put(url, headers=headers, content=b'abc')).status_code, 403)
        self.assertEqual((await self.client.put(url, headers=self.headers(row, **{'Content-Length':'2'}), content=b'ab')).status_code, 400)
        with patch('attachment_uploads.time.time', return_value=99999999999):
            self.assertEqual((await self.client.put(url, headers=self.headers(row), content=b'abc')).status_code, 410)

    async def test_hash_length_ready_and_repeated_put(self):
        for payload, status in ((b'xyz', 422), (b'ab', 400), (b'abcd', 413), (b'abc', 200)):
            row = self.prepare()
            url = '/uploads/'+row['upload_id']
            response = await self.client.put(url, headers=self.headers(row), content=payload)
            self.assertEqual(response.status_code, status, response.text)
            if status == 200:
                self.assertEqual(response.json()['sha256'], hashlib.sha256(b'abc').hexdigest())
                self.assertEqual((await self.client.put(url, headers=self.headers(row), content=b'xyz')).status_code, 409)
                self.assertEqual(self.store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com').content, b'abc')
            else:
                with self.assertRaises(Exception):
                    self.store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com')

    async def test_missing_length_disconnect_and_timeout_never_publish(self):
        row = self.prepare()
        async def missing_length(): yield b'abc'
        response = await self.client.put('/uploads/'+row['upload_id'], headers={'Authorization':'Bearer '+row['upload_token']}, content=missing_length())
        self.assertEqual(response.status_code, 411)
        async def interrupted():
            yield b'a'
            raise httpx.ReadError('private content must not be logged')
        response = await self.client.put('/uploads/'+row['upload_id'], headers=self.headers(row), content=interrupted())
        self.assertEqual(response.status_code, 400)
        row = self.prepare()
        async def stalled():
            yield b'a'
            await asyncio.Event().wait()
        with patch('attachment_uploads.IDLE_SECONDS', .02):
            response = await self.client.put('/uploads/'+row['upload_id'], headers=self.headers(row), content=stalled())
        self.assertEqual(response.status_code, 408)
        with self.assertRaises(Exception): self.store.open_verified(row['upload_id'], lanid='employee', mailbox='employee@example.com')

    async def test_parallel_receive_limit_and_cleanup_protection(self):
        entered, release = asyncio.Event(), asyncio.Event()
        rows = [self.prepare() for _ in range(5)]
        count = 0
        async def body():
            nonlocal count
            count += 1
            if count == 4: entered.set()
            yield b'a'
            await release.wait()
            yield b'bc'
        pending = [asyncio.create_task(self.client.put('/uploads/'+r['upload_id'], headers=self.headers(r), content=body())) for r in rows[:4]]
        try:
            await asyncio.wait_for(entered.wait(), 3)
            self.assertEqual(self.store.claim_cleanup(now=99999999999)[0].upload_id, rows[4]['upload_id'])
            repeat = await self.client.put('/uploads/'+rows[0]['upload_id'], headers=self.headers(rows[0]), content=b'abc')
            self.assertEqual(repeat.status_code, 409)
            # Fifth record was claimed for cleanup above; use a fresh prepared record for capacity.
            fifth = self.prepare()
            self.assertEqual((await self.client.put('/uploads/'+fifth['upload_id'], headers=self.headers(fifth), content=b'abc')).status_code, 429)
        finally:
            release.set()
            responses = await asyncio.gather(*pending)
        self.assertTrue(all(r.status_code == 200 for r in responses), [r.text for r in responses])

    async def test_http_failure_log_excludes_token_bytes_and_disk_paths(self):
        row = self.prepare(b'PRIVATE_BYTES')
        with self.assertLogs('attachment_uploads', level='INFO') as logs:
            with patch('attachment_uploads.os.replace', side_effect=OSError('C:\\PRIVATE_PATH\\file')):
                result = await self.client.put('/uploads/'+row['upload_id'], headers=self.headers(row), content=b'PRIVATE_BYTES')
        self.assertEqual(result.status_code, 503)
        logged = '\n'.join(logs.output) + result.text
        for secret in (row['upload_token'], 'PRIVATE_BYTES', 'PRIVATE_PATH'):
            self.assertNotIn(secret, logged)

    async def test_slow_fsync_times_out_without_blocking_other_requests(self):
        row = self.prepare()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        fsync = os.fsync
        def slow_fsync(fd):
            loop.call_soon_threadsafe(entered.set)
            release.wait(1)
            fsync(fd)
        with patch('attachment_uploads.RECEIVE_SECONDS', .2), \
             patch('attachment_uploads.os.fsync', side_effect=slow_fsync):
            receiving = asyncio.create_task(self.client.put('/uploads/'+row['upload_id'], headers=self.headers(row), content=b'abc'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                other = await self.client.put('/uploads/'+row['upload_id'], content=b'abc')
                remained_active = not receiving.done()
                loop.call_later(.3, release.set)
                response = await asyncio.wait_for(receiving, 2)
            finally:
                release.set()
                await asyncio.gather(receiving, return_exceptions=True)
        self.assertEqual(response.status_code, 408, response.text)
        self.assertEqual(other.status_code, 403)
        self.assertTrue(remained_active, 'a different request must respond while disk I/O is still active')
        self.assertFalse((self.store.directory / (row['upload_id']+'.bin')).exists())
        self.assertFalse((self.store.directory / (row['upload_id']+'.part')).exists())

    async def test_cancelled_receive_waits_for_disk_work_before_cleanup(self):
        row = self.prepare()
        entered, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        def blocked_fsync(fd):
            loop.call_soon_threadsafe(entered.set)
            release.wait(1)
        with patch('attachment_uploads.os.fsync', side_effect=blocked_fsync):
            receiving = asyncio.create_task(self.client.put('/uploads/'+row['upload_id'], headers=self.headers(row), content=b'abc'))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                receiving.cancel()
                checkpoint = asyncio.Event()
                loop.call_later(.01, checkpoint.set)
                await checkpoint.wait()
                waited_for_disk = not receiving.done()
                protected = self.store.claim_cleanup(now=99999999999)
            finally:
                release.set()
                result = (await asyncio.gather(receiving, return_exceptions=True))[0]
        self.assertTrue(waited_for_disk)
        self.assertEqual(protected, [])
        self.assertIsInstance(result, asyncio.CancelledError)
        self.assertFalse((self.store.directory / (row['upload_id']+'.bin')).exists())
        self.assertFalse((self.store.directory / (row['upload_id']+'.part')).exists())

    async def test_lifespan_waits_for_active_cleanup_thread(self):
        from attachment_uploads import attachment_lifespan
        entered, tick, finished = asyncio.Event(), asyncio.Event(), threading.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        calls = 0
        def blocking_cleanup():
            nonlocal calls
            calls += 1
            if calls == 2:
                loop.call_soon_threadsafe(entered.set)
                release.wait(2)
                finished.set()
        async def controlled_sleep(delay):
            await tick.wait()
            tick.clear()
        with patch.dict(os.environ, {'EWS_MCP_DOWNLOAD_ENABLED':'false'}), \
             patch('attachment_uploads.asyncio.sleep', side_effect=controlled_sleep), \
             patch.object(self.store, 'cleanup', side_effect=blocking_cleanup):
            lifespan = attachment_lifespan(self.server)
            await lifespan.__aenter__()
            tick.set()
            await asyncio.wait_for(entered.wait(), 2)
            closing = asyncio.create_task(lifespan.__aexit__(None, None, None))
            try:
                checkpoint = asyncio.Event()
                loop.call_later(.02, checkpoint.set)
                await checkpoint.wait()
                waited_for_thread = not closing.done()
            finally:
                release.set()
                await asyncio.wait_for(closing, 2)
        self.assertTrue(waited_for_thread, 'lifespan ended with a live cleanup thread')
        self.assertTrue(finished.is_set())

    async def test_periodic_cleanup_runs_when_enabled_or_disabled(self):
        from attachment_uploads import attachment_lifespan
        for enabled in (False, True):
            now = [1000]
            sleeping, wake, cleaned = asyncio.Event(), asyncio.Event(), asyncio.Event()
            loop = asyncio.get_running_loop()
            cleanup = self.store.cleanup
            calls = 0
            def observed_cleanup():
                nonlocal calls
                cleanup()
                calls += 1
                if calls >= 2: loop.call_soon_threadsafe(cleaned.set)
            async def controlled_sleep(delay):
                if not sleeping.is_set():
                    sleeping.set()
                    await wake.wait()
                else:
                    await asyncio.Event().wait()
            with patch('attachment_uploads.time.time', side_effect=lambda:now[0]), \
                 patch.dict(os.environ, {'EWS_MCP_DATA_DIR':self.tmp.name, 'EWS_MCP_DOWNLOAD_ENABLED':'false',
                                         'EWS_MCP_UPLOAD_ENABLED':str(enabled).lower()}):
                row = self.prepare()
                handle = self.store.begin_receive(row['upload_id'], row['upload_token'], 3, worker_id='r')
                handle.path.write_bytes(b'abc')
                self.store.publish_ready(handle, size=3, sha256=hashlib.sha256(b'abc').hexdigest())
                with patch('attachment_uploads.asyncio.sleep', side_effect=controlled_sleep), \
                     patch.object(self.store, 'cleanup', side_effect=observed_cleanup):
                    async with attachment_lifespan(self.server):
                        await asyncio.wait_for(sleeping.wait(), 2)
                        self.assertTrue(handle.path.exists())
                        now[0] = 100000
                        wake.set()
                        await asyncio.wait_for(cleaned.wait(), 3)
                        self.assertFalse(handle.path.exists())
