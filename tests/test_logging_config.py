"""Daily host logs and the actual noisy logger paths, without OA/Exchange I/O."""

import io
import json
import logging
from datetime import date
from pathlib import Path
import tempfile
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
        self.assertIn('Traceback', errors)
        self.assertIn('ValueError: diagnostic failure', errors)
        self.assertNotIn('service ready', errors)
        self.assertEqual(errors.count('test failure'), 1)
        records = [json.loads(line) for line in audits.splitlines()]
        self.assertEqual([r['status'] for r in records], ['success','failure','rejected','error','processing'])
        for status in ('failure','rejected','error'):
            self.assertIn('event-'+status, errors)
            self.assertIn('event-'+status, self.console.getvalue())
        self.assertNotIn('event-success', errors)
        self.assertNotIn('event-processing', errors)
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
        self.assertFalse((self.directory/'errors-2026-09-26.log').exists())
        self.assertFalse((self.directory/'audit-2026-09-26.log').exists())
        self.assertTrue((self.directory/'errors-2026-09-27.log').exists())
        logging.getLogger('mcp_server').error('day one')
        self.day = date(2026,10,27)
        # Even if there are no errors today, the next audit prunes old error files.
        logging.getLogger('mail_mcp.audit').info('{"status":"success"}')
        self.assertFalse((self.directory/'errors-2026-09-27.log').exists())
        self.assertFalse((self.directory/'audit-2026-09-27.log').exists())
        logging.getLogger('mcp_server').error('day two')
        self.configure()
        logging.getLogger('mcp_server').error('after restart')
        self.assertIn('day one',self.read('errors-2026-10-26.log'))
        self.assertNotIn('day two',self.read('errors-2026-10-26.log'))
        self.assertEqual(self.read('errors-2026-10-27.log').count('day two'),1)
        self.assertEqual(self.read('errors-2026-10-27.log').count('after restart'),1)
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
        self.assertEqual(self.console.getvalue().count('Unverified HTTPS request'),1)

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
        self.assertEqual(output.count('Unverified HTTPS request'),1)
        self.assertIn('different important warning',output)
        self.assertIn('protocol failure',output)
        self.assertIn('Returning naive datetime',output)
        self.assertIn('protocol failure',self.read('errors-2026-10-26.log'))

    def test_reconfiguration_and_concurrent_errors_are_written_once(self):
        from concurrent.futures import ThreadPoolExecutor
        self.configure()
        self.configure()
        logger=logging.getLogger('mcp_server')
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda n:logger.error('failure-%02d',n),range(20)))
        output=self.read('errors-2026-10-26.log')
        for n in range(20): self.assertEqual(output.count('failure-%02d'%n),1)

    def test_midnight_open_failure_can_recover_without_restarting(self):
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
                logger.error('recovered after permissions restored')
            self.assertTrue((self.directory/'errors-2026-10-27.log').exists())
            self.assertIn('recovered after permissions restored',self.read('errors-2026-10-27.log'))
        finally:
            if handler.stream is not None and handler.stream.closed: handler.stream=None

    def test_tls_deduplication_never_hides_errors_or_other_warning_categories(self):
        self.configure()
        sdk=logging.getLogger('mcp.server.lowlevel.server')
        message="Unverified HTTPS request is being made to host 'exchange.example.test'."
        sdk.info('Warning: %s: %s','InsecureRequestWarning',message)
        sdk.error('transport failure: InsecureRequestWarning: %s',message)
        sdk.info('Warning: %s: %s','RuntimeWarning','check InsecureRequestWarning: '+message)
        self.assertIn('transport failure',self.read('errors-2026-10-26.log'))
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
        self.assertEqual(len(audit.splitlines()),1)
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
