"""每次请求创建绑定目标邮箱的 Exchange 客户端。"""
from exchangelib import Account, Configuration, Credentials, DELEGATE
from exchangelib.protocol import BaseProtocol, NoVerifyHTTPAdapter
from calendar_operations import CalendarOperations
from config import OutlookConfig, http_timeout
from flag_operations import FlagOperations
from mail_operations import MailOperations
from people_operations import PeopleOperations
from status_operations import StatusOperations
from write_operations import WriteOperations


class OutlookClient(MailOperations, WriteOperations, CalendarOperations,
                    PeopleOperations, FlagOperations, StatusOperations):
    def __init__(self, config: OutlookConfig):
        self.config = config
        self.account = self._create_account(config.email)

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
