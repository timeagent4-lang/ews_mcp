"""Contact field contracts at the real SDK serialization boundary."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from exchangelib import Account, Body, Contact
from jsonschema import Draft202012Validator

from people_operations import PeopleOperations
from tool_specs import SPECS
from tool_support import ToolOperationError


class ContactFieldsTests(unittest.TestCase):
    def setUp(self):
        self.ops = PeopleOperations()
        self.ops.config = SimpleNamespace(email="employee@example.com")
        self.ops.account = Mock(spec=Account)
        self.ops._tool_folder = Mock(return_value=None)
        self.saved = []

        def save(item):
            self.saved.append(item)
            item.id, item.changekey = "contact", "v1"
        self.saving = patch.object(Contact, "save", autospec=True, side_effect=save)
        self.saving.start()
        self.addCleanup(self.saving.stop)

    def test_optional_email_and_new_fields_survive_preview_save_receipt_and_detail(self):
        fields = dict(display_name="Alex", phone="1001", mobile_phone="2002",
                      department="研发", office="A301", notes="普通文本备注")
        validator = Draft202012Validator(SPECS["create_contact"]["inputSchema"])
        validator.validate({"lanid": "employee", **fields})
        preview = self.ops.create_contact(**fields)
        self.assertEqual(self.saved, [])
        for key, value in fields.items():
            self.assertEqual(preview["preview"]["items"][0][key], value)
        receipt = self.ops.create_contact(**fields, confirm=True)
        item = self.saved[0]
        self.assertEqual(item.email_addresses, [])
        self.assertIsInstance(item.body, Body)
        self.assertEqual(str(item.body), fields["notes"])
        self.assertEqual({p.label: p.phone_number for p in item.phone_numbers},
                         {"BusinessPhone": "1001", "MobilePhone": "2002"})
        self.ops._tool_item = Mock(return_value=item)
        detail = self.ops.get_contact("contact")
        for key, value in fields.items():
            self.assertEqual(receipt[key], value)
            self.assertEqual(detail[key], value)

    def test_name_only_and_mobile_only_do_not_invent_email_or_business_phone(self):
        for extra in ({}, {"mobile_phone": "2002"}):
            receipt = self.ops.create_contact(display_name="Alex", confirm=True, **extra)
            self.assertIsNone(receipt["email"])
            self.assertIsNone(receipt["phone"])
            self.assertEqual(self.saved[-1].email_addresses, [])

    def test_supplied_invalid_email_is_rejected_before_save(self):
        for email in ("", "bad", "a@example.com;b@example.com"):
            with self.assertRaises(ToolOperationError) as caught:
                self.ops.create_contact("Alex", email=email, confirm=True)
            self.assertEqual(caught.exception.code, "INVALID_CONTACT_EMAIL")
        self.assertEqual(self.saved, [])
