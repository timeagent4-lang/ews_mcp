"""Scoped, bounded mail reads for the already authenticated employee account."""

from pathlib import PurePath

from bs4 import BeautifulSoup
from exchangelib import EWSDateTime, FileAttachment, ItemAttachment, Message
from exchangelib.folders import (
    Calendar,
    Contacts,
    DeletedItems,
    Drafts,
    Inbox,
    Root,
    SentItems,
    Tasks,
)

from config import DownloadSettings, downloads_enabled
from attachment_downloads import AttachmentDownloadStore
from tool_support import LOCAL_TIMEZONE, ToolOperationError, iso_datetime, parse_datetime


MAIL_FOLDERS = ("inbox", "drafts", "sent")
# The folders whose reachability decides which tools are usable, probed by
# list_folders. Deliberately a separate vocabulary from MAIL_FOLDERS: that one
# doubles as the ID scope for _tool_item (mail items are only ever looked up in
# inbox/drafts/sent), so widening it would widen the ID scope too. deleteditems
# is absent on purpose -- no registered tool addresses it (deletions go through
# move_to_trash), which never resolves that folder).
TOOL_FOLDERS = ("inbox", "drafts", "sent", "calendar", "contacts", "tasks")
SIMPLE_FIELDS = (
    "parent_folder_id",
    "subject",
    "datetime_received",
    "datetime_sent",
    "datetime_created",
    "is_read",
    "has_attachments",
    "conversation_id",
)
MESSAGE_FIELDS = SIMPLE_FIELDS + (
    "sender",
    "author",
    "to_recipients",
    "cc_recipients",
    "importance",
    "attachments",
    "is_draft",
    "categories",
)
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
MAX_ATTACHMENT_CHARS = 20000
TEXT_EXTENSIONS = {".txt", ".csv", ".tsv", ".log", ".md", ".json", ".xml"}


def _folder_coverage(exc) -> str:
    """Classify a folder access failure as denied vs unreachable."""
    name = exc.__class__.__name__
    if name.startswith("Error") and (
        "Denied" in name or "NotFound" in name or "Access" in name
    ):
        return "denied"
    return "unreachable"


def _mailbox(value):
    if value is None:
        return None
    return {"name": value.name, "email_address": value.email_address}


def _attachment_info(value):
    return {
        "attachment_id": value.attachment_id.id if value.attachment_id else None,
        "name": value.name,
        "size": value.size,
        "content_type": value.content_type,
        "kind": "item" if isinstance(value, ItemAttachment) else "file",
        "is_inline": value.is_inline,
    }


def _page(offset, limit):
    if (
        type(offset) is not int
        or offset < 0
        or type(limit) is not int
        or not 1 <= limit <= 100
    ):
        raise ToolOperationError(
            "INVALID_PAGINATION", "offset 必须非负且 limit 必须为 1 到 100。"
        )


def _fields(include_body=True):
    fields = list(MESSAGE_FIELDS)
    if "flag_status" in Message.FIELDS:
        fields.append("flag_status")
    if include_body:
        fields.append("body")
    return fields


class MailOperations:
    def _tool_folder(self, name):
        classes = {
            "inbox": Inbox,
            "drafts": Drafts,
            "sent": SentItems,
            "calendar": Calendar,
            "contacts": Contacts,
            "tasks": Tasks,
            "deleteditems": DeletedItems,
        }
        if name not in classes:
            raise ToolOperationError(
                "INVALID_FOLDER",
                "仅支持 inbox、drafts、sent、calendar、contacts、tasks、deleteditems 文件夹。",
            )
        cache = self.__dict__.setdefault("_tool_folders", {})
        if name not in cache:
            # Do not touch Account.root or its default-folder discovery cache.
            cache[name] = classes[name].get_distinguished(root=Root(account=self.account))
        return cache[name]

    def _tool_item(self, item_id, folder_names=MAIL_FOLDERS, only_fields=None):
        if not isinstance(item_id, str) or not item_id.strip() or not folder_names:
            raise ToolOperationError("INVALID_ITEM_ID", "请提供有效的项目 ID。")
        # 逐个解析允许文件夹。无关文件夹（优先 DELEGATE 权限的 drafts/sent）不可达时
        # 逐个跳过，“不得”因它们整体提前失败——目标项目真正归属的文件夹才决定能否读取。
        folders = {}          # 可解析的允许文件夹: name -> Folder
        unresolved = {}       # 不可解析的文件夹: name -> 异常（无可达文件夹时用于如实上报）
        for name in folder_names:
            try:
                folders[name] = self._tool_folder(name)
            except Exception as exc:  # noqa: BLE001 - 需区分“无关文件夹不可达”与“全部不可达”
                unresolved[name] = exc
        if not folders:
            # 没有任何允许文件夹可用，抛出一个上游异常，而不是伪装成“无权限/空结果/成功”
            raise next(iter(unresolved.values()))
        # 用第一个可解析文件夹做元数据探测（GetItem 按 Id，跨文件夹取 parent_folder_id 以判定归属）。
        first = next(iter(folders.values()))
        metadata = self._fetch_one(item_id, first, ["parent_folder_id"])
        parent_id = metadata.parent_folder_id.id if metadata.parent_folder_id else None
        folder = next(
            (
                folders[name]
                for name in folder_names
                if name in folders and folders[name].id == parent_id
            ),
            None,
        )
        if folder is None:
            # 目标项父文件夹不在“可访问且允许”范围（含：位于不可达允许文件夹，或越界） -> 拒绝，不取正文。
            raise ToolOperationError(
                "ITEM_OUT_OF_SCOPE", "项目不属于当前员工允许访问的文件夹。"
            )
        if only_fields is not None and set(only_fields) <= {
            "parent_folder_id",
            "id",
            "changekey",
        }:
            metadata.folder = folder
            return metadata
        fields = (
            None
            if only_fields is None
            else list(dict.fromkeys((*only_fields, "parent_folder_id")))
        )
        item = self._fetch_one(item_id, folder, fields)
        if not item.parent_folder_id or item.parent_folder_id.id != folder.id:
            raise ToolOperationError(
                "ITEM_OUT_OF_SCOPE", "项目的父文件夹已变化，请重新读取。"
            )
        item.folder = folder
        return item

    def _fetch_one(self, item_id, folder, fields):
        item = next(
            iter(
                self.account.fetch(
                    ids=[(item_id, None)], folder=folder, only_fields=fields
                )
            ),
            None,
        )
        if item is None:
            raise ToolOperationError("ITEM_NOT_FOUND", "未找到指定项目。")
        if isinstance(item, Exception):
            raise item
        return item

    def list_folders(self):
        """Probe reach to every folder that gates tool availability.

        Each TOOL_FOLDERS entry is resolved through a real EWS request, so the
        result is a permission signal, not a static list. A denied/unreachable
        folder is reported explicitly instead of failing the whole call."""
        folders = []
        coverage = {}
        for name in TOOL_FOLDERS:
            try:
                folder = self._tool_folder(name)
            except Exception as exc:  # noqa: BLE001 - per-folder coverage
                status = _folder_coverage(exc)
                coverage[name] = status
                folders.append(
                    {"name": name, "folder": name, "accessible": False, "error": status}
                )
                continue
            coverage[name] = "ok"
            folders.append(
                {
                    "name": name,
                    # The alias this service uses for the folder. Only the mail
                    # subset of TOOL_FOLDERS is accepted as a `folder` argument by
                    # the mail tools; each such field carries its own enum. `id`
                    # below is diagnostic only: raw EWS folder ids are rejected,
                    # so no caller can point a tool at a folder outside this
                    # employee's own mailbox.
                    "folder": name,
                    "accessible": True,
                    "id": folder.id,
                    "display_name": folder.name,
                    "total_count": folder.total_count,
                    "unread_count": folder.unread_count,
                }
            )
        return {
            "items": folders,
            "coverage": coverage,
            "partial": any(status != "ok" for status in coverage.values()),
        }

    @staticmethod
    def _message_dict(
        item,
        *,
        include_body=True,
        clean_body=False,
        max_body_chars=10000,
        body_offset=0,
    ):
        received = item.datetime_received
        result = {
            "id": item.id,
            "changekey": item.changekey,
            "subject": item.subject or "",
            "sender": item.sender.email_address if item.sender else None,
            "datetime_received": (
                parse_datetime(received)
                .astimezone(LOCAL_TIMEZONE)
                .strftime("%Y-%m-%d %H:%M")
                if received
                else None
            ),
            "from": _mailbox(item.author),
            "author": _mailbox(item.author),
            "sender_details": _mailbox(item.sender),
            "to_recipients": [_mailbox(m) for m in (item.to_recipients or [])],
            "cc_recipients": [_mailbox(m) for m in (item.cc_recipients or [])],
            "received_at": iso_datetime(received),
            "sent_at": iso_datetime(item.datetime_sent),
            "created_at": iso_datetime(item.datetime_created),
            "is_read": item.is_read,
            "is_draft": item.is_draft,
            "importance": item.importance,
            "flag_status": getattr(item, "flag_status", None),
            "categories": list(item.categories or []),
            "conversation_id": item.conversation_id.id if item.conversation_id else None,
            "has_attachments": item.has_attachments,
            "attachments": [_attachment_info(a) for a in (item.attachments or [])],
        }
        if include_body:
            body = str(item.body or "")
            if clean_body:
                body = BeautifulSoup(body, "html.parser").get_text("\n", strip=True)
            end = body_offset + max_body_chars
            result.update(
                body=body[body_offset:end],
                body_offset=body_offset,
                body_length=len(body),
                body_truncated=end < len(body),
                next_body_offset=end if end < len(body) else None,
            )
        return result

    def find_message(
        self,
        query="",
        aqs=None,
        folder="inbox",
        since=None,
        until=None,
        is_unread=None,
        has_attachments=None,
        offset=0,
        limit=10,
        include_body=True,
    ):
        _page(offset, limit)
        if folder not in MAIL_FOLDERS:
            raise ToolOperationError(
                "INVALID_FOLDER", "邮件搜索仅支持 inbox、drafts、sent。"
            )
        if aqs is not None and (
            query
            or any(v is not None for v in (since, until, is_unread, has_attachments))
        ):
            raise ToolOperationError(
                "INVALID_QUERY", "AQS 不能与 query 或结构化筛选混用。"
            )
        target = self._tool_folder(folder)
        time_field = (
            "datetime_created"
            if folder == "drafts"
            else "datetime_sent"
            if folder == "sent"
            else "datetime_received"
        )
        if aqs is not None:
            if not isinstance(aqs, str) or not aqs.strip():
                raise ToolOperationError("INVALID_QUERY", "AQS 不能为空。")
            queryset = target.filter(aqs)
        else:
            filters = {}
            if query:
                filters["subject__contains"] = query
            if since is not None:
                filters[time_field + "__gte"] = EWSDateTime.from_datetime(
                    parse_datetime(since).astimezone(LOCAL_TIMEZONE)
                )
            if until is not None:
                filters[time_field + "__lt"] = EWSDateTime.from_datetime(
                    parse_datetime(until).astimezone(LOCAL_TIMEZONE)
                )
            if since is not None and until is not None and parse_datetime(since) >= parse_datetime(until):
                raise ToolOperationError(
                    "INVALID_DATETIME_RANGE", "since 必须早于 until。"
                )
            if is_unread is not None:
                filters["is_read"] = not is_unread
            if has_attachments is not None:
                filters["has_attachments"] = has_attachments
            queryset = target.filter(**filters)
        rows = list(
            queryset.only(*SIMPLE_FIELDS)
            .order_by("-" + time_field)[offset : offset + limit + 1]
        )
        # EWS may yield exceptions as rows, including the pagination lookahead.
        # Preserve the upstream error instead of claiming a successful page.
        for row in rows:
            if isinstance(row, Exception):
                raise row
        items = []
        for row in rows[:limit]:
            item = self._tool_item(
                row.id, folder_names=(folder,), only_fields=_fields(include_body)
            )
            items.append(self._message_dict(item, include_body=include_body))
        return {
            "items": items,
            "next_offset": offset + limit if len(rows) > limit else None,
        }

    def get_message(
        self, message_id, clean_body=False, max_body_chars=10000, body_offset=0
    ):
        if (
            type(body_offset) is not int
            or body_offset < 0
            or type(max_body_chars) is not int
            or not 1 <= max_body_chars <= 100000
        ):
            raise ToolOperationError(
                "INVALID_BODY_RANGE", "正文偏移必须非负，长度必须为 1 到 100000。"
            )
        item = self._tool_item(message_id, only_fields=_fields())
        return self._message_dict(
            item,
            clean_body=clean_body,
            max_body_chars=max_body_chars,
            body_offset=body_offset,
        )

    def get_thread(self, message_id, offset=0, limit=20):
        _page(offset, limit)
        source = self._tool_item(message_id, only_fields=["conversation_id"])
        if not source.conversation_id:
            raise ToolOperationError(
                "NO_CONVERSATION_ID", "此邮件没有 conversation ID，无法读取会话。"
            )
        rows = []
        coverage = {}
        for name in ("inbox", "sent"):
            try:
                target = self._tool_folder(name)
            except Exception as exc:  # noqa: BLE001 - per-folder coverage
                coverage[name] = _folder_coverage(exc)
                continue
            coverage[name] = "ok"
            queryset = (
                target.filter(conversation_id=source.conversation_id)
                .only(*SIMPLE_FIELDS)
                .order_by("datetime_sent")
            )
            rows.extend((row, name) for row in queryset[: offset + limit + 1])
        if not any(status == "ok" for status in coverage.values()):
            raise ToolOperationError(
                "THREAD_FOLDERS_UNAVAILABLE", "收件箱与已发送邮件均不可访问，无法读取会话。"
            )
        rows.sort(
            key=lambda pair: (
                iso_datetime(
                    pair[0].datetime_sent
                    or pair[0].datetime_received
                    or pair[0].datetime_created
                )
                or "",
                pair[0].id,
            )
        )
        page = rows[offset : offset + limit]
        items = [
            self._message_dict(
                self._tool_item(row.id, folder_names=(name,), only_fields=_fields())
            )
            for row, name in page
        ]
        partial = any(status != "ok" for status in coverage.values())
        return {
            "conversation_id": source.conversation_id.id,
            "items": items,
            "coverage": coverage,
            "partial": partial,
            "next_offset": offset + limit if len(rows) > offset + limit else None,
        }

    def _save_export(self, attachment, *, store=None):
        """Stream a bounded attachment into the existing mailbox export directory."""
        if not isinstance(attachment, FileAttachment):
            raise ToolOperationError(
                "ATTACHMENT_NOT_SUPPORTED", "仅支持保存文件类附件。"
            )
        store = store or AttachmentDownloadStore(DownloadSettings.from_env())
        return store.save(
            self.config.email, attachment.name, attachment.content_type,
            lambda: attachment.fp, reported_size=attachment.size,
        )

    def prepare_attachment_download(self, message_id, attachment_id):
        """Authorize the mailbox/item before caching bytes and issuing a link."""
        if not downloads_enabled():
            raise ToolOperationError("DOWNLOAD_DISABLED", "附件链接下载未启用。")
        for value in (message_id, attachment_id):
            if not isinstance(value, str) or not value.strip() or len(value) > 4096:
                raise ToolOperationError("INVALID_ATTACHMENT_ID", "请提供邮件 ID 和附件 ID。")
        try:
            settings = DownloadSettings.from_env()
        except ValueError:
            raise ToolOperationError("DOWNLOAD_CONFIG_INVALID", "附件下载配置无效，请联系管理员。") from None
        item = self._tool_item(message_id, only_fields=["attachments"])
        attachment = self._attachment_by_id(item.attachments or [], attachment_id)
        store = AttachmentDownloadStore(settings)
        exported = self._save_export(attachment, store=store)
        return store.issue(exported["download_id"])

    @staticmethod
    def _attachment_by_id(attachments, attachment_id):
        for attachment in attachments:
            if attachment.attachment_id and attachment.attachment_id.id == attachment_id:
                return attachment
        raise ToolOperationError("ATTACHMENT_NOT_FOUND", "该附件 ID 不属于指定邮件。")

    def get_attachment(self, message_id, attachment_id=None, mode="auto", save=False):
        if mode not in ("info", "text", "auto"):
            raise ToolOperationError(
                "INVALID_ATTACHMENT_MODE", "附件模式仅支持 info、text、auto。"
            )
        if not isinstance(save, bool):
            raise ToolOperationError("INVALID_SAVE_FLAG", "save 必须为布尔值。")
        item = self._tool_item(message_id, only_fields=["attachments"])
        attachments = item.attachments or []
        if attachment_id is None:
            return {
                "message_id": message_id,
                "attachments": [_attachment_info(a) for a in attachments],
            }
        attachment = self._attachment_by_id(attachments, attachment_id)
        if save:
            return self._save_export(attachment)
        info = _attachment_info(attachment)
        content_type = (attachment.content_type or "").split(";", 1)[0].lower()
        supported = isinstance(attachment, FileAttachment) and (
            PurePath(attachment.name or "").suffix.lower() in TEXT_EXTENSIONS
            or content_type.startswith("text/")
            or content_type in ("application/json", "application/xml")
        )
        info["text_supported"] = supported
        if mode == "info":
            return info
        if not supported:
            info["notice"] = "此附件类型仅提供元数据，不支持文本提取。"
            return info
        if attachment.size is not None and attachment.size > MAX_ATTACHMENT_BYTES:
            raise ToolOperationError(
                "ATTACHMENT_TOO_LARGE", "附件超过 5 MiB 文本读取上限。"
            )
        data = bytearray()
        with attachment.fp as stream:
            while True:
                chunk = stream.read(min(65536, MAX_ATTACHMENT_BYTES + 1 - len(data)))
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > MAX_ATTACHMENT_BYTES:
                    raise ToolOperationError(
                        "ATTACHMENT_TOO_LARGE", "附件实际内容超过 5 MiB 文本读取上限。"
                    )
        encodings = (
            ["utf-16"] if data.startswith((b"\xff\xfe", b"\xfe\xff")) else ["utf-8-sig", "gb18030"]
        )
        text = None
        encoding = None
        for candidate in encodings:
            try:
                text = data.decode(candidate)
                encoding = candidate
                break
            except UnicodeDecodeError:
                continue
        if text is None or any(
            ord(c) < 32 and c not in "\t\n\r\f" for c in text
        ):
            raise ToolOperationError(
                "ATTACHMENT_DECODE_ERROR", "附件无法解码为受支持的文本。"
            )
        info.update(
            text=text[:MAX_ATTACHMENT_CHARS],
            truncated=len(text) > MAX_ATTACHMENT_CHARS,
            text_length=len(text),
            bytes_read=len(data),
            encoding=encoding,
        )
        return info

    def get_mailbox_overview(self, limit=5):
        _page(0, limit)
        inbox = self._tool_folder("inbox")
        return {
            "inbox_total_count": inbox.total_count,
            "inbox_unread_count": inbox.unread_count,
            "recent_unread": self.find_message(
                is_unread=True, limit=limit, include_body=False
            ),
        }
