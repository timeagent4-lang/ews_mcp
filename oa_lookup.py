"""按 LANID 只读查询 OA/ESB 联系人与邮箱。"""

import json
import logging
import os
import random
from datetime import datetime
from typing import List, Dict, Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

OA_URL = os.getenv("OA_BASE_URL", "http://<REDACTED>").rstrip("/")
OA_PATH = os.getenv("OA_PATH", "/aias")
SVC_CD = os.getenv("SVC_CD", "50230002")
SVC_SCN = os.getenv("SVC_SCN", "01")
CNSMR_SYS_ID = os.getenv("CNSMR_SYS_ID", "602400")
TLR_NO = os.getenv("TLR_NO", "1ch04654600")

# ESB 要求 Svc 等业务字段同时放在 HTTP Header。
HEADERS = {
    "Content-Type": "application/json",
    "Accept": "application/json",
    "SvcCd": SVC_CD,
    "SvcScn": SVC_SCN,
    "CnsmrSysId": CNSMR_SYS_ID,
    "TlrNo": TLR_NO,
}

_glblSeqNo = 1


def build_request_body(lanid: str) -> Dict[str, Any]:
    """构造 OA ESB 查询报文 Head + Body(LANIDCd=lanid)。"""
    global _glblSeqNo
    now = datetime.now()
    txn_dt = now.strftime("%Y%m%d")
    txn_tm = now.strftime("%H%M%S")
    cnsmr_seq_no = f"{int(now.timestamp())}{random.randint(1000, 9999)}"
    sequence_number_str = f"{_glblSeqNo:012d}"
    _glblSeqNo += 1
    glbl_seq_no = f"{CNSMR_SYS_ID}{txn_dt}{txn_tm}{sequence_number_str}"

    head = {
        "SvcCd": SVC_CD,
        "SvcScn": SVC_SCN,
        "CnlTp": "",
        "CnsmrSysId": CNSMR_SYS_ID,
        "CnsmrSeqNo": cnsmr_seq_no,
        "SrcCnsmrSysId": "",
        "GlblSeqNo": glbl_seq_no,
        "TxnDt": txn_dt,
        "TxnTm": txn_tm,
        "CnsmrSvrId": "",
        "SvcVerNo": "",
        "InstId": "",
        "TlrNo": TLR_NO,
        "TmNo": "",
        "IPAdr": "",
        "LangCode": "",
    }
    return {
        "Head": head,
        "Body": {"EmlNm": "", "LANIDCd": lanid, "Email": "", "ChinNm": ""},
    }


def query_lanid(lanid: str, timeout: int = 15) -> List[Dict[str, Any]]:
    """返回 OA 联系人的原始 ESB 字段。"""
    body = build_request_body(lanid)
    resp = requests.post(
        OA_URL + OA_PATH,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers=HEADERS,
        timeout=timeout,
    )
    resp.raise_for_status()
    parsed = resp.json()
    return parsed.get("Body", {}).get("CtcInfArry") or []


def _normalize_email(value) -> Optional[str]:
    if not isinstance(value, str):
        return None
    email = value.strip().lower()
    if not email or len(email) > 254:
        return None
    if email.count("@") != 1:
        return None
    local, domain = email.split("@")
    if not local or not domain:
        return None
    if any(ord(ch) < 33 or ch.isspace() for ch in email):
        return None
    if local.startswith(".") or local.endswith(".") or ".." in local:
        return None
    for label in domain.split("."):
        if not label or label.startswith("-") or label.endswith("-"):
            return None
        if not all(ch.isalnum() or ch == "-" for ch in label):
            return None
    return email


def email_by_lanid(lanid: str, timeout: int = 15) -> Optional[str]:
    """按 lanid 查 OA，仅当恰好返回 1 条合法记录时返回其邮箱，否则返回 None。"""
    try:
        contacts = query_lanid(lanid, timeout=timeout)
    except requests.exceptions.RequestException as exc:
        logger.warning("OA 查询失败: error_type=%s", exc.__class__.__name__)
        return None
    if not isinstance(contacts, list) or len(contacts) != 1:
        return None
    record = contacts[0]
    if not isinstance(record, dict):
        return None
    return _normalize_email(record.get("EmailAdr"))


def strip_lanid_prefix(raw: str) -> str:
    raw = (raw or "").strip()
    if "\\" in raw:
        raw = raw.rsplit("\\", 1)[-1]
    return raw.strip()
