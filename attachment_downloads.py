"""Bounded mailbox exports and short-lived bearer download links.

Only the authenticated mail tool may create exports. HTTP clients get access
to one immutable cached file via its signed URL; no Exchange credentials or
server paths are accepted from them. The service account must own the data dir.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
import sqlite3
import stat
import time
import uuid
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from urllib.parse import quote, unquote, urlencode

from config import DownloadSettings, downloads_enabled
from tool_support import ToolOperationError

logger = logging.getLogger(__name__)
CHUNK_BYTES = 65536
WRITE_LEASE_SECONDS = 3600


class DownloadAccessLogFilter(logging.Filter):
    """Keep normal uvicorn fields while masking all download query parameters."""
    def filter(self, record):
        def redact(value):
            if isinstance(value, str) and "/downloads/" in unquote(value):
                return re.sub(r"\?[^\s\"']*", "?token=<redacted>", value)
            return value
        record.msg = redact(record.msg)
        if isinstance(record.args, tuple):
            record.args = tuple(redact(value) for value in record.args)
        elif isinstance(record.args, dict):
            record.args = {key: redact(value) for key, value in record.args.items()}
        return True


class DownloadError(Exception):
    def __init__(self, code, status_code):
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def _safe_storage_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, sqlite3.Error) as exc:
            logger.error("attachment_storage_failed error_type=%s", type(exc).__name__)
            # The existing MCP envelope traverses __cause__ and includes raw
            # Exchange messages; do not chain local exceptions containing paths.
            raise ToolOperationError("EXPORT_DIR_UNAVAILABLE", "附件导出存储不可用，请联系管理员。") from None
    return wrapped


def _filename(name):
    name = str(name or "attachment").replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if ord(c) >= 32 and ord(c) != 127).strip()
    return name[:180] if name not in ("", ".", "..") else "attachment"


def _mime(value):
    value = (value or "").split(";", 1)[0].strip().lower()
    return value if re.fullmatch(r"[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+", value) else "application/octet-stream"


class AttachmentDownloadStore:
    @_safe_storage_errors
    def __init__(self, settings: DownloadSettings):
        self.settings = settings
        self.root = Path(settings.root).resolve()
        self.exports = self.root / "exports"
        try:
            self.exports.mkdir(parents=True, exist_ok=True)
            if self.exports.is_symlink() or self.exports.resolve().parent != self.root:
                raise OSError("export directory escaped data directory")
            with self._db() as db:
                db.execute("""CREATE TABLE IF NOT EXISTS attachment_downloads (
                    download_id TEXT PRIMARY KEY, mailbox TEXT NOT NULL,
                    name TEXT NOT NULL, content_type TEXT NOT NULL,
                    relative_path TEXT NOT NULL, state TEXT NOT NULL,
                    size INTEGER NOT NULL, sha256 TEXT,
                    created_at REAL NOT NULL, retained_until REAL NOT NULL
                )""")
        except (OSError, sqlite3.Error):
            raise ToolOperationError("EXPORT_DIR_UNAVAILABLE", "附件导出目录或索引不可用。") from None

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.root / "attachment_downloads.db", timeout=30)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _path(self, relative_path):
        path = self.exports / relative_path
        # Check the actual path, including mailbox directory symlinks/junctions.
        if path.is_symlink() or not path.resolve().is_relative_to(self.exports):
            raise OSError("invalid export path")
        return path

    def _record(self, identifier):
        with self._db() as db:
            row = db.execute("SELECT * FROM attachment_downloads WHERE download_id=?", (identifier,)).fetchone()
        return dict(row) if row else None

    @_safe_storage_errors
    def save(self, mailbox, name, content_type, open_stream, reported_size=None):
        """Reserve quota atomically, stream at most max_bytes+1, then publish."""
        mailbox = (mailbox or "").lower()
        if not re.fullmatch(r"[^/\\:\x00-\x20]+@[^/\\:\x00-\x20]+", mailbox) or len(mailbox) > 254:
            raise ToolOperationError("INVALID_MAILBOX", "无效的附件导出邮箱。")
        limit = self.settings.max_bytes
        if reported_size is not None and reported_size > limit:
            raise ToolOperationError("ATTACHMENT_TOO_LARGE", f"附件超过 {limit} 字节下载上限。")
        self.cleanup()
        identifier = uuid.uuid4().hex
        # Original filename is metadata; only a generated identifier names the disk file.
        relative_path = f"{mailbox}/{identifier}.bin"
        path = self._path(relative_path)
        part = self._path(relative_path + ".part")
        now = time.time()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            count, used = db.execute("SELECT COUNT(*), COALESCE(SUM(size),0) FROM attachment_downloads").fetchone()
            if count >= self.settings.max_files or used + limit > self.settings.max_cache_bytes:
                raise ToolOperationError("DOWNLOAD_CACHE_FULL", "附件缓存配额不足，请稍后重试。")
            db.execute("INSERT INTO attachment_downloads VALUES (?,?,?,?,?,?,?,?,?,?)", (
                identifier, mailbox, _filename(name), _mime(content_type), relative_path,
                "writing", limit, None, now, now + WRITE_LEASE_SECONDS,
            ))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._path(relative_path)
            digest, size, last_lease = hashlib.sha256(), 0, now
            with open_stream() as source, open(part, "xb") as target:
                while True:
                    chunk = source.read(min(CHUNK_BYTES, limit + 1 - size))
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > limit:
                        raise ToolOperationError("ATTACHMENT_TOO_LARGE", f"附件实际内容超过 {limit} 字节下载上限。")
                    target.write(chunk)
                    digest.update(chunk)
                    if time.time() - last_lease >= 30:
                        last_lease = time.time()
                        with self._db() as db:
                            updated = db.execute("UPDATE attachment_downloads SET retained_until=? WHERE download_id=? AND state='writing'",
                                                 (last_lease + WRITE_LEASE_SECONDS, identifier))
                            if updated.rowcount != 1:
                                raise ToolOperationError("DOWNLOAD_UNAVAILABLE", "附件写入预留已失效，请重新下载。")
            os.replace(part, path)
            with self._db() as db:
                updated = db.execute("UPDATE attachment_downloads SET state='ready',size=?,sha256=?,retained_until=? WHERE download_id=? AND state='writing'",
                                     (size, digest.hexdigest(), time.time() + self.settings.retention_seconds, identifier))
                if updated.rowcount != 1:
                    raise ToolOperationError("DOWNLOAD_UNAVAILABLE", "附件写入预留已失效，请重新下载。")
            return {"saved": True, "download_id": identifier, "name": _filename(name),
                    "content_type": _mime(content_type), "saved_path": str(path),
                    "size": size, "sha256": digest.hexdigest()}
        except BaseException:
            # Keep the reserved record if cleanup fails, so it remains accounted
            # for and a later scheduled sweep can remove it.
            removed = True
            for target in (part, path):
                try:
                    target.unlink(missing_ok=True)
                except OSError:
                    removed = False
            if removed:
                with self._db() as db:
                    db.execute("DELETE FROM attachment_downloads WHERE download_id=?", (identifier,))
            raise

    def _signature(self, identifier, expires):
        message = f"ews-attachment:v1:{identifier}:{expires}".encode("ascii")
        return hmac.new(self.settings.secret.encode("utf-8"), message, hashlib.sha256).hexdigest()

    @_safe_storage_errors
    def issue(self, identifier):
        if not self.settings.enabled:
            raise ToolOperationError("DOWNLOAD_DISABLED", "附件链接下载未启用。")
        record = self._record(identifier)
        now = time.time()
        if not record or record["state"] != "ready" or record["retained_until"] <= now:
            raise ToolOperationError("DOWNLOAD_EXPIRED", "附件缓存已失效，请重新准备下载。")
        expires = min(int(now) + self.settings.ttl_seconds, int(record["retained_until"]))
        query = urlencode({"expires": expires, "token": self._signature(identifier, expires)})
        return {
            "download_id": identifier,
            "download_url": f"{self.settings.base_url.rstrip('/')}/downloads/{identifier}?{query}",
            "filename": record["name"], "content_type": record["content_type"],
            "size": record["size"], "sha256": record["sha256"],
            "expires_at": datetime.fromtimestamp(expires, timezone.utc).isoformat(),
        }

    def open_download(self, identifier, expires, token):
        if not self.settings.enabled:
            raise DownloadError("DOWNLOAD_DISABLED", 404)
        if (not re.fullmatch(r"[0-9a-f]{32}", identifier or "")
                or not re.fullmatch(r"[0-9]{1,12}", expires or "")
                or not re.fullmatch(r"[0-9a-f]{64}", token or "")
                or not hmac.compare_digest(token, self._signature(identifier, expires))):
            raise DownloadError("INVALID_DOWNLOAD_TOKEN", 403)
        if int(expires) <= time.time():
            raise DownloadError("DOWNLOAD_EXPIRED", 410)
        record = self._record(identifier)
        if not record or record["state"] != "ready" or record["retained_until"] <= time.time():
            raise DownloadError("DOWNLOAD_EXPIRED", 410)
        stream = None
        try:
            stream = open(self._path(record["relative_path"]), "rb")
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size != record["size"]:
                raise OSError("invalid cached file")
            return record, stream
        except OSError as exc:
            if stream is not None:
                stream.close()
            raise DownloadError("DOWNLOAD_UNAVAILABLE", 410) from exc

    def cleanup(self):
        """Only prune indexed, expired exports; leave legacy files alone."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT download_id,relative_path FROM attachment_downloads WHERE retained_until<=?", (time.time(),)).fetchall()
            for row in rows:
                try:
                    for suffix in (".part", ""):
                        self._path(row["relative_path"] + suffix).unlink(missing_ok=True)
                except OSError:
                    continue
                db.execute("DELETE FROM attachment_downloads WHERE download_id=?", (row["download_id"],))


def _chunks(stream):
    try:
        while chunk := stream.read(CHUNK_BYTES):
            yield chunk
    finally:
        stream.close()


def download_tool_result(result):
    """Expose the metadata to clients that only forward MCP TextContent."""
    from fastmcp.tools.tool import ToolResult
    payload = {"results": result}
    return ToolResult(content=json.dumps(payload, ensure_ascii=False), structured_content=payload)


def register_download_route(mcp):
    from starlette.background import BackgroundTask
    from starlette.responses import JSONResponse, StreamingResponse

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, DownloadAccessLogFilter) for f in access_logger.filters):
        access_logger.addFilter(DownloadAccessLogFilter())

    class DownloadResponse(StreamingResponse):
        async def __call__(self, scope, receive, send):
            try:
                await super().__call__(scope, receive, send)
            finally:
                self.cached_stream.close()

    @mcp.custom_route("/downloads/{identifier}", methods=["GET"], include_in_schema=False)
    async def download_attachment(request):
        headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
        if not downloads_enabled():
            return JSONResponse({"error_code": "DOWNLOAD_DISABLED"}, status_code=404, headers=headers)
        try:
            def load():
                store = AttachmentDownloadStore(DownloadSettings.from_env())
                return store.open_download(request.path_params["identifier"],
                                           request.query_params.get("expires"), request.query_params.get("token"))
            worker = asyncio.create_task(asyncio.to_thread(load))
            try:
                record, stream = await asyncio.shield(worker)
            except asyncio.CancelledError:
                def close_abandoned_result(task):
                    with suppress(Exception, asyncio.CancelledError):
                        _, abandoned_stream = task.result()
                        abandoned_stream.close()
                worker.add_done_callback(close_abandoned_result)
                raise
        except DownloadError as exc:
            return JSONResponse({"error_code": exc.code}, status_code=exc.status_code, headers=headers)
        except Exception:
            logger.error("attachment_download_failed")  # Never log bearer URLs or disk paths.
            return JSONResponse({"error_code": "DOWNLOAD_UNAVAILABLE"}, status_code=503, headers=headers)
        try:
            headers.update({
                "Content-Disposition": "attachment; filename=\"attachment\"; filename*=UTF-8''" + quote(record["name"], safe=""),
                "Content-Length": str(record["size"]),
                "Content-Type": record["content_type"],  # Raw bytes: never invent UTF-8 encoding.
            })
            response = DownloadResponse(_chunks(stream), media_type=record["content_type"],
                                        headers=headers, background=BackgroundTask(stream.close))
            response.cached_stream = stream
            return response
        except BaseException:
            stream.close()
            raise


@asynccontextmanager
async def download_lifespan(mcp):
    """Run cleanup even when nobody requests another export."""
    task = None
    configured_root = os.getenv("EWS_MCP_DATA_DIR", "").strip()
    has_cache = bool(configured_root) and (Path(configured_root) / "attachment_downloads.db").exists()
    if downloads_enabled() or has_cache:
        settings = DownloadSettings.from_env()
        store = await asyncio.to_thread(AttachmentDownloadStore, settings)
        await asyncio.to_thread(store.cleanup)
        async def sweep():
            while True:
                await asyncio.sleep(settings.cleanup_seconds)
                try:
                    await asyncio.to_thread(store.cleanup)
                except Exception:
                    logger.error("attachment_download_cleanup_failed")
        task = asyncio.create_task(sweep())
    try:
        yield {}
    finally:
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
