"""协调认证、批次、测试和放行门禁的应用服务。"""

from __future__ import annotations

import sqlite3
import uuid
from typing import Sequence

from .analytics import confidence_interval, summarize_response_profile, yield_rate
from .auth import Auth
from .errors import Conflict, InvalidState
from .storage import connect, event, transaction, utcnow


# 每种决定落地后的批次状态。
DECISION_STATUS = {"release": "released", "hold": "hold", "reject": "rejected"}
# 只有工程状态下的批次可以受理审批；放行/拒收/挂起均为终结状态。
APPROVABLE_STATUS = "engineering"
# 审批期间允许流转到的状态。
TERMINAL_STATUS = set(DECISION_STATUS.values())


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
                "INSERT INTO metric_batches(lot_id,product,process_rev,sample_count,status,owner,revision,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,1,?,?)",
                (lot_id, product, process_rev, sample_count, APPROVABLE_STATUS, actor.user_id, now, now),
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

    def approve(
        self,
        token: str,
        lot_id: str,
        decision: str,
        reason: str,
        expected_revision: int,
    ) -> dict:
        # 调用方资格与入参在事务外即可判定；目标批次、状态与版本必须在
        # 同一事务内随状态更新一起验证，失败时整笔回滚，不留审批或审计。
        actor = self.auth.require(token, "approve")
        if decision not in DECISION_STATUS or not reason.strip():
            raise ValueError("decision and reason are required")
        if not isinstance(expected_revision, int) or expected_revision < 1:
            raise ValueError("expected_revision must be a positive integer")
        status = DECISION_STATUS[decision]
        now = utcnow()
        with transaction(self.db):
            row = self.db.execute(
                "SELECT revision,status FROM metric_batches WHERE lot_id=?", (lot_id,)
            ).fetchone()
            if row is None:
                raise KeyError(lot_id)
            if row["status"] in TERMINAL_STATUS:
                # 已终结批次：完全一致的重放返回原结果；改变决定或
                # 理由的重放必须指出与哪一版冲突，且不写入任何新事实。
                previous = self.db.execute(
                    "SELECT revision,decision,reason,reviewer FROM approvals "
                    "WHERE lot_id=? ORDER BY revision DESC,rowid DESC LIMIT 1",
                    (lot_id,),
                ).fetchone()
                if previous is None:
                    raise RuntimeError(f"lot {lot_id} is terminal without recorded approval")
                if (
                    previous["revision"] == expected_revision
                    and previous["decision"] == decision
                    and previous["reason"] == reason
                ):
                    return {
                        "lot_id": lot_id,
                        "status": row["status"],
                        "revision": row["revision"],
                        "replayed": True,
                    }
                raise Conflict(
                    f"approval conflicts with revision {previous['revision']} "
                    f"decision {previous['decision']!r}"
                )
            if row["status"] != APPROVABLE_STATUS:
                raise InvalidState(f"lot status {row['status']!r} is not approvable")
            if row["revision"] != expected_revision:
                raise Conflict(
                    f"stale revision: expected {row['revision']}, caller provided {expected_revision}"
                )
            cursor = self.db.execute(
                "UPDATE metric_batches SET status=?,revision=revision+1,updated_at=? "
                "WHERE lot_id=? AND status=? AND revision=?",
                (status, now, lot_id, APPROVABLE_STATUS, expected_revision),
            )
            # 只有目标状态实际更新成功，才允许写入决定和审计事件。
            if cursor.rowcount != 1:
                raise Conflict("lot state or revision changed concurrently")
            try:
                self.db.execute(
                    "INSERT INTO approvals(lot_id,revision,reviewer,decision,reason,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (lot_id, expected_revision, actor.user_id, decision, reason, now),
                )
            except sqlite3.IntegrityError as exc:
                raise Conflict("approval for this revision already exists") from exc
            event(
                self.db,
                lot_id,
                "approval",
                actor.user_id,
                {"decision": decision, "reason": reason, "revision": expected_revision},
            )
        result = self.get_lot(token, lot_id)
        result["replayed"] = False
        return result

    def audit(self, token: str, lot_id: str) -> list[dict]:
        self.auth.require(token, "read")
        return [dict(r) for r in self.db.execute("SELECT * FROM lot_events WHERE lot_id=? ORDER BY event_id", (lot_id,)).fetchall()]
