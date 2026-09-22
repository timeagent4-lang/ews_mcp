"""Scoped draft and ordinary mail mutations for the OA-resolved employee."""

import hashlib
import re

from exchangelib import Mailbox, Message
from exchangelib.items.base import MOVE_TO_DELETED_ITEMS

from tool_support import (
    ToolOperationError,
    classify_submission_failure,
    require_confirmation,
    require_send,
    sanitize,
)


def _mailbox_state(value):
    if value is None:
        return None
    return {
        key: getattr(value, key, None)
        for key in ("email_address", "name", "routing_type", "mailbox_type")
    }


def _address(value):
    return (getattr(value, "email_address", None) or "").strip().lower()


def _recipients(value):
    if value is None or value == "":
        return []
    values = re.split("[,;]", value) if isinstance(value, str) else value
    if not isinstance(values, (list, tuple)):
        raise ToolOperationError("INVALID_RECIPIENTS", "收件人必须为邮箱地址列表。")
    result = []
    seen = set()
    for entry in values:
        if not isinstance(entry, str):
            raise ToolOperationError("INVALID_RECIPIENTS", "收件人必须为邮箱地址列表。")
        email = entry.strip()
        if not email:
            continue
        if not re.fullmatch(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
            raise ToolOperationError("INVALID_RECIPIENTS", "收件人邮箱地址格式无效。")
        if email.lower() not in seen:
            seen.add(email.lower())
            result.append(Mailbox(email_address=email))
    return result


def _deduplicate(groups, excluded=()):
    seen = {address.lower() for address in excluded}
    results = []
    for group in groups:
        unique = []
        for recipient in group or []:
            address = _address(recipient)
            if address and address not in seen:
                seen.add(address)
                unique.append(recipient)
        results.append(unique)
    return results


def _batch_ids(ids):
    if not isinstance(ids, (list, tuple)) or not 1 <= len(ids) <= 50:
        raise ToolOperationError("INVALID_BATCH", "每次须提供 1 至 50 个邮件 ID。")
    if any(not isinstance(value, str) or not value.strip() for value in ids):
        raise ToolOperationError("INVALID_BATCH", "邮件 ID 不能为空。")
    if len(set(ids)) != len(ids):
        raise ToolOperationError("INVALID_BATCH", "同一批次不能包含重复邮件 ID。")
    return list(ids)


def _failure(item_id, exc):
    result = {
        "id": item_id,
        "success": False,
        "error_code": (
            exc.code if isinstance(exc, ToolOperationError) else "MAIL_OPERATION_FAILED"
        ),
        "message": (
            exc.public_message
            if isinstance(exc, ToolOperationError)
            else "邮件操作失败，请检查权限或稍后重试。"
        ),
    }
    if not isinstance(exc, ToolOperationError):
        # A raw Exchange error must keep its ResponseCode per item instead of
        # collapsing into the generic tool code above - the batch summary stays at
        # the top level, the root cause stays with the row it belongs to.
        result["exchange_code"] = exc.__class__.__name__
        exchange_message = sanitize(str(exc))
        if exchange_message:
            result["exchange_message"] = exchange_message
    return result


def _ordinary_message(item):
    # Meeting requests/cancellations are separate SDK classes and have other side effects.
    if type(item) is not Message:
        raise ToolOperationError("NOT_MAIL_MESSAGE", "此操作仅支持普通邮件。")
    return item


def _item_state(item):
    body = str(item.body or "")
    attachments = []
    for attachment in item.attachments or []:
        metadata = {
            key: getattr(attachment, key, None)
            for key in (
                "name",
                "size",
                "content_type",
                "content_id",
                "content_location",
                "is_inline",
            )
        }
        metadata["id"] = getattr(getattr(attachment, "attachment_id", None), "id", None)
        metadata["type"] = type(attachment).__name__
        modified = getattr(attachment, "last_modified_time", None)
        metadata["last_modified_time"] = modified.isoformat() if modified else None
        attachments.append(metadata)
    return {
        "id": item.id,
        "changekey": item.changekey,
        "parent_folder_id": getattr(item.parent_folder_id, "id", None),
        "is_draft": item.is_draft,
        "subject": item.subject,
        "author": _mailbox_state(item.author),
        "to_recipients": [_mailbox_state(x) for x in item.to_recipients or []],
        "cc_recipients": [_mailbox_state(x) for x in item.cc_recipients or []],
        "bcc_recipients": [_mailbox_state(x) for x in item.bcc_recipients or []],
        "body_preview": body[:2000],
        "body_length": len(body),
        "body_type": type(item.body).__name__,
        "body_sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
        "attachments": attachments,
    }


class WriteOperations:
    def _write_draft(self, draft_id):
        draft = _ordinary_message(self._tool_item(draft_id, folder_names=("drafts",)))
        if not draft.is_draft or getattr(draft.parent_folder_id, "id", None) != self._tool_folder("drafts").id:
            raise ToolOperationError(
                "NOT_A_DRAFT", "邮件必须位于当前员工草稿箱且处于草稿状态。"
            )
        return draft

    def create_draft(
        self,
        subject="",
        body="",
        to_emails="",
        cc_emails="",
        bcc_emails="",
        mode="new",
        reply_to=None,
        confirm=False,
        confirmation_id=None,
    ):
        if mode not in ("new", "reply", "reply_all", "forward"):
            raise ToolOperationError("INVALID_DRAFT_MODE", "不支持的草稿类型。")
        author = Mailbox(email_address=self.config.email)
        recipients = [_recipients(to_emails), _recipients(cc_emails), _recipients(bcc_emails)]
        folder = self._tool_folder("drafts")
        if mode == "new":
            if reply_to:
                raise ToolOperationError("INVALID_DRAFT_MODE", "新邮件不能指定回复邮件 ID。")
            to, cc, bcc = _deduplicate(recipients)
            draft = Message(
                account=self.account,
                folder=folder,
                author=author,
                subject=subject,
                body=body,
                to_recipients=to,
                cc_recipients=cc,
                bcc_recipients=bcc,
                is_draft=True,
            )
        else:
            if not reply_to:
                raise ToolOperationError("ORIGINAL_REQUIRED", "回复或转发需要原邮件 ID。")
            original = _ordinary_message(self._tool_item(reply_to))
            target_subject = subject or (
                ("FW: " if mode == "forward" else "RE: ") + (original.subject or "")
            )
            if mode == "forward":
                to, cc, bcc = _deduplicate(recipients)
                draft = original.create_forward(
                    subject=target_subject,
                    body=body,
                    to_recipients=to,
                    cc_recipients=cc,
                    bcc_recipients=bcc,
                )
                draft.author = author
            else:
                # 显式传参(to_emails/cc_emails)始终保留；仅当调用方未指定时，才从
                # 原邮件/原发件人推导，且推导来源剔除发件人自己(避免“自发”)。
                # 这样“自发自收”(显式 to_emails=自己)不会被 excluded 误清空成空收件人。
                explicit_to = list(recipients[0])
                explicit_cc = list(recipients[1])
                to = list(explicit_to)
                cc = list(explicit_cc)
                bcc = list(recipients[2])
                targets = original.reply_to or ([original.author] if original.author else [])
                if mode == "reply_all":
                    # reply_all: 未显式指定时合并 reply_to 目标与原收件人/抄送，均排己。
                    if not to:
                        to = [
                            r for r in (list(targets) + list(original.to_recipients or []))
                            if _address(r) != self.config.email.lower()
                        ]
                    if not cc:
                        cc = [
                            r for r in (original.cc_recipients or [])
                            if _address(r) != self.config.email.lower()
                        ]
                elif not to:
                    # 普通 reply: 用 reply_to(或原发件人)作为候选，排己。
                    to = [r for r in targets if _address(r) != self.config.email.lower()]
                to, cc, bcc = _deduplicate([to, cc, bcc])
                if not to and not cc:
                    raise ToolOperationError(
                        "REPLY_RECIPIENT_REQUIRED", "原邮件没有可用的回复收件人，请明确指定。"
                    )
                if mode == "reply_all":
                    draft = original.create_reply_all(subject=target_subject, body=body, author=author)
                    # SDK defaults copy Bcc and ignore Reply-To; replace all recipient fields.
                    draft.to_recipients, draft.cc_recipients, draft.bcc_recipients = to, cc, bcc
                else:
                    draft = original.create_reply(
                        subject=target_subject,
                        body=body,
                        to_recipients=to,
                        cc_recipients=cc,
                        bcc_recipients=bcc,
                        author=author,
                    )
                    # create_reply defaults an empty To back to the author.
                    draft.to_recipients = to
        preview = require_confirmation(
            mailbox=self.config.email,
            action="create_draft",
            items=[{
                "subject": subject or "",
                "body_preview": str(body or "")[:500],
                "to": to_emails,
                "cc": cc_emails,
                "bcc": bcc_emails,
                "mode": mode,
                "reply_to": reply_to,
            }],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        try:
            saved = draft.save() if mode == "new" else draft.save(folder=folder)
        except Exception as exc:
            raise ToolOperationError("DRAFT_CREATE_FAILED", "保存草稿失败，请检查邮箱权限。") from exc
        return {"id": saved.id, "changekey": saved.changekey, "mode": mode, "created": True, "folder": "drafts"}

    def update_draft(
        self,
        draft_id,
        subject=None,
        body=None,
        to_emails=None,
        cc_emails=None,
        bcc_emails=None,
        confirm=False,
        confirmation_id=None,
    ):
        draft = self._write_draft(draft_id)
        updates = {
            name: value
            for name, value in (("subject", subject), ("body", body))
            if value is not None
        }
        for field, value in (
            ("to_recipients", to_emails),
            ("cc_recipients", cc_emails),
            ("bcc_recipients", bcc_emails),
        ):
            if value is not None:
                updates[field] = _recipients(value)
        if _address(draft.author) != self.config.email.lower():
            updates["author"] = Mailbox(email_address=self.config.email)
        preview = require_confirmation(
            mailbox=self.config.email,
            action="update_draft",
            items=[{
                "draft_id": draft_id,
                "subject": draft.subject,
                "author": _address(draft.author),
            }],
            details={
                "subject": subject,
                "body_length": (len(body) if body is not None else None),
                "to": to_emails,
                "cc": cc_emails,
                "bcc": bcc_emails,
            },
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        for field, value in updates.items():
            setattr(draft, field, value)
        if updates:
            try:
                draft.save(update_fields=list(updates))
            except Exception as exc:
                raise ToolOperationError(
                    "DRAFT_UPDATE_FAILED", "更新草稿失败，请检查草稿权限或重新读取草稿。"
                ) from exc
        return {"id": draft.id, "changekey": draft.changekey, "updated": bool(updates), "folder": "drafts"}

    def send_draft(self, draft_id, confirm=False, confirmation_id=None):
        draft = self._write_draft(draft_id)
        if _address(draft.author) != self.config.email.lower():
            raise ToolOperationError(
                "DRAFT_AUTHOR_MISMATCH", "草稿发件人不是当前员工，请先更新草稿再重新预览。"
            )
        if not any((draft.to_recipients, draft.cc_recipients, draft.bcc_recipients)):
            raise ToolOperationError("RECIPIENT_REQUIRED", "发送邮件前必须指定收件人。")
        preview = require_confirmation(
            mailbox=self.config.email,
            action="send_draft",
            items=[_item_state(draft)],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview:
            return preview
        require_send("发送邮件")
        try:
            draft.send(save_copy=True, copy_to_folder=self._tool_folder("sent"))
        except Exception as exc:
            # A timeout can mean the send succeeded; a definite rejection did not.
            code, message, _ = classify_submission_failure(
                exc,
                action="发送邮件",
                unknown_code="SEND_FAILED_OR_UNKNOWN",
                rejected_code="SEND_REJECTED",
            )
            raise ToolOperationError(code, message) from exc
        return {"id": draft.id, "sent": True, "copy_folder": "sent"}

    def delete_draft(self, draft_id, confirm=False, confirmation_id=None):
        draft = self._write_draft(draft_id)
        preview = require_confirmation(
            mailbox=self.config.email,
            action="delete_draft",
            items=[_item_state(draft)],
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview:
            return preview
        try:
            # move_to_trash() 内部为 MOVE_TO_DELETED_ITEMS（移至已删除文件夹）。
            # 原生 Message.delete() 是 HARD_DELETE（永久删除），语义不符，弃用。
            # 调用后对象 ID 会被清空，返回仍以调用方传入的 draft_id 为准。
            draft.move_to_trash()
        except Exception as exc:
            raise ToolOperationError("DRAFT_DELETE_FAILED", "普通删除草稿失败，请检查邮箱权限。") from exc
        return {"id": draft_id, "deleted": True, "delete_type": MOVE_TO_DELETED_ITEMS}

    def update_messages(
        self,
        ids,
        set_read=None,
        categories_add=None,
        categories_remove=None,
        confirm=False,
        confirmation_id=None,
    ):
        ids = _batch_ids(ids)
        if set_read is not None and type(set_read) is not bool:
            raise ToolOperationError("INVALID_READ_STATE", "已读状态必须为布尔值。")
        for categories in (categories_add, categories_remove):
            if categories is not None and (
                not isinstance(categories, (list, tuple))
                or any(not isinstance(value, str) or not value.strip() for value in categories)
            ):
                raise ToolOperationError("INVALID_CATEGORIES", "分类必须为非空文字列表。")
        items, failures, snapshot = [], {}, []
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id))
                items.append((item_id, item))
                snapshot.append({"id": item_id, **_item_state(item)})
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
                snapshot.append(failures[item_id])
        preview = require_confirmation(
            mailbox=self.config.email,
            action="update_messages",
            items=snapshot,
            details={
                "set_read": set_read,
                "categories_add": categories_add,
                "categories_remove": categories_remove,
            },
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        results = dict(failures)
        for item_id, item in items:
            try:
                fields = []
                if set_read is not None:
                    item.is_read = set_read
                    fields.append("is_read")
                if categories_add is not None or categories_remove is not None:
                    remove = set(categories_remove or [])
                    item.categories = list(
                        dict.fromkeys(
                            value
                            for value in list(item.categories or []) + list(categories_add or [])
                            if value not in remove
                        )
                    )
                    fields.append("categories")
                if fields:
                    item.save(update_fields=fields)
                results[item_id] = {
                    "id": item_id, "success": True, "changekey": item.changekey, "updated": bool(fields)
                }
            except Exception as exc:
                results[item_id] = _failure(item_id, exc)
        return {"results": [results[item_id] for item_id in ids]}

    def move_messages(self, ids, to_folder, confirm=False, confirmation_id=None):
        ids = _batch_ids(ids)
        if to_folder not in ("inbox", "sent"):
            raise ToolOperationError(
                "INVALID_MOVE_FOLDER", "普通邮件仅可在收件箱与已发送邮件间移动。"
            )
        folder = self._tool_folder(to_folder)
        items, failures, snapshot = [], {}, []
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id, folder_names=("inbox", "sent")))
                if item.is_draft:
                    raise ToolOperationError("DRAFT_MOVE_NOT_ALLOWED", "不能通过移动更改草稿状态。")
                items.append((item_id, item))
                snapshot.append({"id": item_id, **_item_state(item)})
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
                snapshot.append(failures[item_id])
        preview = require_confirmation(
            mailbox=self.config.email,
            action="move_messages",
            items=snapshot,
            details={"to_folder": to_folder},
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        results = dict(failures)
        for item_id, item in items:
            try:
                item.move(to_folder=folder)
                if not item.id:
                    raise ToolOperationError(
                        "MOVE_RESULT_UNKNOWN", "移动返回的新邮件 ID 不可用，请重新列出目标文件夹。"
                    )
                results[item_id] = {
                    "id": item_id,
                    "new_id": item.id,
                    "changekey": item.changekey,
                    "success": True,
                    "folder": to_folder,
                }
            except Exception as exc:
                results[item_id] = _failure(item_id, exc)
        return {"results": [results[item_id] for item_id in ids]}

    def delete_messages(self, ids, confirm=False, confirmation_id=None):
        ids = _batch_ids(ids)
        items, failures, snapshot = [], {}, []
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id))
                items.append((item_id, item))
                snapshot.append(_item_state(item))
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
                snapshot.append(failures[item_id])
        preview = require_confirmation(
            mailbox=self.config.email,
            action="delete_messages",
            items=snapshot,
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview:
            return preview
        results = dict(failures)
        for item_id, item in items:
            try:

                # move_to_trash() 内部为 MOVE_TO_DELETED_ITEMS（移至已删除文件夹）。
                # 原生 Message.delete() 是 HARD_DELETE（永久删除），语义不符，弃用。
                # 调用后对象 ID 会被清空；逐项结果以调用方输入 item_id 键控。
                item.move_to_trash()
                results[item_id] = {"id": item_id, "success": True, "deleted": True}
            except Exception as exc:
                results[item_id] = _failure(item_id, exc)
        return {"results": [results[item_id] for item_id in ids], "delete_type": MOVE_TO_DELETED_ITEMS}
