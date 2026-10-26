"""MCP 工具调用的单行 JSON 审计。"""

import json
import logging
import math
import re
import sys
import time
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone

from fastmcp.server.middleware import Middleware
from mcp import types as mcp_types


AUDIT_IDENTITY_STATE_KEY = "mail_mcp.identity_assurance"
_UNICODE_LINE_SEPARATOR_ESCAPES = {
    0x0085: "\\u0085",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}
_AUDIT_EMITTED = ContextVar("mail_mcp.audit_emitted", default=False)
_MAX_INPUT_BYTES = 16384
_PRIVATE_FIELDS = frozenset({
    "subject", "body", "message", "internalreply", "externalreply", "content",
    "contentbytes", "contentbase64", "base64", "attachmentdata", "attachment", "attachments",
    "filename", "path", "filepath", "localpath", "serverpath", "savedpath",
})
_SECRET_PARTS = (
    "password", "passwd", "secret", "token", "apikey", "accesskey",
    "authorization", "cookie", "credential", "privatekey", "idempotencykey", "signature",
)
_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)


def _json_type(value):
    if value is None:
        return "null"
    for kind, name in ((bool, "boolean"), (str, "string"), (dict, "object"),
                       (list, "array"), (int, "integer"), (float, "number")):
        if isinstance(value, kind):
            return name
    return type(value).__name__


def _snapshot_arguments(arguments):
    """Capture bounded, redacted input before validation or downstream mutation."""
    nodes_left = 400

    def text(value):
        value = _URL_RE.sub("<redacted-url>", value)
        value = _BEARER_RE.sub("Bearer <redacted>", value)
        return value if len(value) <= 512 else value[:512] + "<truncated>"

    def visit(value, field="", depth=0):
        nonlocal nodes_left
        normalized = re.sub(r"[^a-z0-9]", "", field.lower())
        if value is not None and (
            normalized in _PRIVATE_FIELDS or normalized in {"key", "pwd", "sig"}
            or any(part in normalized for part in _SECRET_PARTS)
        ):
            metadata = {"redacted": True, "type": _json_type(value)}
            if isinstance(value, (str, list, dict)):
                metadata["length"] = len(value)
            return metadata
        nodes_left -= 1
        if nodes_left < 0 or depth > 6:
            return {"truncated": True, "type": _json_type(value)}
        if isinstance(value, dict):
            result = {}
            for index, (key, item) in enumerate(value.items()):
                if index >= 100:
                    result["<truncated>"] = {"remaining_fields": len(value) - index}
                    break
                key = key if isinstance(key, str) else "<non-string-key>"
                result[text(key)] = visit(item, key, depth + 1)
            return result
        if isinstance(value, list):
            result = [visit(item, depth=depth + 1) for item in value[:100]]
            if len(value) > 100:
                result.append({"truncated": True, "remaining_items": len(value) - 100})
            return result
        if isinstance(value, str):
            return text(value)
        if value is None or isinstance(value, (bool, int)):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return {"type": _json_type(value)}

    snapshot = visit(arguments)
    serialized = _single_line_json(snapshot).encode("utf-8")
    if len(serialized) <= _MAX_INPUT_BYTES:
        return snapshot
    bounded = {
        "truncated": True,
        "preview": serialized[:_MAX_INPUT_BYTES // 4].decode("utf-8", errors="ignore"),
    }
    if isinstance(snapshot, dict):
        bounded.update({
            key: snapshot[key] for key in ("lanid", "name")
            if isinstance(snapshot.get(key), str)
        })
    return bounded


def _params_dict(arguments):
    return arguments if isinstance(arguments, dict) else {}


def _safe_string(value):
    return value if isinstance(value, str) else None


def _text_length(value):
    if isinstance(value, dict) and value.get("redacted"):
        return value.get("length")
    return len(value) if isinstance(value, str) else None


def _recipient_count(value):
    if not isinstance(value, str):
        return None
    return len([item for item in value.split(",") if item.strip()])


def summarize_arguments(tool_name, arguments):
    params = _params_dict(arguments)
    requester = {
        "lanid": _safe_string(params.get("lanid")),
        # Optional legacy caller label; OA resolution verifies only the LANID.
        "name": _safe_string(params.get("name")),
    }
    if tool_name == "summary_yesterday":
        summary = {"unread_only": params.get("unread_only", False)}
    elif tool_name == "search_mail":
        summary = {
            "query": _safe_string(params.get("query")),
            "limit": params.get("limit", 10),
        }
    elif tool_name == "create_mail_draft":
        summary = {
            "subject_length": _text_length(params.get("subject")),
            "body_length": _text_length(params.get("body")),
            "recipient_count": _recipient_count(params.get("to_emails", "")),
        }
    elif tool_name == "create_meeting":
        summary = {
            "subject_length": _text_length(params.get("subject")),
            "body_length": _text_length(params.get("body")),
            "attendee_count": _recipient_count(params.get("to_emails", "")),
            "has_start_date": bool(params.get("start_date")),
            "has_end_date": bool(params.get("end_date")),
        }
    elif tool_name == "list_meetings":
        summary = {"num_days": params.get("num_days", 10)}
    else:
        summary = {}
    return requester, summary


def create_audit_logger():
    logger = logging.getLogger("mail_mcp.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _single_line_json(record):
    return json.dumps(
        record,
        ensure_ascii=False,
        separators=(",", ":"),
    ).translate(_UNICODE_LINE_SEPARATOR_ESCAPES).encode(
        "utf-8", errors="backslashreplace",
    ).decode("utf-8")


def _classify_result(result):
    root = getattr(result, "root", result)
    structured = getattr(root, "structured_content", None)
    if structured is None:
        structured = getattr(root, "structuredContent", None)

    if (
        isinstance(structured, dict)
        and structured.get("status") == "processing"
        and structured.get("wait_timed_out") is True
    ):
        status = "processing"
        error_code = None
    elif isinstance(structured, dict) and structured.get("ok") is False:
        error_code = structured.get("error_code")
        status = (
            "rejected"
            if str(error_code).startswith("IDENTITY_")
            else "failure"
        )
    elif getattr(root, "isError", False) is True:
        status = "failure"
        error_code = "MCP_TOOL_ERROR"
    else:
        status = "success"
        error_code = None

    values = (
        structured.get("results")
        if isinstance(structured, dict)
        else None
    )
    result_count = len(values) if isinstance(values, list) else None
    return status, error_code, result_count


def _emit_audit_record(
    logger,
    *,
    tool_name,
    arguments,
    assurance,
    status,
    error_code,
    result_count,
    event_id,
    started_at,
    started,
):
    requester, input_summary = summarize_arguments(tool_name, arguments)
    record = {
        "event_type": "mcp_tool_audit",
        "schema_version": 1,
        "event_id": event_id,
        "timestamp": started_at.isoformat(),
        "tool": tool_name,
        **requester,
        "identity_assurance": assurance or "not_verified",
        "status": status,
        "error_code": error_code,
        "duration_ms": round((time.perf_counter() - started) * 1000),
        "input_summary": input_summary,
        "input_arguments": arguments,
    }
    if result_count is not None:
        record["result_count"] = result_count
    logger.info(_single_line_json(record), extra={
        'audit_status': status, 'audit_tool': tool_name,
        'audit_error_code': error_code, 'audit_event_id': event_id,
    })


class ToolAuditMiddleware(Middleware):
    def __init__(self, logger=None):
        self.logger = logger or create_audit_logger()

    async def on_call_tool(self, context, call_next):
        event_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        tool_name = context.message.name
        arguments = _snapshot_arguments(context.message.arguments)
        fastmcp_context = context.fastmcp_context
        if fastmcp_context is not None:
            fastmcp_context.set_state(
                AUDIT_IDENTITY_STATE_KEY,
                "not_verified",
            )

        error_code = None
        status = "error"
        result_count = None
        try:
            result = await call_next(context)
            status, error_code, result_count = _classify_result(result)
            return result
        except Exception:
            error_code = "TOOL_CALL_ERROR"
            raise
        finally:
            assurance = (
                fastmcp_context.get_state(AUDIT_IDENTITY_STATE_KEY)
                if fastmcp_context is not None
                else "not_verified"
            )
            _emit_audit_record(
                self.logger,
                tool_name=tool_name,
                arguments=arguments,
                assurance=assurance,
                status=status,
                error_code=error_code,
                result_count=result_count,
                event_id=event_id,
                started_at=started_at,
                started=started,
            )
            _AUDIT_EMITTED.set(True)


def install_tool_audit_fallback(fastmcp, middleware):
    """审计 FastMCP 在工具中间件之前终止的 tools/call。"""

    low_level_server = fastmcp._mcp_server
    marker = "_mail_mcp_audit_fallback_installed"
    if getattr(low_level_server, marker, False):
        return

    original_handler = low_level_server.request_handlers[
        mcp_types.CallToolRequest
    ]

    async def audited_handler(request):
        token = _AUDIT_EMITTED.set(False)
        event_id = str(uuid.uuid4())
        started_at = datetime.now(timezone.utc)
        started = time.perf_counter()
        arguments = _snapshot_arguments(request.params.arguments)
        result = None
        raised = False
        try:
            result = await original_handler(request)
            return result
        except Exception:
            raised = True
            raise
        finally:
            try:
                if not _AUDIT_EMITTED.get():
                    if raised:
                        status = "error"
                        error_code = "TOOL_CALL_ERROR"
                        result_count = None
                    else:
                        status, error_code, result_count = _classify_result(
                            result
                        )
                    _emit_audit_record(
                        middleware.logger,
                        tool_name=request.params.name,
                        arguments=arguments,
                        assurance="not_verified",
                        status=status,
                        error_code=error_code,
                        result_count=result_count,
                        event_id=event_id,
                        started_at=started_at,
                        started=started,
                    )
                    _AUDIT_EMITTED.set(True)
            finally:
                _AUDIT_EMITTED.reset(token)

    low_level_server.request_handlers[
        mcp_types.CallToolRequest
    ] = audited_handler
    setattr(low_level_server, marker, True)
