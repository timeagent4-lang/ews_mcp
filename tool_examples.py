"""平台可读取的工具参数示例；ID 和凭据均为需替换的占位值。"""

MAIL_ID = "<find_message返回的id>"
DRAFT_ID = "<create_draft返回的id>"
EVENT_ID = "<list_events返回的id>"
FOLDER_ID = "<list_contact_folders返回的folder_id>"
CONTACT_ID = "<list_contacts返回的id>"
ATTACHMENT_ID = "<get_attachment返回的附件attachment_id>"

# 每项首例同时展示公邮与个人两种凭据；其余示例展示同一工具的不同场景。
TOOL_EXAMPLES = {
    "list_folders": [("查看邮件文件夹", {})],
    "find_message": [
        ("按主题、时间和未读状态搜索", {"folder": "inbox", "query": "项目进度", "since": "2026-10-01",
                                       "until": "2026-11-01", "is_unread": True, "offset": 0, "limit": 10}),
        ("只使用 AQS 搜索", {"folder": "inbox", "aqs": 'subject:"项目进度"', "limit": 10}),
    ],
    "get_message": [
        ("读取邮件纯文本正文", {"message_id": MAIL_ID, "clean_body": True, "max_body_chars": 10000}),
        ("接续读取长正文", {"message_id": MAIL_ID, "clean_body": True, "body_offset": 10000, "max_body_chars": 10000}),
    ],
    "get_thread": [("读取邮件所属会话", {"message_id": MAIL_ID, "offset": 0, "limit": 20})],
    "get_attachment": [
        ("先列出该邮件附件", {"message_id": MAIL_ID, "mode": "info"}),
        ("读取该邮件的文本附件", {"message_id": MAIL_ID, "attachment_id": ATTACHMENT_ID, "mode": "text"}),
    ],
    "prepare_attachment_download": [("生成该文件附件的下载链接", {"message_id": MAIL_ID, "attachment_id": ATTACHMENT_ID})],
    "get_mailbox_overview": [("查看收件箱概览", {"limit": 5})],
    "create_draft": [
        ("新邮件草稿", {"mode": "new", "to_emails": "receiver@example.com;second@example.com",
                        "subject": "项目进度", "body": "本周项目进度如下。"}),
        ("新建 HTML 草稿", {"mode": "new", "to_emails": "receiver@example.com", "subject": "进度表",
                            "body_format": "html", "body": '<table style="border-collapse:collapse"><tr><td>进度</td><td>已完成</td></tr></table>'}),
        ("回复原邮件", {"mode": "reply", "reply_to": MAIL_ID, "body": "收到，我会在周五前反馈。"}),
        ("全部回复原邮件", {"mode": "reply_all", "reply_to": MAIL_ID, "body": "各位好，以下是新增说明。"}),
        ("转发原邮件", {"mode": "forward", "reply_to": MAIL_ID, "to_emails": "receiver@example.com", "body": "请参考下方原邮件。"}),
    ],
    "update_draft": [
        ("只修改收件人和主题", {"draft_id": DRAFT_ID, "to_emails": "receiver@example.com", "subject": "更新后的主题"}),
        ("保留原正文并前置纯文本", {"draft_id": DRAFT_ID, "body": "补充：请优先处理第一项。", "body_action": "prepend"}),
        ("在已有 HTML 草稿前置 HTML 片段", {"draft_id": DRAFT_ID, "body": "<p>补充说明</p>", "body_action": "prepend", "body_format": "html"}),
    ],
    "delete_draft": [("将草稿移到已删除邮件", {"draft_id": DRAFT_ID})],
    "send_draft": [("发送已保存的草稿", {"draft_id": DRAFT_ID})],
    "update_messages": [("标为已读并调整分类", {"ids": [MAIL_ID], "set_read": True, "categories_add": ["已跟进"], "categories_remove": ["待处理"]})],
    "move_messages": [("将邮件移动到已发送文件夹", {"ids": [MAIL_ID], "to_folder": "sent"})],
    "delete_messages": [("将邮件移到已删除邮件", {"ids": [MAIL_ID]})],
    "set_message_flag": [
        ("设置跟进旗标和截止日期", {"message_id": MAIL_ID, "flag": "flagged", "due_date": "2026-10-20T18:00:00+08:00"}),
        ("完成跟进并保留旧截止日期", {"message_id": MAIL_ID, "flag": "complete"}),
        ("清除旗标和相关日期", {"message_id": MAIL_ID, "flag": "clear"}),
    ],
    "list_flagged_messages": [("查询未完成跟进邮件", {"folder": "inbox", "status": "flagged", "limit": 20})],
    "list_events": [("查询时间窗内日程", {"start": "2026-10-20T00:00:00+08:00", "end": "2026-10-21T00:00:00+08:00", "offset": 0, "limit": 20})],
    "get_event": [("读取日程详情", {"event_id": EVENT_ID})],
    "create_event": [
        ("创建个人预约", {"subject": "整理项目资料", "start": "2026-10-20T09:00:00+08:00", "end": "2026-10-20T10:00:00+08:00", "location": "办公室"}),
        ("创建会议并发送邀请", {"subject": "项目讨论", "start": "2026-10-20T14:00:00+08:00", "end": "2026-10-20T15:00:00+08:00",
                                "attendees": ["receiver@example.com"], "send_invitations": True}),
    ],
    "update_event": [
        ("静默更新自己组织的日程地点", {"event_id": EVENT_ID, "location": "会议室 B", "notify_attendees": False}),
        ("改期并通知参与者", {"event_id": EVENT_ID, "start": "2026-10-20T15:00:00+08:00", "end": "2026-10-20T16:00:00+08:00", "notify_attendees": True}),
    ],
    "respond_to_event": [
        ("作为参与者接受邀请", {"event_id": EVENT_ID, "response": "accept", "message": "我会准时参加。"}),
        ("暂定接受邀请", {"event_id": EVENT_ID, "response": "tentative", "message": "待确认时间。"}),
        ("拒绝邀请", {"event_id": EVENT_ID, "response": "decline", "message": "时间冲突，无法参加。"}),
    ],
    "cancel_event": [("取消自己组织的日程", {"event_id": EVENT_ID, "message": "计划调整，本次日程取消。"})],
    "list_contact_folders": [("发现已有联系人文件夹", {})],
    "list_contacts": [
        ("读取指定自定义文件夹联系人", {"folder_id": FOLDER_ID, "offset": 0, "limit": 50}),
        ("读取默认 Contacts", {"offset": 0, "limit": 50}),
    ],
    "find_people": [
        ("只查指定文件夹", {"folder_id": FOLDER_ID, "source": "contacts", "query": "张三", "limit": 20}),
        ("只查询 GAL", {"source": "gal", "query": "张三", "limit": 20}),
        ("同时查询 GAL 与默认 Contacts", {"source": "auto", "query": "张三", "limit": 20}),
    ],
    "get_contact": [
        ("读取指定文件夹联系人", {"folder_id": FOLDER_ID, "contact_id": CONTACT_ID}),
        ("按准确邮箱读取 GAL 联系人", {"contact_id": "zhang@example.com"}),
    ],
    "create_contact": [
        ("在指定文件夹新建联系人", {"folder_id": FOLDER_ID, "contact": {
            "display_name": "张三", "given_name": "三", "surname": "张", "company_name": "示例公司",
            "email_addresses": {"EmailAddress1": "zhang@example.com"},
            "phone_numbers": {"MobilePhone": "13800000000", "BusinessPhone": "010-12345678"},
            "physical_addresses": {"Business": {"street": "示例路1号", "city": "北京", "state": "北京", "country": "中国", "zipcode": "100000"}},
            "notes": "项目联系人，下午方便联系。",
        }}),
        ("在默认 Contacts 新建最简联系人", {"contact": {"display_name": "李四"}}),
    ],
    "update_contact": [
        ("修改手机号、备注并清空工作地址邮编", {"folder_id": FOLDER_ID, "contact_id": CONTACT_ID, "contact": {
            "phone_numbers": {"MobilePhone": "13900000000"}, "physical_addresses": {"Business": {"zipcode": ""}}, "notes": "下周再次跟进。",
        }}),
        ("清空备注、邮箱和地址，保留其他资料", {"folder_id": FOLDER_ID, "contact_id": CONTACT_ID,
                                                    "contact": {"notes": "", "email_addresses": {}, "physical_addresses": {}}}),
        ("只删除联系人照片", {"folder_id": FOLDER_ID, "contact_id": CONTACT_ID, "contact": {"photo": None}}),
        ("上传或更换照片，替换 Base64 占位内容", {"folder_id": FOLDER_ID, "contact_id": CONTACT_ID, "contact": {
            "photo": {"filename": "photo.jpg", "content_type": "image/jpeg", "content_base64": "<照片文件的Base64内容>"},
        }}),
    ],
    "get_server_status": [("检查目标邮箱连接与发送开关", {})],
}

TOOL_GUIDANCE = {
    "list_folders": "邮件工具的 folder/to_folder 使用各工具允许的 inbox、drafts、sent 别名；联系人 folder_id 使用 list_contact_folders 的真实 ID。",
    "find_message": "返回的 results 每项 id 可用于后续邮件工具；下一页使用返回的 next_offset。until 不包含边界时刻。aqs 与非空 query、since/until/is_unread/has_attachments 不能合用，未使用字段直接省略。",
    "get_message": "message_id 来自同一 mailbox 的邮件查询；正文续读用返回的 next_body_offset 作为 body_offset，并保持 clean_body 一致。示例中的 10000 要替换为实际 next_body_offset。",
    "get_thread": "message_id 来自同一 mailbox 的 find_message/get_message；用返回的 next_offset 续页，检查 coverage/source_errors 了解未覆盖的文件夹。",
    "get_attachment": "先省略 attachment_id 列出附件，再用 results.attachments 条目里的 attachment_id 读取；message_id 与 attachment_id 必须配对。仅受支持文本附件可读文本；PDF/Office 等原文件下载用 prepare_attachment_download。",
    "prepare_attachment_download": "先 get_attachment 获取同一邮件的文件附件 attachment_id，再把它原样传入；嵌入项目附件不支持原文件下载。返回 download_url 是临时签名链接，过期后重新生成。",
    "create_draft": "new 不传 reply_to；reply/reply_all/forward 的 reply_to 来自同一 mailbox 的邮件查询，body 只写新增纯文本，不粘贴原文。to_emails/cc_emails/bcc_emails 为分号分隔字符串。保存成功后将 results.id 作为 send_draft 的 draft_id；创建草稿本身不发送。",
    "update_draft": "draft_id 用 create_draft 返回的 id，或在 drafts 中查询所得 id。只改收件人/主题时省略所有正文参数；prepend 保留原文，replace 会替换整个正文。HTML prepend 必须用于已有 HTML 草稿，body 必须为 HTML 片段。",
    "send_draft": "先 create_draft 保存带收件人的草稿，再把 results.id 传为 draft_id。发送需 EWS_MCP_SEND_ENABLED=true；发送后草稿 ID 不再用于访问邮件，应重新查询 sent。公邮代理允许显示“公邮代用户发送”。",
    "delete_draft": "draft_id 来自 create_draft 或 drafts 查询；只移入 Deleted Items，不物理删除。",
    "update_messages": "ids 是同一 mailbox 的普通邮件 ID 数组，1–50 个且互不重复；set_read=false 标为未读。分类用字符串数组，categories_remove 优先于 categories_add；检查逐项结果。",
    "move_messages": "ids 来自同一 mailbox 的普通邮件查询，1–50 个且互不重复；to_folder 只支持 inbox/sent，不接受联系人 folder_id。成功后用逐项结果 new_id 继续操作。",
    "delete_messages": "ids 来自同一 mailbox 的普通邮件查询，1–50 个且互不重复；不支持会议通知对象。只移入 Deleted Items，检查逐项结果。",
    "set_message_flag": "一次操作一封普通邮件。flagged 省略 due_date 会清空日期；complete 省略 due_date 保留旧日期；clear 清除全部旗标日期，省略 due_date。",
    "list_flagged_messages": "status 可用 flagged/complete/all；本工具只按 limit 返回最多指定条数，没有 offset 参数。",
    "list_events": "使用 ISO 时间，建议显式携带 +08:00；下一页 offset=本页 offset+limit，has_more=false 表示无下一页。返回日程 id 用于日历详情及写入。",
    "get_event": "event_id 来自同一 mailbox 的 list_events/create_event；先检查组织者、参与者、type 等信息再选择更新/响应/取消工具。",
    "create_event": "start/end 建议使用带时区的 ISO 时间；省略 end 默认一小时。attendees 是邮箱数组；发送邀请需 send_invitations=true 且 EWS_MCP_SEND_ENABLED=true。只支持本工具 schema 中的参数。",
    "update_event": "先用 list_events/get_event 获取当前目标邮箱组织的明确单次/Occurrence/Exception 日程；不能编辑周期主事件。至少传一个要修改的字段，省略字段保留；notify_attendees=true 需 EWS_MCP_SEND_ENABLED=true。",
    "respond_to_event": "先用 list_events/get_event 获取当前邮箱作为参与者的明确日程；组织者不能响应自己的邀请，周期主事件不支持。accept/tentative/decline 都会发送响应，需要 EWS_MCP_SEND_ENABLED=true。",
    "cancel_event": "event_id 来自同一 mailbox 的日历查询且目标邮箱须是组织者；会议会发送取消通知，需 EWS_MCP_SEND_ENABLED=true，省略 message 不会关闭通知。无参与者的个人预约只移入 Deleted Items。",
    "list_contact_folders": "按 name/path 选中已有普通联系人文件夹，将该行 folder_id 原样用于联系人工具；同名文件夹按 ID 区分。不创建文件夹；旧版无法可靠分类的目录列在 unclassified_folders。",
    "list_contacts": "先用同一 mailbox 和同一凭据模式调用 list_contact_folders，再选 folder_id；省略 folder_id 只查默认 Contacts。下一页使用返回的 next_offset；联系人 id 用于 get_contact/update_contact。",
    "find_people": "提供 folder_id 时只查该文件夹，不能同时 source=gal；查 GAL 时省略 folder_id。source=auto 查询 GAL 和默认 Contacts；检查 sources/source_errors，GAL 只读。",
    "get_contact": "个人联系人 id 来自同一文件夹的 list_contacts/find_people；自定义文件夹的 folder_id 必须一起传。只有省略 folder_id 时，contact_id 才可传准确 GAL 邮箱；GAL 结果只读。",
    "create_contact": "指定目录流程：list_contact_folders → 选择 folder_id → create_contact。省略 folder_id 新建到默认 Contacts。资料放在 contact 对象内；display_name 必填，其余省略默认为空。EmailAddress1–3、MobilePhone/BusinessPhone、Business/Home/Other 是固定标签。",
    "update_contact": "先在所选文件夹用 list_contacts/find_people 获取 id；contact_id 和 folder_id 保持同一邮箱及文件夹。编辑省略字段保留；文本/日期用空字符串或 null 清空，数组用 []，索引对象用 {} 或 null。地址和标签只更新传入部分，display_name 不能清空；photo=null 仅删除照片，保留其他附件。上传照片用 filename/content_type/content_base64 对象，不能把查询返回的照片元数据数组直接传入。不能编辑 GAL。",
    "get_server_status": "可先用此工具检查目标 mailbox 连接与发送开关；不会返回密码。每次后续调用仍需传 mailbox，并按所选凭据模式传入个人凭据或省略 password。",
}
