"""aias_mcp: 单次凭据、直接执行、统一结果与安全服务日志。"""
import json
import logging
import os
import secrets
import traceback
from pathlib import Path

import mcp.types as mcp_types
from mcp.shared import session as mcp_session
from anyio import to_thread
from dotenv import load_dotenv
from fastmcp import Context, FastMCP
from fastmcp.server.middleware import Middleware
from fastmcp.tools.tool import FunctionTool, ParsedFunction, ToolResult
from jsonschema import Draft202012Validator
from starlette.middleware import Middleware as HTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

load_dotenv()
from attachment_downloads import download_lifespan, register_download_route
from config import OutlookConfig
from outlook_client import OutlookClient
from tool_specs import SPECS, TOOL_NAMES
from tool_support import ToolOperationError, error_details
from utils.audit import ToolAuditMiddleware, current_request_id, install_tool_audit_fallback, request_scope

logging.basicConfig(level=logging.INFO, format="%(message)s")


class MCPSessionLogFilter(logging.Filter):
    """SDK 会话直接调用 root logger，且协议校验发生在请求脱敏上下文之前。"""

    def filter(self, record):
        if os.path.normcase(record.pathname) == os.path.normcase(mcp_session.__file__):
            record.msg = json.dumps({"event_type": "mcp_session_diagnostic", "level": record.levelname,
                                     "line": record.lineno, "function": record.funcName})
            record.args = ()
            record.exc_info = record.exc_text = record.stack_info = None
        return True


logging.getLogger().addFilter(MCPSessionLogFilter())
# SDK 在异常分支可能打印认证信息及完整 XML。业务错误统一走安全适配，
# 阻断其原始日志传播；服务日志保留我们的请求记录和安全栈位置。
for sdk_logger_name in ("exchangelib", "mcp.server", "fastmcp.server", "fastmcp.tools"):
    sdk_logger = logging.getLogger(sdk_logger_name)
    sdk_logger.addHandler(logging.NullHandler())
    sdk_logger.propagate = False
logger = logging.getLogger(__name__)
# 本服务统一校验并返回安全错误；SDK 的原始校验错误会回显参数值。
mcp = FastMCP("AiasExchangeServer", lifespan=download_lifespan, strict_input_validation=False)
tool_audit = ToolAuditMiddleware()
mcp.add_middleware(tool_audit)
install_tool_audit_fallback(mcp, tool_audit)
CONTROL_FIELDS = frozenset(("mailbox", "username", "password"))
_INPUT_VALIDATORS = {name: Draft202012Validator(spec["inputSchema"]) for name, spec in SPECS.items()}
_SCHEMA_ERROR_MESSAGES = {
    "INVALID_DATETIME": "时间格式无效，请使用 ISO 日期时间。",
    "INVALID_PAGINATION": "分页参数类型或范围无效。",
    "INVALID_FOLDER": "邮件文件夹别名无效。",
    "INVALID_BODY_FORMAT": "正文格式无效：仅支持 text/html，回复和转发的新增正文使用 text。",
    "INVALID_BODY_ACTION": "正文处理方式仅支持 replace/prepend。",
    "INVALID_PARAMS": "参数不符合公开 schema，请检查必填字段、类型、范围与字段组合。",
}


def _validate_arguments(tool_name, arguments):
    validator = _INPUT_VALIDATORS.get(tool_name)
    if validator is None:
        raise ToolOperationError("UNKNOWN_TOOL", "该工具未注册。")
    error = next(validator.iter_errors(arguments), None)
    if error is not None:
        node = validator.schema
        for part in error.absolute_path:
            node = node.get("properties", {}).get(str(part), {})
        code = node.get("x-error-code", "INVALID_PARAMS")
        if code not in _SCHEMA_ERROR_MESSAGES:
            code = "INVALID_PARAMS"
        raise ToolOperationError(code, _SCHEMA_ERROR_MESSAGES[code])


def _correlate_errors(value):
    if isinstance(value, dict):
        if "error_code" in value:
            value["request_id"] = current_request_id()
        for child in value.values():
            _correlate_errors(child)
    elif isinstance(value, list):
        for child in value:
            _correlate_errors(child)


def _tool_result(payload):
    _correlate_errors(payload)
    return ToolResult(content=json.dumps(payload, ensure_ascii=False), structured_content=payload)


def _tool_failure(tool_name, *, exc):
    details = error_details(exc)
    payload = {"ok": False, "results": None, **details, "request_id": current_request_id()}
    # 仅记录异常类型和栈位置，不记录原消息、异常链文本或局部变量。
    logger.error(json.dumps({
        "event_type": "mcp_tool_diagnostic", "request_id": current_request_id(), "tool": tool_name if tool_name in SPECS else "unknown_tool",
        "error_code": details["error_code"], "error_type": type(exc).__name__,
        "locations": [{"file": Path(frame.filename).name, "line": frame.lineno, "function": frame.name}
                      for frame in traceback.extract_tb(exc.__traceback__)],
    }, ensure_ascii=False))
    return _tool_result(payload)


class ToolInputValidationMiddleware(Middleware):
    async def on_call_tool(self, context, call_next):
        try:
            _validate_arguments(context.message.name, context.message.arguments)
        except ToolOperationError as exc:
            return _tool_failure(context.message.name, exc=exc)
        return await call_next(context)


mcp.add_middleware(ToolInputValidationMiddleware())


def initialize_outlook_client(params):
    return OutlookClient(OutlookConfig.from_tool_params(
        params["mailbox"], username=params.get("username"), password=params.get("password"),
    ))


def _wrap_result(result):
    status = "success"
    extras = {}
    if isinstance(result, dict) and isinstance(result.get("results"), list) and any(
        isinstance(row, dict) and ("success" in row or "status" in row) for row in result["results"]
    ):
        rows = result["results"]
        for row in rows:
            if row.get("success") is False or row.get("status") in ("failed", "unknown"):
                row["request_id"] = current_request_id()
        unknown = sum(row.get("status") == "unknown" for row in rows)
        success = sum(row.get("success") is True or row.get("status") == "success" for row in rows)
        failed = len(rows) - success - unknown
        status = "unknown" if unknown else ("partial" if success and failed else "failed" if failed else "success")
        extras = {k: v for k, v in result.items() if k not in ("results", "status", "ok")}
        extras.update(success_count=success, failed_count=failed, unknown_count=unknown)
        result = rows
    elif isinstance(result, dict) and "items" in result:
        extras = {k: v for k, v in result.items() if k not in ("items", "status", "ok")}
        status = result.get("status", "success")
        if status not in ("success", "failed", "partial", "unknown"):
            status = "success"
        if result.get("partial") and status == "success":
            coverage = result.get("coverage", {})
            status = "failed" if coverage and not any(v == "ok" for v in coverage.values()) else "partial"
        result = result["items"]
    elif isinstance(result, dict) and result.get("partial"):
        coverage = result.get("coverage", {})
        status = "failed" if coverage and not any(v == "ok" for v in coverage.values()) else "partial"
    payload = {"results": result, **extras, "ok": status == "success", "status": status, "request_id": current_request_id()}
    if status != "success":
        payload.setdefault("error_code", {"partial": "PARTIAL_FAILURE", "unknown": "OUTCOME_UNKNOWN", "failed": "OPERATION_FAILED"}[status])
        payload.setdefault("message", {"partial": "操作部分失败，请检查逐项或逐来源错误。", "unknown": "部分操作结果未知，请先核对实际邮箱，勿重复提交。", "failed": "操作失败，请检查逐项或逐来源错误。"}[status])
    return _tool_result(payload)


def _dispatch(tool_name, params, ctx=None):
    with request_scope(params):
        try:
            _validate_arguments(tool_name, params)
            client = initialize_outlook_client(params)
            business = {key: value for key, value in params.items() if key not in CONTROL_FIELDS}
            return _wrap_result(getattr(client, tool_name)(**business))
        except Exception as exc:
            return _tool_failure(tool_name, exc=exc)


def _make_handler(tool_name):
    async def handler(ctx: Context = None, **params) -> ToolResult:
        # 已开始的 SDK 调用等待真实结果；取消请求不丢弃后台写入结果。
        return await to_thread.run_sync(_dispatch, tool_name, params, ctx)
    handler.__name__ = f"tool_{tool_name}"
    return handler


for name in TOOL_NAMES:
    spec = SPECS[name]
    parsed = ParsedFunction.from_function(_make_handler(name), validate=False)
    mcp.add_tool(FunctionTool(fn=parsed.fn, name=name, description=spec["description"], parameters=spec["inputSchema"], output_schema=parsed.output_schema))
register_download_route(mcp)


def install_failure_envelope(fastmcp):
    # SDK 的普通 ToolResult 不能设置 isError，使用真实低层返回保留结构化失败。
    low_level = fastmcp._mcp_server
    original = low_level.request_handlers[mcp_types.CallToolRequest]
    async def envelope_handler(request):
        result = await original(request)
        root = getattr(result, "root", result)
        payload = getattr(root, "structuredContent", None)
        if isinstance(payload, dict) and payload.get("ok") is False:
            root.isError = True
        return result
    low_level.request_handlers[mcp_types.CallToolRequest] = envelope_handler


install_failure_envelope(mcp)


@mcp.custom_route("/healthz", methods=["GET"], include_in_schema=False)
async def healthz(request: Request):
    return JSONResponse({"status": "ok", "service": "aias_mcp", "tool_count": len(TOOL_NAMES)})


class MCPKeyAuthMiddleware:
    """Gate every HTTP request to the MCP endpoint behind a shared key.

    Deliberately scoped to ``/mcp`` only: ``/healthz`` (liveness probe) and the
    signed ``/downloads/{identifier}`` routes stay open, so the container
    healthcheck and attachment links keep working.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self.api_key = api_key.encode("utf-8")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["path"].rstrip("/") == "/mcp":
            request = Request(scope)
            keys = request.query_params.getlist("key")
            if len(keys) != 1 or not secrets.compare_digest(
                keys[0].encode("utf-8"), self.api_key
            ):
                response = JSONResponse(
                    {"error": "Invalid or missing API key"},
                    status_code=401,
                )
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def main():
    host = os.getenv("EWS_MCP_HOST", "127.0.0.1")
    port = int(os.getenv("EWS_MCP_PORT", "7805"))
    api_key = os.getenv("EWS_MCP_API_KEY", "")
    if not api_key.strip():
        raise ValueError("缺少 EWS_MCP_API_KEY")
    logger.info("aias_mcp listening on %s:%s", host, port)
    mcp.run(transport="streamable-http", host=host, port=port, path="/mcp",
            middleware=[HTTPMiddleware(MCPKeyAuthMiddleware, api_key=api_key)],
            uvicorn_config={"access_log": False})


if __name__ == "__main__":
    main()
