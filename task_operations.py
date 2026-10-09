"""Scoped Exchange task reads and writes for the OA-resolved employee."""

from datetime import date, datetime
from decimal import Decimal

from exchangelib import EWSDate, Task

from tool_support import (
    ToolOperationError,
    iso_datetime,
    parse_datetime,
    require_confirmation,
)


def _task_date(value):
    """Task start/due dates are EWSDate(date) objects; format them safely."""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return iso_datetime(value)


def _ews_date(value):
    """Task start/due dates are date-valued EWSDate fields, not datetimes."""
    if value is None:
        return None
    return EWSDate.from_date(parse_datetime(value).date())


def _task_row(task):
    # `Task.complete` is a *method* (marks complete on the server), not an
    # attribute; derive the Boolean from the `status` field instead.
    status = getattr(task, "status", None)
    percent = getattr(task, "percent_complete", None)
    if percent is not None:
        try:
            percent = int(percent)
        except (TypeError, ValueError):
            percent = None
    return {
        "id": getattr(task, "id", None),
        "changekey": getattr(task, "changekey", None),
        "subject": getattr(task, "subject", None),
        "complete": status == Task.COMPLETED,
        "status": status,
        "start_date": _task_date(getattr(task, "start_date", None)),
        "due_date": _task_date(getattr(task, "due_date", None)),
        "percent_complete": percent,
    }


class TaskOperations:
    def create_task(
        self,
        subject,
        body=None,
        start_date=None,
        due_date=None,
        confirm=False,
        confirmation_id=None,
    ):
        """Create one task in the employee's own Tasks folder.

        Writes only the mailbox Tasks item; it sends nothing and invites nobody,
        so it needs Tasks create access and no send switch.
        """
        subject = (subject or "").strip()
        if not subject:
            raise ToolOperationError("INVALID_TASK", "任务主题不能为空。 ")
        # Convert before the preview so a bad date fails the read-only phase.
        start = _ews_date(start_date)
        due = _ews_date(due_date)
        preview = require_confirmation(
            mailbox=self.config.email,
            action="create_task",
            items=[
                {
                    "subject": subject,
                    "body_preview": str(body or "")[:500],
                    "start_date": _task_date(start),
                    "due_date": _task_date(due),
                }
            ],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        item = Task(
            account=self.account,
            folder=self._tool_folder("tasks"),
            subject=subject,
            body=body or "",
            start_date=start,
            due_date=due,
        )
        try:
            item.save()
        except Exception as exc:
            raise ToolOperationError(
                "TASK_CREATE_FAILED", "新建任务失败，请检查邮箱权限。 "
            ) from exc
        if not (isinstance(item.id, str) and item.id) or not (
            isinstance(item.changekey, str) and item.changekey
        ):
            raise ToolOperationError(
                "TASK_RECEIPT_INCOMPLETE",
                "任务可能已写入但回执不完整，请勿重发，仅查询原操作号。 ",
            )
        return {
            "id": item.id,
            "task_id": item.id,
            "changekey": item.changekey,
            "created": True,
            "folder": "tasks",
            "subject": subject,
            "start_date": _task_date(item.start_date),
            "due_date": _task_date(item.due_date),
        }

    def list_tasks(self, folder="tasks", incomplete_only=True, limit=50):
        # Re-check here, like every other folder-taking tool: the schema enum is
        # client-side only, and the argument must drive the lookup instead of
        # being silently replaced by a literal.
        if folder not in ("tasks",):
            raise ToolOperationError("INVALID_FOLDER", "任务文件夹仅支持 tasks。 ")
        tasks_folder = self._tool_folder(folder)
        # `complete` is a server-marking *method*, not a filterable field, so filter
        # in Python on the status field.
        rows = list(tasks_folder.all().order_by("-due_date")[:limit])
        if incomplete_only:
            rows = [
                task for task in rows
                if getattr(task, "status", None) != Task.COMPLETED
            ]
        return {
            "items": [_task_row(task) for task in rows],
            "incomplete_only": bool(incomplete_only),
        }

    def update_task(
        self,
        task_id,
        complete=None,
        due_date=None,
        confirm=False,
        confirmation_id=None,
    ):
        task = self._tool_item(task_id, folder_names=("tasks",))
        if not isinstance(task, Task):
            raise ToolOperationError("INVALID_TASK", "指定项目不是当前员工的任务。 ")
        changes = {}
        if complete is not None:
            # `Task.complete` is a method, not an attribute; map to status fields.
            if complete:
                changes["status"] = Task.COMPLETED
                changes["percent_complete"] = Decimal(100)
            else:
                changes["status"] = Task.NOT_STARTED
                changes["percent_complete"] = Decimal(0)
        if due_date is not None:
            # Task.due_date is an EWSDate field (a date, not a datetime).
            changes["due_date"] = EWSDate.from_date(parse_datetime(due_date).date())
        if not changes:
            raise ToolOperationError("NO_CHANGES", "请至少指定 complete 或 due_date。 ")
        preview = require_confirmation(
            mailbox=self.config.email,
            action="update_task",
            items=[_task_row(task)],
            details={"complete": complete, "due_date": due_date},
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        for field, value in changes.items():
            setattr(task, field, value)
        try:
            task.save(update_fields=list(changes))
        except Exception as exc:
            raise ToolOperationError("TASK_UPDATE_FAILED", "更新任务失败，请检查邮箱权限。 ") from exc
        return {
            "id": task.id,
            "changekey": task.changekey,
            "updated_fields": list(changes),
            "complete": getattr(task, "status", None) == Task.COMPLETED,
        }
