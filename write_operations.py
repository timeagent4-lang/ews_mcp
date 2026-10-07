"""当前目标邮箱的草稿与普通邮件写入操作。"""

import re
from html import escape as _html_escape

from exchangelib import HTMLBody, Mailbox, Message
from exchangelib.items.base import MOVE_TO_DELETED_ITEMS, NEVER_OVERWRITE

from tool_support import (
    ToolOperationError,
    classify_submission_failure,
    error_details,
    move_to_deleted_items,
    require_send,
    validate_item_fields,
)


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


def _failure(item_id, exc, *, submitted=False):
    return {"id": item_id, "success": False, **error_details(exc, submitted=submitted)}


def _new_body(text, source_body):
    """Serialize caller-supplied plain text with ``source_body``'s BodyType.

    A bare Python ``str`` reaches ``BodyField.clean()``, which coerces it to
    ``Body`` -> ``BodyType="Text"``. Exchange then flattens the *whole* composed
    item to plain text, so a quoted HTML original loses its tables and its
    inline images lose the ``cid:`` reference that kept them out of the
    attachment list. Mirroring the source item's BodyType keeps an HTML thread
    HTML.

    The caller string is never treated as trusted HTML: it is escaped, and
    newlines become ``<br>`` so they survive HTML whitespace collapsing. The
    quoted original is left to Exchange's native reply/forward (reference item
    id) -- we only supply the new content.
    """
    text = str(text or "")
    if isinstance(source_body, HTMLBody):
        return HTMLBody(_html_escape(text).replace("\n", "<br>"))
    return text


# The opening <body ...> tag. Only used to find a splice anchor, never to pick
# apart the markup: everything between anchors is copied through untouched.
_BODY_OPEN_RE = re.compile(r"<body\b[^>]*>", re.IGNORECASE)


def _prepend_html(existing, snippet):
    """Splice ``snippet`` in at the top of an HTML body's content.

    The markup that is already there is never parsed and re-serialized -- the
    new fragment is inserted straight after the opening ``<body ...>`` tag (or
    in front of a bare fragment), so tables, inline ``style=`` attributes,
    ``cid:`` image references and their attachment ContentIds survive byte for
    byte. A full document with no locatable ``<body>`` is refused rather than
    guessed at: rebuilding the body would silently drop Outlook's own markup.
    """
    existing = str(existing or "")
    if not existing.strip():
        return snippet
    match = _BODY_OPEN_RE.search(existing)
    if match:
        if not match.group(0).rstrip().endswith("/>"):
            return existing[:match.end()] + snippet + existing[match.end():]
    elif not re.search(r"<html\b|<!doctype", existing, re.IGNORECASE):
        # No <body> and not a document: a bare fragment. Prefix it verbatim.
        return snippet + existing
    raise ToolOperationError(
        "BODY_PREPEND_UNSUPPORTED",
        "无法在现有 HTML 正文中可靠定位插入位置，已拒绝写入；请改用 body_action=replace。",
    )


def _prepend_body(source_body, text):
    """Existing body with ``text`` added at its start, BodyType preserved.

    Only the new text is escaped; whatever is already in the draft is carried
    through as is, in the BodyType it already has, so an HTML thread stays HTML
    and a plain-text one stays plain text.
    """
    text = str(text or "")
    if isinstance(source_body, HTMLBody):
        snippet = _html_escape(text).replace("\n", "<br>")
        return HTMLBody(_prepend_html(str(source_body or ""), snippet))
    existing = str(source_body or "")
    if not existing.strip():
        return text
    return f"{text}\n{existing}"


# Anything that marks a whole document rather than a fragment. A full document
# is refused instead of having its <body> extracted: the <head> can carry the
# styles, and silently dropping it would change how the content renders.
_DOCUMENT_MARKER_RE = re.compile(r"<\s*(!doctype|html|head|body)\b", re.IGNORECASE)
# cid: URL references to inline images, e.g. src="cid:logo@contoso".
_CID_REF_RE = re.compile(r"""cid:\s*([^"'\s)>]+)""", re.IGNORECASE)


def _validate_html_fragment(fragment):
    """Refuse a whole HTML document where a fragment is required."""
    text = str(fragment or "")
    if not text.strip():
        raise ToolOperationError("HTML_FRAGMENT_REQUIRED", "前置 HTML 片段不能为空。")
    if _DOCUMENT_MARKER_RE.search(text):
        raise ToolOperationError(
            "HTML_FRAGMENT_REQUIRED",
            "前置内容只接受 HTML 片段（如 <p>、<table>、<div>），不支持含 html/head/body/doctype "
            "的完整文档；请只提交片段本身，本次不自动提取 body，以免 <head> 中的样式被静默丢弃。",
        )
    return text


def _missing_cid_refs(fragment, attachments):
    """ContentIds referenced by ``fragment`` that no existing attachment provides.

    The new fragment is submitted as-is, so an inline image reference it makes
    must resolve to an attachment already on the draft. This version never
    uploads images, so a dangling reference is refused rather than written out
    as a broken image.
    """
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
    """Existing HTML body with a raw HTML ``fragment`` spliced in at its start.

    Unlike ``_prepend_body`` the fragment is not escaped -- it is HTML the
    caller authored. The original body is still carried through byte for byte
    by ``_prepend_html``; only a draft that is already HTML can receive it, and
    any inline image the fragment references must already exist on the draft.
    """
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
            + "；本次不提供图片上传，请改为引用草稿中已有内嵌图片的 ContentId，或不引用内嵌图片。",
        )
    return HTMLBody(_prepend_html(str(draft.body or ""), fragment))


def _ordinary_message(item):
    # Meeting requests/cancellations are separate SDK classes and have other side effects.
    if type(item) is not Message:
        raise ToolOperationError("NOT_MAIL_MESSAGE", "此操作仅支持普通邮件。")
    if not item.id or not item.changekey:
        raise ToolOperationError("INVALID_ITEM_ID", "邮件响应缺少 ID 或 ChangeKey，本次未提交写入。")
    return item


class WriteOperations:
    def _write_draft(self, draft_id):
        draft = _ordinary_message(self._tool_item(draft_id, folder_names=("drafts",)))
        if not draft.is_draft or getattr(draft.parent_folder_id, "id", None) != self._tool_folder("drafts").id:
            raise ToolOperationError(
                "NOT_A_DRAFT", "邮件必须位于目标邮箱草稿箱且处于草稿状态。"
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
    ):
        if mode not in ("new", "reply", "reply_all", "forward"):
            raise ToolOperationError("INVALID_DRAFT_MODE", "不支持的草稿类型。")
        if body_format not in ("text", "html"):
            raise ToolOperationError("INVALID_BODY_FORMAT", "body_format 仅支持 text 或 html。")
        if body_format == "html" and mode != "new":
            # 回复/转发的新增文本始终按纯文本转义；引用原文自带格式。若把调用方
            # 的字符串直接当 HTML，等于让调用方替换掉整封信的标记与引文。
            raise ToolOperationError(
                "INVALID_BODY_FORMAT",
                "HTML 正文仅支持 mode=new；回复/转发请传纯文本，引用原文的格式由 Exchange 保留。",
            )
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
                # html is used verbatim by explicit request; the default text path
                # keeps handing exchangelib a plain str (coerced to Body/Text), so
                # nothing changes for callers that never ask for HTML.
                body=HTMLBody(body) if body_format == "html" else body,
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
                    body=_new_body(body, original.body),
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
                    draft = original.create_reply_all(
                        subject=target_subject, body=_new_body(body, original.body), author=author
                    )
                    # SDK defaults copy Bcc and ignore Reply-To; replace all recipient fields.
                    draft.to_recipients, draft.cc_recipients, draft.bcc_recipients = to, cc, bcc
                else:
                    draft = original.create_reply(
                        subject=target_subject,
                        body=_new_body(body, original.body),
                        to_recipients=to,
                        cc_recipients=cc,
                        bcc_recipients=bcc,
                        author=author,
                    )
                    # create_reply defaults an empty To back to the author.
                    draft.to_recipients = to
        validate_item_fields(draft)
        try:
            saved = draft.save() if mode == "new" else draft.save(folder=folder)
        except Exception as exc:
            raise ToolOperationError(
                "DRAFT_CREATE_FAILED", "保存草稿失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        if not saved or not saved.id or not saved.changekey:
            raise ToolOperationError(
                "DRAFT_SAVE_RESULT_UNKNOWN", "保存响应缺少草稿 ID 或 ChangeKey，请先检查草稿箱。",
                status="unknown",
            )
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
    ):
        if body_format is not None and body_format not in ("text", "html"):
            raise ToolOperationError("INVALID_BODY_FORMAT", "body_format 仅支持 text 或 html。")
        if body_action is not None and body_action not in ("replace", "prepend"):
            raise ToolOperationError("INVALID_BODY_ACTION", "body_action 仅支持 replace 或 prepend。")
        if body is None and (body_format is not None or body_action is not None):
            # 静默忽略比拒绝更危险：调用方以为提交了 HTML 或前置，实际什么都没发生。
            raise ToolOperationError(
                "BODY_PARAM_REQUIRES_BODY",
                "body_format/body_action 仅在提供 body 时生效；只改收件人或主题时请一并省略。",
            )
        action = body_action or "replace"
        html_prepend = action == "prepend" and body_format == "html"
        if html_prepend:
            # 只在前置时才校验片段形态；replace+html 提交的是完整正文，不是片段。
            _validate_html_fragment(body)

        draft = self._write_draft(draft_id)
        updates = {}
        if subject is not None:
            updates["subject"] = subject
        if body is not None:
            if action == "prepend":
                # 只新增一段内容：读完整现有正文，在开头插入；原正文（表格/样式/
                # 引用邮件/内嵌图片）不重建、不截断，附件与 cid 引用保持不动。
                # html 分支提交的是调用方自写的片段，不转义，但要求目标草稿本身是 HTML。
                updates["body"] = (
                    _html_prepend_body(draft, body) if html_prepend
                    else _prepend_body(draft.body, body)
                )
            elif body_format == "html":
                # 显式 HTML 且整体替换：整段按 HTML 提交，不做转义。
                updates["body"] = HTMLBody(body)
            else:
                # 旧调用（未传新参数）走这里，行为与原有版本完全一致：按草稿当前
                # BodyType 序列化纯文本，避免更新正文时把 HTML 草稿降级回 Text。
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
        for field, value in updates.items():
            setattr(draft, field, value)
        if updates:
            validate_item_fields(draft, fields=list(updates))
            try:
                # NeverOverwrite 是 EWS 侧的乐观并发：ChangeKey 已随 ItemId 发出，
                # 若在本次读取之后、写入之前又被改动，Exchange 会拒绝而不是覆盖。
                draft.save(update_fields=list(updates), conflict_resolution=NEVER_OVERWRITE)
            except Exception as exc:
                raise ToolOperationError(
                    "DRAFT_UPDATE_FAILED", "更新草稿失败，请重新读取草稿。",
                    status=error_details(exc, submitted=True)["status"],
                ) from exc
        return {"id": draft.id, "changekey": draft.changekey, "updated": bool(updates), "folder": "drafts"}

    def send_draft(self, draft_id):
        draft = self._write_draft(draft_id)
        if _address(draft.author) != self.config.email.lower():
            raise ToolOperationError(
                "DRAFT_AUTHOR_MISMATCH", "草稿发件人与目标邮箱不一致，请先更新草稿。"
            )
        if not any((draft.to_recipients, draft.cc_recipients, draft.bcc_recipients)):
            raise ToolOperationError("RECIPIENT_REQUIRED", "发送邮件前必须指定收件人。")
        require_send("发送邮件")
        sent_folder = self._tool_folder("sent")
        validate_item_fields(draft, fields=())
        try:
            draft.send(save_copy=True, copy_to_folder=sent_folder)
        except Exception as exc:
            # A timeout can mean the send succeeded; a definite rejection did not.
            code, message, uncertain = classify_submission_failure(
                exc,
                action="发送邮件",
                unknown_code="SEND_FAILED_OR_UNKNOWN",
                rejected_code="SEND_REJECTED",
            )
            raise ToolOperationError(code, message, status="unknown" if uncertain else "failed") from exc
        # SendItem clears the draft ID and does not return a Sent item ID.
        return {"id": draft_id, "sent": True, "copy_folder": "sent"}

    def delete_draft(self, draft_id):
        draft = self._write_draft(draft_id)
        validate_item_fields(draft, fields=())
        try:
            move_to_deleted_items(draft)
        except Exception as exc:
            raise ToolOperationError(
                "DRAFT_DELETE_FAILED", "移至已删除邮件失败。",
                status=error_details(exc, submitted=True)["status"],
            ) from exc
        return {"id": draft_id, "deleted": True, "delete_type": MOVE_TO_DELETED_ITEMS}

    def update_messages(
        self,
        ids,
        set_read=None,
        categories_add=None,
        categories_remove=None,
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
        items, failures = [], {}
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id))
                items.append((item_id, item))
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
        results = dict(failures)
        for item_id, item in items:
            submitted = False
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
                    validate_item_fields(item, fields=fields)
                    submitted = True
                    item.save(update_fields=fields, conflict_resolution=NEVER_OVERWRITE)
                results[item_id] = {
                    "id": item_id, "success": True, "status": "success", "changekey": item.changekey, "updated": bool(fields)
                }
            except Exception as exc:
                results[item_id] = _failure(item_id, exc, submitted=submitted)
        return {"results": [results[item_id] for item_id in ids]}

    def move_messages(self, ids, to_folder):
        ids = _batch_ids(ids)
        if to_folder not in ("inbox", "sent"):
            raise ToolOperationError(
                "INVALID_MOVE_FOLDER", "普通邮件仅可在收件箱与已发送邮件间移动。"
            )
        folder = self._tool_folder(to_folder)
        items, failures = [], {}
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id, folder_names=("inbox", "sent")))
                if item.is_draft:
                    raise ToolOperationError("DRAFT_MOVE_NOT_ALLOWED", "不能通过移动更改草稿状态。")
                items.append((item_id, item))
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
        results = dict(failures)
        for item_id, item in items:
            submitted = False
            try:
                validate_item_fields(item, fields=())
                submitted = True
                item.move(to_folder=folder)
                if not item.id:
                    raise ToolOperationError(
                        "MOVE_RESULT_UNKNOWN", "移动响应缺少新邮件 ID，请先列出目标文件夹。",
                        status="unknown",
                    )
                results[item_id] = {
                    "id": item_id,
                    "new_id": item.id,
                    "changekey": item.changekey,
                    "success": True,
                    "status": "success",
                    "folder": to_folder,
                }
            except Exception as exc:
                results[item_id] = _failure(item_id, exc, submitted=submitted)
        return {"results": [results[item_id] for item_id in ids]}

    def delete_messages(self, ids):
        ids = _batch_ids(ids)
        items, failures = [], {}
        for item_id in ids:
            try:
                item = _ordinary_message(self._tool_item(item_id))
                items.append((item_id, item))
            except Exception as exc:
                failures[item_id] = _failure(item_id, exc)
        results = dict(failures)
        for item_id, item in items:
            submitted = False
            try:
                validate_item_fields(item, fields=())
                submitted = True
                move_to_deleted_items(item)
                results[item_id] = {"id": item_id, "success": True, "status": "success", "deleted": True}
            except Exception as exc:
                results[item_id] = _failure(item_id, exc, submitted=submitted)
        return {"results": [results[item_id] for item_id in ids], "delete_type": MOVE_TO_DELETED_ITEMS}
