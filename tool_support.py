"""Shared Exchange tool helpers; no authentication state."""

import hashlib
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from dateutil import parser


LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/_\-]{24,}")
_WS_RE = re.compile(r"\s+")


def sanitize(text, limit: int = 300):
    """Redact emails and opaque tokens before truncating caller-visible errors."""
    if not text:
        return None
    cleaned = _EMAIL_RE.sub("<email>", str(text))
    cleaned = _LONG_TOKEN_RE.sub("<redacted>", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or None


class ToolOperationError(ValueError):
    """Expected failure with a safe, caller-visible explanation."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


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
    """Use the IANA timezone required by exchangelib, preserving the instant."""
    return parse_datetime(value).astimezone(LOCAL_TIMEZONE)


def iso_datetime(value):
    return (
        parse_local_datetime(value).isoformat()
        if value is not None
        else None
    )


def require_send(action_desc: str) -> None:
    """Gate confirmed sends/notifications/OOF on EWS_MCP_SEND_ENABLED; allow previews."""
    from config import send_enabled

    if not send_enabled():
        raise ToolOperationError(
            "SEND_DISABLED",
            f"{action_desc} 未启用（须 EWS_MCP_SEND_ENABLED=true 且经两阶段确认）。",
        )


# These errors may follow submission; other cause-free tool errors are pre-submit validation.
UNKNOWN_OUTCOME_CODES = frozenset(
    {
        "SEND_FAILED_OR_UNKNOWN",
        "CANCEL_OUTCOME_UNKNOWN",
        "AVAILABILITY_UNAVAILABLE",
        "OOF_WRITE_UNAVAILABLE",
        "CONTACT_RECEIPT_INCOMPLETE",
        "TASK_RECEIPT_INCOMPLETE",
        "CONFIRMATION_MISMATCH",
    }
)

# Only known rejections prove nothing was applied; unknown errors remain uncertain.
_DEFINITE_REJECTION_ERRORS = frozenset(
    {
        "ErrorAccessDenied",
        "ErrorSendAsDenied",
        "ErrorImpersonateUserDenied",
        "ErrorInvalidRecipients",
        "ErrorMissingRecipients",
        "ErrorInvalidArgument",
        "ErrorInvalidValueForProperty",
        "ErrorMailRecipientNotFound",
        "ErrorItemNotFound",
        "ErrorInvalidId",
        "ErrorInvalidIdEmpty",
        "ErrorInvalidIdMalformed",
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
        "ErrorInvalidUserOofSettings",
    }
)


def is_definite_rejection(exc) -> bool:
    """Require definite non-application; retrying ambiguous writes can duplicate sends."""
    code = getattr(exc, "code", None)
    if code is not None and exc.__class__.__name__ == "ToolOperationError":
        if code in UNKNOWN_OUTCOME_CODES:
            return False
        cause = getattr(exc, "__cause__", None)
        return True if cause is None else is_definite_rejection(cause)
    return exc.__class__.__name__ in _DEFINITE_REJECTION_ERRORS


def classify_submission_failure(exc, *, action, unknown_code, rejected_code):
    """Return (code, public_message, uncertain); only definite rejections permit retries."""
    if is_definite_rejection(exc):
        return (
            rejected_code,
            f"{action}被服务端拒绝（未提交）：{exc.__class__.__name__}。",
            False,
        )
    return (
        unknown_code,
        f"{action}失败或结果未知；请先查询原操作号确认结果，勿重复提交。",
        True,
    )


def require_confirmation(
    *,
    mailbox,
    action,
    items,
    details=None,
    confirm=False,
    confirmation_id=None,
):
    """Build a preview; the MCP layer's OperationStore validates confirmed execution.

    The digest signals preview changes and is not an authentication token.
    """
    snapshot = {
        "mailbox": mailbox.lower(),
        "action": action,
        "items": items,
        "details": details,
    }
    encoded = json.dumps(
        snapshot, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    if not confirm:
        return {
            "confirmation_required": True,
            "confirmation_id": digest,
            "action": action,
            "preview": snapshot,
        }
    return None
