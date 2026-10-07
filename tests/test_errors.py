import os
import unittest
from unittest.mock import patch

import requests
from exchangelib.errors import ErrorAccessDenied, ErrorInvalidChangeKey, InvalidEnumValue, InvalidTypeError, NaiveDateTimeNotAllowed, UnauthorizedError

import tool_support


class ErrorTests(unittest.TestCase):
    def test_authentication_is_not_generic_exchange(self):
        error = tool_support.error_details(UnauthorizedError("login failed"))
        self.assertEqual(error["error_code"], "AUTHENTICATION_FAILED")
        self.assertEqual(error["status"], "failed")

    def test_pre_submit_connection_is_failed_post_submit_unknown(self):
        exc = requests.ConnectionError("connection failed")
        self.assertEqual(tool_support.error_details(exc)["status"], "failed")
        self.assertEqual(tool_support.error_details(exc, submitted=True)["status"], "unknown")

    def test_exchange_rejection_and_conflict_are_definite(self):
        for exc in (ErrorAccessDenied("denied"), ErrorInvalidChangeKey("conflict")):
            result = tool_support.error_details(exc, submitted=True)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["exchange_code"], type(exc).__name__)

    def test_wrapped_exchange_preserves_cause_and_status(self):
        try:
            raise ErrorAccessDenied("denied")
        except ErrorAccessDenied as cause:
            try:
                raise tool_support.ToolOperationError("CONTACT_WRITE_FAILED", "联系人保存失败") from cause
            except tool_support.ToolOperationError as exc:
                result = tool_support.error_details(exc)
        self.assertEqual(result["error_code"], "CONTACT_WRITE_FAILED")
        self.assertEqual(result["exchange_code"], "ErrorAccessDenied")

    def test_partial_carries_known_result(self):
        exc = tool_support.ToolOperationError("PHOTO_FAILED", "照片保存失败", status="partial", results={"id": "saved"})
        result = tool_support.error_details(exc)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["results"], {"id": "saved"})

    def test_short_server_secrets_are_redacted(self):
        with patch.dict(os.environ, {"OUTLOOK_ADMIN_PASSWORD": "pw!", "EWS_MCP_API_KEY": "key!"}):
            self.assertNotIn("pw!", tool_support.sanitize("error pw! and key!"))
            self.assertNotIn("key!", tool_support.sanitize("error pw! and key!"))

    def test_program_error_is_safe_and_unknown_only_after_submit(self):
        result = tool_support.error_details(TypeError("private body"))
        self.assertEqual(result["error_code"], "INTERNAL_ERROR")
        self.assertNotIn("private body", result["message"])
        self.assertEqual(tool_support.error_details(TypeError("private body"), submitted=True)["status"], "unknown")

    def test_named_sdk_local_validation_is_failed_even_inside_save(self):
        for error in (InvalidEnumValue("field", "bad", ["allowed"]), InvalidTypeError("field", "bad", int), NaiveDateTimeNotAllowed("date")):
            result = tool_support.error_details(error, submitted=True)
            self.assertEqual(result["status"], "failed")
            self.assertEqual(result["error_code"], "INVALID_PARAMS")

    def test_generic_sdk_value_error_after_write_remains_unknown(self):
        # Item._update raises this when a successful response has a different ID.
        self.assertEqual(tool_support.error_details(ValueError("unexpected returned id"), submitted=True)["status"], "unknown")


if __name__ == "__main__":
    unittest.main()
