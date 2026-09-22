import hashlib
import io
import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from config import DownloadSettings
from attachment_downloads import AttachmentDownloadStore, DownloadError
from tool_support import ToolOperationError


class DownloadStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = DownloadSettings(
            root=self.tmp.name, enabled=True, base_url="http://mail.internal:7712",
            secret="test-only-secret-" * 4, max_bytes=100, max_cache_bytes=1000,
            max_files=10, ttl_seconds=60, retention_seconds=120,
        )
        self.store = AttachmentDownloadStore(self.settings)

    def save(self, body=b"example attachment", **kwargs):
        return self.store.save("employee@example.com", "报表.txt", "text/plain",
                               lambda: io.BytesIO(body), **kwargs)

    def url_parts(self, result):
        url = urlsplit(result["download_url"])
        params = {key: value[0] for key, value in parse_qs(url.query).items()}
        return url.path.rsplit("/", 1)[1], params["expires"], params["token"]

    def test_signed_download_survives_new_store_and_has_no_private_path(self):
        record = self.save()
        result = self.store.issue(record["download_id"])
        self.assertTrue(result["download_url"].startswith("http://mail.internal:7712/downloads/"))
        self.assertNotIn("saved_path", result)
        self.assertNotIn("employee@example.com", str(result))
        restarted = AttachmentDownloadStore(self.settings)
        metadata, stream = restarted.open_download(*self.url_parts(result))
        with stream:
            body = stream.read()
        self.assertEqual(body, b"example attachment")
        self.assertEqual(metadata["sha256"], hashlib.sha256(body).hexdigest())
        self.assertEqual(result["filename"], "报表.txt")
        self.assertEqual(result["size"], len(body))

    def test_missing_tampered_and_expired_tokens(self):
        with patch("attachment_downloads.time.time", return_value=1000):
            result = self.store.issue(self.save()["download_id"])
        identifier, expires, token = self.url_parts(result)
        cases = [(identifier, expires, ""), (identifier, "99999", token),
                 ("0" * 32, expires, token), ("../config.py", expires, token)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(DownloadError) as cm:
                self.store.open_download(*args)
            self.assertEqual(cm.exception.status_code, 403)
        with patch("attachment_downloads.time.time", return_value=1060):
            with self.assertRaises(DownloadError) as cm:
                self.store.open_download(identifier, expires, token)
            self.assertEqual(cm.exception.status_code, 410)

    def test_size_limit_reads_no_more_than_limit_plus_one_and_releases_quota(self):
        reads = []
        class TrackingStream(io.BytesIO):
            def read(self, n=-1):
                reads.append(n)
                return super().read(n)
        with self.assertRaises(ToolOperationError) as cm:
            self.store.save("employee@example.com", "large", None,
                            lambda: TrackingStream(b"x" * 1000))
        self.assertEqual(cm.exception.code, "ATTACHMENT_TOO_LARGE")
        self.assertEqual(sum(reads), 101)
        self.assertEqual([p for p in Path(self.tmp.name).glob("exports/**/*") if p.is_file()], [])
        self.save(b"ok")

    def test_reported_oversize_does_not_open_exchange_stream(self):
        with self.assertRaises(ToolOperationError) as cm:
            self.save(reported_size=101)
        self.assertEqual(cm.exception.code, "ATTACHMENT_TOO_LARGE")
        self.assertEqual([p for p in Path(self.tmp.name).glob("exports/**/*") if p.is_file()], [])

    def test_failed_stream_removes_partial_and_allows_retry(self):
        class BrokenStream(io.BytesIO):
            def read(self, n=-1):
                raise RuntimeError("exchange interrupted")
        with self.assertRaises(RuntimeError):
            self.store.save("employee@example.com", "broken", None, BrokenStream)
        self.assertEqual([p for p in Path(self.tmp.name).glob("exports/**/*") if p.is_file()], [])
        self.save()

    def test_empty_file_is_valid_and_unsafe_name_cannot_control_path(self):
        record = self.store.save("employee@example.com", "../../outside\r\n.txt", "text/plain\r\nx: y",
                                 lambda: io.BytesIO(b""))
        self.assertEqual(record["size"], 0)
        self.assertEqual(Path(record["saved_path"]).parent.name, "employee@example.com")
        self.assertNotIn("\n", record["name"])
        self.assertEqual(record["content_type"], "application/octet-stream")
        with self.assertRaises(ToolOperationError):
            self.store.save("../outside", "x", None, io.BytesIO)

    def test_quota_is_atomic_for_concurrent_reservations(self):
        settings = replace(self.settings, max_files=1, max_cache_bytes=100)
        store = AttachmentDownloadStore(settings)
        def save_one(_):
            try:
                return store.save("employee@example.com", "x", None, lambda: io.BytesIO(b"a"))
            except ToolOperationError as exc:
                return exc.code
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(save_one, range(4)))
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(results.count("DOWNLOAD_CACHE_FULL"), 3)

    def test_cleanup_preserves_unindexed_files_and_removes_expired_exports(self):
        legacy = Path(self.tmp.name) / "exports" / "legacy.txt"
        legacy.parent.mkdir(exist_ok=True)
        legacy.write_text("preexisting")
        with patch("attachment_downloads.time.time", return_value=1000):
            saved = self.save()
        with patch("attachment_downloads.time.time", return_value=1119):
            self.store.cleanup()
            self.assertTrue(Path(saved["saved_path"]).exists())
        with patch("attachment_downloads.time.time", return_value=1120):
            self.store.cleanup()
            self.assertFalse(Path(saved["saved_path"]).exists())
        self.assertTrue(legacy.exists())

    def test_missing_file_is_reported_and_disabled_download_cannot_be_issued(self):
        record = self.save()
        result = self.store.issue(record["download_id"])
        Path(record["saved_path"]).unlink()
        with self.assertRaises(DownloadError) as cm:
            self.store.open_download(*self.url_parts(result))
        self.assertEqual(cm.exception.status_code, 410)
        store = AttachmentDownloadStore(replace(self.settings, enabled=False))
        with self.assertRaises(ToolOperationError) as cm:
            store.issue(record["download_id"])
        self.assertEqual(cm.exception.code, "DOWNLOAD_DISABLED")

    def test_settings_reject_invalid_public_url_and_limits(self):
        env = {"EWS_MCP_DATA_DIR": self.tmp.name, "EWS_MCP_DOWNLOAD_ENABLED": "true",
               "EWS_MCP_DOWNLOAD_SECRET": "a" * 32,
               "EWS_MCP_PUBLIC_BASE_URL": "http://host:7712"}
        for key, value in [("EWS_MCP_PUBLIC_BASE_URL", "http://user:pass@host"),
                           ("EWS_MCP_DOWNLOAD_SECRET", "short"),
                           ("EWS_MCP_DOWNLOAD_MAX_BYTES", "0"),
                           ("EWS_MCP_DOWNLOAD_TTL_SECONDS", "-1")]:
            with self.subTest(key=key), patch.dict(os.environ, {**env, key: value}, clear=True):
                with self.assertRaises(ValueError):
                    DownloadSettings.from_env()

    def test_cleanup_does_not_delete_a_live_write_when_retention_is_short(self):
        # Cache retention begins when writing finishes, not when it starts.
        store = AttachmentDownloadStore(replace(self.settings, ttl_seconds=1, retention_seconds=1))
        def delayed_open():
            with patch("attachment_downloads.time.time", return_value=1002):
                store.cleanup()
            return io.BytesIO(b"abc")
        with patch("attachment_downloads.time.time", return_value=1000):
            result = store.save("employee@example.com", "slow", None, delayed_open)
        self.assertTrue(Path(result["saved_path"]).exists())
        with patch("attachment_downloads.time.time", return_value=1000):
            self.assertIn("download_url", store.issue(result["download_id"]))

    def test_storage_failure_returns_safe_error_without_path_in_cause(self):
        bad_directory = Path(self.tmp.name) / "exports" / "employee@example.com"
        bad_directory.write_text("directory is unavailable")
        with self.assertRaises(ToolOperationError) as cm:
            self.save()
        self.assertNotIn(self.tmp.name, str(cm.exception))
        self.assertIsNone(cm.exception.__cause__)
