from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from equipment_quality.errors import Conflict, InvalidState
from equipment_quality.service import MetricQualityService


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService(":memory:")
        self.service.bootstrap_admin()
        self.token = self.service.auth.login("admin", "metric-admin")
        self.service.auth.create_user("op1", "operator-password", "operator")
        self.operator_token = self.service.auth.login("op1", "operator-password")
        self.service.create_lot(self.token, "VLV-001", "subsea-valve", "WP-4.0", 10)

    def _count(self, table: str, lot_id: str) -> int:
        return self.service.db.execute(
            f"SELECT count(*) FROM {table} WHERE lot_id=?", (lot_id,)
        ).fetchone()[0]

    def test_successful_approval_updates_revision_and_records_facts(self) -> None:
        result = self.service.approve(self.token, "VLV-001", "release", "all checks passed", 1)
        self.assertEqual(result["status"], "released")
        self.assertEqual(result["revision"], 2)
        self.assertFalse(result["replayed"])
        self.assertEqual(self._count("approvals", "VLV-001"), 1)
        events = self.service.audit(self.token, "VLV-001")
        self.assertEqual([e["event_type"] for e in events], ["created", "approval"])
        approval = self.service.db.execute(
            "SELECT revision,decision,reason,reviewer FROM approvals WHERE lot_id='VLV-001'"
        ).fetchone()
        self.assertEqual((approval["revision"], approval["decision"]), (1, "release"))

    def test_missing_lot_rolls_back_everything(self) -> None:
        with self.assertRaises(KeyError):
            self.service.approve(self.token, "VLV-GHOST", "release", "wrong lot number", 1)
        self.assertEqual(self._count("approvals", "VLV-GHOST"), 0)
        self.assertEqual(self._count("lot_events", "VLV-GHOST"), 0)
        # 日后创建同名批次，时间线必须从真实创建事件开始。
        self.service.create_lot(self.token, "VLV-GHOST", "subsea-valve", "WP-4.0", 4)
        events = self.service.audit(self.token, "VLV-GHOST")
        self.assertEqual([e["event_type"] for e in events], ["created"])
        lot = self.service.get_lot(self.token, "VLV-GHOST")
        self.assertEqual(lot["status"], "engineering")
        self.assertEqual(lot["revision"], 1)

    def test_unprivileged_call_leaves_no_trace(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator_token, "VLV-001", "release", "no authority", 1)
        self.assertEqual(self._count("approvals", "VLV-001"), 0)
        self.assertEqual(
            [e["event_type"] for e in self.service.audit(self.token, "VLV-001")],
            ["created"],
        )

    def test_stale_revision_is_rejected_without_writes(self) -> None:
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "VLV-001", "release", "behind version", 7)
        lot = self.service.get_lot(self.token, "VLV-001")
        self.assertEqual((lot["status"], lot["revision"]), ("engineering", 1))
        self.assertEqual(self._count("approvals", "VLV-001"), 0)
        self.assertEqual(
            [e["event_type"] for e in self.service.audit(self.token, "VLV-001")],
            ["created"],
        )

    def test_identical_replay_returns_original_result(self) -> None:
        first = self.service.approve(self.token, "VLV-001", "release", "all checks passed", 1)
        second = self.service.approve(self.token, "VLV-001", "release", "all checks passed", 1)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["status"], first["status"])
        self.assertEqual(second["revision"], first["revision"])
        self.assertEqual(self._count("approvals", "VLV-001"), 1)
        self.assertEqual(
            [e["event_type"] for e in self.service.audit(self.token, "VLV-001")],
            ["created", "approval"],
        )

    def test_replay_with_changed_decision_conflicts_and_names_revision(self) -> None:
        self.service.approve(self.token, "VLV-001", "release", "all checks passed", 1)
        with self.assertRaises(Conflict) as caught:
            self.service.approve(self.token, "VLV-001", "hold", "changed my mind", 1)
        self.assertIn("revision 1", str(caught.exception))
        self.assertEqual(self._count("approvals", "VLV-001"), 1)
        self.assertEqual(self.service.get_lot(self.token, "VLV-001")["status"], "released")

    def test_replay_with_changed_reason_conflicts(self) -> None:
        self.service.approve(self.token, "VLV-001", "reject", "crack detected", 1)
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "VLV-001", "reject", "different reason", 1)
        self.assertEqual(self._count("approvals", "VLV-001"), 1)
        approval = self.service.db.execute(
            "SELECT reason FROM approvals WHERE lot_id='VLV-001'"
        ).fetchone()
        self.assertEqual(approval["reason"], "crack detected")

    def test_terminal_lot_rejects_any_new_approval(self) -> None:
        self.service.approve(self.token, "VLV-001", "reject", "crack detected", 1)
        # 连"版本正确但决定已终结"以外的状态机路径也不能重新打开批次。
        with self.assertRaises(Conflict):
            self.service.approve(self.token, "VLV-001", "release", "crack detected", 1)
        self.assertEqual(self.service.get_lot(self.token, "VLV-001")["status"], "rejected")


LEGACY_SCHEMA = """
CREATE TABLE metric_batches(
 lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
 sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
 created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE measurements(
 measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL,
 test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
 instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL);
CREATE TABLE lot_events(
 event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
 event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE approvals(
 lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
 reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
"""


class MigrationTests(unittest.TestCase):
    def test_legacy_ghost_facts_are_purged_on_open(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "legacy.sqlite3")
            seed = sqlite3.connect(path)
            seed.executescript(LEGACY_SCHEMA)
            seed.execute(
                "INSERT INTO metric_batches VALUES('VLV-REAL','valve','WP-1',5,'engineering',"
                "'admin','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')"
            )
            # 幽灵审批与事件：指向从未成功创建的批次。
            seed.execute(
                "INSERT INTO approvals VALUES('VLV-GHOST','admin','release','typo','2026-01-02T00:00:00+00:00')"
            )
            seed.execute(
                "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at) "
                "VALUES('VLV-GHOST','approval','admin','{}','2026-01-02T00:00:00+00:00')"
            )
            seed.execute(
                "INSERT INTO approvals VALUES('VLV-REAL','admin','hold','watch','2026-01-03T00:00:00+00:00')"
            )
            seed.commit()
            seed.close()

            service = MetricQualityService(path)
            self.assertEqual(
                service.db.execute("SELECT count(*) FROM lot_events WHERE lot_id='VLV-GHOST'").fetchone()[0],
                0,
            )
            self.assertEqual(
                service.db.execute("SELECT count(*) FROM approvals WHERE lot_id='VLV-GHOST'").fetchone()[0],
                0,
            )
            # 真实数据保留并补齐修订号。
            row = service.db.execute("SELECT revision,status FROM metric_batches WHERE lot_id='VLV-REAL'").fetchone()
            self.assertEqual((row["revision"], row["status"]), (1, "engineering"))
            approval = service.db.execute(
                "SELECT revision,decision FROM approvals WHERE lot_id='VLV-REAL'"
            ).fetchone()
            self.assertEqual((approval["revision"], approval["decision"]), (1, "hold"))


if __name__ == "__main__":
    unittest.main()
