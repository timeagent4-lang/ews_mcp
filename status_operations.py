"""Adapter status: current mailbox counters and effective safety settings.

Deliberately exposes no credentials or other-mailbox statistics.
"""

import os


class StatusOperations:
    def get_server_status(self):
        flags = {
            "send_enabled": os.getenv("EWS_MCP_SEND_ENABLED", "false").lower()
            in ("1", "true", "yes"),
            "cache_enabled": os.getenv("EWS_MCP_CACHE_ENABLED", "false").lower()
            in ("1", "true", "yes"),
        }
        mirror = getattr(self, "_mirror", None)
        mirror_coverage = (
            getattr(mirror, "coverage", None) if mirror is not None else None
        )
        mailboxes_accessed = 1 if self.account is not None else 0
        return {
            "mailbox": self.config.email,
            "mailboxes_accessed": mailboxes_accessed,
            "safety_settings": flags,
            "mirror_coverage": mirror_coverage,
            "data_dir": os.path.basename(os.getenv("EWS_MCP_DATA_DIR", "")),
        }
