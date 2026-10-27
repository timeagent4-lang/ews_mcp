"""Offline send response deadlines preserve eventual, non-duplicated receipts."""

import asyncio
import json
import os
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from anyio import to_thread
from exchangelib import Mailbox, Message
from exchangelib.errors import ErrorAccessDenied
from exchangelib.properties import ParentFolderId
from fastmcp import Client
from mcp.shared.exceptions import McpError
import mcp.types as mcp_types

from confirmation import OperationStore
from utils.lanid_email import IdentityResolutionError
from write_operations import WriteOperations
import mcp_server


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"


class SendResponseWaitTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.addCleanup(self.release.set)
        self.sends = []
        self.send_error = None
        self.tasks = []
        self.draft = Message(
            id="draft-1", changekey="version-1", is_draft=True,
            parent_folder_id=ParentFolderId(id="drafts-folder"),
            author=Mailbox(email_address=MAILBOX),
            to_recipients=[Mailbox(email_address="recipient@example.com")],
            subject="Offline response wait regression", body="Draft body",
        )
        self.business = WriteOperations()
        self.business.config = SimpleNamespace(email=MAILBOX)
        self.business._tool_item = Mock(return_value=self.draft)
        self.business._tool_folder = lambda name: SimpleNamespace(id=name + "-folder")
        self.business.list_folders = lambda: {"folders": ["drafts", "sent"]}
        self.audit_logger = Mock()
        for patcher in (
            patch.dict(os.environ, {"EWS_MCP_SEND_ENABLED": "true"}),
            patch.object(mcp_server, "SEND_RESPONSE_WAIT_SECONDS", 0.08, create=True),
            patch.object(mcp_server, "get_store", return_value=self.store),
            patch.object(mcp_server, "resolve_identity_by_lanid",
                         return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(mcp_server.OutlookConfig, "from_service_env",
                         return_value=self.business.config),
            patch.object(mcp_server, "OutlookClient", return_value=self.business),
            patch.object(mcp_server.tool_audit, "logger", self.audit_logger),
            patch.object(Message, "send", autospec=True, side_effect=self.send),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def asyncTearDown(self):
        self.release.set()
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks, return_exceptions=True), 3)
        background = tuple(mcp_server._SEND_DISPATCH_TASKS)
        if background:
            await asyncio.wait_for(asyncio.gather(*background, return_exceptions=True), 3)

    def block(self):
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise TimeoutError("Test did not release the external operation")

    def send(self, draft, *, save_copy, copy_to_folder):
        self.assertTrue(save_copy)
        self.assertEqual(copy_to_folder.id, "sent-folder")
        self.sends.append(draft.id)
        self.block()
        if self.send_error is not None:
            raise self.send_error

    async def eventually(self, condition):
        async def poll():
            while not condition():
                await asyncio.sleep(0.005)
        await asyncio.wait_for(poll(), 3)

    async def call(self, client, **params):
        return await client.call_tool("send_draft", {**IDENTITY, **params}, raise_on_error=False)

    async def preview(self, client):
        preview = (await self.call(client, draft_id=self.draft.id)).structured_content
        self.assertTrue(preview["confirmation_required"], preview)
        return {"draft_id": self.draft.id, "operation_id": preview["operation_id"],
                "confirm_token": preview["confirm_token"]}

    async def blocked_response(self, client, **params):
        task = asyncio.create_task(self.call(client, **params))
        self.tasks.append(task)
        await self.eventually(self.entered.is_set)
        try:
            return await asyncio.wait_for(asyncio.shield(task), 0.6)
        except asyncio.TimeoutError:
            self.release.set()
            await asyncio.wait_for(task, 3)
            self.fail("send_draft must respond before the blocked external operation is released")

    def assert_processing(self, result, operation_id):
        data = result.structured_content
        self.assertFalse(result.is_error, data)
        self.assertNotIn("ok", data)
        self.assertEqual(data["status"], "processing")
        self.assertEqual(data["operation_id"], operation_id)
        self.assertIsNone(data["results"])
        self.assertTrue(data["wait_timed_out"])
        self.assertEqual(data["message"], "邮箱服务不稳定 请五分钟后查看结果")
        self.assertNotIn("error_code", data)
        self.assertEqual(json.loads(result.content[0].text), data)
        self.assertNotIn("confirm_token", data)
        self.assertFalse(self.release.is_set())

    async def wait_for_receipt(self, operation_id, status):
        await self.eventually(lambda: self.store.get(operation_id)["status"] == status)

    async def test_blocked_send_returns_processing_then_receipt_and_reconfirmation_send_once(self):
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)
            result = await self.blocked_response(client, **params)
            self.assert_processing(result, params["operation_id"])
            self.assertEqual(self.store.get(params["operation_id"])["status"], "executing")
            self.assertEqual(self.store._conn.execute('SELECT operation_id FROM draft_claims').fetchone()[0], params['operation_id'])
            duplicate = await self.call(client, **params)
            self.assertEqual(duplicate.structured_content["status"], "executing")
            quick = await client.call_tool("list_folders", IDENTITY)
            self.assertFalse(quick.is_error)
            self.assertEqual(quick.structured_content["results"], {"folders": ["drafts", "sent"]})
            processing_audits = [json.loads(call.args[0]) for call in self.audit_logger.info.call_args_list]
            self.assertEqual(sum(record["status"] == "processing" for record in processing_audits), 1)
            self.release.set()
            await self.wait_for_receipt(params["operation_id"], "completed")
            self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)
            query = await self.call(client, operation_id=params["operation_id"])
            replay = await self.call(client, **params)
        self.assertNotIn("ok", query.structured_content)
        self.assertEqual(query.structured_content["status"], "completed")
        self.assertTrue(replay.structured_content["replayed"])
        self.assertEqual(replay.structured_content["results"], query.structured_content["results"])
        self.assertEqual(self.sends, ["draft-1"])
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)

    async def failed_background_send(self, exception, status, code):
        self.send_error = exception
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)
            result = await self.blocked_response(client, **params)
            self.assert_processing(result, params["operation_id"])
            self.release.set()
            await self.wait_for_receipt(params["operation_id"], status)
            receipt = await self.call(client, operation_id=params["operation_id"])
        self.assertTrue(receipt.is_error)
        self.assertEqual(receipt.structured_content["status"], status)
        self.assertEqual(receipt.structured_content["error_code"], code)
        if status == "failed":
            self.assertEqual(receipt.structured_content["exchange_code"], "ErrorAccessDenied")
        if status == "unknown":
            self.assertEqual(receipt.structured_content["message"], "邮箱服务不稳定 请五分钟后查看结果")
        self.assertEqual(self.sends, ["draft-1"])

    async def test_timeout_preserves_eventual_unknown_submission_receipt(self):
        await self.failed_background_send(TimeoutError("External submission timed out"),
                                          "unknown", "SEND_FAILED_OR_UNKNOWN")
        self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)

    async def test_pre_submission_reads_fail_without_poisoning_draft_claim(self):
        self.release.set()
        async with Client(mcp_server.mcp) as client:
            for boundary in ("draft", "sent"):
                with self.subTest(boundary=boundary):
                    params = await self.preview(client)
                    if boundary == "draft":
                        patcher = patch.object(self.business, "_tool_item", side_effect=TimeoutError("GetItem timeout"))
                    else:
                        patcher = patch.object(self.business, "_tool_folder", side_effect=lambda name:
                            (_ for _ in ()).throw(TimeoutError("GetFolder timeout")) if name == "sent"
                            else SimpleNamespace(id=name + "-folder"))
                    with patcher:
                        result = await self.call(client, **params)
                    self.assertEqual(result.structured_content["status"], "failed")
                    self.assertEqual(result.structured_content["error_code"], "UPSTREAM_TIMEOUT")
                    self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)
                    receipt = await self.call(client, operation_id=params["operation_id"])
                    self.assertEqual(result.structured_content, receipt.structured_content)
        self.assertEqual(self.sends, [])

    async def test_unknown_send_releases_draft_but_same_operation_never_resubmits(self):
        self.release.set()
        self.send_error = TimeoutError("lost SendItem response")
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)
            result = await self.call(client, **params)
            self.assertEqual(result.structured_content["status"], "unknown")
            self.assertEqual(result.structured_content["message"], "邮箱服务不稳定 请五分钟后查看结果")
            self.assertEqual(self.store._conn.execute('SELECT COUNT(*) FROM draft_claims').fetchone()[0], 0)
            replay = await self.call(client, **params)
            self.assertEqual(replay.structured_content["status"], "unknown")
            self.assertEqual(self.sends, ["draft-1"])
            self.send_error = None
            fresh = await self.preview(client)
            completed = await self.call(client, **fresh)
            self.assertEqual(completed.structured_content["status"], "completed")

    def test_restart_releases_only_orphaned_send_claims(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "operations.db")
            store = OperationStore(path)
            rows = []
            for tool in ("send_draft", "update_draft"):
                row = store.create_preview(tool=tool, mailbox=MAILBOX, action=tool,
                    business_params={"draft_id": tool}, items=[], details={"canonical_draft_id": tool})
                store.begin_draft_confirm(row["operation_id"], row["confirm_token"], {"draft_id": tool},
                    mailbox=MAILBOX, lanid="employee", worker_id="worker")
                rows.append(row)
            store.close()
            reopened = OperationStore(path)
            try:
                self.assertEqual([reopened.get(row["operation_id"])["status"] for row in rows], ["unknown", "unknown"])
                claims = reopened._conn.execute('SELECT canonical_draft_id FROM draft_claims').fetchall()
                self.assertEqual([row[0] for row in claims], ["update_draft"])
            finally:
                reopened.close()

    async def test_timeout_preserves_eventual_definite_rejection_receipt(self):
        await self.failed_background_send(ErrorAccessDenied("Submission rejected"),
                                          "failed", "SEND_REJECTED")

    async def test_oa_timeout_returns_no_foreign_receipt_and_never_sends(self):
        foreign = self.store.create_preview(
            tool="send_draft", mailbox="other@example.com", action="send_draft",
            business_params={"draft_id": "private-draft"},
            items=[{"secret": "PRIVATE_PREVIEW"}], details={},
        )
        self.store.mark(foreign["operation_id"], "completed", result={"secret": "PRIVATE_RECEIPT"})
        resolved = threading.Event()

        def reject_identity(*args):
            self.block()
            resolved.set()
            raise IdentityResolutionError("LANID did not resolve to a mailbox")

        with patch.object(mcp_server, "resolve_identity_by_lanid", side_effect=reject_identity):
            async with Client(mcp_server.mcp) as client:
                result = await self.blocked_response(client, operation_id=foreign["operation_id"])
                self.assert_processing(result, foreign["operation_id"])
                self.assertNotIn("PRIVATE", json.dumps(result.structured_content))
                self.release.set()
                await self.eventually(resolved.is_set)
        self.assertEqual(self.sends, [])
        self.assertEqual(self.store.get(foreign["operation_id"])["status"], "completed")

    async def test_preclaim_initialization_timeout_leaves_pending_until_worker_can_continue(self):
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)

            def initialize(*args):
                self.block()
                return self.business

            with patch.object(mcp_server, "OutlookClient", side_effect=initialize):
                result = await self.blocked_response(client, **params)
                self.assert_processing(result, params["operation_id"])
                self.assertEqual(self.store.get(params["operation_id"])["status"], "pending")
                self.assertEqual(self.sends, [])
                self.release.set()
                await self.wait_for_receipt(params["operation_id"], "completed")
        self.assertEqual(self.sends, ["draft-1"])

    async def test_preview_timeout_has_no_operation_token_or_send(self):
        def read_draft(*args, **kwargs):
            self.block()
            return self.draft

        self.business._tool_item.side_effect = read_draft
        async with Client(mcp_server.mcp) as client:
            result = await self.blocked_response(client, draft_id=self.draft.id,
                                                 idempotency_key="slow-preview")
            data = result.structured_content
            self.assertTrue(result.is_error)
            self.assertNotIn("ok", data)
            self.assertEqual(data["error_code"], "RESPONSE_WAIT_TIMEOUT")
            self.assertNotIn("operation_id", data)
            self.assertNotIn("confirm_token", data)
            self.assertNotEqual(data.get("status"), "processing")
            self.assertEqual(self.sends, [])
            self.release.set()
            await self.eventually(lambda: self.store.find_by_idempotency_key(
                "slow-preview", mailbox=MAILBOX) is not None)
        self.assertEqual(self.sends, [])

    async def test_quick_send_success_and_oa_error_keep_existing_contract(self):
        self.release.set()
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)
            result = await self.call(client, **params)
            self.assertFalse(result.is_error)
            self.assertNotIn("ok", result.structured_content)
            self.assertEqual(result.structured_content["status"], "completed")
            with patch.object(mcp_server, "resolve_identity_by_lanid",
                              side_effect=IdentityResolutionError("LANID not found")):
                rejected = await self.call(client, draft_id=self.draft.id)
        self.assertTrue(rejected.is_error)
        self.assertEqual(rejected.structured_content["error_code"], "IDENTITY_LOOKUP_FAILED")
        self.assertNotIn("wait_timed_out", rejected.structured_content)
        self.assertEqual(self.sends, ["draft-1"])

    async def test_other_tool_is_not_limited_by_send_response_wait(self):
        def read_folders():
            self.block()
            return {"folders": ["drafts", "sent"]}

        self.business.list_folders = read_folders
        async with Client(mcp_server.mcp) as client:
            task = asyncio.create_task(client.call_tool("list_folders", IDENTITY))
            self.tasks.append(task)
            await self.eventually(self.entered.is_set)
            await asyncio.sleep(0.15)
            self.assertFalse(task.done())
            self.release.set()
            result = await task
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["results"], {"folders": ["drafts", "sent"]})

    async def test_cancelled_mcp_send_still_persists_one_completed_receipt(self):
        with patch.object(mcp_server, "SEND_RESPONSE_WAIT_SECONDS", 3):
            async with Client(mcp_server.mcp) as client:
                params = await self.preview(client)
                request_id = client.session._request_id
                confirm = asyncio.create_task(self.call(client, **params))
                self.tasks.append(confirm)
                await self.eventually(self.entered.is_set)
                await client.session.send_notification(mcp_types.ClientNotification(
                    mcp_types.CancelledNotification(params=mcp_types.CancelledNotificationParams(
                        requestId=request_id, reason="Caller stopped waiting",
                    )),
                ))
                with self.assertRaisesRegex(McpError, "Request cancelled"):
                    await asyncio.wait_for(confirm, 0.6)
                self.assertEqual(self.store.get(params["operation_id"])["status"], "executing")
                self.assertFalse(self.release.is_set())
                self.release.set()
                await self.wait_for_receipt(params["operation_id"], "completed")
                receipt = await self.call(client, operation_id=params["operation_id"])
                replay = await self.call(client, **params)
        self.assertEqual(receipt.structured_content["status"], "completed")
        self.assertNotIn("ok", receipt.structured_content)
        self.assertTrue(replay.structured_content["replayed"])
        self.assertEqual(replay.structured_content["results"], receipt.structured_content["results"])
        self.assertEqual(self.sends, ["draft-1"])

    async def test_timed_out_send_keeps_worker_capacity_until_submission_finishes(self):
        async with Client(mcp_server.mcp) as client:
            params = await self.preview(client)
            limiter = to_thread.current_default_thread_limiter()
            previous_tokens = limiter.total_tokens
            limiter.total_tokens = 1
            try:
                first = await self.blocked_response(client, **params)
                self.assert_processing(first, params["operation_id"])
                self.assertEqual(self.store.get(params["operation_id"])["status"], "executing")
                # The query must queue behind the still-running submission.
                # Releasing the worker slot at response timeout would expose an
                # immediate executing receipt instead of a processing response.
                queued = await self.blocked_response(client, operation_id=params["operation_id"])
                self.assert_processing(queued, params["operation_id"])
                self.release.set()
                await self.wait_for_receipt(params["operation_id"], "completed")
                receipt = await self.call(client, operation_id=params["operation_id"])
            finally:
                self.release.set()
                limiter.total_tokens = previous_tokens
        self.assertNotIn("ok", receipt.structured_content)
        self.assertEqual(receipt.structured_content["status"], "completed")
        self.assertEqual(self.sends, ["draft-1"])


if __name__ == "__main__":
    unittest.main()
