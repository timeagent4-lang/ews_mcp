"""Offline contract tests. Missing deployment adapters are stubbed only at import.

The real FastMCP registry, MCP Client transport, dispatcher and SQLite store run;
OA, audit integration and Exchange connectivity are not integration-tested here.
"""
import importlib.util
import inspect
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import MagicMock, Mock, patch

import mcp.types as mcp_types
from fastmcp import Client
from fastmcp.server.middleware import Middleware
from jsonschema import Draft202012Validator

from calendar_operations import CalendarOperations
from confirmation import OperationStore
from flag_operations import FlagOperations
from mail_operations import MailOperations
from tool_specs import DISABLED_TOOLS, SPECS, TOOLS
from tool_support import ToolOperationError
from write_operations import WriteOperations


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"


def load_server(*, audit_middleware=Middleware, audit_fallback=None):
    audit = ModuleType("utils.audit")
    audit.AUDIT_IDENTITY_STATE_KEY = "identity"
    audit.ToolAuditMiddleware = audit_middleware
    audit.install_tool_audit_fallback = audit_fallback or (lambda *args: None)
    identity = ModuleType("utils.lanid_email")
    identity.IdentityResolutionError = type("IdentityResolutionError", (ValueError,), {})
    identity.IdentityNameMismatchError = type("IdentityNameMismatchError", (ValueError,), {})
    identity.resolve_identity_by_lanid = Mock(side_effect=AssertionError("OA must be mocked"))
    outlook = ModuleType("outlook_client")
    outlook.OutlookClient = Mock(side_effect=AssertionError("Exchange must be mocked"))
    spec = importlib.util.spec_from_file_location(
        "contract_test_server", Path(__file__).resolve().parents[1] / "mcp_server.py")
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {"utils": ModuleType("utils"), "utils.audit": audit,
                                "utils.lanid_email": identity, "outlook_client": outlook}):
        spec.loader.exec_module(module)
    return module


class PublicSchemaTests(unittest.TestCase):
    def valid(self, tool, **params):
        return Draft202012Validator(SPECS[tool]["inputSchema"]).is_valid(
            {"params": {**IDENTITY, **params}})

    def test_new_parameters_match_signatures_and_defaults(self):
        for tool, owner, fields in (
            ("find_message", MailOperations, ("query", "aqs", "folder")),
            ("list_flagged_messages", FlagOperations, ("folder", "status")),
            ("create_draft", WriteOperations, ("mode",)),
            ("respond_to_event", CalendarOperations, ("response",)),
        ):
            signature = inspect.signature(getattr(owner, tool))
            props = SPECS[tool]["inputSchema"]["properties"]["params"]["properties"]
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
        owners = (MailOperations, WriteOperations, FlagOperations, CalendarOperations)
        for name, spec in SPECS.items():
            owner = next((c for c in owners if hasattr(c, name)), None)
            if owner is None:
                continue  # Missing deployment modules cannot be verified offline.
            signature = inspect.signature(getattr(owner, name)).parameters
            for field in TOOLS[name]:
                with self.subTest(tool=name, field=field["name"]):
                    self.assertIn(field["name"], signature)
                    if "default" in field:
                        self.assertEqual(field["default"], signature[field["name"]].default)
            Draft202012Validator.check_schema(spec["inputSchema"])

    def test_write_branches_for_every_registered_write(self):
        from tool_specs import READ_TOOLS
        for tool in set(SPECS) - READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(self.valid(tool, operation_id="op"))
                self.assertFalse(self.valid(tool, confirm_token="token"))
                self.assertFalse(self.valid(tool, operation_id=""))
                self.assertFalse(self.valid(tool, operation_id="op", idempotency_key="key"))
        self.assertTrue(self.valid("send_draft", draft_id="draft"))
        self.assertTrue(self.valid("send_draft", draft_id="draft", operation_id="op", confirm_token="token"))
        self.assertFalse(self.valid("send_draft", draft_id="draft", operation_id="op"))
        self.assertFalse(self.valid("send_draft", operation_id="op", confirm_token="token"))

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
        self.assertNotIn("save", SPECS["get_attachment"]["inputSchema"]["properties"]["params"]["properties"])
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
        return await client.call_tool(tool, {"params": {**IDENTITY, **params}}, raise_on_error=False)

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
            self.assertEqual({t.name for t in listed}, set(SPECS))
            for t in listed:
                self.assertEqual(t.inputSchema, SPECS[t.name]["inputSchema"])
            for arguments in (
                {"params": {**IDENTITY, "limit": 0}},
                {"params": {**IDENTITY, "is_unread": "false"}},
                {"params": {**IDENTITY, "is_unread": None}},
                {"params": {**IDENTITY, "unknown": 1}},
                {"params": IDENTITY, "_tool": "send_draft"},
                {}, {"params": None},
            ):
                result = await client.call_tool("find_message", arguments, raise_on_error=False)
                self.assertTrue(result.is_error, arguments)
                self.assertEqual(result.structured_content["error_code"], "INVALID_PARAMS")
            self.server.resolve_identity_by_lanid.assert_not_called()
            self.business.find_message.assert_not_called()

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
        self.server.resolve_identity_by_lanid.side_effect = self.server.IdentityResolutionError("OA unavailable")
        async with Client(self.server.mcp) as client:
            result = await self.call(client, operation_id=row["operation_id"])
        self.assertTrue(result.is_error)
        self.assertEqual(result.structured_content["error_code"], "IDENTITY_LOOKUP_FAILED")
        self.assertNotIn("SECRET", str(result))

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
        # Test spies replace only the absent audit adapter. The actual MCP
        # transport, middleware chain, schema validator and envelope run.
        events = []

        class RecordingAudit(Middleware):
            async def on_call_tool(self, context, call_next):
                result = await call_next(context)
                events.append(("middleware", result.structured_content["error_code"]))
                return result

        def install_fallback(server, audit):
            handlers = server._mcp_server.request_handlers
            original = handlers[mcp_types.CallToolRequest]

            async def fallback(request):
                result = await original(request)
                events.append(("fallback", result.root.structuredContent["error_code"]))
                return result

            handlers[mcp_types.CallToolRequest] = fallback

        server = load_server(audit_middleware=RecordingAudit, audit_fallback=install_fallback)
        cases = [
            ("get_message", {"params": IDENTITY}, "INVALID_PARAMS"),
            ("find_message", {"params": {**IDENTITY, "is_unread": "false"}}, "INVALID_PARAMS"),
            ("find_message", {"params": IDENTITY, "_tool": "send_draft"}, "INVALID_PARAMS"),
            ("find_message", {}, "INVALID_PARAMS"),
            ("find_message", {"params": None}, "INVALID_PARAMS"),
            ("send_draft", {"params": {**IDENTITY, "confirm_token": "SECRET_TOKEN"}}, "INVALID_PARAMS"),
            ("unregistered_tool", {"params": IDENTITY}, "UNKNOWN_TOOL"),
            (next(iter(DISABLED_TOOLS)), {"params": IDENTITY}, "UNKNOWN_TOOL"),
        ]
        with patch.dict(os.environ, {"EWS_MCP_DOWNLOAD_ENABLED": "false"}):
            async with Client(server.mcp) as client:
                for tool, arguments, code in cases:
                    with self.subTest(tool=tool, arguments=arguments):
                        events.clear()
                        result = await client.call_tool(tool, arguments, raise_on_error=False)
                        self.assertTrue(result.is_error)
                        self.assertEqual(result.structured_content["error_code"], code)
                        self.assertNotIn("SECRET_TOKEN", str(result))
                        self.assertEqual(events, [("middleware", code), ("fallback", code)])
        server.resolve_identity_by_lanid.assert_not_called()
        server.OutlookClient.assert_not_called()


if __name__ == "__main__":
    unittest.main()
