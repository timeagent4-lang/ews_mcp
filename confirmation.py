"""Persistent two-phase write confirmation state machine.

Every mutating tool first produces a read-only "preview" and persists a pending
operation record (`operation_id` + a random `confirm_token` bound to it).
The caller then re-sends the *identical* business params plus `operation_id`
and `confirm_token` to execute. Records live in a local SQLite db so receipts
and idempotency survive restarts.

Rules enforced here (mirrors the reference 28-tool adapter):
- `operation_id` alone (no token) is a "query" and never executes anything.
- `idempotency_key` is persisted across restarts; a completed key returns its
  receipt instead of re-executing; an in-flight/unknown key is reported read-only.
- Restart migrates any pending/confirmed/executing record to `unknown`.
- Uncertainty (timeouts, non-atomic EWS ops such as OOF, concurrent Outlook
  writers) is kept as `unknown` / `partial` and is never auto-retried.
- No unlock/reconciliation is provided.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import uuid
from datetime import datetime, timezone

STATUS_PENDING = "pending"
STATUS_CONFIRMED = "confirmed"
STATUS_EXECUTING = "executing"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_UNKNOWN = "unknown"
STATUS_PARTIAL = "partial"

_ACTIVE_STATUSES = (STATUS_PENDING, STATUS_CONFIRMED, STATUS_EXECUTING)
_UNCERTAIN_STATUSES = (STATUS_UNKNOWN, STATUS_PARTIAL)


class ConfirmationError(ValueError):
    """Expected confirmation-flow failure with a safe, caller-visible message."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


class OperationStore:
    """SQLite-backed store of confirmation operations (one DB per data dir)."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.RLock()
        if db_path and db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()
        self.migrate_active_to_unknown()

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS operations (
                    operation_id   TEXT PRIMARY KEY,
                    tool           TEXT NOT NULL,
                    mailbox        TEXT NOT NULL,
                    action         TEXT NOT NULL,
                    business_json  TEXT NOT NULL,
                    items_json     TEXT,
                    details_json   TEXT,
                    idempotency_key TEXT,
                    confirm_token  TEXT NOT NULL,
                    status         TEXT NOT NULL,
                    result_json    TEXT,
                    created_at     TEXT NOT NULL,
                    updated_at     TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_operations_idem
                ON operations(idempotency_key);
                """
            )
            self._conn.commit()

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    def _row_to_dict(self, row: sqlite3.Row) -> dict:
        return {
            "operation_id": row["operation_id"],
            "tool": row["tool"],
            "mailbox": row["mailbox"],
            "action": row["action"],
            "business_params": json.loads(row["business_json"] or "{}"),
            "items": json.loads(row["items_json"]) if row["items_json"] else None,
            "details": json.loads(row["details_json"]) if row["details_json"] else None,
            "idempotency_key": row["idempotency_key"],
            "confirm_token": row["confirm_token"],
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def create_preview(
        self,
        *,
        tool,
        mailbox,
        action,
        business_params,
        items=None,
        details=None,
        idempotency_key=None,
    ) -> dict:
        operation_id = uuid.uuid4().hex
        confirm_token = secrets.token_urlsafe(32)
        now = self._now()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO operations
                (operation_id, tool, mailbox, action, business_json, items_json,
                 details_json, idempotency_key, confirm_token, status, created_at, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    operation_id,
                    tool,
                    mailbox,
                    action,
                    json.dumps(business_params, ensure_ascii=False, sort_keys=True),
                    json.dumps(items, ensure_ascii=False) if items is not None else None,
                    json.dumps(details, ensure_ascii=False) if details is not None else None,
                    idempotency_key,
                    confirm_token,
                    STATUS_PENDING,
                    now,
                    now,
                ),
            )
            self._conn.commit()
        return {
            "operation_id": operation_id,
            "confirm_token": confirm_token,
            "status": STATUS_PENDING,
            "tool": tool,
            "mailbox": mailbox,
            "action": action,
            "items": items,
            "details": details,
        }

    def get(self, operation_id) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM operations WHERE operation_id = ?", (operation_id,)
            ).fetchone()
        return self._row_to_dict(row) if row else None

    def find_by_idempotency_key(self, idempotency_key, mailbox=None) -> dict | None:
        """Most recent operation bound to a key, scoped to `mailbox` when given.

        A key names a business action *within one mailbox*: two mailboxes that
        reuse a key are two different actions, so one must never be answered
        with - or blocked by - the other. Callers must pass the mailbox; the
        unscoped form exists only for whole-store inspection.

        Latest-wins: when an expired preview is safely re-previewed it mints a
        new row under the same key, and that fresh preview is the one that
        governs. A terminal row is never superseded in practice, because the
        idempotency path only mints a new row when the latest one is an expired
        `pending` (i.e. nothing was ever executed under that key).
        """
        sql = (
            "SELECT * FROM operations WHERE idempotency_key = ? "
            "{scope}ORDER BY created_at DESC, rowid DESC LIMIT 1"
        )
        if mailbox is None:
            sql, args = sql.format(scope=""), (idempotency_key,)
        else:
            sql, args = sql.format(scope="AND mailbox = ? "), (idempotency_key, mailbox)
        with self._lock:
            row = self._conn.execute(sql, args).fetchone()
        return self._row_to_dict(row) if row else None

    def begin_confirm(self, operation_id, confirm_token, business_params) -> dict:
        """Validate an execution attempt and atomically claim the run right.

        Only a fresh preview (`pending`) or a definite pre-submit failure
        (`failed`) may be claimed. `executing` / `unknown` / `completed` /
        `partial` records are never re-executed: the claim is a single
        conditional UPDATE so concurrent confirms cannot both win.

        The claim is additionally scoped to the whole `idempotency_key`: a key
        names a *business action*, so if a sibling row under the same key is
        already in-flight or finished, this row must not run either. Without
        that, two live rows sharing a key (concurrent first preview, or
        concurrent rebuild of an expired one) would each execute once. That
        scope is the key *within one mailbox*: the same key used by two
        mailboxes names two different actions and must not cross-block.
        """
        record = self.get(operation_id)
        if record is None:
            raise ConfirmationError(
                "OPERATION_UNKNOWN", "未找到该操作号，请先重新预览。"
            )
        if not secrets.compare_digest(str(confirm_token), record["confirm_token"]):
            raise ConfirmationError(
                "CONFIRMATION_MISMATCH",
                "确认令牌不匹配（本次未执行）；请用正确来源原样重发同一 operation_id。",
            )
        if _normalize(business_params) != _normalize(record["business_params"]):
            raise ConfirmationError(
                "CONFIRMATION_MISMATCH",
                "确认参数与预览不一致（本次未执行）；请以完全相同参数重新预览后确认。",
            )
        now = self._now()
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE operations SET status=?, updated_at=? "
                "WHERE operation_id=? AND status IN (?,?) "
                "AND NOT EXISTS ("
                "  SELECT 1 FROM operations AS sibling "
                "  WHERE sibling.idempotency_key IS NOT NULL "
                "    AND sibling.idempotency_key = operations.idempotency_key "
                "    AND sibling.mailbox = operations.mailbox "
                "    AND sibling.operation_id <> operations.operation_id "
                "    AND sibling.status NOT IN (?,?)"
                ")",
                (
                    STATUS_EXECUTING,
                    now,
                    operation_id,
                    STATUS_PENDING,
                    STATUS_FAILED,
                    STATUS_PENDING,
                    STATUS_FAILED,
                ),
            )
            self._conn.commit()
            if cursor.rowcount != 1:
                if self._key_already_consumed(record):
                    raise ConfirmationError(
                        "IDEMPOTENCY_KEY_CONSUMED",
                        "该幂等键对应的业务动作已执行或进行中，本次不会执行；"
                        "请查询原操作号获取结果。",
                    )
                current = self.get(operation_id)
                status = current["status"] if current else STATUS_UNKNOWN
                raise ConfirmationError(
                    "CONFIRMATION_NOT_EXECUTABLE",
                    f"该操作当前状态为 {status}，不会再次执行；请查询原操作号获取结果。",
                )
        record["status"] = STATUS_EXECUTING
        return record

    def _key_already_consumed(self, record) -> bool:
        """True when a sibling row under the same idempotency key has left the
        claimable set - the business action is already in flight or finished.

        The claim UPDATE enforces this atomically; this only classifies the
        refusal for the caller's message.
        """
        key = record.get("idempotency_key")
        if not key:
            return False
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM operations WHERE idempotency_key=? AND mailbox=? "
                "AND operation_id<>? AND status NOT IN (?,?) LIMIT 1",
                (
                    key,
                    record["mailbox"],
                    record["operation_id"],
                    STATUS_PENDING,
                    STATUS_FAILED,
                ),
            ).fetchone()
        return row is not None

    def mark(
        self, operation_id, status, result=None, *, tool=None, mailbox=None, action=None
    ):
        now = self._now()
        with self._lock:
            self._conn.execute(
                "UPDATE operations SET status=?, result_json=?, updated_at=? WHERE operation_id=?",
                (
                    status,
                    json.dumps(result, ensure_ascii=False) if result is not None else None,
                    now,
                    operation_id,
                ),
            )
            self._conn.commit()

    def close(self) -> None:
        """Release the SQLite connection explicitly (WAL files are checkpointed)."""
        with self._lock:
            self._conn.close()

    def migrate_active_to_unknown(self) -> int:
        """Starce-terminated pending/confirmed/executing ops become unknown."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE operations SET status=?, updated_at=? WHERE status IN (?,?,?)",
                (STATUS_UNKNOWN, self._now(), STATUS_PENDING, STATUS_CONFIRMED, STATUS_EXECUTING),
            )
            self._conn.commit()
            return cursor.rowcount


def _normalize(value):
    """Deterministic, order-insensitive normalization for snapshot comparison."""
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


_store = None


def configure_store(db_path):
    """Install the process-wide operation store (one DB per data dir)."""
    global _store
    _store = OperationStore(db_path)
    return _store


def get_store():
    if _store is None:
        raise RuntimeError("confirmation store not configured (EWS_MCP_DATA_DIR required)")
    return _store


def reset_store():
    global _store
    _store = None
