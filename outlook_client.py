import logging
import os
from datetime import datetime
from typing import List, Optional

from exchangelib import (
    Q,
    Account,
    Attendee,
    CalendarItem,
    Configuration,
    Credentials,
    DELEGATE,
    EWSDateTime,
    Mailbox,
    Message,
)
from exchangelib.folders import Inbox
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter

from utils.lanid_email import LOCAL_TIMEZONE

from availability_operations import AvailabilityOperations
from calendar_operations import CalendarOperations
from config import OutlookConfig, http_timeout
from flag_operations import FlagOperations
from mail_operations import MailOperations
from mirror import MirrorStore, WaitingOnOperations
from oof_operations import OofOperations
from people_operations import PeopleOperations
from status_operations import StatusOperations
from task_operations import TaskOperations
from write_operations import WriteOperations


logger = logging.getLogger(__name__)


class OutlookClient(
    MailOperations,
    WriteOperations,
    CalendarOperations,
    PeopleOperations,
    TaskOperations,
    FlagOperations,
    AvailabilityOperations,
    OofOperations,
    StatusOperations,
    WaitingOnOperations,
):
    """使用固定服务凭据访问当前请求者邮箱的 Exchange 客户端。

    聚合扩展的 28 工具能力: mail read / draft+mail writes / calendar /
    people/(GAL+contacts) / tasks / availability / oof / status / waiting_on 镜像。
    账户初始化与既有读写方法保持不变。
    """

    def __init__(self, config: OutlookConfig):
        self.config = config
        self.account = self._connect()
        self._mirror = None
        data_dir = os.getenv("EWS_MCP_DATA_DIR")
        if data_dir:
            self._mirror = MirrorStore(os.path.join(data_dir, "mirror.db"))

    def _connect(self) -> Account:
        try:
            account = self._create_account(self.config.email)
            logger.info("Exchange 邮箱连接成功")
            return account
        except Exception as exc:
            logger.error("连接失败: error_type=%s", exc.__class__.__name__)
            raise

    def _create_account(self, mailbox: str) -> Account:
        BaseProtocol.HTTP_ADAPTER_CLS = NoVerifyHTTPAdapter
        BaseProtocol.TIMEOUT = http_timeout()
        credentials = Credentials(
            username=self.config.lanid,
            password=self.config.password,
        )
        exchange_config = Configuration(
            server=self.config.server,
            credentials=credentials,
        )
        return Account(
            primary_smtp_address=mailbox,
            config=exchange_config,
            autodiscover=self.config.autodiscover,
            access_type=DELEGATE,
        )

    @staticmethod
    def _body(body, use_html):
        if use_html:
            from exchangelib import HTMLBody

            return HTMLBody(body)
        return body

    def get_calendar(self) -> CalendarItem:
        """获取当前请求者的日程。"""
        return self.account.calendar

    def get_inbox(self) -> Inbox:
        """获取当前请求者的收件箱。"""
        return self.account.inbox

    def search(self, keyword, limit: int = 10):
        """搜索当前请求者收件箱中主题包含关键词的邮件。"""
        filter_query = Q(subject__contains=keyword)
        return (
            self.get_inbox()
            .filter(filter_query)
            .order_by("-datetime_sent")[:limit]
        )

    def create_email_draft(self, subject, body, to_emails) -> bool:
        """在当前请求者邮箱中创建邮件草稿。"""
        try:
            recipients = [Mailbox(email_address=email) for email in to_emails]
            draft = Message(
                account=self.account,
                folder=self.account.drafts,
                subject=subject,
                body=body,
                to_recipients=recipients,
                is_draft=True,
            )
            draft.save()
            return True
        except Exception as exc:
            logger.error(
                "存入草稿箱失败: error_type=%s", exc.__class__.__name__
            )
            return False

    def create_meeting_draft(
        self,
        subject,
        body,
        to_emails,
        start_date=None,
        end_date=None,
    ) -> dict:
        """在当前请求者日历创建会议，并向参会人发送会议邀请。

        成功返回 {"id": ..., "changekey": ...}（新日程的 event id）；失败返回 None。
        """
        try:
            attendees = [
                Attendee(mailbox=Mailbox(email_address=email))
                for email in to_emails
            ]
            draft = CalendarItem(
                account=self.account,
                folder=self.account.calendar,
                subject=subject,
                start=start_date,
                end=end_date,
                body=body,
                required_attendees=attendees,
                is_draft=True,
            )
            draft.save(send_meeting_invitations="SendToAllAndSaveCopy")
            return {"id": draft.id, "changekey": draft.changekey}
        except Exception as exc:
            logger.error(
                "存入会议草稿箱失败: error_type=%s", exc.__class__.__name__
            )
            return None

    def get_meeting(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        limit: int = 10,
    ) -> List[CalendarItem]:
        """获取当前请求者的日程列表。"""
        return (
            self.get_calendar()
            .view(start=start_date, end=end_date)
            .order_by("start")[:limit]
        )

    def get_emails(
        self,
        start_date: datetime = None,
        end_date: datetime = None,
        limit: int = None,
        unread_only: bool = False,
    ) -> List[Message]:
        """获取当前请求者的邮件列表。"""
        if not limit:
            limit = 1000
        # 健康路径（与 find_message 一致）：走 _tool_folder（distinguished 文件夹，不经
        # account.inbox 默认发现）；时间范围用 __gte/__lt + EWSDateTime，并用 .only() 字段投影只取
        # 所需字段。整条(full)抓取会在本收件箱某封邮件上触发 ErrorConnectionFailed（实测 0/5），
        # 用 .only() 可稳定跳过（实测 5/5，含 body）。
        inbox = self._tool_folder("inbox")
        filters = {}
        if start_date is not None:
            filters["datetime_received__gte"] = EWSDateTime.from_datetime(
                start_date.astimezone(LOCAL_TIMEZONE)
            )
        if end_date is not None:
            filters["datetime_received__lt"] = EWSDateTime.from_datetime(
                end_date.astimezone(LOCAL_TIMEZONE)
            )
        if unread_only:
            filters["is_read"] = False
        return (
            inbox.filter(**filters)
            .only("id", "subject", "sender", "datetime_received", "is_read", "body")
            .order_by("-datetime_received")[:limit]
        )

    def get_emails1(
        self, limit: int = 10, unread_only: bool = False
    ) -> List[Message]:
        """获取当前请求者最近的邮件列表。"""
        inbox = self.get_inbox()
        emails = inbox.filter(is_read=False) if unread_only else inbox.all()
        return emails.order_by("-datetime_received")[:limit]

    def get_email_by_id(self, message_id: str) -> Optional[Message]:
        """根据 ID 获取当前请求者邮箱中的邮件。"""
        try:
            return self.account.inbox.get(id=message_id)
        except Exception as exc:
            logger.error(
                "获取邮件失败: error_type=%s", exc.__class__.__name__
            )
            return None

    def mark_as_read(self, message: Message) -> bool:
        try:
            message.is_read = True
            message.save()
            return True
        except Exception as exc:
            logger.error(
                "标记邮件为已读失败: error_type=%s", exc.__class__.__name__
            )
            return False

    def mark_as_unread(self, message: Message) -> bool:
        try:
            message.is_read = False
            message.save()
            return True
        except Exception as exc:
            logger.error(
                "标记邮件为未读失败: error_type=%s", exc.__class__.__name__
            )
            return False

    def reply_to_email(
        self, original_email: Message, body: str, html: bool = False
    ) -> bool:
        try:
            reply = original_email.create_reply()
            reply.body = self._body(body, html)
            reply.send()
            logger.info("回复已发送")
            return True
        except Exception as exc:
            logger.error(
                "回复邮件失败: error_type=%s", exc.__class__.__name__
            )
            return False
