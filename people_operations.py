"""目标邮箱中的普通联系人文件夹与 GAL 查询，直接执行资料修改。"""
from exchangelib import Contact, FileAttachment
from exchangelib.errors import ErrorNameResolutionNoResults
from exchangelib.folders import Contacts, Folder, MsgFolderRoot, Root
from exchangelib.folders.base import BaseFolder
from exchangelib.folders.collections import FolderCollection
from exchangelib.items import NEVER_OVERWRITE
from exchangelib.properties import FieldPath
from exchangelib.services import DeleteAttachment, GetFolder

from contact_fields import build_contact_values, contact_photo, serialize_contact, validate_contact
from tool_support import ToolOperationError, error_details


SEARCH_SCOPE_ACTIVE_DIRECTORY = "ActiveDirectory"
_FOLDER_FIELDS = ("name", "folder_class", "parent_folder_id", "total_count", "child_folder_count")
_SYSTEM_CONTACT_FOLDERS = {"PersonMetadata", "RecipientCache", "QuickContacts", "IMContactList", "OrganizationalContacts", "PeopleCentricConversationBuddies"}
_MAX_FOLDER_ANCESTORS = 256


def _folder_fields():
    return {FieldPath(field=BaseFolder.get_field_by_fieldname(name)) for name in _FOLDER_FIELDS}


def _parent_id(folder):
    parent = getattr(folder, "parent_folder_id", None)
    return getattr(parent, "id", None)


def _regular_contacts(folder):
    return getattr(folder, "folder_class", None) == "IPF.Contact" and type(folder).__name__ not in _SYSTEM_CONTACT_FOLDERS


def _folder_row(folder):
    return {"id": folder.id, "folder_id": folder.id, "name": folder.name,
            "folder_class": folder.folder_class, "parent_id": _parent_id(folder),
            "total_count": folder.total_count, "child_folder_count": folder.child_folder_count}


def _pagination(offset, limit):
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise ToolOperationError("INVALID_PAGINATION", "offset 必须非负，limit 必须为 1 到 100。")


def _directory_row(mailbox, contact):
    row = serialize_contact(contact) if contact is not None else {}
    row.update({"source": "gal", "email": getattr(mailbox, "email_address", None),
                "display_name": getattr(mailbox, "name", None) or getattr(contact, "display_name", None)})
    return row


def _contact_row(contact):
    row = serialize_contact(contact)
    row["email"] = next(iter(row["email_addresses"].values()), None)
    phones = row["phone_numbers"]
    row["phone"] = phones.get("BusinessPhone") or next(iter(phones.values()), None)
    return row


class PeopleOperations:
    def list_contact_folders(self):
        """一次分页发现 IPM 子树元数据；不使用 walk/parent/absolute 缓存。"""
        root = Root(account=self.account)
        ipm = MsgFolderRoot.get_distinguished(root=root)
        rows, folders = {}, []
        for folder in FolderCollection(account=self.account, folders=[ipm]).find_folders(
            depth="Deep", shape="IdOnly", additional_fields=_folder_fields(), page_size=100,
        ):
            if isinstance(folder, Exception):
                raise folder
            rows[folder.id] = _folder_row(folder)
            if _regular_contacts(folder):
                folders.append(folder)
        items = []
        for folder in folders:
            row = dict(rows[folder.id])
            names, visited = [], set()
            current = folder.id
            while current and current != ipm.id and current in rows and current not in visited:
                visited.add(current)
                names.append(rows[current]["name"] or "")
                current = rows[current]["parent_id"]
            row["path"] = "/" + "/".join(reversed(names))
            row["path_complete"] = current == ipm.id
            items.append(row)
        return {"items": sorted(items, key=lambda row: (row["path"], row["id"]))}

    def _contact_folder(self, folder_id=None):
        root = Root(account=self.account)
        if folder_id is None:
            folder = Contacts.get_distinguished(root=root)
        else:
            if not isinstance(folder_id, str) or not folder_id.strip():
                raise ToolOperationError("INVALID_CONTACT_FOLDER", "请使用 list_contact_folders 返回的文件夹 ID。")
            folder = self._folder_metadata(root, folder_id)
            if folder is None:
                raise ToolOperationError("CONTACT_FOLDER_NOT_FOUND", "未找到指定联系人文件夹。")
        if not _regular_contacts(folder):
            raise ToolOperationError("INVALID_CONTACT_FOLDER", "指定文件夹不是普通联系人文件夹。")
        if folder_id is not None:
            self._require_folder_scope(folder, root)
        return folder

    def _folder_metadata(self, root, folder_id):
        folder = next(iter(GetFolder(account=self.account).call(
            folders=[Folder(root=root, id=folder_id)], additional_fields=_folder_fields(), shape="IdOnly",
        )), None)
        if isinstance(folder, Exception):
            raise folder
        return folder

    def _require_folder_scope(self, folder, root):
        # raw FolderId 的 SOAP 不带 Mailbox；本地 Root 绑定不能证明实际邮箱归属。
        ipm = MsgFolderRoot.get_distinguished(root=root)
        metadata, visited = {folder.id: folder}, {folder.id}
        current = folder
        for _ in range(_MAX_FOLDER_ANCESTORS):
            parent_id = _parent_id(current)
            if ipm.id and parent_id == ipm.id:
                return
            if not parent_id or parent_id in visited:
                break
            visited.add(parent_id)
            parent = metadata.get(parent_id)
            if parent is None:
                # 保留真实 EWS 查询异常，不把权限拒绝解释为异箱或不存在。
                parent = self._folder_metadata(root, parent_id)
                if parent is None or parent.id != parent_id:
                    break
                metadata[parent_id] = parent
            current = parent
        raise ToolOperationError("ITEM_OUT_OF_SCOPE", "无法确认指定联系人文件夹属于目标邮箱，未继续读写。")

    def _fetch_contact(self, contact_id, folder):
        if not isinstance(contact_id, str) or not contact_id.strip():
            raise ToolOperationError("INVALID_CONTACT_ID", "请提供联系人 ID。")
        item = next(iter(self.account.fetch(ids=[(contact_id, None)], folder=folder)), None)
        if isinstance(item, Exception):
            raise item
        if item is None:
            raise ToolOperationError("CONTACT_NOT_FOUND", "未找到指定联系人。")
        if type(item) is not Contact:
            raise ToolOperationError("INVALID_CONTACT", "指定项目不是普通联系人。")
        if _parent_id(item) != folder.id:
            raise ToolOperationError("ITEM_OUT_OF_SCOPE", "项目不属于指定联系人文件夹。")
        item.folder = folder
        return item

    def list_contacts(self, folder_id=None, limit=50, offset=0):
        _pagination(offset, limit)
        folder = self._contact_folder(folder_id)
        rows = list(folder.all().order_by("display_name")[offset:offset + limit + 1])
        if any(isinstance(item, Exception) for item in rows):
            raise next(item for item in rows if isinstance(item, Exception))
        window = rows[:limit]
        items = [_contact_row(item) for item in window if type(item) is Contact]
        has_more = len(rows) > limit
        return {"items": items, "folder_id": folder.id, "offset": offset, "limit": limit,
                "has_more": has_more, "next_offset": offset + len(window) if has_more else None}

    def _resolve_directory(self, query):
        # ResolveNames 需要已协商版本；正常无匹配会返回异常对象而不是抛出。
        _ = self.account.version
        results = []
        for entry in self.account.protocol.resolve_names([query], return_full_contact_data=True, search_scope=SEARCH_SCOPE_ACTIVE_DIRECTORY):
            if isinstance(entry, ErrorNameResolutionNoResults):
                continue
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, tuple) and len(entry) == 2:
                results.append(entry)
        return results

    def find_people(self, query, source="auto", limit=20, folder_id=None):
        if not isinstance(query, str) or not query.strip():
            raise ToolOperationError("INVALID_QUERY", "联系人搜索关键词不能为空。")
        if source not in ("auto", "gal", "contacts"):
            raise ToolOperationError("INVALID_SOURCE", "来源必须是 auto、gal 或 contacts。")
        _pagination(0, limit)
        if folder_id is not None and source == "gal":
            raise ToolOperationError("INVALID_SOURCE", "指定联系人文件夹时不能使用 GAL 来源。")
        sources = ("contacts",) if folder_id is not None else (("gal", "contacts") if source == "auto" else (source,))
        items, coverage, failures = [], {}, {}
        for name in sources:
            try:
                if name == "gal":
                    found = [_directory_row(mailbox, contact) for mailbox, contact in self._resolve_directory(query.strip())[:limit]]
                else:
                    folder = self._contact_folder(folder_id)
                    found = []
                    for item in folder.filter(display_name__icontains=query.strip()).order_by("display_name")[:limit]:
                        if isinstance(item, Exception):
                            raise item
                        if type(item) is Contact:
                            found.append(_contact_row(item))
                items.extend(found)
                coverage[name] = "success"
            except Exception as exc:
                details = error_details(exc)
                coverage[name] = "failed"
                failures[name] = details
        status = "success" if not failures else ("failed" if len(failures) == len(sources) else "partial")
        result = {"items": items, "sources": coverage, "source_errors": failures, "status": status, "ok": status == "success"}
        if failures:
            result["error_code"] = "PEOPLE_SEARCH_FAILED" if status == "failed" else "PEOPLE_SEARCH_PARTIAL"
            result["message"] = "联系人搜索全部来源失败。" if status == "failed" else "部分联系人来源不可用，其余来源结果已返回。"
        return result

    def get_contact(self, contact_id, folder_id=None):
        if not isinstance(contact_id, str) or not contact_id.strip():
            raise ToolOperationError("INVALID_CONTACT_ID", "请提供联系人 ID 或准确的 GAL 邮箱。")
        if folder_id is None and "@" in contact_id:
            for mailbox, contact in self._resolve_directory(contact_id.strip()):
                if str(getattr(mailbox, "email_address", "") or "").casefold() == contact_id.strip().casefold():
                    return _directory_row(mailbox, contact)
            raise ToolOperationError("CONTACT_NOT_FOUND", "未在 GAL 找到该邮箱。")
        folder = self._contact_folder(folder_id)
        return _contact_row(self._fetch_contact(contact_id, folder))

    def _save_contact(self, item, values, *, creating):
        if not creating and (not item.id or not item.changekey):
            raise ToolOperationError("CONTACT_IDENTIFIER_INCOMPLETE", "读取结果缺少联系人 ID 或 ChangeKey，未提交修改。")
        try:
            for name, value in values.items():
                Contact.get_field_by_fieldname(name).clean(value, version=self.account.version)
        except (TypeError, ValueError) as exc:
            raise ToolOperationError("INVALID_CONTACT", "联系人资料不符合 Exchange 字段约束，未提交修改。") from exc
        try:
            if creating:
                item.save()
            elif values:
                item.save(update_fields=list(values), conflict_resolution=NEVER_OVERWRITE)
        except Exception as exc:
            details = error_details(exc, submitted=True)
            raise ToolOperationError(
                "CONTACT_CREATE_FAILED" if creating else "CONTACT_UPDATE_FAILED",
                "联系人保存失败。", status=details["status"],
                results={"id": item.id, "contact_id": item.id, "changekey": item.changekey, "folder_id": item.folder.id} if item.id else None,
            ) from exc
        if not item.id or not item.changekey:
            raise ToolOperationError("CONTACT_SAVE_RESULT_UNKNOWN", "联系人可能已保存，但服务端未返回完整的项目标识。", status="unknown",
                                     results={"id": item.id, "contact_id": item.id, "changekey": item.changekey, "folder_id": item.folder.id} if item.id else None)

    def _apply_photo(self, item, photo, receipt, *, has_written):
        old_photos = [attachment for attachment in (item.attachments or []) if isinstance(attachment, FileAttachment) and attachment.is_contact_photo]
        removed = 0
        step = "remove"
        try:
            for attachment in old_photos:
                # SDK Attachment.detach() 丢弃 RootItemId；直接保留服务端的新 ChangeKey。
                result = DeleteAttachment(account=self.account).get(items=[attachment.attachment_id])
                if result.id != item.id or not result.changekey:
                    raise ToolOperationError("CONTACT_PHOTO_RESULT_UNKNOWN", "删除照片后未返回完整的联系人标识。", status="unknown")
                item.changekey = result.changekey
                item.attachments.remove(attachment)
                attachment.parent_item = None
                attachment.attachment_id = None
                removed += 1
                has_written = True
            if photo is not None:
                step = "add"
                item.attach(photo)
                if not photo.attachment_id or not item.changekey:
                    raise ToolOperationError("CONTACT_PHOTO_RESULT_UNKNOWN", "添加照片后未返回完整的附件标识。", status="unknown")
                has_written = True
        except Exception as exc:
            details = error_details(exc, submitted=True)
            status = "unknown" if details["status"] == "unknown" else ("partial" if has_written else "failed")
            photo_result = {"removed": removed, "added": None if status == "unknown" and step == "add" else False}
            if status == "unknown":
                photo_result["pending_step"] = step
            known = {**receipt, "changekey": item.changekey, "photo_result": photo_result}
            if "updated" in known:
                known["updated"] = has_written
            raise ToolOperationError("CONTACT_PHOTO_FAILED", "联系人照片处理失败，已确定成功的部分已返回。", status=status, results=known) from exc
        receipt.update({"changekey": item.changekey, "photo_result": {"removed": removed, "added": photo is not None}})
        if "updated" in receipt:
            receipt["updated"] = has_written

    def create_contact(self, contact, folder_id=None):
        values = build_contact_values(contact)
        photo = contact_photo(contact["photo"]) if "photo" in contact else None
        folder = self._contact_folder(folder_id)
        item = Contact(account=self.account, folder=folder, **values)
        self._save_contact(item, values, creating=True)
        receipt = {"id": item.id, "contact_id": item.id, "changekey": item.changekey, "folder_id": folder.id, "created": True}
        if "photo" in contact:
            self._apply_photo(item, photo, receipt, has_written=True)
        return receipt

    def update_contact(self, contact_id, contact, folder_id=None):
        # 先验证独立结构与照片，读取当前值后才做递归补丁合并。
        validate_contact(contact)
        photo = contact_photo(contact["photo"]) if isinstance(contact, dict) and "photo" in contact else None
        folder = self._contact_folder(folder_id)
        item = self._fetch_contact(contact_id, folder)
        values = build_contact_values(contact, existing=item)
        for name, value in values.items():
            setattr(item, name, value)
        self._save_contact(item, values, creating=False)
        receipt = {"id": item.id, "contact_id": item.id, "changekey": item.changekey, "folder_id": folder.id, "updated": bool(values)}
        if "photo" in contact:
            self._apply_photo(item, photo, receipt, has_written=bool(values))
        return receipt
