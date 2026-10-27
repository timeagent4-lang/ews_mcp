"""Scoped draft and ordinary mail mutations for the OA-resolved employee."""

import hashlib
import io
import json
import re
import uuid
import warnings
from html import escape as _html_escape

from exchangelib import FileAttachment, HTMLBody, Mailbox, Message
from exchangelib.items.base import MOVE_TO_DELETED_ITEMS, NEVER_OVERWRITE
from exchangelib.items.calendar_item import MeetingRequest

from mail_replies import meeting_text_reply

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
        # Preserve each item's Exchange ResponseCode for batch diagnosis.
        result["exchange_code"] = exc.__class__.__name__
        exchange_message = sanitize(str(exc))
        if exchange_message:
            result["exchange_message"] = exchange_message
    return result


def _new_body(text, source_body):
    """Escape new text using the source BodyType to preserve quoted HTML and cid images.

    Exchange's native reply/forward supplies the quoted original.
    """
    text = str(text or "")
    if isinstance(source_body, HTMLBody):
        return HTMLBody(_html_escape(text).replace("\n", "<br>"))
    return text


_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def _body_sha256(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _prepend_html(existing, snippet):
    """Insert without reserializing existing HTML, preserving styles and cid images.

    Reject documents whose body cannot be located safely.
    """
    existing = str(existing or "")
    if not existing.strip():
        return snippet
    match = _BODY_OPEN_RE.search(existing)
    if match:
        if not match.group(0).rstrip().endswith("/>"):
            return existing[:match.end()] + snippet + existing[match.end():]
    elif not re.search(r"<html\b|<!doctype", existing, re.IGNORECASE):
        return snippet + existing
    raise ToolOperationError(
        "BODY_PREPEND_UNSUPPORTED",
        "无法在现有 HTML 正文中可靠定位插入位置，已拒绝写入；请改用 body_action=replace。",
    )


def _prepend_body(source_body, text):
    """Escape only new text and preserve the existing body's BodyType."""
    text = str(text or "")
    if isinstance(source_body, HTMLBody):
        snippet = _html_escape(text).replace("\n", "<br>")
        return HTMLBody(_prepend_html(str(source_body or ""), snippet))
    existing = str(source_body or "")
    if not existing.strip():
        return text
    return f"{text}\n{existing}"


# Reject full documents rather than dropping styles from <head>.
_DOCUMENT_MARKER_RE = re.compile(r"<\s*/?\s*(!doctype|html|head|body)\b", re.IGNORECASE)
_CID_REF_RE = re.compile(r"""cid:\s*([^"'\s)>]+)""", re.IGNORECASE)


def _validate_html_fragment(fragment):
    text = str(fragment or "")
    if not text.strip():
        raise ToolOperationError("HTML_FRAGMENT_REQUIRED", "前置 HTML 片段不能为空。")
    if _DOCUMENT_MARKER_RE.search(text):
        raise ToolOperationError(
            "HTML_FRAGMENT_REQUIRED",
            "新增内容只接受 HTML 片段（如 <p>、<table>、<div>），不支持含 html/head/body/doctype "
            "的完整文档；请只提交片段本身，本次不自动提取 body，以免 <head> 中的样式被静默丢弃。",
        )
    return text


def _missing_cid_refs(fragment, attachments):
    """References must match images already attached to this draft."""
    referenced = {
        match.group(1).strip().strip("<>").lower()
        for match in _CID_REF_RE.finditer(str(fragment or ""))
    }
    if not referenced:
        return set()
    available = {
        str(content_id).strip().strip("<>").lower()
        for content_id in (
            getattr(attachment, "content_id", None) for attachment in attachments or []
        )
        if content_id
    }
    return referenced - available


def _html_prepend_body(draft, fragment):
    """Prepend caller-authored HTML verbatim while preserving existing markup."""
    if not isinstance(draft.body, HTMLBody):
        raise ToolOperationError(
            "INVALID_BODY_FORMAT",
            "纯文本草稿不支持前置 HTML 片段；请改传纯文本新增内容，或先用 body_action=replace "
            "把草稿正文转为 HTML（本次不做隐式格式转换）。",
        )
    missing = _missing_cid_refs(fragment, draft.attachments)
    if missing:
        raise ToolOperationError(
            "CID_ATTACHMENT_MISSING",
            "新增片段引用的内嵌图片在草稿现有附件中不存在："
            + ", ".join(sorted(missing))
            + "；请先上传并确认 attach_draft_attachment，再引用成功回执中的 ContentId。",
        )
    return HTMLBody(_prepend_html(str(draft.body or ""), fragment))


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


def _draft_version(draft):
    encoded = json.dumps(_item_state(draft), sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    return {'changekey': draft.changekey, 'snapshot_sha256': hashlib.sha256(encoded).hexdigest()}


def _check_draft_version(draft, expected_version):
    if not expected_version:
        raise ToolOperationError('PREVIEW_VERSION_REQUIRED', '请重新预览草稿后再确认。')
    if _draft_version(draft) != expected_version:
        raise ToolOperationError('DRAFT_CHANGED', '草稿在预览后发生变化，本次未执行；请重新预览。')


def _inline_type(data):
    from PIL import Image
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data), formats=['PNG', 'JPEG']) as image:
                if (image.width > 8192 or image.height > 8192 or image.width * image.height > 16000000
                        or getattr(image, 'n_frames', 1) != 1):
                    raise ValueError('image limit')
                format_ = image.format
                image.verify()
            with Image.open(io.BytesIO(data), formats=['PNG', 'JPEG']) as image:
                image.load()
        return 'image/png' if format_ == 'PNG' else 'image/jpeg'
    except Exception:
        raise ToolOperationError('INVALID_INLINE_IMAGE', '内嵌图片须为完整的单帧 PNG/JPEG，最大1600万像素且单边不超过8192。') from None


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
        body_format="text",
        reply_to=None,
        confirm=False,
        confirmation_id=None,
    ):
        if mode not in ("new", "reply", "reply_all", "forward"):
            raise ToolOperationError("INVALID_DRAFT_MODE", "不支持的草稿类型。")
        if body_format not in ("text", "html"):
            raise ToolOperationError("INVALID_BODY_FORMAT", "body_format 仅支持 text 或 html。")
        html_reply = body_format == "html" and mode != "new"
        if html_reply and str(body or "").strip():
            # NewBodyContent prefixes the quote; nonempty HTML must be a fragment.
            _validate_html_fragment(body)
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
                body=HTMLBody(body) if body_format == "html" else body,
                to_recipients=to,
                cc_recipients=cc,
                bcc_recipients=bcc,
                is_draft=True,
            )
        else:
            if not reply_to:
                raise ToolOperationError("ORIGINAL_REQUIRED", "回复或转发需要原邮件 ID。")
            original = self._tool_item(reply_to)
            if not (type(original) is MeetingRequest and mode in ("reply", "reply_all")):
                original = _ordinary_message(original)
            new_body = HTMLBody(body) if html_reply else _new_body(body, original.body)
            target_subject = subject or (
                ("FW: " if mode == "forward" else "RE: ") + (original.subject or "")
            )
            if mode == "forward":
                to, cc, bcc = _deduplicate(recipients)
                draft = original.create_forward(
                    subject=target_subject,
                    body=new_body,
                    to_recipients=to,
                    cc_recipients=cc,
                    bcc_recipients=bcc,
                )
                draft.author = author
            else:
                # 保留显式指定的自己地址；仅推导收件人时排除自己。
                explicit_to = list(recipients[0])
                explicit_cc = list(recipients[1])
                to = list(explicit_to)
                cc = list(explicit_cc)
                bcc = list(recipients[2])
                targets = original.reply_to or ([original.author] if original.author else [])
                if mode == "reply_all":
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
                    to = [r for r in targets if _address(r) != self.config.email.lower()]
                to, cc, bcc = _deduplicate([to, cc, bcc])
                if not to and not cc:
                    raise ToolOperationError(
                        "REPLY_RECIPIENT_REQUIRED", "原邮件没有可用的回复收件人，请明确指定。"
                    )
                if type(original) is MeetingRequest:
                    draft = meeting_text_reply(
                        original, mode=mode, subject=target_subject, body=new_body,
                        author=author, to=to, cc=cc, bcc=bcc)
                elif mode == "reply_all":
                    draft = original.create_reply_all(
                        subject=target_subject, body=new_body, author=author
                    )
                    # SDK defaults copy Bcc and ignore Reply-To; replace all recipient fields.
                    draft.to_recipients, draft.cc_recipients, draft.bcc_recipients = to, cc, bcc
                else:
                    draft = original.create_reply(
                        subject=target_subject,
                        body=new_body,
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
                "body_format": body_format,
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
        body_format=None,
        body_action=None,
        confirm=False,
        confirmation_id=None,
        expected_version=None,
    ):
        if body_format is not None and body_format not in ("text", "html"):
            raise ToolOperationError("INVALID_BODY_FORMAT", "body_format 仅支持 text 或 html。")
        if body_action is not None and body_action not in ("replace", "prepend"):
            raise ToolOperationError("INVALID_BODY_ACTION", "body_action 仅支持 replace 或 prepend。")
        if body is None and (body_format is not None or body_action is not None):
            raise ToolOperationError(
                "BODY_PARAM_REQUIRES_BODY",
                "body_format/body_action 仅在提供 body 时生效；只改收件人或主题时请一并省略。",
            )
        action = body_action or "replace"
        html_prepend = action == "prepend" and body_format == "html"
        if html_prepend:
            # HTML 整体替换允许完整文档；仅前置要求片段。
            _validate_html_fragment(body)

        draft = self._write_draft(draft_id)
        if expected_version is not None:
            # 拒绝覆盖预览后被 Outlook 或其他操作修改的草稿。
            current = {"changekey": draft.changekey, "body_sha256": _body_sha256(draft.body)}
            if current != expected_version:
                raise ToolOperationError(
                    "DRAFT_CHANGED",
                    "草稿在预览后被其他操作修改，本次未执行；请重新预览后再确认。",
                )

        updates = {}
        if subject is not None:
            updates["subject"] = subject
        if body is not None:
            if action == "prepend":
                updates["body"] = (
                    _html_prepend_body(draft, body) if html_prepend
                    else _prepend_body(draft.body, body)
                )
            elif body_format == "html":
                updates["body"] = HTMLBody(body)
            else:
                updates["body"] = _new_body(body, draft.body)
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
                "body_action": action,
                "body_format": body_format or "text",
                "body_preview": str(body or "")[:500],
            }],
            details={
                "canonical_draft_id": draft.id,
                "subject": subject,
                "body_length": (len(body) if body is not None else None),
                "body_action": action,
                "body_format": body_format,
                "to": to_emails,
                "cc": cc_emails,
                "bcc": bcc_emails,
                # 确认时从服务端取回版本快照，不信任调用方回传值。
                "version": {"changekey": draft.changekey, "body_sha256": _body_sha256(draft.body)},
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
                # NeverOverwrite 使 Exchange 拒绝覆盖本次读取后发生的并发修改。
                self.submission_started = True
                draft.save(update_fields=list(updates), conflict_resolution=NEVER_OVERWRITE)
            except Exception as exc:
                raise ToolOperationError(
                    "DRAFT_UPDATE_FAILED", "更新草稿失败，请检查草稿权限或重新读取草稿。"
                ) from exc
        return {"id": draft.id, "changekey": draft.changekey, "updated": bool(updates), "folder": "drafts"}

    def attach_draft_attachment(self, draft_id, upload_id, is_inline=False, confirm=False,
                                confirmation_id=None, expected_version=None, expected_attachment=None):
        if type(is_inline) is not bool:
            raise ToolOperationError('INVALID_PARAMS', 'is_inline 必须为布尔值。')
        draft = self._write_draft(draft_id)
        if confirm:
            _check_draft_version(draft, expected_version)
            if not isinstance(expected_attachment, dict) or expected_attachment.get('is_inline') != is_inline:
                raise ToolOperationError('PREVIEW_VERSION_REQUIRED', '附件预览不完整，请重新预览。')
        uploads = self.attachment_uploads
        expected = None
        if confirm:
            expected = {**expected_attachment, 'content_type': expected_attachment.get('stored_content_type')}
        verified = uploads.open_verified(upload_id, lanid=self.requester_lanid, mailbox=self.config.email,
            operation_id=getattr(self, 'attachment_operation_id', None) if confirm else None, expected=expected)
        sizes = [getattr(a, 'size', None) for a in draft.attachments or []]
        if any(type(size) is not int or size < 0 for size in sizes):
            raise ToolOperationError('ATTACHMENT_SIZE_UNKNOWN', '现有附件大小不可确认，已拒绝附加。')
        if (len(sizes) + 1 > uploads.settings.draft_max_attachments
                or sum(sizes) + verified.metadata['size'] > uploads.settings.draft_max_bytes):
            raise ToolOperationError('DRAFT_ATTACHMENT_LIMIT', '草稿附件数量或总大小超过上限。')
        mime = _inline_type(verified.content) if is_inline else verified.metadata['content_type']
        content_id = expected_attachment.get('content_id') if confirm else (uuid.uuid4().hex + '@ews-mcp' if is_inline else None)
        if is_inline and (not content_id or any(str(getattr(a, 'content_id', '') or '').strip().strip('<>').lower() == content_id.lower()
                                               for a in draft.attachments or [])):
            raise ToolOperationError('CID_CONFLICT', '草稿已有相同 ContentId，请重新预览。')
        metadata = {**verified.metadata, 'stored_content_type': verified.metadata['content_type'],
                    'content_type': mime, 'is_inline': is_inline, 'content_id': content_id}
        if confirm and metadata != expected_attachment:
            raise ToolOperationError('UPLOAD_CHANGED', '附件元数据与预览不一致，请重新预览。')
        preview = require_confirmation(mailbox=self.config.email, action='attach_draft_attachment',
            items=[_item_state(draft)], details={'canonical_draft_id': draft.id, 'version': _draft_version(draft),
                'requester_lanid': self.requester_lanid, 'attachment': metadata}, confirm=confirm, confirmation_id=confirmation_id)
        if preview is not None:
            return preview
        attachment = FileAttachment(name=metadata['filename'], content=verified.content, size=metadata['size'],
                                    content_type=mime, is_inline=is_inline, content_id=content_id)
        try:
            self.submission_started = True
            draft.attach(attachment)
        except Exception as exc:
            code, message, _ = classify_submission_failure(exc, action='附加草稿附件',
                unknown_code='ATTACHMENT_FAILED_OR_UNKNOWN', rejected_code='ATTACHMENT_REJECTED')
            raise ToolOperationError(code, message) from exc
        return {'draft_id': draft.id,
                'attachment_id': getattr(getattr(attachment, 'attachment_id', None), 'id', None),
                'changekey': draft.changekey, 'filename': metadata['filename'],
                'size': metadata['size'], 'content_type': mime, 'sha256': metadata['sha256'],
                'is_inline': is_inline, 'content_id': content_id}

    def send_draft(self, draft_id, confirm=False, confirmation_id=None, expected_version=None):
        draft = self._write_draft(draft_id)
        if confirm:
            _check_draft_version(draft, expected_version)
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
            details={'version': _draft_version(draft), 'canonical_draft_id': draft.id},
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview:
            return preview
        require_send("发送邮件")
        sent_folder = self._tool_folder("sent")
        try:
            self.submission_started = True
            draft.send(save_copy=True, copy_to_folder=sent_folder)
        except Exception as exc:
            # A timeout can mean the send succeeded; a definite rejection did not.
            code, message, _ = classify_submission_failure(
                exc,
                action="发送邮件",
                unknown_code="SEND_FAILED_OR_UNKNOWN",
                rejected_code="SEND_REJECTED",
            )
            if code == "SEND_FAILED_OR_UNKNOWN":
                message = (
                    "邮箱服务不稳定 请五分钟后查看结果"
                    if "timeout" in exc.__class__.__name__.lower()
                    else "发送结果未知，请核实实际已发送邮件，勿重复提交。"
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
            # move_to_trash 可恢复且会清空对象 ID；Message.delete 默认永久删除。
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

                # move_to_trash 可恢复且会清空对象 ID；保留输入 ID 作为结果键。
                item.move_to_trash()
                results[item_id] = {"id": item_id, "success": True, "deleted": True}
            except Exception as exc:
                results[item_id] = _failure(item_id, exc)
        return {"results": [results[item_id] for item_id in ids], "delete_type": MOVE_TO_DELETED_ITEMS}
