"""OOF settings for the OA-resolved employee.

Folder delegation may not grant OOF rights; report policy limits without bypassing them.
"""

from exchangelib.services import GetUserOofSettings, SetUserOofSettings
from exchangelib.settings import OofSettings

from tool_support import (
    ToolOperationError,
    classify_submission_failure,
    require_confirmation,
    require_send,
)


def _oof_to_dict(oof):
    if oof is None:
        return {"enabled": False}
    start = getattr(oof, "start", None)
    end = getattr(oof, "end", None)
    return {
        "enabled": getattr(oof, "state", None) != OofSettings.DISABLED,
        "external_audience": getattr(oof, "external_audience", None),
        "internal_reply": str(getattr(oof, "internal_reply", "") or ""),
        "external_reply": str(getattr(oof, "external_reply", "") or ""),
        "start": start.isoformat() if start else None,
        "end": end.isoformat() if end else None,
    }


class OofOperations:
    def get_oof_settings(self):
        try:
            oof = GetUserOofSettings(
                protocol=self.account.protocol
            ).call(mailbox=self.account.primary_smtp_address)
        except Exception as exc:
            raise ToolOperationError(
                "OOF_READ_UNAVAILABLE",
                "读取自动回复设置失败，可能受现场策略限制。 ",
            ) from exc
        return _oof_to_dict(oof)

    def set_oof(
        self,
        enabled,
        internal_reply=None,
        external_reply=None,
        start=None,
        end=None,
        confirm=False,
        confirmation_id=None,
    ):
        internal = str(internal_reply or "").strip()
        external = str(external_reply or "").strip()
        state = OofSettings.ENABLED if enabled else OofSettings.DISABLED
        # External delivery requires explicit external text; otherwise audience is None.
        external_audience = "All" if (enabled and external) else None
        kwargs = {"state": state, "external_audience": external_audience}
        if enabled:
            if not internal:
                raise ToolOperationError(
                    "OOF_INTERNAL_REQUIRED", "启用自动回复必须提供内部回复正文。 "
                )
            kwargs["internal_reply"] = internal
            kwargs["external_reply"] = external if external else internal
            if start:
                kwargs["start"] = start
            if end:
                kwargs["end"] = end
        oof = OofSettings(**kwargs)
        preview = require_confirmation(
            mailbox=self.config.email,
            action="set_oof",
            items=[_oof_to_dict(oof)],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        if enabled:
            require_send("设置自动回复")
        try:
            SetUserOofSettings(protocol=self.account.protocol).call(
                oof_settings=oof, mailbox=self.account.primary_smtp_address
            )
        except Exception as exc:
            code, message, _ = classify_submission_failure(
                exc,
                action="设置自动回复",
                unknown_code="OOF_WRITE_UNAVAILABLE",
                rejected_code="OOF_REJECTED",
            )
            raise ToolOperationError(code, message) from exc
        return {"enabled": bool(enabled), "external_audience": external_audience}
