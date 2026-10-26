"""Persistent preview/confirm operations and idempotent receipts.

Execution requires the preview token and identical business parameters.
Operation ID alone is read-only. Restarts mark active operations unknown;
uncertain outcomes are never retried automatically.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
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
    """Confirmation failure with a caller-safe message."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.public_message = message
        super().__init__(message)


@dataclass(frozen=True)
class DraftExecution:
    operation_id: str
    mailbox: str
    canonical_draft_id: str
    worker_id: str
    boot_id: str


class OperationStore:
    """SQLite confirmation store; one database per data directory."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self.boot_id = uuid.uuid4().hex
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
                CREATE TABLE IF NOT EXISTS attachment_uploads (
                    upload_id TEXT PRIMARY KEY, lanid TEXT NOT NULL, mailbox TEXT NOT NULL,
                    filename TEXT NOT NULL, content_type TEXT NOT NULL, size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL, token_hash TEXT NOT NULL, token_expires REAL NOT NULL,
                    retained_until REAL NOT NULL, state TEXT NOT NULL, worker_id TEXT,
                    boot_id TEXT, consumed_by_operation_id TEXT
                );
                CREATE TABLE IF NOT EXISTS operation_upload_refs (
                    operation_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL UNIQUE,
                    kind TEXT NOT NULL, expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS draft_claims (
                    mailbox TEXT NOT NULL, canonical_draft_id TEXT NOT NULL,
                    operation_id TEXT NOT NULL, worker_id TEXT NOT NULL, boot_id TEXT NOT NULL,
                    PRIMARY KEY(mailbox, canonical_draft_id)
                );
                CREATE TABLE IF NOT EXISTS upload_readers (
                    reader_id TEXT PRIMARY KEY, upload_id TEXT NOT NULL, boot_id TEXT NOT NULL
                );
                """
            )
            self._conn.commit()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                yield self._conn
                self._conn.commit()
            except BaseException:
                self._conn.rollback()
                raise

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
        with self.transaction() as db:
            return self._create_preview_tx(db, tool=tool, mailbox=mailbox, action=action,
                business_params=business_params, items=items, details=details, idempotency_key=idempotency_key)

    def _create_preview_tx(self, db, *, tool, mailbox, action, business_params,
                           items=None, details=None, idempotency_key=None):
        operation_id = uuid.uuid4().hex
        confirm_token = secrets.token_urlsafe(32)
        now = self._now()
        db.execute(
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
        """Return the latest row for a mailbox-scoped idempotency key.

        Only expired pending previews may be superseded; unscoped lookup is for inspection.
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
        """Atomically claim pending or pre-submit failed operations.

        A conditional UPDATE allows one execution per operation and per idempotency
        key within a mailbox. Executing, unknown, completed, and partial rows cannot rerun.
        """
        with self.transaction() as db:
            return self._begin_confirm_tx(db, operation_id, confirm_token, business_params)

    def _begin_confirm_tx(self, db, operation_id, confirm_token, business_params):
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
        cursor = db.execute(
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
        if cursor.rowcount != 1:
            if self._key_already_consumed(record):
                raise ConfirmationError(
                    "IDEMPOTENCY_KEY_CONSUMED",
                    "该幂等键对应的业务动作已执行或进行中，本次不会执行；请查询原操作号获取结果。",
                )
            current = self.get(operation_id)
            status = current["status"] if current else STATUS_UNKNOWN
            raise ConfirmationError("CONFIRMATION_NOT_EXECUTABLE",
                f"该操作当前状态为 {status}，不会再次执行；请查询原操作号获取结果。")
        record["status"] = STATUS_EXECUTING
        return record

    def create_attachment_preview(self, *, mailbox, business_params, items, details,
                                  upload_snapshot, canonical_draft_id, expires_at, idempotency_key=None):
        from tool_support import ToolOperationError
        with self.transaction() as db:
            row = db.execute("SELECT * FROM attachment_uploads WHERE upload_id=?",
                             (business_params['upload_id'],)).fetchone()
            if (not row or row['state'] != 'ready' or row['retained_until'] <= time.time()
                    or row['consumed_by_operation_id'] or row['mailbox'] != mailbox
                    or row['lanid'] != details.get('requester_lanid')
                    or any(row[k] != upload_snapshot.get(k) for k in ('upload_id', 'filename', 'size', 'sha256'))
                    or row['content_type'] != upload_snapshot.get('stored_content_type', upload_snapshot.get('content_type'))):
                raise ToolOperationError('UPLOAD_UNAVAILABLE', '上传文件已变化、失效或已被占用。')
            ref = db.execute("SELECT * FROM operation_upload_refs WHERE upload_id=?", (row['upload_id'],)).fetchone()
            if ref and (ref['kind'] != 'preview' or ref['expires_at'] > time.time()):
                raise ToolOperationError('UPLOAD_IN_USE', '该上传已有有效预览或执行，请使用原操作号。')
            db.execute('DELETE FROM operation_upload_refs WHERE upload_id=?', (row['upload_id'],))
            details = {**details, 'canonical_draft_id': canonical_draft_id, 'expires_at': expires_at}
            operation = self._create_preview_tx(db, tool='attach_draft_attachment', mailbox=mailbox,
                action='attach_draft_attachment', business_params=business_params, items=items,
                details=details, idempotency_key=idempotency_key)
            db.execute('INSERT INTO operation_upload_refs VALUES (?,?,?,?)',
                       (operation['operation_id'], row['upload_id'], 'preview', expires_at))
            return operation

    def require_attachment_deadline(self, operation_id, *, now=None):
        record = self.get(operation_id)
        deadline = (record or {}).get('details', {}) or {}
        deadline = deadline.get('expires_at')
        if type(deadline) not in (float, int) or deadline <= (time.time() if now is None else now):
            raise ConfirmationError('PREVIEW_EXPIRED', '附件预览已过期，请重新准备上传并预览。')

    def begin_draft_confirm(self, operation_id, confirm_token, business_params, *, mailbox, lanid, worker_id):
        from tool_support import ToolOperationError
        with self.transaction() as db:
            record = self.get(operation_id)
            if not record or record['mailbox'] != mailbox:
                raise ConfirmationError('OPERATION_SCOPE_MISMATCH', '该操作不属于当前邮箱。')
            details = record.get('details') or {}
            draft_id = details.get('canonical_draft_id')
            if not draft_id:
                raise ToolOperationError('PREVIEW_VERSION_REQUIRED', '请重新预览草稿后再确认。')
            if record['tool'] == 'attach_draft_attachment':
                self.require_attachment_deadline(operation_id)
                upload = db.execute('SELECT * FROM attachment_uploads WHERE upload_id=?', (business_params['upload_id'],)).fetchone()
                ref = db.execute('SELECT * FROM operation_upload_refs WHERE operation_id=?', (operation_id,)).fetchone()
                if (not upload or upload['state'] != 'ready' or upload['mailbox'] != mailbox or upload['lanid'] != lanid
                        or upload['consumed_by_operation_id'] not in (None, operation_id)
                        or not ref or ref['kind'] != 'preview' or ref['expires_at'] <= time.time()):
                    raise ToolOperationError('UPLOAD_UNAVAILABLE', '上传已失效、归属不符或已占用。')
            if db.execute('SELECT 1 FROM draft_claims WHERE mailbox=? AND canonical_draft_id=?', (mailbox, draft_id)).fetchone():
                raise ToolOperationError('DRAFT_BUSY', '草稿正在执行或存在结果未知操作，请查询原操作号。')
            self._begin_confirm_tx(db, operation_id, confirm_token, business_params)
            db.execute('INSERT INTO draft_claims VALUES (?,?,?,?,?)', (mailbox, draft_id, operation_id, worker_id, self.boot_id))
            if record['tool'] == 'attach_draft_attachment':
                db.execute('UPDATE attachment_uploads SET consumed_by_operation_id=? WHERE upload_id=?', (operation_id, business_params['upload_id']))
                db.execute("UPDATE operation_upload_refs SET kind='execution' WHERE operation_id=?", (operation_id,))
            return DraftExecution(operation_id, mailbox, draft_id, worker_id, self.boot_id)

    def finish_draft_operation(self, execution, status, result, *, definitely_not_applied=False):
        if status not in (STATUS_COMPLETED, STATUS_FAILED, STATUS_UNKNOWN) or (status == STATUS_FAILED and not definitely_not_applied):
            raise ValueError('unsafe draft outcome')
        with self.transaction() as db:
            claim = db.execute('SELECT * FROM draft_claims WHERE mailbox=? AND canonical_draft_id=?',
                               (execution.mailbox, execution.canonical_draft_id)).fetchone()
            record = self.get(execution.operation_id)
            if (not claim or not record or record['status'] != STATUS_EXECUTING
                    or claim['operation_id'] != execution.operation_id or claim['worker_id'] != execution.worker_id
                    or claim['boot_id'] != execution.boot_id or execution.boot_id != self.boot_id):
                raise ConfirmationError('OPERATION_UNKNOWN', '执行占用已变化，请核查原回执。')
            db.execute('UPDATE operations SET status=?,result_json=?,updated_at=? WHERE operation_id=?',
                       (status, json.dumps(result, ensure_ascii=False), self._now(), execution.operation_id))
            ref = db.execute('SELECT * FROM operation_upload_refs WHERE operation_id=?', (execution.operation_id,)).fetchone()
            if ref:
                if status == STATUS_COMPLETED:
                    db.execute("UPDATE attachment_uploads SET state='attached',retained_until=? WHERE upload_id=?", (time.time(), ref['upload_id']))
                    db.execute('DELETE FROM operation_upload_refs WHERE operation_id=?', (execution.operation_id,))
                else:
                    kind = 'quarantine' if status == STATUS_UNKNOWN else 'preview'
                    db.execute('UPDATE operation_upload_refs SET kind=? WHERE operation_id=?', (kind, execution.operation_id))
                    if status == STATUS_UNKNOWN:
                        db.execute("UPDATE attachment_uploads SET state='quarantine' WHERE upload_id=?", (ref['upload_id'],))
            if status != STATUS_UNKNOWN:
                db.execute('DELETE FROM draft_claims WHERE operation_id=?', (execution.operation_id,))

    def _key_already_consumed(self, record) -> bool:
        """Classify a refused claim; begin_confirm enforces mailbox/key exclusion atomically."""
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
        with self._lock:
            self._conn.close()

    def migrate_active_to_unknown(self) -> int:
        """Mark active operations unknown after a restart."""
        with self._lock:
            cursor = self._conn.execute(
                "UPDATE operations SET status=?, updated_at=? WHERE status IN (?,?,?)",
                (STATUS_UNKNOWN, self._now(), STATUS_PENDING, STATUS_CONFIRMED, STATUS_EXECUTING),
            )
            changed = cursor.rowcount
            self._conn.execute("UPDATE operation_upload_refs SET kind='quarantine' WHERE kind='execution'")
            self._conn.execute("UPDATE attachment_uploads SET state='quarantine' WHERE upload_id IN "
                               "(SELECT upload_id FROM operation_upload_refs WHERE kind='quarantine')")
            self._conn.execute("DELETE FROM operation_upload_refs WHERE kind='preview' AND operation_id IN "
                               "(SELECT operation_id FROM operations WHERE status='unknown')")
            self._conn.execute("UPDATE attachment_uploads SET state='failed',retained_until=0 WHERE state='receiving'")
            self._conn.execute('DELETE FROM upload_readers')
            self._conn.commit()
            return changed


def _normalize(value):
    """Normalize dictionary keys and sequences for snapshot comparison."""
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(v) for v in value]
    return value


_store = None


def configure_store(db_path):
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
