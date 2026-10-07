import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


@dataclass
class OutlookConfig:
    """单次请求的 Exchange 配置，不保存凭据。"""

    lanid: Optional[str] = field(default=None, repr=False)
    email: Optional[str] = None
    password: Optional[str] = field(default=None, repr=False)
    server: Optional[str] = None
    autodiscover: bool = False
    access_type: str = "delegate"
    credential_mode: str = "delegate"

    @classmethod
    def from_tool_params(cls, mailbox: str, username=None, password=None) -> "OutlookConfig":
        from tool_support import ToolOperationError

        email = mailbox.strip().lower() if isinstance(mailbox, str) else ""
        if not email or email.count("@") != 1 or any(c.isspace() for c in email):
            raise ToolOperationError("INVALID_MAILBOX", "目标邮箱地址无效。")
        if password is not None and not isinstance(password, str):
            raise ToolOperationError("INVALID_PARAMS", "密码必须是字符串或 null。")
        server = os.getenv("OUTLOOK_SERVER") or os.getenv("outlook_host")
        if not server or not server.strip():
            raise ToolOperationError("CONFIG_ERROR", "缺少 Exchange 服务账号配置: OUTLOOK_SERVER")
        if password is not None and password != "":
            login = (username or email).strip()
            if not login:
                raise ToolOperationError("INVALID_PARAMS", "个人登录账号不能为空。")
            return cls(lanid=login, email=email, password=password, server=server.strip(), credential_mode="personal")
        return cls.from_service_env(email)

    @classmethod
    def from_service_env(cls, target_email: str) -> "OutlookConfig":
        """使用公邮凭据代理访问指定邮箱。"""
        from tool_support import ToolOperationError

        lanid = os.getenv("OUTLOOK_ADMIN_LANID")
        password = os.getenv("OUTLOOK_ADMIN_PASSWORD")
        server = os.getenv("OUTLOOK_SERVER") or os.getenv("outlook_host")
        email = (target_email or "").strip().lower()

        required = {
            "OUTLOOK_ADMIN_LANID": lanid,
            "OUTLOOK_ADMIN_PASSWORD": password,
            "OUTLOOK_SERVER": server,
        }
        missing = [
            name for name, value in required.items() if not value or not value.strip()
        ]
        if missing:
            raise ToolOperationError("CONFIG_ERROR", "缺少 Exchange 服务账号配置: " + ", ".join(missing))
        if not email or "@" not in email:
            raise ToolOperationError("INVALID_MAILBOX", "缺少或无效的目标邮箱")

        return cls(
            lanid=lanid.strip(),
            email=email,
            password=password,
            server=server.strip(),
            autodiscover=False,
            access_type="delegate",
        )


def data_dir() -> str:
    """附件临时下载存储目录，仅启用下载时需要。"""
    value = os.getenv("EWS_MCP_DATA_DIR", "").strip()
    if not value:
        raise ValueError("缺少 EWS_MCP_DATA_DIR（本机绝对路径）")
    return value


def _flag(name: str) -> bool:
    return os.getenv(name, "false").lower() in ("1", "true", "yes")


def send_enabled() -> bool:
    """默认禁止真实发送和会议通知，部署时明确启用。"""
    return _flag("EWS_MCP_SEND_ENABLED")


def http_timeout() -> int:
    """EWS HTTP 请求超时（秒），默认 300（对齐参考项目）。"""
    try:
        return int(os.getenv("EWS_MCP_HTTP_TIMEOUT", "300"))
    except (TypeError, ValueError):
        return 300


def downloads_enabled() -> bool:
    return _flag("EWS_MCP_DOWNLOAD_ENABLED")


@dataclass(frozen=True)
class DownloadSettings:
    root: str
    enabled: bool = False
    base_url: str = ""
    secret: str = field(default="", repr=False)
    ttl_seconds: int = 600
    retention_seconds: int = 86400
    max_bytes: int = 5 * 1024 * 1024
    max_cache_bytes: int = 1024 * 1024 * 1024
    max_files: int = 1000
    cleanup_seconds: int = 300

    def __post_init__(self):
        if not os.path.isabs(self.root):
            raise ValueError("EWS_MCP_DATA_DIR 必须是绝对路径")
        for name in ("ttl_seconds", "retention_seconds", "max_bytes", "max_cache_bytes", "max_files", "cleanup_seconds"):
            if type(getattr(self, name)) is not int or getattr(self, name) <= 0:
                raise ValueError(f"附件下载配置 {name} 必须是正整数")
        if self.ttl_seconds > self.retention_seconds or self.max_bytes > self.max_cache_bytes:
            raise ValueError("链接有效期不得超过文件保留时间，单文件上限不得超过总缓存上限")
        if self.enabled:
            url = urlsplit(self.base_url)
            if (url.scheme not in ("http", "https") or not url.hostname or url.username is not None
                    or url.password is not None or url.query or url.fragment
                    or any(c.isspace() or ord(c) < 32 for c in self.base_url)):
                raise ValueError("EWS_MCP_PUBLIC_BASE_URL 必须是无凭据、查询参数及片段的 HTTP(S) 地址")
            _ = url.port  # Reject malformed port numbers before issuing links.
            if len(self.secret.encode("utf-8")) < 32:
                raise ValueError("EWS_MCP_DOWNLOAD_SECRET 至少需要 32 字节的随机密钥")

    @classmethod
    def from_env(cls):
        def number(key, default):
            try:
                return int(os.getenv(key, str(default)))
            except ValueError as exc:
                raise ValueError(f"{key} 必须是正整数") from exc
        return cls(
            root=data_dir(), enabled=downloads_enabled(),
            base_url=os.getenv("EWS_MCP_PUBLIC_BASE_URL", "").strip().rstrip("/"),
            secret=os.getenv("EWS_MCP_DOWNLOAD_SECRET", ""),
            ttl_seconds=number("EWS_MCP_DOWNLOAD_TTL_SECONDS", 600),
            retention_seconds=number("EWS_MCP_DOWNLOAD_RETENTION_SECONDS", 86400),
            max_bytes=number("EWS_MCP_DOWNLOAD_MAX_BYTES", 5 * 1024 * 1024),
            max_cache_bytes=number("EWS_MCP_DOWNLOAD_CACHE_MAX_BYTES", 1024 * 1024 * 1024),
            max_files=number("EWS_MCP_DOWNLOAD_CACHE_MAX_FILES", 1000),
            cleanup_seconds=number("EWS_MCP_DOWNLOAD_CLEANUP_SECONDS", 300),
        )
