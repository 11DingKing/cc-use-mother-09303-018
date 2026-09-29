"""SQLite 存储层。

数据库级约束与服务层事务共同保证：
- 正式版本号全局唯一（部分唯一索引）；
- 每份已封账报告至多有一个继任版本（parent_report_id 部分唯一索引）；
- 同一方对一份报告只能会签一次；
- 一份报告至多有一条生效中的重开批准（部分唯一索引）。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

SCHEMA = """
CREATE TABLE IF NOT EXISTS data_versions (
    digest       TEXT PRIMARY KEY,
    content      TEXT NOT NULL,
    byte_length  INTEGER NOT NULL,
    source_label TEXT,
    received_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    digest      TEXT PRIMARY KEY,
    content     TEXT NOT NULL,
    byte_length INTEGER NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS reports (
    id               TEXT PRIMARY KEY,
    version_no       INTEGER,
    title            TEXT NOT NULL,
    status           TEXT NOT NULL,
    data_digest      TEXT NOT NULL,
    rule_digest      TEXT NOT NULL,
    input_summary    TEXT NOT NULL,
    body             BLOB NOT NULL,
    body_digest      TEXT NOT NULL,
    parent_report_id TEXT,
    superseded_by    TEXT,
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL,
    sealed_at        TEXT,
    manifest         TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_version
    ON reports(version_no) WHERE version_no IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_reports_parent
    ON reports(parent_report_id) WHERE parent_report_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS signatures (
    report_id TEXT NOT NULL REFERENCES reports(id),
    party     TEXT NOT NULL,
    signer    TEXT NOT NULL,
    signed_at TEXT NOT NULL,
    PRIMARY KEY (report_id, party)
);

CREATE TABLE IF NOT EXISTS reopen_approvals (
    id                 TEXT PRIMARY KEY,
    report_id          TEXT NOT NULL REFERENCES reports(id),
    requester          TEXT NOT NULL,
    approver           TEXT NOT NULL,
    reason             TEXT NOT NULL,
    status             TEXT NOT NULL,
    sealed_body_digest TEXT NOT NULL,
    requested_at       TEXT NOT NULL,
    approved_at        TEXT,
    new_report_id      TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_approval
    ON reopen_approvals(report_id) WHERE status IN ('pending', 'approved');

CREATE TABLE IF NOT EXISTS downloads (
    report_id     TEXT NOT NULL REFERENCES reports(id),
    client_key    TEXT NOT NULL,
    chunk_index   INTEGER NOT NULL,
    byte_count    INTEGER NOT NULL,
    downloaded_at TEXT NOT NULL,
    PRIMARY KEY (report_id, client_key, chunk_index)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key        TEXT PRIMARY KEY,
    report_id  TEXT NOT NULL REFERENCES reports(id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    ts        TEXT NOT NULL,
    report_id TEXT,
    event     TEXT NOT NULL,
    actor     TEXT,
    detail    TEXT
);
"""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """串行化访问的 SQLite 存储（调用方持锁或单线程使用）。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        # isolation_level=None：关闭 sqlite3 的隐式事务，所有事务由本类
        # 显式 BEGIN IMMEDIATE / COMMIT / ROLLBACK 控制，避免单条语句失败后
        # 残留隐式事务（会让后续显式事务报"事务中不能开启事务"）。
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._tx_active = False

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    # ---- 事务 ----------------------------------------------------------

    def begin_immediate(self) -> None:
        """开启写事务；事务期间内部各方法的 commit() 被挂起。"""
        self._conn.execute("BEGIN IMMEDIATE")
        self._tx_active = True

    def commit_transaction(self) -> None:
        self._conn.commit()
        self._tx_active = False

    def rollback_transaction(self) -> None:
        self._conn.rollback()
        self._tx_active = False

    def commit(self) -> None:
        """自动提交；显式事务进行中时为空操作。"""
        if not self._tx_active:
            self._conn.commit()

    def query_one(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> sqlite3.Row | None:
        return self._conn.execute(sql, params).fetchone()

    def query_all(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> list[sqlite3.Row]:
        return list(self._conn.execute(sql, params).fetchall())

    def execute(self, sql: str, params: Iterable[Any] | Mapping[str, Any] = ()) -> sqlite3.Cursor:
        return self._conn.execute(sql, params)

    # ---- 数据版本 / 规则版本 -------------------------------------------

    def register_data(self, digest: str, content_text: str, byte_length: int, source_label: str | None) -> bool:
        with self._lock:
            cur = self.execute(
                "INSERT OR IGNORE INTO data_versions(digest, content, byte_length, source_label, received_at)"
                " VALUES (?,?,?,?,?)",
                (digest, content_text, byte_length, source_label, utc_now()),
            )
            self.commit()
            return cur.rowcount > 0

    def get_data(self, digest: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM data_versions WHERE digest=?", (digest,))

    def register_ruleset(self, digest: str, content_text: str, byte_length: int, note: str | None) -> bool:
        with self._lock:
            cur = self.execute(
                "INSERT OR IGNORE INTO rule_versions(digest, content, byte_length, note, created_at)"
                " VALUES (?,?,?,?,?)",
                (digest, content_text, byte_length, note, utc_now()),
            )
            self.commit()
            return cur.rowcount > 0

    def get_ruleset(self, digest: str) -> sqlite3.Row | None:
        return self.query_one("SELECT rule_versions.* FROM rule_versions WHERE digest=?", (digest,))

    # ---- 报告 ----------------------------------------------------------

    def insert_report(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.execute(
                "INSERT INTO reports(id, version_no, title, status, data_digest, rule_digest,"
                " input_summary, body, body_digest, parent_report_id, created_at, updated_at)"
                " VALUES (:id,:version_no,:title,:status,:data_digest,:rule_digest,"
                " :input_summary,:body,:body_digest,:parent_report_id,:created_at,:updated_at)",
                row,
            )
            self.commit()

    def get_report(self, report_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM reports WHERE id=?", (report_id,))

    def list_reports(self) -> list[sqlite3.Row]:
        return self.query_all("SELECT * FROM reports ORDER BY created_at, id")

    def formal_version_exists(self) -> bool:
        """是否已存在任何正式版本（封账时分配的版本号）。"""
        row = self.query_one("SELECT 1 FROM reports WHERE version_no IS NOT NULL LIMIT 1")
        return row is not None

    def add_signature(self, report_id: str, party: str, signer: str) -> bool:
        with self._lock:
            cur = self.execute(
                "INSERT OR IGNORE INTO signatures(report_id, party, signer, signed_at)"
                " VALUES (?,?,?,?)",
                (report_id, party, signer, utc_now()),
            )
            self.commit()
            return cur.rowcount > 0

    def get_signatures(self, report_id: str) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM signatures WHERE report_id=? ORDER BY party", (report_id,)
        )

    def set_status(self, report_id: str, status: str) -> None:
        self.execute(
            "UPDATE reports SET status=?, updated_at=? WHERE id=?",
            (status, utc_now(), report_id),
        )

    def seal_report(self, report_id: str, version_no: int, manifest: dict[str, Any]) -> None:
        self.execute(
            "UPDATE reports SET status='已封账', version_no=?, sealed_at=?, updated_at=?,"
            " manifest=? WHERE id=?",
            (version_no, utc_now(), utc_now(), json.dumps(manifest, ensure_ascii=False), report_id),
        )

    def mark_superseded(self, report_id: str, successor_id: str) -> None:
        self.execute(
            "UPDATE reports SET status='已更正', superseded_by=?, updated_at=? WHERE id=? AND status='已封账'",
            (successor_id, utc_now(), report_id),
        )

    # ---- 重开批准 ------------------------------------------------------

    def insert_approval(self, row: dict[str, Any]) -> None:
        with self._lock:
            self.execute(
                "INSERT INTO reopen_approvals(id, report_id, requester, approver, reason, status,"
                " sealed_body_digest, requested_at) VALUES (:id,:report_id,:requester,:approver,"
                ":reason,:status,:sealed_body_digest,:requested_at)",
                row,
            )
            self.commit()

    def get_approval(self, approval_id: str) -> sqlite3.Row | None:
        return self.query_one("SELECT * FROM reopen_approvals WHERE id=?", (approval_id,))

    def approve(self, approval_id: str) -> None:
        self.execute(
            "UPDATE reopen_approvals SET status='approved', approved_at=? WHERE id=? AND status='pending'",
            (utc_now(), approval_id),
        )

    def consume_approval(self, approval_id: str, new_report_id: str) -> None:
        self.execute(
            "UPDATE reopen_approvals SET status='used', new_report_id=? WHERE id=? AND status='approved'",
            (new_report_id, approval_id),
        )

    # ---- 下载台账 / 事件 ----------------------------------------------

    def record_download(self, report_id: str, client_key: str, chunk_index: int, byte_count: int) -> bool:
        """登记一次分块下载；同一 client_key 重复下载同一块幂等忽略。

        返回 True 表示首次下载。任何情况下报告正文都不被触碰。
        """
        with self._lock:
            cur = self.execute(
                "INSERT OR IGNORE INTO downloads(report_id, client_key, chunk_index, byte_count, downloaded_at)"
                " VALUES (?,?,?,?,?)",
                (report_id, client_key, chunk_index, byte_count, utc_now()),
            )
            self.commit()
            return cur.rowcount > 0

    def list_downloads(self, report_id: str) -> list[sqlite3.Row]:
        return self.query_all(
            "SELECT * FROM downloads WHERE report_id=? ORDER BY client_key, chunk_index",
            (report_id,),
        )

    def append_event(self, event: str, report_id: str | None, actor: str | None, detail: dict[str, Any]) -> None:
        with self._lock:
            self.execute(
                "INSERT INTO events(ts, report_id, event, actor, detail) VALUES (?,?,?,?,?)",
                (utc_now(), report_id, event, actor, json.dumps(detail, ensure_ascii=False)),
            )
            self.commit()

    def list_events(self, report_id: str | None = None) -> list[sqlite3.Row]:
        if report_id is None:
            return self.query_all("SELECT * FROM events ORDER BY id")
        return self.query_all("SELECT * FROM events WHERE report_id=? ORDER BY id", (report_id,))

    def close(self) -> None:
        with self._lock:
            self._conn.close()
