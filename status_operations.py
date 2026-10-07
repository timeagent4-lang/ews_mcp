"""Adapter status: current mailbox counters and effective safety settings.

Deliberately exposes no credentials or other-mailbox statistics.
"""

from config import send_enabled


class StatusOperations:
    def get_server_status(self):
        flags = {
            "send_enabled": send_enabled(),
        }
        mailboxes_accessed = 1 if self.account is not None else 0
        return {
            "mailbox": self.config.email,
            "mailboxes_accessed": mailboxes_accessed,
            "safety_settings": flags,
        }
