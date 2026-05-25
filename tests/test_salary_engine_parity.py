"""薪資引擎 parity 測試：
驗證新版 _sum_leave_deduction(att_leave_pairs, ...) 與
_sum_leave_deduction_legacy(leaves, ...) 在已對齊資料下產出相同 deduction。

Scope 限定：
- 所有 case 都在單月內（start/end 同月），避免 legacy 跨月重複計算問題
- Attendance 透過 sync.apply 從 LeaveRecord 寫入（確保兩版輸入對齊）

注意：
- 全天多日假的 leave_hours = days * 8（而非 8），確保 legacy 版本計算正確
- LeaveRecord.deduction_ratio 欄位有 Column default=1.0，即便傳 None 仍會在
  flush 後變成 1.0（SQLAlchemy default 行為）。生產端 leaves API 在建立/核准時
  會把 deduction_ratio 設成 LEAVE_DEDUCTION_RULES[leave_type]，因此本測試
  一律使用 production-correct ratio（sick=0.5, annual=0.0, compensatory=0.0 等），
  而非 None，以確保測到正確業務路徑。
"""

import sys
import os
from datetime import date, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# SQLite 相容性修補（必須在所有模型 import 前）
import sqlalchemy as _sa
import sqlalchemy.sql.sqltypes as _sqltypes
import sqlalchemy.dialects.postgresql as _pg_dialects
from sqlalchemy import JSON as _JSON

_pg_dialects.JSONB = _JSON  # type: ignore[assignment]


class _SQLiteInteger(_sa.Integer):  # type: ignore[misc]
    pass


_sa.BigInteger = _SQLiteInteger  # type: ignore[assignment]
_sqltypes.BigInteger = _SQLiteInteger  # type: ignore[assignment]

from models.base import Base
from models.attendance import Attendance, AttendanceStatus
from models.leave import LeaveRecord
from services import employee_leave_attendance_sync as sync
from services.salary.utils import _sum_leave_deduction, _sum_leave_deduction_legacy
from services.salary.constants import (
    LEAVE_DEDUCTION_RULES,
    SICK_LEAVE_ANNUAL_HALF_PAY_CAP_HOURS,
)

# 日薪：月薪 30000 / 30 = 1000
DAILY_SALARY = 1000.0


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def db_session(tmp_path):
    """In-memory SQLite session，建全套 schema。"""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'parity_test.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    s = SessionLocal()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def sample_employee(db_session):
    """建一個測試員工。"""
    from models.employee import Employee

    emp = Employee(
        employee_id="P001",
        name="同位素測試員工",
        base_salary=30000,
        is_active=True,
    )
    db_session.add(emp)
    db_session.commit()
    return emp


def build_leave_with_attendance(
    session,
    emp_id: int,
    start: date,
    end: date,
    leave_type: str = "personal",
    hours: float | None = None,
    start_time: str | None = None,
    end_time: str | None = None,
    deduction_ratio: float | None = None,
) -> LeaveRecord:
    """建 LeaveRecord(approved) + 用 sync.apply 寫對應 Attendance。

    hours: 明確傳入時使用；None 時依全天/部分假邏輯自動計算。
    - 全天假 (start_time=None): hours = (end - start).days + 1 * 8
    - 部分假 (start_time != None): hours 必須明確傳入

    deduction_ratio: None 代表讓 fallback 走 LEAVE_DEDUCTION_RULES（非病假推薦）。
    病假建議傳 None（標準 0.5 由 cap logic 處理），
    個人事假傳 1.0 或 None 均可（LEAVE_DEDUCTION_RULES["personal"]=1.0）。
    """
    is_partial = start_time is not None
    if hours is None:
        if is_partial:
            raise ValueError("部分假必須明確傳入 hours")
        hours = ((end - start).days + 1) * 8.0

    lv = LeaveRecord(
        employee_id=emp_id,
        leave_type=leave_type,
        start_date=start,
        end_date=end,
        start_time=start_time,
        end_time=end_time,
        leave_hours=hours,
        is_approved=True,
        deduction_ratio=deduction_ratio,
    )
    session.add(lv)
    session.flush()
    sync.apply(session, lv.id)
    return lv


def collect_pairs(session, lv: LeaveRecord) -> list[tuple[Attendance, LeaveRecord]]:
    """取回 lv 對應的所有 Attendance rows，組成 (att, lv) 列表。"""
    atts = session.query(Attendance).filter_by(leave_record_id=lv.id).all()
    return [(att, lv) for att in atts]


# ── 核心比較 helper ───────────────────────────────────────────────────────────


def _assert_parity(
    session,
    leaves: list[LeaveRecord],
    label: str,
    ytd_sick: float = 0.0,
    tolerance: float = 0.01,
) -> tuple[float, float]:
    """計算兩版 deduction 並 assert 一致。

    回傳 (new_result, legacy_result) 供 caller 額外檢查。
    """
    all_pairs: list[tuple[Attendance, LeaveRecord]] = []
    for lv in leaves:
        all_pairs.extend(collect_pairs(session, lv))

    new_val = _sum_leave_deduction(all_pairs, DAILY_SALARY, ytd_sick)
    old_val = _sum_leave_deduction_legacy(leaves, DAILY_SALARY, ytd_sick)

    assert (
        abs(new_val - old_val) < tolerance
    ), f"[{label}] new={new_val:.4f} old={old_val:.4f} diff={abs(new_val-old_val):.4f}"
    return new_val, old_val


# ── 30+ parametrize cases ─────────────────────────────────────────────────────
#
# 每個 tuple：(label, leave_params_list, ytd_sick)
# leave_params_list: list of dict，傳入 build_leave_with_attendance（不含 emp_id/session）
#
# 月份統一使用 2026 年 5 月（不跨月），確保 legacy 不重複計算。

MAY = lambda d: date(2026, 5, d)  # noqa: E731

PARITY_CASES = [
    # ── 全勤（無請假）─────────────────────────────────────────────
    (
        "全勤無假",
        [],
        0.0,
    ),
    # ── 全天事假 ─────────────────────────────────────────────────
    (
        "全天事假 1 天",
        [dict(start=MAY(5), end=MAY(5), leave_type="personal", deduction_ratio=1.0)],
        0.0,
    ),
    (
        "全天事假 3 天",
        [dict(start=MAY(5), end=MAY(7), leave_type="personal", deduction_ratio=1.0)],
        0.0,
    ),
    (
        "全天事假 5 天",
        [dict(start=MAY(12), end=MAY(16), leave_type="personal", deduction_ratio=1.0)],
        0.0,
    ),
    # ── 全天病假 ─────────────────────────────────────────────────
    (
        "全天病假 1 天（年度內）",
        [dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.5)],
        0.0,
    ),
    (
        "全天病假 5 天（年度內）",
        [dict(start=MAY(5), end=MAY(9), leave_type="sick", deduction_ratio=0.5)],
        0.0,
    ),
    (
        "全天病假 30 天（恰好達上限 240h）",
        [dict(start=MAY(1), end=MAY(30), leave_type="sick", deduction_ratio=0.5)],
        0.0,
    ),
    (
        "全天病假超過 cap（ytd=240h，8h 全數扣全薪）",
        [dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.5)],
        240.0,
    ),
    (
        "全天病假跨 cap（ytd=232h，16h 分裂：8h 半薪+8h 全薪）",
        [dict(start=MAY(5), end=MAY(6), leave_type="sick", deduction_ratio=0.5)],
        232.0,
    ),
    (
        "病假 HR 人工覆寫（ratio=0.0，全薪特殊病假）",
        [dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.0)],
        0.0,
    ),
    (
        "病假 HR 覆寫為全扣（ratio=1.0，偏離標準 0.5）",
        [dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=1.0)],
        0.0,
    ),
    # ── 部分假（半天/小時）─────────────────────────────────────────
    (
        "半天事假（4h，有 start/end_time）",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="personal",
                hours=4.0,
                start_time="09:00",
                end_time="13:00",
                deduction_ratio=1.0,
            )
        ],
        0.0,
    ),
    (
        "小時事假 1.5hr",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="personal",
                hours=1.5,
                start_time="09:00",
                end_time="10:30",
                deduction_ratio=1.0,
            )
        ],
        0.0,
    ),
    (
        "小時事假 3hr",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="personal",
                hours=3.0,
                start_time="09:00",
                end_time="12:00",
                deduction_ratio=1.0,
            )
        ],
        0.0,
    ),
    (
        "小時事假 4hr",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="personal",
                hours=4.0,
                start_time="09:00",
                end_time="13:00",
                deduction_ratio=1.0,
            )
        ],
        0.0,
    ),
    (
        "小時事假 7hr（近全天）",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="personal",
                hours=7.0,
                start_time="09:00",
                end_time="16:00",
                deduction_ratio=1.0,
            )
        ],
        0.0,
    ),
    (
        "半天病假（4h）",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="sick",
                hours=4.0,
                start_time="09:00",
                end_time="13:00",
                deduction_ratio=0.5,
            )
        ],
        0.0,
    ),
    # ── 特休 / 補休（不扣薪）─────────────────────────────────────
    (
        "特休 1 天（不扣薪）",
        [dict(start=MAY(5), end=MAY(5), leave_type="annual", deduction_ratio=0.0)],
        0.0,
    ),
    (
        "補休 1 天（不扣薪）",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="compensatory",
                deduction_ratio=0.0,
            )
        ],
        0.0,
    ),
    (
        "婚假 3 天（不扣薪）",
        [dict(start=MAY(5), end=MAY(7), leave_type="marriage", deduction_ratio=0.0)],
        0.0,
    ),
    (
        "公假 1 天（不扣薪）",
        [dict(start=MAY(5), end=MAY(5), leave_type="official", deduction_ratio=0.0)],
        0.0,
    ),
    # ── 混合假（同月多筆）─────────────────────────────────────────
    (
        "同月事假 + 病假混合",
        [
            dict(start=MAY(5), end=MAY(5), leave_type="personal", deduction_ratio=1.0),
            dict(start=MAY(10), end=MAY(10), leave_type="sick", deduction_ratio=0.5),
        ],
        0.0,
    ),
    (
        "同月多筆事假（不連續）",
        [
            dict(start=MAY(3), end=MAY(3), leave_type="personal", deduction_ratio=1.0),
            dict(start=MAY(8), end=MAY(8), leave_type="personal", deduction_ratio=1.0),
            dict(
                start=MAY(20), end=MAY(20), leave_type="personal", deduction_ratio=1.0
            ),
        ],
        0.0,
    ),
    (
        "同月病假 + 補休混合（補休不扣、病假扣半）",
        [
            dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.5),
            dict(
                start=MAY(12),
                end=MAY(12),
                leave_type="compensatory",
                deduction_ratio=0.0,
            ),
        ],
        0.0,
    ),
    (
        "同月多筆病假累計跨 cap（ytd=228h，第二筆病假 16h 部分超限）",
        [
            dict(
                start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.5
            ),  # 8h → total=236h
            dict(
                start=MAY(10), end=MAY(11), leave_type="sick", deduction_ratio=0.5
            ),  # 16h → 236+4=240h 半薪, 12h 全薪
        ],
        228.0,
    ),
    (
        "半天病假 + 全天事假混合",
        [
            dict(
                start=MAY(5),
                end=MAY(5),
                leave_type="sick",
                hours=4.0,
                start_time="09:00",
                end_time="13:00",
                deduction_ratio=0.5,
            ),
            dict(start=MAY(6), end=MAY(6), leave_type="personal", deduction_ratio=1.0),
        ],
        0.0,
    ),
    # ── 月底邊界（start=5/28, end=5/30，仍在同月）───────────────
    (
        "月底邊界事假（5/28～5/30）",
        [dict(start=MAY(28), end=MAY(30), leave_type="personal", deduction_ratio=1.0)],
        0.0,
    ),
    (
        "月底邊界病假（5/29～5/30）",
        [dict(start=MAY(29), end=MAY(30), leave_type="sick", deduction_ratio=0.5)],
        0.0,
    ),
    # ── 全月高量病假（不跨月）────────────────────────────────────
    (
        "全天病假 20 天（年度半薪上限內）",
        [dict(start=MAY(1), end=MAY(20), leave_type="sick", deduction_ratio=0.5)],
        0.0,
    ),
    (
        "病假恰好觸碰 cap（ytd=232h + 本月 8h = 240h 半薪上限）",
        [dict(start=MAY(5), end=MAY(5), leave_type="sick", deduction_ratio=0.5)],
        232.0,
    ),
    # ── 連續特休後接事假（確認累計計算正確）──────────────────────
    (
        "特休 2 天 + 事假 2 天",
        [
            dict(start=MAY(5), end=MAY(6), leave_type="annual", deduction_ratio=0.0),
            dict(start=MAY(8), end=MAY(9), leave_type="personal", deduction_ratio=1.0),
        ],
        0.0,
    ),
    # ── 事假 + 小時病假混合 ────────────────────────────────────
    (
        "全天事假 + 小時病假（1.5h）",
        [
            dict(start=MAY(5), end=MAY(5), leave_type="personal", deduction_ratio=1.0),
            dict(
                start=MAY(7),
                end=MAY(7),
                leave_type="sick",
                hours=1.5,
                start_time="09:00",
                end_time="10:30",
                deduction_ratio=0.5,
            ),
        ],
        0.0,
    ),
]


@pytest.mark.parametrize("label,leave_specs,ytd_sick", PARITY_CASES)
def test_parity_new_vs_legacy(
    db_session, sample_employee, label, leave_specs, ytd_sick
):
    """新版 _sum_leave_deduction 與 legacy 版在單月場景下 deduction 結果必須一致。

    透過 sync.apply 確保 Attendance 已正確 backfill（與生產環境一致）。
    """
    leaves = []
    for spec in leave_specs:
        lv = build_leave_with_attendance(
            db_session,
            emp_id=sample_employee.id,
            **spec,
        )
        leaves.append(lv)

    db_session.flush()

    new_val, old_val = _assert_parity(db_session, leaves, label, ytd_sick=ytd_sick)

    # 全勤 case 兩版都應該 == 0
    if not leave_specs:
        assert new_val == 0.0
        assert old_val == 0.0


# ── 額外驗證：新版 vs legacy 的絕對值合理性抽查 ───────────────────────────────


def test_sanity_full_day_personal_1day(db_session, sample_employee):
    """全天事假 1 天：扣款應為 1000（日薪）"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(5),
        leave_type="personal",
        deduction_ratio=1.0,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    assert abs(new_val - 1000.0) < 0.01, f"expected 1000, got {new_val}"


def test_sanity_full_day_sick_within_cap(db_session, sample_employee):
    """全天病假 1 天（年度上限內）：扣款應為 500（日薪 × 0.5）"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(5),
        leave_type="sick",
        deduction_ratio=0.5,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    assert abs(new_val - 500.0) < 0.01, f"expected 500, got {new_val}"


def test_sanity_annual_leave_zero_deduction(db_session, sample_employee):
    """特休 1 天：扣款應為 0"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(5),
        leave_type="annual",
        deduction_ratio=0.0,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    assert new_val == 0.0, f"expected 0, got {new_val}"


def test_sanity_compensatory_leave_zero_deduction(db_session, sample_employee):
    """補休 1 天：扣款應為 0"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(5),
        leave_type="compensatory",
        deduction_ratio=0.0,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    assert new_val == 0.0, f"expected 0, got {new_val}"


def test_sanity_3day_personal_leaves(db_session, sample_employee):
    """全天事假 3 天：扣款應為 3000"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(7),
        leave_type="personal",
        deduction_ratio=1.0,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    old_val = _sum_leave_deduction_legacy([lv], DAILY_SALARY)
    assert abs(new_val - 3000.0) < 0.01, f"new expected 3000, got {new_val}"
    assert abs(old_val - 3000.0) < 0.01, f"legacy expected 3000, got {old_val}"


def test_sanity_hourly_personal_15h(db_session, sample_employee):
    """小時事假 1.5h：扣款應為 187.5（1000 × 1.5/8）"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(5),
        leave_type="personal",
        hours=1.5,
        start_time="09:00",
        end_time="10:30",
        deduction_ratio=1.0,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(pairs, DAILY_SALARY)
    expected = (1.5 / 8) * DAILY_SALARY * 1.0  # 187.5
    assert abs(new_val - expected) < 0.01, f"expected {expected}, got {new_val}"


def test_sanity_sick_cap_split_232ytd_16h(db_session, sample_employee):
    """病假 ytd=232h + 本月 16h：前 8h 半薪=500 + 後 8h 全薪=1000 = 1500"""
    lv = build_leave_with_attendance(
        db_session,
        emp_id=sample_employee.id,
        start=MAY(5),
        end=MAY(6),  # 2天 = 16h
        leave_type="sick",
        deduction_ratio=0.5,
    )
    db_session.flush()

    pairs = collect_pairs(db_session, lv)
    new_val = _sum_leave_deduction(
        pairs, DAILY_SALARY, ytd_sick_hours_before_month=232.0
    )
    old_val = _sum_leave_deduction_legacy(
        [lv], DAILY_SALARY, ytd_sick_hours_before_month=232.0
    )
    assert abs(new_val - 1500.0) < 0.01, f"new expected 1500, got {new_val}"
    assert abs(old_val - 1500.0) < 0.01, f"legacy expected 1500, got {old_val}"
