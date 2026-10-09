"""Free/busy availability for the employee's directory contacts.

Unknown or NoData attendees must never be reported as free (their status is
treated as busy/unknown so no slot is wrongly claimed as mutually free).
"""

from datetime import datetime, timedelta

from exchangelib import EWSDateTime
from exchangelib.ewsdatetime import EWSTimeZone
from exchangelib.properties import (
    Email,
    FreeBusyViewOptions,
    MailboxData,
    TimeWindow,
    TimeZone,
)
from exchangelib.services import GetUserAvailability

from tool_support import LOCAL_TIMEZONE, ToolOperationError, parse_local_datetime


def _suggested(start_dt, end_dt, duration):
    """Build a bounded suggested slot dict from the window/interval."""
    slot_end = min(start_dt + timedelta(minutes=duration), end_dt)
    return {
        "start": start_dt.astimezone(LOCAL_TIMEZONE).isoformat(),
        "end": slot_end.astimezone(LOCAL_TIMEZONE).isoformat(),
        "duration_minutes": int((slot_end - start_dt).total_seconds() // 60),
    }


# MergedFreeBusy digits index exchangelib's FREE_BUSY_CHOICES:
# 0 Free, 1 Tentative, 2 Busy, 3 OOF, 4 NoData, 5 WorkingElsewhere.
_BUSY_DIGITS = frozenset("1235")
_NODATA_DIGIT = "4"


def _classify_busy(merged, events):
    """Map a MergedFreeBusy string to free/busy/unknown.

    Anything that carries no information (empty merged data, NoData slots,
    unrecognised digits) is ``unknown`` — never ``free``.
    """
    if events:
        return "busy"
    if not merged:
        return "unknown"
    if any(ch in _BUSY_DIGITS for ch in merged):
        return "busy"
    if any(ch == _NODATA_DIGIT for ch in merged):
        return "unknown"
    if all(ch == "0" for ch in merged):
        return "free"
    return "unknown"


class AvailabilityOperations:
    def check_availability(self, start, end, attendees, duration=30):
        start_dt = parse_local_datetime(start)
        end_dt = parse_local_datetime(end)
        if start_dt >= end_dt:
            raise ToolOperationError("INVALID_TIME_RANGE", "忙闲时间窗口开始必须早于结束。")
        mailbox_data = []
        for value in attendees or []:
            email = str(value).strip().lower()
            if not email or "@" not in email:
                raise ToolOperationError("INVALID_ATTENDEE", "参会人邮箱格式无效。")
            mailbox_data.append(MailboxData(email=Email(email_address=email)))
        if not mailbox_data:
            raise ToolOperationError("INVALID_ATTENDEE", "请至少提供一个参会人。")
        time_window = TimeWindow(
            start=EWSDateTime.from_datetime(start_dt),
            end=EWSDateTime.from_datetime(end_dt),
        )
        view_options = FreeBusyViewOptions(
            time_window=time_window,
            merged_free_busy_interval=duration,
            requested_view="MergedOnly",
        )
        # Minimal requester TimeZone (UTC offset bias). LOCAL_TIMEZONE is not DST,
        # so a bias-only element is sufficient; from_server_timezone needs pytz
        # which is not installed here.
        offset_minutes = int(
            datetime.now(LOCAL_TIMEZONE).utcoffset().total_seconds() // 60
        )
        requester_tz = TimeZone(bias=offset_minutes)
        tzinfo_arg = EWSTimeZone.from_zoneinfo(LOCAL_TIMEZONE)
        try:
            # ``call()`` returns a lazy generator (_chunked_get_elements); the HTTP
            # request fires while it is iterated, NOT when it is created. Consume it
            # inside the wrapper so a transport/EWS failure cannot escape as a raw,
            # unclassified exception. The wrapper chains the original error, so the
            # envelope still surfaces its desensitized exchange_code/exchange_message.
            views = list(
                GetUserAvailability(protocol=self.account.protocol).call(
                    tzinfo=tzinfo_arg,
                    mailbox_data=mailbox_data,
                    timezone=requester_tz,
                    free_busy_view_options=view_options,
                )
                or []
            )
        except Exception as exc:
            raise ToolOperationError(
                "AVAILABILITY_UNAVAILABLE",
                "忙闲查询失败或受现场策略限制，结果未知。",
            ) from exc
        # GetUserAvailability returns ONE element per MailboxData entry, in the
        # same order: either a FreeBusyView or the EWS exception for that
        # mailbox. There is no outer {mailbox, error, free_busy_view} wrapper.
        # If the counts disagree the positional mapping is broken, and nothing
        # may be reported as free.
        statuses = {}
        merged_by = {}
        if len(views) != len(mailbox_data):
            for data in mailbox_data:
                email = data.email.email_address.lower()
                statuses[email] = "unknown"
                merged_by[email] = None
        else:
            for data, response in zip(mailbox_data, views):
                email = data.email.email_address.lower()
                if isinstance(response, Exception) or response is None:
                    statuses[email] = "unknown"
                    merged_by[email] = None
                    continue
                merged = str(getattr(response, "merged", "") or "")
                events = getattr(response, "calendar_events", None) or []
                statuses[email] = _classify_busy(merged, events)
                merged_by[email] = merged
        all_free = all(status == "free" for status in statuses.values())

        # Suggested slot: if everyone is free, suggest the window head;
        # otherwise, if all attendees have merged data with no unknowns, suggest
        # the first interval where everyone is free ('0').
        suggested = None
        if all_free:
            suggested = _suggested(start_dt, end_dt, duration)
        elif (
            all(status != "unknown" for status in statuses.values())
            and merged_by
            and all(value is not None for value in merged_by.values())
        ):
            slot_count = min(len(value) for value in merged_by.values())
            for index in range(slot_count):
                if all(value[index] == "0" for value in merged_by.values()):
                    suggested = _suggested(
                        start_dt + timedelta(minutes=index * duration),
                        end_dt,
                        duration,
                    )
                    break

        return {
            "window_start": start_dt.astimezone(LOCAL_TIMEZONE).isoformat(),
            "window_end": end_dt.astimezone(LOCAL_TIMEZONE).isoformat(),
            "attendees": [
                {"email": email, "status": status}
                for email, status in statuses.items()
            ],
            "mutually_free_slot_possible": bool(all_free),
            "suggested_slot": suggested,
            "note": (
                "Unknown/NoData attendees are never reported as free."
                if not all_free
                else None
            ),
        }
