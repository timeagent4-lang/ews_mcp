"""Safe JSONL logs, coordinated daily files and verified background gzip history."""

from datetime import date, datetime, timedelta, timezone
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sys
import tempfile
import threading
import uuid


_FAILED = frozenset(('failure', 'rejected', 'error'))
_LOG_NAME = re.compile(r'(?:errors|audit)-(\d{4}-\d{2}-\d{2})\.log')
_CHUNK = 1024 * 1024


def _json_line(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False).translate(
        {0x85: '\\u0085', 0x2028: '\\u2028', 0x2029: '\\u2029'})


def _stderr_health(stream, event_type, **fields):
    """Never render the failed LogRecord or exception text on the emergency path."""
    try:
        stream.write(_json_line({'event_type': event_type, **fields}) + '\n')
        stream.flush()
    except Exception:
        pass


def _safe_event(record):
    event = getattr(record, '_ews_safe_event', None)
    if event is None:
        try:
            from utils.audit import event_from_record
            event = event_from_record(record)
        except Exception as exc:
            _stderr_health(sys.stderr, 'logging.serialization_failed', error_type=type(exc).__name__)
            event = {'schema_version': 2, 'event_id': str(uuid.uuid4()),
                     'timestamp': datetime.now(timezone.utc).isoformat(),
                     'event_type': 'logging.serialization_failed', 'is_error': True,
                     'error_type': type(exc).__name__}
        record._ews_safe_event = event
        event['is_error'] = (bool(event.get('is_error')) or record.levelno >= logging.ERROR
                             or event.get('mcp_is_error') is True
                             or getattr(record, 'audit_status', None) in _FAILED)
    return event


def _is_error(record):
    event = _safe_event(record)
    return (record.levelno >= logging.ERROR or bool(event.get('is_error'))
            or event.get('mcp_is_error') is True or event.get('call_status') == 'error'
            or getattr(record, 'audit_status', None) in _FAILED)


def _sync_directory(path):
    # Windows cannot open directory handles with os.open. POSIX errors must
    # propagate: never remove a source after an undurable archive publication.
    if os.name != 'nt':
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_DIRECTORY', 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def _directory(path):
    """Only chmod newly created directories, retaining inherited setgid."""
    path = Path(path)
    try:
        path.mkdir(mode=0o750)
    except FileNotFoundError:
        _directory(path.parent)
        return _directory(path)
    except FileExistsError:
        if path.is_symlink() or not path.is_dir():
            raise NotADirectoryError(str(path))
        return
    inherited_mode = path.stat().st_mode
    if inherited_mode & 0o777 != 0o750:
        os.chmod(path, (inherited_mode & 0o7000) | 0o750)
        if inherited_mode & 0o2000 and not path.stat().st_mode & 0o2000:
            # Linux may clear setgid on chmod when this UID is not a member of
            # the inherited group. Do not silently create a broken archive tree.
            path.rmdir()
            _sync_directory(path.parent)
            raise PermissionError('cannot retain inherited shared group permissions')
    _sync_directory(path.parent)


def _file_opener(path, flags):
    flags |= getattr(os, 'O_NOFOLLOW', 0)
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, 0o640)
    except FileExistsError:
        return os.open(path, flags & ~os.O_CREAT)
    try:
        if hasattr(os, 'fchmod'):
            os.fchmod(fd, 0o640)
        else:
            os.chmod(path, 0o640)
    except Exception:
        os.close(fd)
        raise
    return fd


def _fingerprint(path):
    stat = path.stat(follow_symlinks=False)
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _digest(stream):
    digest, size = hashlib.sha256(), 0
    while chunk := stream.read(_CHUNK):
        digest.update(chunk)
        size += len(chunk)
    return digest.digest(), size


class _ArchiveCoordinator:
    def __init__(self, directory):
        self.directory = Path(directory)
        _directory(self.directory)
        self.lock = threading.RLock()
        self.handlers = []
        self.stderr = sys.stderr
        self.recovered = []
        self._stop = threading.Event()
        self._requested = threading.Event()
        self._condition = threading.Condition()
        self._generation = self._completed = 0
        self._worker = None

    def start(self):
        self._worker = threading.Thread(target=self._run, name='ews-log-archive', daemon=True)
        self._worker.start()
        self.request_archive()

    def request_archive(self):
        with self._condition:
            self._generation += 1
        self._requested.set()

    def wait_for_archive(self, timeout=None):
        """Drain requested maintenance, including orderly service shutdown."""
        with self._condition:
            target = self._generation
            return self._condition.wait_for(lambda: self._completed >= target, timeout)

    def shutdown(self, wait=True):
        self._stop.set()
        self._requested.set()
        if wait and self._worker is not None and self._worker is not threading.current_thread():
            self._worker.join()

    def active_paths(self):
        return {Path(h.baseFilename) for h in self.handlers if h.stream is not None}

    def rotate(self, today):
        # All handlers use this RLock; there is no handler-to-handler lock order.
        changing = [h for h in self.handlers if h.day != today and not h._closed]
        if not changing:
            return
        replacements = []
        try:
            for handler in changing:
                old_name = handler.baseFilename
                handler.baseFilename = str(self.directory / f'{handler.kind}-{today.isoformat()}.log')
                try:
                    stream = handler._open()
                    replacements.append((handler, handler.baseFilename, stream))
                finally:
                    handler.baseFilename = old_name
        except Exception:
            for _, _, stream in replacements:
                stream.close()
            raise
        for handler, name, stream in replacements:
            old_stream = handler.stream
            handler.baseFilename, handler.stream, handler.day = name, stream, today
            if old_stream is not None:
                old_stream.close()
        self.request_archive()

    def health(self, event_type, **fields):
        if fields.get('is_error'):
            _stderr_health(self.stderr, event_type, **fields)
        try:
            from utils.audit import emit_event
            emit_event(event_type, logger=logging.getLogger('mail_mcp.audit'), **fields)
        except Exception as exc:
            _stderr_health(self.stderr, event_type, error_type=type(exc).__name__)

    def _run(self):
        while True:
            self._requested.wait()
            self._requested.clear()
            if self._stop.is_set():
                return
            with self._condition:
                generation = self._generation
            try:
                cutoff = date.today() - timedelta(days=29)
                for source in sorted(self.directory.glob('*.log')):
                    if self._stop.is_set():
                        break
                    match = _LOG_NAME.fullmatch(source.name)
                    if not match or source.is_symlink() or not source.is_file():
                        continue
                    try:
                        stamp = date.fromisoformat(match[1])
                    except ValueError:
                        continue
                    if stamp >= cutoff:
                        continue
                    try:
                        self._archive_one(source, stamp)
                    except Exception as exc:
                        self.health('archive.failed', is_error=True, file=source.name,
                                    error_type=type(exc).__name__)
            except Exception as exc:
                self.health('archive.failed', is_error=True, error_type=type(exc).__name__)
            finally:
                with self._condition:
                    self._completed = max(self._completed, generation)
                    self._condition.notify_all()

    def _archive_one(self, source, stamp):
        with self.lock:
            if source in self.active_paths() or source.is_symlink():
                return
            before = _fingerprint(source)
        archive_dir = self.directory / 'archive'
        _directory(archive_dir)
        target_dir = archive_dir / stamp.strftime('%Y-%m')
        _directory(target_dir)
        target = target_dir / (source.name + '.gz')
        temporary = None
        try:
            if target.exists() or target.is_symlink():
                if target.is_symlink() or not target.is_file():
                    self.health('archive.conflict', is_error=True, file=source.name)
                    return
                try:
                    with target.open('r+b' if os.name == 'nt' else 'rb') as compressed:
                        with source.open('rb') as raw, gzip.GzipFile(fileobj=compressed, mode='rb') as archived:
                            matches = _digest(raw) == _digest(archived)
                        if matches:
                            os.fsync(compressed.fileno())
                except (OSError, EOFError):
                    matches = False
                if not matches:
                    self.health('archive.conflict', is_error=True, file=source.name)
                    return
            else:
                fd, name = tempfile.mkstemp(prefix='.' + source.name + '.', suffix='.tmp', dir=target_dir)
                temporary = Path(name)
                digest, size = hashlib.sha256(), 0
                with os.fdopen(fd, 'wb') as output:
                    if hasattr(os, 'fchmod'):
                        os.fchmod(output.fileno(), 0o640)
                    else:
                        os.chmod(temporary, 0o640)
                    with source.open('rb') as raw, gzip.GzipFile(filename='', mode='wb', fileobj=output, mtime=0) as zipped:
                        while chunk := raw.read(_CHUNK):
                            if self._stop.is_set():
                                return
                            zipped.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                with gzip.open(temporary, 'rb') as archived:
                    if _digest(archived) != (digest.digest(), size):
                        raise OSError('archive verification failed')
            with self.lock:
                if (self._stop.is_set() or source in self.active_paths()
                        or source.is_symlink() or _fingerprint(source) != before):
                    return
                if temporary is not None:
                    # Atomic and refuses to overwrite existing historical gzip.
                    os.link(temporary, target)
                    temporary.unlink()
                    temporary = None
                _sync_directory(target_dir)
                source.unlink()
                _sync_directory(self.directory)
            self.health('archive.completed', file=source.name, archive=str(target.relative_to(self.directory)))
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class DailyFileHandler(logging.FileHandler):
    """Append by local date and coordinate both file kinds before archival."""
    def __init__(self, directory, kind, coordinator=None):
        self.directory, self.kind = Path(directory), kind
        self._coordinator = coordinator or _ArchiveCoordinator(self.directory)
        self.day = date.today()
        self._write_failures = 0
        super().__init__(self.directory / f'{kind}-{self.day.isoformat()}.log',
                         encoding='utf-8', errors='backslashreplace')
        self.lock = self._coordinator.lock
        self._coordinator.handlers.append(self)

    def _open(self):
        path = Path(self.baseFilename)
        if path.is_symlink():
            raise OSError('active log must not be a symlink')
        if path.exists() and path.stat().st_size:
            with path.open('r+b') as stream:
                stream.seek(-1, os.SEEK_END)
                if stream.read(1) != b'\n':
                    stream.seek(0, os.SEEK_END)
                    original_size = stream.tell()
                    stream.write(b'\n')
                    stream.flush()
                    os.fsync(stream.fileno())
                    self._coordinator.recovered.append({'file': path.name, 'original_bytes': original_size,
                                                       'reason': 'unterminated_previous_line'})
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                    opener=_file_opener)

    def emit(self, record):
        if self._closed:
            return
        try:
            self._coordinator.rotate(date.today())
            if self.stream is None:
                self.stream = self._open()
            self.stream.write(self.format(record) + '\n')
            self.stream.flush()
            failures, self._write_failures = self._write_failures, 0
            recovered, self._coordinator.recovered = self._coordinator.recovered, []
            if failures:
                recovered.append({'file': Path(self.baseFilename).name, 'missed_records': failures,
                                  'reason': 'writes_resumed'})
            for fields in recovered:
                self._coordinator.health('logging.recovered', **fields)
        except Exception as exc:
            self._write_failures += 1
            if self.stream is not None:
                try:
                    self.stream.close()
                except Exception:
                    pass
                self.stream = None
            _stderr_health(self._coordinator.stderr, 'logging.write_failed', kind=self.kind,
                           missed_records=self._write_failures, error_type=type(exc).__name__)

    def close(self):
        coordinator = getattr(self, '_coordinator', None)
        super().close()
        if coordinator is not None:
            with coordinator.lock:
                if self in coordinator.handlers:
                    coordinator.handlers.remove(self)
                last = not coordinator.handlers
            if last:
                # logging.shutdown calls close while holding the handler lock;
                # the worker may itself be waiting to commit under that lock.
                coordinator.shutdown(wait=False)


class _OutputFilter(logging.Filter):
    def __init__(self, errors_only=False):
        super().__init__()
        self.errors_only = errors_only

    def filter(self, record):
        return _is_error(record) if self.errors_only else record.levelno >= logging.INFO


class _Formatter(logging.Formatter):
    def __init__(self, console=False):
        super().__init__()
        self.console = console

    def format(self, record):
        event = _safe_event(record)
        if self.console:
            names = ('timestamp', 'event_type', 'event_id', 'call_id', 'operation_id', 'origin_call_id',
                     'tool', 'stage', 'call_status', 'status', 'is_error', 'error_code', 'duration_ms',
                     'message', 'logger', 'file', 'error_type', 'reason', 'missed_records', 'log_dir', 'uid', 'gid')
            names += ('level', 'warning_category', 'host')
            event = {name: event[name] for name in names if name in event}
        return _json_line(event)


class _ConsoleHandler(logging.StreamHandler):
    def handleError(self, record):
        _stderr_health(sys.__stderr__, 'logging.console_failed')


class _NoiseFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.seen_tls = set()
        self.lock = threading.Lock()

    def filter(self, record):
        if record.levelno >= logging.ERROR:
            return True
        if record.levelno == logging.INFO and record.msg == 'Processing request of type %s':
            return False
        try:
            message = record.getMessage()
        except Exception:
            return True
        forwarded = record.msg == 'Warning: %s: %s' and isinstance(record.args, tuple) and len(record.args) == 2
        if forwarded:
            record.levelno, record.levelname = logging.WARNING, 'WARNING'
            is_tls = record.args[0] == 'InsecureRequestWarning' and str(record.args[1]).startswith('Unverified HTTPS request')
        else:
            is_tls = record.name == 'py.warnings' and re.match(
                r'^(?:[^\n]*:\d+:\s*)?InsecureRequestWarning:\s*Unverified HTTPS request', message)
        if is_tls:
            host = re.search(r"host '([^']+)'", message)
            key = host[1] if host else message
            with self.lock:
                if key in self.seen_tls:
                    return False
                self.seen_tls.add(key)
            if host:
                record.msg = _json_line({'event_type': 'runtime.tls_warning',
                    'warning_category': 'InsecureRequestWarning', 'host': key,
                    'reason': 'certificate_verification_disabled'})
                record.args = ()
                record._ews_safe_json = True
        elif forwarded:
            category = record.args[0]
            record.msg = _json_line({'event_type': 'runtime.warning',
                'warning_category': category if isinstance(category, str) else 'unknown',
                'reason': 'untrusted_warning_text_omitted'})
            record.args = ()
            record._ews_safe_json = True
        return True


def configure_logging(directory):
    """Current logs must open successfully; historical maintenance is best effort."""
    directory = Path(directory).resolve()
    coordinator = _ArchiveCoordinator(directory)
    opened = []
    try:
        audit = DailyFileHandler(directory, 'audit', coordinator)
        opened.append(audit)
        errors = DailyFileHandler(directory, 'errors', coordinator)
        opened.append(errors)
    except Exception:
        for handler in opened:
            handler.close()
        raise
    audit.setFormatter(_Formatter())
    errors.addFilter(_OutputFilter(errors_only=True))
    errors.setFormatter(_Formatter())
    console = _ConsoleHandler(sys.stderr)
    console.addFilter(_OutputFilter())
    console.setFormatter(_Formatter(console=True))
    root, audit_logger = logging.getLogger(), logging.getLogger('mail_mcp.audit')
    previous = set(root.handlers + audit_logger.handlers)
    root.handlers = [console, audit, errors]
    root.setLevel(logging.INFO)
    audit_logger.handlers = [console, audit, errors]
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
    for old_coordinator in {getattr(h, '_coordinator', None) for h in previous} - {None}:
        old_coordinator.shutdown()
    for handler in previous:
        if isinstance(handler, DailyFileHandler):
            handler.close()
    noise = _NoiseFilter()
    for name in ('mcp.server.lowlevel.server', 'py.warnings'):
        logger = logging.getLogger(name)
        logger.filters = [f for f in logger.filters if not isinstance(f, _NoiseFilter)] + [noise]
        logger.setLevel(logging.INFO)
    logging.captureWarnings(True)
    for name in ('exchangelib.fields', 'httpx', 'httpcore'):
        logging.getLogger(name).setLevel(logging.WARNING)
    for name in ('fastmcp', 'uvicorn', 'uvicorn.error'):
        logger = logging.getLogger(name)
        logger.handlers = []
        logger.propagate = True
    pending, coordinator.recovered = coordinator.recovered, []
    for fields in pending:
        coordinator.health('logging.recovered', **fields)
    coordinator.health('service.logging_configured', log_dir=str(directory),
                       uid=os.getuid() if hasattr(os, 'getuid') else None,
                       gid=os.getgid() if hasattr(os, 'getgid') else None)
    coordinator.start()
