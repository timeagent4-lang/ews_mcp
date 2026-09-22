import os
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit


@dataclass
class OutlookConfig:
    """Exchange 连接配置。凭据来自服务器，邮箱来自 OA 校验后的可信身份。"""

    lanid: Optional[str] = None
    email: Optional[str] = None
    password: Optional[str] = None
    server: Optional[str] = None
    autodiscover: bool = False
    access_type: str = "delegate"

    @classmethod
    def from_service_env(cls, target_email: str) -> "OutlookConfig":
        """使用固定服务凭据连接当前请求者的邮箱。"""

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
            raise ValueError("缺少 Exchange 服务账号配置: " + ", ".join(missing))
        if not email or "@" not in email:
            raise ValueError("缺少或无效的目标邮箱")

        return cls(
            lanid=lanid.strip(),
            email=email,
            password=password,
            server=server.strip(),
            autodiscover=False,
            access_type="delegate",
        )


def data_dir() -> str:
    """本机绝对路径，服务账号独占；用于镜像与确认数据库。"""
    value = os.getenv("EWS_MCP_DATA_DIR", "").strip()
    if not value:
        raise ValueError("缺少 EWS_MCP_DATA_DIR（本机绝对路径）")
    return value


def _flag(name: str) -> bool:
    return os.getenv(name, "false").lower() in ("1", "true", "yes")


def send_enabled() -> bool:
    """默认禁止真实发送/会议通知/OOF 执行，仍允许预览。"""
    return _flag("EWS_MCP_SEND_ENABLED")


def cache_enabled() -> bool:
    """默认不镜像正文；启用后仅同步明确列出的文件夹。"""
    return _flag("EWS_MCP_CACHE_ENABLED")


def operations_db_path() -> str:
    return os.path.join(data_dir(), "operations.db")


def http_timeout() -> int:
    """EWS HTTP 请求超时（秒），默认 300（对齐参考项目）。"""
    try:
        return int(os.getenv("EWS_MCP_HTTP_TIMEOUT", "300"))
    except (TypeError, ValueError):
        return 300


def preview_ttl_seconds() -> int:
    """未执行预览的有效期（秒），默认 1800。<=0 表示预览不过期。"""
    try:
        return int(os.getenv("EWS_MCP_PREVIEW_TTL_SECONDS", "1800"))
    except (TypeError, ValueError):
        return 1800


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
