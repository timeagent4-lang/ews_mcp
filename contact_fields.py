"""标准 Contact 资料的 schema、递归合并和 SDK 字段转换。"""
import base64
import binascii
from copy import deepcopy
from datetime import date

from bs4 import BeautifulSoup
from exchangelib import Contact, FileAttachment, HTMLBody
from exchangelib.indexed_properties import EmailAddress, ImAddress, PhoneNumber, PhysicalAddress
from jsonschema import Draft202012Validator

from tool_support import ToolOperationError


TEXT_FIELDS = (
    "display_name", "given_name", "middle_name", "surname", "initials", "nickname", "generation",
    "file_as", "company_name", "department", "job_title", "office", "manager", "assistant_name",
    "profession", "business_homepage", "mileage", "spouse_name",
)
LIST_FIELDS = ("companies", "children", "categories")
DATE_FIELDS = ("birthday", "wedding_anniversary")
ADDRESS_FIELDS = ("street", "city", "state", "country", "zipcode")
FILE_AS_CHOICES = sorted(c.value for c in Contact.get_field_by_fieldname("file_as_mapping").choices)
POSTAL_CHOICES = ("Business", "Home", "Other", "None")
INDEXED_FIELDS = {
    "email_addresses": (EmailAddress, "email"),
    "phone_numbers": (PhoneNumber, "phone_number"),
    "im_addresses": (ImAddress, "im_address"),
}


def _text_schema(max_length=None):
    schema = {"type": ["string", "null"]}
    if max_length is not None:
        schema["maxLength"] = max_length
    return schema


PHOTO_SCHEMA = {
    "type": ["object", "null"], "additionalProperties": False,
    "required": ["filename", "content_type", "content_base64"],
    "properties": {
        "filename": {"type": "string", "minLength": 1},
        "content_type": {"type": "string", "pattern": "^image/[A-Za-z0-9.+-]+$"},
        "content_base64": {"type": "string", "minLength": 1},
    },
    "description": "照片文件；null 删除现有照片，保留其他附件。",
}
_properties = {
    name: _text_schema(getattr(Contact.get_field_by_fieldname(name), "max_length", None))
    for name in TEXT_FIELDS
}
_properties["display_name"].update({"type": "string", "minLength": 1, "pattern": r"\S"})
_properties.update({
    name: {"type": ["array", "null"], "items": {"type": "string", **({"maxLength": 255} if name == "categories" else {})}}
    for name in LIST_FIELDS
})
_properties.update({name: {"type": ["string", "null"], "pattern": r"^(?:\d{4}-\d{2}-\d{2})?$"} for name in DATE_FIELDS})
for _name, (_model, _value_field) in INDEXED_FIELDS.items():
    _value_schema = _text_schema()
    if _name == "email_addresses":
        _value_schema["pattern"] = r"^$|^[^\s@<>;,]+@[^\s@<>;,]+$"
    _properties[_name] = {
        "type": ["object", "null"], "additionalProperties": False,
        "properties": {label: deepcopy(_value_schema) for label in _model.LABEL_CHOICES},
    }
_properties["physical_addresses"] = {
    "type": ["object", "null"], "additionalProperties": False,
    "properties": {
        label: {"type": ["object", "null"], "additionalProperties": False,
                "properties": {name: _text_schema() for name in ADDRESS_FIELDS}}
        for label in PhysicalAddress.LABEL_CHOICES
    },
}
_properties.update({
    "file_as_mapping": {"type": ["string", "null"], "enum": FILE_AS_CHOICES + ["", None]},
    "postal_address_index": {"type": ["string", "null"], "enum": list(POSTAL_CHOICES) + ["", None]},
    "notes": _text_schema(), "photo": PHOTO_SCHEMA,
})
CONTACT_SCHEMA = {
    "type": "object", "properties": _properties, "additionalProperties": False,
    "description": "完整标准联系人资料。新增未传为空；编辑未传保持、明确空值清除，规则递归应用于索引槽位与地址子字段。",
}
_VALIDATOR = Draft202012Validator(CONTACT_SCHEMA)
_PHOTO_VALIDATOR = Draft202012Validator(PHOTO_SCHEMA)


def _empty(value):
    return value is None or value == ""


def contact_photo(value):
    """验证并构造离线照片附件，提交之前完成 base64 检查。"""
    if value is None:
        return None
    try:
        if not _PHOTO_VALIDATOR.is_valid(value):
            raise ValueError("photo fields")
        if not value["filename"].strip():
            raise ValueError("filename")
        content = base64.b64decode(value["content_base64"], validate=True)
        if not content:
            raise ValueError("empty photo")
        return FileAttachment(name=value["filename"], content_type=value["content_type"], content=content, is_contact_photo=True)
    except (ValueError, TypeError, binascii.Error, KeyError) as exc:
        raise ToolOperationError("INVALID_CONTACT_PHOTO", "照片须提供文件名、image 媒体类型和有效的非空 base64 内容。") from exc


def _merge_indexed(name, incoming, existing):
    model, value_field = INDEXED_FIELDS[name]
    values = {entry.label: getattr(entry, value_field) for entry in (getattr(existing, name, None) or [])} if existing else {}
    if incoming is None or incoming == {}:
        return []
    for label, value in incoming.items():
        if _empty(value):
            values.pop(label, None)
        else:
            values[label] = value
    return [model(label=label, **{value_field: values[label]}) for label in model.LABEL_CHOICES if label in values]


def _merge_addresses(incoming, existing):
    values = {
        entry.label: {name: getattr(entry, name) for name in ADDRESS_FIELDS}
        for entry in (getattr(existing, "physical_addresses", None) or [])
    } if existing else {}
    if incoming is None or incoming == {}:
        return []
    for label, address in incoming.items():
        if address is None or address == {}:
            values.pop(label, None)
            continue
        current = values.setdefault(label, {name: None for name in ADDRESS_FIELDS})
        current.update({name: None if _empty(value) else value for name, value in address.items()})
        if all(_empty(value) for value in current.values()):
            values.pop(label, None)
    return [PhysicalAddress(label=label, **values[label]) for label in PhysicalAddress.LABEL_CHOICES if label in values]


def validate_contact(contact, *, creating=False):
    """独立参数校验，不需要当前联系人或 Exchange 连接。"""
    error = next(_VALIDATOR.iter_errors(contact), None)
    if error is not None:
        # 不回显错误实例，它可能含有完整联系人资料或照片内容。
        raise ToolOperationError("INVALID_CONTACT", "联系人字段、类型、索引标签或枚举值无效。")
    if creating and "display_name" not in contact:
        raise ToolOperationError("INVALID_CONTACT", "新建联系人须提供显示名称。")
    if "display_name" in contact and (contact["display_name"] is None or not contact["display_name"].strip()):
        raise ToolOperationError("INVALID_CONTACT", "联系人显示名称不能为空。")
    if "photo" in contact:
        contact_photo(contact["photo"])
    for name in DATE_FIELDS:
        if name in contact and not _empty(contact[name]):
            try:
                date.fromisoformat(contact[name])
            except ValueError as exc:
                raise ToolOperationError("INVALID_CONTACT_DATE", "联系人日期必须是有效的 YYYY-MM-DD 日期。") from exc


def build_contact_values(contact, existing=None):
    """返回 SDK 资料字段；编辑仅返回明确传入字段，照片由业务层处理。"""
    validate_contact(contact, creating=existing is None)
    values = {}
    selected = set(contact) if existing is not None else set(CONTACT_SCHEMA["properties"]) - {"photo"}
    for name in CONTACT_SCHEMA["properties"]:
        if name not in selected or name == "photo":
            continue
        incoming = contact.get(name)
        if name in INDEXED_FIELDS:
            values[name] = _merge_indexed(name, incoming, existing)
        elif name == "physical_addresses":
            values[name] = _merge_addresses(incoming, existing)
        elif name in LIST_FIELDS:
            values[name] = list(incoming or [])
        elif name in DATE_FIELDS:
            values[name] = None if _empty(incoming) else date.fromisoformat(incoming)
        elif name == "postal_address_index":
            values[name] = "None" if _empty(incoming) else incoming
        elif name == "notes":
            values["body"] = None if _empty(incoming) else incoming
        else:
            values[name] = None if _empty(incoming) else incoming
    return values


def serialize_contact(item):
    """完整联系人详情；照片只报告附件元数据，不隐式下载内容。"""
    result = {name: getattr(item, name, None) for name in TEXT_FIELDS}
    result.update({name: list(getattr(item, name, None) or []) for name in LIST_FIELDS})
    result.update({name: (getattr(item, name, None).isoformat() if getattr(item, name, None) is not None else None) for name in DATE_FIELDS})
    result.update({name: {entry.label: getattr(entry, value_field) for entry in (getattr(item, name, None) or [])} for name, (_model, value_field) in INDEXED_FIELDS.items()})
    result["physical_addresses"] = {
        entry.label: {name: getattr(entry, name, None) for name in ADDRESS_FIELDS}
        for entry in (getattr(item, "physical_addresses", None) or [])
    }
    result.update({
        "id": getattr(item, "id", None), "contact_id": getattr(item, "id", None),
        "changekey": getattr(item, "changekey", None), "source": "contacts",
        "file_as_mapping": getattr(item, "file_as_mapping", None),
        "postal_address_index": getattr(item, "postal_address_index", None) or "None",
        "notes": (BeautifulSoup(str(item.body), "html.parser").get_text("\n", strip=True)
                  if isinstance(getattr(item, "body", None), HTMLBody)
                  else str(item.body) if getattr(item, "body", None) is not None else None),
        "photo": [
            {"attachment_id": attachment.attachment_id.id if attachment.attachment_id else None,
             "filename": attachment.name, "content_type": attachment.content_type, "size": attachment.size}
            for attachment in (getattr(item, "attachments", None) or [])
            if isinstance(attachment, FileAttachment) and attachment.is_contact_photo
        ] or None,
    })
    for name in ("contact_source", "has_picture", "phonetic_full_name", "phonetic_first_name", "phonetic_last_name", "email_alias", "directory_id"):
        result[name] = getattr(item, name, None)
    for name in ("datetime_created", "last_modified_time"):
        value = getattr(item, name, None)
        result[name] = value.isoformat() if value is not None else None
    parent = getattr(item, "parent_folder_id", None)
    result["folder_id"] = parent.id if parent is not None else getattr(getattr(item, "folder", None), "id", None)
    return result
