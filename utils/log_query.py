"""Read-only queries of current and archived EWS MCP logs.

Only standard-library imports run at import time. The shared audit sanitizer is
loaded when records are read, without importing the application entry point.
Payloads are streamed; memory retains the selected tail plus hashes of unique
event IDs/content variants across the requested range for integrity and dedup.
"""

import argparse
from contextlib import contextmanager
from datetime import date, datetime, time, timedelta, timezone
import gzip
import hashlib
import heapq
import json
import os
from pathlib import Path
import re
import stat
import sys
import zlib


_NAME = re.compile(r"(audit|errors)-(\d{4}-\d{2}-\d{2})\.log(\.gz)?\Z")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_LEGACY_HEADER = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(DEBUG|INFO|WARNING|ERROR|CRITICAL)\]")
_LEGACY_FRAME = re.compile(r'^\s*File "([^"\r\n]+)", line (\d+), in ([A-Za-z_][A-Za-z0-9_]*)\s*$')
_JSON_START = re.compile(r'(?:[\{\[\"]|-?(?:\d|Infinity\b)|NaN\b|(?:null|true|false)\b)')
_SHANGHAI = timezone(timedelta(hours=8))
_SUMMARY_FIELDS = (
    "schema_version", "event_type", "timestamp", "event_id", "call_id",
    "operation_id", "origin_call_id", "service_instance_id", "sequence", "pid",
    "tool", "stage", "call_status", "status", "is_error", "mcp_is_error",
    "error_code", "duration_ms", "caller", "level", "logger", "omitted_reason",
    "length", "frame", "legacy", "line_number",
)


def _write(record, *, error=False):
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # argparse's ordinary error text may echo arbitrary credential values.
        _write({"record_type": "diagnostic", "code": "invalid_arguments"}, error=True)
        raise SystemExit(2)


def _day(value):
    if not _DATE.fullmatch(value):
        raise argparse.ArgumentTypeError("expected YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise argparse.ArgumentTypeError("invalid calendar date") from None


def _tail(value):
    try:
        count = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("expected an integer") from None
    if count < 0:
        raise argparse.ArgumentTypeError("tail must be nonnegative")
    return count


def _arguments(argv):
    parser = _Parser(description=__doc__)
    parser.add_argument("--date", type=_day)
    parser.add_argument("--from", dest="start", type=_day)
    parser.add_argument("--to", dest="end", type=_day)
    parser.add_argument("--kind", choices=("audit", "errors", "all"), default="all")
    for field in ("call-id", "operation-id", "event-id", "tool", "status"):
        parser.add_argument("--" + field)
    parser.add_argument("--tail", type=_tail, default=200)
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--info", action="store_true")
    args = parser.parse_args(argv)
    if args.info:
        if argv != ["--info"]:
            parser.error("info must be used alone")
    elif args.date:
        if args.start or args.end:
            parser.error("date and range are mutually exclusive")
        args.start = args.end = args.date
    elif not args.start or not args.end or args.start > args.end:
        parser.error("a complete ascending date range is required")
    return args


def _root():
    configured = os.environ.get("EWS_MCP_LOG_DIR", "").strip()
    if not configured:
        data = os.environ.get("EWS_MCP_DATA_DIR", "").strip()
        if not data:
            raise ValueError("missing log directory configuration")
        configured = os.path.join(data, "logs")
    return Path(configured).resolve()


class _Query:
    def __init__(self):
        self.failed = False

    def diagnostic(self, code, *, paths=(), line=None, exc=None):
        self.failed = True
        item = {"record_type": "diagnostic", "code": code}
        if paths:
            item["sources"] = [str(path) for path in paths]
        if line is not None:
            item["line_number"] = line
        if exc is not None:
            item["exception_type"] = type(exc).__name__
            if isinstance(exc, OSError):
                item["errno"] = exc.errno
        _write(item, error=True)

    def regular_file(self, path):
        try:
            mode = path.lstat().st_mode
        except FileNotFoundError:
            return False
        except OSError as exc:
            self.diagnostic("source_read_error", paths=[path], exc=exc)
            return False
        if not stat.S_ISREG(mode):
            self.diagnostic("unsafe_source_type", paths=[path])
            return False
        return True


@contextmanager
def _binary(path):
    # Use Path.open for plain files too; no text decoder can hide bad bytes.
    with path.open("rb") as raw:
        if path.suffix == ".gz":
            with gzip.GzipFile(fileobj=raw, mode="rb") as stream:
                yield stream
        else:
            yield raw


def _hash(path):
    digest = hashlib.sha256()
    with _binary(path) as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.digest()


def _sources(root, day, kind, query):
    """Return (selected path, all identical sources) without overwriting history."""
    plain = root / f"{kind}-{day}.log"
    archive_root = root / "archive"
    month = archive_root / day[:7]
    archive = month / f"{kind}-{day}.log.gz"
    paths = [plain]
    # Never follow archive-directory symlinks, even with a fixed filename.
    if archive_root.is_symlink() or month.is_symlink():
        query.diagnostic("unsafe_archive_directory", paths=[archive])
    else:
        paths.append(archive)
    present = [path for path in paths if query.regular_file(path)]
    if len(present) != 2:
        return [(path, [path]) for path in present]
    hashes = []
    for path in present:
        try:
            hashes.append(_hash(path))
        except (OSError, EOFError, ValueError, zlib.error) as exc:
            query.diagnostic("gzip_integrity_error" if path.suffix == ".gz" else "source_read_error", paths=[path], exc=exc)
            hashes.append(None)
    if hashes[0] is not None and hashes[0] == hashes[1]:
        return [(plain, present)]
    if all(value is not None for value in hashes):
        query.diagnostic("source_conflict", paths=present)
    return [(path, [path]) for path in present]


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def _legacy(text):
    """Recognize safe metadata, never copying arbitrary old diagnostic prose."""
    item = {"event_type": "legacy.error", "legacy": True,
            "omitted_reason": "omitted_unsafe_legacy_text", "length": len(text.rstrip("\r\n"))}
    header = _LEGACY_HEADER.match(text)
    if header:
        item["timestamp"] = header[1].replace(" ", "T") + "+08:00"
        item["level"] = header[2]
    frame = _LEGACY_FRAME.fullmatch(text.rstrip("\r\n"))
    if frame:
        item["frame"] = {"line_number": int(frame[2])}
        if re.fullmatch(r"[A-Za-z0-9_./\\:\-]+\.py", frame[1]):
            item["frame"].update(filename=frame[1], function=frame[3])
    return item


def _records(path, aliases, day, kind, query):
    try:
        with _binary(path) as stream:
            for number, raw in enumerate(stream, 1):
                if not raw.endswith(b"\n"):
                    query.diagnostic("incomplete_jsonl_record", paths=[path], line=number)
                    continue
                try:
                    text = raw.decode("utf-8", errors="strict")
                except UnicodeDecodeError as exc:
                    query.diagnostic("invalid_log_encoding", paths=[path], line=number, exc=exc)
                    continue
                try:
                    record = json.loads(text, object_pairs_hook=_object, parse_constant=_invalid_constant)
                    if not isinstance(record, dict):
                        raise ValueError("event must be an object")
                    fingerprint = hashlib.sha256(json.dumps(record, sort_keys=True, ensure_ascii=True, allow_nan=False).encode("ascii")).digest()
                except (ValueError, RecursionError) as exc:
                    legacy_text = _LEGACY_HEADER.match(text) or not _JSON_START.match(text.lstrip())
                    if kind == "errors" and legacy_text:
                        record = _legacy(text)
                        fingerprint = hashlib.sha256(raw).digest()
                    else:
                        query.diagnostic("invalid_json_record", paths=[path], line=number, exc=exc)
                        continue
                try:
                    from utils.audit import sanitize_for_log
                    safe = sanitize_for_log(record, tool=record.get("tool"), kind="event")
                    if not isinstance(safe, dict):
                        raise ValueError("safe event must be an object")
                except Exception as exc:
                    query.diagnostic("event_sanitization_error", paths=[path], line=number, exc=exc)
                    continue
                sources = [{"path": str(alias), "line_number": number, "kind": kind, "date": day} for alias in aliases]
                yield safe, sources, fingerprint
    except (OSError, EOFError, ValueError, zlib.error) as exc:
        query.diagnostic("gzip_integrity_error" if path.suffix == ".gz" else "source_read_error", paths=[path], exc=exc)


def _sort_key(record, sources):
    try:
        stamp = datetime.fromisoformat(record.get("timestamp", "").replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=_SHANGHAI)
    except (TypeError, ValueError, AttributeError):
        stamp = datetime.combine(date.fromisoformat(sources[0]["date"]), time(), _SHANGHAI)
    sequence = record.get("sequence")
    sequence = sequence if isinstance(sequence, int) else -1
    return (stamp.astimezone(timezone.utc), str(record.get("service_instance_id", "")), sequence,
            str(record.get("event_id", "")), sources[0]["path"], sources[0]["line_number"])


def _matches(record, args):
    return all(getattr(args, field) is None or record.get(field) == getattr(args, field)
               for field in ("call_id", "operation_id", "event_id", "tool", "status"))


def _directory_ready(root, query):
    try:
        if not root.is_dir():
            query.diagnostic("log_directory_missing", paths=[root])
            return False
        # A real directory enumeration distinguishes permission failures.
        with os.scandir(root):
            pass
        return True
    except OSError as exc:
        query.diagnostic("directory_read_error", paths=[root], exc=exc)
        return False


def _info(root, query):
    current, archived = set(), set()
    try:
        directories = [(root, False)]
        archive = root / "archive"
        if archive.is_symlink():
            query.diagnostic("unsafe_archive_directory", paths=[archive])
        elif archive.is_dir():
            for month in archive.iterdir():
                if month.is_symlink():
                    query.diagnostic("unsafe_archive_directory", paths=[month])
                elif month.is_dir() and re.fullmatch(r"\d{4}-\d{2}", month.name):
                    directories.append((month, True))
        for directory, compressed in directories:
            for path in directory.iterdir():
                match = _NAME.fullmatch(path.name)
                if not match or bool(match[3]) != compressed:
                    continue
                try:
                    _day(match[2])
                except argparse.ArgumentTypeError:
                    continue
                if not query.regular_file(path):
                    continue
                (archived if compressed else current).add(match[2])
                try:
                    with _binary(path) as stream:
                        stream.read(1)
                except (OSError, EOFError, ValueError, zlib.error) as exc:
                    query.diagnostic("source_read_error", paths=[path], exc=exc)
    except OSError as exc:
        query.diagnostic("directory_read_error", paths=[root], exc=exc)
    _write({"record_type": "info", "directory": str(root),
            "uid": os.getuid() if hasattr(os, "getuid") else None,
            "gid": os.getgid() if hasattr(os, "getgid") else None,
            "current_file_dates": sorted(current), "archived_file_dates": sorted(archived),
            "retention_days": 30, "archive_retention": "indefinite", "readable": not query.failed})
    return 4 if query.failed else 0


def _query(root, args, query):
    _write({"record_type": "query", "directory": str(root), "from": args.start.isoformat(),
            "to": args.end.isoformat(), "kind": args.kind})
    selected, retained, seen = [], {}, {}
    missing_dates, present_dates = [], []
    matched, counter = 0, 0
    day = args.start
    while day <= args.end:
        stamp = day.isoformat()
        found = False
        for kind in ("audit", "errors") if args.kind == "all" else (args.kind,):
            for path, aliases in _sources(root, stamp, kind, query):
                found = True
                for record, sources, fingerprint in _records(path, aliases, stamp, kind, query):
                    event_id = record.get("event_id")
                    event_key = hashlib.sha256(event_id.encode("utf-8", errors="surrogatepass")).digest() if isinstance(event_id, str) else None
                    identity = (event_key, fingerprint) if event_key is not None else (str(path), sources[0]["line_number"])
                    if event_key is not None:
                        versions = seen.setdefault(event_key, set())
                        if fingerprint in versions:
                            if identity in retained:
                                retained[identity]["sources"].extend(sources)
                            continue
                        if versions:
                            query.diagnostic("event_id_conflict", paths=[path], line=sources[0]["line_number"])
                        versions.add(fingerprint)
                    if not _matches(record, args):
                        continue
                    matched += 1
                    if not args.tail:
                        continue
                    entry = {"record": record, "sources": sources, "identity": identity}
                    key = _sort_key(record, sources)
                    counter += 1
                    item = (key, counter, entry)
                    if len(selected) < args.tail:
                        heapq.heappush(selected, item)
                        retained[identity] = entry
                    elif key > selected[0][0]:
                        removed = heapq.heapreplace(selected, item)[2]
                        retained.pop(removed["identity"], None)
                        retained[identity] = entry
        (present_dates if found else missing_dates).append(stamp)
        if day == args.end:  # Avoid overflowing date.max after the final day.
            break
        day += timedelta(days=1)
    for _, _, entry in sorted(selected):
        record = entry["record"]
        rendered = dict(record) if args.full else {field: record[field] for field in _SUMMARY_FIELDS if field in record}
        rendered["record_type"] = "event"
        rendered["sources"] = entry["sources"]
        _write(rendered)
    _write({"record_type": "summary", "matched": matched, "returned": len(selected),
            "present_dates": present_dates, "missing_dates": missing_dates,
            "coverage_complete": not missing_dates and not query.failed,
            "call_state_evaluation": "not_performed"})
    if query.failed:
        return 4
    return 0 if present_dates else 3


def main(argv=None):
    """Return a documented exit code; never initialize the MCP application."""
    try:
        args = _arguments(list(sys.argv[1:] if argv is None else argv))
    except SystemExit as exc:
        return exc.code
    query = _Query()
    try:
        root = _root()
    except (OSError, ValueError) as exc:
        query.diagnostic("log_directory_configuration_error", exc=exc)
        return 4
    if not _directory_ready(root, query):
        return 4
    return _info(root, query) if args.info else _query(root, args, query)


if __name__ == "__main__":
    raise SystemExit(main())
