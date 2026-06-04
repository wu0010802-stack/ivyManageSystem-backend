"""考核 auto-derivation DB 整合測試。

端對端驗證「真實 sync_score_items → recompute_summaries」路徑：
  留校率 → tier → delta
  考勤遲到 → per_unit → delta
  最終 AppraisalSummary 等第與總分與推導值吻合

此測試解決「純函式通過不代表 live DB 路徑正確」的問題。

場景：
  - cycle 114上（2025-08-01～2026-01-31），base_score_calc_date=2025-09-15
  - enrollment_actual=121, enrollment_target=160 → base_score=75.6
  - 班級 10 期初學生，1 中途退學（withdrawal_date 在 cycle 內）→ 期末 9 → 留校率 90.00%
  - RETURNING_RATE_0315 規則採 production 5-tier 配置（apxlal01）：
      100→+6.0, 95→0.0, 90→-1.7, 80→-3.0, 0→-6.0
      90% 命中 min≥90 tier → delta = -1.7
  - RETURNING_RATE_0915 規則採 production 3-tier 配置（apxlal01）：
      100→0, 95→-1.7, 0→-3.0
      90% 命中 min≥0 tier（90 < 95，不命中 95）→ delta = -3.0
  - LATE_EARLY：3 筆 is_late=True Attendance → 3 × -0.25 = -0.75
  - 其餘 rules 全 0 → event_sum = -0.75 + -3.0 + -1.7 = -5.45
  - total_score = 75.6 + (-5.45) = 70.15 → 乙等 (PASS)
  - PASS 不發獎金 → bonus = 0.00
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.appraisal import appraisal_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.appraisal import (
    AppraisalCycle,
    AppraisalParticipant,
    AppraisalScoreItem,
    AppraisalScoringRule,
    AppraisalSummary,
    CycleStatus,
    RoleGroup,
    Semester,
)
from models.attendance import Attendance
from models.auth import User
from models.classroom import LIFECYCLE_ACTIVE, LIFECYCLE_WITHDRAWN, Classroom, Student
from models.database import Base
from models.employee import Employee
from utils.auth import hash_password
from utils.permissions import Permission

# ---------------------------------------------------------------------------
# Fixture: fresh DB + TestClient（與 test_appraisal_sync_score_items_extended.py 同模式）
# ---------------------------------------------------------------------------


@pytest.fixture
def client_with_db(tmp_path):
    db_path = tmp_path / "appraisal-auto-derive.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router)
    app.include_router(appraisal_router)

    with TestClient(app) as client:
        yield client, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


# ---------------------------------------------------------------------------
# Helper: 建立 user + login
# ---------------------------------------------------------------------------


def _create_user(
    session, username, perms, password="TempPass123", role="admin"
) -> User:
    if isinstance(perms, str):
        perms = [perms]
    user = User(
        username=username,
        password_hash=hash_password(password),
        role=role,
        permission_names=perms,
        is_active=True,
    )
    session.add(user)
    session.flush()
    return user


def _login(client, username, password="TempPass123"):
    return client.post(
        "/api/auth/login", json={"username": username, "password": password}
    )


# ---------------------------------------------------------------------------
# Helper: 種 production scoring rules（對齊 apxlal01，effective_from=2025-08-01）
#
# 只需 auto-item codes（LATE_EARLY / RETURNING_RATE_0915 / RETURNING_RATE_0315）
# 及其他 auto items（MISSING_PUNCH, LEAVE, AFTER_CLASS_RATE, REWARD_PUNISH）。
# Manual item codes 不需 scoring rules 也能 sync（compute_all_deltas 對 manual code
# 若找不到 rule 只是不產出該 code，但存在時才計算 delta）。
# 我們補齊所有 auto codes，讓 sync 產出完整的 auto rows，避免警告干擾。
# ---------------------------------------------------------------------------

_TEACHING = ["HEAD_TEACHER", "ASSISTANT"]

PRODUCTION_RULES = [
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    (
        "RETURNING_RATE_0915",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 0},
                {"min": 95, "delta": -1.7},
                {"min": 0, "delta": -3.0},
            ],
        },
        _TEACHING,
    ),
    (
        "RETURNING_RATE_0315",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -1.7},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -6.0},
            ],
        },
        _TEACHING,
    ),
    (
        "AFTER_CLASS_RATE",
        "FLAT_THRESHOLD",
        {
            "input_field": "activity_rate",
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
        },
        _TEACHING,
    ),
    (
        "REWARD_PUNISH",
        "DISCIPLINARY_TIERED",
        {"warning_delta": -1.0, "minor_delta": -3.0, "major_delta": -10.0},
        None,
    ),
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3.0}, None),
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("SPED", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
]


def _seed_production_rules(session):
    """種 15 條 production scoring rules（effective_from=2025-08-01）。"""
    for code, rtype, cfg, roles in PRODUCTION_RULES:
        session.add(
            AppraisalScoringRule(
                item_code=code,
                effective_from=date(2025, 8, 1),
                rule_type=rtype,
                rule_config=cfg,
                applies_to_role_groups=roles,
            )
        )


# ---------------------------------------------------------------------------
# 主測試：end-to-end sync → recompute，驗證推導值
# ---------------------------------------------------------------------------


class TestAutoDerivationIntegration:
    """真實 DB 路徑：留校率→tier→delta + 考勤→delta → AppraisalSummary 正確。"""

    def test_retention_and_attendance_derive_correct_summary(self, client_with_db):
        """場景：90% 留校率 + 3 遲到 → 推導後 total=70.15, grade=PASS, bonus=0.

        驗證重點：
          1. AppraisalScoreItem RETURNING_RATE_0315 有 score_delta = -1.7
          2. AppraisalScoreItem LATE_EARLY 有 score_delta = -0.75
          3. AppraisalSummary.grade = PASS（乙等），total_score = 70.15
          4. AppraisalSummary.bonus_amount = 0.00（PASS 不發獎金）
        """
        client, sf = client_with_db

        with sf() as s:
            _create_user(s, "admin1", Permission.APPRAISAL_EVENT_WRITE)

            # ── 員工 + 班級 ──────────────────────────────────────────
            emp = Employee(employee_id="E001", name="王小華", is_active=True)
            s.add(emp)
            s.flush()

            cls = Classroom(name="大班A", school_year=114, semester=1, is_active=True)
            s.add(cls)
            s.flush()

            # ── cycle：enrollment_actual=121, enrollment_target=160 → base_score=75.6 ──
            cycle = AppraisalCycle(
                academic_year=114,
                semester=Semester.FIRST,
                start_date=date(2025, 8, 1),
                end_date=date(2026, 1, 31),
                base_score_calc_date=date(2025, 9, 15),
                base_score=Decimal("75.6"),
                enrollment_target=160,
                enrollment_actual=121,
                status=CycleStatus.OPEN,
            )
            s.add(cycle)
            s.flush()

            # ── participant（hire_months=6，通過 Task-4 的 2-month gate）──
            p = AppraisalParticipant(
                cycle_id=cycle.id,
                employee_id=emp.id,
                role_group=RoleGroup.HEAD_TEACHER,
                classroom_id=cls.id,
                hire_months_in_cycle=Decimal("6"),
                is_excluded=False,
            )
            s.add(p)
            s.flush()

            # ── 留校率：10 期初學生，1 在 cycle 內退學 → 9 期末 → 90.00% ──
            # 期初：enrollment_date <= 2025-08-01
            # 期末 active：lifecycle=active + 無 withdrawal_date（或 withdrawal > 2026-01-31）
            for i in range(9):
                s.add(
                    Student(
                        student_id=f"S{i:03d}",
                        name=f"學生{i}",
                        classroom_id=cls.id,
                        enrollment_date=date(2025, 6, 1),
                        lifecycle_status=LIFECYCLE_ACTIVE,
                    )
                )
            # 第 10 個：入學期初（enrollment < start），但在 cycle 內退學
            s.add(
                Student(
                    student_id="S009",
                    name="退學生",
                    classroom_id=cls.id,
                    enrollment_date=date(2025, 6, 1),
                    withdrawal_date=date(2025, 12, 1),  # 在 cycle 內退學
                    lifecycle_status=LIFECYCLE_WITHDRAWN,
                )
            )

            # ── 考勤：3 筆遲到（在 cycle 內），LATE_EARLY = 3×(-0.25) = -0.75 ──
            for d in [date(2025, 9, 1), date(2025, 10, 1), date(2025, 11, 1)]:
                s.add(
                    Attendance(
                        employee_id=emp.id,
                        attendance_date=d,
                        is_late=True,
                    )
                )

            # ── scoring rules（對齊 apxlal01 production config）──
            _seed_production_rules(s)

            s.commit()
            cycle_id = cycle.id
            p_id = p.id

        # ── 登入 ──────────────────────────────────────────────────────
        assert _login(client, "admin1").status_code == 200

        # ── Step 1: sync_score_items → 寫入 auto AppraisalScoreItem rows ──
        r_sync = client.post(f"/api/appraisal/cycles/{cycle_id}/sync_score_items")
        assert r_sync.status_code == 200, r_sync.text

        # ── Step 2: recompute_summaries → 計算 AppraisalSummary ──
        r_recompute = client.post(
            f"/api/appraisal/cycles/{cycle_id}/summaries:recompute"
        )
        assert r_recompute.status_code == 200, r_recompute.text

        # ── 驗證 AppraisalScoreItem 推導值 ──
        with sf() as s:
            items = s.query(AppraisalScoreItem).filter_by(participant_id=p_id).all()
            by_code = {i.item_code: i for i in items}

            # RETURNING_RATE_0315：90% 命中 min=90 tier → -1.7
            assert (
                "RETURNING_RATE_0315" in by_code
            ), f"sync 未產出 RETURNING_RATE_0315；有 codes={list(by_code)}"
            rr315 = by_code["RETURNING_RATE_0315"]
            assert rr315.score_delta == Decimal("-1.7"), (
                f"期望 -1.7，實際 {rr315.score_delta}；"
                f"raw_value={rr315.raw_value}，note={rr315.note}"
            )

            # RETURNING_RATE_0915：90% 命中 min=0 tier → -3.0
            assert (
                "RETURNING_RATE_0915" in by_code
            ), f"sync 未產出 RETURNING_RATE_0915；有 codes={list(by_code)}"
            rr915 = by_code["RETURNING_RATE_0915"]
            assert rr915.score_delta == Decimal("-3.0"), (
                f"期望 -3.0，實際 {rr915.score_delta}；"
                f"raw_value={rr915.raw_value}，note={rr915.note}"
            )

            # LATE_EARLY：3 遲到 × -0.25 = -0.75
            assert (
                "LATE_EARLY" in by_code
            ), f"sync 未產出 LATE_EARLY；有 codes={list(by_code)}"
            late = by_code["LATE_EARLY"]
            assert late.score_delta == Decimal("-0.75"), (
                f"期望 -0.75，實際 {late.score_delta}；"
                f"raw_value={late.raw_value}，note={late.note}"
            )
            # raw_value 應等於遲到次數 3
            assert late.raw_value == Decimal(
                "3"
            ), f"LATE_EARLY raw_value 期望 3，實際 {late.raw_value}"

            # ── 驗證 AppraisalSummary ──────────────────────────────────
            summary = s.query(AppraisalSummary).filter_by(participant_id=p_id).first()
            assert summary is not None, "recompute 未建立 AppraisalSummary"

            # total_score = 75.6 + (-0.75 + -3.0 + -1.7 + 其餘0) = 75.6 - 5.45 = 70.15
            assert summary.total_score == Decimal("70.15"), (
                f"total_score 期望 70.15，實際 {summary.total_score}；"
                f"base={summary.base_score}，event_sum={summary.event_score_sum}"
            )

            # base_score 從 enrollment_actual=121, target=160 → 75.6
            assert summary.base_score == Decimal("75.6"), (
                f"base_score 期望 75.6，實際 {summary.base_score}（"
                f"確認 enrollment_actual/target 已設定）"
            )

            # 70.15 → 乙等 (PASS)
            from models.appraisal import Grade

            assert summary.grade == Grade.PASS, f"grade 期望 PASS，實際 {summary.grade}"

            # PASS 不發獎金
            assert summary.bonus_amount == Decimal(
                "0.00"
            ), f"bonus_amount 期望 0.00，實際 {summary.bonus_amount}"
