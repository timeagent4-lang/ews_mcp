"""Offline contracts through real MCP transport, audit and SQLite receipts."""
import importlib.util
import inspect
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

from fastmcp import Client
from jsonschema import Draft202012Validator

from calendar_operations import CalendarOperations
from confirmation import OperationStore
from flag_operations import FlagOperations
from mail_operations import MailOperations
import outlook_client
from tool_specs import DISABLED_TOOLS, READ_TOOLS, SPECS, TOOLS
from tool_support import ToolOperationError
import utils.audit as audit_adapter
import utils.lanid_email as identity_adapter
from write_operations import WriteOperations


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"
READ_ARGUMENTS = {
    "list_folders": {},
    "find_message": {"query": "report", "folder": "sent", "limit": 3},
    "get_message": {"message_id": "message"},
    "get_thread": {"message_id": "message"},
    "get_attachment": {"message_id": "message"},
    "prepare_attachment_download": {"message_id": "message", "attachment_id": "attachment"},
    "get_mailbox_overview": {},
    "list_events": {"start": "2026-10-09", "end": "2026-10-10"},
    "get_event": {"event_id": "event"},
    "check_availability": {
        "start": "2026-10-09T09:00:00+08:00", "end": "2026-10-09T10:00:00+08:00",
        "attendees": ["attendee@example.com"],
    },
    "find_people": {"query": "colleague"},
    "get_contact": {"contact_id": "contact"},
    "list_tasks": {},
    "get_server_status": {},
    "list_flagged_messages": {},
}
WRITE_ARGUMENTS = {
    "create_draft": {"subject": "report", "body": "content"},
    "update_draft": {"draft_id": "draft", "subject": "updated"},
    "send_draft": {"draft_id": "draft"},
    "set_message_flag": {"message_id": "message", "flag": "complete"},
    "update_messages": {"ids": ["message"], "set_read": True},
    "move_messages": {"ids": ["message"], "to_folder": "inbox"},
    "create_event": {"subject": "meeting", "start": "2026-10-09T09:00:00+08:00"},
    "update_event": {"event_id": "event", "subject": "updated"},
    "respond_to_event": {"event_id": "event", "response": "accept"},
    "cancel_event": {"event_id": "event"},
    "create_contact": {"display_name": "contact", "email": "contact@example.com"},
    "create_task": {"subject": "task"},
    "update_task": {"task_id": "task", "complete": True},
}


def load_server(*, audit_logger=None):
    spec = importlib.util.spec_from_file_location(
        "contract_test_server", Path(__file__).resolve().parents[1] / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    with patch.object(identity_adapter, "resolve_identity_by_lanid",
                      side_effect=AssertionError("OA must be mocked")), \
            patch.object(outlook_client, "OutlookClient",
                         side_effect=AssertionError("Exchange must be mocked")), \
            patch.object(audit_adapter, "create_audit_logger", return_value=audit_logger or Mock()):
        spec.loader.exec_module(module)
    return module


class PublicSchemaTests(unittest.TestCase):
    def valid(self, tool, **params):
        return Draft202012Validator(SPECS[tool]["inputSchema"]).is_valid(
            {**IDENTITY, **params})

    def test_new_parameters_match_signatures_and_defaults(self):
        for tool, owner, fields in (
            ("find_message", MailOperations, ("query", "aqs", "folder")),
            ("list_flagged_messages", FlagOperations, ("folder", "status")),
            ("create_draft", WriteOperations, ("mode",)),
            ("respond_to_event", CalendarOperations, ("response",)),
        ):
            signature = inspect.signature(getattr(owner, tool))
            props = SPECS[tool]["inputSchema"]["properties"]
            for name in fields:
                with self.subTest(tool=tool, field=name):
                    self.assertIn(name, props)
                    default = signature.parameters[name].default
                    if default is not None and default is not inspect.Parameter.empty:
                        self.assertEqual(props[name]["default"], default)
        self.assertTrue(self.valid("find_message", aqs="subject:hello"))
        self.assertFalse(self.valid("respond_to_event", event_id="e"))
        self.assertFalse(self.valid("respond_to_event", event_id="e", response="yes"))
        for response in ("accept", "tentative", "decline"):
            self.assertTrue(self.valid("respond_to_event", event_id="e", response=response))
        for status in ("flagged", "complete", "all"):
            self.assertTrue(self.valid("list_flagged_messages", status=status))
        self.assertFalse(self.valid("list_flagged_messages", status="clear"))

    def test_known_business_schemas_only_expose_real_method_parameters(self):
        for name, spec in SPECS.items():
            signature = inspect.signature(getattr(outlook_client.OutlookClient, name)).parameters
            for field in TOOLS[name]:
                with self.subTest(tool=name, field=field["name"]):
                    self.assertIn(field["name"], signature)
                    if "default" in field:
                        self.assertEqual(field["default"], signature[field["name"]].default)
            Draft202012Validator.check_schema(spec["inputSchema"])

    def test_write_branches_for_every_registered_write(self):
        self.assertEqual(set(WRITE_ARGUMENTS), set(SPECS) - READ_TOOLS)
        for tool, business in WRITE_ARGUMENTS.items():
            with self.subTest(tool=tool):
                self.assertTrue(self.valid(tool, **business))
                self.assertTrue(self.valid(tool, **business, idempotency_key="key"))
                self.assertTrue(self.valid(tool, **business, operation_id="op", confirm_token="token"))
                self.assertTrue(self.valid(tool, operation_id="op"))
                self.assertFalse(self.valid(tool, **business, operation_id="op"))
                self.assertFalse(self.valid(tool, **business, confirm_token="token"))
                self.assertFalse(self.valid(tool, confirm_token="token"))
                self.assertFalse(self.valid(tool, operation_id=""))
                self.assertFalse(self.valid(tool, operation_id="op", idempotency_key="key"))
        self.assertTrue(self.valid("send_draft", draft_id="draft"))
        self.assertTrue(self.valid("send_draft", draft_id="draft", operation_id="op", confirm_token="token"))
        self.assertFalse(self.valid("send_draft", draft_id="draft", operation_id="op"))
        self.assertFalse(self.valid("send_draft", operation_id="op", confirm_token="token"))

    def test_every_tool_requires_flat_identity_and_rejects_internal_arguments(self):
        fixtures = {**READ_ARGUMENTS, **WRITE_ARGUMENTS}
        self.assertEqual(set(fixtures), set(SPECS))
        self.assertEqual(set(READ_ARGUMENTS), READ_TOOLS)
        for tool, business in fixtures.items():
            validator = Draft202012Validator(SPECS[tool]["inputSchema"])
            valid = {**IDENTITY, **business}
            with self.subTest(tool=tool):
                self.assertTrue(validator.is_valid(valid))
                self.assertFalse(validator.is_valid({"params": valid}))
                for field in ("lanid", "name"):
                    self.assertFalse(validator.is_valid({k: v for k, v in valid.items() if k != field}))
                    for value in ("", None, 123, True):
                        self.assertFalse(validator.is_valid({**valid, field: value}))
                for hidden in ("params", "ctx", "_tool", "expected_version", "directory_target"):
                    self.assertFalse(validator.is_valid({**valid, hidden: "injected"}))

    def test_draft_mode_and_aqs_conditional_contracts(self):
        for mode in ("reply", "reply_all", "forward"):
            self.assertFalse(self.valid("create_draft", mode=mode))
            self.assertTrue(self.valid("create_draft", mode=mode, reply_to="original"))
        self.assertTrue(self.valid("create_draft"))
        self.assertFalse(self.valid("create_draft", reply_to="original"))
        self.assertTrue(self.valid("find_message", query="hello", is_unread=False))
        for extra in ({"query": "hello"}, {"is_unread": False}, {"has_attachments": False},
                      {"since": "2026-01-01"}, {"until": "2026-01-01"}):
            self.assertFalse(self.valid("find_message", aqs="subject:hello", **extra))
        self.assertTrue(self.valid("find_message", aqs="subject:hello", query=""))

    def test_existing_implementation_bounds_and_nullable_fields(self):
        for tool in ("find_message", "get_thread", "get_mailbox_overview", "list_events"):
            required = {"message_id": "m"} if tool == "get_thread" else (
                {"start": "2026-01-01", "end": "2026-01-02"} if tool == "list_events" else {})
            for value in (0, -1, 101, True):
                self.assertFalse(self.valid(tool, **required, limit=value))
            self.assertTrue(self.valid(tool, **required, limit=100))
        self.assertFalse(self.valid("find_message", offset=-1))
        self.assertTrue(self.valid("find_message", offset=10001))
        self.assertTrue(self.valid("get_message", message_id="m", body_offset=100001))
        self.assertFalse(self.valid("get_message", message_id="m", max_body_chars=0))
        self.assertFalse(self.valid("get_message", message_id="m", body_offset=-1))
        self.assertFalse(self.valid("find_message", is_unread=None))
        self.assertFalse(self.valid("update_draft", draft_id="d", subject=None))
        for tool in ("update_messages", "move_messages"):
            extras = {"to_folder": "inbox"} if tool == "move_messages" else {}
            for ids in ([], [""], ["a", "a"], ["a"] * 51):
                self.assertFalse(self.valid(tool, ids=ids, **extras))
        self.assertNotIn("save", SPECS["get_attachment"]["inputSchema"]["properties"])
        self.assertTrue(set(DISABLED_TOOLS).isdisjoint(SPECS))


class SearchBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.mail = MailOperations()
        self.folder = MagicMock()
        self.folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.return_value = []
        self.mail._tool_folder = Mock(return_value=self.folder)

    def test_aqs_alone_uses_positional_exchange_filter(self):
        self.mail.find_message(aqs="subject:hello", folder="sent")
        self.folder.filter.assert_called_once_with("subject:hello")
        self.mail._tool_folder.assert_called_once_with("sent")

    def test_keyword_combines_with_structured_filters(self):
        self.mail.find_message(query="hello", is_unread=False, has_attachments=True)
        self.folder.filter.assert_called_once_with(subject__contains="hello", is_read=True, has_attachments=True)

    def test_mixed_aqs_rejected_before_exchange(self):
        for extra in ({"query": "x"}, {"is_unread": False}, {"has_attachments": False},
                      {"since": "2026-01-01"}, {"until": "2026-01-02"}):
            with self.assertRaises(ToolOperationError):
                self.mail.find_message(aqs="subject:x", **extra)
        self.folder.filter.assert_not_called()


class MCPContractTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        self.business = Mock(config=SimpleNamespace(email=MAILBOX))
        self.business.send_draft.side_effect = lambda **kw: (
            {"sent": True} if kw.get("confirm") else {"preview": {"items": [], "details": {}}})
        for patcher in (
            patch.dict(os.environ, {"EWS_MCP_DATA_DIR": self.tmp.name, "EWS_MCP_DOWNLOAD_ENABLED": "false"}),
            patch.object(self.server, "get_store", return_value=self.store),
            patch.object(self.server, "resolve_identity_by_lanid", return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(self.server.OutlookConfig, "from_service_env", return_value=self.business.config),
            patch.object(self.server, "OutlookClient", return_value=self.business),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def call(self, client, tool="send_draft", **params):
        return await client.call_tool(tool, {**IDENTITY, **params}, raise_on_error=False)

    def record(self, tool="send_draft", mailbox=MAILBOX, status="pending", key=None):
        row = self.store.create_preview(tool=tool, mailbox=mailbox, action=tool,
            business_params={"draft_id": "d"}, items=[{"private": "SECRET_PREVIEW"}],
            details={}, idempotency_key=key)
        if status != "pending":
            self.store.mark(row["operation_id"], status, result={"private": "SECRET_RESULT"})
        return row

    async def test_actual_mcp_entry_rejects_invalid_types_and_hidden_arguments(self):
        async with Client(self.server.mcp) as client:
            listed = await client.list_tools()
            self.assertEqual({t.name for t in listed}, set(READ_ARGUMENTS) | set(WRITE_ARGUMENTS))
            for t in listed:
                self.assertEqual(t.inputSchema, SPECS[t.name]["inputSchema"])
            self.assertEqual(len(listed), 28)
            for arguments, code in (
                ({**IDENTITY, "limit": 0}, "INVALID_PAGINATION"),
                ({**IDENTITY, "is_unread": "false"}, "INVALID_PARAMS"),
                ({**IDENTITY, "is_unread": None}, "INVALID_PARAMS"),
                ({**IDENTITY, "unknown": 1}, "INVALID_PARAMS"),
                ({**IDENTITY, "_tool": "send_draft"}, "INVALID_PARAMS"),
                ({**IDENTITY, "ctx": {}}, "INVALID_PARAMS"),
                ({**IDENTITY, "expected_version": {}}, "INVALID_PARAMS"),
                ({**IDENTITY, "directory_target": {}}, "INVALID_PARAMS"),
                ({"params": IDENTITY}, "INVALID_PARAMS"),
                ({**IDENTITY, "params": {}}, "INVALID_PARAMS"),
                ({}, "INVALID_PARAMS"),
                ({"params": None}, "INVALID_PARAMS"),
            ):
                result = await client.call_tool("find_message", arguments, raise_on_error=False)
                self.assertTrue(result.is_error, arguments)
                self.assertEqual(result.structured_content["error_code"], code)
            self.server.resolve_identity_by_lanid.assert_not_called()
            self.business.find_message.assert_not_called()

    async def test_all_registered_reads_keep_identity_out_of_business_arguments(self):
        async with Client(self.server.mcp) as client:
            for tool, business in READ_ARGUMENTS.items():
                with self.subTest(tool=tool):
                    method = Mock(return_value={"marker": tool})
                    setattr(self.business, tool, method)
                    self.server.tool_audit.logger.reset_mock()
                    result = await self.call(client, tool, **business)
                    self.assertFalse(result.is_error, result)
                    self.assertEqual(result.structured_content["results"], {"marker": tool})
                    method.assert_called_once_with(**business)
                    self.assertEqual(self.business.requester_lanid, "employee")
                    event = json.loads(self.server.tool_audit.logger.info.call_args.args[0])
                    self.assertEqual(event["identity_assurance"], "oa_name_match")
                    self.assertEqual(event["status"], "success")

    async def test_all_registered_writes_preview_confirm_and_replay_once(self):
        async with Client(self.server.mcp) as client:
            for tool, business in WRITE_ARGUMENTS.items():
                with self.subTest(tool=tool):
                    method = Mock(side_effect=lambda _tool=tool, **kw: (
                        {"applied": _tool} if kw.get("confirm")
                        else {"preview": {"items": [{"tool": _tool}], "details": {}}}))
                    setattr(self.business, tool, method)
                    preview = (await self.call(client, tool, **business)).structured_content
                    self.assertTrue(preview["confirmation_required"])
                    operation_id, token = preview["operation_id"], preview["confirm_token"]
                    stored = self.store.get(operation_id)
                    self.assertEqual(stored["status"], "pending")
                    self.assertEqual(stored["business_params"], business)
                    self.assertEqual(stored["mailbox"], MAILBOX)
                    method.assert_called_once_with(**business, confirm=False)
                    for replayed in (False, True):
                        result = await self.call(client, tool, **business,
                                                 operation_id=operation_id, confirm_token=token)
                        self.assertFalse(result.is_error, result)
                        self.assertEqual(result.structured_content["status"], "completed")
                        self.assertEqual(result.structured_content["results"], {"applied": tool})
                        self.assertEqual(result.structured_content["replayed"], replayed)
                    self.assertEqual(method.call_count, 2)
                    method.assert_called_with(**business, confirm=True, confirmation_id=token)
                    self.server.OutlookClient.reset_mock()
                    receipt = await self.call(client, tool, operation_id=operation_id)
                    self.assertEqual(receipt.structured_content["results"], {"applied": tool})
                    self.server.OutlookClient.assert_not_called()

    async def test_submission_failures_keep_classification_and_never_resubmit_on_query(self):
        from exchangelib.errors import ErrorAccessDenied
        cases = (
            (ErrorAccessDenied("denied"), "ErrorAccessDenied", "exchange", "failed"),
            (TimeoutError("timed out"), "UPSTREAM_TIMEOUT", "transport", "unknown"),
            (TypeError("SECRET_PROGRAM_DETAIL"), "INTERNAL_ERROR", "program", "unknown"),
        )
        async with Client(self.server.mcp) as client:
            for exc, code, kind, status in cases:
                with self.subTest(error=code):
                    self.business.send_draft.side_effect = lambda **kw: {
                        "preview": {"items": [], "details": {}}}
                    preview = (await self.call(client, draft_id="d")).structured_content
                    self.business.send_draft.reset_mock()
                    self.business.send_draft.side_effect = exc
                    confirm = {"draft_id": "d", "operation_id": preview["operation_id"],
                               "confirm_token": preview["confirm_token"]}
                    result = await self.call(client, **confirm)
                    self.assertTrue(result.is_error)
                    self.assertEqual(result.structured_content["error_code"], code)
                    self.assertEqual(result.structured_content["error_kind"], kind)
                    self.assertNotIn("SECRET_PROGRAM_DETAIL", str(result))
                    self.assertEqual(self.store.get(preview["operation_id"])["status"], status)
                    replay_params = [{"operation_id": preview["operation_id"]}]
                    if status == "unknown":
                        replay_params.append(confirm)
                    for params in replay_params:
                        replay = await self.call(client, **params)
                        self.assertTrue(replay.is_error)
                        self.assertEqual(replay.structured_content["status"], status)
                        self.assertEqual(replay.structured_content["error_code"], code)
                        self.assertNotIn("SECRET_PROGRAM_DETAIL", str(replay))
                        if kind == "exchange":
                            self.assertEqual(replay.structured_content["error_code_normalized"],
                                             "EXCHANGE_ACCESS_DENIED")
                    self.business.send_draft.assert_called_once()

    async def test_batch_verdicts_survive_receipt_query_and_confirmation_replay(self):
        cases = (
            ([{"success": True}, {"success": True}], "completed", None, 2, 0),
            ([{"success": True}, {"success": False}], "partial", "BATCH_PARTIAL_FAILURE", 1, 1),
            ([{"success": False}, {"success": False}], "failed", "BATCH_FAILED", 0, 2),
        )
        async with Client(self.server.mcp) as client:
            for rows, status, code, successes, failures in cases:
                with self.subTest(status=status):
                    self.business.update_messages.side_effect = lambda **kw: {
                        "preview": {"items": [], "details": {}}}
                    business = {"ids": ["one", "two"], "set_read": True}
                    preview = (await self.call(client, "update_messages", **business)).structured_content
                    self.business.update_messages.reset_mock()
                    self.business.update_messages.side_effect = None
                    self.business.update_messages.return_value = {"results": rows}
                    confirm = {**business, "operation_id": preview["operation_id"],
                               "confirm_token": preview["confirm_token"]}
                    for params in (confirm, {"operation_id": preview["operation_id"]}, confirm):
                        self.server.tool_audit.logger.reset_mock()
                        result = await self.call(client, "update_messages", **params)
                        payload = result.structured_content
                        self.assertEqual(result.is_error, failures != 0)
                        self.assertEqual(payload["ok"], failures == 0)
                        self.assertEqual(payload["status"], status)
                        self.assertEqual(payload.get("error_code"), code)
                        self.assertEqual(payload["results"], {"results": rows})
                        if failures:
                            self.assertEqual(payload["success_count"], successes)
                            self.assertEqual(payload["failed_count"], failures)
                        event = json.loads(self.server.tool_audit.logger.info.call_args.args[0])
                        self.assertEqual(event["status"], "failure" if failures else "success")
                        self.assertEqual(event["error_code"], code)
                    self.assertEqual(self.store.get(preview["operation_id"])["status"], status)
                    self.business.update_messages.assert_called_once()

    async def test_preview_query_confirm_replay_only_executes_once(self):
        async with Client(self.server.mcp) as client:
            preview = (await self.call(client, draft_id="d")).structured_content
            op, token = preview["operation_id"], preview["confirm_token"]
            self.business.send_draft.reset_mock()
            self.server.OutlookClient.reset_mock()
            query = await self.call(client, operation_id=op)
            self.assertEqual(query.structured_content["status"], "pending")
            self.server.OutlookClient.assert_not_called()
            self.business.send_draft.assert_not_called()
            self.assertEqual(self.server.resolve_identity_by_lanid.call_count, 2)
            for _ in range(2):
                result = await self.call(client, draft_id="d", operation_id=op, confirm_token=token)
                self.assertEqual(result.structured_content["status"], "completed")
            self.business.send_draft.assert_called_once_with(draft_id="d", confirm=True, confirmation_id=token)

    async def test_oa_failure_blocks_receipt_query(self):
        row = self.record(status="completed")
        with patch.object(self.store, "get", wraps=self.store.get) as lookup:
            async with Client(self.server.mcp) as client:
                for exc, code, assurance in (
                    (self.server.IdentityResolutionError("OA unavailable"),
                     "IDENTITY_LOOKUP_FAILED", "oa_lookup_failed"),
                    (self.server.IdentityNameMismatchError("name mismatch"),
                     "IDENTITY_NAME_MISMATCH", "oa_name_mismatch"),
                ):
                    with self.subTest(error=code):
                        self.server.resolve_identity_by_lanid.side_effect = exc
                        self.server.tool_audit.logger.reset_mock()
                        result = await self.call(client, operation_id=row["operation_id"])
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error_code"], code)
                        self.assertEqual(result.structured_content["error_kind"], "identity")
                        self.assertNotIn("SECRET", str(result))
                        event = json.loads(self.server.tool_audit.logger.info.call_args.args[0])
                        self.assertEqual(event["identity_assurance"], assurance)
                        self.assertEqual(event["status"], "rejected")
            lookup.assert_not_called()
        self.server.OutlookClient.assert_not_called()

    async def test_directory_actor_uses_normalized_employee_lanid(self):
        self.business.get_server_status.return_value = {"ready": True}
        async with Client(self.server.mcp) as client:
            result = await self.call(client, "get_server_status", lanid=" DOMAIN\\Employee ")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["results"], {"ready": True})
        self.assertEqual(self.business.requester_lanid, "Employee")
        self.server.resolve_identity_by_lanid.assert_called_once_with(" DOMAIN\\Employee ", "员工")

    async def test_receipt_confirmation_and_terminal_replay_are_scoped(self):
        async with Client(self.server.mcp) as client:
            for scope in ({"mailbox": "other@example.com"}, {"tool": "update_draft"}):
                for status in ("pending", "completed", "partial", "unknown", "executing", "failed"):
                    row = self.record(status=status, **scope)
                    for extra in ({}, {"draft_id": "d", "confirm_token": row["confirm_token"]}):
                        result = await self.call(client, operation_id=row["operation_id"], **extra)
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error_code"], "OPERATION_SCOPE_MISMATCH")
                        self.assertNotIn("SECRET", str(result))
                        self.assertNotIn(row["confirm_token"], str(result))
            self.business.send_draft.assert_not_called()

    async def test_cross_tool_idempotency_replay_is_rejected(self):
        async with Client(self.server.mcp) as client:
            for status in ("pending", "completed"):
                self.record(tool="update_draft", status=status, key=status)
                result = await self.call(client, draft_id="d", idempotency_key=status)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error_code"], "OPERATION_SCOPE_MISMATCH")
                self.assertNotIn("SECRET", str(result))
            self.business.send_draft.assert_not_called()

    async def test_same_key_is_independent_across_mailboxes(self):
        other = self.record(mailbox="other@example.com", status="completed", key="key")
        async with Client(self.server.mcp) as client:
            result = await self.call(client, draft_id="d", idempotency_key="key")
        self.assertTrue(result.structured_content["confirmation_required"])
        self.assertNotEqual(result.structured_content["operation_id"], other["operation_id"])
        self.assertNotIn("SECRET", str(result))
        self.business.send_draft.assert_called_once_with(draft_id="d", confirm=False)

    async def test_mailbox_variants_replay_existing_idempotency_records(self):
        async with Client(self.server.mcp) as client:
            for status in ("pending", "completed", "executing", "unknown"):
                row = self.record(status=status, key=status)
                for email in ("Employee@Example.COM", " employee@example.com "):
                    self.server.resolve_identity_by_lanid.return_value = SimpleNamespace(email=email)
                    result = await self.call(client, draft_id="d", idempotency_key=status)
                    self.assertEqual(result.structured_content["operation_id"], row["operation_id"])
                    self.assertTrue(result.structured_content["replayed"])
        self.server.OutlookClient.assert_not_called()
        self.business.send_draft.assert_not_called()

    async def test_preview_stores_canonical_mailbox_and_confirms_once_across_variants(self):
        async with Client(self.server.mcp) as client:
            self.server.resolve_identity_by_lanid.return_value = SimpleNamespace(email=" Employee@Example.COM ")
            preview = (await self.call(client, draft_id="d", idempotency_key="key")).structured_content
            op = preview["operation_id"]
            self.assertEqual(self.store.get(op)["mailbox"], "employee@example.com")
            self.server.OutlookConfig.from_service_env.assert_called_once_with("employee@example.com")
            self.business.send_draft.reset_mock()
            for email in ("employee@example.com", " EMPLOYEE@example.com "):
                self.server.resolve_identity_by_lanid.return_value = SimpleNamespace(email=email)
                query = await self.call(client, operation_id=op)
                self.assertFalse(query.is_error)
                result = await self.call(client, draft_id="d", operation_id=op,
                                         confirm_token=preview["confirm_token"])
                self.assertEqual(result.structured_content["status"], "completed")
                replay = await self.call(client, draft_id="d", idempotency_key="key")
                self.assertEqual(replay.structured_content["operation_id"], op)
            self.business.send_draft.assert_called_once_with(
                draft_id="d", confirm=True, confirmation_id=preview["confirm_token"])

    async def test_invalid_oa_mailbox_is_rejected_before_receipt_lookup(self):
        row = self.record(status="completed")
        with patch.object(self.store, "get", wraps=self.store.get) as lookup:
            async with Client(self.server.mcp) as client:
                for email in (None, "", "   ", "no-address", 123):
                    self.server.resolve_identity_by_lanid.return_value = SimpleNamespace(email=email)
                    result = await self.call(client, operation_id=row["operation_id"])
                    self.assertTrue(result.is_error)
                    self.assertNotIn("SECRET", str(result))
            lookup.assert_not_called()
        self.server.OutlookClient.assert_not_called()

    async def test_idempotency_record_mailbox_defense_before_return(self):
        row = self.record(mailbox="other@example.com", key="key")
        with patch.object(self.store, "find_by_idempotency_key", return_value=self.store.get(row["operation_id"])):
            async with Client(self.server.mcp) as client:
                result = await self.call(client, draft_id="d", idempotency_key="key")
        self.assertEqual(result.structured_content["error_code"], "OPERATION_SCOPE_MISMATCH")
        self.assertNotIn("SECRET", str(result))
        self.business.send_draft.assert_not_called()

    async def test_expired_preview_rejected_and_idempotency_builds_fresh_preview(self):
        row = self.record(key="expired")
        self.store._conn.execute("UPDATE operations SET created_at=? WHERE operation_id=?",
                                 ("2000-01-01T00:00:00+00:00", row["operation_id"]))
        self.store._conn.commit()
        with patch.object(self.server, "preview_ttl_seconds", return_value=60):
            async with Client(self.server.mcp) as client:
                result = await self.call(client, draft_id="d", operation_id=row["operation_id"],
                                         confirm_token=row["confirm_token"])
                self.assertEqual(result.structured_content["error_code"], "PREVIEW_EXPIRED")
                self.business.send_draft.assert_not_called()
                result = await self.call(client, draft_id="d", idempotency_key="expired")
                self.assertNotEqual(result.structured_content["operation_id"], row["operation_id"])
                self.assertTrue(result.structured_content["confirmation_required"])

    async def test_preview_expiring_during_exchange_connection_never_executes(self):
        row = self.record()
        now = datetime.now(timezone.utc)
        def slow_connection(config):
            nonlocal now
            now += timedelta(seconds=120)
            return self.business
        self.server.OutlookClient.side_effect = slow_connection
        with patch.object(self.server, "preview_ttl_seconds", return_value=60), \
                patch.object(self.server, "datetime", wraps=datetime) as clock:
            clock.now.side_effect = lambda tz: now
            async with Client(self.server.mcp) as client:
                result = await self.call(client, draft_id="d", operation_id=row["operation_id"],
                                         confirm_token=row["confirm_token"])
        self.assertEqual(result.structured_content.get("error_code"), "PREVIEW_EXPIRED")
        self.assertEqual(self.store.get(row["operation_id"])["status"], "pending")
        self.business.send_draft.assert_not_called()

    async def test_valid_idempotency_and_uncertain_confirm_never_reexecute(self):
        async with Client(self.server.mcp) as client:
            for status in ("pending", "completed", "executing", "unknown", "partial", "failed"):
                row = self.record(status=status, key=status)
                result = await self.call(client, draft_id="d", idempotency_key=status)
                self.assertTrue(result.structured_content["replayed"])
                self.assertEqual(result.structured_content["operation_id"], row["operation_id"])
                if status in ("executing", "unknown", "partial"):
                    result = await self.call(client, draft_id="d", operation_id=row["operation_id"],
                                             confirm_token=row["confirm_token"])
                    self.assertEqual(result.structured_content["status"], status)
        self.business.send_draft.assert_not_called()

    async def test_confirm_requires_identical_params_and_token_even_after_completion(self):
        async with Client(self.server.mcp) as client:
            for status in ("pending", "completed"):
                row = self.record(status=status)
                for draft, token in (("other", row["confirm_token"]), ("d", "incorrect"), ("d", "非原始令牌")):
                    result = await self.call(client, draft_id=draft, operation_id=row["operation_id"], confirm_token=token)
                    self.assertEqual(result.structured_content["error_code"], "CONFIRMATION_MISMATCH")
        self.business.send_draft.assert_not_called()

    async def test_exposed_search_arguments_reach_real_business_method(self):
        mail = MailOperations()
        folder = MagicMock()
        folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.return_value = []
        mail._tool_folder = Mock(return_value=folder)
        self.business.find_message.side_effect = mail.find_message
        async with Client(self.server.mcp) as client:
            for params in ({"aqs": "subject:x", "folder": "sent"},
                           {"query": "x", "is_unread": False}):
                result = await self.call(client, "find_message", **params)
                self.assertFalse(result.is_error)
            self.assertEqual(folder.filter.call_count, 2)
            result = await self.call(client, "find_message", aqs="subject:x", has_attachments=False)
            self.assertEqual(result.structured_content["error_code"], "INVALID_PARAMS")
            self.assertEqual(folder.filter.call_count, 2)

    async def test_search_lookahead_error_keeps_exchange_error_at_mcp_entry(self):
        from exchangelib.errors import ErrorAccessDenied
        mail = MailOperations()
        folder = MagicMock()
        folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.return_value = [
            SimpleNamespace(id="m"), ErrorAccessDenied("denied"),
        ]
        mail._tool_folder = Mock(return_value=folder)
        mail._tool_item = Mock(return_value=SimpleNamespace(id="m"))
        mail._message_dict = Mock(return_value={"id": "m"})
        self.business.find_message.side_effect = mail.find_message
        async with Client(self.server.mcp) as client:
            result = await self.call(client, "find_message", limit=1)
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error_code"], "ErrorAccessDenied")
        self.assertEqual(result.structured_content["error_kind"], "exchange")
        self.assertIsNone(result.structured_content["results"])
        mail._tool_item.assert_not_called()

    async def test_empty_thread_at_mcp_entry_returns_empty_results_and_coverage(self):
        mail = MailOperations()
        folder = MagicMock()
        folder.filter.return_value.only.return_value.order_by.return_value.__getitem__.return_value = []
        mail._tool_folder = Mock(return_value=folder)
        mail._tool_item = Mock(return_value=SimpleNamespace(conversation_id=SimpleNamespace(id="conversation")))
        self.business.get_thread.side_effect = mail.get_thread
        async with Client(self.server.mcp) as client:
            result = await self.call(client, "get_thread", message_id="source")
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content["results"], [])
        self.assertEqual(result.structured_content["coverage"], {"inbox": "ok", "sent": "ok"})
        self.assertFalse(result.structured_content["partial"])
        self.assertIsNone(result.structured_content["next_offset"])

    async def test_invalid_confirmation_combinations_never_reach_business(self):
        async with Client(self.server.mcp) as client:
            for params in ({"confirm_token": "t", "draft_id": "d"},
                           {"operation_id": "op", "draft_id": "d"},
                           {"operation_id": "op", "confirm_token": "t"}):
                result = await self.call(client, **params)
                self.assertTrue(result.is_error)
                self.assertEqual(result.structured_content["error_code"], "INVALID_PARAMS")
        self.business.send_draft.assert_not_called()


class AuditEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejected_raw_arguments_reach_audit_hooks_without_business_execution(self):
        audit_logger = Mock()
        server = load_server(audit_logger=audit_logger)
        cases = [
            ("get_message", IDENTITY, "INVALID_PARAMS"),
            ("find_message", {**IDENTITY, "is_unread": "false"}, "INVALID_PARAMS"),
            ("find_message", {**IDENTITY, "_tool": "send_draft"}, "INVALID_PARAMS"),
            ("find_message", {**IDENTITY, "limit": 0}, "INVALID_PAGINATION"),
            ("find_message", {"params": IDENTITY}, "INVALID_PARAMS"),
            ("find_message", {**IDENTITY, "params": IDENTITY}, "INVALID_PARAMS"),
            ("find_message", {}, "INVALID_PARAMS"),
            ("find_message", {"params": None}, "INVALID_PARAMS"),
            ("send_draft", {**IDENTITY, "confirm_token": "SECRET_TOKEN"}, "INVALID_PARAMS"),
            ("unregistered_tool", IDENTITY, "UNKNOWN_TOOL"),
            *[(tool, IDENTITY, "UNKNOWN_TOOL") for tool in sorted(DISABLED_TOOLS)],
        ]
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}):
            async with Client(server.mcp) as client:
                for tool, arguments, code in cases:
                    with self.subTest(tool=tool, arguments=arguments):
                        audit_logger.reset_mock()
                        result = await client.call_tool(tool, arguments, raise_on_error=False)
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error_code"], code)
                        self.assertNotIn("SECRET_TOKEN", str(result))
                        audit_logger.info.assert_called_once()
                        serialized = audit_logger.info.call_args.args[0]
                        event = json.loads(serialized)
                        self.assertEqual(event["tool"], tool)
                        self.assertEqual(event["error_code"], code)
                        self.assertEqual(event["status"], "failure")
                        self.assertEqual(event["identity_assurance"], "not_verified")
                        self.assertEqual(event["lanid"], arguments.get("lanid"))
                        self.assertEqual(event["name"], arguments.get("name"))
                        self.assertNotIn("SECRET_TOKEN", serialized)
        server.resolve_identity_by_lanid.assert_not_called()
        server.OutlookClient.assert_not_called()


if __name__ == "__main__":
    unittest.main()
