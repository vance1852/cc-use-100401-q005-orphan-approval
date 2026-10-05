"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import uuid

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .errors import ReplayConflict, ReviewClosed, StaleRevision
from .storage import DECISION_STATUS, TERMINAL_STATUSES, connect, event, transaction, utcnow


class MetricQualityService:
    def __init__(self, database: str = ":memory:"):
        self.db = connect(database)
        self.auth = Auth(self.db)

    def bootstrap_admin(self, user_id: str = "admin", password: str = "metric-admin") -> None:
        try:
            self.auth.create_user(user_id, password, "admin")
        except Exception:
            pass

    def create_lot(self, token: str, lot_id: str, product: str, process_rev: str, sample_count: int) -> dict:
        actor = self.auth.require(token, "submit")
        if sample_count <= 0 or not lot_id.strip() or not process_rev.strip():
            raise ValueError("lot fields are invalid")
        now = utcnow()
        with transaction(self.db):
            self.db.execute(
                "INSERT INTO metric_batches VALUES(?,?,?,?,?,?,?,?,1)",
                (lot_id, product, process_rev, sample_count, "engineering", actor.user_id, now, now),
            )
            event(self.db, lot_id, "created", actor.user_id, {"product": product, "process_rev": process_rev})
        return self.get_lot(token, lot_id)

    def get_lot(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "read")
        row = self.db.execute("SELECT * FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone()
        if not row:
            raise KeyError(lot_id)
        return dict(row)

    def add_measurement(self, token: str, lot_id: str, test_frequency_hz: float, response: float, noise: float, instrument: str) -> dict:
        actor = self.auth.require(token, "measure")
        measurement_id = uuid.uuid4().hex
        with transaction(self.db):
            if not self.db.execute("SELECT 1 FROM metric_batches WHERE lot_id=?", (lot_id,)).fetchone():
                raise KeyError(lot_id)
            self.db.execute("INSERT INTO measurements VALUES(?,?,?,?,?,?,?,?)", (measurement_id, lot_id, float(test_frequency_hz), float(response), float(noise), instrument, actor.user_id, utcnow()))
            event(self.db, lot_id, "measurement", actor.user_id, {"measurement_id": measurement_id, "test_frequency_hz": test_frequency_hz})
        return {"measurement_id": measurement_id, "lot_id": lot_id}

    def analyze(self, token: str, lot_id: str) -> dict:
        self.auth.require(token, "analyze")
        rows = self.db.execute("SELECT test_frequency_hz,response FROM measurements WHERE lot_id=? ORDER BY test_frequency_hz", (lot_id,)).fetchall()
        if len(rows) < 3:
            raise ValueError("three measurements are required")
        summary = summarize_response_profile([r[0] for r in rows], [r[1] for r in rows])
        rates = yield_rate(self.get_lot(token, lot_id)["sample_count"], sum(1 for r in rows if r[1] >= 0.8), 0)
        ci = confidence_interval([r[1] for r in rows])
        return {"lot_id": lot_id, "response_profile": summary.__dict__, "yield": rates, "response_ci": ci}

    def approve(self, token: str, lot_id: str, decision: str, reason: str, expected_revision: int) -> dict:
        """在同一事务内完成门禁决定。

        事务开启后依次验证操作者资格、目标批次存在性、允许的批次状态以及
        调用方提供的修订号；批次行的状态条件更新实际命中后，才允许写入
        审批决定和审计事件。任一环节失败（不存在、已终结、无权限、版本
        落后）都随事务整体回滚，不留下任何业务事实。

        同一审批人对同一批次重放：决定与理由完全一致时返回原结果；
        内容改变则抛出 ReplayConflict，并指明与哪一版已生效决定冲突。
        """
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")
        if not isinstance(expected_revision, int) or isinstance(expected_revision, bool) or expected_revision < 1:
            raise ValueError("expected_revision is required")
        with transaction(self.db):
            actor = self.auth.require(token, "approve")
            lot = self.db.execute(
                "SELECT status,revision FROM metric_batches WHERE lot_id=?", (lot_id,)
            ).fetchone()
            if lot is None:
                raise KeyError(lot_id)

            recorded = self.db.execute(
                "SELECT decision,reason,revision,created_at FROM approvals WHERE lot_id=? AND reviewer=?",
                (lot_id, actor.user_id),
            ).fetchone()
            if recorded is not None:
                if recorded["decision"] == decision and recorded["reason"] == reason:
                    return {
                        "lot_id": lot_id,
                        "decision": recorded["decision"],
                        "reason": recorded["reason"],
                        "revision": recorded["revision"],
                        "decided_at": recorded["created_at"],
                        "replayed": True,
                    }
                raise ReplayConflict(
                    f"replay conflicts with revision {recorded['revision']} "
                    f"decision '{recorded['decision']}': {recorded['reason']}"
                )

            if lot["status"] in TERMINAL_STATUSES:
                raise ReviewClosed(f"lot {lot_id} is {lot['status']} and cannot be decided again")
            if expected_revision != lot["revision"]:
                raise StaleRevision(
                    f"expected revision {expected_revision} but lot {lot_id} is at {lot['revision']}"
                )

            new_revision = lot["revision"] + 1
            new_status = DECISION_STATUS[decision]
            now = utcnow()
            cursor = self.db.execute(
                "UPDATE metric_batches SET status=?,updated_at=?,revision=? "
                "WHERE lot_id=? AND revision=?",
                (new_status, now, new_revision, lot_id, lot["revision"]),
            )
            if cursor.rowcount != 1:
                raise StaleRevision(f"lot {lot_id} revision changed before the decision was written")

            self.db.execute(
                "INSERT INTO approvals(lot_id,reviewer,decision,reason,created_at,revision) "
                "VALUES(?,?,?,?,?,?)",
                (lot_id, actor.user_id, decision, reason, now, new_revision),
            )
            event(
                self.db,
                lot_id,
                "approval",
                actor.user_id,
                {"decision": decision, "reason": reason, "revision": new_revision},
            )
        lot_state = self.get_lot(token, lot_id)
        lot_state.update(
            {"decision": decision, "decision_revision": new_revision, "replayed": False}
        )
        return lot_state

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
