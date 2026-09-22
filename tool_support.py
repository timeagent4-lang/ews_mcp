"""Small shared helpers for Exchange tool operations; no authentication state."""

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
    """Redact PII/secrets before an upstream message leaves the server.

    Not merely a truncation: e-mail addresses and long opaque tokens (ids,
    changekeys, hashes) are masked first, whitespace is collapsed, then the
    result is capped. Shared by the server envelope and by per-item batch
    results, so both redact identically.
    """
    if not text:
        return None
    cleaned = _EMAIL_RE.sub("<email>", str(text))
    cleaned = _LONG_TOKEN_RE.sub("<redacted>", cleaned)
    cleaned = _WS_RE.sub(" ", cleaned).strip()
    if len(cleaned) > limit:
        cleaned = cleaned[: limit - 1] + "…"
    return cleaned or None


class ToolOperationError(ValueError):
    """An expected operation failure with a safe, caller-visible explanation."""

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
    """Block real sends/notifications/OOF unless EWS_MCP_SEND_ENABLED=true.

    Previews remain allowed; only the confirmed execution is gated. This is the
    default-off safety switch, not a global read-only toggle.
    """
    from config import send_enabled

    if not send_enabled():
        raise ToolOperationError(
            "SEND_DISABLED",
            f"{action_desc} 未启用（须 EWS_MCP_SEND_ENABLED=true 且经两阶段确认）。",
        )


# ToolOperationError codes whose outcome is genuinely uncertain (the request may
# have reached the server). Anything else raised without a cause is our own
# pre-submit validation, which proves nothing was submitted.
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
        # OOF settings rejected outright
        "ErrorInvalidUserOofSettings",
    }
)


def is_definite_rejection(exc) -> bool:
    """True only when we can PROVE the operation was not applied.

    Unknown errors default to False (ambiguous) on purpose: re-executing an
    ambiguous write can duplicate a send.
    """
    code = getattr(exc, "code", None)
    if code is not None and exc.__class__.__name__ == "ToolOperationError":
        if code in UNKNOWN_OUTCOME_CODES:
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
    """Build a preview snapshot; the server owns the persistent gate.

    This is deliberately a pass-through now: the durable two-phase confirmation
    (operation_id / confirm_token / idempotency_key) is enforced in the MCP layer
    via the persistent OperationStore, not here. When ``confirm`` is False the
    caller receives the read-only preview; when True the handler proceeds to
    execute (the server has already validated the token and identical params).
    The digest is only a preview-change hint, not an authentication token.
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
