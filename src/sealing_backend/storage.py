"""SQLite 持久层：表结构、连接与写事务。

并发约定：所有写操作都通过 ``write()`` 以 ``BEGIN IMMEDIATE`` 开启
事务，SQLite 会在同一时刻只放行一个写事务，因此"同时封账"等并发
写会被串行化；配合表上的唯一约束，保证不会产生两个正式版本。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
  report_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  period TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  current_official_version_id TEXT
);

CREATE TABLE IF NOT EXISTS versions (
  version_id TEXT PRIMARY KEY,
  report_id TEXT NOT NULL REFERENCES reports(report_id),
  version_no INTEGER NOT NULL,
  state TEXT NOT NULL CHECK (state IN ('试算', '会签', '封账', '导出')),
  data_version TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  input_digest TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sealed_at TEXT,
  sealed_seq INTEGER,
  UNIQUE (report_id, version_no),
  UNIQUE (report_id, sealed_seq)
);

CREATE TABLE IF NOT EXISTS signatures (
  version_id TEXT NOT NULL REFERENCES versions(version_id),
  party TEXT NOT NULL CHECK (party IN ('中方', '外方')),
  signer TEXT NOT NULL,
  signed_digest TEXT NOT NULL,
  signed_at TEXT NOT NULL,
  PRIMARY KEY (version_id, party)
);

CREATE TABLE IF NOT EXISTS corrections (
  correction_id TEXT PRIMARY KEY,
  version_id TEXT NOT NULL REFERENCES versions(version_id),
  state TEXT NOT NULL CHECK (state IN ('试算', '会签', '更正')),
  reason TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  content_digest TEXT NOT NULL,
  requested_by TEXT NOT NULL,
  approver TEXT NOT NULL,
  created_at TEXT NOT NULL,
  sealed_at TEXT
);

-- 同一版本同一时刻只允许一张未生效的更正单。
CREATE UNIQUE INDEX IF NOT EXISTS one_open_correction
  ON corrections(version_id) WHERE state != '更正';

CREATE TABLE IF NOT EXISTS correction_signatures (
  correction_id TEXT NOT NULL REFERENCES corrections(correction_id),
  party TEXT NOT NULL CHECK (party IN ('中方', '外方')),
  signer TEXT NOT NULL,
  signed_digest TEXT NOT NULL,
  signed_at TEXT NOT NULL,
  PRIMARY KEY (correction_id, party)
);

CREATE TABLE IF NOT EXISTS exports (
  target_type TEXT NOT NULL CHECK (target_type IN ('version', 'correction')),
  target_id TEXT NOT NULL,
  chunk_size INTEGER NOT NULL,
  chunk_count INTEGER NOT NULL,
  document BLOB NOT NULL,
  manifest_json TEXT NOT NULL,
  manifest_digest TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (target_type, target_id)
);

CREATE TABLE IF NOT EXISTS idempotency_keys (
  key TEXT NOT NULL,
  scope TEXT NOT NULL,
  request_json TEXT NOT NULL,
  response_json TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (key, scope)
);

CREATE TABLE IF NOT EXISTS audit_log (
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  target_type TEXT NOT NULL,
  target_id TEXT NOT NULL,
  detail_json TEXT NOT NULL
);
"""


class Storage:
    """按请求开启连接的 SQLite 存储，天然支持多线程。"""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        con = self.connect()
        try:
            con.execute("PRAGMA journal_mode = WAL")
            con.executescript(SCHEMA)
        finally:
            con.close()

    def connect(self) -> sqlite3.Connection:
        # timeout 即 busy_timeout：写事务被占用时等待而非立即报错。
        con = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA busy_timeout = 30000")
        return con

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE，串行化所有并发写。"""
        con = self.connect()
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.execute("COMMIT")
        except BaseException:
            try:
                con.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            con.close()

    @contextmanager
    def read(self) -> Iterator[sqlite3.Connection]:
        con = self.connect()
        try:
            yield con
        finally:
            con.close()
