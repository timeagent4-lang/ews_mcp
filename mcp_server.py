"""EWS MCP: OA+DELEGATE mail server; tool_specs defines the enabled tool set.

Every tool exposes flat ``arguments`` (lanid/name required). Read tools
dispatch directly; every write tool routes through the persistent two-phase
confirmation (operation_id / confirm_token / idempotency_key). Uncertain
outcomes are never auto-retried; callers query by the original operation_id.
"""

import atexit
import logging
import os
import re
import secrets
from datetime import datetime, timezone

import mcp.types as mcp_types
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import FunctionTool, ParsedFunction, ToolResult
from jsonschema import Draft202012Validator
from starlette.middleware import Middleware as HTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()

from config import OutlookConfig, data_dir, preview_ttl_seconds
from attachment_downloads import download_lifespan, download_tool_result, register_download_route
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
from tool_support import ToolOperationError, is_definite_rejection, sanitize
from utils.audit import (
    AUDIT_IDENTITY_STATE_KEY,
    ToolAuditMiddleware,
    install_tool_audit_fallback,
)
from utils.lanid_email import (
    IdentityNameMismatchError,
    IdentityResolutionError,
    normalize_lanid,
    resolve_identity_by_lanid,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(filename)s:%(lineno)d - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

mcp = FastMCP("EwsOutlookServer", lifespan=download_lifespan)
tool_audit = ToolAuditMiddleware()
mcp.add_middleware(tool_audit)
install_tool_audit_fallback(mcp, tool_audit)

CONTROL_FIELDS = frozenset(("lanid", "name", "operation_id", "confirm_token", "idempotency_key"))
_INPUT_VALIDATORS = {
    name: Draft202012Validator(spec["inputSchema"]) for name, spec in SPECS.items()
}


_SCHEMA_ERROR_MESSAGES = {
    "INVALID_DATETIME": "时间格式无效，请使用 ISO 日期时间。",
    "INVALID_PAGINATION": "分页参数无效，请检查 offset／limit 的类型与范围。",
    "INVALID_FOLDER": "文件夹参数无效，请使用 list_folders 返回的别名。",
    "INVALID_BODY_FORMAT": "正文格式无效：body_format 仅支持 text 或 html（create_draft 的 html 仅限 mode=new；update_draft 的 html 用于 replace 整体替换或 prepend 前置片段，但 prepend+html 要求目标草稿本身已是 HTML）。",
    "INVALID_BODY_ACTION": "正文处理方式无效：body_action 仅支持 replace 或 prepend。",
    "BODY_PARAM_REQUIRES_BODY": "body_format/body_action 仅在提供 body 时生效；只改收件人或主题时请省略 body 及这两个参数。",
    "INVALID_PARAMS": "参数不符合公开 schema：请检查必填字段、类型、范围及预览／确认／仅回执查询的参数组合。",
}


def _schema_error_code(schema, error):
    """Map a JSON-schema violation to a public error code.

    Only fields annotated with ``x-error-code`` in the tool spec keep a
    fine-grained code. Identity fields, unknown properties and the
    preview/confirm/query ``oneOf`` combinations fall through to
    INVALID_PARAMS, so a schema failure is never reported as an identity or a
    business rejection.
    """
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
    error = next(validator.iter_errors(arguments), None)
    if error is not None:
        code = _schema_error_code(validator.schema, error)
        # Never echo the invalid instance: it may contain a confirmation token.
        raise ToolOperationError(code, _SCHEMA_ERROR_MESSAGES[code])


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

# Confirmation-flow receipts that are NOT failures (a preview awaiting confirm,
# or an operation still in flight).
_NON_FAILURE_STATUSES = frozenset(("pending", "confirmed", "executing"))


def install_failure_envelope(fastmcp) -> None:
    """Make genuine business failures come back as ``isError=true`` while
    keeping the structured payload.

    fastmcp's ``ToolResult`` path always yields ``isError=false``, and its only
    ``isError=true`` path (raising) drops ``structuredContent``. The low-level
    server passes a returned ``CallToolResult`` through verbatim, so we
    post-process the result here - the same wrapper point the audit fallback
    already uses.
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
        if (
            isinstance(structured, dict)
            and structured.get("ok") is False
            and structured.get("status") not in _NON_FAILURE_STATUSES
        ):
            try:
                root.isError = True
            except Exception:  # pragma: no cover - defensive; model is mutable
                logger.warning("无法为失败结果设置 isError=true")
        return result

    low_level_server.request_handlers[
        mcp_types.CallToolRequest
    ] = envelope_handler
    setattr(low_level_server, marker, True)


def _set_identity_assurance(ctx: Context, value: str) -> None:
    if ctx is not None:
        ctx.set_state(AUDIT_IDENTITY_STATE_KEY, value)


def _resolve_mailbox(params: dict, ctx: Context | None) -> str:
    """OA-verify every request, including receipt-only calls without Exchange."""
    try:
        identity = resolve_identity_by_lanid(params.get("lanid"), params.get("name"))
    except IdentityNameMismatchError:
        _set_identity_assurance(ctx, "oa_name_mismatch")
        raise
    except IdentityResolutionError:
        _set_identity_assurance(ctx, "oa_lookup_failed")
        raise
    # Match OutlookConfig.from_service_env before any receipt lookup or write:
    # SQLite idempotency scopes compare mailbox strings exactly.
    email = identity.email
    mailbox = email.strip().lower() if isinstance(email, str) else ""
    if not mailbox or "@" not in mailbox:
        raise ValueError("缺少或无效的目标邮箱")
    _set_identity_assurance(ctx, "oa_name_match")
    return mailbox


def requester_lanid(params: dict) -> str:
    """The OA-verified employee LANID, used only to match directory allow-lists.

    ``config.lanid`` is the fixed service account, never an employee identity, so
    it must not be used for authorization. An unusable value returns "" - the
    shared directory then stays closed instead of guessing who is calling.
    """
    try:
        return normalize_lanid(params.get("lanid"))
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


# Errors that mean the server answered but the payload could not be parsed:
# the outcome is unknown, never a definite rejection.
_PARSE_ERROR_NAMES = frozenset(
    {
        "MalformedResponseError",
        "SOAPError",
        "ResponseMessageError",
        "ErrorResponseSchemaValidation",
        "ErrorSchemaValidation",
    }
)
# Stable, category-level codes kept ALONGSIDE the raw Exchange code (never
# instead of it).
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
    if error_kind == "program":
        # Keep the full traceback server-side; callers only get INTERNAL_ERROR.
        logger.exception(
            "%s 执行失败(程序异常): error_type=%s", tool_name, exc.__class__.__name__
        )
        return
    logger.error(
        "%s 执行失败: error_type=%s detail=%s",
        tool_name,
        exc.__class__.__name__,
        sanitize(str(exc), 200),
    )


def _exchange_cause(exc: Exception):
    """The raw Exchange error chained behind a tool-level wrapper, if any.

    A submission failure surfaces as a stable tool code (``SEND_REJECTED``,
    ``CANCEL_OUTCOME_UNKNOWN``) with the real error chained as ``__cause__``.
    That raw error must stay legible rather than only living inside a prose
    message.
    """
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
    """Map an exception to ``(error_code, error_kind, message)``.

    For Exchange errors ``error_code`` is the RAW exchangelib class name (the
    real ResponseCode), verbatim - the categorised form goes in a separate
    field. Program errors are separated out instead of becoming UPSTREAM_ERROR.
    """
    name = exc.__class__.__name__
    if isinstance(exc, IdentityNameMismatchError):
        return "IDENTITY_NAME_MISMATCH", "identity", sanitize(str(exc))
    if isinstance(exc, IdentityResolutionError):
        return "IDENTITY_LOOKUP_FAILED", "identity", sanitize(str(exc))
    if isinstance(exc, ConfirmationError):
        return exc.code, "confirmation", exc.public_message
    if isinstance(exc, ToolOperationError):
        return exc.code, "tool", exc.public_message
    if isinstance(exc, _PROGRAM_ERROR_TYPES):
        # Checked before the name pattern: AttributeError/IndexError also end in
        # "Error" but are program bugs, not Exchange ResponseCodes.
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
    """Compact, replayable root-cause record persisted with a failed operation."""
    code, kind, message = _classify_exception(exc)
    record = {"error_code": code}
    if kind == "exchange":
        record["error_code_normalized"] = _normalized_code(code)
    else:
        # A tool-level wrapper keeps the real ResponseCode in its cause. Persist
        # it, so a later replay of this record cannot hide the actual error.
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
        "ok": False,
        "error_code": error_code,
        "error_kind": error_kind,
        "results": results,
    }
    if error_kind == "exchange" and error_code:
        payload["error_code_normalized"] = _normalized_code(error_code)
    elif exc is not None:
        # A tool-level wrapper must not bury the raw ResponseCode it chained:
        # expose it as its own field so the caller can still branch on it.
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

    content = f"{tool_name} 执行失败: {error_code}"
    if message:
        content += f" - {message}"
    return ToolResult(content=content, structured_content=payload)


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
        return ToolResult(content=f"{tool_name} 执行成功", structured_content=payload)
    return ToolResult(
        content=f"{tool_name} 执行成功", structured_content={"results": result}
    )


def _wrap_batch(tool_name: str, result: dict) -> ToolResult:
    rows = result["results"]
    failed = sum(row.get("success") is False for row in rows)
    payload = {**result, "ok": failed == 0,
               "success_count": len(rows) - failed, "failed_count": failed}
    if failed:
        payload["error_code"] = "BATCH_FAILED" if failed == len(rows) else "BATCH_PARTIAL_FAILURE"
    status = ("执行成功" if failed == 0 else "执行失败" if failed == len(rows) else "部分失败")
    return ToolResult(content=f"{tool_name} {status}", structured_content=payload)


def _invoke_read(tool_name: str, params: dict, ctx: Context) -> ToolResult:
    try:
        client = initialize_outlook_client(params, ctx)
        result = getattr(client, tool_name)(**_business(params))
        return _wrap_read_result(tool_name, result)
    except Exception as exc:
        return _tool_failure(tool_name, exc=exc)


def _require_record_scope(tool_name, mailbox, record):
    if record is not None and (
        record["tool"] != tool_name
        or record["mailbox"].strip().casefold() != mailbox.strip().casefold()
    ):
        raise ToolOperationError(
            "OPERATION_SCOPE_MISMATCH", "该操作不属于当前员工邮箱或当前工具，无法查询或确认。"
        )


def _require_directory_read(record, lanid) -> None:
    """Re-check shared-directory read permission before a stored record is returned.

    A record can outlive the authorization it was created under. Receipts,
    completed replays and idempotency replays all read the directory context from
    the record, so every one of them passes through here: once an employee is
    removed from the allow-list, no replay can hand the shared data back.
    """
    directory_id = ((record or {}).get("business_params") or {}).get("directory_id")
    if not directory_id:
        return
    spec = team_directories.get_directory(directory_id)
    team_directories.authorize(spec, lanid, write=False)


def _verified_directory_target(record, params):
    """Confirm-time re-check of a shared-directory write; returns the target to use.

    Takes the raw ``params`` (not ``_business(params)``), because the LANID that
    the directory allow-list matches on is a control field.

    Two things must still hold when the confirmation is presented: the employee
    keeps create permission, and the configured target is byte-for-byte the one
    the preview resolved. If an administrator has re-pointed the directory (or the
    registration version changed), the old confirmation is refused rather than
    writing into the new folder. ``None`` means "not a shared-directory write".
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
    _require_directory_read(record, lanid)
    status = record["status"]
    result = record["result"]
    if result is None and status not in (STATUS_COMPLETED, STATUS_PARTIAL):
        result = {"status": status}
    ok = status in (STATUS_COMPLETED, STATUS_PARTIAL)
    stored_failure = (
        result
        if isinstance(result, dict)
        and result.get("error_code")
        and status in (STATUS_FAILED, STATUS_UNKNOWN)
        else None
    )
    # A batch write reports success per item, so its failure carries no top-level
    # error_code and would otherwise fall through to OPERATION_UNKNOWN - naming a
    # known outcome as unknown. Keep the item rows and name the batch outcome.
    batch = (
        result
        if _is_batch_result(result)
        and result["results"]
        and not stored_failure
        and status in (STATUS_FAILED, STATUS_PARTIAL)
        else None
    )
    payload = {
        "ok": ok,
        "operation_id": record["operation_id"],
        "status": status,
        "results": None if stored_failure else result,
        "replayed": replay,
    }
    if stored_failure:
        # Replays must keep the SAME root cause as the first failure.
        payload["error_code"] = stored_failure["error_code"]
        if stored_failure.get("error_code_normalized"):
            payload["error_code_normalized"] = stored_failure["error_code_normalized"]
        for inherited in ("exchange_code", "exchange_message"):
            if stored_failure.get(inherited):
                payload[inherited] = stored_failure[inherited]
        if stored_failure.get("message"):
            payload["message"] = stored_failure["message"]
    elif batch is not None:
        rows = batch["results"]
        failed = sum(row.get("success") is False for row in rows)
        payload["error_code"] = (
            "BATCH_FAILED" if failed == len(rows) else "BATCH_PARTIAL_FAILURE"
        )
        payload["success_count"] = len(rows) - failed
        payload["failed_count"] = failed
    elif status == STATUS_PARTIAL:
        payload["error_code"] = "BATCH_PARTIAL_FAILURE"
    elif status not in (STATUS_COMPLETED,):
        payload["error_code"] = "OPERATION_UNKNOWN"
    if status == STATUS_COMPLETED:
        note = None
    elif status == STATUS_PARTIAL:
        note = "部分成功：请仅对失败条目重试，勿整批重发。"
    elif status == STATUS_FAILED:
        note = (
            "全部失败：请仅对失败条目重试，勿整批重发。"
            if batch is not None
            else "执行失败：请查询原操作号确认后重试。"
        )
    else:
        note = "结果未知或未完成：请勿盲目重发，仅查询原操作号。"
    structured = {**payload, **({"note": note} if note else {})}
    return ToolResult(content=f"{tool_name} {status}", structured_content=structured)

def _query_receipt(tool_name: str, operation_id: str, mailbox: str, lanid=None) -> ToolResult:
    record = get_store().get(operation_id)
    if record is None:
        return _tool_failure(
            tool_name, error_code="OPERATION_UNKNOWN", results=None,
            operation_id=operation_id,
        )
    return _receipt(tool_name, record, mailbox=mailbox, lanid=lanid)


def _preview_expired(record) -> bool:
    """True when a still-unexecuted preview is past its TTL.

    An unparseable timestamp counts as expired: we never hand back a token we
    cannot prove is still fresh.
    """
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
    """Replay a still-valid, unexecuted preview: same details, same token.

    Nothing is written and the caller may confirm with this exact
    operation_id / confirm_token pair.
    """
    _require_record_scope(tool_name, mailbox, record)
    _require_directory_read(record, lanid)
    return ToolResult(
        content=f"{tool_name} 待确认（预览返回）",
        structured_content={
            "ok": True,
            "confirmation_required": True,
            "operation_id": record["operation_id"],
            "confirm_token": record["confirm_token"],
            "preview": {"items": record["items"], "details": record["details"]},
            "replayed": True,
        },
    )


def _is_batch_result(result) -> bool:
    """True when a write reported per-item outcomes instead of one verdict."""
    return isinstance(result, dict) and isinstance(result.get("results"), list)


def _classify_write_result(result):
    if _is_batch_result(result):
        rows = result["results"]
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

    # (1) operation_id alone is a query; never executes.
    if operation_id and not confirm_token:
        return _query_receipt(tool_name, operation_id, mailbox, lanid=lanid)

    # (2) idempotency replay: a known key returns its receipt and never
    #     re-executes. Skipped when a confirm_token is present: that is an
    #     explicit intent to execute THIS operation, and the key is already
    #     bound to the preview record, so short-circuiting here would make the
    #     documented "identical params + operation_id + confirm_token" confirm
    #     silently return the pending preview instead of executing.
    if idem and not confirm_token:
        record = store.find_by_idempotency_key(idem, mailbox=mailbox)
        if record is not None:
            _require_record_scope(tool_name, mailbox, record)
            status = record["status"]
            if status != STATUS_PENDING:
                # Terminal/in-flight for this key: replay the receipt and never
                # reactivate it, whatever it was.
                return _receipt(tool_name, record, mailbox=mailbox, lanid=lanid, replay=True)
            if not _preview_expired(record):
                # Still-valid unexecuted preview: hand back its details and the
                # same token so the caller can confirm it.
                return _preview_replay(tool_name, record, mailbox=mailbox, lanid=lanid)
            # Expired preview: fall through to build a fresh preview. The stale
            # row is left untouched and the new one is a distinct operation, so
            # nothing executing/unknown/completed is ever revived.

    if confirm_token:
        # A finished or already-claimed operation is never re-executed: the
        # receipt is returned instead (guards retries and concurrent confirms).
        existing = store.get(operation_id)
        _require_record_scope(tool_name, mailbox, existing)
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
            # A batch that already ran reported per-item outcomes. Re-claiming it
            # would re-run the WHOLE batch - the "整批重试" we must never do. The
            # per-item receipt tells the caller what to retry instead.
            return _receipt(tool_name, existing, mailbox=mailbox, lanid=lanid, replay=True)
        try:
            _require_unexpired_preview(existing)
            # Re-check the shared-directory target and create permission BEFORE
            # connecting or claiming: a stale confirmation must never write into
            # a re-pointed folder, and the record stays pending so a re-preview is
            # the only way forward.
            verified_target = _verified_directory_target(existing, params)
            # Connect before claiming, so connection failure leaves pending.
            # Connection setup may cross the TTL; check again before execution.
            client = initialize_outlook_client(params, ctx, mailbox=mailbox)
            _require_unexpired_preview(existing)
            store.begin_confirm(operation_id, confirm_token, business)
        except (ConfirmationError, ToolOperationError) as exc:
            if isinstance(exc, ConfirmationError) and exc.code == "CONFIRMATION_NOT_EXECUTABLE":
                current = store.get(operation_id)
                if current is not None:
                    return _receipt(tool_name, current, mailbox=mailbox, lanid=lanid, replay=True)
            return _tool_failure(tool_name, exc=exc, operation_id=operation_id)
        try:
            execute = {**business, "confirm": True, "confirmation_id": confirm_token}
            recorded_version = (existing.get("details") or {}).get("version")
            if recorded_version is not None:
                # The preview snapshotted the target's version; hand the server's
                # own copy back so the handler can refuse a draft that was edited
                # between preview and confirm. Callers never supply this value.
                execute["expected_version"] = recorded_version
            if verified_target is not None:
                # Execute against the target that was just verified, not a second
                # resolution that could disagree with it.
                execute["directory_target"] = verified_target
            result = getattr(client, tool_name)(**execute)
        except Exception as exc:
            # Default is UNKNOWN: only a named, definite rejection proves nothing
            # was written. Ambiguous outcomes are never auto-retried.
            ambiguous = not is_definite_rejection(exc)
            store.mark(
                operation_id,
                STATUS_UNKNOWN if ambiguous else STATUS_FAILED,
                result=_failure_record(exc),
            )
            return _tool_failure(tool_name, exc=exc, operation_id=operation_id)
        # Bookkeeping and the receipt stay OUTSIDE that handler: the write has
        # already happened, so no later failure here may rewrite its terminal
        # status (that would invite a duplicate create).
        status, final_result = _classify_write_result(result)
        store.mark(operation_id, status, result=final_result)
        return _receipt(tool_name, store.get(operation_id), mailbox=mailbox, lanid=lanid)

    # (3) preview: run the handler read-only, persist a pending operation.
    client = initialize_outlook_client(params, ctx, mailbox=mailbox)
    try:
        result = getattr(client, tool_name)(**business, confirm=False)
    except Exception as exc:
        return _tool_failure(tool_name, exc=exc)
    preview = result.get("preview") if isinstance(result, dict) else None
    items = preview.get("items") if isinstance(preview, dict) else None
    details = preview.get("details") if isinstance(preview, dict) else None
    record = store.create_preview(
        tool=tool_name,
        mailbox=mailbox,
        action=tool_name,
        business_params=business,
        items=items,
        details=details,
        idempotency_key=idem,
    )
    return ToolResult(
        content=f"{tool_name} 待确认",
        structured_content={
            "ok": True,
            "confirmation_required": True,
            "operation_id": record["operation_id"],
            "confirm_token": record["confirm_token"],
            "preview": result,
        },
    )


def _dispatch(tool_name: str, params: dict, ctx: Context) -> ToolResult:
    try:
        _validate_arguments(tool_name, params)
        if tool_name in READ_TOOLS:
            return _invoke_read(tool_name, params, ctx)
        return _run_write(tool_name, params, ctx)
    except Exception as exc:  # noqa: BLE001 - outermost safety net
        return _tool_failure(tool_name, exc=exc)


# ---- register the current enabled tools with the unified schema ----
def _make_handler(tool_name):
    def handler(ctx: Context = None, **params) -> ToolResult:
        return _dispatch(tool_name, params, ctx)

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
install_failure_envelope(mcp)


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
    """Gate every HTTP request to the MCP endpoint behind a shared key.

    Deliberately scoped to ``/mcp`` only: ``/healthz`` (liveness probe) and the
    signed ``/downloads/{identifier}`` routes stay open, so the container
    healthcheck and attachment links keep working.
    """

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
    # Persistent confirmation store lives under the data dir (required).
    store = configure_store(os.path.join(data_dir(), "operations.db"))
    # Close the SQLite connection explicitly on shutdown so the WAL is
    # checkpointed, rather than leaving it to process teardown.
    atexit.register(store.close)
    host = os.getenv("EWS_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("EWS_MCP_PORT", "7805"))
    api_key = os.getenv("EWS_MCP_API_KEY", "")
    if not api_key.strip():
        raise ValueError("缺少 EWS_MCP_API_KEY")
    logger.info("EWS MCP listening on %s:%s", host, port)
    mcp.run(
        transport="streamable-http",
        host=host,
        port=port,
        path="/mcp",
        middleware=[
            HTTPMiddleware(MCPKeyAuthMiddleware, api_key=api_key),
        ],
        # Avoid writing the key, which travels in the query string, to the log.
        uvicorn_config={"access_log": False},
    )


if __name__ == "__main__":
    main()
