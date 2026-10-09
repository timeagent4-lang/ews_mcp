"""Return employee-directory free/busy facts; callers choose meeting times."""

from datetime import time, timedelta

from exchangelib import EWSDateTime
from exchangelib.ewsdatetime import EWSTimeZone
from exchangelib.properties import (
    DaylightTime,
    Email,
    FreeBusyViewOptions,
    MailboxData,
    StandardTime,
    TimeWindow,
    TimeZone,
)
from exchangelib.services import GetUserAvailability

from tool_support import LOCAL_TIMEZONE, ToolOperationError, parse_local_datetime


_MERGED_STATUSES = {
    "0": "free",
    "1": "tentative",
    "2": "busy",
    "3": "oof",
    "4": "unknown",
}


def _interval(start_dt, end_dt, status):
    return {"start": start_dt.isoformat(), "end": end_dt.isoformat(), "status": status}


def _busy_intervals(merged, start_dt, end_dt, step):
    """Validate coverage before interpreting the stream; coalesce equal statuses."""
    full_slots, remainder = divmod(end_dt - start_dt, step)
    expected_slots = full_slots + bool(remainder)
    if (
        not isinstance(merged, str)
        or len(merged) != expected_slots
        or any(digit not in _MERGED_STATUSES for digit in merged)
    ):
        return [_interval(start_dt, end_dt, "unknown")]

    intervals = []
    for index, digit in enumerate(merged):
        slot_start = start_dt + index * step
        slot_end = min(slot_start + step, end_dt)
        status = _MERGED_STATUSES[digit]
        if intervals and intervals[-1]["status"] == status:
            intervals[-1]["end"] = slot_end.isoformat()
        else:
            intervals.append(_interval(slot_start, slot_end, status))
    return intervals


def _window_status(intervals):
    statuses = {interval["status"] for interval in intervals}
    if "unknown" in statuses:
        return "unknown"
    return "free" if statuses == {"free"} else "busy"


def _requester_timezone(start_dt):
    # EWS Bias is added to local time to obtain UTC. Shanghai has no current DST;
    # both transition elements are still required, with Month=0 for no transition.
    transition = {
        "bias": 0,
        "time": time(0, 0),
        "occurrence": 0,
        "iso_month": 0,
        "weekday": "Sunday",
    }
    offset_minutes = int(start_dt.utcoffset().total_seconds() // 60)
    return TimeZone(
        bias=-offset_minutes,
        standard_time=StandardTime(**transition),
        daylight_time=DaylightTime(**transition),
    )


class AvailabilityOperations:
    def check_availability(self, start, end, attendees, interval_minutes=30):
        start_dt = parse_local_datetime(start)
        end_dt = parse_local_datetime(end)
        if start_dt >= end_dt:
            raise ToolOperationError("INVALID_TIME_RANGE", "忙闲时间窗口开始必须早于结束。")
        if (
            isinstance(interval_minutes, bool)
            or not isinstance(interval_minutes, int)
            or not 5 <= interval_minutes <= 1440
        ):
            raise ToolOperationError("INVALID_PARAMS", "忙闲采样间隔必须为 5–1440 分钟的整数。")
        if not isinstance(attendees, (list, tuple)) or not 1 <= len(attendees) <= 100:
            raise ToolOperationError("INVALID_ATTENDEE", "请提供 1–100 个参会人邮箱。")
        mailbox_data = []
        seen = set()
        for value in attendees:
            email = value.strip().lower() if isinstance(value, str) else ""
            if not email or "@" not in email:
                raise ToolOperationError("INVALID_ATTENDEE", "参会人邮箱格式无效。")
            if email in seen:
                continue
            seen.add(email)
            mailbox_data.append(MailboxData(email=Email(email_address=email)))
        time_window = TimeWindow(
            start=EWSDateTime.from_datetime(start_dt),
            end=EWSDateTime.from_datetime(end_dt),
        )
        view_options = FreeBusyViewOptions(
            time_window=time_window,
            merged_free_busy_interval=interval_minutes,
            requested_view="MergedOnly",
        )
        requester_tz = _requester_timezone(start_dt)
        tzinfo_arg = EWSTimeZone.from_zoneinfo(LOCAL_TIMEZONE)
        try:
            # Consume the lazy response here so transport/EWS failures are wrapped.
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
        # Responses match MailboxData order, including per-mailbox exceptions.
        # A count mismatch invalidates the mapping; all statuses stay unknown.
        aligned = len(views) == len(mailbox_data)
        step = timedelta(minutes=interval_minutes)
        results = []
        for index, data in enumerate(mailbox_data):
            response = views[index] if aligned else None
            merged = (
                getattr(response, "merged", None)
                if response is not None and not isinstance(response, Exception)
                else None
            )
            intervals = _busy_intervals(merged, start_dt, end_dt, step)
            results.append({
                "email": data.email.email_address,
                "status": _window_status(intervals),
                "intervals": intervals,
            })

        return {
            "window_start": start_dt.astimezone(LOCAL_TIMEZONE).isoformat(),
            "window_end": end_dt.astimezone(LOCAL_TIMEZONE).isoformat(),
            "timezone": LOCAL_TIMEZONE.key,
            "interval_minutes": interval_minutes,
            "attendees": results,
        }
