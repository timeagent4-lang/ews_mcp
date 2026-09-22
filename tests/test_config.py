"""Service configuration checks without OA or Exchange network access."""
import os
import unittest
from unittest.mock import patch

from exchangelib import Credentials

from config import OutlookConfig


class OutlookConfigTests(unittest.TestCase):
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
