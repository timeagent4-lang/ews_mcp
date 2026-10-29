"""Audit the actual tool input without logging credentials or message content."""

import io
import json
import logging
import asyncio
import os
import tempfile
from contextlib import redirect_stderr
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastmcp.tools.tool import ToolResult

from utils.audit import ToolAuditMiddleware
from utils import audit


class AuditArgumentTests(unittest.IsolatedAsyncioTestCase):
    async def record(self, arguments, downstream=None):
        output = io.StringIO()
        logger = logging.Logger("audit-arguments-test")
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger=logger)
        context = SimpleNamespace(
            message=SimpleNamespace(name="check_availability", arguments=arguments),
            fastmcp_context=None,
        )

        async def call_next(context):
            if downstream is not None:
                downstream(context.message.arguments)
            return ToolResult(content="done", structured_content={"ok": True})

        await middleware.on_call_tool(context, call_next)
        serialized = output.getvalue()
        events = [json.loads(line) for line in serialized.splitlines()]
        self.assertEqual([e['event_type'] for e in events], ['tool.started', 'tool.finished'])
        event = events[-1]
        self.assertIn("input_arguments", event)
        return event, serialized

    async def test_records_explicit_values_and_preserves_missing_defaults(self):
        event, _ = await self.record({
            "lanid": "employee", "name": "员工", "start": "2026-10-12T14:00:00+08:00",
            "end": "2026-10-12T15:00:00+08:00", "attendees": ["person@example.test"],
            "duration": 30, "limit": "10", "is_unread": False, "extra": None,
        })
        self.assertEqual(event["input_arguments"], {
            "lanid": "employee", "name": "员工", "start": "2026-10-12T14:00:00+08:00",
            "end": "2026-10-12T15:00:00+08:00", "attendees": ["person@example.test"],
            "duration": 30, "limit": "10", "is_unread": False, "extra": None,
        })
        self.assertNotIn("interval_minutes", event["input_arguments"])

    async def test_snapshots_input_before_downstream_mutates_nested_arguments(self):
        arguments = {"attendees": ["first@example.test"], "params": {"duration": 30}}

        def mutate(params):
            params["attendees"].append("later@example.test")
            params["params"]["duration"] = 60
            params["interval_minutes"] = 30

        event, _ = await self.record(arguments, mutate)
        self.assertEqual(event["input_arguments"], {
            "attendees": ["first@example.test"], "params": {"duration": 30},
        })

    async def test_recursively_redacts_credentials_and_private_content(self):
        event, serialized = await self.record({
            "params": {
                "confirm_token": "CONFIRM_SECRET",
                "nested": [{"Password": "PASSWORD_SECRET", "body": "敏感正文"}],
            },
            "apiKey": "API_SECRET", "authorization": "Bearer AUTH_SECRET",
            "idempotency_key": "IDEMPOTENCY_SECRET", "subject": "SUBJECT_SECRET",
            "internal_reply": "INTERNAL_SECRET", "external_reply": "EXTERNAL_SECRET",
            "message": "MESSAGE_SECRET", "content_base64": "BASE64_SECRET",
            "notes": "CONTACT_NOTES_SECRET",
            "confirm_token": None,
            "query": "同事",
        })
        for secret in (
            "CONFIRM_SECRET", "PASSWORD_SECRET", "API_SECRET", "AUTH_SECRET",
            "IDEMPOTENCY_SECRET", "INTERNAL_SECRET",
            "EXTERNAL_SECRET", "MESSAGE_SECRET", "BASE64_SECRET", "敏感正文",
        ):
            self.assertNotIn(secret, serialized)
        snapshot = event["input_arguments"]
        self.assertIn("confirm_token", snapshot["params"])
        self.assertEqual(snapshot["params"]["nested"][0]["body"], {
            "redacted": True, "type": "string", "length": 4,
        })
        self.assertIsNone(snapshot["confirm_token"])
        self.assertEqual(snapshot["query"], "同事")
        self.assertEqual(snapshot['subject'], 'SUBJECT_SECRET')
        self.assertEqual(snapshot['notes'], 'CONTACT_NOTES_SECRET')

    async def test_redacts_signed_urls_and_bearer_values_in_unexpected_fields(self):
        _, serialized = await self.record({
            "unexpected": "download https://files.example.test/downloads/abc?expires=1&token=SIGNED_SECRET",
            "other": "Bearer BEARER_SECRET",
        })
        self.assertNotIn("SIGNED_SECRET", serialized)
        self.assertNotIn("BEARER_SECRET", serialized)

    async def test_multiline_input_stays_one_json_log_record(self):
        event, _ = await self.record({"query": "甲\n乙\r丙\u0085丁\u2028戊\u2029己"})
        self.assertEqual(event["input_arguments"]["query"], "甲\n乙\r丙\u0085丁\u2028戊\u2029己")

    async def test_unpaired_surrogates_in_values_and_keys_are_safe_to_log(self):
        arguments = {"query": "prefix\ud800suffix", "field\udfff": "value"}
        event, serialized = await self.record(arguments)
        self.assertEqual(event["input_arguments"], arguments)
        serialized.encode("utf-8")

    async def test_redacts_singular_attachment_content(self):
        event, serialized = await self.record({
            "params": {"attachment": {"data": "ATTACHMENT_BYTES_SENTINEL"}},
        })
        self.assertNotIn("ATTACHMENT_BYTES_SENTINEL", serialized)
        self.assertTrue(event['input_arguments']['params']['attachment']['data']['redacted'])

    async def test_malformed_identity_fields_remain_complete(self):
        event, serialized = await self.record({
            "lanid": ["x" * 512] * 100, "name": {"a": "y" * 512},
        })
        snapshot = event["input_arguments"]
        self.assertEqual(snapshot['lanid'], ['x' * 512] * 100)
        self.assertEqual(snapshot['name'], {'a': 'y' * 512})

    async def test_complete_large_json_is_not_silently_truncated(self):
        event, serialized = await self.record({"ids": ["标识" * 1000] * 1000})
        snapshot = event["input_arguments"]
        self.assertEqual(snapshot['ids'], ['标识' * 1000] * 1000)
        self.assertNotIn('truncated', snapshot)

    def test_preserves_subject_recipients_paths_notes_and_ids(self):
        value = {'subject': '会议安排', 'to_recipients': ['person@example.test'],
                 'notes': '联系说明', 'saved_path': 'D:/exports/report.txt',
                 'changekey': 'a' * 1200, 'attachments': [{'filename': 'report.txt', 'size': 10}]}
        self.assertEqual(audit.sanitize_for_log(value, tool='create_contact'), value)

    def test_redacts_body_aliases_and_contextual_messages(self):
        value = {'body': 'BODY_SENTINEL', 'new_body': 'NEW_SENTINEL',
                 'body_preview': 'PREVIEW_SENTINEL', 'preview': {'details': {'message': 'NOTE_SENTINEL'}}}
        safe = audit.sanitize_for_log(value, tool='respond_to_event', kind='result')
        text = json.dumps(safe)
        for secret in ('BODY_SENTINEL', 'NEW_SENTINEL', 'PREVIEW_SENTINEL', 'NOTE_SENTINEL'):
            self.assertNotIn(secret, text)
        self.assertEqual(audit.sanitize_for_log({'body': 'task description'}, tool='create_task'),
                         {'body': 'task description'})

    def test_json_text_mirrors_and_flattened_errors_cannot_leak(self):
        payload = {'results': {'subject': 'visible', 'body': 'HIDDEN_BODY'},
                   'message': 'request XML contains HIDDEN_ERROR'}
        result = ToolResult(content=json.dumps(payload), structured_content=payload)
        safe = audit.result_for_log(result, tool='get_message')
        self.assertNotIn('HIDDEN_BODY', json.dumps(safe))
        self.assertNotIn('HIDDEN_ERROR', json.dumps(safe))
        self.assertEqual(safe['structured_content']['results']['subject'], 'visible')
        self.assertEqual(safe['content'][0]['reference'], 'structured_content')
        self.assertEqual(payload['results']['body'], 'HIDDEN_BODY')

    def test_credentials_exact_echo_urls_and_attachment_payloads(self):
        value = {'confirm_token': 'short-secret', 'echo': 'again short-secret',
                 'download_url': 'https://user:pw@example.test/a?part=1&token=SECRET#token=OTHER',
                 'attachments': [{'filename': 'a.txt', 'content_type': 'text/plain', 'data': b'BYTES'}]}
        safe = audit.sanitize_for_log(value)
        serialized = json.dumps(safe)
        for secret in ('short-secret', 'SECRET', 'OTHER', 'user:pw', 'BYTES'):
            self.assertNotIn(secret, serialized)
        self.assertIn('part=1', safe['download_url'])
        self.assertEqual(safe['attachments'][0]['filename'], 'a.txt')
        with patch.dict(os.environ, {'OUTLOOK_ADMIN_PASSWORD': 'ENV_SECRET'}):
            self.assertNotIn('ENV_SECRET', json.dumps(audit.sanitize_for_log({'query': 'ENV_SECRET'})))

    def test_exception_chain_never_formats_raw_exception_values(self):
        class HostileError(Exception):
            def __str__(self):
                raise AssertionError('raw exception must not be formatted')
        inner = HostileError('SECRET_IN_ARGS')
        outer = ValueError('BODY_IN_ARGS')
        outer.__cause__ = inner
        safe = audit.exception_for_log(outer)
        text = json.dumps(safe)
        self.assertIn('HostileError', text)
        self.assertNotIn('SECRET_IN_ARGS', text)
        self.assertNotIn('BODY_IN_ARGS', text)

    def test_event_record_unknown_free_text_is_omitted(self):
        record = logging.LogRecord('third.party', logging.ERROR, __file__, 1,
                                   'SOAP body SECRET', (), None)
        safe = audit.event_from_record(record)
        self.assertNotIn('SOAP body SECRET', json.dumps(safe))
        record.msg = 'service ready'
        self.assertEqual(audit.event_from_record(record)['message'], 'service ready')

    def test_cyclic_and_unsupported_values_do_not_use_repr(self):
        class Hostile:
            def __repr__(self):
                raise AssertionError('repr must not be used')
        value = {'object': Hostile()}
        value['cycle'] = value
        safe = audit.sanitize_for_log(value)
        self.assertEqual(safe['cycle']['omitted_reason'], 'cycle')
        self.assertEqual(safe['object']['omitted_reason'], 'unsupported_type')

    async def test_call_event_ids_context_and_business_status_are_separate(self):
        output = io.StringIO()
        logger = logging.Logger('audit-status')
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger)
        seen = []
        async def call_next(context):
            seen.append(audit.current_audit_context())
            return ToolResult(content='ignored', structured_content={'status': 'unknown', 'operation_id': 'OP'})
        for _ in range(2):
            await middleware.on_call_tool(SimpleNamespace(message=SimpleNamespace(name='send_draft',
                arguments={'lanid': 'employee', 'operation_id': 'OP'}), fastmcp_context=None), call_next)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len({e['event_id'] for e in events}), 4)
        self.assertEqual(events[0]['call_id'], events[1]['call_id'])
        self.assertNotEqual(events[1]['call_id'], events[2]['call_id'])
        self.assertEqual(events[1]['call_status'], 'returned')
        self.assertEqual(events[1]['status'], 'unknown')
        self.assertTrue(events[1]['is_error'])
        self.assertEqual(events[0]['operation_id_source'], 'unverified_input')
        self.assertEqual(seen[0]['call_id'], events[0]['call_id'])
        self.assertEqual(audit.current_audit_context(), {})

    async def test_concurrent_calls_and_cancel_restore_context(self):
        output = io.StringIO()
        logger = logging.Logger('audit-concurrent')
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger)
        async def invoke(name):
            context = SimpleNamespace(message=SimpleNamespace(name=name, arguments={'lanid': name}), fastmcp_context=None)
            async def next_call(context):
                await asyncio.sleep(0)
                self.assertEqual(audit.current_audit_context()['tool'], name)
                if name == 'cancel':
                    raise asyncio.CancelledError()
                return ToolResult(content='', structured_content={'status': 'processing'})
            try:
                await middleware.on_call_tool(context, next_call)
            except asyncio.CancelledError:
                pass
        await asyncio.gather(invoke('first'), invoke('cancel'))
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 4)
        terminal = next(e for e in events if e['event_type'] == 'tool.cancelled')
        self.assertEqual(terminal['call_status'], 'cancelled')
        self.assertEqual(audit.current_audit_context(), {})

    async def test_worker_and_background_context_keep_origin_and_credentials(self):
        output = io.StringIO()
        logger = logging.Logger('audit-worker')
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger)
        captured = []
        release = asyncio.Event()
        async def worker():
            audit.update_audit_context(verified_lanid='employee', mailbox='employee@example.test',
                                       identity_assurance='oa_lanid_resolved')
            return audit.current_audit_context()
        async def background():
            await release.wait()
            origin = audit.current_audit_context()
            audit.emit_event('dispatch.finished', logger=logger,
                             origin_call_id=origin['call_id'], echo='TOKEN_WORKER')
        async def next_call(context):
            captured.append(await asyncio.to_thread(lambda: asyncio.run(worker())))
            captured.append(asyncio.create_task(background()))
            return ToolResult(content='', structured_content={'status': 'processing'})
        await middleware.on_call_tool(SimpleNamespace(message=SimpleNamespace(name='send_draft',
            arguments={'lanid': 'employee', 'confirm_token': 'TOKEN_WORKER'}), fastmcp_context=None), next_call)
        release.set()
        await captured[1]
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(events[1]['caller']['verified_lanid'], 'employee')
        self.assertEqual(events[2]['origin_call_id'], events[0]['call_id'])
        self.assertNotIn('TOKEN_WORKER', output.getvalue())

    def test_emit_handler_failure_has_health_signal_without_raising(self):
        class BrokenHandler(logging.Handler):
            def emit(self, record):
                raise OSError('BODY_SECRET')
        logger = logging.Logger('broken')
        logger.addHandler(BrokenHandler())
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            event = audit.emit_event('tool.stage', logger=logger, stage='ready')
        self.assertEqual(event['stage'], 'ready')
        self.assertIn('LOG_EMISSION_FAILED', stderr.getvalue())
        self.assertNotIn('BODY_SECRET', stderr.getvalue())

    async def test_snapshot_failure_does_not_replace_business_result(self):
        class HostileDict(dict):
            def items(self):
                raise ValueError('BODY_SECRET')
        output = io.StringIO()
        logger = logging.Logger('snapshot-failure')
        logger.addHandler(logging.StreamHandler(output))
        middleware = ToolAuditMiddleware(logger)
        result = ToolResult(content='', structured_content={'ok': True})
        async def next_call(context):
            return result
        with redirect_stderr(io.StringIO()):
            returned = await middleware.on_call_tool(SimpleNamespace(message=SimpleNamespace(name='test',
                arguments=HostileDict()), fastmcp_context=None), next_call)
        self.assertIs(returned, result)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(events), 2)

    def test_json_only_result_has_original_business_status(self):
        result = SimpleNamespace(content=[SimpleNamespace(type='text', text='{"status":"failed","error_code":"E"}')], isError=True)
        self.assertEqual(audit._classify_result(result), ('failed', 'E', None))

    def test_legacy_event_keeps_supplied_id_and_failure_severity(self):
        output = io.StringIO()
        logger = logging.Logger('legacy-event')
        logger.addHandler(logging.StreamHandler(output))
        audit._emit_audit_record(logger, tool_name='send_draft', arguments={}, assurance=None,
            status='unknown', error_code='OPERATION_UNKNOWN', result_count=None,
            event_id='supplied-event', started_at=None, started=0)
        event = json.loads(output.getvalue())
        self.assertEqual(event['event_id'], 'supplied-event')
        self.assertTrue(event['is_error'])
        self.assertIn('service_instance_id', event)
        external = logging.LogRecord('third.party', logging.ERROR, __file__, 1, 'SOAP SECRET', (), None)
        self.assertTrue(audit.event_from_record(external)['is_error'])

    def test_legacy_context_does_not_mistake_response_body_for_safe_template(self):
        value = {'tool': 'respond_to_event', 'input_arguments': {'message': 'service ready'},
                 'result': {'content': [{'type': 'text', 'text': 'BODY_IN_TEXTCONTENT'}]}}
        safe = audit.sanitize_for_log(value, tool='respond_to_event', kind='event')
        serialized = json.dumps(safe)
        self.assertNotIn('service ready', serialized)
        self.assertNotIn('BODY_IN_TEXTCONTENT', serialized)

    def test_diagnostic_objects_do_not_bypass_unknown_text_policy(self):
        value = {'exchange_message': {'payload': 'SOAP_BODY'}, 'message': ['MAIL_BODY']}
        self.assertNotIn('SOAP_BODY', json.dumps(audit.sanitize_for_log(value, kind='result')))
        self.assertNotIn('MAIL_BODY', json.dumps(audit.sanitize_for_log(value, kind='result')))

    async def test_protocol_error_without_business_verdict_is_failure(self):
        output = io.StringIO()
        logger = logging.Logger('protocol-error')
        logger.addHandler(logging.StreamHandler(output))
        result = SimpleNamespace(structuredContent={}, content=[], isError=True)
        async def next_call(context):
            return result
        returned = await ToolAuditMiddleware(logger).on_call_tool(SimpleNamespace(message=SimpleNamespace(
            name='test', arguments={}), fastmcp_context=None), next_call)
        event = json.loads(output.getvalue().splitlines()[-1])
        self.assertIs(returned, result)
        self.assertTrue(event['is_error'])
        self.assertTrue(event['mcp_is_error'])
        self.assertEqual(event['status'], 'failure')

    def test_forged_redaction_markers_cannot_hide_secret_in_metadata(self):
        value = {'body': {'redacted': True, 'type': 'BODY_MARKER_LEAK', 'fingerprint': 'MORE_BODY'},
                 'confirm_token': {'redacted': True, 'type': 'string', 'fingerprint': 'TOKEN_MARKER_LEAK'}}
        safe = audit.sanitize_for_log(value)
        for secret in ('BODY_MARKER_LEAK', 'MORE_BODY', 'TOKEN_MARKER_LEAK'):
            self.assertNotIn(secret, json.dumps(safe))

    def test_untrusted_json_logrecords_are_not_structured_adapters(self):
        record = logging.LogRecord('thirdparty.transport', logging.INFO, __file__, 1,
            '{"response":"<Envelope><Body>BODY_CANARY</Body></Envelope>"}', (), None)
        safe = audit.event_from_record(record)
        self.assertNotIn('BODY_CANARY', json.dumps(safe))
        self.assertNotIn('response', safe)
        marked = logging.LogRecord('py.warnings', logging.WARNING, __file__, 1,
            '{"event_type":"runtime.tls_warning","host":"mail.example.test"}', (), None)
        marked._ews_safe_json = True
        self.assertEqual(audit.event_from_record(marked)['host'], 'mail.example.test')

    def test_json_scalar_and_list_text_are_not_typed_tool_results(self):
        for text in ('"BODY_CANARY"', '["BODY_CANARY"]'):
            with self.subTest(text=text):
                result = SimpleNamespace(content=[SimpleNamespace(type='text', text=text)], isError=False)
                safe = audit.result_for_log(result, tool='get_message')
                self.assertNotIn('BODY_CANARY', json.dumps(safe))
                self.assertIsNone(safe['structured_content'])

    def test_url_encoded_known_secret_echo_is_scrubbed_after_decode(self):
        with patch.dict(os.environ, {'EWS_MCP_API_KEY': 'a+b/c'}):
            safe = audit.sanitize_for_log({'url': 'https://example.test/a?echo=a%2Bb%2Fc&safe=yes&a%2Bb%2Fc=other'})
        query = parse_qs(urlsplit(safe['url']).query)
        self.assertEqual(query['safe'], ['yes'])
        self.assertNotIn('a+b/c', json.dumps(query))
        self.assertNotIn('a%2Bb%2Fc', safe['url'])

    def test_forged_valid_hex_fingerprint_cannot_echo_configured_secret(self):
        secret = 'a' * 64
        with patch.dict(os.environ, {'EWS_MCP_API_KEY': secret}):
            safe = audit.sanitize_for_log({'confirm_token': {'redacted': True, 'type': 'string', 'fingerprint': secret}})
        self.assertNotIn(secret, json.dumps(safe))

    def upload_record(self, *, status=503, code='UPLOAD_STORAGE_UNAVAILABLE', name='attachment_uploads'):
        return logging.LogRecord(name, logging.INFO, __file__, 1,
            'attachment_upload_http upload_id=%s status=%s error_code=%s bytes=%s duration_ms=%s',
            ('a' * 32, status, code, 120, 15), None)

    def test_upload_http_template_has_validated_operational_metadata(self):
        safe = audit.event_from_record(self.upload_record())
        self.assertEqual(safe['upload_id'], 'a' * 32)
        self.assertEqual(safe['status'], 503)
        self.assertEqual(safe['error_code'], 'UPLOAD_STORAGE_UNAVAILABLE')
        self.assertEqual(safe['bytes'], 120)
        self.assertEqual(safe['duration_ms'], 15)
        self.assertTrue(safe['is_error'])
        self.assertFalse(audit.event_from_record(self.upload_record(status=200, code=None))['is_error'])
        rejected = self.upload_record(code='BODY_CANARY')
        self.assertNotIn('BODY_CANARY', json.dumps(audit.event_from_record(rejected)))
        self.assertNotIn('upload_id', audit.event_from_record(self.upload_record(name='thirdparty.transport')))

    def test_file_console_and_query_share_safe_upload_and_untrusted_json_policy(self):
        from utils.logging_config import _Formatter, _OutputFilter
        from utils.log_query import main
        uploaded = self.upload_record()
        external = logging.LogRecord('thirdparty.transport', logging.ERROR, __file__, 1,
            '{"response":"BODY_CANARY"}', (), None)
        full = _Formatter()
        console = _Formatter(console=True)
        self.assertTrue(_OutputFilter(errors_only=True).filter(uploaded))
        self.assertNotIn('BODY_CANARY', console.format(external))
        day = date.today().isoformat()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ('audit-' + day + '.log')
            target.write_text(full.format(uploaded) + '\n' + full.format(external) + '\n', encoding='utf-8')
            self.assertNotIn('BODY_CANARY', target.read_text(encoding='utf-8'))
            output = io.StringIO()
            with patch.dict(os.environ, {'EWS_MCP_LOG_DIR': directory}), redirect_stdout(output):
                status = main(['--date', day, '--kind', 'audit', '--full'])
            self.assertEqual(status, 0)
            self.assertIn('UPLOAD_STORAGE_UNAVAILABLE', output.getvalue())
            self.assertIn('"upload_id"', output.getvalue())
            self.assertNotIn('BODY_CANARY', output.getvalue())


if __name__ == "__main__":
    unittest.main()
