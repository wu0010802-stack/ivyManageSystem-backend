# 考核模組對齊人事規章第六篇 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 考核獎金率與計分規則全面對齊人事規章第六篇（115.01.01），新增九個計分項目與 MANUAL_DELTA 規則型別，生效自 2026-02-01（114下），歷史學期結果不變。

**Architecture:** 規則引擎（`services/appraisal/rule_applier.py`）是純函式＋DB config（`appraisal_scoring_rules` 以 effective_from 選版）。本計畫：①擴充 enum 與兩個聚合來源（曠職、復學）②新增 MANUAL_DELTA 純函式與分流 ③一支 migration 改獎金率五值＋插入 `2026-02-01` 全套 24 條規則 ④API 範圍驗證 ⑤前端手填項與標籤 ⑥規章修訂建議書。既有 `2025-08-01` 規則列完全不動，114上 重算結果不變。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、pytest（SQLite in-memory 慣例）、Vue 3 + TS、Vitest

**Spec:** `docs/superpowers/specs/2026-06-11-appraisal-regulation-alignment-design.md`（含 §14 計畫階段修訂，為本計畫的權威依據）

**重要既知事實（implementer 必讀）：**
- `appraisal_scoring_rules.item_code` 是 `String(40)`；`ScoreItemCode` 是純 Python str enum，**無 PG enum**。
- `MANUAL_ITEM_CODES = frozenset(set(ScoreItemCode) - AUTO_ITEM_CODES)`（models/appraisal.py）——新 code 不加進 AUTO 就自動歸 manual。
- `AppraisalManualEventCount.count` 是 `Numeric(8,2)`，可存 0.5 時數或負分值。
- 後端有 PostToolUse black hook：.py 檔 Edit 後會自動 format，正常現象。
- 金額/分數一律 `Decimal`＋`ROUND_HALF_UP`（rule_applier `_round`）；禁用 builtin `round()` 於金額。
- pytest 跑 SQLite；migration 需另對本機 PG dev DB `alembic upgrade heads` 實機驗證。
- commit 訊息一律繁體中文 Conventional Commits。

---

### Task 1: 建分支與基線驗證

**Files:**
- 無程式碼變更（worktree／分支／spec commit）

- [ ] **Step 1: 從 origin/main 建 worktree**

```bash
cd /Users/yilunwu/Desktop/ivy-backend
git fetch origin main
git worktree add .worktrees/appraisal-reg-align -b feat/appraisal-regulation-align-2026-06-11-be origin/main
cd .worktrees/appraisal-reg-align
git branch --show-current
```
Expected: `feat/appraisal-regulation-align-2026-06-11-be`

- [ ] **Step 2: 驗證 alembic 單 head**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align && python3 -m alembic heads
```
Expected: 單一 head `yebnd01`。若不是（origin/main 已前進），記下實際 head，Task 9 的 `down_revision` 改用它。

- [ ] **Step 3: 收進 spec 並 commit**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align
cp ../../docs/superpowers/specs/2026-06-11-appraisal-regulation-alignment-design.md docs/superpowers/specs/
git add docs/superpowers/specs/2026-06-11-appraisal-regulation-alignment-design.md
git commit -m "docs(appraisal): 規章第六篇對齊設計 spec"
```

- [ ] **Step 4: 基線測試（記錄起點）**

```bash
python3 -m pytest tests/test_appraisal_rule_applier.py tests/test_appraisal_engine.py tests/test_appraisal_excel_reconcile_114.py -q
```
Expected: 全綠。若有紅，停下回報（基線即紅不可開工）。

---

### Task 2: ScoreItemCode 擴充九個新項目

**Files:**
- Modify: `models/appraisal.py`（ScoreItemCode enum 與 AUTO_ITEM_CODES）
- Test: `tests/test_appraisal_regulation_align.py`（新檔）

- [ ] **Step 1: 寫失敗測試**

```python
"""tests/test_appraisal_regulation_align.py — 規章第六篇對齊（spec 2026-06-11）"""
from decimal import Decimal

from models.appraisal import AUTO_ITEM_CODES, MANUAL_ITEM_CODES, ScoreItemCode


NEW_CODES = {
    "ABSENTEEISM",
    "STUDENT_WITHDRAWAL",
    "STUDENT_REINSTATE",
    "TRIAL_LEAVE",
    "CLASS_TRANSFER",
    "EXAM_RESULT",
    "RECRUIT_SCORE",
    "SUPERVISOR_SCORE",
    "EXCELLENCE_NOMINATION",
}


def test_score_item_code_新增九項():
    assert NEW_CODES <= {c.value for c in ScoreItemCode}


def test_auto_manual_歸類():
    assert ScoreItemCode.ABSENTEEISM in AUTO_ITEM_CODES
    assert ScoreItemCode.STUDENT_REINSTATE in AUTO_ITEM_CODES
    # 休學降級手填（spec §14.3）；其餘新項皆手填
    for code in ("STUDENT_WITHDRAWAL", "TRIAL_LEAVE", "CLASS_TRANSFER",
                 "EXAM_RESULT", "RECRUIT_SCORE", "SUPERVISOR_SCORE",
                 "EXCELLENCE_NOMINATION"):
        assert ScoreItemCode(code) in MANUAL_ITEM_CODES
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -q
```
Expected: FAIL（`'ABSENTEEISM' is not a valid ScoreItemCode`）

- [ ] **Step 3: 實作**

在 `models/appraisal.py` 的 `ScoreItemCode` enum（`OTHER = "OTHER"` 之前）插入：

```python
    # 規章第六篇對齊新增（2026-06-11 spec；effective 2026-02-01 起有規則）
    ABSENTEEISM = "ABSENTEEISM"  # 曠職 −4/日（auto：考勤 status='absent'）
    STUDENT_WITHDRAWAL = "STUDENT_WITHDRAWAL"  # 休學 −2/人（手填；月費未繳條件人工判定）
    STUDENT_REINSTATE = "STUDENT_REINSTATE"  # 復學 +1/人（auto：StudentChangeLog）
    TRIAL_LEAVE = "TRIAL_LEAVE"  # 試讀離園 −1/人（手填）
    CLASS_TRANSFER = "CLASS_TRANSFER"  # 轉班 −0.5/人（手填）
    EXAM_RESULT = "EXAM_RESULT"  # 檢測成績（手填分值 ±10）
    RECRUIT_SCORE = "RECRUIT_SCORE"  # 招生人數（手填分值 0~20）
    SUPERVISOR_SCORE = "SUPERVISOR_SCORE"  # 主管加分（手填分值 0~10）
    EXCELLENCE_NOMINATION = "EXCELLENCE_NOMINATION"  # 呈報優異 +2（每學期全園 1 位）
```

`AUTO_ITEM_CODES` frozenset 增加兩項：

```python
        ScoreItemCode.ABSENTEEISM,
        ScoreItemCode.STUDENT_REINSTATE,
```

- [ ] **Step 4: 跑測試確認通過**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/appraisal.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): ScoreItemCode 新增規章九項（曠職/復學 auto，其餘手填）"
```

---

### Task 3: MANUAL_DELTA 規則型別（純函式）

**Files:**
- Modify: `services/appraisal/rule_applier.py`
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫失敗測試**

追加到 `tests/test_appraisal_regulation_align.py`：

```python
from datetime import date

from models.appraisal import RoleGroup
from services.appraisal.rule_applier import ScoringRule, apply_manual_delta


def _md_rule(lo, hi):
    return ScoringRule(
        item_code="CHILD_ACCIDENT",
        effective_from=date(2026, 2, 1),
        rule_type="MANUAL_DELTA",
        rule_config={"min_delta": lo, "max_delta": hi},
        applies_to_role_groups=None,
    )


def test_manual_delta_範圍內原值():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-3.5"), RoleGroup.HEAD_TEACHER) == Decimal("-3.50")


def test_manual_delta_下限clamp():
    rule = _md_rule(-10, 0)
    assert apply_manual_delta(rule, Decimal("-15"), RoleGroup.HEAD_TEACHER) == Decimal("-10.00")


def test_manual_delta_上限clamp():
    rule = _md_rule(0, 20)
    assert apply_manual_delta(rule, Decimal("25"), RoleGroup.HEAD_TEACHER) == Decimal("20.00")
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k manual_delta -q
```
Expected: FAIL（`cannot import name 'apply_manual_delta'`）

- [ ] **Step 3: 實作**

`services/appraisal/rule_applier.py` 在 `apply_disciplinary_tiered` 之後新增（並把 `apply_manual_delta` 加進檔尾 `__all__`）：

```python
def apply_manual_delta(
    rule: ScoringRule, value: Decimal, role_group: RoleGroup
) -> Decimal:
    """MANUAL_DELTA：count 欄存主任手填「分值」本身（可正可負）。

    依 rule_config 的 min_delta/max_delta clamp（API 層另有 422 驗證，
    此處 clamp 是第二道防線，保證舊資料/旁路寫入不會炸出範圍外分數）。
    """
    cfg = rule.rule_config
    lo = Decimal(str(cfg["min_delta"]))
    hi = Decimal(str(cfg["max_delta"]))
    v = Decimal(value)
    if v < lo:
        v = lo
    elif v > hi:
        v = hi
    return _round(v)
```

- [ ] **Step 4: 跑測試確認通過**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k manual_delta -q
```
Expected: 3 PASS

- [ ] **Step 5: Commit**

```bash
git add services/appraisal/rule_applier.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): MANUAL_DELTA 規則型別（手填分值＋範圍 clamp）"
```

---

### Task 4: 考勤聚合修正 — LEAVE 數 'leave'、新增曠職 absent_days

**背景（spec §14.3）**：`status_aggregator._aggregate_attendance` 目前 `leave_days` 數的是 `Attendance.status='absent'`（曠職），但模型定義 `LEAVE='leave'` 才是全天請假（員工請假同步寫入考勤）。這是既有 bug：請假天數實際上數到曠職。本 task 修正並讓兩者各歸其位。**行為變更**：LEAVE 項從此數 `'leave'`。

**Files:**
- Modify: `services/appraisal/status_aggregator.py`（`AttendanceAggregate` dataclass ＋ attendance 聚合查詢）
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 先讀現況**

```bash
grep -n "absent_expr\|leave_days\|def _aggregate_attendance\|case((Attendance" services/appraisal/status_aggregator.py | head -20
```
確認聚合函式名稱與行號（前次查證：absent_expr 在 149 行附近、`agg.leave_days = int(absent or 0)` 在 171 行附近）。

- [ ] **Step 2: 寫失敗測試**

追加（用既有 appraisal 測試的 in-memory session fixture 慣例；參考 `tests/test_appraisal_rule_applier.py` 開頭的 fixture import 方式，沿用同一套 `db_session`/cycle factory；若該檔用 `conftest.py` 的 fixture，直接沿用）：

```python
def test_attendance_aggregate_leave與absent分流(db_session, make_cycle_with_participant):
    """status='leave' 算請假、status='absent' 算曠職，不再混用。"""
    from datetime import date as d
    from models.attendance import Attendance

    cycle, participant, employee = make_cycle_with_participant(db_session)
    db_session.add_all([
        Attendance(employee_id=employee.id, attendance_date=d(2026, 3, 2), status="leave"),
        Attendance(employee_id=employee.id, attendance_date=d(2026, 3, 3), status="leave"),
        Attendance(employee_id=employee.id, attendance_date=d(2026, 3, 4), status="absent"),
    ])
    db_session.flush()

    from services.appraisal.status_aggregator import aggregate_cycle_status

    statuses = aggregate_cycle_status(db_session, cycle)
    st = next(s for s in statuses if s.employee_id == employee.id)
    assert st.attendance.leave_days == 2
    assert st.attendance.absent_days == 1
```

> 若 repo 沒有現成 `make_cycle_with_participant` factory，在本測試檔頂部自建最小 helper：建 `AppraisalCycle`（`start_date=date(2026,2,1)`、`end_date=date(2026,7,31)`、`base_score_calc_date=date(2026,3,15)`、`academic_year=114`、`semester=Semester.SECOND`）＋一個 `Employee` ＋ `AppraisalParticipant(role_group=RoleGroup.HEAD_TEACHER)`，欄位以 `models/appraisal.py` 與既有測試（`grep -rn "AppraisalCycle(" tests/ | head -3`）為準。

- [ ] **Step 3: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 分流 -q
```
Expected: FAIL（`absent_days` 不存在，且 leave_days 數錯來源）

- [ ] **Step 4: 實作**

`AttendanceAggregate` dataclass：

```python
    leave_days: int = 0  # status='leave'（全天請假）
    absent_days: int = 0  # status='absent'（曠職，規章 −4/日）
```

聚合查詢處（原 `absent_expr` 旁）新增 leave 表達式並分別賦值：

```python
    leave_expr = func.sum(case((Attendance.status == "leave", 1), else_=0)).label(
        "leave"
    )
    absent_expr = func.sum(case((Attendance.status == "absent", 1), else_=0)).label(
        "absent"
    )
```

select 同時帶兩個欄位，迴圈展開 `for eid, late, early, missing, leave, absent in rows:`：

```python
        agg.leave_days = int(leave or 0)
        agg.absent_days = int(absent or 0)
```

- [ ] **Step 5: 跑測試與既有聚合測試**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 分流 -q
python3 -m pytest tests/ -k "status_aggregator or aggregate" -q
```
Expected: 新測試 PASS；既有聚合測試若有依賴「absent 算 leave_days」的斷言會紅——逐一檢視，**那些是舊 bug 的鏡像，修正測試資料改用 status='leave'**（在 commit message 註明行為變更）。

- [ ] **Step 6: Commit**

```bash
git add services/appraisal/status_aggregator.py tests/
git commit -m "fix(appraisal): 考勤聚合 leave/absent 分流——請假數 status='leave'，新增曠職 absent_days（行為變更：原 leave_days 誤數曠職）"
```

---

### Task 5: 復學事件自動計數（StudentChangeLog）

**Files:**
- Modify: `services/appraisal/status_aggregator.py`（`ParticipantStatus` 加欄位＋新聚合）
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫失敗測試**

```python
def test_復學事件計入帶班老師(db_session, make_cycle_with_participant):
    from datetime import date as d
    from models.student_log import StudentChangeLog

    cycle, participant, employee = make_cycle_with_participant(db_session)
    # participant 須有 classroom（factory 若未配班，建 Classroom 並回填 participant/Employee 的 classroom_id，
    # 以 aggregate_cycle_status 取 classroom 的同一來源欄位為準——先讀 retention 聚合段確認）
    classroom_id = participant.classroom_id or employee.classroom_id
    db_session.add(
        StudentChangeLog(
            student_id=1,
            school_year=114,
            semester=2,
            event_type="復學",
            event_date=d(2026, 3, 10),
            classroom_id=classroom_id,
        )
    )
    db_session.flush()

    from services.appraisal.status_aggregator import aggregate_cycle_status

    st = next(
        s
        for s in aggregate_cycle_status(db_session, cycle)
        if s.employee_id == employee.id
    )
    assert st.reinstate_count == 1
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 復學 -q
```
Expected: FAIL（`reinstate_count` 不存在）

- [ ] **Step 3: 實作**

`ParticipantStatus` dataclass 加：

```python
    reinstate_count: int = 0  # 復學事件數（StudentChangeLog event_type='復學'，依班級＋時間窗）
```

在 facade `aggregate_cycle_status` 組裝處（與 retention/activity 同一層）新增聚合函式並接上：

```python
def _aggregate_reinstates(
    session: Session, cycle, classroom_ids: list[int], window_end: date
) -> dict[int, int]:
    """classroom_id → 復學事件數。時間窗與其他聚合一致 [start, min(end, today)]。"""
    from sqlalchemy import select

    from models.student_log import StudentChangeLog

    if not classroom_ids:
        return {}
    rows = session.execute(
        select(StudentChangeLog.classroom_id, func.count())
        .where(
            StudentChangeLog.event_type == "復學",
            StudentChangeLog.event_date >= cycle.start_date,
            StudentChangeLog.event_date <= window_end,
            StudentChangeLog.classroom_id.in_(classroom_ids),
        )
        .group_by(StudentChangeLog.classroom_id)
    ).all()
    return {cid: int(cnt) for cid, cnt in rows}
```

組裝 `ParticipantStatus` 時：`reinstate_count=reinstate_by_classroom.get(classroom_id, 0)`（classroom_id 為 None 者保持 0）。時間窗 `window_end` 沿用檔內既有的 `min(cycle.end_date, today_taipei())` 變數。

- [ ] **Step 4: 跑測試確認通過**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 復學 -q
```
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add services/appraisal/status_aggregator.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): 復學事件自動聚合（StudentChangeLog→reinstate_count）"
```

---

### Task 6: 未帶班全校平均留校率＋才藝率年級名

**Files:**
- Modify: `services/appraisal/status_aggregator.py`（retention 聚合補全校平均；`ActivityRateAggregate` 加 `grade_name`）
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫失敗測試**

```python
def test_未帶班參與者取全校平均留校率(db_session, make_cycle_with_participant):
    """無 classroom 的 participant（園長/辦公室），retention_rate = 全校加權平均。"""
    cycle, p_office, emp_office = make_cycle_with_participant(
        db_session, role_group="STAFF", with_classroom=False
    )
    # 建兩個有班參與者（班 A 留校 10/10=100%、班 B 5/10=50%）→ 全校 15/20 = 75%
    # （班級與學生 seeding 沿用檔內既有 retention 測試的做法；
    #   先 grep -rn "retention" tests/ 找現成 factory 重用）
    from services.appraisal.status_aggregator import aggregate_cycle_status

    st = next(
        s
        for s in aggregate_cycle_status(db_session, cycle)
        if s.employee_id == emp_office.id
    )
    assert st.retention.retention_rate == Decimal("75.00")


def test_activity_aggregate_含年級名(db_session, make_cycle_with_participant):
    cycle, participant, employee = make_cycle_with_participant(db_session)
    from services.appraisal.status_aggregator import aggregate_cycle_status

    st = next(
        s
        for s in aggregate_cycle_status(db_session, cycle)
        if s.employee_id == employee.id
    )
    # factory 的班級掛 ClassGrade(name='大班')
    assert st.activity.grade_name == "大班"
```

> `make_cycle_with_participant` factory 需支援 `role_group=`、`with_classroom=` 參數與掛 `ClassGrade`；本 task 開工前先擴充 factory。留校率的分子分母口徑（initial/final 的定義）以檔內既有 `ClassRetentionAggregate` 聚合段為準，測試 seeding 仿照既有 retention 測試。

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k "未帶班 or 年級名" -q
```
Expected: FAIL

- [ ] **Step 3: 實作**

retention 聚合段（per-classroom rate 算完之後）：

```python
    # 規章第五條(七)2：未帶班人員依全校平均留校率核算。
    # 全校平均 = Σfinal / Σinitial（加權，非班級率簡單平均），與班級率同樣 2 位 HALF_UP。
    total_initial = sum(a.initial_count for a in retention_by_classroom.values())
    total_final = sum(a.final_count for a in retention_by_classroom.values())
    school_avg_rate = (
        (Decimal(total_final) / Decimal(total_initial) * 100).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )
        if total_initial
        else Decimal("0")
    )
```

組裝 `ParticipantStatus` 時，`classroom_id is None` 的 participant 其 `ClassRetentionAggregate` 改填 `retention_rate=school_avg_rate`（initial/final 保持 0，`classroom_name=None`）。變數名以檔內實際結構為準（先讀該段，retention 聚合可能以 employee 為 key——對齊現況改寫）。

`ActivityRateAggregate` 加欄位：

```python
    grade_name: Optional[str] = None  # ClassGrade.name（大班/中班/小班/幼幼班），AFTER_CLASS_RATE 分年級門檻用
```

activity 聚合查詢 join `Classroom.grade`（`models/classroom.py:117` relationship → `ClassGrade.name`）填入。

- [ ] **Step 4: 跑測試確認通過＋既有測試**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k "未帶班 or 年級名" -q
python3 -m pytest tests/ -k appraisal -q
```
Expected: PASS；既有 appraisal 測試不紅（`grade_name`/`absent_days`/`reinstate_count` 都是加欄位、預設值向後相容）

- [ ] **Step 5: Commit**

```bash
git add services/appraisal/status_aggregator.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): 未帶班全校平均留校率＋才藝聚合帶年級名"
```

---

### Task 7: 規則函式擴充 — 分年級門檻、獎懲加分側、merit action types

**Files:**
- Modify: `services/appraisal/rule_applier.py`（`apply_flat_threshold`、`apply_disciplinary_tiered`）
- Modify: `models/disciplinary.py`（ACTION_TYPES 加三個 merit 值）
- Modify: `services/appraisal/status_aggregator.py`（DisciplinaryAggregate 加 merit counts）
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫失敗測試**

```python
def test_flat_threshold_分年級門檻():
    from services.appraisal.rule_applier import apply_flat_threshold

    rule = ScoringRule(
        item_code="AFTER_CLASS_RATE",
        effective_from=date(2026, 2, 1),
        rule_type="FLAT_THRESHOLD",
        rule_config={
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
            "grade_thresholds": {"大班": 100, "中班": 90, "小班": 80, "幼幼班": 70},
        },
        applies_to_role_groups=None,
    )
    rg = RoleGroup.HEAD_TEACHER
    assert apply_flat_threshold(rule, Decimal("95"), rg, grade_name="大班") == Decimal("0.00")
    assert apply_flat_threshold(rule, Decimal("100"), rg, grade_name="大班") == Decimal("2.00")
    assert apply_flat_threshold(rule, Decimal("75"), rg, grade_name="幼幼班") == Decimal("2.00")
    # 無年級（或 map 沒有該年級）→ 回退 threshold=80
    assert apply_flat_threshold(rule, Decimal("85"), rg, grade_name=None) == Decimal("2.00")


def test_disciplinary_tiered_加分側():
    from services.appraisal.rule_applier import apply_disciplinary_tiered

    rule = ScoringRule(
        item_code="REWARD_PUNISH",
        effective_from=date(2026, 2, 1),
        rule_type="DISCIPLINARY_TIERED",
        rule_config={
            "warning_delta": -2.0, "minor_delta": -3.0, "major_delta": -6.0,
            "commend_delta": 2.0, "minor_merit_delta": 3.0, "major_merit_delta": 6.0,
        },
        applies_to_role_groups=None,
    )
    # 1 大功 + 1 警告 = +6 − 2 = +4（功過相抵）
    assert apply_disciplinary_tiered(
        rule, warning_count=1, minor_count=0, major_count=0,
        commend_count=0, minor_merit_count=0, major_merit_count=1,
    ) == Decimal("4.00")


def test_disciplinary_tiered_舊config向後相容():
    from services.appraisal.rule_applier import apply_disciplinary_tiered

    rule = ScoringRule(
        item_code="REWARD_PUNISH",
        effective_from=date(2025, 8, 1),
        rule_type="DISCIPLINARY_TIERED",
        rule_config={"warning_delta": -1.0, "minor_delta": -3.0, "major_delta": -10.0},
        applies_to_role_groups=None,
    )
    # 舊三參數呼叫（既有 caller）不帶 merit → 行為不變
    assert apply_disciplinary_tiered(rule, 1, 1, 0) == Decimal("-4.00")


def test_merit_action_types_註冊():
    from models.disciplinary import ACTION_TYPES, ACTION_TYPE_LABELS

    for t, label in (("commendation", "嘉獎"), ("minor_merit", "小功"), ("major_merit", "大功")):
        assert t in ACTION_TYPES
        assert ACTION_TYPE_LABELS[t] == label
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k "分年級 or 加分側 or 向後相容 or merit" -q
```
Expected: FAIL

- [ ] **Step 3: 實作**

`models/disciplinary.py`（緊接既有三常數之後）：

```python
ACTION_TYPE_COMMEND = "commendation"
ACTION_TYPE_MINOR_MERIT = "minor_merit"
ACTION_TYPE_MAJOR_MERIT = "major_merit"

ACTION_TYPES = (
    ACTION_TYPE_WARNING,
    ACTION_TYPE_MINOR,
    ACTION_TYPE_MAJOR,
    ACTION_TYPE_COMMEND,
    ACTION_TYPE_MINOR_MERIT,
    ACTION_TYPE_MAJOR_MERIT,
)

ACTION_TYPE_LABELS = {
    ACTION_TYPE_WARNING: "警告",
    ACTION_TYPE_MINOR: "小過",
    ACTION_TYPE_MAJOR: "大過",
    ACTION_TYPE_COMMEND: "嘉獎",
    ACTION_TYPE_MINOR_MERIT: "小功",
    ACTION_TYPE_MAJOR_MERIT: "大功",
}
```

> `action_type` 是 `String(20)` 欄位，免 migration；API 驗證（`api/disciplinary.py:117,181`）吃 `ACTION_TYPES` 常數自動放行。Merit 列 `deduction_amount` 維持 0，不進薪資扣款。

`apply_flat_threshold` 加 `grade_name` 參數：

```python
def apply_flat_threshold(
    rule: ScoringRule,
    value: Decimal,
    role_group: RoleGroup,
    grade_name: Optional[str] = None,
) -> Decimal:
    """value >= threshold → above_delta；否則 below_delta。

    config 含 grade_thresholds（年級名→門檻）時優先依 grade_name 取門檻，
    取不到（grade_name=None 或不在 map）回退單一 threshold。
    """
    cfg = rule.rule_config
    threshold = Decimal(str(cfg["threshold"]))
    grade_map = cfg.get("grade_thresholds") or {}
    if grade_name is not None and grade_name in grade_map:
        threshold = Decimal(str(grade_map[grade_name]))
    if value >= threshold:
        return _round(Decimal(str(cfg["above_delta"])))
    return _round(Decimal(str(cfg["below_delta"])))
```

`apply_disciplinary_tiered` 加 keyword-only merit 參數（預設 0，舊 caller 不變）：

```python
def apply_disciplinary_tiered(
    rule: ScoringRule,
    warning_count: int,
    minor_count: int,
    major_count: int,
    *,
    commend_count: int = 0,
    minor_merit_count: int = 0,
    major_merit_count: int = 0,
) -> Decimal:
    """REWARD_PUNISH：懲（警告/小過/大過）＋功（嘉獎/小功/大功）各自單價加總（功過相抵）。

    加分側 config 鍵為 optional（舊 2025-08-01 config 沒有），缺鍵視為 0。
    """
    cfg = rule.rule_config
    delta = (
        Decimal(str(cfg["warning_delta"])) * Decimal(warning_count)
        + Decimal(str(cfg["minor_delta"])) * Decimal(minor_count)
        + Decimal(str(cfg["major_delta"])) * Decimal(major_count)
        + Decimal(str(cfg.get("commend_delta", 0))) * Decimal(commend_count)
        + Decimal(str(cfg.get("minor_merit_delta", 0))) * Decimal(minor_merit_count)
        + Decimal(str(cfg.get("major_merit_delta", 0))) * Decimal(major_merit_count)
    )
    return _round(delta)
```

`status_aggregator.DisciplinaryAggregate` 加三欄（預設 0）並在懲處聚合段以 `action_type` 分組計入：

```python
    commend_count: int = 0
    minor_merit_count: int = 0
    major_merit_count: int = 0
```

（聚合段現況用 warning/minor/major 三個 case 加總——同 pattern 加三個 case；先讀該段對齊寫法。）

- [ ] **Step 4: 跑測試確認通過**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k "分年級 or 加分側 or 向後相容 or merit" -q
python3 -m pytest tests/ -k "disciplinary or rule_applier" -q
```
Expected: PASS、既有測試綠（簽名向後相容）

- [ ] **Step 5: Commit**

```bash
git add services/appraisal/rule_applier.py models/disciplinary.py services/appraisal/status_aggregator.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): 分年級才藝門檻＋獎懲加分側（嘉獎/小功/大功 merit action types）"
```

---

### Task 8: compute_all_deltas 分流接線（新 auto 項＋MANUAL_DELTA）

**Files:**
- Modify: `services/appraisal/rule_applier.py`（`_apply_auto_item` 與 `compute_all_deltas`）
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫失敗測試**

仿照 `tests/test_appraisal_rule_applier.py` 既有 fake-status 測試 pattern（先讀該檔確認 fake ParticipantStatus 的建構方式並沿用）：

```python
def test_auto_item_曠職與復學():
    from services.appraisal.rule_applier import _apply_auto_item

    absent_rule = ScoringRule("ABSENTEEISM", date(2026, 2, 1), "PER_UNIT",
                              {"per_unit_delta": -4.0}, None)
    reinstate_rule = ScoringRule("STUDENT_REINSTATE", date(2026, 2, 1), "PER_UNIT",
                                 {"per_unit_delta": 1.0}, None)
    status = _make_fake_status(absent_days=2, reinstate_count=1)  # 仿既有 fake helper
    d1, raw1, _ = _apply_auto_item(absent_rule, status, RoleGroup.HEAD_TEACHER)
    assert (d1, raw1) == (Decimal("-8.00"), Decimal("2"))
    d2, raw2, _ = _apply_auto_item(reinstate_rule, status, RoleGroup.HEAD_TEACHER)
    assert (d2, raw2) == (Decimal("1.00"), Decimal("1"))


def test_auto_item_才藝率帶年級門檻():
    from services.appraisal.rule_applier import _apply_auto_item

    rule = ScoringRule("AFTER_CLASS_RATE", date(2026, 2, 1), "FLAT_THRESHOLD",
                       {"threshold": 80, "above_delta": 2.0, "below_delta": 0,
                        "grade_thresholds": {"大班": 100}}, None)
    status = _make_fake_status(activity_rate=Decimal("95"), grade_name="大班")
    delta, _, _ = _apply_auto_item(rule, status, RoleGroup.HEAD_TEACHER)
    assert delta == Decimal("0.00")  # 大班門檻 100，95 不達標
```

- [ ] **Step 2: 跑測試確認失敗**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k "auto_item" -q
```
Expected: FAIL（`_apply_auto_item: 未知 auto item_code ABSENTEEISM`）

- [ ] **Step 3: 實作**

`_apply_auto_item` 增加兩個分支（放在 `CLASS_HEADCOUNT_BONUS` 之前）、改 AFTER_CLASS_RATE 與 REWARD_PUNISH 分支：

```python
    if code == "ABSENTEEISM":
        cnt = Decimal(status.attendance.absent_days)
        return apply_per_unit(rule, cnt, role_group), cnt, f"曠職 {cnt} 天"
    if code == "STUDENT_REINSTATE":
        cnt = Decimal(status.reinstate_count)
        return apply_per_unit(rule, cnt, role_group), cnt, f"復學 {cnt} 人"
```

AFTER_CLASS_RATE 分支改傳年級：

```python
        return (
            apply_flat_threshold(
                rule, rate, role_group, grade_name=status.activity.grade_name
            ),
            rate,
            f"才藝率 {rate}%",
        )
```

REWARD_PUNISH 分支補 merit counts 與 note：

```python
        delta = apply_disciplinary_tiered(
            rule,
            d.warning_count,
            d.minor_count,
            d.major_count,
            commend_count=d.commend_count,
            minor_merit_count=d.minor_merit_count,
            major_merit_count=d.major_merit_count,
        )
        raw = Decimal(
            d.warning_count + d.minor_count + d.major_count
            + d.commend_count + d.minor_merit_count + d.major_merit_count
        )
        return (
            delta,
            raw,
            f"警告 {d.warning_count} / 小過 {d.minor_count} / 大過 {d.major_count}"
            f" / 嘉獎 {d.commend_count} / 小功 {d.minor_merit_count} / 大功 {d.major_merit_count}",
        )
```

`compute_all_deltas` manual 分流加 MANUAL_DELTA（在 `elif rule.rule_type == "FLAT_THRESHOLD"` 之後）：

```python
                elif rule.rule_type == "MANUAL_DELTA":
                    delta = apply_manual_delta(rule, cnt, role)
```

並把該分支的 note 對 MANUAL_DELTA 改成分值語意：

```python
                note = (
                    f"手填分值 {cnt}" if rule.rule_type == "MANUAL_DELTA"
                    else (f"手填 {cnt} 次" if cnt else "未填")
                )
                result[(status.participant_id, code)] = DeltaResult(delta, cnt, note)
```

- [ ] **Step 4: 跑測試確認通過**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -q
python3 -m pytest tests/ -k "rule_applier" -q
```
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add services/appraisal/rule_applier.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): compute_all_deltas 接線曠職/復學 auto 與 MANUAL_DELTA 分流"
```

---

### Task 9: Migration aprreg01 — 獎金率五值＋2026-02-01 規則全套

**Files:**
- Create: `alembic/versions/20260611_aprreg01_appraisal_regulation_align.py`
- Test: 既有 pytest 不吃 migration（SQLite seed 由 fixture 管）；本 task 用本機 PG dev DB 實機驗證

- [ ] **Step 1: 寫 migration**

```python
"""考核對齊人事規章第六篇（spec 2026-06-11）。

① appraisal_bonus_rates：五組值改為規章值（兩個 effective set 都改，in-place）：
   SUPERVISOR 優 8000→10000、HEAD_TEACHER 優 6000→8000、STAFF 優 6000→8000、
   ASSISTANT 優 5500→6000、COOK 甲 4000→3500。
   （114上 僅出現 HEAD_TEACHER 甲等案例，本次改值不影響任何歷史金額。）
② appraisal_scoring_rules：插入 effective_from='2026-02-01'（114下起）全套 24 條，
   既有 2025-08-01 列不動（歷史學期重算結果不變）。

Revision ID: aprreg01
Revises: yebnd01
Create Date: 2026-06-11
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "aprreg01"
down_revision = "yebnd01"
branch_labels = None
depends_on = None

RATE_EFFECTIVES = ("2025-08-01", "2026-08-01")

# (role_group, grade, 規章值, 還原值)
RATE_CHANGES = [
    ("SUPERVISOR", "OUTSTANDING", 10000, 8000),
    ("HEAD_TEACHER", "OUTSTANDING", 8000, 6000),
    ("STAFF", "OUTSTANDING", 8000, 6000),
    ("ASSISTANT", "OUTSTANDING", 6000, 5500),
    ("COOK", "GOOD", 3500, 4000),
]

RULES_EFFECTIVE = "2026-02-01"  # 114下（2026-02-01~07-31）起適用
_TEACHING = ["HEAD_TEACHER", "ASSISTANT"]

# 24 條：15 既有 code（規章新值或照抄）＋ 9 新 code
RULES = [
    # --- 出缺勤（第五條(二)）---
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("ABSENTEEISM", "PER_UNIT", {"per_unit_delta": -4.0}, None),
    # --- 留校率（第五條(七)；未帶班吃全校平均 → applies None）---
    (
        "RETURNING_RATE_0915",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -2.0},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -4.0},
            ],
        },
        None,
    ),
    (
        "RETURNING_RATE_0315",
        "TIER",
        {
            "input_field": "retention_rate",
            "tiers": [
                {"min": 100, "delta": 6.0},
                {"min": 95, "delta": 0.0},
                {"min": 90, "delta": -2.0},
                {"min": 80, "delta": -3.0},
                {"min": 0, "delta": -4.0},
            ],
        },
        None,
    ),
    # --- 才藝班參加率（第五條(九)：分年級門檻）---
    (
        "AFTER_CLASS_RATE",
        "FLAT_THRESHOLD",
        {
            "input_field": "activity_rate",
            "threshold": 80,
            "above_delta": 2.0,
            "below_delta": 0,
            "grade_thresholds": {"大班": 100, "中班": 90, "小班": 80, "幼幼班": 70},
        },
        _TEACHING,
    ),
    # --- 獎懲（第五條(十)：功過相抵）---
    (
        "REWARD_PUNISH",
        "DISCIPLINARY_TIERED",
        {
            "warning_delta": -2.0,
            "minor_delta": -3.0,
            "major_delta": -6.0,
            "commend_delta": 2.0,
            "minor_merit_delta": 3.0,
            "major_merit_delta": 6.0,
        },
        None,
    ),
    # --- 會議活動（第五條(十二)：每時數 −0.5；count=計分時數，每次活動最多計 4 小時=封頂 −2 由填報執行）---
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    # --- 幼兒意外（第五條(六)：主管評議 1~10 分）---
    ("CHILD_ACCIDENT", "MANUAL_DELTA", {"min_delta": -10, "max_delta": 0}, None),
    # --- 帶班/特教（不變值，新版本照抄）---
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("SPED", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
    # --- 休學細則（第五條(五)）---
    ("STUDENT_WITHDRAWAL", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("STUDENT_REINSTATE", "PER_UNIT", {"per_unit_delta": 1.0}, None),
    ("TRIAL_LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("CLASS_TRANSFER", "PER_UNIT", {"per_unit_delta": -0.5}, None),
    # --- 公告制手填分值 ---
    ("EXAM_RESULT", "MANUAL_DELTA", {"min_delta": -10, "max_delta": 10}, None),
    ("RECRUIT_SCORE", "MANUAL_DELTA", {"min_delta": 0, "max_delta": 20}, None),
    ("SUPERVISOR_SCORE", "MANUAL_DELTA", {"min_delta": 0, "max_delta": 10}, None),
    # --- 呈報優異（第五條(十一)1：每學期 1 位 → unit_cap=1）---
    ("EXCELLENCE_NOMINATION", "PER_UNIT", {"per_unit_delta": 2.0, "unit_cap": 1}, None),
]


def _has_table(bind: sa.engine.Connection, name: str) -> bool:
    return name in sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "appraisal_bonus_rates"):
        for eff in RATE_EFFECTIVES:
            for rg, gr, new_amt, _old in RATE_CHANGES:
                bind.execute(
                    sa.text(
                        "UPDATE appraisal_bonus_rates SET base_amount = :a"
                        " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                    ),
                    {"a": new_amt, "e": eff, "rg": rg, "gr": gr},
                )

    if _has_table(bind, "appraisal_scoring_rules"):
        existing = {
            (r[0], r[1].isoformat() if hasattr(r[1], "isoformat") else str(r[1]))
            for r in bind.execute(
                sa.text("SELECT item_code, effective_from FROM appraisal_scoring_rules")
            ).fetchall()
        }
        for code, rtype, cfg, roles in RULES:
            if (code, RULES_EFFECTIVE) in existing:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO appraisal_scoring_rules"
                    " (item_code, effective_from, rule_type, rule_config,"
                    "  applies_to_role_groups)"
                    " VALUES (:c, :e, :t, CAST(:cfg AS JSONB), CAST(:roles AS JSONB))"
                ),
                {
                    "c": code,
                    "e": RULES_EFFECTIVE,
                    "t": rtype,
                    "cfg": json.dumps(cfg),
                    "roles": json.dumps(roles) if roles is not None else None,
                },
            )


def downgrade() -> None:
    """還原獎金率五組 seed 值（非 HR 後續手調值）＋刪 2026-02-01 規則列。"""
    bind = op.get_bind()
    if _has_table(bind, "appraisal_bonus_rates"):
        for eff in RATE_EFFECTIVES:
            for rg, gr, _new, old_amt in RATE_CHANGES:
                bind.execute(
                    sa.text(
                        "UPDATE appraisal_bonus_rates SET base_amount = :a"
                        " WHERE effective_from = :e AND role_group = :rg AND grade = :gr"
                    ),
                    {"a": old_amt, "e": eff, "rg": rg, "gr": gr},
                )
    if _has_table(bind, "appraisal_scoring_rules"):
        bind.execute(
            sa.text("DELETE FROM appraisal_scoring_rules WHERE effective_from = :e"),
            {"e": RULES_EFFECTIVE},
        )
```

> 注意（workspace 慣例）：`op.execute`/`sa.text` 內**不可**出現字面冒號字串（`:word` 會被當 bind param）——上面 SQL 僅用具名參數，安全。SQLite 測試環境不跑 migration（fixture 自 seed）。

- [ ] **Step 2: 先查 dev DB 是否有 HR 手調痕跡（spec §13 風險）**

```bash
psql postgresql://yilunwu@localhost:5432/ivymanagement -c \
 "SELECT effective_from, role_group, grade, base_amount FROM appraisal_bonus_rates ORDER BY 1,2,3;"
```
Expected: 與 apxlal01 的 ALIGNED_RATES 一致（8000/5000/6000/4000/5500/3500/6000/4000/6000/4000 兩組）。若有不一致值＝HR 手調過，停下回報（migration 會覆寫，需業主確認）。

- [ ] **Step 3: 對 dev DB 實機 upgrade**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align
python3 -m alembic upgrade heads
psql postgresql://yilunwu@localhost:5432/ivymanagement -c \
 "SELECT role_group, grade, base_amount FROM appraisal_bonus_rates WHERE effective_from='2025-08-01' ORDER BY 1,2;"
psql postgresql://yilunwu@localhost:5432/ivymanagement -c \
 "SELECT count(*) FROM appraisal_scoring_rules WHERE effective_from='2026-02-01';"
```
Expected: SUPERVISOR 優=10000、HEAD_TEACHER 優=8000、STAFF 優=8000、ASSISTANT 優=6000、COOK 甲=3500；rules count=24。
（⚠ 本機 dev DB 與主 checkout 共用——upgrade 後主 checkout 跑 server 不受影響，aprreg01 只動 seed 值；若需還原 `python3 -m alembic downgrade yebnd01`。）

- [ ] **Step 4: downgrade/upgrade 往返驗證**

```bash
python3 -m alembic downgrade yebnd01 && python3 -m alembic upgrade heads
```
Expected: 無錯誤；再查 Step 3 的兩個 SELECT 結果相同。

- [ ] **Step 5: Commit**

```bash
git add alembic/versions/20260611_aprreg01_appraisal_regulation_align.py
git commit -m "feat(appraisal): migration aprreg01——獎金率對齊規章五值＋2026-02-01 計分規則全套 24 條"
```

---

### Task 10: 手填同步 API 的 MANUAL_DELTA 範圍驗證

**Files:**
- Modify: `api/appraisal/__init__.py`（manual_event_counts 批次 upsert 端點，1724 行附近找 PUT/POST handler）
- Test: `tests/test_appraisal_regulation_align.py`（追加；用既有 API 測試 client fixture 慣例，參考 `tests/` 內既有 manual_event_counts 測試）

- [ ] **Step 1: 先讀端點現況**

```bash
grep -n "manual_event_counts" -A 30 api/appraisal/__init__.py | sed -n '1,80p'
grep -rn "manual_event_counts" tests/ -l
```
確認 batch upsert handler 名稱、權限 dependency 與既有測試檔（沿用其 fixture）。

- [ ] **Step 2: 寫失敗測試**

```python
def test_manual_delta_範圍外_422(appraisal_api_client, seeded_cycle_2026):
    """CHILD_ACCIDENT 規則 min=-10：填 -15 應 422。"""
    cycle_id, participant_id = seeded_cycle_2026  # cycle base_score_calc_date ≥ 2026-02-01，DB 已 seed 2026-02-01 規則
    resp = appraisal_api_client.put(
        f"/api/appraisal/cycles/{cycle_id}/manual_event_counts",
        json={"items": [{"participant_id": participant_id,
                         "item_code": "CHILD_ACCIDENT", "count": -15}]},
    )
    assert resp.status_code == 422
    assert "範圍" in resp.json()["detail"]
```

> fixture `seeded_cycle_2026` 需在 SQLite 測試 DB 手動插 `appraisal_scoring_rules`（CHILD_ACCIDENT, 2026-02-01, MANUAL_DELTA, {"min_delta":-10,"max_delta":0}）——SQLite 不跑 migration，仿照既有 rules-seed 測試（`grep -rn "appraisal_scoring_rules\|AppraisalScoringRule(" tests/ | head` 找現成寫法）。URL prefix 與 method 以 Step 1 讀到的實際 router 為準。

- [ ] **Step 3: 跑測試確認失敗**

Expected: 回 200（驗證不存在）→ assert 422 FAIL

- [ ] **Step 4: 實作**

在 batch upsert handler 寫入迴圈前加驗證（與既有 import 對齊）：

```python
    from services.appraisal.rule_applier import load_rules_for_date

    rules = load_rules_for_date(session, cycle.base_score_calc_date)
    for item in body.items:
        rule = rules.get(item.item_code)
        if rule is not None and rule.rule_type == "MANUAL_DELTA":
            lo = Decimal(str(rule.rule_config["min_delta"]))
            hi = Decimal(str(rule.rule_config["max_delta"]))
            if not (lo <= Decimal(item.count) <= hi):
                raise HTTPException(
                    status_code=422,
                    detail=f"{item.item_code} 分值 {item.count} 超出範圍 [{lo}, {hi}]",
                )
```

（變數名 `body.items`/`item.count` 以實際 schema `ManualEventCountBatchIn` 欄位為準。）

- [ ] **Step 5: 跑測試確認通過 + Commit**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 422 -q
git add api/appraisal/__init__.py tests/test_appraisal_regulation_align.py
git commit -m "feat(appraisal): manual_event_counts 對 MANUAL_DELTA 分值做範圍驗證（422）"
```

---

### Task 11: 歷史保護回歸＋effective 邊界測試

**Files:**
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 跑 114上 對帳鎖**

```bash
python3 -m pytest tests/test_appraisal_excel_reconcile_114.py -q
```
Expected: PASS（該測試以 2025-08-01 規則與 HEAD_TEACHER 甲 4000 計算，本計畫皆未動）。若紅，停下——表示動到了不該動的歷史路徑。

- [ ] **Step 2: 寫 effective 邊界測試（失敗→實作已在 Task 9 完成→直接綠）**

```python
def test_effective_邊界_0131用舊版_0201用新版(db_session):
    """SQLite 手動 seed 兩版 LEAVE-同碼規則，驗證 load_rules_for_date 選版。"""
    from datetime import date as d

    from models.appraisal import AppraisalScoringRule
    from services.appraisal.rule_applier import load_rules_for_date

    db_session.add_all([
        AppraisalScoringRule(
            item_code="RETURNING_RATE_0315", effective_from=d(2025, 8, 1),
            rule_type="TIER",
            rule_config={"tiers": [{"min": 0, "delta": -6.0}]},
            applies_to_role_groups=["HEAD_TEACHER", "ASSISTANT"],
        ),
        AppraisalScoringRule(
            item_code="RETURNING_RATE_0315", effective_from=d(2026, 2, 1),
            rule_type="TIER",
            rule_config={"tiers": [{"min": 0, "delta": -4.0}]},
            applies_to_role_groups=None,
        ),
    ])
    db_session.flush()
    old = load_rules_for_date(db_session, d(2026, 1, 31))["RETURNING_RATE_0315"]
    new = load_rules_for_date(db_session, d(2026, 2, 1))["RETURNING_RATE_0315"]
    assert old.effective_from == d(2025, 8, 1)
    assert new.effective_from == d(2026, 2, 1)
    assert new.applies_to_role_groups is None
```

- [ ] **Step 3: 跑測試 + Commit**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 邊界 -q
git add tests/test_appraisal_regulation_align.py
git commit -m "test(appraisal): effective 2026-02-01 選版邊界＋114上歷史保護驗證"
```

---

### Task 12: 新制金標準測試（規章值端到端純函式）

**Files:**
- Test: `tests/test_appraisal_regulation_align.py`（追加）

- [ ] **Step 1: 寫測試（鎖規章值，TDD 意義上是「規格鎖」，預期直接綠；若紅即實作有誤）**

```python
def test_規章金標準_主管優等():
    """主管 92 分優等：10000 × 92/100 = 9200.00"""
    from services.appraisal.engine import BonusRateLookup, compute_summary
    from models.appraisal import Grade, RoleGroup

    rates = BonusRateLookup(rates={
        ("2025-08-01", RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("10000"),
        ("2025-08-01", RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
    })
    r = compute_summary(
        actual_enrollment=160, enrollment_target=160,  # base = 100
        score_deltas=[Decimal("-8")],  # total 92
        role_group=RoleGroup.SUPERVISOR, bonus_rates=rates,
        on_date=date(2026, 3, 15),
    )
    assert r.total_score == Decimal("92.00")
    assert r.grade == Grade.OUTSTANDING
    assert r.bonus_amount == Decimal("9200.00")


def test_規章金標準_廚師甲等3500():
    from services.appraisal.engine import BonusRateLookup, compute_summary
    from models.appraisal import Grade, RoleGroup

    rates = BonusRateLookup(rates={
        ("2025-08-01", RoleGroup.COOK, Grade.GOOD): Decimal("3500"),
    })
    r = compute_summary(
        actual_enrollment=121, enrollment_target=160,  # base 75.6
        score_deltas=[Decimal("6"), Decimal("2")],  # total 83.6 → 甲
        role_group=RoleGroup.COOK, bonus_rates=rates,
        on_date=date(2026, 3, 15),
    )
    assert r.grade == Grade.GOOD
    assert r.bonus_amount == Decimal("2926.00")  # 3500 × 83.6/100
```

（`compute_summary` 參數名以 `services/appraisal/engine.py:212` 實際簽名為準——撰寫時先讀。）

- [ ] **Step 2: 跑測試 + Commit**

```bash
python3 -m pytest tests/test_appraisal_regulation_align.py -k 金標準 -q
git add tests/test_appraisal_regulation_align.py
git commit -m "test(appraisal): 規章獎金率金標準鎖（主管優 10000、廚師甲 3500）"
```

---

### Task 13: appraisal_sync 過時 B3 註解與 mark_salary_stale 查證

**Files:**
- Modify: `services/year_end/appraisal_sync.py:399-407`

- [ ] **Step 1: 查證 special_bonus_items 是否仍進薪資/補充保費**

```bash
grep -rn "query_appraisal_year_end_bonus" --include="*.py" services/ api/ | grep -v "appraisal_year_end.py"
grep -n "appraisal_year_end_bonus" services/salary/engine.py | head -5
```
Expected: 第一個 grep 無 runtime caller（只在 deprecated 模組自身與測試）；第二個 grep 顯示 engine 一律填 0（CLAUDE.md §10 決策⑥B）。若查到活的 caller，停下回報（B3 註解就不是過時的，本 task 取消）。

- [ ] **Step 2: 修正**

確認無 caller 後，刪除 `generate_payouts` 內 B3 區塊（`from services.salary.utils import mark_salary_stale_from_month` 與其 for 迴圈），替換為：

```python
    # 決策⑥B（2026-06-02）後考核獎金走 year_end settlement 表外發放，
    # 不進月薪資、不進二代健保補充保費累計（CLAUDE.md §10/§11），
    # 故不再標記薪資 needs_recalc。
```

- [ ] **Step 3: 跑 payout 測試 + Commit**

```bash
python3 -m pytest tests/ -k "payout or appraisal_sync" -q
```
Expected: PASS（若有測試斷言 mark stale 行為，連同更新並在 commit message 說明）。

```bash
git add services/year_end/appraisal_sync.py tests/
git commit -m "fix(year-end): 移除考核 payout 的過時薪資重算標記（決策⑥B 後表外發放，B3 註解已失效）"
```

---

### Task 14: 後端全套件驗證

- [ ] **Step 1: 全量 pytest**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align
python3 -m pytest tests/ -q 2>&1 | tail -15
```
Expected: 全綠（已知 baseline 紅以 Task 1 Step 4 記錄為準；新紅一律先查自己）。

- [ ] **Step 2: OpenAPI 漂移**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align && ENV=development python3 scripts/dump_openapi.py
cd /Users/yilunwu/Desktop/ivy-frontend && npm run gen:api:check
```
Expected: 本計畫未改 Pydantic response schema，應無漂移；若有，產出 schema.d.ts 留給前端 task 一併 commit。

---

### Task 15: 前端 — 標籤、手填項、懲處 merit 選項

**Files:**（ivy-frontend repo，分支 `feat/appraisal-regulation-align-2026-06-11-fe` 從 FE origin/main）
- Modify: `src/views/appraisal/scoreItemLabels.ts`
- Modify: `src/views/appraisal/composables/useManualEventEntry.ts`
- Modify: `src/views/appraisal/components/ManualEventEntrySection.vue`
- Modify: 懲處表單元件（`grep -rln "小過\|action_type" src/views --include="*.vue"` 定位）
- Test: 對應 `__tests__`（沿用 co-located 慣例）

- [ ] **Step 1: 建分支**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend
git fetch origin main && git checkout -b feat/appraisal-regulation-align-2026-06-11-fe origin/main
```
（若主 checkout 被佔用，依 workspace 慣例開 worktree 並重建 node_modules symlink——見 memory `feedback_frontend_worktree_node_modules_symlink`。）

- [ ] **Step 2: scoreItemLabels.ts 補九項（含 hint 語意在 label 後綴）**

```typescript
export const ITEM_CODE_LABELS = {
  LATE_EARLY: '遲到 / 早退',
  MISSING_PUNCH: '未打卡',
  LEAVE: '請假',
  ABSENTEEISM: '曠職',
  RETURNING_RATE_0915: '9/15 留校率（學期初）',
  RETURNING_RATE_0315: '3/15 留校率（學期末）',
  AFTER_CLASS_RATE: '才藝報名率',
  REWARD_PUNISH: '獎懲（功過相抵）',
  SCHOOL_MEETING_ABSENCE: '園務會議缺席（填時數，每次最多計 4 小時）',
  INSTITUTION_MEETING_0913: '9/13 機構會議研習（填時數，每次最多計 4 小時）',
  INSTITUTION_MEETING_1115: '11/15 機構會議研習（填時數，每次最多計 4 小時）',
  SELF_IMPROVEMENT_ACTIVITY: '自強活動（填時數，每次最多計 4 小時）',
  CHILD_ACCIDENT: '幼兒意外（填扣分 −1~−10，主管評議）',
  CLASS_HEADCOUNT_BONUS: '帶班人數加分',
  SPED: '特教加分（在園逾 4 個月）',
  STUDENT_WITHDRAWAL: '休學人數（當月月費未繳者）',
  STUDENT_REINSTATE: '復學人數',
  TRIAL_LEAVE: '試讀離園',
  CLASS_TRANSFER: '轉班',
  EXAM_RESULT: '檢測成績（填分值 ±10，依當學期公告）',
  RECRUIT_SCORE: '招生加分（填分值 0~20，依當學期公告）',
  SUPERVISOR_SCORE: '主管加分（填分值 0~10）',
  EXCELLENCE_NOMINATION: '呈報優異（每學期全園 1 位）',
  OTHER: '其他',
}
```

`AUTO_ITEM_CODES` Set 加 `'ABSENTEEISM'`、`'STUDENT_REINSTATE'`。

- [ ] **Step 3: useManualEventEntry / ManualEventEntrySection 接新項**

先讀兩檔，依其資料驅動方式（多半從 labels/AUTO set 推導 manual 清單）確認新手填項自動出現；MANUAL_DELTA 類（CHILD_ACCIDENT/EXAM_RESULT/RECRUIT_SCORE/SUPERVISOR_SCORE）的輸入框需允許負值與範圍 hint——新增常數：

```typescript
export const MANUAL_DELTA_RANGES: Record<string, { min: number; max: number }> = {
  CHILD_ACCIDENT: { min: -10, max: 0 },
  EXAM_RESULT: { min: -10, max: 10 },
  RECRUIT_SCORE: { min: 0, max: 20 },
  SUPERVISOR_SCORE: { min: 0, max: 10 },
}
```

數字輸入元件以 `:min`/`:max` 綁定（沿用該 section 既有 el-input-number 寫法）。

- [ ] **Step 4: 懲處表單加 merit 選項**

定位懲處建立表單的 action_type 下拉，選項清單加：嘉獎 `commendation`、小功 `minor_merit`、大功 `major_merit`（label 與後端 `ACTION_TYPE_LABELS` 一致）。

- [ ] **Step 5: Vitest＋typecheck**

新增 `src/views/appraisal/__tests__/scoreItemLabels.spec.ts`：

```typescript
import { describe, expect, it } from 'vitest'
import { AUTO_ITEM_CODES, ITEM_CODE_LABELS, MANUAL_DELTA_RANGES } from '../scoreItemLabels'

describe('scoreItemLabels 規章對齊', () => {
  it('九個新 code 都有標籤', () => {
    for (const code of ['ABSENTEEISM','STUDENT_WITHDRAWAL','STUDENT_REINSTATE','TRIAL_LEAVE',
      'CLASS_TRANSFER','EXAM_RESULT','RECRUIT_SCORE','SUPERVISOR_SCORE','EXCELLENCE_NOMINATION'])
      expect(ITEM_CODE_LABELS[code as keyof typeof ITEM_CODE_LABELS]).toBeTruthy()
  })
  it('auto 集合含曠職與復學', () => {
    expect(AUTO_ITEM_CODES.has('ABSENTEEISM')).toBe(true)
    expect(AUTO_ITEM_CODES.has('STUDENT_REINSTATE')).toBe(true)
  })
  it('MANUAL_DELTA 範圍與後端一致', () => {
    expect(MANUAL_DELTA_RANGES.CHILD_ACCIDENT).toEqual({ min: -10, max: 0 })
  })
})
```

```bash
npm run typecheck && npx vitest run src/views/appraisal
```
Expected: 0 error、appraisal 範圍測試綠（改了 view 後跑完整 `npx vitest run` 一次——co-located 測試教訓）。

- [ ] **Step 6: Commit（前端單獨一筆）**

```bash
git add src/views/appraisal/ src/views/<懲處表單檔>
git commit -m "feat(appraisal): 規章九項計分標籤/手填範圍/懲處功獎選項（對齊後端 2026-06-11 spec）"
```

---

### Task 16: 規章修訂建議書

**Files:**
- Create: `docs/sop/2026-06-11-hr-regulation-amendment-proposal.md`（ivy-backend）

- [ ] **Step 1: 撰寫**

```markdown
# 人事規章修訂建議（系統對齊後，2026-06-11）

依業主決策（D1-D5，見 specs/2026-06-11-appraisal-regulation-alignment-design.md），系統自 114下 起完全照第六篇執行。以下為規章文字需同步修訂處：

## 一、附表八與第六篇三處不一致（D3：以第六篇為準，附表八建議改）
1. 舊生留校率 100%：附表八「+4」→ 改「+6」（第六篇第五條(七)）；附表八的 <70% −6、<60% 解聘層建議刪除或併入第六篇正文（擇一，需執行長核定）。
2. 會議活動：附表八「出席率 90% +3、80% +2」→ 改第六篇第五條(十二)扣分制（每時數 −0.5、每次上限 −2）。
3. 才藝班：附表八「全期授課者 +2」→ 改第六篇第五條(九)各年級達成門檻 +2。
4. 附表八獎金率段落（主管 10000/5000 等）與第六篇一致，無需修訂。

## 二、會計作業需配合處（D1：發放時點照規章）
- 學期考核獎金停止於 3/15 單獨轉帳；上＋下學期考核獎金合併於「次一完整學年結束後之年終」（隔年 2/5 年終批次）發給。
- 時滯說明：上學期考核分數含 3/15 核計項目（舊生留校率），故任何年度的考核獎金最早能併入的年終為隔年 2 月——這是第五條(七)核計日設計的必然結果，非系統限制。

## 三、填報規則明確化建議
- 會議活動扣分：建議於第五條(十二)補註「每次活動計分時數以 4 小時為上限（即每次最高扣 2 分）」，與系統填報欄一致。
- 休學扣分（−2/人）之「當月月費未繳」由行政會計人工認定後填入系統（系統不自動判定繳費狀態）。

## 四、規章未明、系統暫不處理（另案）
- 全勤獎金（第三篇：C級副導 500/其餘 1500）：實務未見發放，規章與實務漂移，待裁定。
- 自主成長契約獎金（核薪表 620/600）：系統無此欄。
```

- [ ] **Step 2: Commit**

```bash
git add docs/sop/2026-06-11-hr-regulation-amendment-proposal.md
git commit -m "docs(hr): 規章修訂建議書（附表八對齊第六篇＋會計流程配合處）"
```

---

### Task 17: 收尾

- [ ] **Step 1: 後端全套件最終跑**

```bash
cd /Users/yilunwu/Desktop/ivy-backend/.worktrees/appraisal-reg-align && python3 -m pytest tests/ -q 2>&1 | tail -5
```

- [ ] **Step 2: 前端 typecheck＋lint＋完整 vitest**

```bash
cd /Users/yilunwu/Desktop/ivy-frontend && npm run typecheck && npx eslint . && npx vitest run 2>&1 | tail -5
```

- [ ] **Step 3: 彙整回報**

回報內容：兩 repo 分支名與 SHA、測試數、行為變更清單（LEAVE 聚合修正、獎金率五值、114下 起新規則）、**不 push**（押後給 user 跑 finish gate）、prod 注意事項（aprreg01 需 `alembic upgrade heads`；Zeabur 後端 SUSPENDED push 不會自動跑 migration）。

---

## Self-Review 紀錄

- **Spec 覆蓋**：§4 獎金率→Task 9；§5 規則逐項→Task 7/8/9；§6 新項目→Task 2/4/5/8/9；§7 生效→Task 9/11；§8 migration→Task 9；§9 API/前端→Task 10/15；§10 測試→Task 3-12；§11 交付→Task 16；§3 順手修→Task 13；§14 修訂全數落入對應 task。✔
- **無 placeholder**：各步皆附實碼或精確讀檔指令＋對齊指示（fixture/簽名以實檔為準屬必要的現場對齊，非 TBD）。✔
- **型別一致**：`apply_manual_delta(rule, value, role_group)`、`absent_days`、`reinstate_count`、`grade_name`、merit 三參數 keyword-only——Task 3/4/5/6/7/8 交叉引用一致。✔
