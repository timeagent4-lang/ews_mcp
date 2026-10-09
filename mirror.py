"""Opt-in local mirror powering the ``waiting_on`` follow-up heuristic.

This is *not* proof that nobody replied: it only reports that within a bounded,
freshly synced mirror window no later message in the conversation was seen.
Missing/truncated/revoked coverage is reported honestly, never as success.
"""

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

from exchangelib import Q

from tool_support import ToolOperationError

DEFAULT_WINDOW_DAYS = 365
DEFAULT_MAX_PER_FOLDER = 2000


def _now():
    return datetime.now(timezone.utc)


class MirrorStore:
    """SQLite mirror of bounded inbox+sent item/conversation references."""

    def __init__(self, db_path):
        self.db_path = db_path
        if db_path and db_path != ":memory:":
            os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init()
        self.coverage = {}

    def _init(self):
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS mirror_items (
                mailbox      TEXT NOT NULL,
                folder       TEXT NOT NULL,
                item_id      TEXT NOT NULL,
                conversation_id TEXT,
                received     TEXT,
                PRIMARY KEY (mailbox, folder, item_id)
            );
            """
        )
        self._conn.commit()

    def replace_snapshot(self, mailbox, folder, rows, window_start, max_rows):
        now = _now().isoformat()
        with self._conn:
            self._conn.execute(
                "DELETE FROM mirror_items WHERE mailbox=? AND folder=?",
                (mailbox, folder),
            )
            self._conn.executemany(
                "INSERT OR REPLACE INTO mirror_items"
                "(mailbox, folder, item_id, conversation_id, received) VALUES (?,?,?,?,?)",
                [(mailbox, folder, r["item_id"], r.get("conversation_id"), r.get("received")) for r in rows[:max_rows]],
            )
            self._conn.commit()
        self.coverage[f"{mailbox}:{folder}"] = {
            "window_start": window_start.isoformat(),
            "count": min(len(rows), max_rows),
            "truncated": len(rows) > max_rows,
            "updated_at": now,
        }

    def sent_by_item(self, mailbox, item_id):
        row = self._conn.execute(
            "SELECT * FROM mirror_items WHERE mailbox=? AND folder='sent' AND item_id=?",
            (mailbox, item_id),
        ).fetchone()
        return dict(row) if row else None

    def inbox_replies_after(self, mailbox, conversation_id, after_received):
        rows = self._conn.execute(
            "SELECT * FROM mirror_items WHERE mailbox=? AND folder='inbox' "
            "AND conversation_id=? AND received>? ORDER BY received",
            (mailbox, conversation_id, after_received),
        ).fetchall()
        return [dict(r) for r in rows]

    @property
    def complete(self):
        return bool(self.coverage)


class WaitingOnOperations:
    def _ensure_mirror(self, days, mailbox):
        if os.getenv("EWS_MCP_CACHE_ENABLED", "false").lower() not in ("1", "true", "yes"):
            raise ToolOperationError(
                "MIRROR_DISABLED", "跟进功能需要启用本地镜像（EWS_MCP_CACHE_ENABLED=true）。"
            )
        store = self._mirror
        if store is None:
            raise ToolOperationError("MIRROR_DISABLED", "本地镜像未初始化。")
        window_start = _now() - timedelta(days=min(int(days or 30), DEFAULT_WINDOW_DAYS))
        if not store.coverage:
            self._refresh_mirror(store, mailbox, window_start)
        return store, window_start

    def _refresh_mirror(self, store, mailbox, window_start):
        now = _now()
        for folder in ("inbox", "sent"):
            try:
                folder_obj = self._tool_folder(folder)
                query = folder_obj.filter(
                    Q(datetime_received__range=(window_start, now))
                ).order_by("-datetime_received")
                rows = []
                for item in query:
                    rows.append(
                        {
                            "item_id": item.id,
                            "conversation_id": getattr(item, "conversation_id", None),
                            "received": getattr(item, "datetime_received", None),
                        }
                    )
                    if len(rows) >= DEFAULT_MAX_PER_FOLDER:
                        rows.append({"truncated": True})
                        break
                store.replace_snapshot(
                    mailbox, folder, rows, window_start, DEFAULT_MAX_PER_FOLDER
                )
            except Exception as exc:  # noqa: BLE001 - revoked folder is incomplete, not success
                store.coverage[f"{mailbox}:{folder}"] = {
                    "unavailable": exc.__class__.__name__,
                }

    def waiting_on(self, id, days=30):
        mailbox = self.config.email
        store, window_start = self._ensure_mirror(days, mailbox)
        sent = store.sent_by_item(mailbox, id)
        if sent is None:
            return {
                "id": id,
                "outcome": "insufficient_data",
                "detail": "已在镜像中定位到该外发邮件（id 未命中 sent）。",
                "clearly_no_reply": False,
                "window_start": window_start.isoformat(),
            }
        replies = store.inbox_replies_after(
            mailbox, sent.get("conversation_id"), sent.get("received") or ""
        )
        return {
            "id": id,
            "outcome": "no_reply_in_window" if not replies else "has_reply_in_window",
            "reply_count_in_window": len(replies),
            "clearly_no_reply": not replies,
            "window_start": window_start.isoformat(),
            "note": "仅表示镜像窗口内未见后续来信，不等于无人回复。",
        }
