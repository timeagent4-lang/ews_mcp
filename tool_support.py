"""Small shared helpers for Exchange tool operations; no authentication state."""

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil import parser


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_\-]{24,}")
_WS_RE = re.compile(r"\s+")


def sanitize(text, limit: int = 300):
    """Redact PII/secrets before an upstream message leaves the server.

    Not merely a truncation: e-mail addresses and long opaque tokens (ids,
    changekeys, hashes) are masked first, whitespace is collapsed, then the
    result is capped. Shared by the server envelope and by per-item batch
    results, so both redact identically.
    """
    if not text:
        return None
    secrets = [os.getenv(key, "") for key in ("OUTLOOK_ADMIN_PASSWORD", "OUTLOOK_ADMIN_LANID", "EWS_MCP_API_KEY", "EWS_MCP_DOWNLOAD_SECRET")]
    try:
        from utils.audit import request_secrets
        secrets.extend(request_secrets())
    except ImportError:
        pass
    cleaned = str(text)
    for secret in sorted(set(s for s in secrets if s), key=len, reverse=True):
        cleaned = cleaned.replace(secret, "<redacted>")
    cleaned = _EMAIL_RE.sub("<email>", cleaned)
    cleaned = _LONG_TOKEN_RE.sub("<redacted>", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or None


class ToolOperationError(ValueError):
    """An expected operation failure with a safe, caller-visible explanation."""

    def __init__(self, code: str, message: str, *, status="failed", results=None):
        self.code = code
        self.public_message = message
        self.status = status
        self.results = results
        super().__init__(message)


def validate_item_fields(item, fields=None):
    """SDK 字段清理在提交 try 之前运行，避免本地拒绝误报结果未知。"""
    if fields is not None and (not getattr(item, "id", None) or not getattr(item, "changekey", None)):
        raise ToolOperationError("ITEM_IDENTIFIER_INCOMPLETE", "项目 ID 或 ChangeKey 不完整，本次未提交写入。")
    # 版本协商可能读取服务器，但仍在实际写入之前。
    account = getattr(item, "account", None)
    version = account.version if account is not None else None
    try:
        if fields is None:
            item.clean(version=version)
        else:
            for name in fields:
                field = item.get_field_by_fieldname(name)
                field.clean(getattr(item, name), version=version)
    except (TypeError, ValueError) as exc:
        raise ToolOperationError("INVALID_PARAMS", "参数不符合 Exchange 字段约束，本次未提交写入。") from exc


def move_to_deleted_items(item):
    """明确 MoveToDeletedItems，直接消费删除结果，不执行后置 trash 查找。"""
    from exchangelib.items import MOVE_TO_DELETED_ITEMS, SEND_TO_NONE

    results = item.account.bulk_delete(
        ids=[item], delete_type=MOVE_TO_DELETED_ITEMS, send_meeting_cancellations=SEND_TO_NONE,
    )
    if len(results) != 1:
        raise ToolOperationError("DELETE_RESULT_UNKNOWN", "删除响应不完整，请先核对已删除邮件。", status="unknown")
    if isinstance(results[0], Exception):
        raise results[0]
    if results[0] is not True:
        raise ToolOperationError("DELETE_RESULT_UNKNOWN", "删除结果无法确认，请先核对已删除邮件。", status="unknown")


def parse_datetime(value):
    try:
        parsed = value if isinstance(value, datetime) else parser.parse(value)
        if not isinstance(parsed, datetime):
            raise ValueError("datetime required")
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed
    except (ValueError, TypeError, OverflowError) as exc:
        raise ToolOperationError(
            "INVALID_DATETIME", "时间格式无效，请使用 ISO 日期时间。"
        ) from exc


def parse_local_datetime(value):
    """Parse a timestamp and normalise its tzinfo to the service timezone.

    exchangelib can only map an IANA-named tzinfo to an EWS timezone; a
    fixed-offset one (e.g. "+08:00") dies during EWS conversion. Re-pointing the
    tzinfo keeps the same instant, and naive input is already localised by
    ``parse_datetime``.
    """
    return parse_datetime(value).astimezone(LOCAL_TIMEZONE)


def iso_datetime(value):
    return (
        parse_local_datetime(value).isoformat()
        if value is not None
        else None
    )


def require_send(action_desc: str) -> None:
    """Block sends/notifications unless EWS_MCP_SEND_ENABLED=true."""
    from config import send_enabled

    if not send_enabled():
        raise ToolOperationError(
            "SEND_DISABLED",
            f"{action_desc} 未启用（须 EWS_MCP_SEND_ENABLED=true）。",
        )


# ToolOperationError codes whose outcome is genuinely uncertain (the request may
# have reached the server). Anything else raised without a cause is our own
# pre-submit validation, which proves nothing was submitted.
UNKNOWN_OUTCOME_CODES = frozenset(
    {
        "SEND_FAILED_OR_UNKNOWN",
        "CANCEL_OUTCOME_UNKNOWN",
        "CONTACT_RECEIPT_INCOMPLETE",
        "WRITE_OUTCOME_UNKNOWN",
    }
)

# ALLOWLIST, deliberately: an error may only be treated as "the server rejected
# it, nothing was applied" when we can name it. Anything unrecognised —
# including unknown EWS errors, disconnects and response-parse failures — must
# stay UNKNOWN, because it may already have been applied.
_DEFINITE_REJECTION_ERRORS = frozenset(
    {
        # permission / addressing
        "ErrorAccessDenied",
        "ErrorSendAsDenied",
        "ErrorImpersonateUserDenied",
        "ErrorInvalidRecipients",
        "ErrorMissingRecipients",
        "ErrorInvalidArgument",
        "ErrorInvalidValueForProperty",
        "ErrorMailRecipientNotFound",
        # item / id
        "ErrorItemNotFound",
        "ErrorInvalidId",
        "ErrorInvalidIdEmpty",
        "ErrorInvalidIdMalformed",
        "ErrorInvalidChangeKey",
        "ErrorIrresolvableConflict",
        "ErrorChangeKeyRequiredForWriteOperations",
        "UnauthorizedError",
        # calendar response/cancel preconditions
        "ErrorCalendarIsNotOrganizer",
        "ErrorCalendarIsCancelledForAccept",
        "ErrorCalendarIsCancelledForDecline",
        "ErrorCalendarIsCancelledForTentative",
        "ErrorCalendarIsCancelledForRemove",
        "ErrorCalendarIsDelegatedForAccept",
        "ErrorCalendarIsDelegatedForDecline",
        "ErrorCalendarIsDelegatedForTentative",
        "ErrorCalendarIsDelegatedForRemove",
        "ErrorCalendarIsOrganizerForAccept",
        "ErrorCalendarIsOrganizerForDecline",
        "ErrorCalendarIsOrganizerForTentative",
        "ErrorCalendarIsOrganizerForRemove",
        "ErrorNotOrganizer",
        "ErrorNotDelegate",
    }
)


def is_definite_rejection(exc) -> bool:
    """True only when we can PROVE the operation was not applied.

    Unknown errors default to False (ambiguous) on purpose: re-executing an
    ambiguous write can duplicate a send.
    """
    from exchangelib.errors import InvalidEnumValue, InvalidTypeError, NaiveDateTimeNotAllowed

    if isinstance(exc, (InvalidEnumValue, InvalidTypeError, NaiveDateTimeNotAllowed)):
        return True
    code = getattr(exc, "code", None)
    if isinstance(exc, ToolOperationError):
        if code in UNKNOWN_OUTCOME_CODES or exc.status in ("unknown", "partial"):
            return False
        cause = getattr(exc, "__cause__", None)
        # No cause -> raised by our own pre-submit validation.
        return True if cause is None else is_definite_rejection(cause)
    return exc.__class__.__name__ in _DEFINITE_REJECTION_ERRORS


def classify_submission_failure(exc, *, action, unknown_code, rejected_code):
    """Tell "never submitted" apart from "submitted but outcome unknown".

    Returns ``(error_code, public_message, uncertain)``. Default is uncertain:
    only a named, definite rejection is treated as safely re-executable.
    """
    if is_definite_rejection(exc):
        return (
            rejected_code,
            f"{action}被服务端拒绝（未提交）：{exc.__class__.__name__}。",
            False,
        )
    return (
        unknown_code,
        f"{action}结果未知；请先核对邮箱中的实际结果，勿重复提交。",
        True,
    )


def error_details(exc, *, submitted=False):
    """Classify an error by real SDK types and preserve sanitized EWS causes."""
    from exchangelib.errors import EWSError, InvalidEnumValue, InvalidTypeError, NaiveDateTimeNotAllowed, ResponseMessageError, TransportError, UnauthorizedError
    from requests.exceptions import RequestException

    chain = []
    cause = exc
    while cause is not None and id(cause) not in {id(item) for item in chain}:
        chain.append(cause)
        cause = cause.__cause__
    exchange = next((e for e in chain if isinstance(e, EWSError)), None)
    status = "unknown" if submitted and not is_definite_rejection(exc) else "failed"
    if isinstance(exc, ToolOperationError):
        status = exc.status if exc.status != "failed" else status
        if exc.code in UNKNOWN_OUTCOME_CODES:
            status = "unknown"
        code, message = exc.code, exc.public_message
    elif isinstance(exc, (InvalidEnumValue, InvalidTypeError, NaiveDateTimeNotAllowed)):
        code, message = "INVALID_PARAMS", "参数不符合 Exchange 字段类型或取值要求。"
    elif isinstance(exc, UnauthorizedError):
        code, message = "AUTHENTICATION_FAILED", "Exchange 认证失败，请检查所选凭据。"
    elif isinstance(exc, ResponseMessageError):
        name = type(exc).__name__
        code = {"ErrorAccessDenied": "EXCHANGE_ACCESS_DENIED", "ErrorSendAsDenied": "EXCHANGE_ACCESS_DENIED",
                "ErrorImpersonateUserDenied": "EXCHANGE_ACCESS_DENIED", "ErrorItemNotFound": "ITEM_NOT_FOUND",
                "ErrorInvalidChangeKey": "VERSION_CONFLICT", "ErrorIrresolvableConflict": "VERSION_CONFLICT"}.get(name, "EXCHANGE_ERROR")
        message = "Exchange 拒绝或无法完成此操作。"
    elif isinstance(exc, (TransportError, RequestException, TimeoutError, ConnectionError)):
        code, message = "NETWORK_ERROR", "Exchange 网络请求失败。"
    elif isinstance(exc, ValueError) and not submitted:
        code, message = "INVALID_PARAMS", "参数或配置无效。"
    elif isinstance(exc, EWSError):
        code, message = "EXCHANGE_ERROR", "Exchange 请求或响应处理失败。"
    else:
        code, message = "INTERNAL_ERROR", "服务内部错误，请使用请求号查询服务日志。"
    result = {"error_code": code, "message": sanitize(message), "status": status}
    if status == "unknown":
        result["message"] = (result["message"] or "操作失败") + " 操作结果未知，请先核对邮箱中的实际结果，勿重复提交。"
    if exchange is not None:
        result["exchange_code"] = type(exchange).__name__
        result["exchange_message"] = sanitize(str(exchange))
    if isinstance(exc, ToolOperationError) and exc.results is not None:
        result["results"] = exc.results
    return result
