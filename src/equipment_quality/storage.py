"""装备样本批次和测量记录的 SQLite 结构及事务辅助函数。"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS metric_batches(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 1,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES metric_batches(lot_id),
 test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, revision INTEGER NOT NULL, reviewer TEXT NOT NULL,
 decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(lot_id,revision,reviewer),
 FOREIGN KEY(lot_id) REFERENCES metric_batches(lot_id));
"""

_APPROVALS_DDL = """
CREATE TABLE approvals(
 lot_id TEXT NOT NULL, revision INTEGER NOT NULL, reviewer TEXT NOT NULL,
 decision TEXT NOT NULL, reason TEXT NOT NULL, created_at TEXT NOT NULL,
 PRIMARY KEY(lot_id,revision,reviewer),
 FOREIGN KEY(lot_id) REFERENCES metric_batches(lot_id))"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _migrate(db: sqlite3.Connection) -> None:
    """把早于修订号/外键约束的旧库升级到当前结构。

    旧版本允许审批指向不存在的批次，这些孤儿审批与事件属于失败尝试留下的
    业务事实，迁移时必须丢弃，使同名批次日后可以从真实创建事件开始。
    """
    exists = db.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metric_batches'"
    ).fetchone()
    if exists is None:
        return
    if "revision" not in _columns(db, "metric_batches"):
        db.execute("ALTER TABLE metric_batches ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "revision" not in _columns(db, "approvals"):
        db.execute("ALTER TABLE approvals RENAME TO approvals_legacy")
        db.execute(_APPROVALS_DDL)
        db.execute(
            "INSERT INTO approvals(lot_id,revision,reviewer,decision,reason,created_at) "
            "SELECT a.lot_id,1,a.reviewer,a.decision,a.reason,a.created_at FROM approvals_legacy a "
            "WHERE EXISTS (SELECT 1 FROM metric_batches b WHERE b.lot_id=a.lot_id)"
        )
        db.execute("DROP TABLE approvals_legacy")
    db.execute(
        "DELETE FROM lot_events WHERE lot_id NOT IN (SELECT lot_id FROM metric_batches)"
    )
    db.execute(
        "DELETE FROM measurements WHERE lot_id NOT IN (SELECT lot_id FROM metric_batches)"
    )
    db.execute(
        "DELETE FROM approvals WHERE lot_id NOT IN (SELECT lot_id FROM metric_batches)"
    )


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    _migrate(db)
    db.commit()
    return db


@contextmanager
def transaction(db: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        db.execute("BEGIN IMMEDIATE")
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise


def event(db: sqlite3.Connection, lot_id: str, event_type: str, actor: str, payload: dict) -> None:
    db.execute("INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) VALUES(?,?,?,?,?)", (lot_id, event_type, actor, json.dumps(payload, sort_keys=True), utcnow()))
