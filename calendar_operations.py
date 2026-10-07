"""当前目标邮箱日历的查询和直接写入。"""

from datetime import date, datetime, time, timedelta

from exchangelib import Attendee, CalendarItem, EWSDate, EWSDateTime, HTMLBody, Mailbox
from exchangelib.errors import ErrorInvalidRecipients, ErrorMissingRecipients
from exchangelib.items import SEND_TO_ALL_AND_SAVE_COPY, SEND_TO_NONE
from exchangelib.items.calendar_item import EXCEPTION, OCCURRENCE, RECURRING_MASTER, SINGLE
from exchangelib.items.base import NEVER_OVERWRITE

from mail_operations import _page
from tool_support import (
    LOCAL_TIMEZONE,
    ToolOperationError,
    error_details,
    move_to_deleted_items,
    iso_datetime,
    parse_datetime,
    parse_local_datetime,
    require_send,
    validate_item_fields,
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
    if not event.id or not event.changekey:
        raise ToolOperationError("INVALID_ITEM_ID", "日程响应缺少 ID 或 ChangeKey，本次未提交写入。")


def _is_organizer(event, email):
    organizer = event.organizer.email_address if event.organizer is not None else None
    return bool(organizer and organizer.strip().casefold() == email.strip().casefold())


def _require_organizer(event, email):
    if not _is_organizer(event, email):
        raise ToolOperationError("NOT_ORGANIZER", "只有目标邮箱组织的日程才能更新或取消。")


def _require_attendee(event, email):
    if _is_organizer(event, email) or event.my_response_type == "Organizer":
        raise ToolOperationError("ORGANIZER_CANNOT_RESPOND", "组织者不能响应自己组织的日程。")
    for group in (event.required_attendees, event.optional_attendees, event.resources):
        for attendee in group or ():
            address = attendee.mailbox.email_address if attendee.mailbox is not None else None
            if address and address.strip().casefold() == email.strip().casefold():
                return
    raise ToolOperationError("NOT_ATTENDEE", "无法确认目标邮箱在该日程的参会人列表中，不能响应。")


class CalendarOperations:
    def _calendar_item(self, event_id):
        event = self._tool_item(
            event_id, folder_names=("calendar",), only_fields=EVENT_FIELDS
        )
        if not isinstance(event, CalendarItem):
            raise ToolOperationError(
                "INVALID_EVENT", "指定项目不是目标邮箱日历中的日程。"
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
        if notify_attendees:
            require_send("发送日程更新通知")
        for name, value in changes.items():
            setattr(event, name, value)
        validate_item_fields(event, fields=list(changes))
        try:
            event.save(
                update_fields=list(changes),
                conflict_resolution=NEVER_OVERWRITE,
                send_meeting_invitations=(
                    SEND_TO_ALL_AND_SAVE_COPY if notify_attendees else SEND_TO_NONE
                ),
            )
        except Exception as exc:
            raise ToolOperationError(
                "EVENT_UPDATE_FAILED", "更新日程失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        return {
            "id": event.id,
            "changekey": event.changekey,
            "updated_fields": list(changes),
        }

    def respond_to_event(self, event_id, response, message=None):
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
        require_send("发送日程响应")
        validate_item_fields(event, fields=())
        # SDK 5.6.0 reply objects have no author/from field. The scoped employee
        # account and exact event reference determine the represented identity;
        # sender remains server-managed. Do not invent unsupported reply fields.
        try:
            getattr(event, methods[response])(
                **({"body": message} if message is not None else {})
            )
        except Exception as exc:
            raise ToolOperationError(
                "EVENT_RESPONSE_FAILED", "响应日程失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        return {"id": event_id, "response": response, "responded": True}

    def cancel_event(self, event_id, message=None):
        event = self._calendar_item(event_id)
        _require_single_or_occurrence(event)
        _require_organizer(event, self.config.email)
        # Two different business actions hide behind this one tool name. A meeting
        # has people to notify, so cancelling sends cancellation notices. A
        # personal appointment has nobody to notify, so EWS rejects the notice for
        # lack of recipients - such an item must simply be deleted. Decide from the
        # event's own attendee set BEFORE acting; never from an
        # ErrorInvalidRecipients caught after the fact, which would blind-delete
        # any genuine meeting whose notice the server happened to reject.
        attendee_count = sum(
            len(group or ())
            for group in (
                event.required_attendees,
                event.optional_attendees,
                event.resources,
            )
        )
        is_meeting = bool(event.is_meeting) or attendee_count > 0
        if is_meeting:
            # Only a real notice leaves the mailbox, so only this branch needs the
            # send gate; deleting an own appointment notifies nobody.
            require_send("发送日程取消通知")
            validate_item_fields(event, fields=())
            # CancelCalendarItem explicitly excludes author in SDK 5.6.0. The
            # organizer check and scoped reference preserve the employee identity.
            try:
                event.cancel(**({"body": message} if message is not None else {}))
            except Exception as exc:
                # The request already left the client, and EWS can apply the
                # cancellation to the ITEM and then fail only on delivering the
                # notice. So an ErrorInvalidRecipients here does not prove nothing
                # changed - observed in practice. Report it as an uncertain
                # outcome; the caller must inspect the calendar before repeating.
                uncertain = (
                    isinstance(exc, (ErrorInvalidRecipients, ErrorMissingRecipients))
                    or error_details(exc, submitted=True)["status"] == "unknown"
                )
                raise ToolOperationError(
                    "CANCEL_OUTCOME_UNKNOWN" if uncertain else "EVENT_CANCEL_FAILED",
                    "取消日程的请求未获得完整成功响应，请先重新获取日程检查状态。",
                    status="unknown" if uncertain else "failed",
                ) from exc
            return {
                "id": event_id,
                "cancelled": True,
                "deleted": False,
            }
        # 个人日程只移动到已删除邮件；直接读取 DeleteItem 结果，避免 SDK 后置查 trash。
        validate_item_fields(event, fields=())
        try:
            move_to_deleted_items(event)
        except Exception as exc:
            raise ToolOperationError(
                "EVENT_DELETE_FAILED", "移除个人日程失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        return {
            "id": event_id,
            "cancelled": True,
            "deleted": True,
        }

    def list_events(self, start, end, offset=0, limit=20):
        # Validate pagination against the tool schema BEFORE touching the query,
        # so a bad limit/offset is a parameter error rather than a query crash.
        _page(offset, limit)
        # Normalise an aware window to the service timezone before it reaches
        # exchangelib; a fixed-offset tzinfo (e.g. "+08:00") cannot be mapped to
        # an IANA name and the call dies as a programmer error.
        start_dt = parse_local_datetime(start)
        end_dt = parse_local_datetime(end)
        if start_dt >= end_dt:
            raise ToolOperationError("INVALID_TIME_RANGE", "日程时间窗开始必须早于结束。")
        calendar = self._tool_folder("calendar")
        query = calendar.view(start=start_dt, end=end_dt).order_by("start")
        page = list(query[offset : offset + limit])
        for event in page:
            if isinstance(event, Exception):
                raise event
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
        if send_invitations:
            require_send("发送会议邀请")
        validate_item_fields(event)
        try:
            if send_invitations:
                event.save(
                    send_meeting_invitations=SEND_TO_ALL_AND_SAVE_COPY
                )
            else:
                event.save()
        except Exception as exc:
            raise ToolOperationError(
                "EVENT_CREATE_FAILED", "创建日程失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        if not event.id or not event.changekey:
            raise ToolOperationError(
                "EVENT_SAVE_RESULT_UNKNOWN", "保存响应缺少日程 ID 或 ChangeKey，请先查询日历。",
                status="unknown",
            )
        return {
            "id": event.id,
            "changekey": event.changekey,
            "created": True,
        }
