"""Offline MCP concurrency and cancelled-write receipt regressions."""

import asyncio
import json
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Message
from fastmcp import Client
from mcp.shared.exceptions import McpError
import mcp.types as mcp_types

from confirmation import OperationStore
from flag_operations import FlagOperations
import mcp_server


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"


class ToolConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.item = Message(id="flag-message", changekey="version-1", flag_status=2)
        self.business = FlagOperations()
        self.business.config = SimpleNamespace(email=MAILBOX)
        self.business._tool_item = Mock(return_value=self.item)
        self.saved = []
        self.audit_logger = Mock()
        for patcher in (
            patch.object(mcp_server, "get_store", return_value=self.store),
            patch.object(mcp_server, "resolve_identity_by_lanid",
                         return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(mcp_server.OutlookConfig, "from_service_env",
                         return_value=self.business.config),
            patch.object(mcp_server, "OutlookClient", return_value=self.business),
            patch.object(mcp_server.tool_audit, "logger", self.audit_logger),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def wait_for_worker(self):
        async def wait():
            while not self.entered.is_set():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(wait(), timeout=3)

    def blocking_save(self, item, *, update_fields):
        self.saved.append(item.id)
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("Event loop did not release the blocked write")
        item.changekey = "version-2"
        return item

    async def preview(self, client):
        params = {**IDENTITY, "message_id": self.item.id, "flag": "complete"}
        pending = (await client.call_tool("set_message_flag", params)).structured_content
        self.assertTrue(pending["confirmation_required"], pending)
        return {
            **params, "operation_id": pending["operation_id"],
            "confirm_token": pending["confirm_token"],
        }

    async def test_blocking_tool_allows_another_tool_to_complete(self):
        def slow_read():
            self.entered.set()
            released = self.release.wait(timeout=2)
            return {"released_by_other_request": released}

        def quick_read():
            self.release.set()
            return {"request_completed": True}

        self.business.list_folders = slow_read
        self.business.get_mailbox_overview = quick_read
        async with Client(mcp_server.mcp) as client:
            slow = asyncio.create_task(client.call_tool("list_folders", IDENTITY))
            await self.wait_for_worker()
            quick = await client.call_tool("get_mailbox_overview", IDENTITY)
            result = await slow
        self.assertTrue(quick.structured_content["results"]["request_completed"])
        self.assertTrue(result.structured_content["results"]["released_by_other_request"])

    async def test_worker_keeps_oa_identity_assurance_in_audit(self):
        loop_thread = threading.get_ident()
        self.business.list_folders = lambda: {"thread": threading.get_ident()}
        async with Client(mcp_server.mcp) as client:
            result = await client.call_tool("list_folders", IDENTITY)
        records = [json.loads(call.args[0]) for call in self.audit_logger.info.call_args_list]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["identity_assurance"], "oa_name_match")
        self.assertEqual(records[0]["status"], "success")
        self.assertNotEqual(result.structured_content["results"]["thread"], loop_thread)

    async def test_concurrent_confirmations_write_once(self):
        with patch.object(Message, "save", autospec=True, side_effect=self.blocking_save):
            async with Client(mcp_server.mcp) as client:
                params = await self.preview(client)
                first = asyncio.create_task(client.call_tool("set_message_flag", params, raise_on_error=False))
                await self.wait_for_worker()
                duplicate = await client.call_tool("set_message_flag", params, raise_on_error=False)
                self.release.set()
                completed = await first
                replay = await client.call_tool("set_message_flag", params, raise_on_error=False)
        self.assertEqual(duplicate.structured_content["status"], "executing")
        self.assertEqual(completed.structured_content["status"], "completed")
        self.assertTrue(replay.structured_content["replayed"])
        self.assertEqual(replay.structured_content["results"], completed.structured_content["results"])
        self.assertEqual(self.saved, ["flag-message"])

    async def test_cancelled_mcp_request_still_persists_completed_receipt(self):
        with patch.object(Message, "save", autospec=True, side_effect=self.blocking_save):
            async with Client(mcp_server.mcp) as client:
                params = await self.preview(client)
                request_id = client.session._request_id
                confirm = asyncio.create_task(client.call_tool("set_message_flag", params, raise_on_error=False))
                await self.wait_for_worker()
                status_before_cancel = self.store.get(params["operation_id"])["status"]
                await client.session.send_notification(mcp_types.ClientNotification(
                    mcp_types.CancelledNotification(params=mcp_types.CancelledNotificationParams(
                        requestId=request_id, reason="Caller stopped waiting",
                    )),
                ))
                with self.assertRaisesRegex(McpError, "Request cancelled"):
                    await confirm
                self.assertEqual(self.store.get(params["operation_id"])["status"], "executing")
                self.release.set()

                async def wait_for_receipt():
                    while self.store.get(params["operation_id"])["status"] == "executing":
                        await asyncio.sleep(0.005)
                await asyncio.wait_for(wait_for_receipt(), timeout=3)
                query = await client.call_tool("set_message_flag", {
                    **IDENTITY, "operation_id": params["operation_id"],
                }, raise_on_error=False)
                replay = await client.call_tool("set_message_flag", params, raise_on_error=False)
        self.assertEqual(status_before_cancel, "executing")
        self.assertEqual(query.structured_content["status"], "completed")
        self.assertTrue(query.structured_content["ok"])
        self.assertTrue(replay.structured_content["replayed"])
        self.assertEqual(replay.structured_content["results"], query.structured_content["results"])
        self.assertEqual(self.saved, ["flag-message"])
        records = [json.loads(call.args[0]) for call in self.audit_logger.info.call_args_list]
        self.assertEqual(len(records), 4)
        self.assertTrue(all(record["status"] == "success" for record in records), records)


if __name__ == "__main__":
    unittest.main()
