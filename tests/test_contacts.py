"""联系人字段与 EWS 边界的离线行为验证。"""
import base64
import importlib
import importlib.util
import unittest
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock, patch


class ContactFieldTests(unittest.TestCase):
    def fields(self):
        self.assertIsNotNone(importlib.util.find_spec("contact_fields"), "缺少完整联系人字段适配模块")
        return importlib.import_module("contact_fields")

    def test_create_allows_no_email_and_leaves_optional_information_empty(self):
        values = self.fields().build_contact_values({"display_name": "客户"})
        self.assertEqual(values["display_name"], "客户")
        for name in ("email_addresses", "phone_numbers", "physical_addresses", "im_addresses", "categories", "children", "companies"):
            self.assertEqual(values[name], [])
        self.assertIsNone(values["birthday"])
        self.assertIsNone(values["body"])
        self.assertEqual(values["postal_address_index"], "None")

    def test_partial_edit_preserves_other_index_slots_and_address_components(self):
        from exchangelib import Contact
        from exchangelib.indexed_properties import EmailAddress, PhoneNumber, PhysicalAddress
        old = Contact(display_name="旧姓名", email_addresses=[EmailAddress(label="EmailAddress1", email="one@example.com"),
                      EmailAddress(label="EmailAddress2", email="two@example.com")],
                      phone_numbers=[PhoneNumber(label="BusinessPhone", phone_number="111"), PhoneNumber(label="MobilePhone", phone_number="222")],
                      physical_addresses=[PhysicalAddress(label="Business", street="旧街道", city="上海", zipcode="00123"),
                                          PhysicalAddress(label="Home", city="北京")], body="旧备注")
        values = self.fields().build_contact_values({"phone_numbers": {"MobilePhone": "333"},
                     "physical_addresses": {"Business": {"street": "新街道"}}}, existing=old)
        self.assertNotIn("display_name", values)
        self.assertNotIn("body", values)
        self.assertEqual({v.label: v.phone_number for v in values["phone_numbers"]}, {"BusinessPhone": "111", "MobilePhone": "333"})
        addresses = {v.label: v for v in values["physical_addresses"]}
        self.assertEqual((addresses["Business"].street, addresses["Business"].city, addresses["Business"].zipcode), ("新街道", "上海", "00123"))
        self.assertEqual(addresses["Home"].city, "北京")

    def test_explicit_empty_clears_only_named_slot_or_whole_collection(self):
        from exchangelib import Contact
        from exchangelib.indexed_properties import EmailAddress, PhysicalAddress
        old = Contact(display_name="客户", email_addresses=[EmailAddress(label="EmailAddress1", email="one@example.com"), EmailAddress(label="EmailAddress2", email="two@example.com")],
                      physical_addresses=[PhysicalAddress(label="Home", street="街道", city="上海")], body="旧备注", birthday=date(2000, 1, 2))
        values = self.fields().build_contact_values({"email_addresses": {"EmailAddress1": ""}, "physical_addresses": {"Home": {"city": None}},
                      "notes": "", "birthday": "", "postal_address_index": None}, existing=old)
        self.assertEqual([v.label for v in values["email_addresses"]], ["EmailAddress2"])
        self.assertIsNone(values["physical_addresses"][0].city)
        self.assertEqual(values["physical_addresses"][0].street, "街道")
        self.assertIsNone(values["body"])
        self.assertIsNone(values["birthday"])
        self.assertEqual(values["postal_address_index"], "None")
        self.assertEqual(self.fields().build_contact_values({"email_addresses": {}}, existing=old)["email_addresses"], [])

    def test_full_standard_schema_and_validation(self):
        from jsonschema import Draft202012Validator
        fields = self.fields()
        schema = fields.CONTACT_SCHEMA
        payload = {"display_name": "客户", "email_addresses": {"EmailAddress3": "third@example.com"},
                   "phone_numbers": {"TtyTddPhone": "555"}, "im_addresses": {"ImAddress3": "im"},
                   "physical_addresses": {"Other": {"street": "路", "country": "中国", "zipcode": "00001"}},
                   "birthday": "2000-02-29", "companies": ["公司"], "file_as_mapping": "DisplayName",
                   "photo": {"filename": "photo.jpg", "content_type": "image/jpeg", "content_base64": base64.b64encode(b"image").decode()}}
        self.assertTrue(Draft202012Validator(schema).is_valid(payload))
        for empty_name in (None, "", "   "):
            self.assertFalse(Draft202012Validator(schema).is_valid({"display_name": empty_name}))
        values = fields.build_contact_values(payload)
        self.assertEqual(values["birthday"], date(2000, 2, 29))
        self.assertEqual(values["im_addresses"][0].label, "ImAddress3")
        for invalid in ({"display_name": ""}, {"birthday": "2000-02-30"}, {"email_addresses": {"EmailAddress4": "x"}},
                        {"photo": {}}, {"phone_numbers": {"Unknown": "x"}}, {"given_name": "x" * 256}):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                fields.build_contact_values(invalid)

    def test_serialization_has_full_information_and_readonly_metadata_without_photo_bytes(self):
        from exchangelib import Contact, FileAttachment
        from exchangelib.indexed_properties import PhoneNumber, PhysicalAddress
        item = Contact(id="c1", changekey="v1", display_name="客户", nickname="昵称", children=["孩子"],
                       phone_numbers=[PhoneNumber(label="MobilePhone", phone_number="123")],
                       physical_addresses=[PhysicalAddress(label="Home", zipcode="00123")], birthday=date(2000, 1, 2), body="备注")
        item.attachments = [FileAttachment(name="p.jpg", content=b"secret bytes", content_type="image/jpeg", is_contact_photo=True)]
        result = self.fields().serialize_contact(item)
        self.assertEqual((result["id"], result["notes"], result["birthday"]), ("c1", "备注", "2000-01-02"))
        self.assertEqual(result["physical_addresses"]["Home"]["zipcode"], "00123")
        self.assertNotIn("content_base64", str(result["photo"]))

    def test_photo_helper_rejects_bad_types_and_media_before_any_submission(self):
        from tool_support import ToolOperationError
        fields = self.fields()
        for invalid in ({"filename": 42, "content_type": "image/jpeg", "content_base64": "aW1hZ2U="},
                        {"filename": "p.jpg", "content_type": "text/plain", "content_base64": "aW1hZ2U="},
                        {"filename": "p.jpg", "content_type": "image/jpeg", "content_base64": "not base64"}):
            with self.subTest(invalid=invalid), self.assertRaises(ToolOperationError):
                fields.contact_photo(invalid)

    def test_existing_html_notes_are_read_as_plain_text(self):
        from exchangelib import Contact, HTMLBody
        item = Contact(display_name="客户", body=HTMLBody("<p>第一段</p><p>第二段 &amp; 内容</p>"))
        self.assertEqual(self.fields().serialize_contact(item)["notes"], "第一段\n第二段 & 内容")


class ContactOperationsTests(unittest.TestCase):
    def setUp(self):
        from exchangelib import Account
        from exchangelib.folders import Contacts, MsgFolderRoot, Root
        from exchangelib.properties import ParentFolderId
        from people_operations import PeopleOperations
        self.ops = PeopleOperations()
        self.ops.config = SimpleNamespace(email="user@example.com")
        self.ops.account = Mock(spec=Account)
        self.ops.account.primary_smtp_address = "user@example.com"
        from exchangelib.version import Build, Version
        self.ops.account.version = Version(build=Build(15, 1, 0, 0))
        root = Root(account=self.ops.account)
        self.ipm = MsgFolderRoot(root=root, id="ipm-A", name="Top")
        self.folder = Contacts(root=root, id="folder1", name="客户", folder_class="IPF.Contact", parent_folder_id=ParentFolderId(id=self.ipm.id))

    def metadata_response(self, rows):
        def respond(*, folders, **kwargs):
            result = rows.get(folders[0].id)
            return iter([] if result is None else [result])
        return respond

    def metadata_folder(self, folder_id, parent_id=None, folder_class="IPF.Note"):
        from exchangelib.folders import Folder
        from exchangelib.properties import ParentFolderId
        return Folder(root=self.folder.root, id=folder_id, name=folder_id, folder_class=folder_class,
                      parent_folder_id=ParentFolderId(id=parent_id) if parent_id else None)

    def contact(self, **kwargs):
        from exchangelib import Contact
        from exchangelib.properties import ParentFolderId
        return Contact(account=self.ops.account, folder=self.folder, id="contact1", changekey="key1", display_name="客户",
                       parent_folder_id=ParentFolderId(id=self.folder.id), **kwargs)

    def test_new_tools_exist_without_credential_or_confirmation_business_arguments(self):
        import inspect
        for name in ("list_contact_folders", "list_contacts", "find_people", "get_contact", "create_contact", "update_contact"):
            self.assertTrue(hasattr(self.ops, name), name)
            self.assertFalse({"mailbox", "username", "password", "confirm", "confirmation_id", "directory_id"} & set(inspect.signature(getattr(self.ops, name)).parameters))

    def test_discovery_builds_nested_duplicate_paths_without_root_tree_cache(self):
        from exchangelib.folders import Folder, MsgFolderRoot, Root
        from exchangelib.properties import ParentFolderId
        root = Root(account=self.ops.account)
        ipm = MsgFolderRoot(root=root, id="ipm", name="Top")
        rows = [Folder(root=root, id="a", name="工作", folder_class="IPF.Note", parent_folder_id=ParentFolderId(id="ipm")),
                Folder(root=root, id="b", name="家庭", folder_class="IPF.Note", parent_folder_id=ParentFolderId(id="ipm")),
                Folder(root=root, id="c", name="客户", folder_class="IPF.Contact", parent_folder_id=ParentFolderId(id="a")),
                Folder(root=root, id="d", name="客户", folder_class="IPF.Contact", parent_folder_id=ParentFolderId(id="b")),
                Folder(root=root, id="cache", name="缓存", folder_class="IPF.Contact.RecipientCache", parent_folder_id=ParentFolderId(id="ipm"))]
        with patch.object(MsgFolderRoot, "get_distinguished", return_value=ipm), patch("people_operations.FolderCollection.find_folders", return_value=iter(rows)) as find:
            result = self.ops.list_contact_folders()
        self.assertEqual({v["path"] for v in result["items"]}, {"/工作/客户", "/家庭/客户"})
        self.assertEqual(find.call_args.kwargs["depth"], "Deep")
        self.assertIsNone(root._subfolders)

    def test_selected_folder_gets_direct_id_binding_and_contact_parent_check(self):
        from exchangelib.folders import MsgFolderRoot
        from exchangelib.properties import ParentFolderId
        item = self.contact()
        self.ops.account.fetch.return_value = iter([item])
        with patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm):
            get.return_value.call.return_value = iter([self.folder])
            result = self.ops.get_contact("contact1", folder_id="folder1")
            template = get.return_value.call.call_args.kwargs["folders"][0]
            self.assertEqual(template.id, "folder1")
        self.assertEqual(result["folder_id"], "folder1")
        item.parent_folder_id = ParentFolderId(id="other")
        self.ops.account.fetch.return_value = iter([item])
        with patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), self.assertRaisesRegex(ValueError, "指定联系人文件夹"):
            get.return_value.call.return_value = iter([self.folder])
            self.ops.get_contact("contact1", folder_id="folder1")

    def test_real_get_folder_payload_only_distinguished_id_carries_target_mailbox(self):
        from exchangelib.folders import Folder, MsgFolderRoot, Root
        from exchangelib.services import GetFolder
        from exchangelib.util import ns_translation
        from people_operations import _folder_fields
        self.ops.account.protocol = SimpleNamespace(version=self.ops.account.version)
        root = Root(account=self.ops.account)
        service = GetFolder(account=self.ops.account)
        raw = service.get_payload(folders=[Folder(root=root, id="folder-B")], additional_fields=_folder_fields(), shape="IdOnly")
        self.assertEqual(raw.xpath("m:FolderIds/t:FolderId/@Id", namespaces=ns_translation), ["folder-B"])
        self.assertEqual(raw.xpath(".//t:Mailbox", namespaces=ns_translation), [])
        # Keep the SDK's real distinguished-folder construction, replacing only its EWS lookup.
        with patch.object(MsgFolderRoot, "_get_distinguished", side_effect=lambda folder: folder):
            ipm = MsgFolderRoot.get_distinguished(root=root)
        scoped = service.get_payload(folders=[ipm], additional_fields=_folder_fields(), shape="IdOnly")
        self.assertEqual(scoped.xpath("m:FolderIds/t:DistinguishedFolderId/@Id", namespaces=ns_translation), ["msgfolderroot"])
        self.assertEqual(scoped.xpath(".//t:Mailbox/t:EmailAddress/text()", namespaces=ns_translation), ["user@example.com"])
        self.assertIsNone(root._subfolders)

    def test_nested_folder_in_target_mailbox_is_accepted_by_lightweight_parent_chain(self):
        from exchangelib.folders import MsgFolderRoot
        from exchangelib.properties import ParentFolderId
        self.folder.parent_folder_id = ParentFolderId(id="nested")
        nested = self.metadata_folder("nested", "parent")
        parent = self.metadata_folder("parent", self.ipm.id)
        self.ops.account.fetch.return_value = iter([self.contact()])
        with patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), patch("people_operations.FolderCollection.find_folders") as find:
            get.return_value.call.side_effect = self.metadata_response({self.folder.id: self.folder, "nested": nested, "parent": parent})
            result = self.ops.get_contact("contact1", folder_id=self.folder.id)
        self.assertEqual(result["folder_id"], self.folder.id)
        self.assertEqual([call.kwargs["folders"][0].id for call in get.return_value.call.call_args_list], ["folder1", "nested", "parent"])
        for call in get.return_value.call.call_args_list:
            self.assertEqual(call.kwargs["shape"], "IdOnly")
            self.assertIsNone(call.kwargs["folders"][0].root._subfolders)
        find.assert_not_called()

    def test_other_mailbox_folder_is_rejected_before_contact_create_read_or_photo_write(self):
        from exchangelib import Contact
        from exchangelib.folders import MsgFolderRoot
        from tool_support import ToolOperationError
        foreign = self.metadata_folder("folder-B", "ipm-B", folder_class="IPF.Contact")
        rows = {"folder-B": foreign, "ipm-B": self.metadata_folder("ipm-B", "root-B"), "root-B": self.metadata_folder("root-B")}
        for action in (lambda: self.ops.get_contact("contact-B", folder_id="folder-B"),
                       lambda: self.ops.create_contact({"display_name": "客户"}, folder_id="folder-B"),
                       lambda: self.ops.update_contact("contact-B", {"photo": None}, folder_id="folder-B")):
            with self.subTest(action=action), patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), patch.object(Contact, "save") as save, patch.object(Contact, "attach") as attach, patch("people_operations.DeleteAttachment") as delete, self.assertRaises(ToolOperationError) as caught:
                get.return_value.call.side_effect = self.metadata_response(rows)
                action()
            self.assertEqual(caught.exception.code, "ITEM_OUT_OF_SCOPE")
            self.ops.account.fetch.assert_not_called()
            save.assert_not_called()
            attach.assert_not_called()
            delete.assert_not_called()

    def test_cycle_missing_parent_and_missing_ancestor_cannot_prove_folder_scope(self):
        from exchangelib.folders import MsgFolderRoot
        from tool_support import ToolOperationError
        cases = ({"folder1": self.metadata_folder("folder1", "cycle", "IPF.Contact"), "cycle": self.metadata_folder("cycle", "folder1")},
                 {"folder1": self.metadata_folder("folder1", folder_class="IPF.Contact")},
                 {"folder1": self.metadata_folder("folder1", "missing", "IPF.Contact")})
        for rows in cases:
            with self.subTest(rows=tuple(rows)), patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), self.assertRaises(ToolOperationError) as caught:
                get.return_value.call.side_effect = self.metadata_response(rows)
                self.ops.get_contact("contact1", folder_id="folder1")
            self.assertEqual(caught.exception.code, "ITEM_OUT_OF_SCOPE")
            self.ops.account.fetch.assert_not_called()

    def test_ancestor_exchange_failure_preserves_actual_access_denied(self):
        from exchangelib.errors import ErrorAccessDenied
        from exchangelib.folders import MsgFolderRoot
        from tool_support import error_details
        denied = ErrorAccessDenied("ancestor denied")
        folder = self.metadata_folder("folder1", "parent", "IPF.Contact")
        with patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), self.assertRaises(ErrorAccessDenied) as caught:
            get.return_value.call.side_effect = self.metadata_response({"folder1": folder, "parent": denied})
            self.ops.get_contact("contact1", folder_id="folder1")
        self.assertIs(caught.exception, denied)
        self.assertEqual(error_details(caught.exception)["exchange_code"], "ErrorAccessDenied")
        self.ops.account.fetch.assert_not_called()

    def test_default_contacts_uses_mailbox_scoped_distinguished_folder_without_parent_lookup(self):
        from exchangelib.folders import Contacts, MsgFolderRoot
        with patch.object(Contacts, "get_distinguished", return_value=self.folder), patch.object(MsgFolderRoot, "get_distinguished") as ipm, patch("people_operations.GetFolder") as get:
            self.assertIs(self.ops._contact_folder(), self.folder)
        ipm.assert_not_called()
        get.assert_not_called()

    def test_fetch_error_objects_are_not_disguised_as_not_found(self):
        from exchangelib.errors import ErrorAccessDenied
        error = ErrorAccessDenied("denied")
        self.ops.account.fetch.return_value = iter([error])
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), self.assertRaises(ErrorAccessDenied):
            self.ops.get_contact("contact1")

    def test_list_only_selected_folder_with_lookahead_pagination(self):
        self.assertTrue(hasattr(self.ops, "list_contacts"))
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.folder, "all") as all_items:
            all_items.return_value.order_by.return_value = [self.contact(), self.contact(), self.contact()]
            result = self.ops.list_contacts(limit=2)
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(result["has_more"])

    def test_search_one_source_failure_is_partial_and_all_failures_are_failed(self):
        from exchangelib.errors import ErrorAccessDenied
        denied = ErrorAccessDenied("denied")
        with patch.object(self.ops, "_resolve_directory", side_effect=denied), patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.folder, "filter") as search:
            search.return_value.order_by.return_value = [self.contact()]
            result = self.ops.find_people("客户")
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["source_errors"]["gal"]["exchange_code"], "ErrorAccessDenied")
        with patch.object(self.ops, "_resolve_directory", side_effect=denied), patch.object(self.ops, "_contact_folder", side_effect=denied):
            result = self.ops.find_people("客户")
            self.assertEqual(result["status"], "failed")
            self.assertFalse(result["ok"])

    def test_create_saves_directly_and_update_only_changes_supplied_fields(self):
        from exchangelib import Contact
        from exchangelib.items import NEVER_OVERWRITE
        def save(item, **kwargs):
            item.id, item.changekey = "created", "v2"
            return item
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(Contact, "save", autospec=True, side_effect=save) as saved:
            result = self.ops.create_contact({"display_name": "新客户"})
            self.assertTrue(result["created"])
            self.assertEqual(saved.call_count, 1)
        item = self.contact(body="旧备注", company_name="原公司")
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.ops, "_fetch_contact", return_value=item), patch.object(Contact, "save", autospec=True, return_value=item) as saved:
            result = self.ops.update_contact("contact1", {"notes": "新备注"})
        self.assertEqual(item.company_name, "原公司")
        self.assertEqual(item.body, "新备注")
        self.assertEqual(saved.call_args.kwargs["update_fields"], ["body"])
        self.assertEqual(saved.call_args.kwargs["conflict_resolution"], NEVER_OVERWRITE)
        self.assertTrue(result["updated"])

    def test_update_payload_preserves_unmentioned_indices_and_clears_only_requested_components(self):
        from exchangelib.indexed_properties import EmailAddress, PhysicalAddress
        from exchangelib.services import UpdateItem
        from exchangelib.util import ns_translation
        from contact_fields import build_contact_values
        item = self.contact(email_addresses=[EmailAddress(label="EmailAddress1", email="one@example.com"), EmailAddress(label="EmailAddress2", email="two@example.com")],
                            physical_addresses=[PhysicalAddress(label="Business", street="路", city="上海"), PhysicalAddress(label="Home", city="北京")])
        values = build_contact_values({"email_addresses": {"EmailAddress1": "changed@example.com"},
                                       "physical_addresses": {"Business": {"city": None}}, "postal_address_index": None}, existing=item)
        for name, value in values.items():
            setattr(item, name, value)
        self.ops.account.protocol = SimpleNamespace(version=self.ops.account.version)
        payload = UpdateItem(account=self.ops.account).get_payload(
            items=[(item, list(values))], conflict_resolution="NeverOverwrite", message_disposition="SaveOnly",
            send_meeting_invitations_or_cancellations="SendToNone", suppress_read_receipts=True)
        changes = {}
        for update in payload.xpath("m:ItemChanges/t:ItemChange/t:Updates/*", namespaces=ns_translation):
            field = update[0]
            changes[(field.get("FieldURI"), field.get("FieldIndex"))] = update.tag.rsplit("}", 1)[-1]
        self.assertEqual(changes[("contacts:EmailAddress", "EmailAddress2")], "SetItemField")
        self.assertEqual(changes[("contacts:PhysicalAddress:Street", "Business")], "SetItemField")
        self.assertEqual(changes[("contacts:PhysicalAddress:City", "Business")], "DeleteItemField")
        self.assertEqual(changes[("contacts:PhysicalAddress:City", "Home")], "SetItemField")
        self.assertEqual(changes[("contacts:PostalAddressIndex", None)], "SetItemField")

    def test_pre_submit_sdk_validation_failure_is_failed_without_calling_save(self):
        from exchangelib import Contact
        from tool_support import ToolOperationError
        item = self.contact()
        # EWS item identity was incomplete in the read result; no update can be submitted.
        item.changekey = None
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.ops, "_fetch_contact", return_value=item), patch.object(Contact, "save", autospec=True) as save, self.assertRaises(ToolOperationError) as caught:
            self.ops.update_contact("contact1", {"notes": "新备注"})
        self.assertEqual(caught.exception.status, "failed")
        save.assert_not_called()

    def test_save_incomplete_response_preserves_returned_contact_id_as_unknown(self):
        from exchangelib import Contact
        from tool_support import ToolOperationError
        def save(item, **kwargs):
            item.id, item.changekey = "saved", None
            return item
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(Contact, "save", autospec=True, side_effect=save), self.assertRaises(ToolOperationError) as caught:
            self.ops.create_contact({"display_name": "客户"})
        self.assertEqual(caught.exception.status, "unknown")
        self.assertEqual(caught.exception.results["contact_id"], "saved")

    def test_photo_failure_keeps_saved_contact_as_partial_or_unknown(self):
        from exchangelib import Contact
        from exchangelib.errors import ErrorAccessDenied
        from requests.exceptions import ReadTimeout
        from tool_support import ToolOperationError
        payload = {"display_name": "客户", "photo": {"filename": "p.jpg", "content_type": "image/jpeg", "content_base64": base64.b64encode(b"image").decode()}}
        def save(item, **kwargs):
            item.id, item.changekey = "saved", "v2"
            return item
        for error, expected in ((ErrorAccessDenied("denied"), "partial"), (ReadTimeout("timeout"), "unknown")):
            with self.subTest(expected=expected), patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(Contact, "save", autospec=True, side_effect=save), patch.object(Contact, "attach", side_effect=error), self.assertRaises(ToolOperationError) as caught:
                self.ops.create_contact(payload)
            self.assertEqual(caught.exception.status, expected)
            self.assertEqual(caught.exception.results["id"], "saved")
            self.assertIs(caught.exception.__cause__, error)
            self.assertEqual(caught.exception.results["photo_result"]["added"], None if expected == "unknown" else False)

    def test_invalid_update_is_rejected_before_contact_lookup(self):
        from tool_support import ToolOperationError
        for invalid in ({"given_name": "x" * 256}, {"birthday": "2000-02-30"}, {"display_name": None}):
            with self.subTest(invalid=invalid), patch.object(self.ops, "_contact_folder") as folder, self.assertRaises(ToolOperationError):
                self.ops.update_contact("contact1", invalid)
            folder.assert_not_called()

    def test_photo_removal_preserves_non_photo_attachments(self):
        from exchangelib import Contact, FileAttachment
        from exchangelib.attachments import AttachmentId
        from exchangelib.properties import RootItemId
        item = self.contact()
        photo = FileAttachment(name="p.jpg", content=b"image", is_contact_photo=True, attachment_id=AttachmentId(id="photo1"), parent_item=item)
        other = FileAttachment(name="doc.txt", content=b"data")
        item.attachments = [photo, other]
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.ops, "_fetch_contact", return_value=item), patch("people_operations.DeleteAttachment", create=True) as delete:
            delete.return_value.get.return_value = RootItemId(id=item.id, changekey="after-delete")
            result = self.ops.update_contact("contact1", {"photo": None})
            self.assertEqual(delete.return_value.get.call_args.kwargs["items"][0].id, "photo1")
        self.assertEqual(item.attachments, [other])
        self.assertEqual(result["changekey"], "after-delete")

    def test_photo_replacement_uses_delete_change_key_and_reports_completed_removals(self):
        from exchangelib import Contact, FileAttachment
        from exchangelib.attachments import AttachmentId
        from exchangelib.errors import ErrorAccessDenied
        from exchangelib.properties import RootItemId
        from tool_support import ToolOperationError
        item = self.contact()
        photos = [FileAttachment(name=f"p{n}.jpg", is_contact_photo=True, attachment_id=AttachmentId(id=f"photo{n}"), parent_item=item) for n in (1, 2)]
        item.attachments = list(photos)
        payload = {"photo": {"filename": "new.jpg", "content_type": "image/jpeg", "content_base64": "aW1hZ2U="}}
        denied = ErrorAccessDenied("denied")
        with patch.object(self.ops, "_contact_folder", return_value=self.folder), patch.object(self.ops, "_fetch_contact", return_value=item), patch("people_operations.DeleteAttachment", create=True) as delete, patch.object(Contact, "attach") as attach, self.assertRaises(ToolOperationError) as caught:
            delete.return_value.get.side_effect = [RootItemId(id=item.id, changekey="after-first"), denied]
            self.ops.update_contact("contact1", payload)
        attach.assert_not_called()
        self.assertEqual(caught.exception.status, "partial")
        self.assertEqual(caught.exception.results["photo_result"]["removed"], 1)
        self.assertEqual(caught.exception.results["changekey"], "after-first")
        self.assertTrue(caught.exception.results["updated"])
        self.assertIs(caught.exception.__cause__, denied)

    def test_photo_add_receives_the_current_change_key(self):
        from exchangelib import Contact, FileAttachment
        from exchangelib.folders import MsgFolderRoot
        from exchangelib.attachments import AttachmentId
        from exchangelib.properties import RootItemId
        item = self.contact()
        item.attachments = [FileAttachment(name="old.jpg", is_contact_photo=True, attachment_id=AttachmentId(id="old"), parent_item=item)]
        payload = {"photo": {"filename": "new.jpg", "content_type": "image/jpeg", "content_base64": "aW1hZ2U="}}
        def attach(current, photo):
            self.assertEqual(current.changekey, "after-delete")
            current.changekey = "after-add"
            photo.attachment_id = AttachmentId(id="new")
            current.attachments.append(photo)
        self.ops.account.fetch.return_value = iter([item])
        with patch("people_operations.GetFolder") as get, patch.object(MsgFolderRoot, "get_distinguished", return_value=self.ipm), patch("people_operations.DeleteAttachment", create=True) as delete, patch.object(Contact, "attach", autospec=True, side_effect=attach):
            get.return_value.call.side_effect = self.metadata_response({self.folder.id: self.folder})
            delete.return_value.get.return_value = RootItemId(id=item.id, changekey="after-delete")
            result = self.ops.update_contact("contact1", payload, folder_id=self.folder.id)
        self.assertEqual(result["changekey"], "after-add")
        self.assertEqual(result["photo_result"], {"removed": 1, "added": True})


if __name__ == "__main__":
    unittest.main()
