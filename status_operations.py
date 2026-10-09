"""Current-mailbox counters and safety settings, without credentials."""

import os


class StatusOperations:
    def get_server_status(self):
        flags = {
            "send_enabled": os.getenv("EWS_MCP_SEND_ENABLED", "false").lower()
            in ("1", "true", "yes"),
        }
        mailboxes_accessed = 1 if self.account is not None else 0
        return {
            "mailbox": self.config.email,
            "mailboxes_accessed": mailboxes_accessed,
            "safety_settings": flags,
            "data_dir": os.path.basename(os.getenv("EWS_MCP_DATA_DIR", "")),
        }
