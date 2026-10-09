"""Calendar operations limited to the OA-resolved employee's own calendar."""

from datetime import date, datetime, time, timedelta

from exchangelib import Attendee, CalendarItem, EWSDate, EWSDateTime, HTMLBody, Mailbox
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
from exchangelib.items.calendar_item import EXCEPTION, OCCURRENCE, RECURRING_MASTER, SINGLE

from mail_operations import _page
from tool_support import (
    LOCAL_TIMEZONE,
    ToolOperationError,
    iso_datetime,
    parse_datetime,
    parse_local_datetime,
    require_confirmation,
    require_send,
)


EVENT_FIELDS = {
    "subject",
    "body",
    "start",
    "end",
    "location",
    "organizer",
    "required_attendees",
    "optional_attendees",
    "resources",
    "my_response_type",
    "is_all_day",
    "is_meeting",
    "is_cancelled",
    "is_recurring",
    "type",
    "recurrence",
    "recurrence_id",
    "original_start",
    "uid",
}


def _event_time(value):
    # exchangelib returns inclusive start/end dates for all-day events.
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return iso_datetime(value)


def _mailbox_detail(mailbox):
    if mailbox is None:
        return None
    return {"name": mailbox.name, "email_address": mailbox.email_address}


def _attendee_detail(attendee):
    return {
        **(
            _mailbox_detail(attendee.mailbox)
            or {"name": None, "email_address": None}
        ),
        "response_type": attendee.response_type,
        "last_response_time": iso_datetime(attendee.last_response_time),
        "proposed_start": iso_datetime(attendee.proposed_start),
        "proposed_end": iso_datetime(attendee.proposed_end),
    }


def _event_detail(event):
    return {
        "id": event.id,
        "changekey": event.changekey,
        "uid": event.uid,
        "subject": event.subject,
        "body": str(event.body) if event.body is not None else "",
        "body_type": "HTML" if isinstance(event.body, HTMLBody) else "Text",
        "start": _event_time(event.start),
        "end": _event_time(event.end),
        "location": event.location,
        "organizer": _mailbox_detail(event.organizer),
        "required_attendees": [
            _attendee_detail(a) for a in event.required_attendees or []
        ],
        "optional_attendees": [
            _attendee_detail(a) for a in event.optional_attendees or []
        ],
        "resources": [_attendee_detail(a) for a in event.resources or []],
        "my_response_type": event.my_response_type,
        "is_all_day": event.is_all_day,
        "is_meeting": event.is_meeting,
        "is_cancelled": event.is_cancelled,
        "is_recurring": event.is_recurring,
        "type": event.type,
        "has_recurrence": event.recurrence is not None,
        "recurrence_id": _event_time(event.recurrence_id),
        "original_start": _event_time(event.original_start),
    }


def _boundary(value, *, end=False):
    if isinstance(value, date) and not isinstance(value, datetime):
        midnight = datetime.combine(value, time(), tzinfo=LOCAL_TIMEZONE)
        return midnight + timedelta(days=1) if end else midnight
    return parse_datetime(value)


def _require_single_or_occurrence(event):
    if event.type == RECURRING_MASTER:
        raise ToolOperationError(
            "RECURRING_MASTER_UNSUPPORTED", "不支持修改或响应周期主事件；请使用明确的单次日程或 occurrence ID。"
        )
    if event.type not in (SINGLE, OCCURRENCE, EXCEPTION):
        raise ToolOperationError(
            "INVALID_EVENT_TYPE", "无法确认日程为单次事件或明确的 occurrence，请重新查询日程。"
        )


def _is_organizer(event, email):
    organizer = event.organizer.email_address if event.organizer is not None else None
    return bool(organizer and organizer.strip().casefold() == email.strip().casefold())


def _require_organizer(event, email):
    if not _is_organizer(event, email):
        raise ToolOperationError("NOT_ORGANIZER", "只有当前员工组织的日程才能更新或取消。")


def _require_attendee(event, email):
    if _is_organizer(event, email) or event.my_response_type == "Organizer":
        raise ToolOperationError("ORGANIZER_CANNOT_RESPOND", "组织者不能响应自己组织的日程。")
    for group in (event.required_attendees, event.optional_attendees, event.resources):
        for attendee in group or ():
            address = attendee.mailbox.email_address if attendee.mailbox is not None else None
            if address and address.strip().casefold() == email.strip().casefold():
                return
    raise ToolOperationError("NOT_ATTENDEE", "无法确认当前员工在该日程的参会人列表中，不能响应。")


class CalendarOperations:
    def _calendar_item(self, event_id):
        event = self._tool_item(
            event_id, folder_names=("calendar",), only_fields=EVENT_FIELDS
        )
        if not isinstance(event, CalendarItem):
            raise ToolOperationError(
                "INVALID_EVENT", "指定项目不是当前员工日历中的日程。"
            )
        return event

    def get_event(self, event_id):
        return _event_detail(self._calendar_item(event_id))

    def update_event(
        self,
        event_id,
        subject=None,
        body=None,
        start=None,
        end=None,
        location=None,
        notify_attendees=False,
        confirm=False,
        confirmation_id=None,
    ):
        event = self._calendar_item(event_id)
        _require_single_or_occurrence(event)
        _require_organizer(event, self.config.email)
        changes = {
            name: value
            for name, value in (
                ("subject", subject),
                ("body", body),
                ("start", start),
                ("end", end),
                ("location", location),
            )
            if value is not None
        }
        if not changes:
            raise ToolOperationError(
                "NO_CHANGES", "请至少指定一个需要更新的日程字段。"
            )
        for name in ("start", "end"):
            if name in changes:
                parsed = parse_local_datetime(changes[name])
                if event.is_all_day:
                    if parsed.time() != time():
                        raise ToolOperationError(
                            "INVALID_ALL_DAY_TIME",
                            "全天日程请使用日期或午夜时间；结束日期包含当天。",
                        )
                    changes[name] = EWSDate.from_date(parsed.date())
                else:
                    changes[name] = EWSDateTime.from_datetime(parsed)
        if "start" in changes or "end" in changes:
            new_start = _boundary(changes.get("start", event.start))
            new_end = _boundary(changes.get("end", event.end), end=True)
            if new_start >= new_end:
                raise ToolOperationError(
                    "INVALID_TIME_RANGE", "日程开始时间必须早于结束时间。"
                )
        changes = {
            name: value
            for name, value in changes.items()
            if value != getattr(event, name)
        }
        if not changes:
            return {
                "id": event.id,
                "changekey": event.changekey,
                "updated_fields": [],
            }
        # Confirmation is required even when attendees are not notified.
        preview = require_confirmation(
            mailbox=self.config.email,
            action="update_event",
            items=[_event_detail(event)],
            details={
                "changes": {
                    name: (
                        _event_time(value)
                        if name in ("start", "end")
                        else value
                    )
                    for name, value in changes.items()
                },
                "notify_attendees": bool(notify_attendees),
            },
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        if notify_attendees:
            require_send("发送日程更新通知")
        for name, value in changes.items():
            setattr(event, name, value)
        event.save(
            update_fields=list(changes),
            send_meeting_invitations=(
                SEND_TO_ALL_AND_SAVE_COPY if notify_attendees else SEND_TO_NONE
            ),
        )
        return {
            "id": event.id,
            "changekey": event.changekey,
            "updated_fields": list(changes),
        }

    def respond_to_event(
        self, event_id, response, message=None, confirm=False, confirmation_id=None
    ):
        methods = {
            "accept": "accept",
            "tentative": "tentatively_accept",
            "decline": "decline",
        }
        if response not in methods:
            raise ToolOperationError(
                "INVALID_RESPONSE", "日程响应必须是 accept、tentative 或 decline。"
            )
        event = self._calendar_item(event_id)
        _require_single_or_occurrence(event)
        _require_attendee(event, self.config.email)
        preview = require_confirmation(
            mailbox=self.config.email,
            action="respond_to_event",
            items=[_event_detail(event)],
            details={"response": response, "message": message},
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        require_send("发送日程响应")
        # SDK reply objects lack author/from; the scoped event determines identity.
        # Exchange manages sender.
        getattr(event, methods[response])(
            **({"body": message} if message is not None else {})
        )
        return {"id": event_id, "response": response, "responded": True}

    def cancel_event(self, event_id, message=None, confirm=False, confirmation_id=None):
        event = self._calendar_item(event_id)
        _require_single_or_occurrence(event)
        _require_organizer(event, self.config.email)
        # Decide from attendee data before submission: delete appointments, notify meetings.
        # A rejected meeting notice must never trigger deletion.
        attendee_count = sum(
            len(group or ())
            for group in (
                event.required_attendees,
                event.optional_attendees,
                event.resources,
            )
        )
        is_meeting = bool(event.is_meeting) or attendee_count > 0
        preview = require_confirmation(
            mailbox=self.config.email,
            action="cancel_event",
            items=[_event_detail(event)],
            details={
                "message": message,
                "delivery": "cancel_notice" if is_meeting else "delete",
                "attendee_count": attendee_count,
            },
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        if is_meeting:
            require_send("发送日程取消通知")
            # CancelCalendarItem excludes author; organizer and scope checks preserve identity.
            try:
                event.cancel(**({"body": message} if message is not None else {}))
            except Exception as exc:
                # EWS may cancel the item before notice delivery fails, even for
                # ErrorInvalidRecipients. Mark uncertain to prevent duplicate execution.
                raise ToolOperationError(
                    "CANCEL_OUTCOME_UNKNOWN",
                    "取消请求已发出但结果不确定：EWS 可能已取消该日程，仅在通知环节失败。"
                    "请重新获取日程确认状态，勿重复确认。",
                ) from exc
            return {
                "id": event.id,
                "cancelled": True,
                "deleted": False,
            }
        # move_to_trash 可恢复；Item.delete 默认永久删除。
        event.move_to_trash(send_meeting_cancellations=SEND_TO_NONE)
        return {
            "id": event.id,
            "cancelled": True,
            "deleted": True,
        }

    def list_events(self, start, end, offset=0, limit=20):
        _page(offset, limit)
        # exchangelib requires an IANA timezone, not a fixed offset such as +08:00.
        start_dt = parse_local_datetime(start)
        end_dt = parse_local_datetime(end)
        if start_dt >= end_dt:
            raise ToolOperationError("INVALID_TIME_RANGE", "日程时间窗开始必须早于结束。")
        calendar = self._tool_folder("calendar")
        query = calendar.view(start=start_dt, end=end_dt).order_by("start")
        page = list(query[offset : offset + limit])
        return {
            "items": [_event_detail(event) for event in page],
            "offset": offset,
            "limit": limit,
            "has_more": len(page) == limit,
        }

    def create_event(
        self,
        subject,
        body="",
        start=None,
        end=None,
        location=None,
        attendees=None,
        send_invitations=False,
        confirm=False,
        confirmation_id=None,
    ):
        if not subject or not str(subject).strip():
            raise ToolOperationError("INVALID_EVENT_SUBJECT", "日程主题不能为空。")
        start_dt = parse_local_datetime(start)
        end_dt = parse_local_datetime(end) if end else start_dt + timedelta(hours=1)
        if start_dt >= end_dt:
            raise ToolOperationError("INVALID_TIME_RANGE", "日程开始时间必须早于结束时间。")
        attendee_objects = []
        seen = set()
        for value in attendees or []:
            email = str(value).strip().lower()
            if not email or "@" not in email:
                raise ToolOperationError("INVALID_ATTENDEE", "参会人邮箱格式无效。")
            if email not in seen:
                seen.add(email)
                attendee_objects.append(
                    Attendee(mailbox=Mailbox(email_address=email))
                )
        calendar = self._tool_folder("calendar")
        event = CalendarItem(
            account=self.account,
            folder=calendar,
            subject=subject,
            body=body,
            start=start_dt,
            end=end_dt,
            location=location,
            required_attendees=attendee_objects,
        )
        preview = require_confirmation(
            mailbox=self.config.email,
            action="create_event",
            items=[{
                "subject": subject,
                "start": _event_time(start_dt),
                "end": _event_time(end_dt),
                "location": location,
                "attendees": sorted(seen),
                "send_invitations": bool(send_invitations),
            }],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        if send_invitations:
            require_send("发送会议邀请")
        try:
            if send_invitations:
                event.save(
                    send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY
                )
            else:
                event.save()
        except Exception as exc:
            raise ToolOperationError("EVENT_CREATE_FAILED", "创建日程失败，请检查邮箱权限。") from exc
        return {
            "id": event.id,
            "changekey": event.changekey,
            "created": True,
        }
