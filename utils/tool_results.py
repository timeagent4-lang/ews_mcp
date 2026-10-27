"""Shared business verdicts and MCP's text/structured compatibility output."""

import json

from fastmcp.tools.tool import ToolResult

ACTIVE_STATUSES = frozenset(("pending", "confirmed", "executing", "processing"))
FAILED_STATUSES = frozenset(("failed", "partial", "unknown"))


def result_is_error(payload):
    if payload.get("status") in ACTIVE_STATUSES:
        return False
    return payload.get("status") in FAILED_STATUSES or bool(payload.get("error_code"))


def json_result(payload):
    return ToolResult(
        content=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        structured_content=payload,
    )
