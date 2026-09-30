"""通过 OA/ESB 联系人接口校验请求者姓名并解析邮箱。"""

import itertools
import json
import os
import random
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

import requests


HEADERS = {
    "Content-Type": "application/json; charset=utf-8",
}

_sequence = itertools.count(1)
_LOCAL_PART_PATTERN = re.compile(
    r"^[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+$"
)
_DOMAIN_LABEL_PATTERN = re.compile(r"^[A-Za-z0-9-]+$")

LOCAL_TIMEZONE = ZoneInfo(os.getenv("MAIL_LOCAL_TIMEZONE", "Asia/Shanghai"))


def _format_local_datetime(value) -> Optional[str]:
    """把 aware/naive datetime 或 ISO 字符串格式化为本地时区，失败返回 None。"""
    if value is None:
        return None
    dt = value
    if not isinstance(dt, datetime):
        try:
            dt = datetime.fromisoformat(str(value))
        except (TypeError, ValueError, OverflowError):
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TIMEZONE)
    else:
        dt = dt.astimezone(LOCAL_TIMEZONE)
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _bounded_llm_input(rows, limit: int = 60000) -> str:
    """把待分析的行拼成一段文本并限制总长度，避免 LLM 输入超限。"""
    if isinstance(rows, str):
        lines = [rows]
    else:
        lines = list(rows or [])
    joined = "\n".join(str(row) for row in lines)
    if len(joined) > limit:
        return joined[:limit]
    return joined


class IdentityResolutionError(PermissionError):
    """LANID 无法唯一解析为有效邮箱。"""


class IdentityNameMismatchError(IdentityResolutionError):
    """显式姓名与 OA 联系人姓名不一致。"""


@dataclass(frozen=True)
class ResolvedIdentity:
    lanid: str
    requested_name: str
    oa_name: str
    email: str


class OAServiceError(RuntimeError):
    """OA/ESB 联系人服务返回了不可用的响应。"""


def normalize_lanid(value: str) -> str:
    """兼容纯 LANID 和 DOMAIN\\LANID，OA 请求只发送 LANID。"""

    normalized = str(value or "").strip()
    if "\\" in normalized:
        normalized = normalized.rsplit("\\", 1)[-1].strip()
    if not normalized:
        raise IdentityResolutionError("LANID 为空")
    return normalized


def _load_settings() -> Dict[str, Any]:
    names = (
        "OA_BASE_URL",
        "OA_PATH",
        "SVC_CD",
        "SVC_SCN",
        "CNSMR_SYS_ID",
        "TLR_NO",
        "OA_TIMEOUT",
    )
    settings = {
        name: str(os.getenv(name) or "").strip()
        for name in names
    }
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise ValueError("缺少 OA 联系人查询配置: " + ", ".join(missing))

    timeout_text = settings["OA_TIMEOUT"]
    try:
        timeout = int(timeout_text)
    except ValueError as exc:
        raise ValueError("OA_TIMEOUT 必须是正整数") from exc
    if timeout <= 0:
        raise ValueError("OA_TIMEOUT 必须是正整数")
    settings["OA_TIMEOUT"] = timeout
    return settings


def _build_request_body(lanid: str, settings: Dict[str, Any]) -> dict:
    now = datetime.now()
    txn_dt = now.strftime("%Y%m%d")
    txn_tm = now.strftime("%H%M%S")
    timestamp = int(now.timestamp())
    cnsmr_seq_no = f"{timestamp}{random.randint(1000, 9999)}"
    sequence_number = next(_sequence)
    sequence_number_text = f"{sequence_number:012d}"
    glbl_seq_no = (
        f"{settings['CNSMR_SYS_ID']}{txn_dt}{txn_tm}"
        f"{sequence_number_text}"
    )

    return {
        "Head": {
            "SvcCd": settings["SVC_CD"],
            "SvcScn": settings["SVC_SCN"],
            "CnlTp": "",
            "CnsmrSysId": settings["CNSMR_SYS_ID"],
            "CnsmrSeqNo": cnsmr_seq_no,
            "SrcCnsmrSysId": "",
            "GlblSeqNo": glbl_seq_no,
            "TxnDt": txn_dt,
            "TxnTm": txn_tm,
            "CnsmrSvrId": "",
            "SvcVerNo": "",
            "InstId": "",
            "TlrNo": settings["TLR_NO"],
            "TmlNo": "",
            "IPAdr": "",
            "LangCode": "",
        },
        "Body": {
            "EmlNm": "",
            "LANIDCd": lanid,
            "Email": "",
            "ChinNm": "",
        },
    }


def _normalize_email(value: object) -> str:
    """只接受 OA 返回的单个、格式明确的邮箱字符串。"""

    if not isinstance(value, str):
        raise IdentityResolutionError("LANID 未对应有效邮箱")

    email = value.strip().lower()
    if (
        not email
        or len(email) > 254
        or email.count("@") != 1
        or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in email
        )
    ):
        raise IdentityResolutionError("LANID 未对应有效邮箱")

    local_part, domain = email.split("@", 1)
    if (
        not local_part
        or len(local_part) > 64
        or local_part.startswith(".")
        or local_part.endswith(".")
        or ".." in local_part
        or not _LOCAL_PART_PATTERN.fullmatch(local_part)
    ):
        raise IdentityResolutionError("LANID 未对应有效邮箱")

    labels = domain.split(".")
    if (
        not domain
        or len(domain) > 253
        or any(
            not label
            or label.startswith("-")
            or label.endswith("-")
            or not _DOMAIN_LABEL_PATTERN.fullmatch(label)
            for label in labels
        )
    ):
        raise IdentityResolutionError("LANID 未对应有效邮箱")

    return email


def _normalize_expected_name(value: object) -> str:
    if not isinstance(value, str):
        raise IdentityNameMismatchError("请求者姓名无效")
    normalized = value.strip()
    if not normalized:
        raise IdentityNameMismatchError("请求者姓名无效")
    return normalized


def _normalize_oa_name(value: object) -> str:
    if not isinstance(value, str):
        raise IdentityNameMismatchError("OA 联系人姓名无效")
    normalized = value.strip()
    if not normalized:
        raise IdentityNameMismatchError("OA 联系人姓名无效")
    return normalized


def resolve_identity_by_lanid(
    lanid: str,
    expected_name: str,
) -> ResolvedIdentity:
    """仅按 LANID 查询 OA，并校验唯一联系人的姓名和邮箱。"""

    normalized_lanid = normalize_lanid(lanid)
    normalized_expected_name = _normalize_expected_name(expected_name)
    settings = _load_settings()
    url = settings["OA_BASE_URL"].rstrip("/") + settings["OA_PATH"]
    request_body = _build_request_body(normalized_lanid, settings)
    request_bytes = json.dumps(
        request_body,
        ensure_ascii=False,
    ).encode("utf-8")

    response = requests.post(
        url,
        data=request_bytes,
        headers=HEADERS,
        timeout=settings["OA_TIMEOUT"],
    )
    if response.status_code != 200:
        raise OAServiceError("OA 联系人查询失败")

    try:
        response_json = response.json()
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise OAServiceError("OA 联系人响应不是有效 JSON") from exc

    body = response_json.get("Body", {})
    contacts = body.get("CtcInfArry", []) if isinstance(body, dict) else []
    if not isinstance(contacts, list) or len(contacts) != 1:
        raise IdentityResolutionError("LANID 未唯一对应联系人")

    contact = contacts[0]
    if not isinstance(contact, dict):
        raise IdentityResolutionError("LANID 未对应有效联系人")

    oa_name = _normalize_oa_name(contact.get("ChinNm1"))
    if oa_name != normalized_expected_name:
        raise IdentityNameMismatchError("请求者姓名与 OA 联系人不一致")

    return ResolvedIdentity(
        lanid=normalized_lanid,
        requested_name=normalized_expected_name,
        oa_name=oa_name,
        email=_normalize_email(contact.get("EmailAdr")),
    )
