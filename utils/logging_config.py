"""Daily error/audit files and narrowly scoped console noise reduction."""

import copy
from datetime import date, timedelta
import json
import logging
import os
from pathlib import Path
import re
import sys
import threading


_FAILED = frozenset(('failure', 'rejected', 'error'))
_LOG_NAME = re.compile(r'(?:errors|audit)-(\d{4}-\d{2}-\d{2})\.log')
_FORMAT = '%(asctime)s [%(levelname)s] %(name)s - %(message)s'


class DailyFileHandler(logging.FileHandler):
    """Append by local calendar date; retain today and the preceding 29 days."""
    def __init__(self, directory, kind):
        self.directory, self.kind = Path(directory), kind
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o750)
        self.day = date.today()
        self._prune()
        super().__init__(self.directory / f'{kind}-{self.day.isoformat()}.log',
                         encoding='utf-8', errors='backslashreplace')

    def _open(self):
        return open(self.baseFilename, self.mode, encoding=self.encoding, errors=self.errors,
                    opener=lambda path, flags: os.open(path, flags, 0o640))

    def _prune(self):
        cutoff = self.day - timedelta(days=29)
        for path in self.directory.glob('*.log'):
            match = _LOG_NAME.fullmatch(path.name)
            if not match or path.is_symlink() or not path.is_file():
                continue
            try:
                stamp = date.fromisoformat(match[1])
            except ValueError:
                continue
            if stamp < cutoff:
                path.unlink(missing_ok=True)

    def emit(self, record):
        try:
            today = date.today()
            if today != self.day:
                old_name, old_stream = self.baseFilename, self.stream
                self.baseFilename = str(self.directory / f'{self.kind}-{today.isoformat()}.log')
                try:
                    new_stream = self._open()
                except Exception:
                    self.baseFilename = old_name
                    raise
                self.stream, self.day = new_stream, today
                if old_stream is not None:
                    old_stream.close()
                self._prune()
            super().emit(record)
        except Exception:
            self.handleError(record)


class _OutputFilter(logging.Filter):
    def __init__(self, errors_only=False):
        super().__init__()
        self.errors_only = errors_only

    def filter(self, record):
        status = getattr(record, 'audit_status', None)
        if status is not None:
            return status in _FAILED
        return record.levelno >= (logging.ERROR if self.errors_only else logging.INFO)


class _Formatter(logging.Formatter):
    def __init__(self, console=False):
        super().__init__(_FORMAT, datefmt='%Y-%m-%d %H:%M:%S')
        self.console = console

    def format(self, record):
        record = copy.copy(record)
        if getattr(record, 'audit_status', None) in _FAILED:
            record.levelno, record.levelname = logging.ERROR, 'ERROR'
            if self.console:
                record.msg = json.dumps({
                    'tool': record.audit_tool, 'status': record.audit_status,
                    'error_code': record.audit_error_code, 'event_id': record.audit_event_id,
                }, ensure_ascii=False, separators=(',', ':'))
                record.args = ()
        if self.console:
            record.exc_info = record.exc_text = record.stack_info = None
        return super().format(record)


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
        message = record.getMessage()
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
                record.msg = 'InsecureRequestWarning: Unverified HTTPS request to host %r; certificate verification is disabled.'
                record.args = (key,)
        return True


def configure_logging(directory):
    """Fail at startup if file logging is unavailable; no silent console fallback."""
    directory = Path(directory).resolve()
    opened = []
    try:
        errors = DailyFileHandler(directory, 'errors')
        opened.append(errors)
        audit = DailyFileHandler(directory, 'audit')
        opened.append(audit)
    except Exception:
        for handler in opened:
            handler.close()
        raise
    errors.addFilter(_OutputFilter(errors_only=True))
    errors.setFormatter(_Formatter())
    audit.setFormatter(logging.Formatter('%(message)s'))
    console = logging.StreamHandler(sys.stderr)
    console.addFilter(_OutputFilter())
    console.setFormatter(_Formatter(console=True))
    root, audit_logger = logging.getLogger(), logging.getLogger('mail_mcp.audit')
    previous = set(root.handlers + audit_logger.handlers)
    root.handlers = [console, errors]
    root.setLevel(logging.INFO)
    audit_logger.handlers = [audit, errors, console]
    audit_logger.setLevel(logging.INFO)
    audit_logger.propagate = False
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
