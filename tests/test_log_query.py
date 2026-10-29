"""Read-only log queries exercise real files without OA or Exchange access."""

from contextlib import redirect_stderr, redirect_stdout
import gzip
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


class LogQueryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.directory = Path(self.tmp.name)
        env = patch.dict(os.environ, {"EWS_MCP_LOG_DIR": self.tmp.name})
        env.start()
        self.addCleanup(env.stop)
        try:
            self.query = importlib.import_module("utils.log_query")
        except ModuleNotFoundError as exc:
            if exc.name == "utils.log_query":
                self.fail("the read-only log query entry point is not implemented")
            raise

    def event(self, event_id="event-1", **values):
        return {
            "schema_version": 2, "event_type": "tool.finished",
            "timestamp": "2026-10-29T01:00:00+00:00", "event_id": event_id,
            "service_instance_id": "instance-1", "sequence": 1,
            "call_id": "call-1", "operation_id": "operation-1", "tool": "get_mail",
            "call_status": "returned", "status": "completed", "is_error": False,
            "result": {"subject": "允许的主题", "item_id": "EWS-ID"}, **values,
        }

    def write(self, rows, day="2026-10-29", kind="audit", compressed=False):
        if compressed:
            target = self.directory / "archive" / day[:7] / f"{kind}-{day}.log.gz"
            target.parent.mkdir(parents=True, exist_ok=True)
        else:
            target = self.directory / f"{kind}-{day}.log"
        content = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows).encode("utf-8")
        target.write_bytes(gzip.compress(content) if compressed else content)
        return target

    def run_query(self, *arguments):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = self.query.main(list(arguments))
        records = [json.loads(line) for line in out.getvalue().splitlines()]
        return code, records, err.getvalue()

    def events(self, rows):
        return [row for row in rows if row.get("record_type") == "event"]

    def diagnostics(self, text):
        return [json.loads(line) for line in text.splitlines()]

    def test_plain_and_gzip_show_the_same_complete_allowed_result(self):
        allowed = "中文\n" * 30000
        event = self.event(result={"subject": allowed, "items": list(range(250)), "body": "MAIL_BODY_NEVER", "confirm_token": "TOKEN_NEVER"})
        source = self.write([event])
        code, rows, err = self.run_query("--date", "2026-10-29", "--full")
        self.assertEqual(code, 0, err)
        full = self.events(rows)[0]
        self.assertEqual(full["result"]["subject"], allowed)
        self.assertEqual(full["result"]["items"], list(range(250)))
        self.assertNotIn("MAIL_BODY_NEVER", json.dumps(rows))
        self.assertNotIn("TOKEN_NEVER", json.dumps(rows))
        self.write([event], compressed=True)
        source.unlink()
        code, archived_rows, err = self.run_query("--date", "2026-10-29", "--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.events(archived_rows)[0]["result"], full["result"])

    def test_default_summary_omits_full_payload(self):
        self.write([self.event()])
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 0, err)
        event = self.events(rows)[0]
        self.assertEqual(event["event_id"], "event-1")
        self.assertNotIn("result", event)

    def test_filters_use_top_level_fields_and_business_status(self):
        self.write([
            self.event("wrong", call_id="other", status="failed", result={"call_id": "call-1"}),
            self.event("chosen", status="failed", call_status="returned"),
            self.event("nested", call_id="other", status="completed", result={"status": "failed"}),
        ])
        code, rows, err = self.run_query("--date", "2026-10-29", "--call-id", "call-1", "--operation-id", "operation-1", "--tool", "get_mail", "--status", "failed", "--event-id", "chosen")
        self.assertEqual(code, 0, err)
        self.assertEqual([row["event_id"] for row in self.events(rows)], ["chosen"])
        code, rows, err = self.run_query("--date", "2026-10-29", "--call-id", "absent")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.events(rows), [])

    def test_tail_counts_unique_events_in_timestamp_and_sequence_order(self):
        events = [self.event(f"event-{n}", sequence=n) for n in range(205)]
        self.write(list(reversed(events)))
        self.write([events[-1]], kind="errors")
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 0, err)
        selected = self.events(rows)
        self.assertEqual(len(selected), 200)
        self.assertEqual(selected[0]["event_id"], "event-5")
        self.assertEqual(selected[-1]["event_id"], "event-204")
        self.assertEqual(len(selected[-1]["sources"]), 2)

    def test_identical_plain_and_archive_are_read_once_with_both_sources(self):
        event = self.event()
        self.write([event])
        self.write([event], compressed=True)
        code, rows, err = self.run_query("--date", "2026-10-29", "--full")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.events(rows)), 1)
        self.assertEqual(len(self.events(rows)[0]["sources"]), 2)

    def test_sink_mirrors_across_midnight_are_one_logical_event(self):
        event = self.event(timestamp="2026-10-28T15:59:59.999999+00:00")
        self.write([event], day="2026-10-28")
        self.write([event], day="2026-10-29", kind="errors")
        code, rows, err = self.run_query("--from", "2026-10-28", "--to", "2026-10-29")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(self.events(rows)), 1)
        self.assertEqual(len(self.events(rows)[0]["sources"]), 2)

    def test_cross_date_conflicts_are_detected_without_retained_payloads(self):
        original = self.event("conflicting", timestamp="2026-10-28T15:59:59+00:00")
        latest = self.event("latest", timestamp="2026-10-29T02:00:00+00:00")
        self.write([original, latest], day="2026-10-28")
        self.write([{**original, "status": "failed"}], day="2026-10-29", kind="errors")
        for extra in (("--tail", "0"), ("--tail", "1"), ("--tail", "1", "--status", "absent")):
            with self.subTest(extra=extra):
                code, rows, err = self.run_query("--from", "2026-10-28", "--to", "2026-10-29", *extra)
                self.assertEqual(code, 4)
                self.assertIn("event_id_conflict", err)
                self.assertFalse(rows[-1]["coverage_complete"])

    def test_cross_date_mirrors_are_counted_once_after_tail_eviction(self):
        mirrored = self.event("mirrored", timestamp="2026-10-28T15:59:59+00:00")
        latest = self.event("latest", timestamp="2026-10-29T02:00:00+00:00")
        self.write([mirrored, latest], day="2026-10-28")
        self.write([mirrored], day="2026-10-29", kind="errors")
        for tail in ("0", "1"):
            with self.subTest(tail=tail):
                code, rows, err = self.run_query("--from", "2026-10-28", "--to", "2026-10-29", "--tail", tail)
                self.assertEqual(code, 0, err)
                self.assertEqual(rows[-1]["matched"], 2)
                self.assertTrue(rows[-1]["coverage_complete"])
                self.assertEqual([row["event_id"] for row in self.events(rows)], [] if tail == "0" else ["latest"])

    def test_conflicting_plain_and_archive_preserve_both_histories(self):
        self.write([self.event("plain")])
        self.write([self.event("archived")], compressed=True)
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 4)
        self.assertEqual({row["event_id"] for row in self.events(rows)}, {"plain", "archived"})
        self.assertIn("source_conflict", err)
        self.assertIn(".log.gz", err)
        self.assertFalse(rows[-1]["coverage_complete"])

    def test_torn_invalid_json_encoding_and_gzip_have_explicit_integrity_errors(self):
        path = self.directory / "audit-2026-10-29.log"
        for content in (b'{"event_id":', b'{}', b'{bad json}\n', b'\xff\n'):
            with self.subTest(content=content):
                path.write_bytes(content)
                code, rows, err = self.run_query("--date", "2026-10-29", "--full")
                self.assertEqual(code, 4)
                self.assertIn(str(path), self.diagnostics(err)[0]["sources"])
                self.assertEqual(self.events(rows), [])
        path.unlink()
        archive = self.directory / "archive" / "2026-10" / "audit-2026-10-29.log.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"invalid gzip")
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 4)
        self.assertIn("gzip", err)

    def test_invalid_deflate_stream_is_a_safe_integrity_failure(self):
        archive = self.directory / "archive" / "2026-10" / "audit-2026-10-29.log.gz"
        archive.parent.mkdir(parents=True)
        archive.write_bytes(b"\x1f\x8b\x08\x00" + bytes(6) + b"\xff" * 16)
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 4)
        self.assertIn("integrity", err)

    def test_json_scalars_and_nonfinite_values_are_not_legacy_error_prose(self):
        path = self.directory / "errors-2026-10-29.log"
        for content in (b'{"result":NaN}\n', b'{"result":Infinity}\n', b'{"result":1e999}\n', b'NaN\n', b'null\n', b'"not an event"\n'):
            with self.subTest(content=content):
                path.write_bytes(content)
                code, rows, err = self.run_query("--date", "2026-10-29")
                self.assertEqual(code, 4)
                self.assertEqual(self.events(rows), [])

    def test_gzip_trailer_corruption_is_not_hidden_by_tail_or_filters(self):
        archive = self.write([self.event()], compressed=True)
        archive.write_bytes(archive.read_bytes()[:-4])
        code, rows, err = self.run_query("--date", "2026-10-29", "--event-id", "absent", "--tail", "0")
        self.assertEqual(code, 4)
        self.assertIn("integrity", err)

    def test_legacy_json_is_sanitized_and_legacy_text_is_metadata_only(self):
        self.write([{"schema_version": 1, "event_type": "mcp_tool_audit", "event_id": "old-id", "tool": "create_mail_draft", "input_arguments": {"subject": "safe", "body": "LEGACY_BODY_NEVER", "confirm_token": "LEGACY_TOKEN_NEVER"}}])
        errors = self.directory / "errors-2026-10-29.log"
        errors.write_text('2026-10-29 09:00:00 [ERROR] worker - LEGACY_FREE_TEXT_NEVER\n  File "/app/mcp_server.py", line 42, in _dispatch\nValueError: LEGACY_EXCEPTION_NEVER\n', encoding="utf-8")
        code, rows, err = self.run_query("--date", "2026-10-29", "--full")
        self.assertEqual(code, 0, err)
        rendered = json.dumps(rows)
        for sentinel in ("LEGACY_BODY_NEVER", "LEGACY_TOKEN_NEVER", "LEGACY_FREE_TEXT_NEVER", "LEGACY_EXCEPTION_NEVER"):
            self.assertNotIn(sentinel, rendered)
        self.assertIn("omitted_unsafe_legacy_text", rendered)
        legacy = next(row for row in self.events(rows) if row.get("event_id") == "old-id")
        self.assertNotIn("call_id", legacy)

    def test_range_reports_missing_dates_without_inventing_operation_state(self):
        self.write([self.event("start", event_type="tool.started", call_status="started")], day="2026-10-28")
        self.write([self.event("finish", timestamp="2026-10-30T01:00:00+00:00")], day="2026-10-30")
        code, rows, err = self.run_query("--from", "2026-10-28", "--to", "2026-10-30", "--tail", "1", "--status", "completed")
        self.assertEqual(code, 0, err)
        self.assertEqual([row["event_id"] for row in self.events(rows)], ["finish"])
        self.assertIn("2026-10-29", rows[-1]["missing_dates"])
        self.assertEqual(rows[-1]["call_state_evaluation"], "not_performed")
        self.assertNotIn("inflight", json.dumps(rows))

    def test_argument_and_missing_source_exit_codes_are_distinct(self):
        for args in ([], ["--date", "../private"], ["--from", "2026-10-29"], ["--to", "2026-10-29"], ["--date", "2026-10-29", "--from", "2026-10-28", "--to", "2026-10-29"], ["--from", "2026-10-30", "--to", "2026-10-29"], ["--date", "2026-10-29", "--tail", "-1"], ["--info", "--date", "2026-10-29"], ["--directory", self.tmp.name]):
            with self.subTest(args=args):
                code, rows, err = self.run_query(*args)
                self.assertEqual(code, 2)
        code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 3)

    def test_info_is_read_only_and_fallback_uses_data_directory(self):
        self.write([self.event()])
        before = {str(path): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        code, rows, err = self.run_query("--info")
        self.assertEqual(code, 0, err)
        self.assertEqual(rows[0]["directory"], str(self.directory.resolve()))
        self.assertIn("2026-10-29", rows[0]["current_file_dates"])
        self.assertEqual(before, {str(path): path.read_bytes() for path in self.directory.rglob("*") if path.is_file()})
        with patch.dict(os.environ, {"EWS_MCP_LOG_DIR": "", "EWS_MCP_DATA_DIR": str(self.directory)}):
            code, rows, err = self.run_query("--info")
        self.assertEqual(code, 4)
        self.assertIn(str(self.directory / "logs"), self.diagnostics(err)[0]["sources"])

    def test_permission_failures_are_read_errors_not_missing_dates(self):
        self.write([self.event()])
        with patch.object(Path, "open", side_effect=PermissionError(13, "denied")):
            code, rows, err = self.run_query("--date", "2026-10-29")
        self.assertEqual(code, 4)
        self.assertIn("PermissionError", err)

    def test_plain_and_archived_file_symlinks_are_not_followed(self):
        outside = self.directory / "outside.log"
        outside.write_text('{"body":"SYMLINK_BODY_NEVER"}\n', encoding="utf-8")
        for compressed in (False, True):
            with self.subTest(compressed=compressed):
                path = (self.directory / "archive" / "2026-10" / "audit-2026-10-29.log.gz"
                        if compressed else self.directory / "audit-2026-10-29.log")
                path.parent.mkdir(parents=True, exist_ok=True)
                try:
                    path.symlink_to(outside)
                except OSError as exc:
                    self.skipTest(f"symlink creation is unavailable: {type(exc).__name__}")
                try:
                    code, rows, err = self.run_query("--date", "2026-10-29", "--full")
                    self.assertEqual(code, 4)
                    self.assertEqual(self.events(rows), [])
                    self.assertNotIn("SYMLINK_BODY_NEVER", json.dumps(rows) + err)
                    self.assertIn("unsafe_source_type", err)
                finally:
                    path.unlink()

    def test_import_does_not_initialize_server_logging_clients_or_credentials(self):
        script = """
import logging, os, pathlib, sys
before = list(logging.getLogger().handlers)
class NoCredentialReads(dict):
    def get(self, key, default=None):
        raise AssertionError('environment read during import')
os.environ = NoCredentialReads()
import utils.log_query
assert logging.getLogger().handlers == before
assert 'mcp_server' not in sys.modules
assert 'outlook_client' not in sys.modules
assert 'oa_lookup' not in sys.modules
assert not list(pathlib.Path(sys.argv[1]).iterdir())
"""
        result = subprocess.run([sys.executable, "-c", script, self.tmp.name], cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
