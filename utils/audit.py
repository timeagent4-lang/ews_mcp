"""单次 MCP 请求上下文及开始／结束两条安全 JSON 日志。"""

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
import re
import sys
import time
import uuid

from fastmcp.server.middleware import Middleware
from mcp import types as mcp_types


_REQUEST = ContextVar("mail_mcp.request", default=None)
_LINE_SEPARATORS = {0x0085: "\\u0085", 0x2028: "\\u2028", 0x2029: "\\u2029"}
_TOOLS = frozenset({
    "list_folders", "find_message", "get_message", "get_thread", "get_attachment",
    "prepare_attachment_download", "get_mailbox_overview", "create_draft", "update_draft",
    "delete_draft", "send_draft", "set_message_flag", "list_flagged_messages",
    "update_messages", "move_messages", "delete_messages", "list_events", "get_event",
    "create_event", "update_event", "respond_to_event", "cancel_event", "find_people",
    "get_contact", "create_contact", "list_contact_folders", "list_contacts",
    "update_contact", "get_server_status",
})
_OBJECT_ID_FIELDS = frozenset({
    "message_id", "event_id", "draft_id", "contact_id", "folder_id", "attachment_id",
    "reply_to", "ids",
})
_WRITE_FIELDS = frozenset({
    "mode", "subject", "body", "body_format", "body_action", "to_emails", "cc_emails",
    "bcc_emails", "flag", "due_date", "set_read", "categories_add", "categories_remove",
    "to_folder", "start", "end", "location", "attendees", "send_invitations",
    "notify_attendees", "response", "message",
})
_CONTACT_FIELDS = frozenset({
    "display_name", "given_name", "middle_name", "surname", "initials", "nickname",
    "generation", "file_as", "file_as_mapping", "company_name", "companies", "department",
    "job_title", "office", "manager", "assistant_name", "profession", "business_homepage",
    "mileage", "spouse_name", "children", "birthday", "wedding_anniversary",
    "email_addresses", "phone_numbers", "physical_addresses", "postal_address_index",
    "im_addresses", "categories", "notes", "photo", "subject", "sensitivity", "importance",
    "culture", "reminder_is_set", "reminder_due_by", "reminder_minutes_before_start",
})
_INDEX_LABELS = {
    "email_addresses": frozenset({"EmailAddress1", "EmailAddress2", "EmailAddress3"}),
    "im_addresses": frozenset({"ImAddress1", "ImAddress2", "ImAddress3"}),
    "phone_numbers": frozenset({
        "AssistantPhone", "BusinessFax", "BusinessPhone", "BusinessPhone2", "Callback",
        "CarPhone", "CompanyMainPhone", "HomeFax", "HomePhone", "HomePhone2", "Isdn",
        "MobilePhone", "OtherFax", "OtherTelephone", "Pager", "PrimaryPhone", "RadioPhone",
        "Telex", "TtyTddPhone",
    }),
    "physical_addresses": frozenset({"Business", "Home", "Other"}),
}
_ADDRESS_FIELDS = frozenset({"street", "city", "state", "country", "zipcode"})
_SECRET_FIELDS = frozenset({
    "username", "password", "body", "message", "query", "aqs", "subject", "contact",
    "to_emails", "cc_emails", "bcc_emails", "attendees",
})
_SECRET_ENV = (
    "OUTLOOK_ADMIN_PASSWORD", "OUTLOOK_ADMIN_LANID", "OUTLOOK_ADMIN_USERNAME", "EWS_MCP_API_KEY",
)


@dataclass
class _RequestState:
    request_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    secrets: tuple[str, ...] = field(default=(), repr=False)
    started: float = field(default_factory=time.perf_counter)
    logged_phases: set[str] = field(default_factory=set)
    metadata: dict = field(default_factory=dict)


def _strings(value):
    pending, seen = [value], set()
    while pending:
        item = pending.pop()
        if isinstance(item, str):
            if item:
                yield item
        elif isinstance(item, (dict, list, tuple)) and id(item) not in seen:
            seen.add(id(item))
            pending.extend(item.values() if isinstance(item, dict) else item)


@contextmanager
def request_scope(arguments=None):
    """复用当前请求；最外层退出时释放仅在内存中的凭据和资料。"""
    existing = _REQUEST.get()
    if existing is not None:
        yield existing
        return
    params = arguments if isinstance(arguments, dict) else {}
    values = {os.getenv(name, "") for name in _SECRET_ENV}
    for name in _SECRET_FIELDS:
        values.update(_strings(params.get(name)))
    values.discard("")
    state = _RequestState(secrets=tuple(sorted(values, key=len, reverse=True)))
    token = _REQUEST.set(state)
    try:
        yield state
    finally:
        _REQUEST.reset(token)


def current_request_id() -> str | None:
    state = _REQUEST.get()
    return state.request_id if state is not None else None


def request_secrets() -> tuple[str, ...]:
    state = _REQUEST.get()
    return state.secrets if state is not None else ()


def _safe_identifier(value):
    if not isinstance(value, str):
        return None
    for secret in request_secrets():
        value = value.replace(secret, "<redacted>")
    return value[:1024]


def _safe_code(value):
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,127}", value):
        return _safe_identifier(value)
    return None


def _modified_fields(tool_name, params):
    fields = []
    if tool_name in {"create_contact", "update_contact"}:
        contact = params.get("contact")
        if isinstance(contact, dict):
            for name in sorted(_CONTACT_FIELDS.intersection(contact)):
                path = "contact." + name
                value = contact[name]
                if name not in _INDEX_LABELS or not isinstance(value, dict) or not value:
                    fields.append(path)
                    continue
                for label in sorted(_INDEX_LABELS[name].intersection(value)):
                    indexed_path = path + "." + label
                    address = value[label]
                    if name == "physical_addresses" and isinstance(address, dict) and address:
                        fields.extend(
                            indexed_path + "." + key for key in sorted(_ADDRESS_FIELDS.intersection(address))
                        )
                    else:
                        fields.append(indexed_path)
    elif isinstance(tool_name, str) and tool_name.startswith(
        ("create_", "update_", "set_", "move_", "respond_", "cancel_")
    ):
        fields = sorted(_WRITE_FIELDS.intersection(params))
    return fields


def summarize_arguments(tool_name, arguments):
    """只提取已知字段名和对象 ID，从不返回全文参数或资料值。"""
    params = arguments if isinstance(arguments, dict) else {}
    mailbox = params.get("mailbox")
    mailbox = mailbox.strip().lower() if isinstance(mailbox, str) else None
    if mailbox is not None and (len(mailbox) > 320 or not re.fullmatch(r"[^@\s]+@[^@\s]+", mailbox)):
        mailbox = None
    password = params.get("password")
    mode = "personal" if isinstance(password, str) and password != "" else "delegate"
    object_ids = {}
    for name in sorted(_OBJECT_ID_FIELDS.intersection(params)):
        value = params[name]
        if name == "ids" and isinstance(value, list):
            object_ids[name] = [_safe_identifier(item) for item in value[:50] if isinstance(item, str)]
        elif isinstance(value, str):
            object_ids[name] = _safe_identifier(value)
    return {
        "tool": tool_name if isinstance(tool_name, str) and tool_name in _TOOLS else "unknown_tool",
        "mailbox": mailbox,
        "credential_mode": mode,
        "object_ids": object_ids,
        "modified_fields": _modified_fields(tool_name, params),
    }


def create_audit_logger():
    logger = logging.getLogger("mail_mcp.audit")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    return logger


def _result_details(result):
    root = getattr(result, "root", result)
    structured = getattr(root, "structured_content", None)
    if structured is None:
        structured = getattr(root, "structuredContent", None)
    structured = structured if isinstance(structured, dict) else {}
    status = structured.get("status")
    if not isinstance(status, str) or status not in {"success", "failed", "partial", "unknown"}:
        if structured.get("partial") is True:
            status = "partial"
        elif structured.get("ok") is False or getattr(root, "isError", False) is True:
            status = "failed"
        else:
            status = "success"
    details = {"status": status}
    for name in ("error_code", "exchange_code", "error_code_normalized"):
        code = _safe_code(structured.get(name))
        if code is not None:
            details[name] = code
    if status == "failed" and "error_code" not in details and getattr(root, "isError", False):
        details["error_code"] = "MCP_TOOL_ERROR"
    values = structured.get("results")
    if isinstance(values, dict):
        result_id = _safe_identifier(values.get("id"))
        if result_id:
            details["result_id"] = result_id
        rows = values.get("results")
    else:
        rows = values
    if isinstance(rows, list):
        details["result_count"] = len(rows)
        if any(isinstance(row, dict) and ("success" in row or "status" in row) for row in rows):
            counts = {"success_count": 0, "failed_count": 0, "unknown_count": 0}
            for row in rows:
                if not isinstance(row, dict):
                    continue
                if row.get("status") == "unknown":
                    counts["unknown_count"] += 1
                elif row.get("success") is True or row.get("status") == "success":
                    counts["success_count"] += 1
                elif row.get("success") is False or row.get("status") == "failed":
                    counts["failed_count"] += 1
            details.update(counts)
            errors = []
            for row in rows[:50]:
                if not isinstance(row, dict) or not (
                    row.get("success") is False or row.get("status") in ("failed", "unknown")
                ):
                    continue
                item = {"status": "unknown" if row.get("status") == "unknown" else "failed"}
                item_id = _safe_identifier(row.get("id"))
                if item_id is not None:
                    item["id"] = item_id
                for name in ("error_code", "exchange_code", "error_code_normalized"):
                    code = _safe_code(row.get(name))
                    if code is not None:
                        item[name] = code
                errors.append(item)
            if errors:
                details["item_errors"] = errors
    for name in ("success_count", "failed_count", "unknown_count", "result_count"):
        value = structured.get(name)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            details[name] = value
    source_errors = structured.get("source_errors")
    if source_errors is None and isinstance(values, dict):
        source_errors = values.get("source_errors")
    if isinstance(source_errors, dict):
        safe_errors = []
        for source in ("gal", "contacts", "inbox", "sent", "drafts", "calendar", "deleteditems"):
            error = source_errors.get(source)
            if not isinstance(error, dict):
                continue
            entry = {"source": source}
            for name in ("error_code", "exchange_code"):
                code = _safe_code(error.get(name))
                if code is not None:
                    entry[name] = code
            safe_errors.append(entry)
        if safe_errors:
            details["source_errors"] = safe_errors
    return details


def _emit(logger, state, phase, tool_name, arguments, *, result=None, exception=None):
    if phase in state.logged_phases:
        return
    if phase == "start":
        state.metadata = summarize_arguments(tool_name, arguments)
    record = {
        "event_type": "mcp_tool_audit", "schema_version": 2,
        "phase": phase, "request_id": state.request_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **state.metadata,
        "status": "started" if phase == "start" else "failed",
        "duration_ms": round((time.perf_counter() - state.started) * 1000),
    }
    if phase == "finish":
        if exception is not None:
            record.update(error_code="TOOL_CALL_ERROR", exception_type=type(exception).__name__)
        else:
            record.update(_result_details(result))
    logger.info(json.dumps(record, ensure_ascii=False, separators=(",", ":")).translate(_LINE_SEPARATORS))
    state.logged_phases.add(phase)


class ToolAuditMiddleware(Middleware):
    def __init__(self, logger=None):
        self.logger = logger or create_audit_logger()

    async def on_call_tool(self, context, call_next):
        arguments = context.message.arguments or {}
        tool_name = context.message.name
        with request_scope(arguments) as state:
            _emit(self.logger, state, "start", tool_name, arguments)
            result, error = None, None
            try:
                result = await call_next(context)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                _emit(self.logger, state, "finish", tool_name, arguments, result=result, exception=error)


def install_tool_audit_fallback(fastmcp, middleware):
    """底层初始化请求并兜底记录工具中间件之前的拒绝；同阶段不重复。"""
    low_level = fastmcp._mcp_server
    marker = "_mail_mcp_audit_fallback_installed"
    if getattr(low_level, marker, False):
        return
    original = low_level.request_handlers[mcp_types.CallToolRequest]

    async def audited_handler(request):
        arguments = request.params.arguments or {}
        tool_name = request.params.name
        with request_scope(arguments) as state:
            _emit(middleware.logger, state, "start", tool_name, arguments)
            result, error = None, None
            try:
                result = await original(request)
                return result
            except BaseException as exc:
                error = exc
                raise
            finally:
                _emit(middleware.logger, state, "finish", tool_name, arguments, result=result, exception=error)

    low_level.request_handlers[mcp_types.CallToolRequest] = audited_handler
    setattr(low_level, marker, True)
