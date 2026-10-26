"""Immutable uploads with operation references in the existing SQLite store."""

import hashlib
import asyncio
import hmac
import logging
import os
import re
import sqlite3
import stat
import time
import threading
import uuid
import secrets
from dataclasses import dataclass
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path

from tool_support import ToolOperationError

MAX_RECEIVERS = 4
MAX_ATTACHMENTS_IN_FLIGHT = 2
RECEIVE_SECONDS = 120
IDLE_SECONDS = 30
CHUNK_BYTES = 65536
logger = logging.getLogger(__name__)


def get_upload_store():
    from config import UploadSettings
    from confirmation import get_store
    try:
        settings = UploadSettings.from_env()
    except ValueError:
        raise ToolOperationError('UPLOAD_CONFIG_INVALID', '上传配置无效，请联系管理员。') from None
    return AttachmentUploadStore(settings, get_store())


class UploadHTTPError(Exception):
    def __init__(self, code, status_code):
        self.code, self.status_code = code, status_code
        super().__init__(code)


def _receive_in_time(deadline, cancelled):
    if ((cancelled is not None and cancelled.is_set())
            or (deadline is not None and time.monotonic() >= deadline)):
        raise TimeoutError


async def _storage_work(fn, *, on_cancel=None):
    """Cancellation must not leave a file/SQLite worker running unobserved."""
    work = asyncio.create_task(asyncio.to_thread(fn))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:
        if on_cancel is not None:
            on_cancel()
        while not work.done():
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        with suppress(BaseException):
            work.result()
        raise


def _storage_errors(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except (OSError, sqlite3.Error):
            raise ToolOperationError('UPLOAD_STORAGE_UNAVAILABLE', '附件上传存储不可用，请联系管理员。') from None
    return wrapped


@dataclass(frozen=True)
class ReceiveHandle:
    upload_id: str
    worker_id: str
    boot_id: str
    path: Path
    part_path: Path
    size: int
    sha256: str


@dataclass(frozen=True)
class DeleteHandle:
    upload_id: str
    worker_id: str
    path: Path
    part_path: Path


@dataclass(frozen=True)
class VerifiedUpload:
    metadata: dict
    content: bytes


def _metadata(row):
    return {key: row[key] for key in ('upload_id', 'lanid', 'mailbox', 'filename', 'content_type', 'size', 'sha256')}


class AttachmentUploadStore:
    @_storage_errors
    def __init__(self, settings, operations):
        self.settings, self.operations = settings, operations
        self.root = Path(settings.root).resolve()
        self.directory = self.root / 'uploads'
        self.directory.mkdir(parents=True, exist_ok=True)
        if self.directory.is_symlink() or self.directory.resolve().parent != self.root:
            raise OSError('invalid upload directory')

    def _path(self, identifier, suffix='.bin'):
        if not re.fullmatch('[0-9a-f]{32}', identifier or ''):
            raise ToolOperationError('UPLOAD_NOT_FOUND', '上传文件不存在或不可用。')
        path = self.directory / (identifier + suffix)
        if path.is_symlink() or path.resolve().parent != self.directory.resolve():
            raise OSError('invalid upload path')
        return path

    def _enabled(self):
        if not self.settings.enabled:
            raise ToolOperationError('UPLOAD_DISABLED', '附件上传未启用。')

    @_storage_errors
    def prepare(self, *, lanid, mailbox, filename, size, sha256, content_type=None):
        self._enabled()
        if (not isinstance(lanid, str) or not lanid.strip() or not isinstance(mailbox, str)
                or not re.fullmatch(r'[^/\\:\x00-\x20]+@[^/\\:\x00-\x20]+', mailbox)):
            raise ToolOperationError('INVALID_MAILBOX', '上传身份无效。')
        if type(size) is not int or not 0 <= size <= self.settings.max_bytes:
            raise ToolOperationError('ATTACHMENT_TOO_LARGE', '文件大小无效或超过上传上限。')
        if not isinstance(sha256, str) or not re.fullmatch('[0-9a-fA-F]{64}', sha256):
            raise ToolOperationError('INVALID_UPLOAD_HASH', 'sha256 必须为64位十六进制。')
        if not isinstance(filename, str) or not filename.strip():
            raise ToolOperationError('INVALID_FILENAME', '附件文件名不能为空。')
        filename = filename.replace('\\', '/').rsplit('/', 1)[-1]
        filename = ''.join(c for c in filename if ord(c) >= 32 and ord(c) != 127).strip()[:180]
        if filename in ('', '.', '..'):
            raise ToolOperationError('INVALID_FILENAME', '附件文件名无效。')
        mime = (content_type or 'application/octet-stream').split(';', 1)[0].strip().lower()
        if not re.fullmatch(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+', mime):
            raise ToolOperationError('INVALID_CONTENT_TYPE', '附件类型无效。')
        identifier, token, now = uuid.uuid4().hex, secrets.token_urlsafe(32), time.time()
        expires = now + self.settings.ttl_seconds
        with self.operations.transaction() as db:
            count, used = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM attachment_uploads').fetchone()
            if count >= self.settings.max_files or used + size > self.settings.max_cache_bytes:
                raise ToolOperationError('UPLOAD_CACHE_FULL', '上传缓存配额不足，请稍后重试。')
            db.execute('INSERT INTO attachment_uploads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (identifier, lanid.strip().lower(), mailbox.strip().lower(), filename, mime, size, sha256.lower(),
                        hashlib.sha256(token.encode()).hexdigest(), expires, expires, 'prepared', None, None, None))
        return {'upload_id': identifier, 'upload_url': self.settings.base_url.rstrip('/') + '/uploads/' + identifier,
                'upload_token': token, 'upload_expires_at': datetime.fromtimestamp(expires, timezone.utc).isoformat(),
                'filename': filename, 'size': size, 'sha256': sha256.lower(), 'content_type': mime,
                'limits': {'max_bytes': self.settings.max_bytes, 'draft_max_bytes': self.settings.draft_max_bytes,
                           'draft_max_attachments': self.settings.draft_max_attachments}}

    @_storage_errors
    def begin_receive(self, upload_id, token, content_length, *, worker_id):
        self._enabled()
        with self.operations.transaction() as db:
            row = db.execute('SELECT * FROM attachment_uploads WHERE upload_id=?', (upload_id,)).fetchone()
            if (not row or not isinstance(token, str) or len(token) > 256
                    or not hmac.compare_digest(row['token_hash'], hashlib.sha256(token.encode()).hexdigest())):
                raise UploadHTTPError('INVALID_UPLOAD_TOKEN', 403)
            if row['token_expires'] <= time.time():
                raise UploadHTTPError('UPLOAD_EXPIRED', 410)
            if row['state'] != 'prepared':
                raise UploadHTTPError('UPLOAD_CONFLICT', 409)
            if type(content_length) is not int or content_length < 0:
                raise UploadHTTPError('CONTENT_LENGTH_REQUIRED', 411)
            if content_length > self.settings.max_bytes or content_length > row['size']:
                raise UploadHTTPError('ATTACHMENT_TOO_LARGE', 413)
            if content_length != row['size']:
                raise UploadHTTPError('UPLOAD_LENGTH_MISMATCH', 400)
            count = db.execute("SELECT COUNT(*) FROM attachment_uploads WHERE state='receiving'").fetchone()[0]
            if count >= MAX_RECEIVERS:
                raise UploadHTTPError('UPLOAD_BUSY', 429)
            db.execute("UPDATE attachment_uploads SET state='receiving',worker_id=?,boot_id=?,retained_until=? WHERE upload_id=?",
                       (worker_id, self.operations.boot_id, time.time() + RECEIVE_SECONDS, upload_id))
            return ReceiveHandle(upload_id, worker_id, self.operations.boot_id,
                                 self._path(upload_id), self._path(upload_id, '.part'), row['size'], row['sha256'])

    def _receiver(self, db, handle):
        row = db.execute('SELECT * FROM attachment_uploads WHERE upload_id=?', (handle.upload_id,)).fetchone()
        if (not row or row['state'] != 'receiving' or row['worker_id'] != handle.worker_id
                or row['boot_id'] != handle.boot_id or handle.boot_id != self.operations.boot_id):
            raise ToolOperationError('UPLOAD_UNAVAILABLE', '上传接收占用已失效。')
        return row

    @_storage_errors
    def renew_receive(self, handle, *, now=None):
        with self.operations.transaction() as db:
            self._receiver(db, handle)
            db.execute('UPDATE attachment_uploads SET retained_until=? WHERE upload_id=?',
                       ((time.time() if now is None else now) + RECEIVE_SECONDS, handle.upload_id))

    @_storage_errors
    def publish_ready(self, handle, *, size, sha256, deadline=None, cancelled=None):
        with self.operations.transaction() as db:
            row = self._receiver(db, handle)
            if size != row['size'] or sha256 != row['sha256']:
                raise ToolOperationError('UPLOAD_CHANGED', '上传实际大小或摘要不匹配。')
            info = self._path(handle.upload_id).stat()
            if not stat.S_ISREG(info.st_mode) or info.st_size != size:
                raise ToolOperationError('UPLOAD_CHANGED', '上传文件不完整。')
            _receive_in_time(deadline, cancelled)
            db.execute("UPDATE attachment_uploads SET state='ready',retained_until=?,worker_id=NULL WHERE upload_id=?",
                       (time.time() + self.settings.retention_seconds, handle.upload_id))
            _receive_in_time(deadline, cancelled)
        return {'status': 'ready', 'upload_id': handle.upload_id, 'size': size, 'sha256': sha256}

    @_storage_errors
    def fail_receive(self, handle):
        with self.operations.transaction() as db:
            db.execute("UPDATE attachment_uploads SET state='failed',retained_until=0 WHERE upload_id=? "
                       "AND state='receiving' AND worker_id=? AND boot_id=?",
                       (handle.upload_id, handle.worker_id, handle.boot_id))

    @_storage_errors
    def open_verified(self, upload_id, *, lanid, mailbox, operation_id=None, expected=None):
        self._enabled()
        reader = 'reader-' + uuid.uuid4().hex
        with self.operations.transaction() as db:
            row = db.execute('SELECT * FROM attachment_uploads WHERE upload_id=?', (upload_id,)).fetchone()
            if (not row or row['state'] != 'ready' or row['lanid'] != lanid or row['mailbox'] != mailbox):
                raise ToolOperationError('UPLOAD_UNAVAILABLE', '上传文件不可用或不属于当前员工。')
            ref = db.execute('SELECT * FROM operation_upload_refs WHERE upload_id=?', (upload_id,)).fetchone()
            if operation_id:
                if not ref or ref['operation_id'] != operation_id or ref['kind'] != 'execution':
                    raise ToolOperationError('UPLOAD_UNAVAILABLE', '附件执行占用已失效。')
            elif row['retained_until'] <= time.time() or row['consumed_by_operation_id']:
                raise ToolOperationError('UPLOAD_UNAVAILABLE', '上传已过期或已占用。')
            metadata = _metadata(row)
            if expected and any(expected.get(key) != metadata[key] for key in metadata):
                raise ToolOperationError('UPLOAD_CHANGED', '封存附件元数据与预览不一致。')
            db.execute('INSERT INTO upload_readers VALUES (?,?,?)', (reader, upload_id, self.operations.boot_id))
        try:
            with open(self._path(upload_id), 'rb') as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size != row['size']:
                    raise ToolOperationError('UPLOAD_CHANGED', '封存附件大小已变化。')
                data = source.read(self.settings.max_bytes + 1)
            if len(data) != row['size'] or hashlib.sha256(data).hexdigest() != row['sha256']:
                raise ToolOperationError('UPLOAD_CHANGED', '封存附件内容已变化。')
            return VerifiedUpload(metadata, data)
        finally:
            with self.operations.transaction() as db:
                db.execute('DELETE FROM upload_readers WHERE reader_id=?', (reader,))

    @_storage_errors
    def claim_cleanup(self, *, now=None, limit=100):
        now = time.time() if now is None else now
        with self.operations.transaction() as db:
            rows = db.execute("SELECT * FROM attachment_uploads AS u WHERE u.state NOT IN ('receiving','quarantine') "
                "AND (u.retained_until<=? OR u.state='deleting') "
                "AND NOT EXISTS (SELECT 1 FROM operation_upload_refs r WHERE r.upload_id=u.upload_id "
                "AND (r.kind!='preview' OR r.expires_at>?)) "
                "AND NOT EXISTS (SELECT 1 FROM upload_readers v WHERE v.upload_id=u.upload_id) LIMIT ?", (now, now, limit)).fetchall()
            result = []
            for row in rows:
                owner = uuid.uuid4().hex
                db.execute("UPDATE attachment_uploads SET state='deleting',worker_id=? WHERE upload_id=?", (owner, row['upload_id']))
                result.append(DeleteHandle(row['upload_id'], owner, self._path(row['upload_id']), self._path(row['upload_id'], '.part')))
            return result

    @_storage_errors
    def finish_cleanup(self, handle):
        if handle.path.exists() or handle.part_path.exists():
            raise ToolOperationError('UPLOAD_STORAGE_UNAVAILABLE', '文件尚未删除，不释放缓存配额。')
        with self.operations.transaction() as db:
            removed = db.execute("DELETE FROM attachment_uploads WHERE upload_id=? AND state='deleting' AND worker_id=?",
                                 (handle.upload_id, handle.worker_id))
            if removed.rowcount:
                db.execute('DELETE FROM operation_upload_refs WHERE upload_id=?', (handle.upload_id,))

    def cleanup(self):
        for handle in self.claim_cleanup():
            try:
                handle.part_path.unlink(missing_ok=True)
                handle.path.unlink(missing_ok=True)
                self.finish_cleanup(handle)
            except (OSError, ToolOperationError):
                continue


class _ReceiveSession:
    """Sequential thread work; the receiving row pins files until abort drains."""
    def __init__(self, deadline):
        self.deadline = deadline
        self.cancelled = threading.Event()
        self.store = self.handle = self.target = None
        self.size = 0
        self.digest = hashlib.sha256()

    def start(self, identifier, token, length):
        self.store = get_upload_store()
        self.handle = self.store.begin_receive(identifier, token, length, worker_id=uuid.uuid4().hex)
        _receive_in_time(self.deadline, self.cancelled)
        self.target = open(self.handle.part_path, 'xb')

    def write(self, chunk):
        _receive_in_time(self.deadline, self.cancelled)
        self.size += len(chunk)
        if self.size > self.handle.size or self.size > self.store.settings.max_bytes:
            raise UploadHTTPError('ATTACHMENT_TOO_LARGE', 413)
        for offset in range(0, len(chunk), CHUNK_BYTES):
            self.target.write(chunk[offset:offset + CHUNK_BYTES])
        self.digest.update(chunk)
        self.store.renew_receive(self.handle)

    def finish(self):
        _receive_in_time(self.deadline, self.cancelled)
        if self.size != self.handle.size:
            raise UploadHTTPError('UPLOAD_LENGTH_MISMATCH', 400)
        if self.digest.hexdigest() != self.handle.sha256:
            raise UploadHTTPError('UPLOAD_HASH_MISMATCH', 422)
        self.target.flush()
        os.fsync(self.target.fileno())
        self.target.close()
        self.target = None
        _receive_in_time(self.deadline, self.cancelled)
        os.replace(self.handle.part_path, self.handle.path)
        return self.store.publish_ready(self.handle, size=self.size, sha256=self.digest.hexdigest(),
                                        deadline=self.deadline, cancelled=self.cancelled)

    def abort(self):
        if self.target is not None:
            with suppress(OSError):
                self.target.close()
            self.target = None
        if self.handle is not None:
            self.store.fail_receive(self.handle)
            self.store.cleanup()


def register_upload_route(mcp):
    from config import uploads_enabled
    from starlette.responses import JSONResponse

    @mcp.custom_route('/uploads/{identifier}', methods=['PUT'], include_in_schema=False)
    async def upload_attachment(request):
        headers = {'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
        identifier = request.path_params['identifier']
        status, code = 503, 'UPLOAD_UNAVAILABLE'
        started = time.monotonic()
        receiver = _ReceiveSession(started + RECEIVE_SECONDS)
        try:
            if not uploads_enabled():
                raise UploadHTTPError('UPLOAD_DISABLED', 404)
            authorizations = request.headers.getlist('authorization')
            if (len(authorizations) != 1 or not re.fullmatch(r'Bearer [A-Za-z0-9_-]{43}', authorizations[0])):
                raise UploadHTTPError('INVALID_UPLOAD_TOKEN', 403)
            lengths = request.headers.getlist('content-length')
            if len(lengths) > 1 or (lengths and not re.fullmatch(r'[0-9]{1,12}', lengths[0])):
                raise UploadHTTPError('UPLOAD_LENGTH_MISMATCH', 400)
            async with asyncio.timeout_at(receiver.deadline):
                await _storage_work(lambda: receiver.start(identifier, authorizations[0][7:],
                                    int(lengths[0]) if lengths else None), on_cancel=receiver.cancelled.set)
                stream = request.stream().__aiter__()
                while True:
                    try:
                        chunk = await asyncio.wait_for(anext(stream), IDLE_SECONDS)
                    except StopAsyncIteration:
                        break
                    await _storage_work(lambda: receiver.write(chunk), on_cancel=receiver.cancelled.set)
                result = await _storage_work(receiver.finish, on_cancel=receiver.cancelled.set)
            status, code = 200, None
            return JSONResponse(result, headers=headers)
        except UploadHTTPError as exc:
            status, code = exc.status_code, exc.code
        except TimeoutError:
            status, code = 408, 'UPLOAD_TIMEOUT'
        except (OSError, ToolOperationError):
            status, code = 503, 'UPLOAD_STORAGE_UNAVAILABLE'
        except asyncio.CancelledError:
            status, code = 400, 'UPLOAD_INTERRUPTED'
            raise
        except Exception:
            status, code = 400, 'UPLOAD_INTERRUPTED'
        finally:
            if status != 200:
                receiver.cancelled.set()
                with suppress(Exception):
                    await _storage_work(receiver.abort)
            logger.info('attachment_upload_http upload_id=%s status=%s error_code=%s bytes=%s duration_ms=%s',
                        identifier if re.fullmatch('[0-9a-f]{32}', identifier) else '<invalid>',
                        status, code, receiver.size, round((time.monotonic() - started) * 1000))
        return JSONResponse({'error_code': code}, status_code=status, headers=headers)


@asynccontextmanager
async def attachment_lifespan(mcp):
    from attachment_downloads import download_lifespan
    from config import uploads_enabled
    task = None
    async with download_lifespan(mcp) as state:
        configured = os.getenv('EWS_MCP_DATA_DIR', '').strip()
        if uploads_enabled() or (configured and (Path(configured) / 'uploads').exists()):
            store = await _storage_work(get_upload_store)
            await _storage_work(store.cleanup)
            async def sweep():
                while True:
                    await asyncio.sleep(store.settings.cleanup_seconds)
                    try:
                        await _storage_work(store.cleanup)
                    except Exception:
                        logger.error('attachment_upload_cleanup_failed')
            task = asyncio.create_task(sweep())
        try:
            yield state
        finally:
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
