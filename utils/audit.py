"""Safe JSONL representations and correlated MCP call events.

Importing this module never configures logging or reads service credentials.
"""
import asyncio
import hashlib
import itertools
import json
import logging
import math
import os
import re
import sys
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from urllib.parse import parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from fastmcp.server.middleware import Middleware
from mcp import types as mcp_types
from utils.tool_results import ACTIVE_STATUSES, result_is_error

AUDIT_IDENTITY_STATE_KEY = 'mail_mcp.identity_assurance'
_UNICODE_LINE_SEPARATOR_ESCAPES = {0x0085: '\\u0085', 0x2028: '\\u2028', 0x2029: '\\u2029'}
_AUDIT_EMITTED = ContextVar('mail_mcp.audit_emitted', default=False)
_AUDIT_CONTEXT = ContextVar('mail_mcp.audit_context', default=None)
_AUDIT_CALL_STATE = ContextVar('mail_mcp.audit_call_state', default=None)
_AUDIT_SECRETS = ContextVar('mail_mcp.audit_secrets', default=())
_CONTEXT_LOCK = threading.RLock()
_INSTANCE_ID = uuid.uuid4().hex
_SEQUENCE = itertools.count(1)
_SEQUENCE_LOCK = threading.Lock()
_BODY_FIELDS = frozenset({'body', 'newbody', 'bodypreview', 'bodytext', 'bodyhtml', 'textbody', 'htmlbody',
    'quotedbody', 'quotedtext', 'originalbody', 'forwardedbody', 'internalreply', 'externalreply'})
_PAYLOAD_FIELDS = frozenset({'contentbytes', 'contentbase64', 'base64', 'attachmentdata', 'rawcontent', 'rawdocument'})
_SECRET_PARTS = ('password', 'passwd', 'secret', 'token', 'apikey', 'accesskey', 'authorization', 'cookie', 'credential', 'privatekey', 'signature')
_TASK_TOOLS = frozenset({'create_task', 'update_task', 'list_tasks', 'get_task'})
_RESPONSE_TOOLS = frozenset({'respond_to_event', 'cancel_event'})
_SECRET_ENV = ('OUTLOOK_ADMIN_PASSWORD', 'EWS_MCP_API_KEY', 'EWS_MCP_DOWNLOAD_SECRET', 'OA_API_KEY', 'OA_API_SECRET')
_URL_RE = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
_BEARER_RE = re.compile(r'\bbearer\s+[^\s,;]+', re.IGNORECASE)
_SAFE_MESSAGES = frozenset({'service ready', '查询部分完成，结果不完整', '邮箱服务不稳定 请五分钟后查看结果',
    '等待确认执行。', '操作尚未取得最终结果。', '执行失败：请查询原操作号确认后重试。',
    '部分成功：请仅对失败条目重试，勿整批重发。', '全部失败：请仅对失败条目重试，勿整批重发。',
    '发送结果未知，请在已发送邮件中核实实际结果；勿盲目重发。', '执行结果未知，请核实实际结果；勿盲目重试。',
    '附件下载存储不可用，请联系管理员。', '附件上传存储不可用，请联系管理员。', '发送后台处理异常', 'EWS MCP 启动或运行失败'})


def _normalized(field):
    return re.sub(r'[^a-z0-9]', '', field.lower())


def _json_type(value):
    if value is None:
        return 'null'
    for kind, name in ((bool, 'boolean'), (str, 'string'), (dict, 'object'), (list, 'array'),
                       (int, 'integer'), (float, 'number'), (bytes, 'bytes')):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _redacted(value, reason=None):
    metadata = {'redacted': True, 'type': _json_type(value)}
    if isinstance(value, (str, list, tuple, dict, bytes, bytearray, memoryview)):
        metadata['length'] = len(value)
    if reason:
        metadata['omitted_reason'] = reason
    return metadata


def _secret_field(field):
    name = _normalized(field)
    if name in {'tokenexpires', 'tokenexpiresat', 'uploadexpiresat'}:
        return False
    return name in {'key', 'pwd', 'sig', 'confirmationid', 'idempotencykey'} or any(part in name for part in _SECRET_PARTS)


def _known_secrets(value):
    """Collect credential values without formatting arbitrary caller objects."""
    secrets = set(_AUDIT_SECRETS.get())
    secrets.update(env_value for name in _SECRET_ENV if (env_value := os.environ.get(name)))
    active = set()
    def visit(item, field='', depth=0):
        if depth > 100:
            return
        if isinstance(item, str):
            if _secret_field(field) and item:
                secrets.add(item)
                if item.lower().startswith('bearer '):
                    secrets.add(item[7:])
            for match in _URL_RE.finditer(item):
                try:
                    url = urlsplit(match.group())
                    if url.password:
                        secrets.add(url.password)
                    for key, part in parse_qsl(url.query, keep_blank_values=True) + parse_qsl(url.fragment, keep_blank_values=True):
                        if _secret_field(key) and part:
                            secrets.add(part)
                except ValueError:
                    pass
        elif isinstance(item, (dict, list, tuple)) and id(item) not in active:
            active.add(id(item))
            try:
                if isinstance(item, dict):
                    for key, part in item.items():
                        visit(part, key if isinstance(key, str) else '', depth + 1)
                else:
                    for part in item:
                        visit(part, field, depth + 1)
            finally:
                active.remove(id(item))
    visit(value)
    return tuple(sorted(secrets, key=len, reverse=True))


def _replace_secrets(value, secrets):
    value = _BEARER_RE.sub('Bearer <redacted>', value)
    for secret in secrets:
        value = value.replace(secret, '<redacted>')
    return value


def _safe_url(match, secrets):
    try:
        url = urlsplit(match.group())
        host = _replace_secrets(url.netloc.rsplit('@', 1)[-1], secrets)
        # URL parsing decodes query keys/values. Scrub these before re-encoding;
        # replacing only the serialized URL misses recoverable percent-encoded keys.
        query = [(_replace_secrets(key, secrets), '<redacted>' if _secret_field(key) else _replace_secrets(value, secrets))
                 for key, value in parse_qsl(url.query, keep_blank_values=True)]
        path = quote(_replace_secrets(unquote(url.path), secrets), safe="/:@!$&'()*+,;=-._~")
        return urlunsplit((url.scheme, host, path, urlencode(query), ''))
    except (TypeError, ValueError):
        return '<redacted-url>'


def _safe_text(value, secrets):
    return _replace_secrets(_URL_RE.sub(lambda match: _safe_url(match, secrets), value), secrets)


def _safe_diagnostic(value):
    return isinstance(value, str) and (value in _SAFE_MESSAGES or re.fullmatch(r'failure-[0-9A-Z]', value) is not None)


def sanitize_for_log(value, *, tool=None, kind='input'):
    """Return detached JSON-safe data; ordinary metadata is never truncated."""
    try:
        secrets = _known_secrets(value)
    except Exception:
        secrets = _AUDIT_SECRETS.get()
    active = set()
    def visit(item, field='', path=(), depth=0):
        name = _normalized(field)
        if isinstance(item, dict) and item.get('redacted') is True and item.get('type') in {
                'null', 'boolean', 'string', 'object', 'array', 'integer', 'number', 'bytes', 'bytearray', 'memoryview', 'tuple'}:
            marker = {'redacted': True, 'type': item['type']}
            if type(item.get('length')) is int and item['length'] >= 0:
                marker['length'] = item['length']
            if item.get('omitted_reason') in {'attachment_payload', 'raw_payload', 'binary_payload', 'untrusted_diagnostic'}:
                marker['omitted_reason'] = item['omitted_reason']
            if item.get('present') is True:
                marker['present'] = True
            fingerprint = item.get('fingerprint')
            if isinstance(fingerprint, str) and re.fullmatch('[0-9a-f]{64}', fingerprint):
                marker['fingerprint'] = _replace_secrets(fingerprint, secrets)
            return marker
        if item is not None and _secret_field(field):
            result = _redacted(item)
            if name == 'idempotencykey' and isinstance(item, str):
                result.update(present=True, fingerprint=_replace_secrets(
                    hashlib.sha256(item.encode('utf-8', errors='surrogatepass')).hexdigest(), secrets))
            return result
        if item is not None and name in _BODY_FIELDS and not (tool in _TASK_TOOLS and name in {'body', 'bodypreview'}):
            return _redacted(item)
        attachment = any(_normalized(part) in {'attachment', 'attachments'} for part in path)
        if item is not None and (name in _PAYLOAD_FIELDS or (name in {'data', 'content', 'text'} and attachment)
                or (name == 'text' and 'content' in path)
                or (name == 'text' and tool == 'get_attachment')
                or (name == 'content' and isinstance(item, (str, bytes, bytearray, memoryview)))):
            return _redacted(item, 'attachment_payload' if attachment or tool == 'get_attachment' else 'raw_payload')
        if item is not None and name in {'message', 'exchangemessage', 'detail', 'errordescription'}:
            if tool in _RESPONSE_TOOLS and (kind == 'input' or any(part in {'details', 'input', 'input_arguments'} for part in path)):
                return _redacted(item)
            if not isinstance(item, str) or not _safe_diagnostic(item):
                return _redacted(item, 'untrusted_diagnostic')
        if depth > 100:
            return {'type': _json_type(item), 'omitted_reason': 'maximum_nesting'}
        if isinstance(item, (dict, list, tuple)):
            if id(item) in active:
                return {'type': _json_type(item), 'omitted_reason': 'cycle'}
            active.add(id(item))
            try:
                if isinstance(item, dict):
                    return {_safe_text(key, secrets) if isinstance(key, str) else '<non-string-key>':
                        visit(part, key if isinstance(key, str) else '', path + (field,), depth + 1)
                        for key, part in item.items()}
                return [visit(part, '', path + (field,), depth + 1) for part in item]
            finally:
                active.remove(id(item))
        if isinstance(item, str):
            return _safe_text(item, secrets)
        if item is None or isinstance(item, (bool, int)):
            return item
        if isinstance(item, float) and math.isfinite(item):
            return item
        if isinstance(item, (bytes, bytearray, memoryview)):
            return _redacted(item, 'binary_payload')
        return {'type': _json_type(item), 'omitted_reason': 'unsupported_type'}
    try:
        return visit(value)
    except Exception:
        return {'type': _json_type(value), 'omitted_reason': 'representation_failed'}


def _snapshot_arguments(arguments, *, tool=None):
    return sanitize_for_log(arguments, tool=tool, kind='input')


def _root_and_structured(result):
    root = getattr(result, 'root', result)
    structured = root if isinstance(root, dict) else getattr(root, 'structured_content', None)
    if structured is None:
        structured = getattr(root, 'structuredContent', None)
    if structured is None:
        blocks = getattr(root, 'content', None) or []
        if isinstance(blocks, str):
            blocks = [blocks]
        for block in blocks:
            text = block if isinstance(block, str) else getattr(block, 'text', None)
            if isinstance(text, str):
                try:
                    parsed = json.loads(text)
                    if isinstance(parsed, dict):
                        structured = parsed
                        break
                except (ValueError, RecursionError):
                    pass
    return root, structured


def result_for_log(result, *, tool=None):
    """Represent structured results once, without unsafe MCP text mirrors."""
    root, structured = _root_and_structured(result)
    protocol_error = getattr(root, 'isError', None)
    blocks = getattr(root, 'content', None) or []
    if isinstance(blocks, str):
        blocks = [blocks]
    content = []
    for block in blocks:
        block_type = 'text' if isinstance(block, str) else getattr(block, 'type', type(block).__name__)
        text = block if isinstance(block, str) else getattr(block, 'text', None)
        if isinstance(text, str):
            try:
                parsed = json.loads(text)
            except (ValueError, RecursionError):
                content.append({'type': 'text', 'length': len(text), 'omitted_reason': 'untrusted_text'})
            else:
                if not isinstance(parsed, dict):
                    content.append({'type': 'text', 'length': len(text), 'omitted_reason': 'untyped_json_text'})
                elif structured is not None and parsed == structured:
                    content.append({'type': 'text', 'reference': 'structured_content'})
                elif structured is None:
                    structured = parsed
                    content.append({'type': 'text', 'reference': 'structured_content'})
                else:
                    content.append({'type': 'text', 'value': sanitize_for_log(parsed, tool=tool, kind='result')})
        else:
            content.append({'type': block_type if isinstance(block_type, str) else 'unknown', 'omitted_reason': 'non_text_payload'})
    return {'structured_content': sanitize_for_log(structured, tool=tool, kind='result'),
            'content': content, 'mcp_is_error': protocol_error if isinstance(protocol_error, bool) else None}


def exception_for_log(exc):
    """Capture chain/frame metadata without str/repr/args/locals/source lines."""
    seen = set()
    def visit(error, depth=0):
        if id(error) in seen or depth >= 20:
            return {'type': type(error).__name__, 'omitted_reason': 'cycle' if id(error) in seen else 'exception_chain_limit'}
        seen.add(id(error))
        result = {'type': type(error).__name__, 'module': type(error).__module__}
        for attr in ('code', 'request_id', 'response_code', 'errno'):
            value = getattr(error, attr, None)
            if isinstance(value, int) or (isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_.:/-]{1,256}', value)):
                result[attr] = value
        public = getattr(error, 'public_message', None)
        if _safe_diagnostic(public):
            result['message'] = public
        else:
            result['message_omitted'] = 'untrusted_exception_text'
        frames = []
        trace = error.__traceback__
        while trace is not None and len(frames) < 100:
            code = trace.tb_frame.f_code
            frames.append({'file': code.co_filename, 'function': code.co_name, 'line': trace.tb_lineno})
            trace = trace.tb_next
        if trace is not None:
            result['frames_omitted'] = 'frame_limit'
        result['frames'] = frames
        if error.__cause__ is not None:
            result['cause'] = visit(error.__cause__, depth + 1)
        elif error.__context__ is not None and not error.__suppress_context__:
            result['context'] = visit(error.__context__, depth + 1)
        if isinstance(error, BaseExceptionGroup):
            result['exceptions'] = [visit(child, depth + 1) for child in error.exceptions[:50]]
            if len(error.exceptions) > 50:
                result['exceptions_omitted'] = len(error.exceptions) - 50
        return result
    return sanitize_for_log(visit(exc), kind='error')


def current_audit_context():
    with _CONTEXT_LOCK:
        return sanitize_for_log(_AUDIT_CONTEXT.get() or {}, kind='event')


def update_audit_context(**fields):
    """Enrich this call's shared carrier after existing authorization succeeds."""
    with _CONTEXT_LOCK:
        current = _AUDIT_CONTEXT.get()
        if current is None:
            return {}
        safe = sanitize_for_log(fields, tool=current.get('tool'), kind='event')
        caller = {**current.get('caller', {}), **safe.pop('caller', {})}
        for key in ('claimed_lanid', 'claimed_name', 'verified_lanid', 'mailbox', 'identity_assurance'):
            if key in safe:
                caller[key] = safe[key]
        current.update(safe)
        current['caller'] = caller
        return sanitize_for_log(current, tool=current.get('tool'), kind='event')


def _single_line_json(record):
    return json.dumps(record, ensure_ascii=False, separators=(',', ':'), allow_nan=False).translate(
        _UNICODE_LINE_SEPARATOR_ESCAPES).encode('utf-8', errors='backslashreplace').decode('utf-8')


def _envelope(event_type, fields):
    with _SEQUENCE_LOCK:
        sequence = next(_SEQUENCE)
    return {'schema_version': 2, 'event_type': event_type, 'event_id': str(uuid.uuid4()),
            'timestamp': datetime.now(timezone.utc).isoformat(), 'service_instance_id': _INSTANCE_ID,
            'sequence': sequence, 'pid': os.getpid(), **fields}


def emit_event(event_type, *, context=None, logger=None, **fields):
    """Best-effort emission cannot change the business outcome or exception."""
    inherited = current_audit_context() if context is None else context
    tool = fields.get('tool') or inherited.get('tool')
    event = _envelope(event_type, sanitize_for_log({**inherited, **fields}, tool=tool, kind='event'))
    try:
        (logger if logger is not None else logging.getLogger('mail_mcp.audit')).info(
            _single_line_json(event), extra={'log_event': event, 'audit_status': event.get('status'),
                'audit_tool': event.get('tool'), 'audit_error_code': event.get('error_code'),
                'audit_event_id': event['event_id']})
    except Exception:
        try:
            sys.stderr.write(_single_line_json({'event_type': 'logging.failed', 'event_id': event['event_id'],
                'error_code': 'LOG_EMISSION_FAILED'}) + '\n')
        except Exception:
            pass
    return event


def event_from_record(record):
    """Pure formatter boundary for our events and third-party LogRecords."""
    existing = getattr(record, 'log_event', None)
    if isinstance(existing, dict):
        return sanitize_for_log(existing, tool=existing.get('tool'), kind='event')
    fields = {'logger': record.name, 'level': record.levelname, 'is_error': record.levelno >= logging.ERROR}
    message = record.msg
    upload = _upload_http_event(record)
    if upload is not None:
        fields.update(upload)
        return _envelope('attachment.upload_http', sanitize_for_log(fields, kind='event'))
    if isinstance(message, str):
        parsed = None
        if getattr(record, '_ews_safe_json', False) is True:
            try:
                parsed = json.loads(message)
            except (ValueError, RecursionError):
                pass
        if isinstance(parsed, dict):
            fields.update(sanitize_for_log(parsed, tool=parsed.get('tool'), kind='event'))
        elif _safe_diagnostic(message) and not record.args:
            fields['message'] = message
        else:
            fields['message'] = {'type': 'string', 'length': len(message), 'omitted_reason': 'untrusted_log_text'}
    else:
        fields['message'] = {'type': type(message).__name__, 'omitted_reason': 'untrusted_log_object'}
    if record.exc_info and isinstance(record.exc_info[1], BaseException):
        fields['error'] = exception_for_log(record.exc_info[1])
    return _envelope(fields.pop('event_type', 'runtime.log'), sanitize_for_log(fields, kind='event'))


def _upload_http_event(record):
    """Adapt one existing fixed HTTP template, never interpolate its arguments."""
    template = 'attachment_upload_http upload_id=%s status=%s error_code=%s bytes=%s duration_ms=%s'
    if record.name != 'attachment_uploads' or record.msg != template:
        return None
    args = record.args
    if not isinstance(args, tuple) or len(args) != 5:
        return None
    upload_id, status, code, size, duration = args
    allowed_codes = {None, 'UPLOAD_UNAVAILABLE', 'UPLOAD_DISABLED', 'INVALID_UPLOAD_TOKEN',
        'UPLOAD_LENGTH_MISMATCH', 'UPLOAD_EXPIRED', 'UPLOAD_CONFLICT', 'CONTENT_LENGTH_REQUIRED',
        'ATTACHMENT_TOO_LARGE', 'UPLOAD_BUSY', 'UPLOAD_TIMEOUT', 'UPLOAD_STORAGE_UNAVAILABLE', 'UPLOAD_INTERRUPTED'}
    if (not isinstance(upload_id, str) or (upload_id != '<invalid>' and not re.fullmatch('[0-9a-f]{32}', upload_id))
            or type(status) is not int or not 100 <= status <= 599
            or not (code is None or isinstance(code, str)) or code not in allowed_codes
            or type(size) is not int or size < 0 or type(duration) is not int or duration < 0):
        return None
    return {'upload_id': upload_id, 'status': status, 'error_code': code,
            'bytes': size, 'duration_ms': duration, 'is_error': status >= 400}


def _params_dict(arguments):
    return arguments if isinstance(arguments, dict) else {}


def _safe_string(value):
    return value if isinstance(value, str) else None


def _text_length(value):
    if isinstance(value, dict) and value.get('redacted'):
        return value.get('length')
    return len(value) if isinstance(value, str) else None


def _recipient_count(value):
    return len([item for item in value.split(',') if item.strip()]) if isinstance(value, str) else None


def summarize_arguments(tool_name, arguments):
    params = _params_dict(arguments)
    requester = {'lanid': _safe_string(params.get('lanid')), 'name': _safe_string(params.get('name'))}
    if tool_name == 'summary_yesterday':
        summary = {'unread_only': params.get('unread_only', False)}
    elif tool_name == 'search_mail':
        summary = {'query': _safe_string(params.get('query')), 'limit': params.get('limit', 10)}
    elif tool_name in {'create_mail_draft', 'create_meeting'}:
        summary = {'subject_length': _text_length(params.get('subject')), 'body_length': _text_length(params.get('body')),
                   'recipient_count': _recipient_count(params.get('to_emails', ''))}
    elif tool_name == 'list_meetings':
        summary = {'num_days': params.get('num_days', 10)}
    else:
        summary = {}
    return requester, summary


def create_audit_logger():
    logger = logging.getLogger('mail_mcp.audit')
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter('%(message)s'))
        logger.addHandler(handler)
    return logger


def _classify_result(result):
    root, structured = _root_and_structured(result)
    if isinstance(structured, dict) and isinstance(structured.get('status'), str):
        status = structured['status']
        error_code = None if status in ACTIVE_STATUSES else structured.get('error_code')
    elif isinstance(structured, dict) and (result_is_error(structured) or structured.get('ok') is False):
        error_code = structured.get('error_code')
        status = 'rejected' if str(error_code).startswith('IDENTITY_') else 'failure'
    elif getattr(root, 'isError', False) is True:
        status, error_code = 'failure', 'MCP_TOOL_ERROR'
    elif isinstance(structured, dict) and structured.get('partial') is True:
        status, error_code = 'incomplete', None
    else:
        status, error_code = 'success', None
    values = structured.get('results') if isinstance(structured, dict) else None
    return status, error_code, len(values) if isinstance(values, list) else None


def _emit_audit_record(logger, *, tool_name, arguments, assurance, status, error_code,
                       result_count, event_id, started_at, started, **fields):
    requester, summary = summarize_arguments(tool_name, arguments)
    return emit_event('tool.finished', logger=logger, tool=tool_name, **requester,
        identity_assurance=assurance or 'not_verified', status=status, error_code=error_code,
        duration_ms=round((time.perf_counter() - started) * 1000), input_summary=summary,
        input_arguments=arguments, result_count=result_count, call_status='returned', event_id=event_id,
        is_error=status in {'failure', 'rejected', 'error', 'failed', 'partial', 'unknown'}, **fields)


def _new_call(tool, arguments):
    params = _params_dict(arguments)
    caller = {'claimed_lanid': _safe_string(params.get('lanid')), 'claimed_name': _safe_string(params.get('name')),
              'verified_lanid': None, 'mailbox': None, 'identity_assurance': 'not_verified'}
    result = {'call_id': str(uuid.uuid4()), 'tool': tool, 'caller': caller,
              'claimed_lanid': caller['claimed_lanid'], 'claimed_name': caller['claimed_name']}
    if isinstance(params.get('operation_id'), str):
        result.update(operation_id=params['operation_id'], operation_id_source='unverified_input')
    return result


async def _audit_call(logger, tool, raw_arguments, call, fastmcp_context=None, *, transport_owner=False):
    state = _AUDIT_CALL_STATE.get()
    if not transport_owner and state is not None and state['awaiting_middleware'] and state['tool'] == tool:
        # FastMCP copies arguments between the transport handler and middleware.
        # Ownership is consumed once rather than inferred from object identity.
        state['awaiting_middleware'] = False
        if fastmcp_context is not None:
            fastmcp_context.set_state(AUDIT_IDENTITY_STATE_KEY, 'not_verified')
            state['fastmcp_context'] = fastmcp_context
        return await call()
    try:
        secrets = _known_secrets(raw_arguments)
    except Exception:
        secrets = _AUDIT_SECRETS.get()
    secret_token = _AUDIT_SECRETS.set(secrets)
    context_token = _AUDIT_CONTEXT.set(_new_call(tool, raw_arguments))
    state_token = _AUDIT_CALL_STATE.set({'awaiting_middleware': transport_owner, 'tool': tool,
                                       'fastmcp_context': fastmcp_context})
    started = time.perf_counter()
    arguments = _snapshot_arguments(raw_arguments, tool=tool)
    requester, summary = summarize_arguments(tool, arguments)
    if fastmcp_context is not None:
        fastmcp_context.set_state(AUDIT_IDENTITY_STATE_KEY, 'not_verified')
    emit_event('tool.started', logger=logger, call_status='started', status='started', **requester,
               identity_assurance='not_verified', input=arguments, input_arguments=arguments, input_summary=summary)
    result = None
    error = None
    try:
        result = await call()
        return result
    except BaseException as exc:
        error = exc
        raise
    finally:
        try:
            actual_context = _AUDIT_CALL_STATE.get()['fastmcp_context']
            assurance = (actual_context.get_state(AUDIT_IDENTITY_STATE_KEY) if actual_context is not None else None) or current_audit_context().get('identity_assurance') or 'not_verified'
            update_audit_context(identity_assurance=assurance)
            fields = {**requester, 'identity_assurance': assurance, 'input_arguments': arguments,
                      'input_summary': summary, 'duration_ms': round((time.perf_counter() - started) * 1000)}
            if error is not None:
                cancelled = isinstance(error, asyncio.CancelledError)
                emit_event('tool.cancelled' if cancelled else 'tool.failed', logger=logger,
                    call_status='cancelled' if cancelled else 'error', status='cancelled' if cancelled else 'error',
                    error_code='TOOL_CANCELLED' if cancelled else 'TOOL_CALL_ERROR', is_error=not cancelled,
                    mcp_is_error=None, error=exception_for_log(error), **fields)
            else:
                status, error_code, count = _classify_result(result)
                root, structured = _root_and_structured(result)
                is_error = ((result_is_error(structured) or structured.get('ok') is False) if isinstance(structured, dict) else getattr(root, 'isError', False) is True)
                if isinstance(structured, dict) and not structured.get('status') and status == 'failure':
                    is_error = True
                emit_event('tool.finished', logger=logger, call_status='returned', status=status,
                    error_code=error_code, is_error=is_error, mcp_is_error=getattr(root, 'isError', None),
                    result=result_for_log(result, tool=tool), result_count=count, **fields)
            _AUDIT_EMITTED.set(True)
        finally:
            _AUDIT_CONTEXT.reset(context_token)
            _AUDIT_SECRETS.reset(secret_token)
            _AUDIT_CALL_STATE.reset(state_token)


class ToolAuditMiddleware(Middleware):
    def __init__(self, logger=None):
        self.logger = logger if logger is not None else create_audit_logger()
    async def on_call_tool(self, context, call_next):
        return await _audit_call(self.logger, context.message.name, context.message.arguments,
                                 lambda: call_next(context), context.fastmcp_context)


def install_tool_audit_fallback(fastmcp, middleware):
    """Audit tools/call even when FastMCP stops before tool middleware."""
    server = fastmcp._mcp_server
    marker = '_mail_mcp_audit_fallback_installed'
    if getattr(server, marker, False):
        return
    original = server.request_handlers[mcp_types.CallToolRequest]
    async def audited_handler(request):
        return await _audit_call(middleware.logger, request.params.name, request.params.arguments,
                                 lambda: original(request), transport_owner=True)
    server.request_handlers[mcp_types.CallToolRequest] = audited_handler
    setattr(server, marker, True)
