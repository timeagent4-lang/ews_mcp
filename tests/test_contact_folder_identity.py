"""用真实 GetFolder XML 分辨系统目录与 SDK 猜出的同名联系人目录。"""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from exchangelib import Contact
from exchangelib.errors import ErrorAccessDenied, ErrorCorruptData, ErrorFolderNotFound, ErrorInvalidOperation, ErrorItemNotFound
from exchangelib.folders import Contacts, MsgFolderRoot, Root
from exchangelib.services import CreateItem, FindItem, GetFolder, GetItem, UpdateItem
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


def contact_xml(item_id="contact1", changekey="old-version"):
    item = create_element("t:Contact")
    item.append(create_element("t:ItemId", attrs={"Id": item_id, "ChangeKey": changekey}))
    item.append(create_element("t:ParentFolderId", attrs={"Id": "custom"}))
    for tag, value in (("DisplayName", "客户"), ("CompanyName", "原公司")):
        child = create_element(f"t:{tag}")
        child.text = value
        item.append(child)
    return item


class ContactFolderIdentityTests(unittest.TestCase):
    def setUp(self):
        self.ops = PeopleOperations()
        self.ops.account = offline_account()
        self.ops.config = SimpleNamespace(email=self.ops.account.primary_smtp_address)
        self.root = Root(account=self.ops.account)
        self.ops.account.root = self.root
        self.ipm = MsgFolderRoot(root=self.root, id="ipm", name="Top")
        self.rows = {"custom": folder_xml("custom", "PersonMetadata"),
                     "normal": folder_xml("normal", "客户"),
                     "metadata": folder_xml("metadata", "PersonMetadata")}
        self.metadata = self.rows["metadata"]
        self.payloads = []
        self.item_payloads = []
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

    def item_boundary(self, service, payload_func, items, **kwargs):
        self.item_payloads.append(payload_func(items=list(items), **kwargs))
        if isinstance(service, CreateItem):
            return iter([contact_xml("created", "new-version")])
        if isinstance(service, UpdateItem):
            return iter([contact_xml("contact1", "updated-version")])
        return iter([contact_xml()])

    def find_boundary(self, service, payload_func, max_items, **kwargs):
        self.item_payloads.append(payload_func(**kwargs))
        return iter([contact_xml()])

    def assert_contact_read_fields(self):
        payload = next(payload for payload in self.item_payloads if payload.xpath("self::m:GetItem", namespaces=ns_translation))
        fields = payload.xpath("m:ItemShape/t:AdditionalProperties/t:FieldURI/@FieldURI", namespaces=ns_translation)
        self.assertNotIn("item:UniqueBody", fields)
        self.assertIn("contacts:DisplayName", fields)
        self.assertIn("item:ParentFolderId", fields)
        self.assertEqual(payload.xpath("m:ItemIds/t:ItemId/@Id", namespaces=ns_translation), ["contact1"])

    def test_same_named_custom_folder_is_accepted_and_contact_saved_in_it(self):
        # 确认测试输入触发真实 SDK 的名称猜测，而非普通 Folder 模拟对象。
        guessed = self.ops._folder_metadata(self.root, "custom")
        self.assertEqual(type(guessed).__name__, "PersonMetadata")
        with patch.object(CreateItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary):
            result = self.ops.create_contact({"display_name": "客户"}, folder_id="custom")
        self.assertEqual(result["folder_id"], "custom")
        self.assertEqual(result["id"], "created")
        payload = self.item_payloads[0]
        self.assertEqual(payload.xpath("m:SavedItemFolderId/t:FolderId/@Id", namespaces=ns_translation), ["custom"])
        self.assertEqual(payload.xpath("m:SavedItemFolderId/t:FolderId/@ChangeKey", namespaces=ns_translation), ["v"])
        self.assertFalse(payload.xpath("m:SavedItemFolderId/t:DistinguishedFolderId", namespaces=ns_translation))
        self.assertEqual(payload.xpath("m:Items/t:Contact/t:DisplayName/text()", namespaces=ns_translation), ["客户"])
        self.assertEqual(len(self.metadata_payloads()), 1)
        payload = self.metadata_payloads()[0]
        self.assertEqual(payload.xpath(".//t:Mailbox/t:EmailAddress/text()", namespaces=ns_translation), ["user@example.com"])
        self.assertEqual(payload.xpath("m:FolderShape/t:BaseShape/text()", namespaces=ns_translation), ["IdOnly"])
        self.assertFalse(payload.xpath("m:FolderShape/t:AdditionalProperties", namespaces=ns_translation))

    def test_same_named_custom_folder_reads_use_contact_supported_fields(self):
        actions = (
            ("list", lambda: self.ops.list_contacts(folder_id="custom")),
            ("find", lambda: self.ops.find_people("客户", source="contacts", folder_id="custom")),
            ("get", lambda: self.ops.get_contact("contact1", folder_id="custom")),
        )
        for name, action in actions:
            self.item_payloads.clear()
            with self.subTest(operation=name), \
                    patch.object(FindItem, "_paged_call", autospec=True, side_effect=self.find_boundary), \
                    patch.object(GetItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary):
                result = action()
                contact = result if name == "get" else result["items"][0]
                self.assertEqual(contact["id"], "contact1")
                self.assertEqual(contact["display_name"], "客户")
                self.assert_contact_read_fields()
                if name != "get":
                    payload = self.item_payloads[0]
                    self.assertEqual(payload.xpath("m:ParentFolderIds/t:FolderId/@Id", namespaces=ns_translation), ["custom"])
                    self.assertFalse(payload.xpath("m:ParentFolderIds/t:DistinguishedFolderId", namespaces=ns_translation))

    def test_same_named_custom_folder_update_reads_contact_fields_and_preserves_other_values(self):
        with patch.object(GetItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary), \
                patch.object(UpdateItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary):
            result = self.ops.update_contact("contact1", {"display_name": "新客户"}, folder_id="custom")
        self.assertTrue(result["updated"])
        self.assertEqual(result["changekey"], "updated-version")
        self.assertEqual(result["folder_id"], "custom")
        self.assert_contact_read_fields()
        payload = self.item_payloads[-1]
        self.assertEqual(payload.get("ConflictResolution"), "NeverOverwrite")
        self.assertEqual(payload.xpath("m:ItemChanges/t:ItemChange/t:ItemId/@Id", namespaces=ns_translation), ["contact1"])
        self.assertEqual(payload.xpath(".//t:SetItemField/t:FieldURI/@FieldURI", namespaces=ns_translation), ["contacts:DisplayName"])
        self.assertEqual(payload.xpath(".//t:SetItemField/t:Contact/t:DisplayName/text()", namespaces=ns_translation), ["新客户"])

    def test_selected_folder_with_missing_or_mismatched_id_is_rejected_before_contact_write(self):
        for response_id in (None, "other-folder"):
            self.rows["custom"] = folder_xml(response_id, "PersonMetadata")
            self.payloads.clear()
            self.item_payloads.clear()
            with self.subTest(response_id=response_id):
                with patch.object(CreateItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary), \
                        self.assertRaises(ToolOperationError) as cm:
                    self.ops.create_contact({"display_name": "客户"}, folder_id="custom")
                self.assertEqual(cm.exception.code, "CONTACT_FOLDER_CLASSIFICATION_FAILED")
                self.assertEqual(self.metadata_payloads(), [])
                self.assertEqual(self.item_payloads, [])

    def test_explicit_contacts_folder_preserves_metadata_and_uses_raw_id_binding(self):
        row = self.rows["custom"] = folder_xml("custom", "Contacts")
        row.append(create_element("t:DistinguishedFolderId", attrs={"Id": "contacts"}))
        for tag, value in (("TotalCount", "7"), ("ChildFolderCount", "2")):
            child = create_element(f"t:{tag}")
            child.text = value
            row.append(child)
        folder = self.ops._contact_folder("custom")
        self.assertIs(folder.root.account, self.ops.account)
        self.assertEqual(folder.name, "Contacts")
        self.assertEqual(folder.folder_class, "IPF.Contact")
        self.assertEqual(folder.parent_folder_id.id, "ipm")
        self.assertEqual(folder.total_count, 7)
        self.assertEqual(folder.child_folder_count, 2)
        with patch.object(CreateItem, "_chunked_get_elements", autospec=True, side_effect=self.item_boundary):
            self.ops.create_contact({"display_name": "客户"}, folder_id="custom")
        payload = self.item_payloads[0]
        self.assertEqual(payload.xpath("m:SavedItemFolderId/t:FolderId/@Id", namespaces=ns_translation), ["custom"])
        self.assertEqual(payload.xpath("m:SavedItemFolderId/t:FolderId/@ChangeKey", namespaces=ns_translation), ["v"])
        self.assertFalse(payload.xpath("m:SavedItemFolderId/t:DistinguishedFolderId", namespaces=ns_translation))

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
