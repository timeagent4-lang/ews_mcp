"""Fresh-process imports exercise the environment read at module startup."""

import os
from pathlib import Path
import subprocess
import sys
import unittest


class TimezoneStartupTests(unittest.TestCase):
    def test_blank_timezone_defaults_and_explicit_timezone_is_preserved(self):
        source = 'import mcp_server; from utils.lanid_email import LOCAL_TIMEZONE; print("TIMEZONE=" + LOCAL_TIMEZONE.key)'
        for value, expected in ((None, 'Asia/Shanghai'), ('', 'Asia/Shanghai'),
                                ('  \t ', 'Asia/Shanghai'), ('UTC', 'UTC'), (' UTC ', 'UTC')):
            with self.subTest(value=value):
                env = os.environ.copy()
                if value is None:
                    env.pop('MAIL_LOCAL_TIMEZONE', None)
                else:
                    env['MAIL_LOCAL_TIMEZONE'] = value
                result = subprocess.run([sys.executable, '-B', '-c', source], env=env,
                    cwd=Path(__file__).resolve().parents[1], capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('TIMEZONE=' + expected, result.stdout)


if __name__ == '__main__':
    unittest.main()
