import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exchangelib import Contact
from exchangelib.folders import Folder
from exchangelib.properties import ParentFolderId

import mcp_server
from confirmation import ConfirmationError
from people_operations import PeopleOperations
import team_directories as directories
from tool_support import ToolOperationError


ENTRY = {
    "display_name": "团队通讯录", "mailbox": "TEAM@Example.COM",
    "folder_id": "team-folder", "parent_folder_id": "parent-folder",
    "folder_class": "IPF.Contact", "members": ["DOMAIN\\employee"],
}


class TeamDirectoryTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {directories.ENV_VAR: json.dumps({"team": ENTRY})})
        env.start()
        self.addCleanup(env.stop)
        self.spec = directories.get_directory("team")

    def test_membership_normalizes_domain_and_case_for_reads_and_creates(self):
        self.assertEqual(self.spec.mailbox, "team@example.com")
        for write in (False, True):
            directories.authorize(self.spec, " Domain\\EMPLOYEE ", write=write)
            for principal in (None, "", "unlisted", "service-account"):
                with self.subTest(write=write, principal=principal):
                    with self.assertRaises(ToolOperationError) as caught:
                        directories.authorize(self.spec, principal, write=write)
                    self.assertEqual(caught.exception.code, "DIRECTORY_NOT_AUTHORIZED")

    def test_unconfigured_unknown_or_malformed_registry_never_falls_back(self):
        for raw, code in (("", "INVALID_DIRECTORY"), ("bad-json", "DIRECTORY_CONFIG_INVALID"),
                          ("[]", "DIRECTORY_CONFIG_INVALID"), ("{}", "INVALID_DIRECTORY")):
            with self.subTest(raw=raw), patch.dict(os.environ, {directories.ENV_VAR: raw}):
                with self.assertRaises(ToolOperationError) as caught:
                    directories.get_directory("team")
                self.assertEqual(caught.exception.code, code)
        with self.assertRaises(ToolOperationError) as caught:
            directories.get_directory("unknown")
        self.assertEqual(caught.exception.code, "INVALID_DIRECTORY")

    def test_registry_requires_members_and_contact_folder(self):
        for update in ({"members": []}, {"members": [None]}, {"members": "employee"},
                       {"mailbox": "not-mail"}, {"folder_id": ""}, {"folder_class": "IPF.Note"}):
            with self.subTest(update=update), patch.dict(os.environ, {
                directories.ENV_VAR: json.dumps({"team": {**ENTRY, **update}}),
            }):
                with self.assertRaises(ToolOperationError) as caught:
                    directories.get_directory("team")
                self.assertEqual(caught.exception.code, "DIRECTORY_CONFIG_INVALID")

    def test_changed_target_rejects_confirmation_before_submission(self):
        record = {"details": {"target": self.spec.identity()}}
        params = {"lanid": "employee", "directory_id": "team"}
        self.assertEqual(mcp_server._verified_directory_target(record, params), self.spec)
        with patch.dict(os.environ, {directories.ENV_VAR: json.dumps({
            "team": {**ENTRY, "folder_id": "other-folder"},
        })}):
            with self.assertRaises(ConfirmationError) as caught:
                mcp_server._verified_directory_target(record, params)
        self.assertEqual(caught.exception.code, "DIRECTORY_TARGET_CHANGED")

    def test_revoked_membership_prevents_receipt_disclosure(self):
        record = {
            "tool": "create_contact", "mailbox": "employee@example.com",
            "status": "completed", "operation_id": "op",
            "business_params": {"directory_id": "team"},
            "result": {"private": "contact-content"},
        }
        with patch.dict(os.environ, {directories.ENV_VAR: json.dumps({
            "team": {**ENTRY, "members": ["another-employee"]},
        })}):
            with self.assertRaises(ToolOperationError) as caught:
                mcp_server._receipt("create_contact", record,
                                    mailbox="employee@example.com", lanid="employee")
        self.assertEqual(caught.exception.code, "DIRECTORY_NOT_AUTHORIZED")

    def test_folder_type_and_parent_are_checked_before_item_access(self):
        ops = PeopleOperations()
        ops._create_account = Mock(return_value=object())
        for folder in (SimpleNamespace(folder_class="IPF.Note"),
                       SimpleNamespace(folder_class="IPF.Contact", parent_folder_id=ParentFolderId(id="other"))):
            with self.subTest(folder=folder), patch("people_operations._get_folder_by_id", return_value=folder):
                with self.assertRaises(ToolOperationError) as caught:
                    ops._bind_directory(self.spec)
                self.assertEqual(caught.exception.code, "DIRECTORY_INVALID_TARGET")

    def test_get_item_checks_parent_even_when_fetch_was_given_folder(self):
        ops = PeopleOperations()
        folder = Folder(id="team-folder")
        account = Mock()
        ops._prepare_directory = Mock(return_value=(self.spec, account, folder))
        ops._resolve_directory = Mock()
        contact = Contact(id="contact", display_name="客户", parent_folder_id=ParentFolderId(id="other"))
        account.fetch.return_value = [contact]
        with self.assertRaises(ToolOperationError) as caught:
            ops.get_contact("customer@example.com", directory_id="team")
        self.assertEqual(caught.exception.code, "ITEM_OUT_OF_SCOPE")
        ops._resolve_directory.assert_not_called()
        account.fetch.assert_called_once_with(ids=[("customer@example.com", None)], folder=folder)
        contact.parent_folder_id = ParentFolderId(id=folder.id)
        result = ops.get_contact("contact", directory_id="team")
        self.assertEqual(result["directory_id"], "team")
        self.assertEqual(result["display_name"], "客户")

    def test_directory_id_requires_explicit_contacts_search_source(self):
        ops = PeopleOperations()
        ops._prepare_directory = Mock()
        for source in ("auto", "gal"):
            with self.subTest(source=source):
                with self.assertRaises(ToolOperationError) as caught:
                    ops.find_people("客户", source=source, directory_id="team")
                self.assertEqual(caught.exception.code, "INVALID_SOURCE")
        ops._prepare_directory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
