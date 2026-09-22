"""各工具的入参模型。

调用约定：所有入参打包到单个 params 对象里传，例如
{"params": {"lanid": "lch123456", "name": "张三", "query": "项目周报"}}。
Authorization/API Key 由客户端/代理携带，不放入工具参数。
"""

from typing import Annotated, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


class RequesterParams(BaseModel):
    """请求人身份；所有工具最父级。"""

    model_config = ConfigDict(extra="forbid")

    lanid: str = Field(
        ...,
        description="请求人的 LANID，例如 lch123456。",
    )
    name: str = Field(
        ...,
        description=(
            "请求人的中文姓名。服务端会与 OA 返回的 ChinNm 校验，"
            "不一致时拒绝调用。"
        ),
    )

    @field_validator("lanid", "name")
    @classmethod
    def normalize_requester_field(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("requester field must not be empty")
        return normalized


class ListEmailsParams(RequesterParams):
    """查看最近 7 天邮件。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                }
            ]
        },
    )


class TodoEmailsParams(RequesterParams):
    """提取待办事项。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                }
            ]
        },
    )


class SummaryYesterdayParams(RequesterParams):
    """总结最近一天邮件。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "unread_only": False,
                }
            ]
        },
    )

    unread_only: bool = Field(
        False,
        description="是否只看未读邮件。",
    )


class SearchMailParams(RequesterParams):
    """按关键词搜索邮件，支持文件夹、时间窗、未读/附件过滤与 AQS 高级查询。

    AQS 不能与 query 或结构化筛选（since/until/is_unread/has_attachments）混用。
    """

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "query": "项目周报",
                    "limit": 10,
                }
            ]
        },
    )

    query: str = Field(
        ...,
        description="邮件搜索关键词（主题包含），例如：项目周报。",
    )
    limit: int = Field(
        10,
        description="最多返回的邮件数量，默认值为 10。",
    )
    aqs: Optional[str] = Field(
        None,
        description=(
            "AQS 高级查询串（如 body:预算 AND hasattachment:true）。"
            "不能与 query 或结构化筛选混用。"
        ),
    )
    folder: Literal["inbox", "drafts", "sent"] = Field(
        "inbox",
        description="搜索的文件夹，默认 inbox。",
    )
    since: Optional[str] = Field(
        None,
        description="起始时间，ISO 日期时间，按文件夹时间字段过滤。",
    )
    until: Optional[str] = Field(
        None,
        description="结束时间，ISO 日期时间，按文件夹时间字段过滤。",
    )
    is_unread: Optional[bool] = Field(
        None,
        description="True 只看未读，False 只看已读，None 不过滤。",
    )
    has_attachments: Optional[bool] = Field(
        None,
        description="True 只看含附件，False 只看不含附件，None 不过滤。",
    )
    offset: int = Field(
        0,
        ge=0,
        description="分页偏移，从 0 开始。",
    )
    include_body: bool = Field(
        True,
        description="是否返回正文，False 只返回元数据。",
    )

    @model_validator(mode="after")
    def validate_query_mode(self):
        if self.aqs is not None:
            if not self.aqs.strip():
                raise ValueError("aqs must not be empty")
            if self.query or any(
                value is not None
                for value in (self.since, self.until, self.is_unread, self.has_attachments)
            ):
                raise ValueError(
                    "aqs cannot be combined with query or structured filters"
                )
        return self


class CreateMailDraftParams(RequesterParams):
    """创建邮件草稿（新建/回复/全部回复/转发），仅保存不发送。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "subject": "项目进度",
                    "body": "请查看最新项目进度。",
                    "to_emails": "lisi@example.com;wangwu@example.com",
                }
            ]
        },
    )

    subject: str = Field(
        ...,
        description="邮件主题（回复/转发时可省略，缺省自动加 RE:/FW: 前缀）。",
    )
    body: str = Field(
        ...,
        description="邮件正文。",
    )
    to_emails: str = Field(
        ...,
        description="收件人邮箱，多个用英文分号(;)分隔；为空则仅存草稿。",
    )
    cc_emails: str = Field(
        "",
        description="抄送邮箱，多个用英文分号(;)分隔；为空则不抄送。",
    )
    bcc_emails: str = Field(
        "",
        description="密送邮箱，多个用英文分号(;)分隔；为空则不密送。",
    )
    mode: Literal["new", "reply", "reply_all", "forward"] = Field(
        "new",
        description="草稿类型：new 新建、reply 回复、reply_all 全部回复、forward 转发。",
    )
    reply_to: Optional[str] = Field(
        None,
        description="回复/转发时要基于的原邮件 ID；mode 非 new 时必填。",
    )

    @model_validator(mode="after")
    def validate_reply(self):
        if self.mode != "new" and not (self.reply_to and self.reply_to.strip()):
            raise ValueError("reply_to is required for reply/forward drafts")
        return self


class CreateMeetingParams(RequesterParams):
    """创建并发送会议邀请。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "subject": "项目会议",
                    "body": "讨论项目进展",
                    "start_date": "2026-08-26 10:00",
                    "end_date": "2026-08-26 11:00",
                    "to_emails": "lisi@example.com;wangwu@example.com",
                }
            ]
        },
    )

    subject: str = Field(
        ...,
        description="会议主题。",
    )
    body: str = Field(
        ...,
        description="会议正文。",
    )
    start_date: str = Field(
        ...,
        description="会议开始时间，推荐格式 YYYY-MM-DD HH:mm。",
    )
    to_emails: str = Field(
        ...,
        description="参会人邮箱，多个用英文分号(;)分隔；为空则仅邀请本人。",
    )
    end_date: Optional[str] = Field(
        None,
        description="会议结束时间，推荐格式 YYYY-MM-DD HH:mm；缺省为开始后 1 小时。",
    )


class ListMeetingsParams(RequesterParams):
    """查看会议日程。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "num_days": 10,
                }
            ]
        },
    )

    num_days: int = Field(
        10,
        description="从现在起往后查看的日程天数，默认 10。",
    )


ItemId = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1),
]


class ListFoldersParams(RequesterParams):
    """列出当前员工的收件箱、草稿箱和已发送邮件。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                }
            ]
        },
    )


class GetMessageParams(RequesterParams):
    """读取当前员工的邮件详情，支持正文分页。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "message_id": "m1",
                }
            ]
        },
    )

    message_id: ItemId = Field(
        ...,
        description="邮件 ID。",
    )
    clean_body: bool = Field(
        False,
        description="是否清洗 HTML 正文为纯文本；默认关闭。",
    )
    max_body_chars: int = Field(
        10000,
        ge=1,
        le=100000,
        description="正文单次返回的最大字符数，1 到 100000，默认 10000。",
    )
    body_offset: int = Field(
        0,
        ge=0,
        description="正文读取偏移，从 0 开始。",
    )


class GetThreadParams(RequesterParams):
    """按指定邮件的会话 ID 整理本人收件箱及已发送邮件中的往来，支持分页。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "message_id": "m1",
                }
            ]
        },
    )

    message_id: ItemId = Field(
        ...,
        description="会话中任意邮件的 ID。",
    )
    offset: int = Field(
        0,
        ge=0,
        description="分页偏移，从 0 开始。",
    )
    limit: int = Field(
        20,
        ge=1,
        le=50,
        description="单页返回邮件数，1 到 50，默认 20。",
    )


class GetAttachmentParams(RequesterParams):
    """查看附件清单或读取单个附件的受限文本内容。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                    "message_id": "m1",
                }
            ]
        },
    )

    message_id: ItemId = Field(
        ...,
        description="附件所属邮件的 ID。",
    )
    attachment_id: Optional[ItemId] = Field(
        None,
        description="附件 ID；缺省时只返回附件清单。",
    )
    mode: Literal["auto", "info", "text"] = Field(
        "auto",
        description=(
            "attachment_id 非空时生效：info 只读元数据；text 读取受限文本；"
            "auto 自动判断是否可提取文本。"
        ),
    )


class PrepareAttachmentDownloadParams(RequesterParams):
    """准备原始附件下载，返回短期链接和校验信息，不返回文件正文。"""

    model_config = ConfigDict(extra="forbid")
    message_id: ItemId = Field(..., description="附件所属邮件的 ID。")
    attachment_id: ItemId = Field(..., description="该邮件附件清单返回的附件 ID，必填。")


class MailboxOverviewParams(RequesterParams):
    """返回当前员工的邮件概览，不调用 LLM。"""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lanid": "lch123456",
                    "name": "张三",
                }
            ]
        },
    )

    limit: int = Field(
        5,
        ge=1,
        le=50,
        description="最近未读邮件的返回数量，1 到 50，默认 5。",
    )


class ConfirmationParams(RequesterParams):
    """二次确认参数；仅用于需要用户明确批准后才执行的写操作。"""

    model_config = ConfigDict(extra="forbid")

    confirm: bool = Field(
        False,
        description="是否明确执行；False 只返回预览。",
    )
    confirmation_id: Optional[str] = Field(
        None,
        description="预览返回的确认 ID；内容变化后旧 ID 会失效。",
    )


class UpdateDraftParams(RequesterParams):
    """修改本人草稿的指定字段；未指定的字段与附件保持不变。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: ItemId = Field(
        ...,
        description="草稿 ID。",
    )
    subject: Optional[str] = Field(
        None,
        description="新主题；None 表示不修改。",
    )
    body: Optional[str] = Field(
        None,
        description="新正文；None 表示不修改。",
    )
    to_emails: Optional[str] = Field(
        None,
        description="收件人（分号分隔）；None 表示不修改。",
    )
    cc_emails: Optional[str] = Field(
        None,
        description="抄送（分号分隔）；None 表示不修改。",
    )
    bcc_emails: Optional[str] = Field(
        None,
        description="密送（分号分隔）；None 表示不修改。",
    )


class SendDraftParams(ConfirmationParams):
    """确认后代表本人发送草稿，并保存到本人已发送邮件。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: ItemId = Field(
        ...,
        description="草稿 ID。",
    )


class DeleteDraftParams(ConfirmationParams):
    """确认后普通删除草稿（移至已删除邮件）。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: ItemId = Field(
        ...,
        description="草稿 ID。",
    )


class UpdateMessagesParams(RequesterParams):
    """批量修改本人普通邮件的已读状态与分类。"""

    model_config = ConfigDict(extra="forbid")

    ids: List[ItemId] = Field(..., min_length=1, max_length=50, description="邮件 ID 列表（1-50）。")
    set_read: Optional[bool] = Field(
        None,
        description="True 标已读，False 标未读，None 不修改。",
    )
    categories_add: Optional[List[str]] = Field(
        None, max_length=50, description="要新增的分类；None 不修改。"
    )
    categories_remove: Optional[List[str]] = Field(
        None, max_length=50, description="要移除的分类；None 不修改。"
    )


class MoveMessagesParams(RequesterParams):
    """在本人收件箱与已发送邮件间移动普通邮件。"""

    model_config = ConfigDict(extra="forbid")

    ids: List[ItemId] = Field(..., min_length=1, max_length=50, description="邮件 ID 列表（1-50）。")
    to_folder: Literal["inbox", "sent"] = Field(
        ..., description="目标文件夹：Inbox 或 sent。"
    )


class DeleteMessagesParams(ConfirmationParams):
    """确认后普通删除本人邮件（移至已删除邮件，变更返回结果）。"""

    model_config = ConfigDict(extra="forbid")

    ids: List[ItemId] = Field(..., min_length=1, max_length=50, description="邮件 ID 列表（1-50）。")


class GetEventParams(RequesterParams):
    """读取本人日历内的日程详情及参与者响应。"""

    model_config = ConfigDict(extra="forbid")

    event_id: ItemId = Field(
        ...,
        description="日程 ID（来自 list_meetings 返回的 id）。",
    )


class UpdateEventParams(ConfirmationParams):
    """更新本人日历日程的指定字段；通知参会者时先预览确认。"""

    model_config = ConfigDict(extra="forbid")

    event_id: ItemId = Field(
        ...,
        description="日程 ID。",
    )
    subject: Optional[str] = Field(
        None,
        description="新主题；None 表示不修改。",
    )
    body: Optional[str] = Field(
        None,
        description="新正文；None 表示不修改。",
    )
    start: Optional[str] = Field(
        None,
        description="新开始时间（ISO 或 YYYY-MM-DD HH:mm）；None 表示不修改。",
    )
    end: Optional[str] = Field(
        None,
        description="新结束时间；None 表示不修改。",
    )
    location: Optional[str] = Field(
        None,
        description="新地点；None 表示不修改。",
    )
    notify_attendees: bool = Field(
        False,
        description="是否在更新时通知参会者；True 会先要求预览确认。",
    )


class RespondToEventParams(ConfirmationParams):
    """确认后响应本人收到的会议邀请（accept/tentative/decline）。"""

    model_config = ConfigDict(extra="forbid")

    event_id: ItemId = Field(
        ...,
        description="会议邀请的日程 ID。",
    )
    response: Literal["accept", "tentative", "decline"] = Field(
        ...,
        description="响应类型：accept 接受、tentative 暂定、decline 拒绝。",
    )
    message: Optional[str] = Field(
        None,
        description="随响应发送的说明；缺省不发送。",
    )


class CancelEventParams(ConfirmationParams):
    """当前员工是组织者时，确认后取消日程并发送取消通知。"""

    model_config = ConfigDict(extra="forbid")

    event_id: ItemId = Field(
        ...,
        description="本人组织的日程 ID。",
    )
    message: Optional[str] = Field(
        None,
        description="取消说明；缺省不发送。",
    )
