import asyncio
import hashlib
import io
import json
import logging
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastmcp import Client, FastMCP

from attachment_downloads import (AttachmentDownloadStore, download_lifespan,
                                  download_tool_result, register_download_route)
from config import DownloadSettings


class DownloadHTTPTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        env = patch.dict(os.environ, {
            "EWS_MCP_DATA_DIR": self.tmp.name, "EWS_MCP_DOWNLOAD_ENABLED": "true",
            "EWS_MCP_PUBLIC_BASE_URL": "http://host.internal:7712",
            "EWS_MCP_DOWNLOAD_SECRET": "x" * 32,
            "EWS_MCP_DOWNLOAD_TTL_SECONDS": "60",
            "EWS_MCP_DOWNLOAD_RETENTION_SECONDS": "120",
            "EWS_MCP_DOWNLOAD_CLEANUP_SECONDS": "1",
        }, clear=True)
        env.start()
        self.addCleanup(env.stop)
        self.store = AttachmentDownloadStore(DownloadSettings.from_env())
        self.record = self.store.save("employee@example.com", "中文报表.csv", "text/csv",
                                      lambda: io.BytesIO(b"a,b\n1,2\n"))
        self.server = FastMCP("attachment-integration", lifespan=download_lifespan)
        register_download_route(self.server)

    async def test_tool_metadata_and_get_bytes_retry_tampering(self):
        @self.server.tool()
        def prepare_fixture():
            return download_tool_result(self.store.issue(self.record["download_id"]))
        async with Client(self.server) as client:
            result = await client.call_tool("prepare_fixture", {})
        self.assertEqual(json.loads(result.content[0].text), result.structured_content)
        payload = result.structured_content["results"]
        self.assertNotIn("a,b", result.content[0].text)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.http_app(transport="sse"))) as client:
            for _ in range(2):
                response = await client.get(payload["download_url"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(hashlib.sha256(response.content).hexdigest(), payload["sha256"])
                self.assertIn("filename*=UTF-8''", response.headers["content-disposition"])
                self.assertEqual(response.headers["cache-control"], "no-store")
                self.assertEqual(response.headers["content-type"], "text/csv")
            bad_url = payload["download_url"].split("&token=")[0] + "&token=" + "0" * 64
            self.assertEqual((await client.get(bad_url)).status_code, 403)
            with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}):
                self.assertEqual((await client.get(payload["download_url"])).status_code, 404)

    async def test_periodic_cleanup_without_another_export(self):
        with patch("attachment_downloads.time.time", return_value=0):
            expired = self.store.save("employee@example.com", "expired", None, io.BytesIO)
        # Start first, expire a file after startup to exercise the recurring sweep.
        async with download_lifespan(self.server):
            self.assertFalse(Path(expired["saved_path"]).exists())
            with patch("attachment_downloads.time.time", return_value=0):
                later = self.store.save("employee@example.com", "later", None, io.BytesIO)
            await asyncio.sleep(1.2)
            self.assertFalse(Path(later["saved_path"]).exists())

    async def test_disabled_downloads_still_clean_existing_cache(self):
        with patch("attachment_downloads.time.time", return_value=0):
            expired = self.store.save("employee@example.com", "expired", None, io.BytesIO)
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}):
            async with download_lifespan(self.server):
                self.assertFalse(Path(expired["saved_path"]).exists())

    async def test_tool_registry_and_parameter_model(self):
        from tool_specs import SPECS, READ_TOOLS
        from tool_params import PrepareAttachmentDownloadParams
        self.assertIn("prepare_attachment_download", READ_TOOLS)
        params = SPECS["prepare_attachment_download"]["inputSchema"]["properties"]["params"]
        self.assertEqual(set(params["required"]), {"lanid", "name", "message_id", "attachment_id"})
        self.assertNotIn("save", params["properties"])
        model = PrepareAttachmentDownloadParams(lanid="test123", name="测试", message_id="m1", attachment_id="a1")
        self.assertEqual(model.attachment_id, "a1")

    async def test_access_log_does_not_record_bearer_token(self):
        payload = self.store.issue(self.record["download_id"])
        for url in (payload["download_url"], payload["download_url"].replace("?", "/?"),
                    payload["download_url"].replace("downloads", "down%6coads")):
            record = logging.LogRecord("uvicorn.access", logging.INFO, "", 0,
                '%s - "%s %s HTTP/%s" %d', ("client", "GET", url, "1.1", 200), None)
            logging.getLogger("uvicorn.access").filter(record)
            self.assertNotIn(payload["download_url"].split("token=")[1], record.getMessage())
            self.assertIn("token=<redacted>", record.getMessage())

    async def test_cancellation_while_opening_file_does_not_leak_handle(self):
        opened, released = threading.Event(), threading.Event()
        streams = []
        original = AttachmentDownloadStore.open_download
        def delayed_open(store, *args):
            record, stream = original(store, *args)
            streams.append(stream)
            opened.set()
            released.wait(3)
            return record, stream
        payload = self.store.issue(self.record["download_id"])
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.server.http_app(transport="sse"))) as client:
            with patch.object(AttachmentDownloadStore, "open_download", delayed_open):
                task = asyncio.create_task(client.get(payload["download_url"]))
                try:
                    self.assertTrue(await asyncio.to_thread(opened.wait, 2))
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    released.set()
                for _ in range(30):
                    if streams and streams[0].closed:
                        break
                    await asyncio.sleep(0.01)
                try:
                    self.assertTrue(streams[0].closed)
                finally:
                    streams[0].close()
