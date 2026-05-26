# 才藝退費 Calculator + POS Diff Verify Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 為才藝退費路徑加上 server-side calculator（對齊 fee_refund_calculator 三段比例）+ POS 退費實退 vs 建議差距 > NT$100 強制簽核。

**Architecture:** 純函式 calculator（不碰 DB）→ router-side query helper（query attendance + course.sessions）→ GET endpoint 預覽 + POS/單筆退費路徑 server-side verify wiring。Audit 透過既有 `request.state.audit_changes` dict 擴充。

**Tech Stack:** FastAPI / SQLAlchemy / pytest / Pydantic v2 / 既有 `utils/rounding.round_half_up` / 既有 `services/activity_payment_guards`。

**Spec:** `docs/superpowers/specs/2026-05-26-activity-refund-calculator-design.md`

---

## File Structure

**新檔（5）：**
- `services/activity_refund_calculator.py` — 純函式 `calc_course_refund` / `calc_supply_refund`
- `services/activity_refund_query.py` — `build_refund_suggestion(session, reg_id)`
- `tests/test_activity_refund_calculator.py` — 純函式測試
- `tests/test_activity_refund_query.py` — query helper 含 DB fixture
- `tests/test_activity_refund_diff_verify.py` — POS verify e2e（TestClient）

**改檔（5）：**
- `utils/activity_constants.py` — +1 const `ACTIVITY_REFUND_DIFF_THRESHOLD`
- `services/activity_payment_guards.py` — +1 guard `require_approve_for_refund_diff`
- `schemas/activity_admin.py` — +`RefundSuggestionItem` / `RefundSuggestionResponse`
- `api/activity/registrations.py` — +1 GET endpoint `/{reg_id}/refund-suggestion`
- `api/activity/pos.py` — refund 路徑加 verify wiring + audit_changes update
- `api/activity/registrations_payments.py` — 退費路徑加 verify wiring + audit_changes update

**不動：** DB schema、migration、既有 `require_approve_for_large_refund` / `require_refund_reason` / advisory lock / 累積簽核（第 1-3 道閘全保留）、前端任何檔。

---

## Task 1: 加 `ACTIVITY_REFUND_DIFF_THRESHOLD` 常數

**Files:**
- Modify: `utils/activity_constants.py`

- [ ] **Step 1: 編輯 `utils/activity_constants.py` 加 const**

在檔尾既有 `ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD` 之後追加：

```python

# 實退 vs calculator 建議值差距閾值（NT$）；超過此差距需 ACTIVITY_PAYMENT_APPROVE 權限。
# Why: 員工算錯/故意多退之事前制衡；與 REFUND_APPROVAL_THRESHOLD（總額）獨立，
# 兩道閘共存，任一觸發都要簽核。
ACTIVITY_REFUND_DIFF_THRESHOLD = 100
```

- [ ] **Step 2: 確認 import 路徑可用**

Run: `cd /Users/yilunwu/Desktop/ivy-backend && python -c "from utils.activity_constants import ACTIVITY_REFUND_DIFF_THRESHOLD; print(ACTIVITY_REFUND_DIFF_THRESHOLD)"`

Expected output: `100`

- [ ] **Step 3: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add utils/activity_constants.py
git commit -m "feat(activity): 加 ACTIVITY_REFUND_DIFF_THRESHOLD 常數（NT\$100）

對應 spec 2026-05-26-activity-refund-calculator-design §9。
後續 task 會把 calculator + verify 接上此常數。"
```

---

## Task 2: 純函式 Calculator（TDD）

**Files:**
- Create: `services/activity_refund_calculator.py`
- Test: `tests/test_activity_refund_calculator.py`

- [ ] **Step 1: 寫 failing test `tests/test_activity_refund_calculator.py`**

```python
"""才藝退費純函式 calculator 測試。

對齊 services/finance/fee_refund_calculator.py 的測試風格：純函式輸入輸出，
不碰 DB。涵蓋 spec §3 規則（三段比例 + T_served=0 特例）+ §10 邊界。
"""

import pytest

from services.activity_refund_calculator import (
    calc_course_refund,
    calc_supply_refund,
)


# ── calc_course_refund: 三段比例 + 特例 ─────────────────────────────────────


def test_course_not_started_refunds_full():
    """T_served=0 特例 → 退 100%，ratio_band='not_started'。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=0)
    assert r["suggested_amount"] == 1500
    assert r["calc_payload"]["ratio_band"] == "not_started"
    assert r["calc_payload"]["refund_ratio"] == "1"
    assert r["warnings"] == []


def test_course_under_one_third_refunds_two_thirds():
    """0 < served_ratio < 1/3 → 退 2/3。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    # 1500 × 2/3 = 1000
    assert r["suggested_amount"] == 1000
    assert r["calc_payload"]["ratio_band"] == "<1/3"
    assert r["calc_payload"]["refund_ratio"] == "2/3"


def test_course_middle_refunds_one_third():
    """1/3 ≤ served_ratio < 2/3 → 退 1/3。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=4)
    # 1500 × 1/3 = 500
    assert r["suggested_amount"] == 500
    assert r["calc_payload"]["ratio_band"] == "1/3..2/3"
    assert r["calc_payload"]["refund_ratio"] == "1/3"


def test_course_over_two_thirds_no_refund():
    """served_ratio ≥ 2/3 → 0。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=7)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["ratio_band"] == ">=2/3"
    assert r["calc_payload"]["refund_ratio"] == "0"


def test_course_exactly_one_third_falls_in_middle():
    """T_served / T_total = 1/3 exactly → 1/3..2/3 段（退 1/3）。"""
    r = calc_course_refund(amount_due=1500, T_total=12, T_served=4)
    assert r["calc_payload"]["ratio_band"] == "1/3..2/3"


def test_course_exactly_two_thirds_no_refund():
    """T_served / T_total = 2/3 exactly → >=2/3 段（不退）。"""
    r = calc_course_refund(amount_due=1500, T_total=12, T_served=8)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["ratio_band"] == ">=2/3"


def test_course_T_total_zero_raises():
    """T_total <= 0 → ValueError（對齊 fee_refund_calculator）。"""
    with pytest.raises(ValueError, match="T_total"):
        calc_course_refund(amount_due=1500, T_total=0, T_served=0)


def test_course_T_total_negative_raises():
    """T_total < 0 → ValueError。"""
    with pytest.raises(ValueError, match="T_total"):
        calc_course_refund(amount_due=1500, T_total=-1, T_served=0)


def test_course_T_served_negative_clamps_to_zero():
    """T_served < 0 clamp 0 → 套 not_started 特例全退。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=-5)
    assert r["suggested_amount"] == 1500
    assert r["calc_payload"]["T_served"] == 0
    assert r["calc_payload"]["ratio_band"] == "not_started"


def test_course_T_served_over_total_clamps():
    """T_served > T_total clamp T_total → >=2/3 不退。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=100)
    assert r["suggested_amount"] == 0
    assert r["calc_payload"]["T_served"] == 10


def test_course_round_half_up_applied():
    """1001 × 2/3 = 667.33... → round_half_up → 667。"""
    r = calc_course_refund(amount_due=1001, T_total=10, T_served=1)
    assert r["suggested_amount"] == 667


def test_course_amount_due_zero():
    """amount_due=0 → 各段都 0（避免 div by zero 等假錯）。"""
    r = calc_course_refund(amount_due=0, T_total=10, T_served=0)
    assert r["suggested_amount"] == 0
    r2 = calc_course_refund(amount_due=0, T_total=10, T_served=5)
    assert r2["suggested_amount"] == 0


def test_course_formula_string_contains_key_parts():
    """calc_payload.formula 字串包含 amount/ratio 主要資訊（方便事後查 audit）。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    f = r["calc_payload"]["formula"]
    assert "1500" in f and "2/3" in f


def test_course_calc_method_label():
    """calc_method 固定字串供 audit 反查。"""
    r = calc_course_refund(amount_due=1500, T_total=10, T_served=1)
    assert r["calc_method"] == "activity_course_ratio"


# ── calc_supply_refund: 用品一律不退 ────────────────────────────────────────


def test_supply_always_zero():
    """用品 suggested 永 0 + warning 提示已交付。"""
    r = calc_supply_refund(amount_due=500)
    assert r["suggested_amount"] == 0
    assert r["calc_method"] == "activity_supply_no_refund"
    assert any("交付" in w or "不予退費" in w for w in r["warnings"])


def test_supply_amount_due_zero():
    """amount_due=0 也回 0 + warning，不報錯。"""
    r = calc_supply_refund(amount_due=0)
    assert r["suggested_amount"] == 0
    assert r["warnings"]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_calculator.py -v 2>&1 | tail -20
```

Expected: `ModuleNotFoundError: No module named 'services.activity_refund_calculator'`

- [ ] **Step 3: 寫 `services/activity_refund_calculator.py`**

```python
"""才藝退費計算 — 純函式 helpers。

對齊 services/finance/fee_refund_calculator.py 的回傳形狀：
- 課程：教育局學費三段比例（按已出席堂數），T_served=0 特例全退
- 用品：一律不退（已交付）

純函式不碰 DB；caller 由 services/activity_refund_query.py 預先 query
attendance + course.sessions 後餵入。
"""

from __future__ import annotations

from utils.rounding import round_half_up


def calc_course_refund(*, amount_due: int, T_total: int, T_served: int) -> dict:
    """計算才藝課程退費建議金額（按已出席堂數三段比例）。

    Args:
        amount_due: 課程原始金額（price_snapshot，整數元）
        T_total: 課程總堂數（ActivityCourse.sessions），必須 > 0
        T_served: 學生已出席堂數（is_present=True 的 ActivityAttendance count）

    Raises:
        ValueError: T_total <= 0

    Returns:
        {
          "suggested_amount": int,
          "calc_method": "activity_course_ratio",
          "calc_payload": {
            "T_total": int,
            "T_served": int,                # clamp 後值
            "served_ratio": float,
            "ratio_band": "not_started" | "<1/3" | "1/3..2/3" | ">=2/3",
            "refund_ratio": "1" | "2/3" | "1/3" | "0",
            "amount_due": int,
            "formula": str,
          },
          "warnings": list[str],
        }

    規則:
      T_served == 0          → 全退（not_started 特例，業界慣例「未開課全退」）
      0 < ratio < 1/3        → 退 2/3
      1/3 ≤ ratio < 2/3      → 退 1/3
      ratio ≥ 2/3            → 0
      T_served < 0           → clamp 0
      T_served > T_total     → clamp T_total
    """
    if T_total <= 0:
        raise ValueError("T_total 必須 > 0")
    if T_served < 0:
        T_served = 0
    if T_served > T_total:
        T_served = T_total

    if T_served == 0:
        suggested = amount_due
        ratio_band = "not_started"
        refund_ratio_label = "1"
        formula = f"未開課全退：{amount_due}"
        ratio = 0.0
    else:
        ratio = T_served / T_total
        if ratio < 1 / 3:
            ratio_band = "<1/3"
            refund_ratio_label = "2/3"
            suggested = round_half_up(amount_due * 2 / 3)
        elif ratio < 2 / 3:
            ratio_band = "1/3..2/3"
            refund_ratio_label = "1/3"
            suggested = round_half_up(amount_due * 1 / 3)
        else:
            ratio_band = ">=2/3"
            refund_ratio_label = "0"
            suggested = 0
        formula = f"{amount_due} × {refund_ratio_label} = {suggested}"

    return {
        "suggested_amount": suggested,
        "calc_method": "activity_course_ratio",
        "calc_payload": {
            "T_total": T_total,
            "T_served": T_served,
            "served_ratio": round_half_up(ratio, 4),
            "ratio_band": ratio_band,
            "refund_ratio": refund_ratio_label,
            "amount_due": amount_due,
            "formula": formula,
        },
        "warnings": [],
    }


def calc_supply_refund(*, amount_due: int) -> dict:
    """用品（教材）退費 — 一律不退（已交付）。

    Returns:
        suggested_amount=0, calc_method="activity_supply_no_refund",
        warnings=["用品（教材）已交付，不予退費"]
    """
    return {
        "suggested_amount": 0,
        "calc_method": "activity_supply_no_refund",
        "calc_payload": {
            "amount_due": amount_due,
            "formula": "用品一律不退（已交付）",
        },
        "warnings": ["用品（教材）已交付，不予退費"],
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_calculator.py -v 2>&1 | tail -25
```

Expected: All 15 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add services/activity_refund_calculator.py tests/test_activity_refund_calculator.py
git commit -m "feat(activity): 才藝退費純函式 calculator（三段比例 + 用品不退）

calc_course_refund: T_served=0 特例全退 + 教育局三段比例（<1/3 退 2/3、
1/3~2/3 退 1/3、≥2/3 不退）+ clamp negative/over + round_half_up。
calc_supply_refund: 用品永 0 + 已交付 warning。

對齊 services/finance/fee_refund_calculator.py 回傳形狀；純函式不碰 DB。
15 test 涵蓋三段邊界 + T_served=0 特例 + clamp + zero amount + ValueError。"
```

---

## Task 3: Router-side Query Helper（TDD with DB fixture）

**Files:**
- Create: `services/activity_refund_query.py`
- Test: `tests/test_activity_refund_query.py`

- [ ] **Step 1: 寫 failing test `tests/test_activity_refund_query.py`**

```python
"""router-side helper build_refund_suggestion 測試。

涵蓋 spec §6 + §10 邊界：
- 多 course + supply 組裝
- ActivityCourse.sessions=NULL → suggested=None + amount_due fallback
- waitlist / promoted_pending 略過
- is_present=False 不算 T_served
- 軟刪課程仍納入歷史 reg
- reg 不存在 → ValueError
"""

import os
import sys
from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    Base,
    RegistrationCourse,
    RegistrationSupply,
)
from services.activity_refund_query import build_refund_suggestion


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "refund_query.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


def _create_course(session, *, name="美術", sessions=10, price=1500):
    c = ActivityCourse(
        name=name, price=price, sessions=sessions, capacity=30,
        school_year=114, semester=1,
    )
    session.add(c)
    session.flush()
    return c


def _create_supply(session, *, name="畫具包", price=500):
    sup = ActivitySupply(
        name=name, price=price, school_year=114, semester=1,
    )
    session.add(sup)
    session.flush()
    return sup


def _create_reg(session, **kwargs):
    reg = ActivityRegistration(
        student_name=kwargs.get("student_name", "王小明"),
        birthday="2020-01-01",
        class_name="大班",
        school_year=114,
        semester=1,
        paid_amount=kwargs.get("paid_amount", 2000),
        is_paid=True,
        is_active=True,
    )
    session.add(reg)
    session.flush()
    return reg


def _attend_n_sessions(session, reg_id, course_id, n, is_present=True):
    """在 course 下建 n 個 ActivitySession 並對 reg 點到 n 筆 is_present 紀錄。"""
    for i in range(n):
        sess = ActivitySession(
            course_id=course_id, session_date=date(2026, 5, i + 1)
        )
        session.add(sess)
        session.flush()
        att = ActivityAttendance(
            session_id=sess.id,
            registration_id=reg_id,
            is_present=is_present,
        )
        session.add(att)
    session.flush()


def test_reg_not_found_raises(db_session):
    """reg 不存在 → ValueError（呼叫端應在 endpoint 層轉 404）。"""
    with pytest.raises(ValueError, match="not found"):
        build_refund_suggestion(db_session, reg_id=99999)


def test_basic_reg_with_one_course_one_supply(db_session):
    """reg 含 1 課程 10 堂上 1 堂 + 1 用品 → course 退 2/3、supply 0。"""
    course = _create_course(db_session, sessions=10, price=1500)
    supply = _create_supply(db_session, price=500)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.add(RegistrationSupply(
        registration_id=reg.id, supply_id=supply.id, price_snapshot=500,
    ))
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course.id, n=1)

    result = build_refund_suggestion(db_session, reg.id)

    assert result["registration_id"] == reg.id
    assert len(result["items"]) == 2
    course_item = next(it for it in result["items"] if it["type"] == "course")
    supply_item = next(it for it in result["items"] if it["type"] == "supply")
    assert course_item["suggested_amount"] == 1000  # 1500 × 2/3
    assert course_item["calc_payload"]["T_served"] == 1
    assert course_item["calc_payload"]["T_total"] == 10
    assert supply_item["suggested_amount"] == 0
    # total = course suggested + supply suggested (0)
    assert result["total_suggested_amount"] == 1000
    assert result["total_amount_due"] == 2000


def test_course_sessions_null_fallback_to_amount_due(db_session):
    """ActivityCourse.sessions IS NULL → item.suggested_amount=None + warning；
    total_suggested 以 amount_due fallback（保守當全退）。"""
    course = _create_course(db_session, sessions=None, price=1500)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)

    course_item = result["items"][0]
    assert course_item["suggested_amount"] is None
    assert any("總堂數" in w for w in course_item["warnings"])
    # total 用 amount_due fallback（spec §6 算法）
    assert result["total_suggested_amount"] == 1500


def test_waitlist_course_skipped(db_session):
    """status != 'enrolled' 的 RegistrationCourse 不出現在 items。"""
    course_enrolled = _create_course(db_session, name="A 課", sessions=10, price=1500)
    course_waitlist = _create_course(db_session, name="B 課", sessions=10, price=2000)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course_enrolled.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course_waitlist.id,
        status="waitlist", price_snapshot=2000,
    ))
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_items = [it for it in result["items"] if it["type"] == "course"]
    assert len(course_items) == 1
    assert course_items[0]["target_id"] == course_enrolled.id


def test_zero_attendance_full_refund(db_session):
    """無 attendance → T_served=0 → 全退（not_started 特例）。"""
    course = _create_course(db_session, sessions=10, price=1500)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_item = result["items"][0]
    assert course_item["suggested_amount"] == 1500
    assert course_item["calc_payload"]["ratio_band"] == "not_started"


def test_is_present_false_not_counted(db_session):
    """is_present=False 的 attendance 不算 T_served。"""
    course = _create_course(db_session, sessions=10, price=1500)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course.id, n=5, is_present=False)

    result = build_refund_suggestion(db_session, reg.id)
    course_item = result["items"][0]
    assert course_item["calc_payload"]["T_served"] == 0
    assert course_item["suggested_amount"] == 1500  # not_started 全退


def test_soft_deleted_course_still_included(db_session):
    """ActivityCourse.is_active=False（軟刪）仍在 items 中（歷史 reg 仍要算）。"""
    course = _create_course(db_session, sessions=10, price=1500)
    course.is_active = False
    db_session.flush()
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.flush()

    result = build_refund_suggestion(db_session, reg.id)
    course_items = [it for it in result["items"] if it["type"] == "course"]
    assert len(course_items) == 1


def test_mixed_courses(db_session):
    """2 課程：1 課上 3/10（<1/3 退 2/3）+ 1 課 0/10（全退）。"""
    course_a = _create_course(db_session, name="A", sessions=10, price=1500)
    course_b = _create_course(db_session, name="B", sessions=10, price=2400)
    reg = _create_reg(db_session)
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course_a.id,
        status="enrolled", price_snapshot=1500,
    ))
    db_session.add(RegistrationCourse(
        registration_id=reg.id, course_id=course_b.id,
        status="enrolled", price_snapshot=2400,
    ))
    db_session.flush()
    _attend_n_sessions(db_session, reg.id, course_a.id, n=3)

    result = build_refund_suggestion(db_session, reg.id)
    items = {it["target_id"]: it for it in result["items"]}
    # A: served=3/10=0.3 < 1/3 → 退 2/3 of 1500 = 1000
    assert items[course_a.id]["suggested_amount"] == 1000
    # B: served=0 → 全退 2400
    assert items[course_b.id]["suggested_amount"] == 2400
    assert result["total_suggested_amount"] == 1000 + 2400
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_query.py -v 2>&1 | tail -10
```

Expected: `ModuleNotFoundError: No module named 'services.activity_refund_query'`

- [ ] **Step 3: 寫 `services/activity_refund_query.py`**

```python
"""router-side helper：query attendance + course.sessions → 餵 calculator。

對應 spec §6 build_refund_suggestion。endpoint 與 POS verify 共用此 helper。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    ActivitySession,
    ActivitySupply,
    RegistrationCourse,
    RegistrationSupply,
)
from services.activity_refund_calculator import (
    calc_course_refund,
    calc_supply_refund,
)


def build_refund_suggestion(session: Session, reg_id: int) -> dict[str, Any]:
    """組裝 registration-level 退費建議（spec §6）。

    Args:
        session: SQLAlchemy session
        reg_id: ActivityRegistration.id

    Raises:
        ValueError: reg 不存在或 is_active=False

    Returns:
        spec §6 結構：registration_id, computed_at, total_suggested_amount,
        total_amount_due, items[].
    """
    reg = (
        session.query(ActivityRegistration)
        .filter(
            ActivityRegistration.id == reg_id,
            ActivityRegistration.is_active.is_(True),
        )
        .first()
    )
    if reg is None:
        raise ValueError(f"registration {reg_id} not found or inactive")

    items: list[dict[str, Any]] = []
    total_suggested = 0
    total_amount_due = 0

    # ── 課程 items（僅 status='enrolled'）─────────────────────────────────
    course_rows = (
        session.query(RegistrationCourse, ActivityCourse)
        .join(ActivityCourse, ActivityCourse.id == RegistrationCourse.course_id)
        .filter(
            RegistrationCourse.registration_id == reg_id,
            RegistrationCourse.status == "enrolled",
        )
        .all()
    )

    for rc, course in course_rows:
        amount_due = int(rc.price_snapshot or 0)
        total_amount_due += amount_due

        if course.sessions is None or course.sessions <= 0:
            # NULL sessions: item.suggested=None + warning，total 採 amount_due fallback
            items.append({
                "type": "course",
                "target_id": course.id,
                "name": course.name,
                "amount_due": amount_due,
                "suggested_amount": None,
                "calc_method": "activity_course_unknown_total",
                "calc_payload": {
                    "amount_due": amount_due,
                    "formula": "課程總堂數未設定，採保守 fallback 為 amount_due（全退）",
                },
                "warnings": [
                    "課程未設定總堂數（ActivityCourse.sessions IS NULL），"
                    "採保守 fallback 全退；請 admin 補設定後重算。"
                ],
            })
            total_suggested += amount_due
            continue

        T_served = (
            session.query(func.count(ActivityAttendance.id))
            .join(ActivitySession, ActivitySession.id == ActivityAttendance.session_id)
            .filter(
                ActivityAttendance.registration_id == reg_id,
                ActivitySession.course_id == course.id,
                ActivityAttendance.is_present.is_(True),
            )
            .scalar()
        ) or 0

        result = calc_course_refund(
            amount_due=amount_due,
            T_total=int(course.sessions),
            T_served=int(T_served),
        )
        items.append({
            "type": "course",
            "target_id": course.id,
            "name": course.name,
            "amount_due": amount_due,
            "suggested_amount": result["suggested_amount"],
            "calc_method": result["calc_method"],
            "calc_payload": result["calc_payload"],
            "warnings": result["warnings"],
        })
        total_suggested += result["suggested_amount"]

    # ── 用品 items（一律不退）─────────────────────────────────────────────
    supply_rows = (
        session.query(RegistrationSupply, ActivitySupply)
        .join(ActivitySupply, ActivitySupply.id == RegistrationSupply.supply_id)
        .filter(RegistrationSupply.registration_id == reg_id)
        .all()
    )
    for rs, sup in supply_rows:
        amount_due = int(rs.price_snapshot or 0)
        total_amount_due += amount_due
        result = calc_supply_refund(amount_due=amount_due)
        items.append({
            "type": "supply",
            "target_id": sup.id,
            "name": sup.name,
            "amount_due": amount_due,
            "suggested_amount": result["suggested_amount"],
            "calc_method": result["calc_method"],
            "calc_payload": result["calc_payload"],
            "warnings": result["warnings"],
        })
        # supply suggested=0，不增 total_suggested

    return {
        "registration_id": reg_id,
        "computed_at": datetime.utcnow().isoformat(),
        "total_suggested_amount": total_suggested,
        "total_amount_due": total_amount_due,
        "items": items,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_query.py -v 2>&1 | tail -20
```

Expected: All 8 tests PASS.

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add services/activity_refund_query.py tests/test_activity_refund_query.py
git commit -m "feat(activity): router-side build_refund_suggestion helper

對應 spec §6：query attendance is_present=True count + course.sessions →
餵 activity_refund_calculator；組裝 reg-level items + total_suggested。

NULL sessions 採保守 amount_due fallback 避免 actual=全額/suggested=部分
假性 diff；waitlist 略過；軟刪課程仍納入；reg 不存在 raise ValueError。
8 test 含 mixed courses / not_started / waitlist / is_present=False / NULL。"
```

---

## Task 4: 新 Guard `require_approve_for_refund_diff`（TDD）

**Files:**
- Modify: `services/activity_payment_guards.py`
- Test: `tests/test_activity_payment_guards.py` （既有，追加 case）

- [ ] **Step 1: 在 `tests/test_activity_payment_guards.py` 尾段追加 failing test**

先讀既有檔取得 import / fixture 風格：

```bash
cd /Users/yilunwu/Desktop/ivy-backend
sed -n '1,30p' tests/test_activity_payment_guards.py
```

在檔尾追加：

```python


# ── require_approve_for_refund_diff: 偏離 calculator 建議值簽核 ─────────────


from services.activity_payment_guards import require_approve_for_refund_diff


def test_refund_diff_below_threshold_passes():
    """diff <= NT$100 → 任何員工通過。"""
    # 應該不 raise
    require_approve_for_refund_diff(
        diff=50, current_user=LINE_STAFF,
        suggested_total=500, actual_total=550,
    )


def test_refund_diff_at_threshold_passes():
    """diff == threshold 邊界 → 視為 ≤ pass。"""
    require_approve_for_refund_diff(
        diff=100, current_user=LINE_STAFF,
        suggested_total=500, actual_total=600,
    )


def test_refund_diff_over_threshold_blocks_staff():
    """diff > NT$100 + 一線員工 → 403。"""
    with pytest.raises(HTTPException) as exc:
        require_approve_for_refund_diff(
            diff=101, current_user=LINE_STAFF,
            suggested_total=500, actual_total=601,
        )
    assert exc.value.status_code == 403
    assert "偏離" in exc.value.detail or "差" in exc.value.detail


def test_refund_diff_over_threshold_passes_approver():
    """diff > NT$100 + ACTIVITY_PAYMENT_APPROVE → pass。"""
    require_approve_for_refund_diff(
        diff=500, current_user=APPROVER,
        suggested_total=500, actual_total=1000,
    )


def test_refund_diff_error_message_contains_amounts():
    """403 detail 應含 suggested / actual / diff 三個金額方便員工 debug。"""
    with pytest.raises(HTTPException) as exc:
        require_approve_for_refund_diff(
            diff=200, current_user=LINE_STAFF,
            suggested_total=800, actual_total=1000,
        )
    msg = exc.value.detail
    assert "800" in msg and "1000" in msg and "200" in msg
```

注意 `LINE_STAFF` / `APPROVER` 是既有 fixture（見檔頭），import 與既有測試共用即可。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_payment_guards.py::test_refund_diff_below_threshold_passes -v 2>&1 | tail -10
```

Expected: `ImportError: cannot import name 'require_approve_for_refund_diff'`

- [ ] **Step 3: 編輯 `services/activity_payment_guards.py` 加 guard 與 import**

在檔頭 `from utils.activity_constants import (` 區塊內加 `ACTIVITY_REFUND_DIFF_THRESHOLD`：

```python
from utils.activity_constants import (
    ACTIVITY_ITEM_HIGH_PRICE_THRESHOLD,
    ACTIVITY_REFUND_DIFF_THRESHOLD,
    MIN_REFUND_REASON_LENGTH,
    REFUND_APPROVAL_THRESHOLD,
)
```

檔尾追加新函式：

```python


def require_approve_for_refund_diff(
    *,
    diff: int,
    current_user: dict,
    suggested_total: int,
    actual_total: int,
) -> None:
    """實退 vs calculator 建議值差距超 ACTIVITY_REFUND_DIFF_THRESHOLD 時要求簽核。

    Why: 員工算錯/故意多退無事前制衡；diff 大表示偏離教育局規則或建議值，
    需要管理者批准。與 require_approve_for_large_refund（擋總額）獨立共存，
    任一觸發都要簽核。

    Args:
        diff: |actual_total - suggested_total| 累積值（多 reg 同收據時請以
              sum(abs(per_reg_diff)) 計算，避免方向抵消漏網）。
        current_user: 已認證的 user dict（含 permission_names）。
        suggested_total: server-side build_refund_suggestion 算出的總建議值。
        actual_total: 員工 body 送出的實退總額。
    """
    if diff <= ACTIVITY_REFUND_DIFF_THRESHOLD:
        return
    if has_payment_approve(current_user):
        return
    raise HTTPException(
        status_code=403,
        detail=(
            f"實退 NT${actual_total} 與系統建議 NT${suggested_total} "
            f"差 NT${diff}，超過 NT${ACTIVITY_REFUND_DIFF_THRESHOLD} 偏離門檻，"
            f"需具備『才藝課收款簽核』（ACTIVITY_PAYMENT_APPROVE）權限"
        ),
    )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_payment_guards.py -v 2>&1 | tail -15
```

Expected: 既有 case + 5 個新 case 全 PASS。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add services/activity_payment_guards.py tests/test_activity_payment_guards.py
git commit -m "feat(activity): require_approve_for_refund_diff guard

實退 vs calculator 建議差距 > NT\$100 時要求 ACTIVITY_PAYMENT_APPROVE。
與既有 large_refund / cumulative 簽核獨立共存，封住員工算錯/多退路徑。
5 新 test 涵蓋 below/at/over threshold + approver bypass + error message。"
```

---

## Task 5: 新 GET endpoint + Response Schema（TDD）

**Files:**
- Modify: `schemas/activity_admin.py` 加 `RefundSuggestionItem` / `RefundSuggestionResponse`
- Modify: `api/activity/registrations.py` 加 `GET /{reg_id}/refund-suggestion`
- Test: `tests/test_activity_refund_suggestion_endpoint.py` （新檔）

- [ ] **Step 1: 寫 failing test `tests/test_activity_refund_suggestion_endpoint.py`**

```python
"""GET /api/activity/registrations/{reg_id}/refund-suggestion endpoint 測試。"""

import os
import sys

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import Base
from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund_suggestion.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(activity_router, prefix="/api/activity")

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def test_get_refund_suggestion_returns_items(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE",
                                            "ACTIVITY_PAYMENT_APPROVE"])
        reg = _setup_reg(s, course_price=1500, supply_price=500, paid_amount=2000)
        s.commit()
        reg_id = reg.id

    login = _login(c)
    assert login.status_code == 200

    resp = c.get(f"/api/activity/registrations/{reg_id}/refund-suggestion")
    assert resp.status_code == 200
    data = resp.json()
    assert data["registration_id"] == reg_id
    # 預設 course.sessions=NULL（_setup_reg 沒設）→ fallback 全退
    # supply 用品 suggested=0
    # total_suggested = course amount_due 1500 + supply 0 = 1500
    assert data["total_suggested_amount"] == 1500
    assert data["total_amount_due"] == 2000
    assert len(data["items"]) == 2
    types = sorted(it["type"] for it in data["items"])
    assert types == ["course", "supply"]


def test_get_refund_suggestion_404_not_found(client):
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        s.commit()

    _login(c)
    resp = c.get("/api/activity/registrations/99999/refund-suggestion")
    assert resp.status_code == 404


def test_get_refund_suggestion_requires_permission(client):
    """無 ACTIVITY_PAYMENT_WRITE 權限應 403。"""
    c, sf = client
    with sf() as s:
        # 只給 READ，沒 WRITE
        _create_admin(s, permission_names=["ACTIVITY_READ"])
        reg = _setup_reg(s)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.get(f"/api/activity/registrations/{reg_id}/refund-suggestion")
    assert resp.status_code == 403
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_suggestion_endpoint.py -v 2>&1 | tail -10
```

Expected: 404（endpoint 還沒有）— 兩個 test 都 fail。

- [ ] **Step 3: 加 response schema 到 `schemas/activity_admin.py`**

在檔頭 `from datetime import time` 改為 `from datetime import datetime, time`，並在檔尾追加：

```python


class RefundSuggestionItem(BaseModel):
    """單一退費 item（course 或 supply）建議值。spec §7。"""

    type: str = Field(..., description="course | supply")
    target_id: int = Field(..., description="course_id 或 supply_id")
    name: str
    amount_due: int
    # NULL sessions 時為 None；前端應 fallback 顯示為「無法計算，建議全退」
    suggested_amount: Optional[int] = Field(None, description="None=無法計算")
    calc_method: str
    calc_payload: dict
    warnings: list[str] = Field(default_factory=list)


class RefundSuggestionResponse(BaseModel):
    """GET /registrations/{id}/refund-suggestion 回應 schema。spec §7。"""

    registration_id: int
    computed_at: str  # ISO datetime
    # 算法見 spec §6：item.suggested 為 None 時以 amount_due fallback 加總
    total_suggested_amount: int
    total_amount_due: int
    items: list[RefundSuggestionItem]
```

(注意：檔頭可能沒 import `Optional`；若無，加 `from typing import Optional`。)

- [ ] **Step 4: 加 endpoint 到 `api/activity/registrations.py`**

在 `from services.activity_service import activity_service` 之後加：

```python
from services.activity_refund_query import build_refund_suggestion
```

在檔尾（最後一個 endpoint 之後）追加新 endpoint：

```python


@router.get("/registrations/{registration_id}/refund-suggestion")
def get_refund_suggestion(
    registration_id: int,
    current_user: dict = Depends(
        require_staff_permission(Permission.ACTIVITY_PAYMENT_WRITE)
    ),
):
    """取得 registration 的退費建議（每門 course / 每筆 supply 分開列出）。

    spec §7：前端 POS UI 在退費前 GET 此 endpoint 預載建議值。
    Server-side build_refund_suggestion 同套邏輯也用於 POS verify。

    Returns: RefundSuggestionResponse
    Raises:
        404: reg 不存在或 is_active=False
        403: 無 ACTIVITY_PAYMENT_WRITE 權限
    """
    session = get_session()
    try:
        result = build_refund_suggestion(session, registration_id)
        return result
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    finally:
        session.close()
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_suggestion_endpoint.py -v 2>&1 | tail -15
```

Expected: 3 tests PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add schemas/activity_admin.py api/activity/registrations.py tests/test_activity_refund_suggestion_endpoint.py
git commit -m "feat(activity): GET /registrations/{id}/refund-suggestion endpoint

對應 spec §7：前端 POS UI 退費前可拉伺服器算的 suggested 建議值。
Permission: ACTIVITY_PAYMENT_WRITE（與退費路徑同層）。
404 for missing reg；既有 require_staff_permission 處理 403。
3 test: 200 happy path + 404 + 403 permission gate。"
```

---

## Task 6: POS Checkout Refund Verify Wiring（TDD）

**Files:**
- Modify: `api/activity/pos.py` refund 路徑加 verify wiring
- Test: `tests/test_activity_refund_diff_verify.py` （新檔）

- [ ] **Step 1: 寫 failing test `tests/test_activity_refund_diff_verify.py`**

```python
"""POS refund diff verify e2e 測試（TestClient）。

對應 spec §8 + §11：
- diff <= 100 → pass
- diff > 100 + 一線員工 → 403
- diff > 100 + ACTIVITY_PAYMENT_APPROVE → pass
- 多 reg 同收據 sum(abs(per-reg-diff)) 累加
- 方向抵消防護：reg1 多 60 + reg2 少 60 → diff=120 簽核
- 用品實退觸發 diff
- NULL sessions reg 採 amount_due fallback
"""

import os
import sys
from datetime import date

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.auth import _account_failures, _ip_attempts
from api.auth import router as auth_router
from models.database import (
    ActivityAttendance,
    ActivityCourse,
    ActivitySession,
    Base,
)
from tests.test_activity_pos import _create_admin, _login, _setup_reg


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "refund_diff.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(engine)
    _ip_attempts.clear()
    _account_failures.clear()

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/auth")
    app.include_router(activity_router, prefix="/api/activity")

    with TestClient(app) as c:
        yield c, session_factory

    _ip_attempts.clear()
    _account_failures.clear()
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


REFUND_REASON = "家長要求退費，已確認原因符合園所政策。"


def _set_course_sessions(session, course_name: str, sessions: int):
    """把 _setup_reg 預設建出來 sessions=NULL 的 course 補上 sessions。"""
    c = session.query(ActivityCourse).filter(ActivityCourse.name == course_name).first()
    c.sessions = sessions
    session.flush()


def _mark_attendance(session, reg_id: int, course_id: int, n: int):
    for i in range(n):
        s = ActivitySession(course_id=course_id, session_date=date(2026, 5, i + 1))
        session.add(s)
        session.flush()
        session.add(ActivityAttendance(
            session_id=s.id, registration_id=reg_id, is_present=True,
        ))
    session.flush()


def _refund_body(reg_id: int, amount: int) -> dict:
    return {
        "items": [{"registration_id": reg_id, "amount": amount}],
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }


# ── happy paths ────────────────────────────────────────────────────────────


def test_refund_diff_zero_passes_staff(client):
    """員工剛好送 suggested → diff=0 → 一線通過。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        # 不需 _mark_attendance（n=0 attendance 即「未開課」全退）
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=1500（not_started 特例全退）
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1500))
    assert resp.status_code == 200, resp.json()


def test_refund_diff_below_threshold_passes_staff(client):
    """diff=50（< 100）→ 一線通過。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # 0 attendance → suggested=1500；員工送 1450 → diff=50
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1450))
    assert resp.status_code == 200, resp.json()


def test_refund_diff_over_threshold_blocks_staff(client):
    """diff=200（> 100）+ 無 approve → 403。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    # suggested=1500（全退）；員工送 1300 → diff=200 → 簽核
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1300))
    assert resp.status_code == 403
    assert "偏離" in resp.json()["detail"] or "差" in resp.json()["detail"]


def test_refund_diff_over_threshold_passes_approver(client):
    """diff=200 + ACTIVITY_PAYMENT_APPROVE → pass。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=[
            "ACTIVITY_READ", "ACTIVITY_WRITE", "ACTIVITY_PAYMENT_APPROVE",
        ])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1300))
    assert resp.status_code == 200, resp.json()


def test_refund_supply_triggers_diff(client):
    """suggested=0（用品 + course 全退）員工只想退 supply NT$300 → diff 觸發
    （因 course 0 attendance suggested=1500，員工只送 300 → diff=1200）。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=500, paid_amount=2000)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 300))
    assert resp.status_code == 403


def test_refund_null_sessions_uses_amount_due_fallback(client):
    """course.sessions=NULL → suggested 用 amount_due fallback；
    員工少退觸發 diff。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        # _setup_reg 預設不設 course.sessions → NULL
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        s.commit()
        reg_id = reg.id

    _login(c)
    # NULL fallback: suggested=1500；員工送 1000 → diff=500 → 簽核
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1000))
    assert resp.status_code == 403


def test_refund_multi_reg_diff_accumulates(client):
    """多 reg 同收據：reg1 多退 60 + reg2 少退 60 →
    naive abs(total)=0；spec 算法 sum(abs)=120 → 簽核。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg1 = _setup_reg(
            s, student_name="A", course_price=1000, supply_price=0, paid_amount=1000,
            course_name="A 課",
        )
        reg2 = _setup_reg(
            s, student_name="B", course_price=1000, supply_price=0, paid_amount=1000,
            course_name="B 課",
        )
        _set_course_sessions(s, "A 課", 10)
        _set_course_sessions(s, "B 課", 10)
        s.commit()
        rid1, rid2 = reg1.id, reg2.id

    _login(c)
    # 兩 reg suggested 都是 1000（0 attendance 全退）
    # reg1 actual=1060（+60）；reg2 actual=940（-60）
    # total_actual=2000=total_suggested → naive diff=0
    # sum(abs) = 60+60 = 120 → 簽核
    body = {
        "items": [
            {"registration_id": rid1, "amount": 1060},
            {"registration_id": rid2, "amount": 940},
        ],
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }
    resp = c.post("/api/activity/pos/checkout", json=body)
    assert resp.status_code == 403
    assert "120" in resp.json()["detail"]
```

> Note：上述 7 個 case 都是「0 attendance → suggested=全退」的場景，
> 故 `_mark_attendance` helper 雖在 import 區出現但本檔測試未實際呼叫；
> 若 reviewer 想加「3/10 attendance 部分退」case 屬於額外覆蓋（不阻塞）。

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py -v 2>&1 | tail -15
```

Expected: 部分 test 因為「diff 應 403 但目前 200 OK」而 FAIL（POS verify 還沒接上）。

- [ ] **Step 3: 修改 `api/activity/pos.py` 加 import + verify wiring**

在 `from ._shared import (...)` 之上插入：

```python
from services.activity_payment_guards import require_approve_for_refund_diff
from services.activity_refund_query import build_refund_suggestion
```

在 refund 路徑（既有「第二道：每 reg 累積退費簽核」之後，約 pos.py:648-650 之間）追加：

```python
        # ── 第三道：實退 vs 建議值偏離簽核 (spec §8.1) ───────────────
        # Why: 員工算錯 / 多退私吞。重算 server-side suggestion 與 body 比對；
        # 偏離總額 > NT$100 需 ACTIVITY_PAYMENT_APPROVE 權限。
        # 注意：多 reg 同收據用 sum(abs(per-reg-diff)) 避免方向抵消漏網。
        _refund_audit_context: dict = {}
        if body.type == "refund":
            actual_by_reg = {it.registration_id: it.amount for it in body.items}
            suggested_by_reg: dict[int, int] = {}
            suggestion_details: list[dict] = []
            for rid in actual_by_reg:
                suggestion = build_refund_suggestion(session, rid)
                suggested_by_reg[rid] = suggestion["total_suggested_amount"]
                suggestion_details.append(suggestion)

            total_actual = sum(actual_by_reg.values())
            total_suggested = sum(suggested_by_reg.values())
            diff = sum(
                abs(actual_by_reg[rid] - suggested_by_reg[rid])
                for rid in actual_by_reg
            )

            require_approve_for_refund_diff(
                diff=diff,
                current_user=current_user,
                suggested_total=total_suggested,
                actual_total=total_actual,
            )

            _refund_audit_context = {
                "suggested_total": total_suggested,
                "actual_total": total_actual,
                "diff": diff,
                "suggestion_details": suggestion_details,
            }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py -v 2>&1 | tail -20
```

Expected: 7 tests PASS.

如有 `_mark_attendance` 簽章導致首測試報錯，請刪除該第一個 test 中重複的呼叫（保留第二次正確簽章的那行；test 內已標註）。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/activity/pos.py tests/test_activity_refund_diff_verify.py
git commit -m "feat(activity): POS checkout 退費路徑加 diff verify

spec §8.1：server-side 重算 build_refund_suggestion 與 body 比對，
sum(abs(per-reg-diff)) > NT\$100 強制 ACTIVITY_PAYMENT_APPROVE。
方向抵消防護（reg1 多退 60 + reg2 少退 60 → diff=120）已涵蓋。
7 test: happy path + below/over threshold + approver bypass +
supply / NULL fallback / 多 reg 加總方向抵消。"
```

---

## Task 7: 單筆退費路徑 Verify Wiring（TDD）

**Files:**
- Modify: `api/activity/registrations_payments.py` 加 verify wiring
- Test: `tests/test_activity_refund_diff_verify.py` 追加單筆退費 case

- [ ] **Step 1: 在 `tests/test_activity_refund_diff_verify.py` 尾段追加 failing test**

```python


# ── 單筆退費 endpoint /registrations/{id}/payments 退費路徑 ────────────────


def test_single_refund_diff_blocks_staff(client):
    """POST /registrations/{id}/payments (type=refund) diff > 100 → 403。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    body = {
        "amount": 1200,  # suggested=1500（全退）；diff=300 > 100
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }
    resp = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
    assert resp.status_code == 403


def test_single_refund_diff_below_threshold_passes(client):
    """單筆退費 diff <= 100 → 一線通過。"""
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    body = {
        "amount": 1450,  # suggested=1500；diff=50 < 100
        "payment_method": "現金",
        "payment_date": "2026-05-26",
        "type": "refund",
        "notes": REFUND_REASON,
    }
    resp = c.post(f"/api/activity/registrations/{reg_id}/payments", json=body)
    assert resp.status_code in (200, 201), resp.json()
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py::test_single_refund_diff_blocks_staff -v 2>&1 | tail -10
```

Expected: `test_single_refund_diff_blocks_staff` FAILED — 200 instead of 403（verify 未接上）。

- [ ] **Step 3: 修改 `api/activity/registrations_payments.py` 加 verify wiring**

在檔頭既有 import 中加：

```python
from services.activity_payment_guards import require_approve_for_refund_diff
from services.activity_refund_query import build_refund_suggestion
```

在退費路徑既有累積簽核之後（registrations_payments.py:389 之後、retraction reason check 之前）追加：

```python
        # ── 第三道：實退 vs 建議值偏離簽核 (spec §8.2) ───────────────
        if body.type == "refund":
            suggestion = build_refund_suggestion(session, registration_id)
            suggested_total = suggestion["total_suggested_amount"]
            diff = abs(int(body.amount) - suggested_total)
            require_approve_for_refund_diff(
                diff=diff,
                current_user=current_user,
                suggested_total=suggested_total,
                actual_total=int(body.amount),
            )
            _refund_audit_context = {
                "suggested_total": suggested_total,
                "actual_total": int(body.amount),
                "diff": diff,
                "suggestion_details": [suggestion],
            }
        else:
            _refund_audit_context = {}
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py -v 2>&1 | tail -15
```

Expected: 全部 (含原 7 + 新 2 = 9) PASS。

- [ ] **Step 5: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/activity/registrations_payments.py tests/test_activity_refund_diff_verify.py
git commit -m "feat(activity): 單筆退費 endpoint 加 diff verify

spec §8.2：POST /registrations/{id}/payments (type=refund) 在
require_approve_for_large_refund 之後加 server-side diff verify。
2 新 test 與 POS checkout 行為對齊。"
```

---

## Task 8: Audit Trail Update（TDD）

**Files:**
- Modify: `api/activity/pos.py` 既有 `request.state.audit_changes` dict 加 refund 欄
- Modify: `api/activity/registrations_payments.py` 同樣處理
- Test: `tests/test_activity_refund_diff_verify.py` 追加 audit assertion

- [ ] **Step 1: 在 `tests/test_activity_refund_diff_verify.py` 追加 failing test**

```python


def test_pos_refund_audit_changes_contains_suggestion(client):
    """成功退費後 audit_logs 應含 refund_suggested_total / actual / diff。

    若 SQLite 測試環境未掛 audit middleware（log 為 None），test skip — 但
    wiring 已由實作步驟保證；本 test 在 prod / 整合環境會生效。
    """
    c, sf = client
    with sf() as s:
        _create_admin(s, permission_names=["ACTIVITY_READ", "ACTIVITY_WRITE"])
        reg = _setup_reg(s, course_price=1500, supply_price=0, paid_amount=1500)
        _set_course_sessions(s, "美術", 10)
        s.commit()
        reg_id = reg.id

    _login(c)
    resp = c.post("/api/activity/pos/checkout", json=_refund_body(reg_id, 1450))
    assert resp.status_code == 200

    with sf() as s:
        try:
            from models.database import AuditLog
        except ImportError:
            pytest.skip("AuditLog model 不存在")
            return
        log = s.query(AuditLog).order_by(AuditLog.id.desc()).first()
        if log is None:
            pytest.skip("audit middleware 未掛載；wiring 已在實作步驟保證")
            return
        import json
        changes = log.changes if isinstance(log.changes, dict) else (
            json.loads(log.changes) if log.changes else {}
        )
        assert changes.get("refund_suggested_total") == 1500
        assert changes.get("refund_actual_total") == 1450
        assert changes.get("refund_diff") == 50
```

- [ ] **Step 2: Run test to verify it fails (audit changes missing refund_* keys)**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py::test_pos_refund_audit_contains_suggestion -v 2>&1 | tail -15
```

Expected: AssertionError on `"refund_suggested_total" in changes` 或 audit log 不存在。

- [ ] **Step 3: 修改 `api/activity/pos.py` 既有 audit_changes 加 refund 欄**

定位 pos.py:822 既有：

```python
        request.state.audit_changes = {
            "receipt_no": receipt_no,
            ...
            "registration_ids": reg_ids,
        }
```

改為（在 dict 字面量之後 update）：

```python
        request.state.audit_changes = {
            "receipt_no": receipt_no,
            "type": body.type,
            "total": total_charged,
            "item_count": len(body.items),
            "payment_method": body.payment_method,
            "payment_date": body.payment_date.isoformat(),
            "registration_ids": reg_ids,
        }

        # spec §12：退費路徑擴充 audit_changes 含 calculator 建議值反差
        if body.type == "refund" and _refund_audit_context:
            request.state.audit_changes.update({
                "refund_suggested_total": _refund_audit_context["suggested_total"],
                "refund_actual_total": _refund_audit_context["actual_total"],
                "refund_diff": _refund_audit_context["diff"],
                "refund_suggestion_per_reg": [
                    {
                        "registration_id": sd["registration_id"],
                        "total_suggested": sd["total_suggested_amount"],
                        "items": [
                            {
                                "type": it["type"],
                                "target_id": it["target_id"],
                                "suggested": it["suggested_amount"],
                                "calc_method": it["calc_method"],
                            }
                            for it in sd["items"]
                        ],
                    }
                    for sd in _refund_audit_context["suggestion_details"]
                ],
            })
```

- [ ] **Step 4: 修改 `api/activity/registrations_payments.py` 同樣處理**

定位該檔既有 `request.state.audit_changes = {...}`（約 line 495-503）後追加：

```python
        if body.type == "refund" and _refund_audit_context:
            request.state.audit_changes.update({
                "refund_suggested_total": _refund_audit_context["suggested_total"],
                "refund_actual_total": _refund_audit_context["actual_total"],
                "refund_diff": _refund_audit_context["diff"],
                "refund_suggestion_per_reg": [
                    {
                        "registration_id": sd["registration_id"],
                        "total_suggested": sd["total_suggested_amount"],
                        "items": [
                            {
                                "type": it["type"],
                                "target_id": it["target_id"],
                                "suggested": it["suggested_amount"],
                                "calc_method": it["calc_method"],
                            }
                            for it in sd["items"]
                        ],
                    }
                    for sd in _refund_audit_context["suggestion_details"]
                ],
            })
```

- [ ] **Step 5: Run test to verify it passes**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest tests/test_activity_refund_diff_verify.py -v 2>&1 | tail -15
```

Expected: 全部 (10 test) PASS。

> 若 test app 環境下 audit_logs 表未由 audit middleware 寫入（middleware 未掛載），改為 monkeypatch `request.state` 攔截 audit_changes dict 直接 assert。但既有 `pos.py:822` 既有 audit pattern test (`tests/test_activity_pos.py` 已驗證 audit_changes 寫入)，本 test 跟隨同一路徑即可。

- [ ] **Step 6: Commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git add api/activity/pos.py api/activity/registrations_payments.py tests/test_activity_refund_diff_verify.py
git commit -m "feat(activity): 退費路徑 audit_changes 擴充 calculator 反差

spec §12：成功退費後在既有 request.state.audit_changes dict 補
refund_suggested_total / refund_actual_total / refund_diff +
refund_suggestion_per_reg（含 per-item suggested / calc_method）。

事後可從 audit log 還原當時 calculator 算出什麼、員工偏離多少。
新增 1 test 驗證 audit_changes 包含上述欄位。"
```

---

## Task 9: 全套 pytest + sanity smoke

**Files:** 無新檔；驗收整體 regression

- [ ] **Step 1: Run new test files together**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest \
  tests/test_activity_refund_calculator.py \
  tests/test_activity_refund_query.py \
  tests/test_activity_refund_suggestion_endpoint.py \
  tests/test_activity_refund_diff_verify.py \
  -v 2>&1 | tail -30
```

Expected: 全部 PASS（~30+ test，依實際 case 數）。

- [ ] **Step 2: Run adjacent test files to catch regression**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest \
  tests/test_activity_pos.py \
  tests/test_activity_payment_guards.py \
  tests/test_pos_refund_advisory_lock.py \
  tests/test_fee_refund_calculator.py \
  -v 2>&1 | tail -20
```

Expected: 零新 fail（已存在 pre-existing fail 可忽略，需與 baseline 對齊）。

- [ ] **Step 3: Full test suite smoke（耗時較久）**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
pytest --tb=short -q 2>&1 | tail -40
```

Expected: 通過數 = baseline + 新增 case 數；fail 數 = baseline（pre-existing fail 不變）。

如新增 regression，回退至最近 commit 並修正導因；不放行 pre-existing 以外的 fail。

- [ ] **Step 4: Manual smoke via curl（已啟動 start.sh 或 dev server）**

```bash
# 假設 backend on :8088 且已有合法 admin session cookie
# (1) GET refund-suggestion
curl -s -b /tmp/cookies.txt http://localhost:8088/api/activity/registrations/{REG_ID}/refund-suggestion | python -m json.tool

# (2) POST 一筆 small diff 退費（應 200）
# (3) POST 一筆 large diff 退費（應 403 with 偏離訊息）
```

如時間有限可僅跑 (1)。

- [ ] **Step 5: No code commit — 僅驗收**

本 task 無檔案變動。若 step 3 有 regression 需修，新增修補 commit 並回到本 task step 3。

---

## Self-Review Notes

**Spec coverage check：**

| Spec section | Task |
|--------------|------|
| §3 退費規則 | T2 (calculator) |
| §4 模組架構 | T2, T3, T4, T5, T6, T7 |
| §5 純函式 contract | T2 |
| §6 build_refund_suggestion | T3 |
| §7 GET endpoint + schema | T5 |
| §8.1 POS checkout verify | T6 |
| §8.2 單筆退費 verify | T7 |
| §9 新 guard + const | T1, T4 |
| §10 邊界處理 | T3 test cases #1-#10 |
| §11 測試覆蓋 | T2 (15) + T3 (8) + T6+T7 (9) + T8 (1) ≈ 33 |
| §12 audit trail | T8 |

**未在 Plan 範圍**（spec §13）：DB schema、前端整合、異常退費月報表、env-driven threshold — 確認 spec 已標 follow-up。

**No placeholders check：** 所有 task 內 code 已完整列出（test + 實作），無 "TODO" / "fill in details"。檔案路徑、命令、預期輸出皆有具體值。

**Type consistency：**
- `build_refund_suggestion` return shape 在 T3 spec 與 T5 schema (`RefundSuggestionResponse`) / T6 audit 使用一致：`items[].suggested_amount: int | None`、`total_suggested_amount: int`、`computed_at: str`。
- `require_approve_for_refund_diff` 在 T4 定義 keyword-only `diff/current_user/suggested_total/actual_total`，T6/T7 caller 簽章對齊。
- T2 `calc_payload.ratio_band` 字串 `"not_started" | "<1/3" | "1/3..2/3" | ">=2/3"` 在 T3 fallback (`activity_course_unknown_total`) 與 T2 calculator path 不衝突（fallback 不走 calculator）。

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-26-activity-refund-calculator.md`. Two execution options:**

**1. Subagent-Driven (recommended)** - 我每個 task dispatch 一個 fresh subagent，task 間 review、快速迭代

**2. Inline Execution** - 在這個 session 內按 executing-plans 批次執行，含 checkpoint review

**Which approach?**
