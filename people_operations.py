"""Contacts and organization-directory (GAL) lookups limited to the employee.

GAL and the mailbox Contacts folder are separate data sources with separate
permissions; each is reported independently and one being unavailable must not
fail the other (``partial``, not whole-failure).

GAL lookups go through the EWS ``ResolveNames`` operation scoped to
ActiveDirectory. ``exchangelib.folders.GALContacts`` is deliberately not used:
in exchangelib 5.6 it rejects the ``account`` kwarg and cannot be instantiated
standalone, so it only ever masked the real (working) directory lookup.
"""

import re

from exchangelib import Contact
from exchangelib.errors import ErrorAccessDenied, ErrorFolderNotFound
from exchangelib.folders import Folder
from exchangelib.folders.base import BaseFolder
from exchangelib.indexed_properties import EmailAddress, PhoneNumber
from exchangelib.properties import FieldPath
from exchangelib.services import GetFolder

import team_directories

from tool_support import ToolOperationError, require_confirmation

SEARCH_SCOPE_ACTIVE_DIRECTORY = "ActiveDirectory"

# 只用于 GetFolder 直取目标目录的属性投影。
_FOLDER_FIELDS = ("name", "folder_class", "parent_folder_id", "total_count")


def _primary_email(contact):
    """First non-empty address. ``email_addresses`` is a list of ``EmailAddress``
    entries (label + email), ordered by label, so EmailAddress1 wins."""
    for entry in getattr(contact, "email_addresses", None) or []:
        value = str(getattr(entry, "email", "") or "").strip()
        if value:
            return value
    return None


def _primary_phone(contact):
    """Business phone first, then any labelled number.

    Contact phones are a labelled list (``contacts:PhoneNumber``), the same shape
    ``create_contact`` writes, so what was written is what is read back.
    """
    entries = getattr(contact, "phone_numbers", None) or []
    by_label = {}
    for entry in entries:
        value = str(getattr(entry, "phone_number", "") or "").strip()
        if value:
            by_label[str(getattr(entry, "label", "") or "")] = value
    return by_label.get("BusinessPhone") or next(iter(by_label.values()), None)


def _contact_row(contact, source):
    return {
        "email": _primary_email(contact),
        "source": source,
        "id": getattr(contact, "id", None),
        "changekey": getattr(contact, "changekey", None),
        "display_name": getattr(contact, "display_name", None),
        "job_title": getattr(contact, "job_title", None),
        "department": getattr(contact, "department", None),
        "company_name": getattr(contact, "company_name", None),
        "phone": _primary_phone(contact),
    }


def _directory_row(mailbox, contact):
    """One GAL entry from ResolveNames. Mailbox is always present; the full
    contact entry carries the extras and may be absent."""
    return {
        "source": "gal",
        "id": getattr(contact, "id", None),
        "changekey": getattr(contact, "changekey", None),
        "display_name": getattr(mailbox, "name", None)
        or getattr(contact, "display_name", None),
        "email": getattr(mailbox, "email_address", None),
        "job_title": getattr(contact, "job_title", None),
        "department": getattr(contact, "department", None),
        "company_name": getattr(contact, "company_name", None),
    }


def _with_directory(row, spec):
    """Tag a row with the team directory it came from, so a later read can be
    traced back to the same logical directory."""
    row["directory_id"] = spec.directory_id
    row["directory"] = spec.display_name
    row["target_mailbox"] = spec.mailbox
    return row


def _denied(exc):
    name = exc.__class__.__name__
    return name.startswith("Error") and ("Denied" in name or "Access" in name)


def _get_folder_by_id(account, folder_id):
    """GetFolder by ID.

    Deliberately not ``folder.children`` / ``account.root.walk()``: on a delegated
    mailbox exchangelib's folder-tree cache raises while building the root map
    (``KeyError`` in ``roots.py``), and traversal would also depend on folders the
    delegate may not be allowed to enumerate. Addressing the folder directly is
    both exact and independent of the rest of the mailbox.
    """
    template = Folder(root=account.root, id=folder_id)
    additional = {
        FieldPath(field=BaseFolder.get_field_by_fieldname(name))
        for name in _FOLDER_FIELDS
    }
    folders = list(
        GetFolder(account=account).call(
            folders=[template], additional_fields=additional, shape="IdOnly"
        )
    )
    return folders[0] if folders else None


class PeopleOperations:
    def _resolve_directory(self, query):
        """Resolve a name or email against the GAL (ActiveDirectory scope).

        A fresh per-call client has not negotiated an EWS version yet and
        ResolveNames needs a version hint, so warm it up first. A no-result
        lookup comes back as an ``ErrorNameResolutionNoResults`` object inside
        the result list (exchangelib returns it rather than raising), so only
        genuine ``(Mailbox, Contact)`` tuples are kept.
        """
        account = self.account
        _ = account.version  # force EWS version negotiation for the hint
        entries = account.protocol.resolve_names(
            [query],
            return_full_contact_data=True,
            search_scope=SEARCH_SCOPE_ACTIVE_DIRECTORY,
        )
        return [entry for entry in entries if isinstance(entry, tuple)]

    def _open_directory(self, directory_id, *, write):
        """Resolve the logical id and authorize the CURRENT employee.

        Authorization is an explicit administrator list, never inferred from the
        service account's write rights. ``write=False`` is checked before any
        read, ``write=True`` before any create.
        """
        spec = team_directories.get_directory(directory_id)
        team_directories.authorize(
            spec, getattr(self, "requester_lanid", None), write=write
        )
        return spec

    def _bind_directory(self, spec):
        """Dedicated Account + verified folder for the target mailbox.

        The requester's identity is untouched: ``self.account`` and
        ``self.config.email`` keep pointing at the OA-resolved employee.
        """
        account = self._create_account(spec.mailbox)
        try:
            folder = _get_folder_by_id(account, spec.folder_id)
        except (ErrorFolderNotFound, ErrorAccessDenied) as exc:
            raise ToolOperationError(
                "DIRECTORY_UNAVAILABLE",
                "团队共享目录当前不可访问（不存在或服务账号无权访问）。",
            ) from exc
        if folder is None:
            raise ToolOperationError(
                "DIRECTORY_UNAVAILABLE", "团队共享目录当前不可访问（不存在）。"
            )
        folder_class = str(getattr(folder, "folder_class", "") or "")
        if folder_class != spec.folder_class or not folder_class.startswith(
            team_directories.CONTACT_FOLDER_CLASS_PREFIX
        ):
            raise ToolOperationError(
                "DIRECTORY_INVALID_TARGET",
                "配置指向的目标不是预期的联系人目录，已拒绝访问。",
            )
        if spec.parent_folder_id and getattr(
            getattr(folder, "parent_folder_id", None), "id", None
        ) != spec.parent_folder_id:
            raise ToolOperationError(
                "DIRECTORY_INVALID_TARGET", "配置指向的目录层级已变化，已拒绝访问。"
            )
        return account, folder

    def _prepare_directory(self, directory_id, *, write):
        spec = self._open_directory(directory_id, write=write)
        account, folder = self._bind_directory(spec)
        return spec, account, folder

    def find_people(self, query, source="auto", limit=20, directory_id=None):
        query = (query or "").strip()
        if not query:
            raise ToolOperationError("INVALID_QUERY", "联系人搜索关键词不能为空。")
        if source not in ("auto", "gal", "contacts"):
            raise ToolOperationError("INVALID_SOURCE", "来源必须是 auto、gal 或 contacts。")
        if directory_id is not None:
            # 共享目录是第三种明确来源，不与 GAL/个人 Contacts 混用；组合非法就拒绝，
            # 不做“猜测意图”的降级。
            if source != "contacts":
                raise ToolOperationError(
                    "INVALID_SOURCE",
                    "团队共享目录仅支持 source=contacts；不接受与 gal/auto 的组合。",
                )
            return self._find_in_directory(query, limit, directory_id)
        sources = ("gal", "contacts") if source == "auto" else (source,)
        results = []
        coverage = {}
        for name in sources:
            try:
                if name == "contacts":
                    rows = (
                        self._tool_folder("contacts")
                        .filter(display_name__icontains=query)
                        .order_by("display_name")[:limit]
                    )
                    coverage[name] = "ok"
                    results.extend(_contact_row(contact, name) for contact in rows)
                else:
                    entries = self._resolve_directory(query)
                    coverage[name] = "ok"
                    results.extend(
                        _directory_row(mailbox, contact) for mailbox, contact in entries[:limit]
                    )
            except Exception as exc:  # noqa: BLE001 - report per-source coverage
                coverage[name] = "denied" if _denied(exc) else "unreachable"
        payload = {"items": results, "sources": coverage}
        if any(status != "ok" for status in coverage.values()):
            payload["partial"] = True
        return payload

    def _find_in_directory(self, query, limit, directory_id):
        """Search ONLY the selected directory: no recursion into sub-folders and
        no sweeping other mailboxes."""
        spec, account, folder = self._prepare_directory(directory_id, write=False)
        try:
            rows = folder.filter(display_name__icontains=query).order_by("display_name")[:limit]
            items = [_with_directory(_contact_row(c, "contacts"), spec) for c in rows]
        except Exception as exc:  # noqa: BLE001 - 目录读取失败如实上报，不伪装成空结果
            raise ToolOperationError(
                "DIRECTORY_UNAVAILABLE", "读取团队共享目录失败。"
            ) from exc
        return {
            "items": items,
            "sources": {"contacts": "ok"},
            "directory": spec.descriptor(),
        }

    def get_contact(self, contact_id, directory_id=None):
        if not contact_id or not str(contact_id).strip():
            raise ToolOperationError("INVALID_CONTACT_ID", "请提供联系人 ID 或邮箱。")
        value = str(contact_id).strip()
        if directory_id is not None:
            # 共享目录分支：先验证员工目录权限，再按目录内 ID 读取并核对归属。
            # 这里绝不进入 GAL 分支 — 即使传的是邮箱地址，也只当目录内 ID 处理。
            return self._get_from_directory(value, directory_id)
        if "@" in value:
            # Exact GAL directory address resolution. A directory address is a
            # contact lookup, never a switch of the target mailbox. GAL is a
            # separate source with its own permission; if unavailable this is a
            # clean directory-limited error, not a crash.
            try:
                entries = self._resolve_directory(value)
            except Exception as exc:  # noqa: BLE001 - GAL unreachable/denied is a clean limit
                raise ToolOperationError(
                    "GAL_UNAVAILABLE",
                    "组织目录(GAL)不可用或受现场策略限制，无法在此解析目录地址。",
                ) from exc
            row = next(
                (
                    _directory_row(mailbox, contact)
                    for mailbox, contact in entries
                    if str(getattr(mailbox, "email_address", "") or "").lower()
                    == value.lower()
                ),
                None,
            )
            if row is None:
                raise ToolOperationError("CONTACT_NOT_FOUND", "未在组织目录中找到该邮箱。")
            return row
        contact = self._tool_item(value, folder_names=("contacts",))
        if type(contact) is not Contact:
            raise ToolOperationError("INVALID_CONTACT", "指定项目不是当前员工的联系人。")
        return _contact_row(contact, "contacts")

    def _get_from_directory(self, contact_id, directory_id):
        spec, account, folder = self._prepare_directory(directory_id, write=False)
        try:
            item = next(iter(account.fetch(ids=[(contact_id, None)], folder=folder)), None)
        except Exception as exc:  # noqa: BLE001 - 越权/失效 ID 一律按“找不到”处理
            raise ToolOperationError(
                "CONTACT_NOT_FOUND", "未在该团队共享目录中找到指定联系人。"
            ) from exc
        if item is None or isinstance(item, Exception):
            raise ToolOperationError(
                "CONTACT_NOT_FOUND", "未在该团队共享目录中找到指定联系人。"
            )
        # EWS GetItem is not folder-scoped, so an ID from another folder can come
        # back here. Both the item class and the parent folder are checked before
        # any detail is returned; a mismatch reports no field at all.
        if type(item) is not Contact:
            raise ToolOperationError(
                "INVALID_CONTACT", "指定项目不是该团队共享目录中的联系人。"
            )
        if getattr(getattr(item, "parent_folder_id", None), "id", None) != folder.id:
            raise ToolOperationError(
                "ITEM_OUT_OF_SCOPE", "指定项目不属于该团队共享目录。"
            )
        item.folder = folder
        return _with_directory(_contact_row(item, "contacts"), spec)

    def create_contact(
        self,
        display_name,
        email,
        phone=None,
        company_name=None,
        job_title=None,
        confirm=False,
        confirmation_id=None,
        directory_id=None,
        directory_target=None,
    ):
        """Create one contact in the employee's own Contacts folder, or in a
        configured team shared directory when ``directory_id`` is given.

        Writes only the mailbox Contacts item: it never touches the GAL and
        sends no mail. The target mailbox still comes only from the OA-resolved
        employee (or from the administrator-verified directory entry); the
        contact's own address never selects a mailbox.

        ``directory_target`` is internal: the dispatcher passes back the target it
        re-verified against the preview, so execution uses exactly the checked
        target rather than resolving a second time.
        """
        display_name = (display_name or "").strip()
        if not display_name:
            raise ToolOperationError("INVALID_CONTACT", "联系人姓名不能为空。")
        email = (email or "").strip()
        if not re.fullmatch(r"^[^\s@<>;,]+@[^\s@<>;,]+$", email):
            raise ToolOperationError("INVALID_CONTACT_EMAIL", "联系人邮箱地址格式无效。")
        details = {
            "display_name": display_name,
            "email": email,
            "phone": phone,
            "company_name": company_name,
            "job_title": job_title,
        }
        spec = None
        prepared = None
        if directory_id is not None:
            if directory_target is not None:
                # Already authorized and compared against the persisted preview.
                spec = directory_target
                prepared = self._bind_directory(spec)
            else:
                # Preview: authorize the create BEFORE showing a preview, so a
                # read-only employee is refused up front and nothing is written.
                spec, _account, folder = self._prepare_directory(directory_id, write=True)
                prepared = (_account, folder)
            details["directory_id"] = spec.directory_id
            details["target_mailbox"] = spec.mailbox
            details["target_folder"] = spec.folder_id
        target = spec.identity() if spec is not None else None
        preview = require_confirmation(
            mailbox=self.config.email,
            action="create_contact",
            items=[details],
            details={"target": target} if target is not None else None,
            confirm=confirm,
            confirmation_id=confirmation_id,
        )
        if preview is not None:
            return preview
        if prepared is None:
            account, folder = self.account, self._tool_folder("contacts")
        else:
            account, folder = prepared
        item = Contact(
            account=account,
            folder=folder,
            display_name=display_name,
            email_addresses=[EmailAddress(label="EmailAddress1", email=email)],
            phone_numbers=(
                [PhoneNumber(label="BusinessPhone", phone_number=phone)]
                if phone is not None
                else []
            ),
            company_name=company_name,
            job_title=job_title,
        )
        try:
            item.save()
        except Exception as exc:
            raise ToolOperationError(
                "CONTACT_CREATE_FAILED", "新建联系人失败，请检查 Contacts 创建权限。"
            ) from exc
        if not (isinstance(item.id, str) and item.id) or not (
            isinstance(item.changekey, str) and item.changekey
        ):
            raise ToolOperationError(
                "CONTACT_RECEIPT_INCOMPLETE",
                "联系人可能已写入但回执不完整；请勿重发，仅查询原操作号。",
            )
        receipt = {
            "id": item.id,
            "contact_id": item.id,
            "changekey": item.changekey,
            "created": True,
            "folder": "contacts",
            "display_name": display_name,
            "email": email,
            "phone": _primary_phone(item) or phone,
        }
        if spec is not None:
            # 回执明确记录实际目标目录，后续读取能带回同一目录标识。
            receipt["directory_id"] = spec.directory_id
            receipt["directory"] = spec.display_name
            receipt["target_mailbox"] = spec.mailbox
            receipt["target_folder"] = spec.folder_id
        return receipt
