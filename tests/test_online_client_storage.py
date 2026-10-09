"""Run real client imports in isolation from older tests' SDK/OA stubs."""

import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


class OnlineClientStorageTests(unittest.TestCase):
    def run_client_check(self, script):
        result = subprocess.run(
            [sys.executable, "-B", "-c", textwrap.dedent(script)],
            cwd=Path(__file__).resolve().parents[1],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_obsolete_local_state_cannot_block_online_client(self):
        self.run_client_check('''
            import os
            import tempfile
            from pathlib import Path
            from unittest.mock import patch
            from config import OutlookConfig
            from outlook_client import OutlookClient

            with tempfile.TemporaryDirectory() as root:
                # A stale/unusable old database must not affect live mail tools.
                old_database = Path(root, "mirror.db")
                old_database.mkdir()
                marker = old_database / "keep"
                marker.write_bytes(b"existing local data")
                for legacy_flag in ("false", "true"):
                    with patch.dict(os.environ, {
                        "EWS_MCP_DATA_DIR": root,
                        "EWS_MCP_CACHE_ENABLED": legacy_flag,
                    }), patch.object(OutlookClient, "_connect", return_value=object()):
                        client = OutlookClient(OutlookConfig(email="a@example.com"))
                        assert client.get_server_status()["mailboxes_accessed"] == 1
                assert marker.read_bytes() == b"existing local data"
                assert sorted(p.relative_to(root).as_posix() for p in Path(root).rglob("*")) == [
                    "mirror.db", "mirror.db/keep",
                ]
        ''')

    def test_online_status_reports_only_current_safety_settings(self):
        self.run_client_check('''
            import os
            from unittest.mock import patch
            from config import OutlookConfig
            from outlook_client import OutlookClient

            for send_flag, enabled in (("false", False), ("true", True)):
                with patch.dict(os.environ, {
                    "EWS_MCP_DATA_DIR": "",
                    "EWS_MCP_CACHE_ENABLED": "true",
                    "EWS_MCP_SEND_ENABLED": send_flag,
                }), patch.object(OutlookClient, "_connect", return_value=object()):
                    status = OutlookClient(OutlookConfig(email="a@example.com")).get_server_status()
                    assert status == {
                        "mailbox": "a@example.com", "mailboxes_accessed": 1,
                        "safety_settings": {"send_enabled": enabled}, "data_dir": "",
                    }, status
        ''')


if __name__ == "__main__":
    unittest.main()
