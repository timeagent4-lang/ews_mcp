"""用真实 GetFolder XML 分辨系统目录与 SDK 猜出的同名联系人目录。"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from exchangelib import Contact
from exchangelib.errors import ErrorAccessDenied, ErrorCorruptData, ErrorFolderNotFound, ErrorInvalidOperation, ErrorItemNotFound
from exchangelib.folders import Contacts, MsgFolderRoot, Root
from exchangelib.services import GetFolder
from exchangelib.util import create_element, ns_translation
from exchangelib.version import Build, Version
from requests.exceptions import ReadTimeout

from people_operations import PeopleOperations
from tests.test_direct_operations import offline_account
from tool_support import ToolOperationError


def folder_xml(item_id, name, folder_class="IPF.Contact", parent="ipm"):
    folder = create_element("t:Folder")
    if item_id:
        folder.append(create_element("t:FolderId", attrs={"Id": item_id, "ChangeKey": "v"}))
    if parent:
        folder.append(create_element("t:ParentFolderId", attrs={"Id": parent}))
    for tag, value in (("DisplayName", name), ("FolderClass", folder_class)):
        child = create_element(f"t:{tag}")
        child.text = value
        folder.append(child)
    return folder


class ContactFolderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ops = PeopleOperations()
        self.ops.account = offline_account()
        self.ops.config = SimpleNamespace(email=self.ops.account.primary_smtp_address)
        self.root = Root(account=self.ops.account)
        self.ipm = MsgFolderRoot(root=self.root, id="ipm", name="Top")
        self.rows = {"custom": folder_xml("custom", "PersonMetadata"),
                     "normal": folder_xml("normal", "客户"),
                     "metadata": folder_xml("metadata", "PersonMetadata")}
        self.metadata = self.rows["metadata"]
        self.payloads = []
        network = patch("requests.Session.request", side_effect=AssertionError("不允许真实网络"))
        boundary = patch.object(GetFolder, "_chunked_get_elements", autospec=True, side_effect=self.read_boundary)
        ipm = patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm)
        for fixture in (network, boundary, ipm):
            fixture.start()
            self.addCleanup(fixture.stop)

    def read_boundary(self, service, payload_func, items, **kwargs):
        templates = list(items)
        self.payloads.append(payload_func(folders=templates, **kwargs))
        results = []
        for template in templates:
            distinguished = getattr(template, "_distinguished_id", None)
            if distinguished is not None:
                self.assertEqual(distinguished.id, "personmetadata")
                result = self.metadata
            else:
                result = self.rows.get(template.id)
            if result is None:
                continue
            results.append(result if isinstance(result, Exception) else deepcopy(result))
        return iter(results)

    def metadata_payloads(self):
        return [payload for payload in self.payloads if payload.xpath(
            "m:FolderIds/t:DistinguishedFolderId[@Id='personmetadata']", namespaces=ns_translation)]

    @staticmethod
    def save_contact(item, **kwargs):
        item.id, item.changekey = "created", "new-version"
        return item

    def test_same_named_custom_folder_is_accepted_and_contact_saved_in_it(self):
        with patch.object(Contact, "save", autospec=True, side_effect=self.save_contact) as save:
            result = self.ops.create_contact({"display_name": "客户"}, folder_id="custom")
        created = save.call_args.args[0]
        # 确认触发真实 SDK 的名称猜测，而非普通 Folder 模拟对象。
        self.assertEqual(type(created.folder).__name__, "PersonMetadata")
        self.assertEqual(result["folder_id"], "custom")
        self.assertEqual(len(self.metadata_payloads()), 1)
        payload = self.metadata_payloads()[0]
        self.assertEqual(payload.xpath(".//t:Mailbox/t:EmailAddress/text()", namespaces=ns_translation), ["user@example.com"])
        self.assertEqual(payload.xpath("m:FolderShape/t:BaseShape/text()", namespaces=ns_translation), ["IdOnly"])
        self.assertFalse(payload.xpath("m:FolderShape/t:AdditionalProperties", namespaces=ns_translation))

    def test_actual_system_id_is_rejected_even_if_renamed_before_any_write(self):
        for name in ("PersonMetadata", "重新命名的系统目录"):
            self.rows["metadata"] = self.metadata = folder_xml("metadata", name)
            with self.subTest(name=name), patch.object(Contact, "save") as save, self.assertRaises(ToolOperationError) as cm:
                self.ops.create_contact({"display_name": "客户"}, folder_id="metadata")
            self.assertEqual(cm.exception.code, "INVALID_CONTACT_FOLDER")
            save.assert_not_called()

    def test_discovery_filters_by_actual_id_and_queries_identity_once_per_operation(self):
        self.rows["metadata"] = self.metadata = folder_xml("metadata", "重命名目录")
        self.rows["cache"] = folder_xml("cache", "缓存", "IPF.Contact.RecipientCache")
        folders = [self.ops._folder_metadata(self.root, key) for key in self.rows]
        self.payloads.clear()
        for expected_calls in (1, 2):
            with patch("people_operations.FolderCollection.find_folders", return_value=iter(folders)):
                result = self.ops.list_contact_folders()
            self.assertEqual({row["id"] for row in result["items"]}, {"custom", "normal"})
            self.assertTrue(all(row["path_complete"] for row in result["items"]))
            self.assertEqual(len(self.metadata_payloads()), expected_calls)
            self.assertIsNone(self.root._subfolders)

    def test_explicitly_missing_system_directory_does_not_block_custom_folder(self):
        for fault in (ErrorFolderNotFound("absent"), ErrorItemNotFound("absent")):
            self.metadata = fault
            with self.subTest(fault=type(fault).__name__):
                self.assertEqual(self.ops._contact_folder("custom").id, "custom")

    def test_identity_read_failures_are_preserved_and_never_submit_contact(self):
        for fault in (ErrorAccessDenied("metadata denied"), ErrorInvalidOperation("invalid"),
                      ErrorCorruptData("broken"), ReadTimeout("metadata timeout")):
            self.metadata = fault
            with self.subTest(fault=type(fault).__name__), patch.object(Contact, "save") as save, self.assertRaises(type(fault)) as cm:
                self.ops.create_contact({"display_name": "客户"}, folder_id="normal")
            self.assertIs(cm.exception, fault)
            save.assert_not_called()

    def test_incomplete_identity_response_is_not_treated_as_missing_directory(self):
        for response in (None, folder_xml(None, "PersonMetadata")):
            self.metadata = response
            with self.subTest(response=response), self.assertRaises(ToolOperationError) as cm:
                self.ops._contact_folder("normal")
            self.assertEqual(cm.exception.code, "CONTACT_FOLDER_CLASSIFICATION_FAILED")
            self.assertEqual(cm.exception.status, "failed")

    def test_default_contacts_uses_distinguished_binding_without_extra_identity_probe(self):
        folder = Contacts(root=self.root, id="contacts", name="联系人", folder_class="IPF.Contact")
        with patch.object(Contacts, "get_distinguished", return_value=folder):
            self.assertIs(self.ops._contact_folder(), folder)
        self.assertEqual(self.payloads, [])

    def test_legacy_version_keeps_regular_folders_and_reports_ambiguous_identity_explicitly(self):
        self.ops.account.version = Version(build=Build(15, 0, 0, 0))
        self.assertEqual(self.ops._contact_folder("normal").id, "normal")
        with self.assertRaises(ToolOperationError) as cm:
            self.ops._contact_folder("custom")
        self.assertEqual(cm.exception.code, "CONTACT_FOLDER_CLASSIFICATION_UNSUPPORTED")
        self.assertEqual(self.metadata_payloads(), [])
        folders = [self.ops._folder_metadata(self.root, key) for key in ("custom", "normal")]
        with patch("people_operations.FolderCollection.find_folders", return_value=iter(folders)):
            result = self.ops.list_contact_folders()
        self.assertEqual([row["id"] for row in result["items"]], ["normal"])
        self.assertEqual([row["id"] for row in result["unclassified_folders"]], ["custom"])
        self.assertEqual(self.metadata_payloads(), [])
