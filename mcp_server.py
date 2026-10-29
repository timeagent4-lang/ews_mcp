"""OA+DELEGATE server with persistent write confirmation and receipt replay."""

import atexit
import asyncio
import json
import logging
import os
import re
import secrets
import sys
import threading
import time
import uuid
from datetime import datetime, timezone

import mcp.types as mcp_types
from anyio import to_thread
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import FunctionTool, ParsedFunction, ToolResult
from jsonschema import Draft202012Validator
from starlette.middleware import Middleware as HTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

from config import OutlookConfig, UploadSettings, data_dir, preview_ttl_seconds
from attachment_downloads import download_tool_result, register_download_route
from attachment_uploads import AttachmentUploadStore, attachment_lifespan, register_upload_route, MAX_ATTACHMENTS_IN_FLIGHT
from confirmation import (
    STATUS_COMPLETED,
    STATUS_EXECUTING,
    STATUS_FAILED,
    STATUS_PARTIAL,
    STATUS_PENDING,
    STATUS_UNKNOWN,
    ConfirmationError,
    configure_store,
    get_store,
)
from outlook_client import OutlookClient
import team_directories
from tool_specs import READ_TOOLS, SPECS, TOOL_NAMES
from tool_support import ToolOperationError, is_definite_rejection, require_confirmation, sanitize
from utils.audit import (
    AUDIT_IDENTITY_STATE_KEY,
    ToolAuditMiddleware,
    install_tool_audit_fallback,
    current_audit_context,
    update_audit_context,
    emit_event,
    exception_for_log,
    result_for_log,
    _classify_result,
)
from utils.lanid_email import (
    IdentityResolutionError,
    normalize_lanid,
    resolve_identity_by_lanid,
)
from utils.logging_config import configure_logging
from utils.tool_results import ACTIVE_STATUSES, json_result, result_is_error

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("EwsOutlookServer", lifespan=attachment_lifespan)
_ATTACHMENT_SLOTS = threading.BoundedSemaphore(MAX_ATTACHMENTS_IN_FLIGHT)
_DRAFT_WRITES = frozenset(('update_draft', 'send_draft', 'attach_draft_attachment'))
tool_audit = ToolAuditMiddleware()
mcp.add_middleware(tool_audit)


def _audit_event(event_type, **fields):
    """Observation must never change a submitted business action's outcome."""
    try:
        return emit_event(event_type, logger=tool_audit.logger, **fields)
    except Exception:
        try:
            sys.stderr.write('EWS_LOG_WRITE_FAILED event_emission\n')
        except Exception:
            pass

CONTROL_FIELDS = frozenset(("lanid", "name", "operation_id", "confirm_token", "idempotency_key"))
_INPUT_VALIDATORS = {
    name: Draft202012Validator(spec["inputSchema"]) for name, spec in SPECS.items()
}


_SCHEMA_ERROR_MESSAGES = {
    "INVALID_DATETIME": "时间格式无效，请使用 ISO 日期时间。",
    "INVALID_PAGINATION": "分页参数无效，请检查 offset／limit 的类型与范围。",
    "INVALID_FOLDER": "文件夹参数无效，请使用 list_folders 返回的别名。",
    "INVALID_BODY_FORMAT": "正文格式无效：body_format 仅支持 text 或 html（create_draft 回复／转发的 html 正文仅接受新增片段；update_draft 的 html 用于 replace 整体替换或 prepend 前置片段，但 prepend+html 要求目标草稿本身已是 HTML）。",
    "INVALID_BODY_ACTION": "正文处理方式无效：body_action 仅支持 replace 或 prepend。",
    "BODY_PARAM_REQUIRES_BODY": "body_format/body_action 仅在提供 body 时生效；只改收件人或主题时请省略 body 及这两个参数。",
    "INVALID_PARAMS": "参数不符合公开 schema：请检查必填字段、类型、范围及预览／确认／仅回执查询的参数组合。",
}


def _schema_error_code(schema, error):
    """Use field x-error-code annotations; other schema failures stay INVALID_PARAMS."""
    node = schema
    for part in error.absolute_path:
        properties = node.get("properties") if isinstance(node, dict) else None
        node = (properties or {}).get(str(part))
        if node is None:
            return "INVALID_PARAMS"
    code = node.get("x-error-code") if isinstance(node, dict) else None
    return code if code in _SCHEMA_ERROR_MESSAGES else "INVALID_PARAMS"


def _validate_arguments(tool_name, arguments):
    validator = _INPUT_VALIDATORS.get(tool_name)
    if validator is None:
        raise ToolOperationError("UNKNOWN_TOOL", "该工具未注册或已禁用。")
    errors = list(validator.iter_errors(arguments))
    error = errors[0] if errors else None
    if error is not None:
        if isinstance(arguments, dict):
            if "lanid" not in arguments:
                raise ToolOperationError("INVALID_PARAMS", "缺少必填参数 lanid，请传入当前登录用户的 LANID")
            extra = sorted(set(arguments) - set(validator.schema["properties"]))
            if extra:
                raise ToolOperationError("INVALID_PARAMS", "请求包含不支持的参数")
            # Prefer the field constraint over a nested branch-combination error.
            error = next((e for e in errors if e.absolute_path), error)
        code = _schema_error_code(validator.schema, error)
        raise ToolOperationError(code, _schema_error_message(validator.schema, error, arguments))


def _schema_error_message(schema, error, arguments):
    """Describe constraints without echoing invalid values or credentials."""
    if error.absolute_path:
        path = list(error.absolute_path)
        field = str(path[0]) + "".join(f"[{part}]" for part in path[1:])
        rule = error.schema
        if error.validator == "type":
            label = {"integer": "整数", "string": "字符串", "boolean": "布尔值", "array": "数组"}.get(rule.get("type"), "有效类型")
            return f"参数 {field} 必须是{label}"
        if error.validator in ("minimum", "maximum"):
            if "minimum" in rule and "maximum" in rule:
                return f"参数 {field} 必须在 {rule['minimum']}–{rule['maximum']} 之间"
            if "minimum" in rule:
                return f"参数 {field} 必须大于或等于 {rule['minimum']}"
            return f"参数 {field} 必须小于或等于 {rule['maximum']}"
        if error.validator == "minLength":
            return f"参数 {field} 不能为空" if rule.get("minLength") == 1 else f"参数 {field} 长度不足"
        if error.validator == "enum":
            return f"参数 {field} 必须是 " + "、".join(map(str, rule["enum"]))
        return f"参数 {field} 不符合要求"
    if isinstance(arguments, dict):
        if "oneOf" in schema and ("operation_id" in arguments or "confirm_token" in arguments):
            return "确认执行需要 operation_id、confirm_token 和原预览业务参数"
        if error.validator == "required":
            return "缺少必填参数 " + "、".join(key for key in error.validator_value if key not in arguments)
        required = schema.get("required", [])
        if "oneOf" in schema:
            required = required + schema["oneOf"][0]["required"]
        missing = [key for key in required if key not in arguments]
        if missing:
            return "缺少必填参数 " + "、".join(missing)
        for child in error.context:
            if child.validator == "required":
                missing = [key for key in child.validator_value if key not in arguments]
                if missing:
                    return "缺少必填参数 " + "、".join(missing)
    return _SCHEMA_ERROR_MESSAGES[_schema_error_code(schema, error)]


class ToolInputValidationMiddleware(Middleware):
    """Validate raw arguments inside the existing audit chain, before coercion."""

    async def on_call_tool(self, context, call_next):
        try:
            _validate_arguments(context.message.name, context.message.arguments)
        except ToolOperationError as exc:
            return _tool_failure(context.message.name, exc=exc)
        return await call_next(context)


# Audit wraps validation so rejected calls also reach its result handling.
mcp.add_middleware(ToolInputValidationMiddleware())

SEND_RESPONSE_WAIT_SECONDS = 30
# Retain dispatch tasks after the caller stops waiting. Their worker capacity and
# receipt persistence remain active until the synchronous dispatch really ends.
_SEND_DISPATCH_TASKS = set()


def install_failure_envelope(fastmcp) -> None:
    """Mirror structured results as JSON text and derive MCP isError.

    FastMCP's raise path drops structuredContent, so wrap the low-level result.
    """
    low_level_server = fastmcp._mcp_server
    marker = "_mail_mcp_failure_envelope_installed"
    if getattr(low_level_server, marker, False):
        return


    original_handler = low_level_server.request_handlers[mcp_types.CallToolRequest]

    async def envelope_handler(request):
        result = await original_handler(request)
        root = getattr(result, "root", None)
        structured = getattr(root, "structuredContent", None)
        if isinstance(structured, dict):
            root.isError = result_is_error(structured)
            root.content = [mcp_types.TextContent(
                type="text", text=json.dumps(structured, ensure_ascii=False, separators=(",", ":")))]
        return result

    low_level_server.request_handlers[
        mcp_types.CallToolRequest
    ] = envelope_handler
    setattr(low_level_server, marker, True)


def _set_identity_assurance(ctx: Context, value: str) -> None:
    if ctx is not None:
        ctx.set_state(AUDIT_IDENTITY_STATE_KEY, value)


def _resolve_mailbox(params: dict, ctx: Context | None) -> str:
    """Resolve the LANID through OA on every call, including receipt queries."""
    _audit_event('tool.stage', stage='identity.lookup')
    try:
        identity = resolve_identity_by_lanid(params.get("lanid"))
    except IdentityResolutionError:
        _set_identity_assurance(ctx, "oa_lookup_failed")
        raise
    # Match OutlookConfig normalization: SQLite idempotency scopes use exact strings.
    email = identity.email
    mailbox = email.strip().lower() if isinstance(email, str) else ""
    if not mailbox or "@" not in mailbox:
        raise ValueError("缺少或无效的目标邮箱")
    _set_identity_assurance(ctx, "oa_lanid_resolved")
    update_audit_context(identity_assurance='oa_lanid_resolved',
                         caller={'verified_lanid': requester_lanid(params),
                                 'mailbox': mailbox, 'identity_assurance': 'oa_lanid_resolved'})
    _audit_event('tool.stage', stage='identity.verified',
                 identity_assurance='oa_lanid_resolved', mailbox=mailbox,
                 verified_lanid=requester_lanid(params))
    return mailbox


def requester_lanid(params: dict) -> str:
    """Authorize directories with the OA employee LANID, not config.lanid's service identity.

    Invalid identities return an empty value so directory access stays closed.
    """
    try:
        return normalize_lanid(params.get("lanid")).lower()
    except IdentityResolutionError:
        return ""


def initialize_outlook_client(params: dict, ctx: Context | None, *, mailbox=None) -> OutlookClient:
    """Connect via fixed DELEGATE creds after OA resolution."""
    if mailbox is None:
        mailbox = _resolve_mailbox(params, ctx)
    config = OutlookConfig.from_service_env(mailbox)
    client = OutlookClient(config)
    client.requester_lanid = requester_lanid(params)
    return client


# An unparseable response leaves the submission outcome unknown.
_PARSE_ERROR_NAMES = frozenset(
    {
        "MalformedResponseError",
        "SOAPError",
        "ResponseMessageError",
        "ErrorResponseSchemaValidation",
        "ErrorSchemaValidation",
    }
)
# Categories supplement the raw Exchange ResponseCode.
_NORMALIZED_EXCHANGE_CODES = {
    "ErrorAccessDenied": "EXCHANGE_ACCESS_DENIED",
    "ErrorSendAsDenied": "EXCHANGE_ACCESS_DENIED",
    "ErrorImpersonateUserDenied": "EXCHANGE_ACCESS_DENIED",
    "ErrorItemNotFound": "ITEM_NOT_FOUND",
}
_PROGRAM_ERROR_TYPES = (TypeError, KeyError, AttributeError, IndexError)


def _snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _log_tool_error(tool_name: str, exc: Exception, error_kind: str = None) -> None:
    cause = _exchange_cause(exc)
    _audit_event('tool.error', tool=tool_name, stage='business.error', status='error',
                 is_error=True, error_kind=error_kind,
                 error_code=getattr(exc, 'code', exc.__class__.__name__),
                 exchange_code=cause.__class__.__name__ if cause is not None else None,
                 error=exception_for_log(exc))


def _exchange_cause(exc: Exception):
    """Find the raw Exchange cause behind a tool wrapper."""
    seen = set()
    cause = getattr(exc, "__cause__", None)
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if _classify_exception(cause)[1] == "exchange":
            return cause
        cause = getattr(cause, "__cause__", None)
    return None


def _normalized_code(error_code: str) -> str:
    return _NORMALIZED_EXCHANGE_CODES.get(
        error_code, "EXCHANGE_" + _snake(error_code).upper()
    )


def _classify_exception(exc: Exception):
    """Return (code, kind, message), retaining raw Exchange ResponseCodes."""
    name = exc.__class__.__name__
    if isinstance(exc, IdentityResolutionError):
        return "IDENTITY_LOOKUP_FAILED", "identity", sanitize(str(exc))
    if isinstance(exc, ConfirmationError):
        return exc.code, "confirmation", exc.public_message
    if isinstance(exc, ToolOperationError):
        return exc.code, "tool", exc.public_message
    if isinstance(exc, _PROGRAM_ERROR_TYPES):
        # Check program errors first; their names can also end in Error.
        return "INTERNAL_ERROR", "program", None
    if name == "OAServiceError":
        return "OA_UNAVAILABLE", "upstream", sanitize(str(exc))
    if "timeout" in name.lower():
        return "UPSTREAM_TIMEOUT", "transport", sanitize(str(exc))
    if name in _PARSE_ERROR_NAMES:
        return name, "transport", sanitize(str(exc))
    if name.startswith("Error") or name.endswith("Error"):
        return name, "exchange", sanitize(str(exc))
    if isinstance(exc, ValueError):
        return "CONFIG_ERROR", "config", sanitize(str(exc))
    return "UPSTREAM_ERROR", "upstream", sanitize(str(exc))


def _failure_record(exc: Exception) -> dict:
    """Persist root-cause details for later receipt replay."""
    code, kind, message = _classify_exception(exc)
    record = {"error_code": code, "error_kind": kind}
    if getattr(exc, "request_id", None):
        record["request_id"] = exc.request_id
    if kind == "exchange":
        record["error_code_normalized"] = _normalized_code(code)
    else:
        cause = _exchange_cause(exc)
        if cause is not None:
            record["exchange_code"] = cause.__class__.__name__
            record["exchange_message"] = sanitize(str(cause))
    if message:
        record["message"] = message
    return record


def _tool_failure(
    tool_name: str,
    *,
    exc: Exception = None,
    error_code: str = None,
    results=None,
    operation_id: str = None,
) -> ToolResult:
    error_kind = None
    message = None
    request_id = None
    if exc is not None:
        request_id = getattr(exc, "request_id", None)
        if error_code is None:
            error_code, error_kind, message = _classify_exception(exc)
        elif isinstance(exc, (ToolOperationError, ConfirmationError)):
            message = exc.public_message
            error_kind = (
                "confirmation" if isinstance(exc, ConfirmationError) else "tool"
            )
        _log_tool_error(tool_name, exc, error_kind)

    payload = {
        "error_code": error_code,
        "error_kind": error_kind,
        "results": results,
    }
    if error_kind == "exchange" and error_code:
        payload["error_code_normalized"] = _normalized_code(error_code)
    elif exc is not None:
        cause = _exchange_cause(exc)
        if cause is not None:
            payload["exchange_code"] = cause.__class__.__name__
            payload["exchange_message"] = sanitize(str(cause))
    if message:
        payload["message"] = message
    if request_id:
        payload["request_id"] = request_id
    if operation_id:
        payload["operation_id"] = operation_id

    return json_result(payload)


def _business(params: dict) -> dict:
    return {k: v for k, v in params.items() if k not in CONTROL_FIELDS}


def _wrap_read_result(tool_name: str, result) -> ToolResult:
    if tool_name == "prepare_attachment_download":
        return download_tool_result(result)
    if isinstance(result, dict) and "results" in result and isinstance(result["results"], list):
        return _wrap_batch(tool_name, result)
    if isinstance(result, dict) and "items" in result:
        payload = {
            "results": result["items"],
            **{k: v for k, v in result.items() if k != "items"},
        }
        if payload.get("partial"):
            payload["message"] = "查询部分完成，结果不完整"
        return json_result(payload)
    return json_result({"results": result})


def _wrap_batch(tool_name: str, result: dict) -> ToolResult:
    rows = result["results"]
    failed = sum(row.get("success") is False for row in rows)
    payload = {**result,
               "success_count": len(rows) - failed, "failed_count": failed}
    if failed:
        payload["error_code"] = "BATCH_FAILED" if failed == len(rows) else "BATCH_PARTIAL_FAILURE"
    return json_result(payload)


def _invoke_read(tool_name: str, params: dict, ctx: Context) -> ToolResult:
    try:
        if tool_name == 'prepare_attachment_upload':
            mailbox = _resolve_mailbox(params, ctx)
            uploads = _uploads(get_store())
            result = uploads.prepare(lanid=requester_lanid(params), mailbox=mailbox, **_business(params))
            return download_tool_result(result)
        client = initialize_outlook_client(params, ctx)
        result = getattr(client, tool_name)(**_business(params))
        return _wrap_read_result(tool_name, result)
    except Exception as exc:
        return _tool_failure(tool_name, exc=exc)


def _uploads(store):
    try:
        return AttachmentUploadStore(UploadSettings.from_env(), store)
    except ValueError:
        raise ToolOperationError('UPLOAD_CONFIG_INVALID', '附件上传配置无效，请联系管理员。') from None


def _require_upload_owner(tool_name, record, lanid):
    if tool_name == 'attach_draft_attachment' and record is not None:
        if (record.get('details') or {}).get('requester_lanid') != lanid:
            raise ToolOperationError('OPERATION_SCOPE_MISMATCH', '该上传操作不属于当前员工。')


def _require_record_scope(tool_name, mailbox, record):
    if record is not None and (
        record["tool"] != tool_name
        or record["mailbox"].strip().casefold() != mailbox.strip().casefold()
    ):
        raise ToolOperationError(
            "OPERATION_SCOPE_MISMATCH", "该操作不属于当前员工邮箱或当前工具，无法查询或确认。"
        )


def _require_directory_read(record, lanid) -> None:
    """Recheck read permission on every replay; records can outlive authorization."""
    directory_id = ((record or {}).get("business_params") or {}).get("directory_id")
    if not directory_id:
        return
    spec = team_directories.get_directory(directory_id)
    team_directories.authorize(spec, lanid, write=False)


def _verified_directory_target(record, params):
    """Require current create permission and the exact target resolved by the preview.

    Use raw params because the employee LANID is a control field.
    """
    directory_id = params.get("directory_id")
    if not directory_id:
        return None
    spec = team_directories.get_directory(directory_id)
    team_directories.authorize(spec, requester_lanid(params), write=True)
    persisted = (record["details"] or {}) if record is not None else {}
    if persisted.get("target") != spec.identity():
        raise ConfirmationError(
            "DIRECTORY_TARGET_CHANGED",
            "目标目录配置已变更，本次确认不会执行；请重新预览后再确认。",
        )
    return spec


def _receipt(tool_name: str, record, *, mailbox, lanid=None, replay=False) -> ToolResult:
    _require_record_scope(tool_name, mailbox, record)
    _require_upload_owner(tool_name, record, lanid)
    _require_directory_read(record, lanid)
    update_audit_context(operation_id=record['operation_id'], operation_id_source='verified')
    status = record["status"]
    result = record["result"]
    stored_failure = (
        result
        if isinstance(result, dict)
        and result.get("error_code")
        and status in (STATUS_FAILED, STATUS_UNKNOWN)
        else None
    )
    # Batch verdicts come from per-item results, not a top-level error_code.
    batch = (
        result
        if _is_batch_result(result)
        and result["results"]
        and not stored_failure
        and status in (STATUS_FAILED, STATUS_PARTIAL, STATUS_UNKNOWN)
        else None
    )
    payload = {
        "operation_id": record["operation_id"],
        "status": status,
        "results": None if stored_failure else result,
        "replayed": replay,
    }
    if stored_failure:
        payload["error_code"] = stored_failure["error_code"]
        if stored_failure.get("error_code_normalized"):
            payload["error_code_normalized"] = stored_failure["error_code_normalized"]
        for inherited in ("exchange_code", "exchange_message", "error_kind", "request_id"):
            if stored_failure.get(inherited):
                payload[inherited] = stored_failure[inherited]
        if stored_failure.get("message"):
            payload["message"] = stored_failure["message"]
    elif batch is not None:
        rows = batch["results"]
        unknown = sum(row.get("status") == STATUS_UNKNOWN for row in rows)
        failed = sum(row.get("success") is False and row.get("status") != STATUS_UNKNOWN for row in rows)
        payload["error_code"] = (
            "BATCH_OUTCOME_UNKNOWN" if unknown else
            "BATCH_FAILED" if failed == len(rows) else "BATCH_PARTIAL_FAILURE"
        )
        payload["success_count"] = len(rows) - failed - unknown
        payload["failed_count"] = failed
        payload["unknown_count"] = unknown
    elif status == STATUS_PARTIAL:
        payload["error_code"] = "BATCH_PARTIAL_FAILURE"
    elif status in (STATUS_UNKNOWN, STATUS_FAILED):
        payload["error_code"] = "OPERATION_UNKNOWN" if status == STATUS_UNKNOWN else "OPERATION_FAILED"
    if status == STATUS_COMPLETED:
        note = None
    elif batch is not None and payload.get("unknown_count"):
        note = "有条目结果未知，请先核实其实际状态，勿重试这些条目或整批重发；仅确定未执行的失败条目可重试。"
    elif status == STATUS_PARTIAL:
        note = "部分成功：请仅对失败条目重试，勿整批重发。"
    elif status == STATUS_FAILED:
        note = (
            "全部失败：请仅对失败条目重试，勿整批重发。"
            if batch is not None
            else "执行失败：请查询原操作号确认后重试。"
        )
    elif status == STATUS_PENDING:
        note = "等待确认执行。"
    elif status in ACTIVE_STATUSES:
        note = "操作尚未取得最终结果。"
    elif tool_name == "send_draft":
        note = "发送结果未知，请在已发送邮件中核实实际结果；勿盲目重发。"
    else:
        note = "执行结果未知，请核实实际结果；勿盲目重试。"
    structured = {**payload, **({"note": note} if note else {})}
    return json_result(structured)

def _query_receipt(tool_name: str, operation_id: str, mailbox: str, lanid=None) -> ToolResult:
    record = get_store().get(operation_id)
    if record is None:
        return _tool_failure(
            tool_name, exc=ConfirmationError("OPERATION_NOT_FOUND", "未找到 operation_id 对应的操作记录"), results=None,
            operation_id=operation_id,
        )
    return _receipt(tool_name, record, mailbox=mailbox, lanid=lanid)


def _preview_expired(record) -> bool:
    """Treat invalid timestamps as expired rather than issuing an unverifiable token."""
    ttl = preview_ttl_seconds()
    if ttl <= 0:
        return False
    try:
        created = datetime.fromisoformat(record["created_at"])
    except (TypeError, ValueError):
        return True
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - created).total_seconds() > ttl


def _require_unexpired_preview(record):
    if record is not None and record["status"] == STATUS_PENDING and _preview_expired(record):
        raise ConfirmationError(
            "PREVIEW_EXPIRED",
            "该预览已过期且未执行；请重新预览获取新的 operation_id 与令牌。",
        )


def _preview_replay(tool_name: str, record, *, mailbox, lanid=None) -> ToolResult:
    """Reuse the valid preview's token without executing the write."""
    _require_record_scope(tool_name, mailbox, record)
    _require_upload_owner(tool_name, record, lanid)
    _require_directory_read(record, lanid)
    update_audit_context(operation_id=record['operation_id'], operation_id_source='verified')
    details = record["details"]
    if tool_name == 'attach_draft_attachment' and isinstance(details, dict):
        # The storage deadline is not part of the original user-visible preview.
        details = {key: value for key, value in details.items() if key != 'expires_at'}
    preview = require_confirmation(mailbox=record["mailbox"], action=record["action"],
                                   items=record["items"], details=details)
    return json_result({
            "status": STATUS_PENDING,
            "confirmation_required": True,
            "operation_id": record["operation_id"],
            "confirm_token": record["confirm_token"],
            "preview": preview,
            "replayed": True,
        })


def _is_batch_result(result) -> bool:
    return isinstance(result, dict) and isinstance(result.get("results"), list)


def _classify_write_result(result):
    if _is_batch_result(result):
        rows = result["results"]
        if any(row.get("status") == STATUS_UNKNOWN for row in rows):
            return STATUS_UNKNOWN, result
        failed = sum(row.get("success") is False for row in rows)
        if failed == 0:
            return STATUS_COMPLETED, result
        if failed == len(rows):
            return STATUS_FAILED, result
        return STATUS_PARTIAL, result
    return STATUS_COMPLETED, result


def _run_write(tool_name: str, params: dict, ctx: Context) -> ToolResult:
    mailbox = _resolve_mailbox(params, ctx)
    lanid = requester_lanid(params)
    store = get_store()
    operation_id = params.get("operation_id")
    confirm_token = params.get("confirm_token")
    idem = params.get("idempotency_key")
    business = _business(params)
    if tool_name == 'attach_draft_attachment' and not (operation_id and not confirm_token):
        business.setdefault('is_inline', False)

    if operation_id and not confirm_token:
        return _query_receipt(tool_name, operation_id, mailbox, lanid=lanid)

    # Confirm tokens bypass idempotency replay so a pending preview can execute.
    if idem and not confirm_token:
        record = store.find_by_idempotency_key(idem, mailbox=mailbox)
        if record is not None:
            _require_record_scope(tool_name, mailbox, record)
            _require_upload_owner(tool_name, record, lanid)
            if tool_name == 'attach_draft_attachment' and business != record['business_params']:
                raise ConfirmationError('CONFIRMATION_MISMATCH', '幂等键对应不同附件参数，请使用原操作参数。')
            status = record["status"]
            if status != STATUS_PENDING:
                return _receipt(tool_name, record, mailbox=mailbox, lanid=lanid, replay=True)
            if not _preview_expired(record):
                return _preview_replay(tool_name, record, mailbox=mailbox, lanid=lanid)
            # Expired previews create a new operation below; never revive the old record.

    if confirm_token:
        existing = store.get(operation_id)
        if existing is None:
            raise ConfirmationError("OPERATION_NOT_FOUND", "未找到 operation_id 对应的操作记录")
        _require_record_scope(tool_name, mailbox, existing)
        _require_upload_owner(tool_name, existing, lanid)
        update_audit_context(operation_id=operation_id, operation_id_source='verified')
        _audit_event('tool.stage', tool=tool_name, stage='confirmation.check',
                     operation_id=operation_id)
        if existing is not None and (
            not secrets.compare_digest(confirm_token.encode("utf-8"), existing["confirm_token"].encode("utf-8"))
            or business != existing["business_params"]
        ):
            raise ConfirmationError(
                "CONFIRMATION_MISMATCH", "确认令牌或业务参数与预览不一致，本次未执行。"
            )
        if existing is not None and existing["status"] in (
            STATUS_COMPLETED,
            STATUS_PARTIAL,
            STATUS_UNKNOWN,
            STATUS_EXECUTING,
        ):
            return _receipt(tool_name, existing, mailbox=mailbox, lanid=lanid, replay=True)
        if existing is not None and _is_batch_result(existing["result"]):
            # Replaying an executed batch could duplicate successful items.
            return _receipt(tool_name, existing, mailbox=mailbox, lanid=lanid, replay=True)
        execution = None
        try:
            _require_unexpired_preview(existing)
            # Verify target and rights before claiming; stale confirmations stay pending.
            verified_target = _verified_directory_target(existing, params)
            # Connect before claiming, so connection failure leaves pending.
            # Connection setup may cross the TTL; check again before execution.
            client = initialize_outlook_client(params, ctx, mailbox=mailbox)
            _require_unexpired_preview(existing)
            if tool_name in _DRAFT_WRITES:
                execution = store.begin_draft_confirm(operation_id, confirm_token, business,
                    mailbox=mailbox, lanid=lanid, worker_id=uuid.uuid4().hex)
            else:
                store.begin_confirm(operation_id, confirm_token, business)
        except (ConfirmationError, ToolOperationError) as exc:
            if isinstance(exc, ConfirmationError) and exc.code == "CONFIRMATION_NOT_EXECUTABLE":
                current = store.get(operation_id)
                if current is not None:
                    return _receipt(tool_name, current, mailbox=mailbox, lanid=lanid, replay=True)
            return _tool_failure(tool_name, exc=exc, operation_id=operation_id)
        if execution:
            client.submission_started = False
        _audit_event('tool.stage', tool=tool_name, stage='operation.execute',
                     operation_id=operation_id, status=STATUS_EXECUTING)
        try:
            execute = {**business, "confirm": True, "confirmation_id": confirm_token}
            recorded_version = (existing.get("details") or {}).get("version")
            if recorded_version is not None:
                # Use the server snapshot to reject edits since preview.
                execute["expected_version"] = recorded_version
            if tool_name == 'attach_draft_attachment':
                client.attachment_uploads = _uploads(store)
                client.attachment_operation_id = operation_id
                execute['expected_attachment'] = (existing.get('details') or {}).get('attachment')
            if verified_target is not None:
                # Reuse the verified target to avoid a second, conflicting resolution.
                execute["directory_target"] = verified_target
            needs_slot = tool_name == 'attach_draft_attachment'
            if needs_slot and not _ATTACHMENT_SLOTS.acquire(blocking=False):
                raise ToolOperationError('UPLOAD_BUSY', '附件处理容量已满，请稍后用原操作确认。')
            try:
                result = getattr(client, tool_name)(**execute)
            finally:
                if needs_slot:
                    _ATTACHMENT_SLOTS.release()
        except Exception as exc:
            # Ambiguous writes stay UNKNOWN and are never automatically retried.
            submitted = not execution or client.submission_started
            ambiguous = submitted and not is_definite_rejection(exc)
            status = STATUS_UNKNOWN if ambiguous else STATUS_FAILED
            try:
                if execution:
                    store.finish_draft_operation(execution, status, _failure_record(exc), definitely_not_applied=not ambiguous)
                else:
                    store.mark(operation_id, status, result=_failure_record(exc))
            except Exception as persist_exc:
                logger.error('write_receipt_persistence_failed tool=%s', tool_name)
                _audit_event('operation.finished', tool=tool_name, operation_id=operation_id,
                             origin_call_id=current_audit_context().get('call_id'),
                             stage='receipt.persist_failed', status=STATUS_UNKNOWN,
                             is_error=True, error_code='OPERATION_UNKNOWN',
                             error=exception_for_log(persist_exc))
                return _tool_failure(tool_name, error_code='OPERATION_UNKNOWN', operation_id=operation_id)
            _log_tool_error(tool_name, exc, _classify_exception(exc)[1])
            receipt = _receipt(tool_name, store.get(operation_id), mailbox=mailbox, lanid=lanid)
            _audit_event('operation.finished', tool=tool_name, operation_id=operation_id,
                         origin_call_id=current_audit_context().get('call_id'),
                         stage='receipt.persisted', status=status, is_error=True,
                         error=exception_for_log(exc), result=result_for_log(receipt, tool=tool_name))
            return receipt
        # Keep post-write bookkeeping outside the submission exception handler:
        # a later failure must not make the write retryable.
        status, final_result = _classify_write_result(result)
        try:
            if execution:
                store.finish_draft_operation(execution, status, final_result)
            else:
                store.mark(operation_id, status, result=final_result)
        except Exception as persist_exc:
            logger.error('write_receipt_persistence_failed tool=%s', tool_name)
            _audit_event('operation.finished', tool=tool_name, operation_id=operation_id,
                         origin_call_id=current_audit_context().get('call_id'),
                         stage='receipt.persist_failed', status=STATUS_UNKNOWN,
                         is_error=True, error_code='OPERATION_UNKNOWN',
                         error=exception_for_log(persist_exc))
            return _tool_failure(tool_name, error_code='OPERATION_UNKNOWN', operation_id=operation_id)
        receipt = _receipt(tool_name, store.get(operation_id), mailbox=mailbox, lanid=lanid)
        _audit_event('operation.finished', tool=tool_name, operation_id=operation_id,
                     origin_call_id=current_audit_context().get('call_id'),
                     stage='receipt.persisted', status=status,
                     is_error=result_is_error(receipt.structured_content),
                     result=result_for_log(receipt, tool=tool_name))
        return receipt

    # Preview handlers are read-only; persist their result for confirmation.
    client = initialize_outlook_client(params, ctx, mailbox=mailbox)
    if tool_name == 'attach_draft_attachment':
        client.attachment_uploads = _uploads(store)
    try:
        result = getattr(client, tool_name)(**business, confirm=False)
    except Exception as exc:
        return _tool_failure(tool_name, exc=exc)
    preview = result.get("preview") if isinstance(result, dict) else None
    items = preview.get("items") if isinstance(preview, dict) else None
    details = preview.get("details") if isinstance(preview, dict) else None
    preview_args = dict(
        tool=tool_name,
        mailbox=mailbox,
        action=tool_name,
        business_params=business,
        items=items,
        details=details,
        idempotency_key=idem,
    )
    if tool_name == 'attach_draft_attachment':
        for key in ('tool', 'action'):
            preview_args.pop(key)
        record = store.create_attachment_preview(**preview_args, upload_snapshot=details['attachment'],
            canonical_draft_id=details['canonical_draft_id'], expires_at=time.time() + preview_ttl_seconds())
    else:
        record = store.create_preview(**preview_args)
    update_audit_context(operation_id=record['operation_id'], operation_id_source='verified')
    _audit_event('operation.preview_created', tool=tool_name,
                 operation_id=record['operation_id'], status=STATUS_PENDING)
    return json_result({
            "status": STATUS_PENDING,
            "confirmation_required": True,
            "operation_id": record["operation_id"],
            "confirm_token": record["confirm_token"],
            "preview": result,
        })


def _dispatch(tool_name: str, params: dict, ctx: Context) -> ToolResult:
    try:
        _audit_event('tool.stage', tool=tool_name, stage='arguments.validation')
        _validate_arguments(tool_name, params)
        _audit_event('tool.stage', tool=tool_name, stage='business.dispatch')
        if tool_name in READ_TOOLS:
            return _invoke_read(tool_name, params, ctx)
        return _run_write(tool_name, params, ctx)
    except Exception as exc:  # noqa: BLE001 - outermost safety net
        return _tool_failure(tool_name, exc=exc)


def _send_dispatch_done(task, *, context=None, started=None):
    _SEND_DISPATCH_TASKS.discard(task)
    context = context if context is not None else current_audit_context()
    common = dict(context=context, tool='send_draft',
                  origin_call_id=context.get('call_id'),
                  duration_ms=round((time.perf_counter() - started) * 1000) if started is not None else None)
    if task.cancelled():
        _audit_event('dispatch.finished', **common, call_status='cancelled',
                     status='cancelled', is_error=False)
        return
    exc = task.exception()
    if exc is not None:
        logger.error("发送后台处理异常: error_type=%s", exc.__class__.__name__)
        _audit_event('dispatch.finished', **common, call_status='error',
                     status='error', is_error=True, error=exception_for_log(exc))
        return
    result = task.result()
    status, error_code, result_count = _classify_result(result)
    data = getattr(result, 'structured_content', None)
    _audit_event('dispatch.finished', **common, call_status='returned', status=status,
                 is_error=result_is_error(data) if isinstance(data, dict) else bool(error_code),
                 operation_id=data.get('operation_id') if isinstance(data, dict) else None,
                 error_code=error_code, result_count=result_count,
                 result=result_for_log(result, tool='send_draft'))


def _send_wait_expired(params) -> ToolResult:
    logger.warning("send_draft 等待达到上限，后台处理可能继续。")
    operation_id = params.get("operation_id")
    if not operation_id:
        return _tool_failure(
            "send_draft",
            exc=ToolOperationError(
                "RESPONSE_WAIT_TIMEOUT",
                "预览等待超时，本次未执行发送。后台预览可能完成；请重新请求预览，"
                "如已使用 idempotency_key，请沿用原值。",
            ),
        )
    # This is request progress, not a receipt: OA or scope validation may still
    # be pending. Never read stored data here using an unverified operation ID.
    return json_result({
            "status": "processing",
            "operation_id": operation_id,
            "results": None,
            "wait_timed_out": True,
            "message": "邮箱服务不稳定 请五分钟后查看结果",
        })


def _make_handler(tool_name):
    async def handler(ctx: Context = None, **params) -> ToolResult:
        # Keep synchronous OA/Exchange work off the loop; finish writes after cancellation.
        if tool_name != "send_draft":
            return await to_thread.run_sync(_dispatch, tool_name, params, ctx)
        dispatch_started = time.perf_counter()
        task = asyncio.create_task(to_thread.run_sync(_dispatch, tool_name, params, ctx))
        _SEND_DISPATCH_TASKS.add(task)
        # add_done_callback retains this ContextVar carrier, including identity
        # enrichment by the worker after the request has returned processing.
        task.add_done_callback(lambda done: _send_dispatch_done(done, started=dispatch_started))
        try:
            # Timeout the shielded wait, not the dispatch. Keeping its AnyIO
            # limiter token also prevents timed-out sends from spawning extra
            # active worker threads while earlier Exchange requests still run.
            return await asyncio.wait_for(asyncio.shield(task), SEND_RESPONSE_WAIT_SECONDS)
        except TimeoutError:
            return _send_wait_expired(params)

    handler.__name__ = f"tool_{tool_name}"
    return handler


def _register_all():
    for name in TOOL_NAMES:
        spec = SPECS[name]
        parsed = ParsedFunction.from_function(_make_handler(name), validate=False)
        mcp.add_tool(FunctionTool(
            fn=parsed.fn, name=name, description=spec["description"],
            parameters=spec["inputSchema"], output_schema=parsed.output_schema,
        ))


_register_all()
register_download_route(mcp)
register_upload_route(mcp)
install_failure_envelope(mcp)
install_tool_audit_fallback(mcp, tool_audit)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request):
    """Lightweight liveness probe. No OA/Exchange contact, no mailbox/credentials."""
    return JSONResponse(
        {
            "status": "ok",
            "service": "ews_mcp",
            "tool_count": len(TOOL_NAMES),
        }
    )


class MCPKeyAuthMiddleware:
    """Authenticate /mcp only; health checks and signed downloads stay accessible."""

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            request = Request(scope)
            keys = request.query_params.getlist("key")
            if len(keys) != 1 or not secrets.compare_digest(
                keys[0].encode("utf-8"), self.api_key
            ):
                response = JSONResponse(
                    {"error": "Invalid or missing API key"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    configure_logging(os.getenv('EWS_MCP_LOG_DIR', '').strip() or os.path.join(data_dir(), 'logs'))
    store = configure_store(os.path.join(data_dir(), "operations.db"))
    # Close explicitly to checkpoint SQLite's WAL.
    atexit.register(store.close)
    host = os.getenv("EWS_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("EWS_MCP_PORT", "7805"))
    api_key = os.getenv("EWS_MCP_API_KEY", "")
    if not api_key.strip():
        raise ValueError("缺少 EWS_MCP_API_KEY")
    logger.info("EWS MCP listening on %s:%s", host, port)
    _audit_event('service.started', host=host, port=port,
                 log_directory=str(os.path.abspath(os.getenv('EWS_MCP_LOG_DIR', '').strip()
                                                  or os.path.join(data_dir(), 'logs'))),
                 uid=getattr(os, 'getuid', lambda: None)(),
                 gid=getattr(os, 'getgid', lambda: None)())
    atexit.register(_audit_event, 'service.stopping', reason='normal_shutdown')
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path="/mcp",
        middleware=[
            HTTPMiddleware(MCPKeyAuthMiddleware, api_key=api_key),
        ],
        # Avoid writing the key, which travels in the query string, to the log.
        uvicorn_config={"access_log": False, "log_config": None},
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logger.exception('EWS MCP 启动或运行失败')
        raise SystemExit(1)
