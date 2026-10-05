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
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL REFERENCES metric_batches(lot_id),
 test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
 UNIQUE(lot_id,measurement_id));
CREATE TABLE IF NOT EXISTS lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1,
 PRIMARY KEY(lot_id,reviewer));
"""

TERMINAL_STATUSES = frozenset({"released", "rejected"})
DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})").fetchall()}


def _sweep_phantom_history(db: sqlite3.Connection) -> None:
    """删除失败尝试遗留的审批与事件（历史版本曾允许幽灵记录落库）。

    审批与事件只应在目标批次真实存在且状态更新成功后写入。需要修复两类
    旧数据：

    1. 指向不存在批次的行——审批按不存在的编号误调用后批次再未创建；
    2. 批次日后以同名创建、但时间线上残留早于真实创建事件的记录——
       非 created 事件若排在该批次首个 created 事件之前，或审批落库时间
       早于批次创建时间，都属于幽灵历史，必须从查询结果中消失。
    """
    db.execute("DELETE FROM approvals WHERE lot_id NOT IN (SELECT lot_id FROM metric_batches)")
    db.execute("DELETE FROM lot_events WHERE lot_id NOT IN (SELECT lot_id FROM metric_batches)")
    db.execute(
        "DELETE FROM lot_events AS e WHERE e.event_type != 'created' AND e.event_id < ("
        "SELECT COALESCE(MIN(c.event_id), 0) FROM lot_events c"
        " WHERE c.lot_id = e.lot_id AND c.event_type = 'created')"
    )
    db.execute(
        "DELETE FROM approvals AS a WHERE a.created_at < ("
        "SELECT b.created_at FROM metric_batches b WHERE b.lot_id = a.lot_id)"
    )


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(path: str = ":memory:") -> sqlite3.Connection:
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    db.executescript(SCHEMA)
    if "revision" not in _columns(db, "metric_batches"):
        db.execute("ALTER TABLE metric_batches ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    if "revision" not in _columns(db, "approvals"):
        db.execute("ALTER TABLE approvals ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
    _sweep_phantom_history(db)
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
