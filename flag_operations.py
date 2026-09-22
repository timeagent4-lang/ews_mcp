"""Outlook follow-up flags on ordinary mail for the OA-resolved employee.

Flag state is Outlook's own follow-up model: a ``0x1090`` FlagStatus plus the
PSETID_Task properties. exchangelib 5.6 ships only ``0x1090``, and it rejects
``property_tag`` for the ``0x8000-0xFFFE`` range ("reserved for custom
properties"), so the rest are declared in PSETID_Task dispid form and
registered on ``Message`` at import. Only a plain ``Message`` accepts these
fields (a ``MeetingCancellation`` raises AttributeError); writes must go
through ``save(update_fields=[...])``.
"""

from datetime import datetime

from exchangelib import EWSDateTime, ExtendedProperty, Message
from exchangelib.restriction import Q

from mail_operations import SIMPLE_FIELDS
from tool_support import (
    LOCAL_TIMEZONE,
    ToolOperationError,
    iso_datetime,
    parse_datetime,
    require_confirmation,
)

FLAG_NONE = 0
FLAG_COMPLETE = 1
FLAG_FLAGGED = 2

TASK_STATUS_NOT_STARTED = 0
TASK_STATUS_COMPLETE = 2

FLAG_LABELS = {FLAG_NONE: "none", FLAG_COMPLETE: "complete", FLAG_FLAGGED: "flagged"}
FLAG_CHOICES = ("flagged", "complete", "clear")


class FlagStatus(ExtendedProperty):
    property_tag = 0x1090
    property_type = "Integer"


class FlagCompleteTime(ExtendedProperty):
    property_tag = 0x1091
    property_type = "SystemTime"


class TaskStatus(ExtendedProperty):
    distinguished_property_set_id = "Task"
    property_id = 0x8101
    property_type = "Integer"


class TaskStartDate(ExtendedProperty):
    distinguished_property_set_id = "Task"
    property_id = 0x8104
    property_type = "SystemTime"


class TaskDueDate(ExtendedProperty):
    distinguished_property_set_id = "Task"
    property_id = 0x8105
    property_type = "SystemTime"


class TaskDateCompleted(ExtendedProperty):
    distinguished_property_set_id = "Task"
    property_id = 0x810F
    property_type = "SystemTime"


class TaskComplete(ExtendedProperty):
    distinguished_property_set_id = "Task"
    property_id = 0x811C
    property_type = "Boolean"


#: attr name -> ExtendedProperty subclass; registered on Message at import.
FLAG_FIELDS = (
    ("flag_status", FlagStatus),
    ("flag_complete_time", FlagCompleteTime),
    ("task_status", TaskStatus),
    ("task_start_date", TaskStartDate),
    ("task_due_date", TaskDueDate),
    ("task_date_completed", TaskDateCompleted),
    ("task_complete", TaskComplete),
)


def register_flag_fields():
    """Register the follow-up flag model on ``Message`` (idempotent)."""
    for name, cls in FLAG_FIELDS:
        try:
            Message.register(name, cls)
        except ValueError:
            # Already registered by an earlier import; register() is not idempotent.
            pass


register_flag_fields()

#: Fields projected when listing flagged mail; ``flag_status`` drives the filter.
LIST_FIELDS = tuple(
    dict.fromkeys(
        (*SIMPLE_FIELDS, "sender", "flag_status", "task_due_date", "task_complete")
    )
)


def _ews_datetime(value):
    """SystemTime extended properties only accept EWSDateTime, not plain datetime."""
    if value is None:
        return None
    return EWSDateTime.from_datetime(parse_datetime(value).astimezone(LOCAL_TIMEZONE))


def _flag_changes(flag, due_date):
    """The full field set for a requested flag state.

    ``flagged`` resets completion artifacts; ``complete`` stamps the completion
    time but leaves start/due dates alone unless a due date is supplied;
    ``clear`` empties every task field.
    """
    now = EWSDateTime.from_datetime(datetime.now(LOCAL_TIMEZONE))
    due = _ews_datetime(due_date)
    if flag == "flagged":
        return {
            "flag_status": FLAG_FLAGGED,
            "task_status": TASK_STATUS_NOT_STARTED,
            "task_complete": False,
            "task_start_date": None,
            "task_due_date": due,
            "task_date_completed": None,
            "flag_complete_time": None,
        }
    if flag == "complete":
        changes = {
            "flag_status": FLAG_COMPLETE,
            "flag_complete_time": now,
            "task_status": TASK_STATUS_COMPLETE,
            "task_complete": True,
            "task_date_completed": now,
        }
        if due_date is not None:
            changes["task_due_date"] = due
        return changes
    return {
        "flag_status": FLAG_NONE,
        "flag_complete_time": None,
        "task_status": None,
        "task_start_date": None,
        "task_due_date": None,
        "task_date_completed": None,
        "task_complete": False,
    }


def _flag_state(item):
    status = getattr(item, "flag_status", None)
    return {
        "id": item.id,
        "subject": item.subject or "",
        "flag": FLAG_LABELS.get(status, "none"),
        "flag_status": status,
        "task_complete": getattr(item, "task_complete", None),
        "task_due_date": iso_datetime(getattr(item, "task_due_date", None)),
    }


def _list_row(item, time_field):
    return {
        **_flag_state(item),
        "changekey": item.changekey,
        "sender": item.sender.email_address if item.sender else None,
        "timestamp": iso_datetime(getattr(item, time_field, None)),
    }


class FlagOperations:
    def set_message_flag(
        self, message_id, flag, due_date=None, confirm=False, confirmation_id=None
    ):
        if flag not in FLAG_CHOICES:
            raise ToolOperationError(
                "INVALID_FLAG", "旗标状态必须是 flagged, complete 或 clear。"
            )
        item = self._tool_item(message_id)
        if type(item) is not Message:
            raise ToolOperationError("NOT_MAIL_MESSAGE", "此操作仅支持普通邮件。")
        changes = _flag_changes(flag, due_date)  # validates due_date before preview
        preview = require_confirmation(
            mailbox=self.config.email,
            action="set_message_flag",
            items=[_flag_state(item)],
            details={"flag": flag, "due_date": iso_datetime(due_date)},
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        for field, value in changes.items():
            setattr(item, field, value)
        try:
            item.save(update_fields=list(changes))
        except Exception as exc:
            raise ToolOperationError(
                "FLAG_UPDATE_FAILED", "更新邮件旗标失败，请检查邮箱权限。"
            ) from exc
        return {
            "id": item.id,
            "changekey": item.changekey,
            "flag": flag,
            "flag_status": changes["flag_status"],
            "task_complete": changes["task_complete"],
            "task_due_date": iso_datetime(changes.get("task_due_date")),
            "updated_fields": list(changes),
        }

    def list_flagged_messages(self, folder="inbox", status="flagged", limit=20):
        if folder not in ("inbox", "drafts", "sent"):
            raise ToolOperationError("INVALID_FOLDER", "仅支持 inbox, drafts, sent 文件夹。")
        if status not in ("flagged", "complete", "all"):
            raise ToolOperationError(
                "INVALID_FLAG_STATUS", "status 必须是 flagged, complete 或 all。"
            )
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolOperationError("INVALID_PAGINATION", "limit 必须为 1 到 100。")
        target = self._tool_folder(folder)
        if status == "flagged":
            query = Q(flag_status=FLAG_FLAGGED)
        elif status == "complete":
            query = Q(flag_status=FLAG_COMPLETE)
        else:
            query = Q(flag_status=FLAG_FLAGGED) | Q(flag_status=FLAG_COMPLETE)
        time_field = (
            "datetime_created"
            if folder == "drafts"
            else "datetime_sent"
            if folder == "sent"
            else "datetime_received"
        )
        rows = list(
            target.filter(query)
            .only(*LIST_FIELDS)
            .order_by("-" + time_field)[:limit]
        )
        return {
            "items": [_list_row(row, time_field) for row in rows],
            "folder": folder,
            "status": status,
        }
