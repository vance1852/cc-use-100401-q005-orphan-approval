from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from equipment_quality.auth import Auth
from equipment_quality.errors import ReplayConflict, ReviewClosed, StaleRevision
from equipment_quality.service import MetricQualityService
from equipment_quality.storage import _sweep_phantom_history, connect, event, utcnow


class ApprovalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = MetricQualityService(":memory:")
        self.service.bootstrap_admin()
        self.admin = self.service.auth.login("admin", "metric-admin")
        self.service.auth.create_user("operator-a", "operator-pw", "operator")
        self.operator = self.service.auth.login("operator-a", "operator-pw")
        self.service.auth.create_user("quality-b", "quality-pw", "quality")
        self.quality = self.service.auth.login("quality-b", "quality-pw")
        self.service.create_lot(self.admin, "VALVE-01", "subsea-valve", "POLICY-3.2", 10)

    def _event_types(self, token: str, lot_id: str) -> list[str]:
        return [row["event_type"] for row in self.service.audit(token, lot_id)]

    def _count(self, table: str, lot_id: str) -> int:
        return self.service.db.execute(
            f"SELECT count(*) FROM {table} WHERE lot_id=?", (lot_id,)
        ).fetchone()[0]

    def test_approve_missing_lot_leaves_no_trace(self) -> None:
        with self.assertRaises(KeyError):
            self.service.approve(self.admin, "VALVE-GHOST", "release", "编号输错", 1)
        self.assertEqual(self._count("approvals", "VALVE-GHOST"), 0)
        self.assertEqual(self._count("lot_events", "VALVE-GHOST"), 0)
        with self.assertRaises(KeyError):
            self.service.get_lot(self.admin, "VALVE-GHOST")

    def test_approve_requires_permission_and_rolls_back(self) -> None:
        with self.assertRaises(PermissionError):
            self.service.approve(self.operator, "VALVE-01", "release", "越权放行", 1)
        self.assertEqual(self._count("approvals", "VALVE-01"), 0)
        self.assertEqual(self._count("lot_events", "VALVE-01"), 1)
        self.assertEqual(self.service.get_lot(self.admin, "VALVE-01")["status"], "engineering")

    def test_stale_revision_is_rejected_and_rolled_back(self) -> None:
        first = self.service.approve(self.admin, "VALVE-01", "hold", "等待复检", 1)
        self.assertEqual(first["revision"], 2)
        with self.assertRaises(StaleRevision):
            self.service.approve(self.quality, "VALVE-01", "release", "复检合格", 1)
        self.assertEqual(self._count("approvals", "VALVE-01"), 1)
        self.assertEqual(self.service.get_lot(self.admin, "VALVE-01")["status"], "hold")

    def test_current_revision_allows_next_decision(self) -> None:
        self.service.approve(self.admin, "VALVE-01", "hold", "等待复检", 1)
        released = self.service.approve(self.quality, "VALVE-01", "release", "复检合格", 2)
        self.assertEqual(released["status"], "released")
        self.assertEqual(released["revision"], 3)
        types = self._event_types(self.admin, "VALVE-01")
        self.assertEqual(types, ["created", "approval", "approval"])

    def test_terminal_lot_cannot_be_decided_again(self) -> None:
        self.service.approve(self.admin, "VALVE-01", "reject", "密封失效", 1)
        with self.assertRaises(ReviewClosed):
            self.service.approve(self.quality, "VALVE-01", "release", "想翻案", 2)
        self.assertEqual(self._count("approvals", "VALVE-01"), 1)
        self.assertEqual(self.service.get_lot(self.admin, "VALVE-01")["status"], "rejected")

    def test_identical_replay_returns_original_without_new_rows(self) -> None:
        first = self.service.approve(self.admin, "VALVE-01", "hold", "等待复检", 1)
        second = self.service.approve(self.admin, "VALVE-01", "hold", "等待复检", 99)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["decision"], first["decision"])
        self.assertEqual(second["revision"], first["decision_revision"])
        self.assertEqual(self._count("approvals", "VALVE-01"), 1)
        self.assertEqual(self._event_types(self.admin, "VALVE-01"), ["created", "approval"])

    def test_changed_replay_reports_conflicting_revision(self) -> None:
        self.service.approve(self.admin, "VALVE-01", "hold", "等待复检", 1)
        with self.assertRaises(ReplayConflict) as ctx:
            self.service.approve(self.admin, "VALVE-01", "release", "改变理由", 1)
        self.assertIn("revision 2", str(ctx.exception))
        self.assertEqual(self._count("approvals", "VALVE-01"), 1)
        self.assertEqual(self._event_types(self.admin, "VALVE-01"), ["created", "approval"])
        self.assertEqual(self.service.get_lot(self.admin, "VALVE-01")["status"], "hold")

    def test_timeline_of_later_same_named_lot_starts_at_creation(self) -> None:
        with self.assertRaises(KeyError):
            self.service.approve(self.admin, "VALVE-LATER", "release", "早于创建的幽灵放行", 1)
        self.service.create_lot(self.admin, "VALVE-LATER", "subsea-valve", "POLICY-4.0", 4)
        events = self.service.audit(self.admin, "VALVE-LATER")
        self.assertEqual([row["event_type"] for row in events], ["created"])
        self.assertNotIn("release", events[0]["payload"])
        self.assertEqual(self._count("approvals", "VALVE-LATER"), 0)

    def test_invalid_revision_is_rejected(self) -> None:
        for bad in (0, -1, "1", 1.0, True, None):
            with self.assertRaises((ValueError, TypeError)):
                self.service.approve(self.admin, "VALVE-01", "hold", "x", bad)
        self.assertEqual(self._count("approvals", "VALVE-01"), 0)


class PhantomSweepTests(unittest.TestCase):
    def test_legacy_phantom_rows_vanish_on_connect(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "legacy.sqlite3"
            raw = sqlite3.connect(path)
            raw.executescript("""
            CREATE TABLE metric_batches(
             lot_id TEXT PRIMARY KEY, product TEXT NOT NULL, process_rev TEXT NOT NULL,
             sample_count INTEGER NOT NULL, status TEXT NOT NULL, owner TEXT NOT NULL,
             created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE measurements(
             measurement_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL,
             test_frequency_hz REAL NOT NULL, response REAL NOT NULL, noise REAL NOT NULL,
             instrument TEXT NOT NULL, operator TEXT NOT NULL, measured_at TEXT NOT NULL,
             UNIQUE(lot_id,measurement_id));
            CREATE TABLE lot_events(
             event_id INTEGER PRIMARY KEY AUTOINCREMENT, lot_id TEXT NOT NULL,
             event_type TEXT NOT NULL, actor TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE approvals(
             lot_id TEXT NOT NULL, reviewer TEXT NOT NULL, decision TEXT NOT NULL,
             reason TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(lot_id,reviewer));
            """)
            # 失败尝试：批次从未存在。
            raw.execute(
                "INSERT INTO approvals VALUES('GHOST','qa','release','编号输错','2026-09-01T00:00:00+00:00')"
            )
            raw.execute(
                "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at)"
                " VALUES('GHOST','approval','qa','{}','2026-09-01T00:00:00+00:00')"
            )
            # 同名批次日后创建，但幽灵审批/事件早于真实创建。
            raw.execute(
                "INSERT INTO metric_batches VALUES('LATE','valve','P1',4,'engineering','qa',"
                "'2026-09-10T00:00:00+00:00','2026-09-10T00:00:00+00:00')"
            )
            raw.execute(
                "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at)"
                " VALUES('LATE','approval','qa','{}','2026-09-05T00:00:00+00:00')"
            )
            raw.execute(
                "INSERT INTO lot_events(lot_id,event_type,actor,payload,created_at)"
                " VALUES('LATE','created','qa','{}','2026-09-10T00:00:00+00:00')"
            )
            raw.execute(
                "INSERT INTO approvals VALUES('LATE','qa','hold','旧幽灵',"
                "'2026-09-06T00:00:00+00:00')"
            )
            raw.commit()
            raw.close()

            db = connect(str(path))
            try:
                ghosts = db.execute("SELECT count(*) FROM lot_events WHERE lot_id='GHOST'").fetchone()[0]
                ghost_approvals = db.execute("SELECT count(*) FROM approvals WHERE lot_id='GHOST'").fetchone()[0]
                self.assertEqual((ghosts, ghost_approvals), (0, 0))
                late_events = [
                    row[0]
                    for row in db.execute(
                        "SELECT event_type FROM lot_events WHERE lot_id='LATE' ORDER BY event_id"
                    )
                ]
                self.assertEqual(late_events, ["created"])
                late_approvals = db.execute("SELECT count(*) FROM approvals WHERE lot_id='LATE'").fetchone()[0]
                self.assertEqual(late_approvals, 0)
                self.assertIn("revision", {r[1] for r in db.execute("PRAGMA table_info(metric_batches)")})
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
