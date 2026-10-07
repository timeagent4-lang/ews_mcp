"""Service configuration checks without OA or Exchange network access."""
import os
import unittest
from unittest.mock import patch

from exchangelib import Credentials

from config import OutlookConfig


class OutlookConfigTests(unittest.TestCase):
    def test_personal_uses_mailbox_default_and_preserves_password(self):
        with patch.dict(os.environ, {"OUTLOOK_SERVER": "exchange.example.com"}, clear=True):
            config = OutlookConfig.from_tool_params(" User@Example.com ", password=" secret ")
        self.assertEqual(config.email, "user@example.com")
        self.assertEqual(config.lanid, "user@example.com")
        self.assertEqual(config.password, " secret ")
        self.assertEqual(config.credential_mode, "personal")
        self.assertNotIn(" secret ", repr(config))

    def test_empty_password_uses_service_and_ignores_username(self):
        for password in (None, ""):
            with self.subTest(password=password), patch.dict(os.environ, {
                "OUTLOOK_SERVER": "exchange.example.com", "OUTLOOK_ADMIN_LANID": "public",
                "OUTLOOK_ADMIN_PASSWORD": "public-password",
            }, clear=True):
                config = OutlookConfig.from_tool_params("user@example.com", username="ignored", password=password)
            self.assertEqual(config.lanid, "public")
            self.assertEqual(config.credential_mode, "delegate")

    def test_whitespace_password_is_supplied(self):
        with patch.dict(os.environ, {"OUTLOOK_SERVER": "exchange.example.com"}, clear=True):
            config = OutlookConfig.from_tool_params("user@example.com", password="  ")
        self.assertEqual(config.password, "  ")
        self.assertEqual(config.credential_mode, "personal")

    def test_invalid_config_has_machine_readable_code(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as caught:
                OutlookConfig.from_tool_params("user@example.com", password="personal")
        self.assertEqual(caught.exception.code, "CONFIG_ERROR")

    def test_service_lanid_reaches_exchange_credentials(self):
        with patch.dict(os.environ, {
            "OUTLOOK_ADMIN_LANID": " svc-user ",
            "OUTLOOK_ADMIN_PASSWORD": "test-password",
            "OUTLOOK_SERVER": " exchange.example.com ",
        }, clear=True):
            config = OutlookConfig.from_service_env(" Employee@Example.COM ")
        # Use the same configuration attributes as OutlookClient._create_account
        # and real SDK credentials; do not instantiate a networked Account.
        credentials = Credentials(username=config.lanid, password=config.password)
        self.assertEqual(credentials.username, "svc-user")
        self.assertEqual(credentials.password, "test-password")
        self.assertEqual(config.email, "employee@example.com")
        self.assertEqual(config.server, "exchange.example.com")

    def test_missing_or_blank_lanid_reports_canonical_environment_name(self):
        for value in (None, "", "   "):
            with self.subTest(value=value):
                env = {"OUTLOOK_ADMIN_PASSWORD": "test-password", "OUTLOOK_SERVER": "exchange.example.com"}
                if value is not None:
                    env["OUTLOOK_ADMIN_LANID"] = value
                with patch.dict(os.environ, env, clear=True):
                    with self.assertRaisesRegex(ValueError, "OUTLOOK_ADMIN_LANID"):
                        OutlookConfig.from_service_env("employee@example.com")


if __name__ == "__main__":
    unittest.main()
