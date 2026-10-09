"""Offline HTML draft contracts using real Exchange reply objects and XML."""

import importlib.util
import os
from pathlib import Path
import sys
import tempfile
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from exchangelib import Account, Body, HTMLBody, Mailbox, Message
from exchangelib.folders import Drafts
from exchangelib.items.base import BaseReplyItem
from exchangelib.version import Build, Version
from fastmcp import Client
from fastmcp.server.middleware import Middleware
from jsonschema import Draft202012Validator

from confirmation import OperationStore
from tool_specs import SPECS
from tool_support import ToolOperationError
from write_operations import WriteOperations


IDENTITY = {"lanid": "employee", "name": "员工"}
MAILBOX = "employee@example.com"
MODES = ("reply", "reply_all", "forward")
NS = {"t": "http://schemas.microsoft.com/exchange/services/2006/types"}
FRAGMENT = ('<table style="color:red"><tr><td>A &amp; B</td></tr></table>'
            '<p>新增</p><img src="https://example.com/logo.png">')


class OfflineDraftFixture:
    def setUp(self):
        # Account is the external boundary. Message creation and XML are real;
        # neither OA lookup nor an Exchange connection is allowed in this suite.
        self.account = Mock(spec=Account)
        self.account.primary_smtp_address = MAILBOX
        self.account.version = Version(build=Build(15, 2, 0, 0))
        self.folder = Drafts(id="drafts", changekey="drafts-key")
        self.ops = WriteOperations()
        self.ops.config = SimpleNamespace(email=MAILBOX)
        self.ops.account = self.account
        self.ops._tool_folder = Mock(return_value=self.folder)
        self.ops._tool_item = Mock()
        self.saved = []

        def save(draft, *args, **kwargs):
            self.saved.append(draft.to_xml(version=self.account.version))
            return SimpleNamespace(id="saved-draft", changekey="saved-key")

        for target in (BaseReplyItem, Message):
            patcher = patch.object(target, "save", autospec=True, side_effect=save)
            patcher.start()
            self.addCleanup(patcher.stop)

    def original(self, body):
        original = Message(
            account=self.account, id="original", changekey="original-key",
            subject="Original", body=body,
            author=Mailbox(email_address="sender@example.com"),
            to_recipients=[Mailbox(email_address=MAILBOX)],
            cc_recipients=[Mailbox(email_address="copied@example.com")],
        )
        self.ops._tool_item.return_value = original
        return original

    def create(self, mode, **kwargs):
        return self.ops.create_draft(
            mode=mode, reply_to="original", to_emails="recipient@example.com",
            confirm=True, **kwargs,
        )


class HTMLDraftSDKTests(OfflineDraftFixture, unittest.TestCase):
    def test_html_fragment_is_native_new_body_for_all_modes_and_original_formats(self):
        for mode in MODES:
            for source in (Body("ORIGINAL TEXT"), HTMLBody("<p>ORIGINAL HTML</p>")):
                with self.subTest(mode=mode, original_type=type(source).__name__):
                    self.original(source)
                    result = self.create(mode, body=FRAGMENT, body_format="html")
                    self.assertTrue(result["created"])
                    xml = self.saved[-1]
                    self.assertEqual(xml.tag.rsplit("}", 1)[-1], {
                        "reply": "ReplyToItem", "reply_all": "ReplyAllToItem",
                        "forward": "ForwardItem",
                    }[mode])
                    new_body = xml.find("t:NewBodyContent", NS)
                    self.assertEqual(new_body.get("BodyType"), "HTML")
                    self.assertEqual(new_body.text, FRAGMENT)
                    reference = xml.find("t:ReferenceItemId", NS)
                    self.assertEqual(reference.get("Id"), "original")
                    self.assertEqual(reference.get("ChangeKey"), "original-key")
                    self.assertNotIn("ORIGINAL", new_body.text)
                    self.assertIsNone(xml.find("t:Body", NS))

    def test_default_text_preserves_original_body_type_and_escapes_html(self):
        for mode in MODES:
            for source, body_type, expected in (
                (Body("ORIGINAL TEXT"), "Text", "<b>A & B</b>\nNext"),
                (HTMLBody("<p>ORIGINAL HTML</p>"), "HTML", "&lt;b&gt;A &amp; B&lt;/b&gt;<br>Next"),
            ):
                with self.subTest(mode=mode, original_type=body_type):
                    self.original(source)
                    self.create(mode, body="<b>A & B</b>\nNext")
                    new_body = self.saved[-1].find("t:NewBodyContent", NS)
                    self.assertEqual(new_body.get("BodyType"), body_type)
                    self.assertEqual(new_body.text, expected)

    def test_empty_and_whitespace_html_reply_bodies_are_allowed(self):
        for mode in MODES:
            for body in ("", " \n\t "):
                with self.subTest(mode=mode, body=body):
                    self.original(Body("ORIGINAL TEXT"))
                    self.create(mode, body=body, body_format="html")
                    new_body = self.saved[-1].find("t:NewBodyContent", NS)
                    self.assertEqual(new_body.get("BodyType"), "HTML")
                    self.assertEqual(new_body.text or "", body)

    def test_html_reply_rejects_document_markers_before_saving(self):
        for mode in MODES:
            for body in ("<html><p>Full</p></html>", "<HEAD><style>x</style></HEAD>",
                         "<body>Full</body>", "<!DOCTYPE html><p>Full</p>",
                         "</html>", "</head>", "</body>", "<p>ok</p></body>"):
                with self.subTest(mode=mode, body=body):
                    self.original(HTMLBody("<p>ORIGINAL HTML</p>"))
                    with self.assertRaises(ToolOperationError) as caught:
                        self.create(mode, body=body, body_format="html")
                    self.assertEqual(caught.exception.code, "HTML_FRAGMENT_REQUIRED")
        self.assertEqual(self.saved, [])

    def test_new_html_draft_still_accepts_a_complete_document(self):
        document = '<!DOCTYPE html><html><head><style>p{color:red}</style></head><body><p>New</p></body></html>'
        result = self.ops.create_draft(body=document, body_format="html", confirm=True)
        self.assertTrue(result["created"])
        body = self.saved[-1].find("t:Body", NS)
        self.assertEqual(body.get("BodyType"), "HTML")
        self.assertEqual(body.text, document)

    def test_html_reply_preview_has_no_save_and_reports_format(self):
        self.original(HTMLBody("<p>ORIGINAL HTML</p>"))
        preview = self.ops.create_draft(
            mode="reply", reply_to="original", body=FRAGMENT, body_format="html",
        )
        self.assertTrue(preview["confirmation_required"])
        self.assertEqual(preview["preview"]["items"][0]["body_format"], "html")
        self.assertEqual(preview["preview"]["items"][0]["body_preview"], FRAGMENT)
        self.assertEqual(self.saved, [])


class HTMLDraftSchemaTests(unittest.TestCase):
    def setUp(self):
        self.validator = Draft202012Validator(SPECS["create_draft"]["inputSchema"])

    def test_html_reply_preview_confirm_and_receipt_are_valid_flat_arguments(self):
        for mode in MODES:
            params = {**IDENTITY, "mode": mode, "reply_to": "original",
                      "body_format": "html", "body": FRAGMENT}
            with self.subTest(mode=mode):
                self.assertTrue(self.validator.is_valid(params))
                self.assertTrue(self.validator.is_valid({
                    **params, "operation_id": "operation", "confirm_token": "token",
                }))
        self.assertTrue(self.validator.is_valid({**IDENTITY, "operation_id": "operation"}))

    def test_identity_reply_target_and_confirmation_requirements_remain_enforced(self):
        params = {**IDENTITY, "mode": "reply", "reply_to": "original",
                  "body_format": "html", "body": FRAGMENT}
        for missing in ("lanid", "name", "reply_to"):
            with self.subTest(missing=missing):
                self.assertFalse(self.validator.is_valid({k: v for k, v in params.items() if k != missing}))
        for extra in ({"confirm_token": "token"}, {"operation_id": "operation"}):
            self.assertFalse(self.validator.is_valid({**params, **extra}))
        self.assertFalse(self.validator.is_valid({**params, "mode": "new"}))

    def test_html_reply_body_keeps_the_public_length_limit(self):
        body = "<p>" + "x" * 65529 + "</p>"
        params = {**IDENTITY, "mode": "reply", "reply_to": "original",
                  "body_format": "html", "body": body}
        self.assertTrue(self.validator.is_valid(params))
        self.assertFalse(self.validator.is_valid({**params, "body": body + "x"}))


def load_server():
    """Stub deployment adapters only while importing the real MCP server."""
    audit = ModuleType("utils.audit")
    audit.AUDIT_IDENTITY_STATE_KEY = "identity"
    audit.ToolAuditMiddleware = Middleware
    audit.install_tool_audit_fallback = lambda *args: None
    identity = ModuleType("utils.lanid_email")
    identity.IdentityResolutionError = type("IdentityResolutionError", (ValueError,), {})
    identity.IdentityNameMismatchError = type("IdentityNameMismatchError", (ValueError,), {})
    identity.normalize_lanid = lambda value: value.strip().lower()
    identity.resolve_identity_by_lanid = Mock(side_effect=AssertionError("OA is offline"))
    outlook = ModuleType("outlook_client")
    outlook.OutlookClient = Mock(side_effect=AssertionError("Exchange is offline"))
    spec = importlib.util.spec_from_file_location(
        "html_draft_test_server", Path(__file__).resolve().parents[1] / "mcp_server.py",
    )
    server = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {
        "utils": ModuleType("utils"), "utils.audit": audit,
        "utils.lanid_email": identity, "outlook_client": outlook,
    }):
        spec.loader.exec_module(server)
    return server


class HTMLDraftMCPTests(OfflineDraftFixture, unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = load_server()

    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = OperationStore(":memory:")
        self.addCleanup(self.store.close)
        for patcher in (
            patch.dict(os.environ, {
                "EWS_MCP_DATA_DIR": self.tmp.name, "EWS_MCP_DOWNLOAD_ENABLED": "false",
            }),
            patch.object(self.server, "get_store", return_value=self.store),
            patch.object(self.server, "resolve_identity_by_lanid", return_value=SimpleNamespace(email=MAILBOX)),
            patch.object(self.server.OutlookConfig, "from_service_env", return_value=self.ops.config),
            patch.object(self.server, "OutlookClient", return_value=self.ops),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_flat_html_reply_preview_confirm_and_receipt_save_once(self):
        async with Client(self.server.mcp) as client:
            for mode in MODES:
                with self.subTest(mode=mode):
                    self.original(HTMLBody("<p>ORIGINAL HTML</p>"))
                    saved_before = len(self.saved)
                    params = {**IDENTITY, "mode": mode, "reply_to": "original",
                              "body": FRAGMENT, "body_format": "html",
                              "to_emails": "recipient@example.com"}
                    preview = await client.call_tool("create_draft", params, raise_on_error=False)
                    self.assertFalse(preview.is_error, preview)
                    pending = preview.structured_content
                    self.assertTrue(pending["confirmation_required"])
                    self.assertEqual(len(self.saved), saved_before)
                    operation_id = pending["operation_id"]
                    self.assertEqual(self.store.get(operation_id)["business_params"]["body"], FRAGMENT)

                    confirmation = {"operation_id": operation_id, "confirm_token": pending["confirm_token"]}
                    changed = await client.call_tool("create_draft", {
                        **params, **confirmation, "body": FRAGMENT + "<p>Changed</p>",
                    }, raise_on_error=False)
                    self.assertTrue(changed.is_error)
                    self.assertEqual(changed.structured_content["error_code"], "CONFIRMATION_MISMATCH")
                    self.assertEqual(len(self.saved), saved_before)

                    confirmed = await client.call_tool("create_draft", {
                        **params, **confirmation,
                    }, raise_on_error=False)
                    self.assertFalse(confirmed.is_error, confirmed)
                    self.assertEqual(confirmed.structured_content["status"], "completed")
                    self.assertEqual(confirmed.structured_content["results"]["id"], "saved-draft")
                    new_body = self.saved[-1].find("t:NewBodyContent", NS)
                    self.assertEqual(new_body.get("BodyType"), "HTML")
                    self.assertEqual(new_body.text, FRAGMENT)

                    receipt = await client.call_tool("create_draft", {
                        **IDENTITY, "operation_id": operation_id,
                    }, raise_on_error=False)
                    self.assertFalse(receipt.is_error, receipt)
                    self.assertEqual(receipt.structured_content["status"], "completed")
                    replay = await client.call_tool("create_draft", {
                        **params, **confirmation,
                    }, raise_on_error=False)
                    self.assertFalse(replay.is_error, replay)
                    self.assertTrue(replay.structured_content["replayed"])
                    self.assertEqual(len(self.saved), saved_before + 1)


if __name__ == "__main__":
    unittest.main()
