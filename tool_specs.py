"""已确认的 29 工具，顶层凭据参数与严格 JSON Schema。"""
from copy import deepcopy
import json
from contact_fields import CONTACT_SCHEMA
from tool_examples import TOOL_EXAMPLES, TOOL_GUIDANCE

SIDE_READ = "read"
SIDE_WRITE = "write"

# string fields that may carry long free text (larger maxLength).
_BIG_TEXT = frozenset({"body", "message", "internal_reply", "external_reply", "query"})

# fields describing an item reference; get a mailbox-scoped hint.
_ID_FIELDS = frozenset(
    {
        "id",
        "ids",
        "message_id",
        "event_id",
        "draft_id",
        "task_id",
        "contact_id",
        "reply_to",
        "parent",
    }
)

# Folder references are a separate vocabulary: only the well-known aliases that
# list_folders reports are accepted, so a caller cannot construct an arbitrary
# FolderId and reach outside this employee's own mailbox.
_FOLDER_FIELDS = frozenset({"folder", "to_folder"})

# Fine-grained error codes for public-schema validation failures. Only these
# fields carry one; everything else - identity fields, unknown properties and
# unsupported properties - stay INVALID_PARAMS, so a
# schema failure is never reported as an identity or a business rejection.
_FIELD_ERROR_CODES = {}
for _code, _names in (
    ("INVALID_DATETIME", ("start", "end", "start_date", "due_date", "since", "until")),
    ("INVALID_PAGINATION", ("limit", "offset")),
    ("INVALID_FOLDER", tuple(sorted(_FOLDER_FIELDS))),
    ("INVALID_BODY_FORMAT", ("body_format",)),
    ("INVALID_BODY_ACTION", ("body_action",)),
):
    for _name in _names:
        _FIELD_ERROR_CODES[_name] = _code
del _code, _names, _name

_ID_HINT = (
    "Use the exact mailbox-scoped ID returned by this service, or a raw EWS ID. "
    "Cross-mailbox / unscoped short aliases are rejected."
)

_FOLDER_HINT = (
    "Use a well-known alias as returned by list_folders and accepted by this "
    "field's enum. Raw EWS folder IDs are rejected."
)

def _s(name, type_, description, *, required=False, default=None, enum=None,
       items=None, minimum=None, maximum=None, min_length=None, max_length=None, min_items=None, max_items=None):
    field = {
        "name": name,
        "type": type_,
        "description": description,
        "required": required,
    }
    if default is not None:
        field["default"] = default
    if enum is not None:
        field["enum"] = list(enum)
    if items is not None:
        field["items"] = items
    if maximum is not None:
        field["maximum"] = maximum
    if minimum is not None:
        field["minimum"] = minimum
    if min_length is not None:
        field["min_length"] = min_length
    if max_length is not None:
        field["max_length"] = max_length
    if max_items is not None:
        field["max_items"] = max_items
    if min_items is not None:
        field["min_items"] = min_items
    return field


# ---------------- tool vocabulary ----------------

# keyed by tool name; value is a list of business parameter specs.
TOOLS: dict[str, list[dict]] = {
    # --- mail read ---
    "list_folders": [],
    "find_message": [
        _s("query", "string", "Subject contains this text; may combine with structured filters.", default=""),
        _s("aqs", "string", "Exchange AQS; cannot combine with nonempty query or structured filters. Omit when unused.", min_length=1),
        _s("folder", "string", "Mail folder to search.", default="inbox", enum=["inbox", "drafts", "sent"]),
        _s("since", "string", "Inclusive start: received time for inbox, sent time for sent, created time for drafts. Datetimes without a timezone use Asia/Shanghai.", default=None),
        _s("until", "string", "Exclusive end using the same folder time field as since; a date-only value means midnight at the start of that date in Asia/Shanghai.", default=None),
        _s("is_unread", "boolean", "True unread only, False read only; omit for no filter.", default=None),
        _s("has_attachments", "boolean", "True/False attachment filter.", default=None),
        _s("limit", "integer", "Max results.", default=10, minimum=1, maximum=100),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("include_body", "boolean", "Return up to 10000 body characters per message without automatically cleaning HTML; use get_message to read further.", default=True),
    ],
    "get_message": [
        _s("message_id", "string", "Scoped message ID.", required=True),
        _s("clean_body", "boolean", "Strip HTML to plain text.", default=False),
        _s("max_body_chars", "integer", "Body char cap.", default=10000, minimum=1, maximum=100000),
        _s("body_offset", "integer", "Body read offset.", default=0, minimum=0),
    ],
    "get_thread": [
        _s("message_id", "string", "Scoped message ID in the thread.", required=True),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("limit", "integer", "Page size.", default=20, minimum=1, maximum=100),
    ],
    "get_attachment": [
        _s("message_id", "string", "Scoped message ID owning the attachment.", required=True),
        _s("attachment_id", "string", "Scoped attachment ID; omit to list.", default=None),
        _s("mode", "string", "info returns metadata only; auto/text read supported text attachments, otherwise return metadata with a notice. Omit attachment_id to list metadata in any mode.", default="auto", enum=["auto", "info", "text"]),

    ],
    "prepare_attachment_download": [
        _s("message_id", "string", "Scoped message ID owning the attachment.", required=True),
        _s("attachment_id", "string", "Exact attachment ID returned for this message; required.", required=True),
    ],
    "get_mailbox_overview": [
        _s("limit", "integer", "Recent-unread cap.", default=5, minimum=1, maximum=100),
    ],
    # --- draft / mail writes ---
    "create_draft": [
        _s("mode", "string", "Draft mode; reply/reply_all/forward require reply_to; new forbids reply_to.", default="new", enum=["new", "reply", "reply_all", "forward"]),
        _s("subject", "string", "Subject; omitted for reply/forward uses RE:/FW: prefix.", default=""),
        _s("body", "string", "With mode=reply/reply_all/forward this is only your new text: Exchange appends the quoted original natively, so do not paste the original back and do not pass a cleaned plain-text copy of it; an HTML original keeps its formatting. With mode=new it is the complete body of the new message.", default="", max_length=65536),
        _s("body_format", "string", "How to read body. text (default) submits it as plain text. html submits it verbatim as an HTML body so tables and inline style= attributes render - use inline CSS, not a <style> block. html is only accepted with mode=new: a reply/forward body is plain text, and the quoted original keeps its own formatting.", default="text", enum=["text", "html"]),
        _s("to_emails", "string", "To recipients, ; separated.", default=""),
        _s("cc_emails", "string", "Cc recipients, ; separated.", default=""),
        _s("bcc_emails", "string", "Bcc recipients, ; separated.", default=""),

        _s("reply_to", "string", "Original message ID for reply/forward.", default=None),
    ],
    "update_draft": [
        _s("draft_id", "string", "Scoped draft ID.", required=True),
        _s("subject", "string", "New subject; omit to leave unchanged.", default=None),
        _s("body", "string", "Body content, read according to body_action and body_format. Omit it to leave the body untouched, which is what you want when changing only recipients or subject; supplying body_format or body_action without body is rejected rather than ignored. With prepend+html this is an HTML fragment only (e.g. <p>, <table>); a full document containing html/head/body/doctype is refused, not trimmed.", default=None, max_length=65536),
        _s("body_format", "string", "How to read body. With body_action=replace (default): text submits plain text keeping the draft's body type, html submits body verbatim as the complete replacement body so tables and inline style= render. With body_action=prepend: text (default) adds escaped plain text, html adds a raw HTML fragment at the start of the existing body -- fragment only, the draft must already be HTML, and any cid: image it references must match an existing attachment (no image upload).", default=None, enum=["text", "html"]),
        _s("body_action", "string", "replace (default) makes body the whole new body, including any quoted original. prepend treats body as one new piece inserted at the start of the server-read existing body, keeping that body's tables, styles, quoted mail and inline images, so the original is never resubmitted. prepend takes plain text, or an HTML fragment with body_format=html.", default=None, enum=["replace", "prepend"]),
        _s("to_emails", "string", "New to recipients; omit to leave unchanged.", default=None),
        _s("cc_emails", "string", "New cc recipients; omit to leave unchanged.", default=None),
        _s("bcc_emails", "string", "New bcc recipients; omit to leave unchanged.", default=None),
    ],
    "delete_draft": [
        _s("draft_id", "string", "Scoped draft ID.", required=True),
    ],
    "send_draft": [
        _s("draft_id", "string", "Scoped draft ID to send.", required=True),
    ],
    "set_message_flag": [
        _s("message_id", "string", "Scoped message ID to flag.", required=True),
        _s("flag", "string", "Follow-up state to apply.", required=True, enum=["flagged", "complete", "clear"]),
        _s("due_date", "string", "ISO due date: flagged sets it (omission clears it); complete updates it when supplied (omission preserves it). For clear, omit this field; all flag dates are removed.", default=None),
    ],
    "list_flagged_messages": [
        _s("folder", "string", "Mail folder to list.", default="inbox", enum=["inbox", "drafts", "sent"]),
        _s("status", "string", "Flag state; all includes flagged and complete.", default="flagged", enum=["flagged", "complete", "all"]),
        _s("limit", "integer", "Max results.", default=20, minimum=1, maximum=100),
    ],
    "update_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
        _s("set_read", "boolean", "True read, False unread; omit to leave unchanged.", default=None),
        _s("categories_add", "array", "Categories to add.", default=None, items="string", max_items=50),
        _s("categories_remove", "array", "Categories to remove.", default=None, items="string", max_items=50),
    ],
    "move_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
        _s("to_folder", "string", "inbox or sent.", required=True, enum=["inbox", "sent"]),
    ],
    "delete_messages": [
        _s("ids", "array", "Scoped message IDs (1-50).", required=True, items="string", max_items=50),
    ],
    # --- calendar ---
    "list_events": [
        _s("start", "string", "Window start datetime; values without a timezone use Asia/Shanghai.", required=True),
        _s("end", "string", "ISO end.", required=True),
        _s("offset", "integer", "Pagination offset.", default=0, minimum=0),
        _s("limit", "integer", "Page size.", default=20, minimum=1, maximum=100),
    ],
    "get_event": [
        _s("event_id", "string", "Scoped calendar event ID.", required=True),
    ],
    "create_event": [
        _s("subject", "string", "Event subject.", required=True),
        _s("body", "string", "Event body.", default="", max_length=65536),
        _s("start", "string", "Start (ISO or YYYY-MM-DD HH:mm).", required=True),
        _s("end", "string", "End; defaults to start+1h.", default=None),
        _s("location", "string", "Location.", default=None),
        _s("attendees", "array", "Attendee emails.", default=None, items="string", max_items=100),
        _s("send_invitations", "boolean", "Email attendees (requires send switch).", default=False),
    ],
    "update_event": [
        _s("event_id", "string", "Scoped event ID.", required=True),
        _s("subject", "string", "New subject.", default=None),
        _s("body", "string", "New body.", default=None, max_length=65536),
        _s("start", "string", "New start.", default=None),
        _s("end", "string", "New end.", default=None),
        _s("location", "string", "New location; empty string clears.", default=None),
        _s("notify_attendees", "boolean", "Notify attendees (requires send switch).", default=False),
    ],
    "respond_to_event": [
        _s("event_id", "string", "Scoped event ID.", required=True),
        _s("response", "string", "Attendee response.", required=True, enum=["accept", "tentative", "decline"]),
        _s("message", "string", "Response message.", default=None),
    ],
    "cancel_event": [
        _s("event_id", "string", "Organizer-owned scoped event ID.", required=True),
        _s("message", "string", "Cancellation note; omitting it does not suppress meeting cancellation notices.", default=None),
    ],
    # --- contacts ---
    "list_contact_folders": [],
    "list_contacts": [
        _s("folder_id", "string", "联系人文件夹 ID；省略为默认 Contacts。", min_length=1),
        _s("limit", "integer", "分页大小。", default=50, minimum=1, maximum=100),
        _s("offset", "integer", "分页偏移。", default=0, minimum=0),
    ],
    "find_people": [
        _s("query", "string", "联系人/GAL 搜索文本。", required=True, min_length=1),
        _s("source", "string", "省略文件夹时选择 auto/contacts/gal；指定文件夹只查询其中联系人。", default="auto", enum=["auto", "contacts", "gal"]),
        _s("limit", "integer", "每来源最多结果。", default=20, minimum=1, maximum=100),
        _s("folder_id", "string", "已有联系人文件夹 ID。", min_length=1),
    ],
    "get_contact": [
        _s("contact_id", "string", "联系人 ID；省略 folder_id 时也允许精确 GAL 邮箱地址。", required=True, min_length=1),
        _s("folder_id", "string", "联系人所在文件夹 ID；省略使用默认 Contacts。", min_length=1),
    ],
    "create_contact": [
        _s("contact", "object", "联系人资料；新建 display_name 必填，未传的选填字段为空。", required=True),
        _s("folder_id", "string", "创建目标已有联系人文件夹；省略使用默认 Contacts。", min_length=1),
    ],
    "update_contact": [
        _s("contact_id", "string", "要编辑的联系人 ID。", required=True, min_length=1),
        _s("contact", "object", "仅修改传入字段，省略保留，明确空值清空；地址和标签递归合并。", required=True),
        _s("folder_id", "string", "联系人所在文件夹；省略使用默认 Contacts。", min_length=1),
    ],
    "get_server_status": [],
}

DESCRIPTIONS = {'list_folders': '列出目标邮箱邮件文件夹和数量；无法读取的文件夹单独返回覆盖错误。',
 'find_message': '搜索目标邮箱指定邮件文件夹，支持主题、时间、未读、附件或独立 AQS；返回分页及邮件 ID。',
 'get_message': '读取指定邮件详情及分页正文；默认保留 HTML，可选择纯文本。',
 'get_thread': '读取目标 Inbox 和 Sent 中的会话，返回覆盖情况与部分失败；每封正文最多一万字。',
 'get_attachment': '列出附件或读取受支持文本文件；不支持的类型只返回元数据。原文件使用 prepare_attachment_download。',
 'prepare_attachment_download': '生成原附件临时签名下载地址，返回文件名、类型、大小、SHA-256 和失效时间；需要启用下载配置。',
 'get_mailbox_overview': '读取目标收件箱总量、未读量和近期未读邮件。',
 'create_draft': '直接保存新邮件或原生回复/全部回复/转发草稿。新邮件可用 HTML；回复/转发输入仅为新增纯文本，Exchange 保留原文格式。',
 'update_draft': '直接修改指定草稿字段并保留未传字段和附件。正文 replace 替换整段，prepend 前置内容；HTML 前置必须是片段且原草稿为 HTML，cid 图片须已存在。真实 EWS '
                 'ChangeKey 检查冲突。',
 'delete_draft': '将草稿移入 Deleted Items；不执行永久删除。',
 'send_draft': '直接发送目标草稿并保存到目标 Sent；From 为目标邮箱，允许公邮代理发送显示。发送须部署开关开启，结果未知时先核对实际邮箱，不自动重发。',
 'update_messages': '批量直接修改普通邮件已读状态和分类，返回逐项成功/失败/未知结果；拒绝会议通知对象。',
 'move_messages': '批量将普通非草稿邮件移到目标 Inbox 或 Sent，使用返回的新 ID。',
 'delete_messages': '批量将普通邮件移入 Deleted Items；不执行永久删除，返回逐项结果。',
 'set_message_flag': '直接修改普通邮件旗标和日期，拒绝会议通知对象。',
 'list_flagged_messages': '读取指定邮件文件夹中未完成/已完成的旗标邮件。',
 'list_events': '按时间窗口读取排序后的日历实例，分页；has_more 表示还有下一页。',
 'get_event': '读取日历详情、参与者、响应与重复信息。',
 'create_event': '直接创建日历条目；send_invitations 默认关闭，开启需发送开关。',
 'update_event': '直接编辑组织者拥有的单次日程或明确实例，拒绝重复主项；可静默或通知参与者。',
 'respond_to_event': '以参与者身份直接接受/暂定/拒绝邀请并发送响应，需发送开关；拒绝组织者和重复主项。',
 'cancel_event': '组织者直接取消日程；会议发送原生取消通知，需发送开关；无参与者普通预约移至已删除。',
 'list_contact_folders': '发现目标邮箱所有已有普通联系人文件夹，返回 ID、父 ID、名称和路径；不创建文件夹。',
 'list_contacts': '读取指定联系人文件夹，省略为默认 Contacts；不递归扫描子文件夹。',
 'find_people': '独立查询联系人/GAL；提供 folder_id 时只查其中联系人，GAL 只读；保留逐来源错误。',
 'get_contact': '读取指定文件夹的完整联系人资料；默认文件夹时也支持精确 GAL 邮箱查询。',
 'create_contact': '直接在指定已有文件夹创建联系人，支持完整标准字段、多个邮箱/电话/地址、备注和照片；未传选填值为空，不发送邮件。',
 'update_contact': '直接编辑指定文件夹联系人：未传保留、显式空清空、地址和标签递归合并。备注对应正文；照片独立附件步骤可能部分成功。',
 'get_server_status': '返回当前目标邮箱、连接状态和发送开关，不包含凭据。'}

TOOL_NAMES = list(TOOLS)
assert len(TOOL_NAMES) == 29
READ_TOOLS = frozenset(['list_folders', 'find_message', 'get_message', 'get_thread', 'get_attachment', 'prepare_attachment_download', 'get_mailbox_overview', 'list_events', 'get_event', 'find_people', 'get_contact', 'list_contact_folders', 'list_contacts', 'get_server_status', 'list_flagged_messages'])


def _tool_description(name):
    lines = [DESCRIPTIONS[name],
             "参数直接放在本工具 arguments 的顶层，不要包 params。mailbox 始终是要操作的用户邮箱。"
             "省略 password、传 null 或空字符串使用公邮代理，此时忽略 username；非空 password 使用个人凭据，username 省略默认 mailbox。"
             "个人认证失败不会切换公邮。示例中的邮箱、登录名、密码及 <...> ID 占位值须替换为实际值；不需要的可选参数直接省略。"]
    if name not in READ_TOOLS:
        lines.append("直接执行，无二次确认；返回 unknown 时先核对实际邮箱，不重复提交。")
    if name in TOOL_GUIDANCE:
        lines.append(TOOL_GUIDANCE[name])
    for index, (label, business) in enumerate(TOOL_EXAMPLES[name]):
        arguments = {"mailbox": "user@example.com", **business}
        lines.extend([f"调用示例（公邮代理，{label}）：", "```json",
                      json.dumps(arguments, ensure_ascii=False, separators=(",", ":")), "```"])
        if index == 0:
            personal = {"mailbox": "user@example.com", "username": r"DOMAIN\user",
                        "password": "<个人邮箱密码>", **business}
            lines.extend([f"调用示例（个人凭据，{label}）：", "```json",
                          json.dumps(personal, ensure_ascii=False, separators=(",", ":")), "```"])
    return "\n".join(lines)

def _business_schema(fields):
    properties = {}
    required = []
    for field in fields:
        name, type_ = field["name"], field["type"]
        schema = {"type": type_}
        if type_ == "array":
            schema = {"type": "array"}
            if field.get("items"):
                schema["items"] = {"type": field["items"], "maxLength": 4096}
            if field.get("max_items"):
                schema["maxItems"] = field["max_items"]
            if "min_items" in field:
                schema["minItems"] = field["min_items"]
            if name == "ids":
                schema.update(minItems=1, uniqueItems=True)
                schema["items"]["minLength"] = 1
        elif type_ == "string":
            if field.get("enum"):
                schema["enum"] = field["enum"]
            else:
                is_big = name in _BIG_TEXT
                schema["maxLength"] = 65536 if is_big else 4096
            if name in _ID_FIELDS:
                schema["minLength"] = 1
            for key, keyword in (("min_length", "minLength"), ("max_length", "maxLength")):
                if key in field:
                    schema[keyword] = field[key]
        elif type_ == "integer":
            for key in ("minimum", "maximum"):
                if key in field:
                    schema[key] = field[key]
        # 原生 CharField 限长，在建立连接前拒绝，避免 save 本地清理误报提交未知。
        if name == "subject":
            schema["maxLength"] = 255
        if name in _FIELD_ERROR_CODES:
            # public-schema failures on this field report this code instead of
            # the blanket INVALID_PARAMS (see mcp_server._validate_arguments).
            schema["x-error-code"] = _FIELD_ERROR_CODES[name]
        desc = field["description"]
        if name in _ID_FIELDS:
            desc = desc + " " + _ID_HINT
        elif name in _FOLDER_FIELDS:
            desc = desc + " " + _FOLDER_HINT
        schema["description"] = desc
        if "default" in field and field["default"] is not None:
            schema["default"] = field["default"]
        properties[name] = schema
        if field.get("required"):
            required.append(name)
    schema = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema, required


def public_tools():
    result = []
    for name in TOOL_NAMES:
        schema, required = _business_schema(TOOLS[name])
        schema["properties"].update({
            "mailbox": {"type": "string", "minLength": 3, "maxLength": 320, "pattern": r"^[^\s@]+@[^\s@]+$", "description": "目标用户邮箱。"},
            "username": {"type": ["string", "null"], "maxLength": 256, "description": "个人登录名，省略默认邮箱；公邮模式忽略。"},
            "password": {"type": ["string", "null"], "maxLength": 4096, "description": "个人密码；未传/null/空字符串使用公邮代理；仅本次内存使用。"},
        })
        schema["required"] = ["mailbox", *required]
        if name in ("create_contact", "update_contact"):
            schema["properties"]["contact"] = deepcopy(CONTACT_SCHEMA)
            if name == "create_contact":
                schema["properties"]["contact"]["required"] = ["display_name"]
        if name == "create_draft":
            schema["allOf"] = [
                {"if": {"required": ["mode"], "properties": {"mode": {"enum": ["reply", "reply_all", "forward"]}}},
                 "then": {"required": ["reply_to"]}, "else": {"not": {"required": ["reply_to"]}}},
                {"not": {"required": ["body_format", "mode"], "properties": {
                    "body_format": {"const": "html"}, "mode": {"enum": ["reply", "reply_all", "forward"]}}}},
            ]
        elif name == "update_draft":
            schema["dependentRequired"] = {"body_format": ["body"], "body_action": ["body"]}
        elif name == "find_message":
            schema["allOf"] = [{"if": {"required": ["aqs"]}, "then": {
                "properties": {"query": {"const": ""}},
                "not": {"anyOf": [{"required": [key]} for key in ("since", "until", "is_unread", "has_attachments")]},
            }}]
        description = _tool_description(name)
        result.append({"name": name, "description": description, "inputSchema": schema})
    return result


SPECS = {tool["name"]: tool for tool in public_tools()}
assert len(SPECS) == 29
