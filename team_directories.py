"""管理员核实的团队联系人目录注册表（EWS_MCP_TEAM_DIRECTORIES JSON）。

调用方只能引用预配置的 directory_id，不能指定任意邮箱或 FolderId。
读与建共用 members 明确名单；不从服务账号权限推导员工权限。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Dict, FrozenSet

from tool_support import ToolOperationError

ENV_VAR = "EWS_MCP_TEAM_DIRECTORIES"

# IPF.Contact 及其子类均为联系人文件夹。
CONTACT_FOLDER_CLASS_PREFIX = "IPF.Contact"


def _error(code: str, message: str) -> ToolOperationError:
    return ToolOperationError(code, message)


def _normalize_principal(value) -> str:
    if not isinstance(value, str):
        raise ValueError("principal must be a string")
    normalized = value.strip()
    if "\\" in normalized:
        normalized = normalized.rsplit("\\", 1)[-1].strip()
    if not normalized:
        raise ValueError("principal must not be empty")
    return normalized.lower()


def _principal_set(raw, field, directory_id):
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise _error(
            "DIRECTORY_CONFIG_INVALID",
            f"团队目录 {directory_id} 的 {field} 必须是员工名单数组。",
        )
    try:
        return frozenset(_normalize_principal(item) for item in raw)
    except ValueError as exc:
        raise _error(
            "DIRECTORY_CONFIG_INVALID",
            f"团队目录 {directory_id} 的 {field} 含无效员工标识。",
        ) from exc


def _required_text(spec, field, directory_id):
    value = spec.get(field)
    if not isinstance(value, str) or not value.strip():
        raise _error(
            "DIRECTORY_CONFIG_INVALID",
            f"团队目录 {directory_id} 缺少有效的 {field}。",
        )
    return value.strip()


@dataclass(frozen=True)
class TeamDirectory:
    """已核实的团队目录配置。"""

    directory_id: str
    display_name: str
    mailbox: str
    folder_id: str
    parent_folder_id: str
    folder_class: str
    members: FrozenSet[str]
    version: str

    def identity(self) -> dict:
        """确认时比对目标快照，防止配置改指后旧确认写入新目录。

        仅展示用的 display_name 不进入快照。
        """
        return {
            "directory_id": self.directory_id,
            "mailbox": self.mailbox,
            "folder_id": self.folder_id,
            "folder_class": self.folder_class,
            "target_version": self.version,
        }

    def descriptor(self) -> dict:
        return {
            "directory_id": self.directory_id,
            "display_name": self.display_name,
            "mailbox": self.mailbox,
            "folder_id": self.folder_id,
        }

    def can_read(self, principal) -> bool:
        return _normalize_principal(principal) in self.members

    def can_create(self, principal) -> bool:
        return _normalize_principal(principal) in self.members


def _parse_entry(directory_id, spec) -> TeamDirectory:
    if not isinstance(directory_id, str) or not directory_id.strip():
        raise _error("DIRECTORY_CONFIG_INVALID", "团队目录标识必须是非空字符串。")
    if not isinstance(spec, dict):
        raise _error(
            "DIRECTORY_CONFIG_INVALID", f"团队目录 {directory_id} 的配置必须是对象。"
        )

    mailbox = _required_text(spec, "mailbox", directory_id).lower()
    if "@" not in mailbox or any(c.isspace() for c in mailbox):
        raise _error(
            "DIRECTORY_CONFIG_INVALID", f"团队目录 {directory_id} 的 mailbox 不是有效邮箱。"
        )
    folder_id = _required_text(spec, "folder_id", directory_id)
    parent_folder_id = (spec.get("parent_folder_id") or "").strip()
    folder_class = (spec.get("folder_class") or CONTACT_FOLDER_CLASS_PREFIX).strip()
    if not folder_class.startswith(CONTACT_FOLDER_CLASS_PREFIX):
        raise _error(
            "DIRECTORY_CONFIG_INVALID",
            f"团队目录 {directory_id} 的 folder_class 不是联系人类型。",
        )
    display_name = (spec.get("display_name") or "").strip() or directory_id
    members = _principal_set(spec.get("members"), "members", directory_id)
    if not members:
        # 空名单表示配置未完成，拒绝访问。
        raise _error(
            "DIRECTORY_CONFIG_INVALID",
            f"团队目录 {directory_id} 未配置任何获授权员工（members）。",
        )

    target = json.dumps(
        [mailbox, folder_id, parent_folder_id, folder_class],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    version = hashlib.sha256(target).hexdigest()[:12]

    return TeamDirectory(
        directory_id=directory_id.strip(),
        display_name=display_name,
        mailbox=mailbox,
        folder_id=folder_id,
        parent_folder_id=parent_folder_id,
        folder_class=folder_class,
        members=members,
        version=version,
    )


def _load() -> Dict[str, TeamDirectory]:
    """每次调用重新读取配置，使权限与目标变更立即生效。"""
    raw = os.getenv(ENV_VAR, "").strip()
    if not raw:
        raise _error(
            "INVALID_DIRECTORY",
            "本服务未配置任何团队共享联系人目录。",
        )
    try:
        parsed = json.loads(raw)
    except ValueError as exc:
        raise _error(
            "DIRECTORY_CONFIG_INVALID", "团队目录配置不是有效 JSON。"
        ) from exc
    if not isinstance(parsed, dict):
        raise _error(
            "DIRECTORY_CONFIG_INVALID", "团队目录配置的顶层必须是 directory_id -> 配置 的对象。"
        )
    return {key: _parse_entry(key, value) for key, value in parsed.items()}


def get_directory(directory_id) -> TeamDirectory:
    """未知标识不回退默认目录。"""
    identifier = (directory_id or "").strip() if isinstance(directory_id, str) else ""
    if not identifier:
        raise _error("INVALID_DIRECTORY", "directory_id 不能为空。")
    directories = _load()
    spec = directories.get(identifier)
    if spec is None:
        raise _error(
            "INVALID_DIRECTORY",
            "未知的团队目录标识；请使用管理员预配置的 directory_id。",
        )
    return spec


def authorize(spec: TeamDirectory, principal, *, write: bool) -> None:
    """在读写前核对当前员工的目录权限。"""
    try:
        allowed = spec.can_create(principal) if write else spec.can_read(principal)
    except ValueError as exc:
        raise _error("DIRECTORY_NOT_AUTHORIZED", "无法确认当前员工的目录访问权限。") from exc
    if not allowed:
        raise _error(
            "DIRECTORY_NOT_AUTHORIZED",
            "当前员工未获授权访问该团队共享联系人目录。",
        )
