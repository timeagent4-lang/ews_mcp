import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock

from jsonschema import Draft202012Validator

from people_operations import PeopleOperations
from tool_specs import SPECS
from tool_support import ToolOperationError


class PeoplePaginationTests(unittest.TestCase):
    def setUp(self):
        self.ops = PeopleOperations()
        self.ops._tool_folder = Mock()
        self.ops._resolve_directory = Mock(return_value=[])
        self.ops._prepare_directory = Mock()

    def test_invalid_limit_is_rejected_before_any_source_access(self):
        for limit in (0, -1, 101, True, False, 1.5, "20", None):
            for source, directory in (("gal", None), ("contacts", None),
                                      ("auto", None), ("contacts", "team")):
                with self.subTest(limit=limit, source=source, directory=directory):
                    with self.assertRaises(ToolOperationError) as caught:
                        self.ops.find_people("Alex", source, limit, directory)
                    self.assertEqual(caught.exception.code, "INVALID_PAGINATION")
        self.ops._tool_folder.assert_not_called()
        self.ops._resolve_directory.assert_not_called()
        self.ops._prepare_directory.assert_not_called()

    def test_public_schema_matches_business_limit_bounds(self):
        validator = Draft202012Validator(SPECS["find_people"]["inputSchema"])
        base = {"lanid": "employee", "name": "员工", "query": "Alex"}
        for limit in (0, -1, 101, True, 1.5, "20", None):
            with self.subTest(limit=limit):
                self.assertFalse(validator.is_valid({**base, "limit": limit}))
        for limit in (1, 20, 100):
            validator.validate({**base, "limit": limit})

    def test_valid_boundaries_reach_contacts_slice_unchanged(self):
        folder = MagicMock()
        query = folder.filter.return_value.order_by.return_value
        query.__getitem__.return_value = []
        self.ops._tool_folder.return_value = folder
        for limit in (1, 100):
            with self.subTest(limit=limit):
                result = self.ops.find_people(" Alex ", "contacts", limit)
                query.__getitem__.assert_called_with(slice(None, limit))
                self.assertEqual(result, {"items": [], "sources": {"contacts": "ok"}})
        folder.filter.assert_called_with(display_name__icontains="Alex")

    def test_one_denied_source_remains_partial_and_keeps_other_results(self):
        from exchangelib.errors import ErrorAccessDenied
        self.ops._tool_folder.side_effect = ErrorAccessDenied("denied")
        self.ops._resolve_directory.return_value = [
            (SimpleNamespace(name="Alex", email_address="alex@example.com"), None),
        ]
        result = self.ops.find_people("Alex")
        self.assertTrue(result["partial"])
        self.assertEqual(result["sources"], {"gal": "ok", "contacts": "denied"})
        self.assertEqual(result["items"][0]["email"], "alex@example.com")


if __name__ == "__main__":
    unittest.main()
