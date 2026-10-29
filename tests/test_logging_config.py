"""Daily host logs and the actual noisy logger paths, without OA/Exchange I/O."""

import io
import gzip
import json
import logging
import os
from datetime import date
from pathlib import Path
import tempfile
import threading
import unittest
import warnings
from unittest.mock import patch

from utils.logging_config import DailyFileHandler, configure_logging


class LoggingConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        self.console = io.StringIO()
        self.names = ('', 'mail_mcp.audit', 'mcp.server.lowlevel.server',
                      'py.warnings', 'exchangelib.fields', 'httpx', 'httpcore',
                      'fastmcp', 'uvicorn', 'uvicorn.error')
        self.saved = {name: (list(logging.getLogger(name).handlers),
                            list(logging.getLogger(name).filters),
                            logging.getLogger(name).level, logging.getLogger(name).propagate)
                      for name in self.names}
        self.addCleanup(self.restore)
        self.warning_hook = warnings.showwarning
        self.warning_capture = logging._warnings_showwarning
        self.day = date(2026, 10, 26)
        p = patch('utils.logging_config.date', wraps=date)
        self.clock = p.start()
        self.clock.today.side_effect = lambda: self.day
        self.addCleanup(p.stop)

    def restore(self):
        warnings.showwarning = self.warning_hook
        logging._warnings_showwarning = self.warning_capture
        old_handlers = {h for values in self.saved.values() for h in values[0]}
        current = {h for name in self.names for h in logging.getLogger(name).handlers}
        for coordinator in {getattr(h, '_coordinator', None) for h in current - old_handlers} - {None}:
            coordinator.shutdown()
        for h in current - old_handlers:
            h.close()
        for name, (handlers, filters, level, propagate) in self.saved.items():
            logger = logging.getLogger(name)
            logger.handlers, logger.filters = handlers, filters
            logger.setLevel(level)
            logger.propagate = propagate

    def configure(self):
        with patch('utils.logging_config.sys.stderr', self.console):
            configure_logging(self.directory)

    def read(self, name):
        return (self.directory / name).read_text(encoding='utf-8')

    def wait_for_archive(self):
        coordinators = {getattr(h, '_coordinator', None)
                        for h in logging.getLogger().handlers}
        for coordinator in coordinators - {None}:
            self.assertTrue(coordinator.wait_for_archive(10), 'archive worker did not settle')

    def test_old_files_are_verified_gzip_archives_not_deleted_history(self):
        old = self.directory / 'audit-2026-09-26.log'
        original = ('{"subject":"archive me 中文"}\n' * 40).encode()
        old.write_bytes(original)
        boundary = self.directory / 'errors-2026-09-27.log'
        boundary.write_bytes(b'boundary\n')
        (self.directory / 'errors-2026-02-30.log').write_bytes(b'invalid date\n')
        self.configure()
        self.wait_for_archive()
        archive = self.directory / 'archive/2026-09/audit-2026-09-26.log.gz'
        self.assertTrue(archive.is_file(), 'expired history must be archived')
        self.assertEqual(gzip.decompress(archive.read_bytes()), original)
        self.assertFalse(old.exists())
        self.assertTrue(boundary.exists())
        self.assertTrue((self.directory / 'errors-2026-02-30.log').exists())

    def test_existing_corrupt_or_different_archive_preserves_both_sources(self):
        target = self.directory / 'archive/2026-09'
        target.mkdir(parents=True)
        for kind, archived in [('audit', b'not gzip'), ('errors', gzip.compress(b'other\n'))]:
            (self.directory / f'{kind}-2026-09-01.log').write_bytes(b'original\n')
            (target / f'{kind}-2026-09-01.log.gz').write_bytes(archived)
        self.configure()
        self.wait_for_archive()
        self.assertEqual((self.directory / 'audit-2026-09-01.log').read_bytes(), b'original\n')
        self.assertEqual((target / 'audit-2026-09-01.log.gz').read_bytes(), b'not gzip')
        self.assertEqual((self.directory / 'errors-2026-09-01.log').read_bytes(), b'original\n')
        self.assertIn('archive.conflict', self.console.getvalue())

    def test_archive_parent_symlink_preserves_source_and_current_logging(self):
        with tempfile.TemporaryDirectory() as destination:
            archive = self.directory / 'archive'
            try:
                archive.symlink_to(destination, target_is_directory=True)
            except OSError as exc:
                self.skipTest(f'symlink creation is unavailable: {type(exc).__name__}')
            source = self.directory / 'audit-2026-09-01.log'
            source.write_bytes(b'preserve source under log root\n')
            self.configure()
            self.wait_for_archive()
            self.assertTrue(source.exists(), 'archive directory symlinks must not migrate history')
            self.assertEqual(source.read_bytes(), b'preserve source under log root\n')
            self.assertEqual(list(Path(destination).iterdir()), [])
            self.assertIn('archive.failed', self.console.getvalue())
            from utils.audit import emit_event
            emit_event('test.after_archive_directory_rejection', is_error=True)
            self.assertIn('test.after_archive_directory_rejection', self.read('errors-2026-10-26.log'))

    def test_matching_published_archive_recovers_interrupted_source_cleanup(self):
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'complete source\n')
        target = self.directory / 'archive/2026-09/audit-2026-09-01.log.gz'
        target.parent.mkdir(parents=True)
        target.write_bytes(gzip.compress(b'complete source\n'))
        self.configure()
        self.wait_for_archive()
        self.assertFalse(source.exists())
        self.assertEqual(gzip.decompress(target.read_bytes()), b'complete source\n')

    def test_existing_archive_must_be_synced_before_source_cleanup(self):
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'complete source\n')
        target = self.directory / 'archive/2026-09/audit-2026-09-01.log.gz'
        target.parent.mkdir(parents=True)
        target.write_bytes(gzip.compress(b'complete source\n'))
        inode = target.stat().st_ino
        sync = os.fsync
        def fail_existing_archive_sync(fd):
            if os.fstat(fd).st_ino == inode:
                raise OSError('archive not durable')
            return sync(fd)
        with patch('utils.logging_config.os.fsync', side_effect=fail_existing_archive_sync):
            self.configure()
            self.wait_for_archive()
        self.assertTrue(source.exists(), 'an unsynced preexisting archive cannot replace the source')

    def test_archive_publish_failure_keeps_source_and_current_logging(self):
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'preserve me\n')
        with patch('utils.logging_config.os.link', side_effect=PermissionError('denied')):
            self.configure()
            self.wait_for_archive()
        self.assertEqual(source.read_bytes(), b'preserve me\n')
        self.assertFalse((self.directory / 'archive/2026-09/audit-2026-09-01.log.gz').exists())
        self.assertEqual(list(self.directory.glob('archive/**/*.tmp')), [])
        logging.getLogger('mcp_server').error('current logging still works')
        self.assertGreater((self.directory / 'errors-2026-10-26.log').stat().st_size, 0)
        self.assertIn('archive.failed', self.console.getvalue())

    def test_undurable_publish_keeps_source_then_restart_finishes_cleanup(self):
        from utils import logging_config
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'durable history\n')
        sync = logging_config._sync_directory
        def fail_archive_directory(path):
            if Path(path).name == '2026-09':
                raise OSError('directory sync failed')
            return sync(path)
        with patch.object(logging_config, '_sync_directory', side_effect=fail_archive_directory):
            self.configure()
            self.wait_for_archive()
        target = self.directory / 'archive/2026-09/audit-2026-09-01.log.gz'
        self.assertEqual(source.read_bytes(), b'durable history\n')
        self.assertEqual(gzip.decompress(target.read_bytes()), b'durable history\n')
        self.configure()
        self.wait_for_archive()
        self.assertFalse(source.exists())

    def test_source_changed_during_verification_is_not_unlinked(self):
        from utils import logging_config
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'original\n')
        digest = logging_config._digest
        def concurrent_append(stream):
            result = digest(stream)
            with source.open('ab') as output:
                output.write(b'late write\n')
            return result
        with patch.object(logging_config, '_digest', side_effect=concurrent_append):
            self.configure()
            self.wait_for_archive()
        self.assertEqual(source.read_bytes(), b'original\nlate write\n')
        self.assertFalse((self.directory / 'archive/2026-09/audit-2026-09-01.log.gz').exists())

    def test_compression_does_not_block_current_record_writes(self):
        from concurrent.futures import ThreadPoolExecutor
        from utils.audit import emit_event
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'compress outside writer lock\n')
        entered, release = threading.Event(), threading.Event()
        write = gzip.GzipFile.write
        def blocked_write(zipped, data):
            entered.set()
            if not release.wait(10):
                raise TimeoutError('test did not release compressor')
            return write(zipped, data)
        with patch.object(gzip.GzipFile, 'write', blocked_write):
            self.configure()
            try:
                self.assertTrue(entered.wait(5))
                with ThreadPoolExecutor(max_workers=1) as pool:
                    future = pool.submit(emit_event, 'test.during_archive', is_error=True)
                    try:
                        future.result(timeout=3)
                    finally:
                        release.set()
                self.assertIn('test.during_archive', self.read('errors-2026-10-26.log'))
            finally:
                release.set()
            self.wait_for_archive()

    def test_handler_close_under_logging_lock_does_not_join_archive_worker(self):
        from utils import logging_config
        source = self.directory / 'audit-2026-09-01.log'
        source.write_bytes(b'pending archive\n')
        entered, release, closed = threading.Event(), threading.Event(), threading.Event()
        def pending_archive(coordinator, path, stamp):
            entered.set()
            release.wait(10)
        with patch.object(logging_config._ArchiveCoordinator, '_archive_one', pending_archive):
            self.configure()
            self.assertTrue(entered.wait(5))
            handlers = [h for h in logging.getLogger().handlers if isinstance(h, DailyFileHandler)]
            coordinator = handlers[0]._coordinator
            def close_like_logging_shutdown():
                with coordinator.lock:
                    for handler in handlers:
                        handler.close()
                closed.set()
            thread = threading.Thread(target=close_like_logging_shutdown, daemon=True)
            thread.start()
            try:
                returned_without_worker = closed.wait(1)
            finally:
                release.set()
                thread.join(5)
                coordinator.shutdown()
            self.assertTrue(returned_without_worker,
                            'logging.shutdown holds the handler lock; joining a worker there can deadlock')

    def test_partial_write_recovery_preserves_bytes_and_next_event_boundary(self):
        from utils.audit import emit_event
        self.configure()
        handler = next(h for h in logging.getLogger().handlers
                       if isinstance(h, DailyFileHandler) and h.kind == 'audit')
        original_write = handler.stream.write
        def interrupted_write(message):
            original_write(message[:13])
            handler.stream.flush()
            raise OSError('disk interruption')
        with patch.object(handler.stream, 'write', side_effect=interrupted_write):
            emit_event('test.interrupted', status='before')
        emit_event('test.resumed', status='after')
        lines = self.read('audit-2026-10-26.log').splitlines()
        valid = []
        broken = []
        for line in lines:
            try:
                valid.append(json.loads(line))
            except json.JSONDecodeError:
                broken.append(line)
        self.assertEqual(len(broken), 1)
        self.assertTrue(any(row.get('event_type') == 'test.resumed' for row in valid),
                        'the first resumed event must not be appended onto the torn line')
        self.assertTrue(any(row.get('event_type') == 'logging.recovered' for row in valid))
        self.assertIn('logging.write_failed', self.console.getvalue())

    def test_first_new_day_event_closes_idle_other_handler_before_archiving(self):
        self.configure()
        logging.getLogger('mcp_server').error('old error')
        old = self.directory / 'errors-2026-10-26.log'
        original = old.read_bytes()
        self.day = date(2026, 11, 26)
        logging.getLogger('mail_mcp.audit').info('{"status":"success"}')
        self.wait_for_archive()
        self.assertTrue((self.directory / 'errors-2026-11-26.log').exists(),
                        'idle errors stream must rotate with the audit stream')
        target = self.directory / 'archive/2026-10/errors-2026-10-26.log.gz'
        self.assertEqual(gzip.decompress(target.read_bytes()), original)
        logging.getLogger('mcp_server').error('new error')
        self.assertNotIn('new error', gzip.decompress(target.read_bytes()).decode())

    def test_runtime_records_are_jsonl_and_share_failure_event_ids(self):
        self.configure()
        logging.getLogger('mcp_server').info('service ready')
        logging.getLogger('mcp_server').error('ordinary error')
        logging.getLogger('mcp_server').error('{"is_error":false,"status":"diagnostic"}')
        audit = [json.loads(line) for line in self.read('audit-2026-10-26.log').splitlines()]
        errors = [json.loads(line) for line in self.read('errors-2026-10-26.log').splitlines()]
        self.assertGreaterEqual(len(audit), 2)
        self.assertTrue(all(row['schema_version'] == 2 for row in audit + errors))
        self.assertEqual({row['event_id'] for row in errors},
                         {row['event_id'] for row in audit if row.get('is_error')})

    def test_unterminated_tail_preserves_bytes_and_reports_recovery(self):
        broken = b'{"event_id":"interrupted"'
        path = self.directory / 'audit-2026-10-26.log'
        path.write_bytes(broken)
        self.configure()
        logging.getLogger('mcp_server').info('service ready')
        output = path.read_bytes()
        self.assertTrue(output.startswith(broken + b'\n'))
        records = [json.loads(line) for line in output.splitlines()[1:]]
        self.assertTrue(any(row.get('event_type') == 'logging.recovered' for row in records))

    @unittest.skipUnless(os.name == 'posix', 'POSIX ownership/mode contract')
    def test_new_files_are_group_readable_despite_restrictive_umask(self):
        old_mask = os.umask(0o077)
        try:
            (self.directory / 'audit-2026-09-01.log').write_bytes(b'old\n')
            self.configure()
            self.wait_for_archive()
        finally:
            os.umask(old_mask)
        for name in ['audit-2026-10-26.log', 'errors-2026-10-26.log',
                     'archive/2026-09/audit-2026-09-01.log.gz']:
            self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o640)

    @unittest.skipUnless(os.name == 'posix', 'POSIX inherited shared group contract')
    def test_archive_subdirectories_keep_shared_group_and_setgid(self):
        root_gid = self.directory.stat().st_gid
        os.chmod(self.directory, 0o2750)
        old_mask = os.umask(0o027)
        try:
            (self.directory / 'audit-2026-09-01.log').write_bytes(b'shared history\n')
            self.configure()
            self.wait_for_archive()
        finally:
            os.umask(old_mask)
        for name in ['archive', 'archive/2026-09']:
            stat = (self.directory / name).stat()
            self.assertEqual(stat.st_gid, root_gid)
            self.assertEqual(stat.st_mode & 0o2777, 0o2750)

    def test_errors_and_failed_audit_persist_but_console_stays_short(self):
        from utils.audit import _emit_audit_record, _snapshot_arguments
        from datetime import datetime, timezone
        import time
        self.configure()
        logger = logging.getLogger('mcp_server')
        logger.info('service ready')
        try:
            raise ValueError('diagnostic failure')
        except ValueError:
            logger.exception('test failure')
        for status in ('success', 'failure', 'rejected', 'error', 'processing'):
            _emit_audit_record(logging.getLogger('mail_mcp.audit'), tool_name='list_events',
                arguments=_snapshot_arguments({'lanid':'employee','body':'PRIVATE_BODY','confirm_token':'PRIVATE_TOKEN'}),
                assurance='oa_lanid_resolved',status=status,error_code='TEST_ERROR' if status in ('failure','rejected','error') else None,
                result_count=None,event_id='event-'+status,started_at=datetime.now(timezone.utc),started=time.perf_counter())
        errors = self.read('errors-2026-10-26.log')
        audits = self.read('audit-2026-10-26.log')
        self.assertIn('ValueError', errors)
        self.assertIn('frames', errors)
        self.assertNotIn('diagnostic failure', errors)
        self.assertNotIn('service ready', errors)
        records = [json.loads(line) for line in audits.splitlines()
                   if json.loads(line).get('tool') == 'list_events']
        self.assertEqual([r['status'] for r in records], ['success','failure','rejected','error','processing'])
        for record in records:
            self.assertEqual(record['event_id'] in errors, record['is_error'])
            self.assertIn(record['event_id'], self.console.getvalue())
        self.assertNotIn('input_arguments', self.console.getvalue())
        self.assertNotIn('Traceback', self.console.getvalue())
        for output in (errors, audits, self.console.getvalue()):
            self.assertNotIn('PRIVATE_BODY', output)
            self.assertNotIn('PRIVATE_TOKEN', output)

    def test_midnight_restart_and_calendar_retention_preserve_other_files(self):
        for name in ('errors-2026-09-26.log','audit-2026-09-26.log',
                     'errors-2026-09-27.log','audit-2026-09-27.log',
                     'notes-2026-09-01.log','errors-not-a-date.log','audit-2026-02-30.log'):
            (self.directory / name).write_text('old\n',encoding='utf-8')
        (self.directory/'errors-2026-09-01.log').mkdir()
        self.configure()
        self.wait_for_archive()
        self.assertFalse((self.directory/'errors-2026-09-26.log').exists())
        self.assertFalse((self.directory/'audit-2026-09-26.log').exists())
        self.assertTrue((self.directory/'errors-2026-09-27.log').exists())
        from utils.audit import emit_event
        emit_event('test.day_one', is_error=True)
        self.day = date(2026,10,27)
        # Even if there are no errors today, the next audit prunes old error files.
        logging.getLogger('mail_mcp.audit').info('{"status":"success"}')
        self.wait_for_archive()
        self.assertFalse((self.directory/'errors-2026-09-27.log').exists())
        self.assertFalse((self.directory/'audit-2026-09-27.log').exists())
        emit_event('test.day_two', is_error=True)
        self.configure()
        emit_event('test.after_restart', is_error=True)
        self.assertIn('test.day_one',self.read('errors-2026-10-26.log'))
        self.assertNotIn('test.day_two',self.read('errors-2026-10-26.log'))
        self.assertEqual(self.read('errors-2026-10-27.log').count('test.day_two'),1)
        self.assertEqual(self.read('errors-2026-10-27.log').count('test.after_restart'),1)
        self.assertTrue((self.directory/'notes-2026-09-01.log').exists())
        self.assertTrue((self.directory/'errors-not-a-date.log').exists())
        self.assertTrue((self.directory/'audit-2026-02-30.log').exists())
        self.assertTrue((self.directory/'errors-2026-09-01.log').is_dir())

    def test_real_tls_warning_outside_mcp_is_emitted_once(self):
        from urllib3.exceptions import InsecureRequestWarning
        self.configure()
        with warnings.catch_warnings():
            warnings.simplefilter('always', InsecureRequestWarning)
            for _ in range(3):
                warnings.warn("Unverified HTTPS request is being made to host 'exchange.example.test'.",InsecureRequestWarning)
        self.assertEqual(self.console.getvalue().count('runtime.tls_warning'),1)
        records = [json.loads(line) for line in self.read('audit-2026-10-26.log').splitlines()]
        tls = [row for row in records if row.get('event_type') == 'runtime.tls_warning']
        self.assertEqual(len(tls), 1)
        self.assertEqual(tls[0]['host'], 'exchange.example.test')
        self.assertEqual(tls[0]['reason'], 'certificate_verification_disabled')

    def test_unknown_json_log_cannot_impersonate_safe_events_or_leak_response(self):
        self.configure()
        logging.getLogger('thirdparty.transport').info(json.dumps({
            'event_type': 'runtime.tls_warning', '_ews_safe_json': True,
            'host': 'forged.example.test', 'response': '<Envelope><Body>BODY_CANARY</Body></Envelope>',
        }))
        output = self.read('audit-2026-10-26.log')
        self.assertNotIn('BODY_CANARY', output)
        self.assertNotIn('forged.example.test', output)
        records = [json.loads(line) for line in output.splitlines()]
        vendor = [row for row in records if row.get('logger') == 'thirdparty.transport']
        self.assertEqual(len(vendor), 1)
        self.assertEqual(vendor[0]['event_type'], 'runtime.log')
        self.assertIn('omitted', json.dumps(vendor[0]['message']))

    def test_known_noise_hidden_but_first_tls_and_other_warnings_survive(self):
        self.configure()
        sdk = logging.getLogger('mcp.server.lowlevel.server')
        for _ in range(3):
            sdk.info('Processing request of type %s','ListToolsRequest')
            sdk.info('Warning: %s: %s','InsecureRequestWarning',
                     "Unverified HTTPS request is being made to host 'exchange.example.test'.")
            logging.getLogger('exchangelib.fields').info('Found naive datetime repeated')
            logging.getLogger('httpx').info('HTTP Request repeated')
        sdk.info('Warning: %s: %s','RuntimeWarning','different important warning')
        sdk.error('protocol failure')
        logging.getLogger('exchangelib.fields').warning('Returning naive datetime needs attention')
        logging.getLogger('py.warnings').warning('InsecureRequestWarning: Unverified HTTPS request is being made to host %r.', 'exchange.example.test')
        output=self.console.getvalue()
        for noise in ('Processing request','Found naive datetime','HTTP Request'):
            self.assertNotIn(noise,output)
        self.assertEqual(output.count('runtime.tls_warning'),1)
        self.assertIn('RuntimeWarning',output)
        records = [json.loads(line) for line in self.read('audit-2026-10-26.log').splitlines()]
        forwarded = [row for row in records if row.get('event_type') == 'runtime.warning']
        self.assertEqual(len(forwarded), 1)
        self.assertEqual(forwarded[0]['warning_category'], 'RuntimeWarning')
        self.assertEqual(forwarded[0]['reason'], 'untrusted_warning_text_omitted')
        self.assertTrue(any(row.get('level') == 'WARNING' and row.get('logger') == 'exchangelib.fields' for row in records))
        errors = [json.loads(line) for line in self.read('errors-2026-10-26.log').splitlines()]
        self.assertTrue(any(row.get('logger') == 'mcp.server.lowlevel.server' for row in errors))

    def test_reconfiguration_and_concurrent_errors_are_written_once(self):
        from concurrent.futures import ThreadPoolExecutor
        from utils.audit import emit_event
        self.configure()
        self.configure()
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n:emit_event('test.concurrent', is_error=True, item=n),range(20)))
        output=[json.loads(line) for line in self.read('errors-2026-10-26.log').splitlines()]
        self.assertEqual(sorted(row['item'] for row in output), list(range(20)))
        self.assertEqual(len({row['event_id'] for row in output}), 20)

    def test_midnight_open_failure_can_recover_without_restarting(self):
        from utils.audit import emit_event
        self.configure()
        handler=next(h for h in logging.getLogger().handlers if isinstance(h,DailyFileHandler))
        logger=logging.getLogger('mcp_server')
        logger.error('previous day')
        self.day=date(2026,10,27)
        original_open=handler._open
        calls=0
        def temporary_failure():
            nonlocal calls
            calls+=1
            if calls==1: raise PermissionError('temporary mount failure')
            return original_open()
        try:
            with patch.object(handler,'_open',side_effect=temporary_failure), patch('sys.stderr',io.StringIO()):
                logger.error('temporary failure')
                emit_event('test.permissions_restored', is_error=True)
            self.assertTrue((self.directory/'errors-2026-10-27.log').exists())
            self.assertIn('test.permissions_restored',self.read('errors-2026-10-27.log'))
            self.assertIn('logging.write_failed', self.console.getvalue())
        finally:
            if handler.stream is not None and handler.stream.closed: handler.stream=None

    def test_tls_deduplication_never_hides_errors_or_other_warning_categories(self):
        self.configure()
        sdk=logging.getLogger('mcp.server.lowlevel.server')
        message="Unverified HTTPS request is being made to host 'exchange.example.test'."
        sdk.info('Warning: %s: %s','InsecureRequestWarning',message)
        sdk.error('transport failure: InsecureRequestWarning: %s',message)
        sdk.info('Warning: %s: %s','RuntimeWarning','check InsecureRequestWarning: '+message)
        errors = [json.loads(line) for line in self.read('errors-2026-10-26.log').splitlines()]
        self.assertTrue(any(row.get('logger') == 'mcp.server.lowlevel.server' and row.get('level') == 'ERROR' for row in errors))
        self.assertIn('RuntimeWarning',self.console.getvalue())

    def test_unwritable_directory_fails_initialization(self):
        with patch.object(DailyFileHandler,'_open',side_effect=PermissionError('denied')):
            with self.assertRaises(PermissionError): self.configure()

    def test_real_mcp_failure_keeps_redacted_audit_and_short_console(self):
        import asyncio
        from fastmcp import FastMCP, Client
        from fastmcp.tools.tool import ToolResult
        from utils.audit import ToolAuditMiddleware
        self.configure()
        server=FastMCP('logging-test')
        server.add_middleware(ToolAuditMiddleware())
        @server.tool()
        def denied(body: str, confirm_token: str):
            return ToolResult(content='denied',structured_content={'ok':False,'error_code':'DENIED'})
        async def invoke():
            async with Client(server) as client:
                await client.list_tools()
                await client.list_tools()
                await client.call_tool('denied',{'body':'PRIVATE_CONTENT','confirm_token':'PRIVATE_CREDENTIAL'},raise_on_error=False)
        asyncio.run(invoke())
        audit=self.read('audit-2026-10-26.log')
        errors=self.read('errors-2026-10-26.log')
        records = [json.loads(line) for line in audit.splitlines()]
        tool_events = [row for row in records if row.get('tool') == 'denied']
        self.assertEqual([row['event_type'] for row in tool_events], ['tool.started', 'tool.finished'])
        self.assertEqual(len({row['call_id'] for row in tool_events}), 1)
        self.assertIn('DENIED',errors)
        self.assertIn('DENIED',self.console.getvalue())
        self.assertNotIn('input_arguments',self.console.getvalue())
        self.assertNotIn('Processing request',self.console.getvalue())
        for output in (audit,errors,self.console.getvalue()):
            self.assertNotIn('PRIVATE_CONTENT',output)
            self.assertNotIn('PRIVATE_CREDENTIAL',output)

    def test_wrapped_exchange_cause_is_available_in_error_file(self):
        import mcp_server
        from exchangelib.errors import ErrorAccessDenied
        from tool_support import ToolOperationError
        self.configure()
        try:
            try:
                raise ErrorAccessDenied('denied for person@example.test')
            except ErrorAccessDenied as cause:
                raise ToolOperationError('DRAFT_UPDATE_FAILED','business wrapper') from cause
        except ToolOperationError as exc:
            mcp_server._tool_failure('update_draft',exc=exc)
        output=self.read('errors-2026-10-26.log')
        self.assertIn('ErrorAccessDenied',output)
        self.assertIn('DRAFT_UPDATE_FAILED',output)
        self.assertNotIn('person@example.test',output)
