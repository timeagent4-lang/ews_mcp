"""使用真实 ResolveNames / Contact XML 解析验证 GAL 备注读取。"""
import unittest
from unittest.mock import patch

from exchangelib import Contact
from exchangelib.services import ResolveNames
from exchangelib.util import add_xml_child, create_element

from people_operations import PeopleOperations
from tests.test_direct_operations import offline_account


class GalNotesTests(unittest.TestCase):
    def setUp(self):
        network = patch("requests.Session.request", side_effect=AssertionError("不允许真实网络"))
        network.start()
        self.addCleanup(network.stop)
        self.account = offline_account()
        self.ops = PeopleOperations()
        self.ops.account = self.account

    def resolution(self, *, notes=None, body=None):
        elem = create_element("t:Resolution")
        mailbox = create_element("t:Mailbox")
        add_xml_child(mailbox, "t:Name", "目录员工")
        add_xml_child(mailbox, "t:EmailAddress", "directory@example.com")
        add_xml_child(mailbox, "t:RoutingType", "SMTP")
        add_xml_child(mailbox, "t:MailboxType", "Mailbox")
        elem.append(mailbox)
        contact = create_element("t:Contact")
        add_xml_child(contact, "t:DisplayName", "目录员工")
        if notes is not None:
            add_xml_child(contact, "t:Notes", notes)
        if body is not None:
            body_elem = create_element("t:Body", attrs={"BodyType": "Text"})
            body_elem.text = body
            contact.append(body_elem)
        elem.append(contact)
        service = ResolveNames(protocol=self.account.protocol)
        service.return_full_contact_data = True
        resolved = service._elem_to_obj(elem)
        self.assertIsInstance(resolved[1], Contact)
        self.assertEqual(resolved[1].notes, notes)
        if body == "":
            # SDK 5.6.0 将空 Body XML 解析成 None；单独覆盖业务层收到空字符串的边界。
            self.assertIsNone(resolved[1].body)
            resolved[1].body = ""
        return resolved

    def read_gal(self, method, **fields):
        resolution = self.resolution(**fields)
        with patch.object(self.account.protocol, "resolve_names", return_value=[resolution]):
            if method == "get_contact":
                row = self.ops.get_contact("DIRECTORY@EXAMPLE.COM")
            else:
                result = self.ops.find_people("目录", source="gal")
                self.assertEqual(result["status"], "success")
                self.assertEqual(result["sources"], {"gal": "success"})
                self.assertEqual(len(result["items"]), 1)
                row = result["items"][0]
        self.assertEqual(row["source"], "gal")
        self.assertEqual(row["email"], "directory@example.com")
        return row

    def test_get_contact_preserves_notes_only_gal_resolution(self):
        row = self.read_gal("get_contact", notes="目录备注\n第二行")
        self.assertEqual(row["notes"], "目录备注\n第二行")

    def test_find_people_preserves_notes_only_gal_resolution(self):
        row = self.read_gal("find_people", notes="目录备注\n第二行")
        self.assertEqual(row["notes"], "目录备注\n第二行")

    def test_body_takes_precedence_over_directory_notes(self):
        for method in ("get_contact", "find_people"):
            with self.subTest(method=method):
                row = self.read_gal(method, body="正文备注", notes="目录备用备注")
                self.assertEqual(row["notes"], "正文备注")

    def test_empty_body_does_not_fall_back_to_directory_notes(self):
        for method in ("get_contact", "find_people"):
            with self.subTest(method=method):
                row = self.read_gal(method, body="", notes="目录备用备注")
                self.assertEqual(row["notes"], "")

    def test_missing_body_and_directory_notes_returns_none(self):
        for method in ("get_contact", "find_people"):
            with self.subTest(method=method):
                self.assertIsNone(self.read_gal(method)["notes"])


if __name__ == "__main__":
    unittest.main()
