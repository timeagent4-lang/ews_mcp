"""Employee contacts and GAL lookups with independent per-source permissions.

Use ActiveDirectory ResolveNames; report unavailable sources as partial coverage.
"""

import re

from exchangelib import Body, Contact
from exchangelib.errors import ErrorAccessDenied, ErrorFolderNotFound, ErrorNameResolutionNoResults
from exchangelib.folders import Folder
from exchangelib.folders.base import BaseFolder
from exchangelib.indexed_properties import EmailAddress, PhoneNumber
from exchangelib.properties import FieldPath
from exchangelib.services import GetFolder

import team_directories

from tool_support import ToolOperationError, require_confirmation

SEARCH_SCOPE_ACTIVE_DIRECTORY = "ActiveDirectory"

_FOLDER_FIELDS = ("name", "folder_class", "parent_folder_id", "total_count")


def _primary_email(contact):
    """Use the first non-empty labelled address (EmailAddress1 first)."""
    for entry in getattr(contact, "email_addresses", None) or []:
        value = str(getattr(entry, "email", "") or "").strip()
        if value:
            return value
    return None


def _phone(contact, label="BusinessPhone"):
    entries = getattr(contact, "phone_numbers", None) or []
    by_label = {}
    for entry in entries:
        value = str(getattr(entry, "phone_number", "") or "").strip()
        if value:
            by_label[str(getattr(entry, "label", "") or "")] = value
    return by_label.get(label)


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
        "phone": _phone(contact),
        "mobile_phone": _phone(contact, "MobilePhone"),
        "office": getattr(contact, "office", None),
    }


def _directory_row(mailbox, contact):
    """ResolveNames may omit Contact details; Mailbox is always present."""
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
    row["directory_id"] = spec.directory_id
    row["directory"] = spec.display_name
    row["target_mailbox"] = spec.mailbox
    return row


def _denied(exc):
    name = exc.__class__.__name__
    return name.startswith("Error") and ("Denied" in name or "Access" in name)


def _get_folder_by_id(account, folder_id):
    """Fetch by ID to avoid delegated folder-tree cache and enumeration permissions."""
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
        """Resolve against ActiveDirectory; discard returned no-result exceptions."""
        account = self.account
        _ = account.version  # ResolveNames needs a negotiated version
        entries = account.protocol.resolve_names(
            [query],
            return_full_contact_data=True,
            search_scope=SEARCH_SCOPE_ACTIVE_DIRECTORY,
        )
        resolved = []
        for entry in entries:
            if isinstance(entry, ErrorNameResolutionNoResults):
                continue
            if isinstance(entry, Exception):
                raise entry
            if isinstance(entry, tuple):
                resolved.append(entry)
        return resolved

    def _open_directory(self, directory_id, *, write):
        """Authorize the employee from the administrator list before reads or creates."""
        spec = team_directories.get_directory(directory_id)
        team_directories.authorize(
            spec, getattr(self, "requester_lanid", None), write=write
        )
        return spec

    def _bind_directory(self, spec):
        """Bind the verified target without changing the requester's account or identity."""
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
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ToolOperationError("INVALID_PAGINATION", "limit 必须为 1 到 100。")
        if source not in ("auto", "gal", "contacts"):
            raise ToolOperationError("INVALID_SOURCE", "来源必须是 auto、gal 或 contacts。")
        if directory_id is not None:
            # 共享目录仅接受明确的 contacts 来源。
            if source != "contacts":
                raise ToolOperationError(
                    "INVALID_SOURCE",
                    "团队共享目录仅支持 source=contacts；不接受与 gal/auto 的组合。",
                )
            return self._find_in_directory(query, limit, directory_id)
        sources = ("gal", "contacts") if source == "auto" else (source,)
        results = []
        coverage = {}
        failures = []
        for name in sources:
            try:
                if name == "contacts":
                    rows = (
                        self._tool_folder("contacts")
                        .filter(display_name__icontains=query)
                        .order_by("display_name")[:limit]
                    )
                    source_rows = []
                    for contact in rows:
                        if isinstance(contact, Exception):
                            raise contact
                        source_rows.append(_contact_row(contact, name))
                else:
                    entries = self._resolve_directory(query)
                    source_rows = [
                        _directory_row(mailbox, contact) for mailbox, contact in entries[:limit]
                    ]
                results.extend(source_rows)
                coverage[name] = "ok"
            except Exception as exc:  # noqa: BLE001 - report per-source coverage
                coverage[name] = "denied" if _denied(exc) else "unreachable"
                failures.append(exc)
        if not any(value == "ok" for value in coverage.values()):
            label = "组织通讯录" if source == "gal" else "联系人" if source == "contacts" else "组织通讯录与联系人"
            raise ToolOperationError(
                "GAL_UNAVAILABLE" if source == "gal" else "DIRECTORY_UNAVAILABLE",
                f"{label}查询失败。",
            ) from failures[-1]
        payload = {"items": results, "sources": coverage}
        if any(status != "ok" for status in coverage.values()):
            payload["partial"] = True
        return payload

    def _find_in_directory(self, query, limit, directory_id):
        """Search the selected directory without traversing other folders."""
        spec, account, folder = self._prepare_directory(directory_id, write=False)
        try:
            rows = folder.filter(display_name__icontains=query).order_by("display_name")[:limit]
            items = []
            for contact in rows:
                if isinstance(contact, Exception):
                    raise contact
                items.append(_with_directory(_contact_row(contact, "contacts"), spec))
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
            # 目录内 ID 必须通过权限和归属检查，不转入 GAL 查询。
            return self._get_from_directory(value, directory_id)
        if "@" in value:
            # A GAL address selects a contact, never a target mailbox.
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
        return {**_contact_row(contact, "contacts"), "notes": str(contact.body) if contact.body is not None else None}

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
        # GetItem is not folder-scoped; verify class and parent before returning details.
        if type(item) is not Contact:
            raise ToolOperationError(
                "INVALID_CONTACT", "指定项目不是该团队共享目录中的联系人。"
            )
        if getattr(getattr(item, "parent_folder_id", None), "id", None) != folder.id:
            raise ToolOperationError(
                "ITEM_OUT_OF_SCOPE", "指定项目不属于该团队共享目录。"
            )
        item.folder = folder
        return _with_directory({**_contact_row(item, "contacts"), "notes": str(item.body) if item.body is not None else None}, spec)

    def create_contact(
        self,
        display_name,
        email=None,
        phone=None,
        company_name=None,
        job_title=None,
        department=None,
        mobile_phone=None,
        office=None,
        notes=None,
        confirm=False,
        confirmation_id=None,
        directory_id=None,
        directory_target=None,
    ):
        """Create in employee Contacts or an administrator-verified team directory.

        Contact email never selects the mailbox. directory_target is the authorized
        target checked against the persisted preview.
        """
        display_name = (display_name or "").strip()
        if not display_name:
            raise ToolOperationError("INVALID_CONTACT", "联系人姓名不能为空。")
        if email is not None:
            email = email.strip()
            if not re.fullmatch(r"^[^\s@<>;,]+@[^\s@<>;,]+$", email):
                raise ToolOperationError("INVALID_CONTACT_EMAIL", "联系人邮箱地址格式无效。")
        details = {
            "display_name": display_name,
            "email": email,
            "phone": phone,
            "company_name": company_name,
            "job_title": job_title,
            "department": department,
            "mobile_phone": mobile_phone,
            "office": office,
            "notes": notes,
        }
        spec = None
        prepared = None
        if directory_id is not None:
            if directory_target is not None:
                # Authorized and checked against the persisted preview.
                spec = directory_target
                prepared = self._bind_directory(spec)
            else:
                # Authorize writes before exposing the preview.
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
            email_addresses=[EmailAddress(label="EmailAddress1", email=email)] if email is not None else [],
            phone_numbers=[PhoneNumber(label=label, phone_number=value)
                           for label, value in (("BusinessPhone", phone), ("MobilePhone", mobile_phone))
                           if value is not None],
            company_name=company_name,
            job_title=job_title,
            department=department,
            office=office,
            body=Body(notes) if notes is not None else None,
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
            **details,
        }
        if spec is not None:
            receipt["directory_id"] = spec.directory_id
            receipt["directory"] = spec.display_name
            receipt["target_mailbox"] = spec.mailbox
            receipt["target_folder"] = spec.folder_id
        return receipt
