# 考核對齊 Excel（P1）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** 把考核計分對齊業主 `114上考核表` Excel：① 獎金基礎額對齊 Excel 3 組 + 解 effective-date silent-0 ② 新增特教生(SPED)手填加分 ③ 帶班人數改自動(超編制×+2) ④ 到職未滿 N 月跳過不計 ⑤ 補逐人 Excel 對帳測試 ⑥ 前端 manual entry 同步。

**Architecture:** 後端為主。多數改動是擴充既有純函式/資料：bonus/scoring rule 走 effective-dated seed migration；SPED 走 `ScoreItemCode` enum + seed（`item_code` 是 String 非 PG enum，免 ALTER TYPE）；帶班人數把 `CLASS_HEADCOUNT_BONUS` 移進 `AUTO_ITEM_CODES` + aggregator 算超編制 + rule_applier auto branch；到職 gate 加在 recompute 迴圈（用已存在的 `AppraisalParticipant.hire_months_in_cycle`）。前端只改 manual 項清單。

**Tech Stack:** FastAPI、SQLAlchemy、Alembic、Pydantic v2、pytest（`test_db_session` SQLite in-memory；engine 純函式測試）、Vue 3 + TS（前端 manual entry）。

**業主已定決策（brainstorm 2026-06-04）：**
1. 獎金 3 組對齊：SUPERVISOR 8000/5000、HEAD_TEACHER 6000/4000、**ASSISTANT 5500/3500**、**STAFF 6000/4000**、**COOK 6000/4000**。
2. 帶班人數自動 = `max(0, 班級在籍 − 編制) × +2`，**無上限**（total clamp 110 自然封頂）。編制用 `Classroom.capacity`。
3. 特教生：新增手填項 SPED，`count × +2`。
4. 請假：**維持每天 -1**（不改）。
5. 舊生率：**維持留校率+tier**，補逐人對帳測試。
6. 到職未滿：cycle 結束時在職 `hire_months_in_cycle < N`（**N=2，可設定**）→ 跳過不產 summary。

**effective date 統一用 `2025-08-01`**（民國114年8月1日 = 114學年上起算），確保覆蓋真實 114上 cycle 的 base_score_calc_date（~2025-09-15）。

---

## ⚠ 執行前修正（advisor review + 資料查證 2026-06-04）

1. **silent-0 確認為真 bug**（dev DB 查證）：114下 cycle base_score_calc_date=2026-03-15、114上≈2025-09-15。故 bonus rates(effective 2026-08-01) 對 114上+114下 **都** 查不到；scoring rules(effective 2026-01-01) 對 114上 查不到。Task 1 的 2025-08-01 seed 解此真 bug。
2. **🚫 Task 3（帶班人數 auto）DEFERRED**：dev DB `classrooms.capacity` 全為預設 30（未維護成真實 per-class 編制）。用 capacity 會讓 `max(0, 在籍−30)=0` 對每班都成立 → 死功能且測試假綠。真實 per-class 編制在年終 `ClassEnrollmentTarget.head_count_target`，但那是**年終週期鍵**（半年考核↔年度年終的對應未定）。**此 task 移出本批次**，待業主/設計決定編制來源後另做（連帶 Task 6 的 CLASS_HEADCOUNT 前端改動一併 defer）。
3. **Task 5 修正**：刪除「reconcile 留校率 vs 休學公式」說法（業主已選維持留校率＝接受兩者分歧，無可 reconcile）；改補一個 **DB-level sync→recompute 整合測試**驗 auto-derivation（留校率→tier→delta）對 live data 出合理值——目前所有 appraisal 測試非純函式即 Excel-parse，**沒有人跑過 live 自動推導路徑**。
4. **#5 後果明講**：Task 1 更新 2026-08-01 列＝**115 學年獎金也跟著調高**（助理/行政/廚師司機）。這超出「對齊 114上」但避免 114>115 不一致，為刻意選擇。
5. **worktree 基底 = local main（非 origin/main）**：P1 含 migration，down_revision 必須接 local main 單一 head **`acadterm01`**；走 memory「migration 例外條款」worktree-off-local-main。

**本批次實際執行：Task 1 / 2 / 4 / 5 / 6(僅 SPED) / 7。Task 3 deferred。**

---

## File Structure

| 檔案 | 變更 |
|---|---|
| `alembic/versions/<new>_appraisal_excel_align_p1.py`（**Create**） | seed：① 對齊 bonus rates(2025-08-01 + 更新 2026-08-01) ② 2025-08-01 scoring rules 全集(14+SPED) ③ SPED catalog(若需) |
| `models/appraisal.py`（**Modify**） | `ScoreItemCode` 加 `SPED`；`AUTO_ITEM_CODES` 加 `CLASS_HEADCOUNT_BONUS` |
| `services/appraisal/status_aggregator.py`（**Modify**） | `ParticipantStatus` 加 `headcount_over_target`；Classroom query 取 `capacity`；算 `max(0, final_count − capacity)` |
| `services/appraisal/rule_applier.py`（**Modify**） | `_apply_auto_item` 加 `CLASS_HEADCOUNT_BONUS` branch（`headcount_over_target × per_unit_delta`） |
| `api/appraisal/__init__.py`（**Modify**） | recompute 迴圈加到職 gate + 清除被 skip 者 stale summary |
| `tests/test_appraisal_*.py`（**Create/Modify**） | bonus 對齊、SPED、帶班人數 auto、到職 gate、effective-date 非空、逐人 Excel 對帳 |
| `ivy-frontend/src/views/appraisal/composables/useManualEventEntry.ts`（**Modify**） | +SPED、−CLASS_HEADCOUNT_BONUS |
| `ivy-frontend/src/views/appraisal/scoreItemLabels.ts`（**Modify**） | +SPED label；`AUTO_ITEM_CODES` 加 CLASS_HEADCOUNT_BONUS |

**執行分支**：後端 Task 1–5,7 走 `feat/appraisal-excel-align-p1-be`；前端 Task 6 走 `feat/appraisal-excel-align-p1-fe`（各自 worktree，BE 先）。

---

## Task 1: 獎金率對齊 + effective-date 覆蓋（解 silent-0）

**Files:**
- Create: `alembic/versions/<rev>_appraisal_excel_align_p1.py`
- Test: `tests/test_appraisal_excel_align_p1.py`

實作前先 `git -C <worktree> log --oneline alembic/versions | head` 找出當前 appraisal 相關 head 當 down_revision（多 head 時用 merge 或接最新 appraisal 鏈尾）。

- [ ] **Step 1: 失敗測試** — `tests/test_appraisal_excel_align_p1.py`：

```python
"""P1 對齊 Excel：bonus 率對齊 + effective-date 覆蓋 114學年上。"""
from datetime import date
from decimal import Decimal

from services.appraisal.engine import BonusRateLookup, compute_bonus_amount
from models.appraisal import RoleGroup, Grade

# 對齊後預期值（Excel 3 組）
ALIGNED = {
    (RoleGroup.SUPERVISOR, Grade.OUTSTANDING): Decimal("8000"),
    (RoleGroup.SUPERVISOR, Grade.GOOD): Decimal("5000"),
    (RoleGroup.HEAD_TEACHER, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.HEAD_TEACHER, Grade.GOOD): Decimal("4000"),
    (RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("5500"),
    (RoleGroup.ASSISTANT, Grade.GOOD): Decimal("3500"),
    (RoleGroup.STAFF, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.STAFF, Grade.GOOD): Decimal("4000"),
    (RoleGroup.COOK, Grade.OUTSTANDING): Decimal("6000"),
    (RoleGroup.COOK, Grade.GOOD): Decimal("4000"),
}


def _lookup(effective="2025-08-01"):
    return BonusRateLookup(
        rates={(effective, rg, gr): amt for (rg, gr), amt in ALIGNED.items()}
    )


def test_assistant_outstanding_aligned_to_5500():
    # 助理 優等 100 分 → 5500（對齊 Excel，原 4500）
    b = compute_bonus_amount(Decimal("100"), Grade.OUTSTANDING,
                             RoleGroup.ASSISTANT, _lookup(), date(2025, 9, 15))
    assert b == Decimal("5500.00")


def test_cook_good_aligned_to_4000():
    b = compute_bonus_amount(Decimal("100"), Grade.GOOD,
                             RoleGroup.COOK, _lookup(), date(2025, 9, 15))
    assert b == Decimal("4000.00")


def test_114_cycle_date_resolves_rate_not_silent_zero():
    # 114上 base date 2025-09-15 必須查得到 rate（effective_from 2025-08-01 ≤ 該日）
    lk = _lookup("2025-08-01")
    assert lk.resolve(date(2025, 9, 15), RoleGroup.HEAD_TEACHER, Grade.GOOD) == Decimal("4000")
```

- [ ] **Step 2: 跑測試確認失敗** — `python3 -m pytest tests/test_appraisal_excel_align_p1.py -v`（這 3 個純函式測試其實只驗 engine + 預期值，會 PASS——它們是「對齊後預期」的 spec lock，非紅燈。真正的紅→綠在 Step 3 的 migration + Step 4 的 DB-level 測試）。改為先寫 Step 3 migration，再加 Step 4 的 DB 測試當紅燈。

- [ ] **Step 3: 寫 migration** `alembic/versions/<rev>_appraisal_excel_align_p1.py`：

```python
"""appraisal P1：對齊 Excel 獎金 3 組 + scoring rules 覆蓋 114學年上 + SPED。

Revision ID: <rev>
Revises: <當前 appraisal head>
"""
import sqlalchemy as sa
from alembic import op

revision = "<rev>"
down_revision = "<head>"
branch_labels = None
depends_on = None

ALIGN_EFFECTIVE = "2025-08-01"          # 114學年上起算
RULES_EFFECTIVE = "2025-08-01"
EXISTING_RATE_EFFECTIVE = "2026-08-01"  # 既有 seed，需更新為對齊值

# 對齊 Excel 3 組
ALIGNED_RATES = [
    ("SUPERVISOR", "OUTSTANDING", 8000), ("SUPERVISOR", "GOOD", 5000),
    ("HEAD_TEACHER", "OUTSTANDING", 6000), ("HEAD_TEACHER", "GOOD", 4000),
    ("ASSISTANT", "OUTSTANDING", 5500), ("ASSISTANT", "GOOD", 3500),
    ("STAFF", "OUTSTANDING", 6000), ("STAFF", "GOOD", 4000),
    ("COOK", "OUTSTANDING", 6000), ("COOK", "GOOD", 4000),
]

# 14 + SPED 規則（與 aprcal001 DEFAULT_RULES 同結構；item_code 是 String 免 enum 變更）
_TEACHING = ["HEAD_TEACHER", "ASSISTANT"]
RULES = [
    ("LATE_EARLY", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("MISSING_PUNCH", "PER_UNIT", {"per_unit_delta": -0.25}, None),
    ("LEAVE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("RETURNING_RATE_0915", "TIER", {"input_field": "retention_rate", "tiers": [
        {"min": 100, "delta": 0}, {"min": 95, "delta": -1.7}, {"min": 0, "delta": -3.0}]}, _TEACHING),
    ("RETURNING_RATE_0315", "TIER", {"input_field": "retention_rate", "tiers": [
        {"min": 100, "delta": 6.0}, {"min": 95, "delta": 0.0}, {"min": 90, "delta": -1.7},
        {"min": 80, "delta": -3.0}, {"min": 0, "delta": -6.0}]}, _TEACHING),
    ("AFTER_CLASS_RATE", "FLAT_THRESHOLD", {"input_field": "activity_rate",
        "threshold": 80, "above_delta": 2.0, "below_delta": 0}, _TEACHING),
    ("REWARD_PUNISH", "DISCIPLINARY_TIERED", {"warning_delta": -1.0, "minor_delta": -3.0, "major_delta": -10.0}, None),
    ("SCHOOL_MEETING_ABSENCE", "PER_UNIT", {"per_unit_delta": -1.0}, None),
    ("INSTITUTION_MEETING_0913", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("INSTITUTION_MEETING_1115", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("SELF_IMPROVEMENT_ACTIVITY", "PER_UNIT", {"per_unit_delta": -2.0}, None),
    ("CHILD_ACCIDENT", "PER_UNIT", {"per_unit_delta": -3.0}, None),
    ("CLASS_HEADCOUNT_BONUS", "PER_UNIT", {"per_unit_delta": 2.0}, None),
    ("SPED", "PER_UNIT", {"per_unit_delta": 2.0}, None),  # 特教生 +2/位
    ("OTHER", "PER_UNIT", {"per_unit_delta": 0}, None),
]


def _has_table(bind, name):
    import sqlalchemy as _sa
    return name in _sa.inspect(bind).get_table_names()


def upgrade() -> None:
    bind = op.get_bind()
    import json

    # ① bonus rates：插 2025-08-01 對齊值（冪等）
    if _has_table(bind, "appraisal_bonus_rates"):
        existing = {(r[0].isoformat(), r[1], r[2]) for r in bind.execute(sa.text(
            "SELECT effective_from, role_group, grade FROM appraisal_bonus_rates"
        )).fetchall()}
        for rg, gr, amt in ALIGNED_RATES:
            if (ALIGN_EFFECTIVE, rg, gr) not in existing:
                bind.execute(sa.text(
                    "INSERT INTO appraisal_bonus_rates (effective_from, role_group, grade, base_amount)"
                    " VALUES (:e,:rg,:gr,:a)"), {"e": ALIGN_EFFECTIVE, "rg": rg, "gr": gr, "a": amt})
        # 更新既有 2026-08-01 列為對齊值（115學年也採 Excel 值）
        for rg, gr, amt in ALIGNED_RATES:
            bind.execute(sa.text(
                "UPDATE appraisal_bonus_rates SET base_amount=:a"
                " WHERE effective_from=:e AND role_group=:rg AND grade=:gr"),
                {"a": amt, "e": EXISTING_RATE_EFFECTIVE, "rg": rg, "gr": gr})

    # ② scoring rules：插 2025-08-01 全集(14+SPED)（冪等；覆蓋 114上 base date）
    if _has_table(bind, "appraisal_scoring_rules"):
        existing_rules = {(r[0], r[1].isoformat()) for r in bind.execute(sa.text(
            "SELECT item_code, effective_from FROM appraisal_scoring_rules"
        )).fetchall()}
        for code, rtype, cfg, roles in RULES:
            if (code, RULES_EFFECTIVE) in existing_rules:
                continue
            bind.execute(sa.text(
                "INSERT INTO appraisal_scoring_rules (item_code, effective_from, rule_type, rule_config, applies_to_role_groups)"
                " VALUES (:c,:e,:t, CAST(:cfg AS JSONB), CAST(:roles AS JSONB))"),
                {"c": code, "e": RULES_EFFECTIVE, "t": rtype,
                 "cfg": json.dumps(cfg), "roles": json.dumps(roles) if roles else None})


def downgrade() -> None:
    bind = op.get_bind()
    if _has_table(bind, "appraisal_bonus_rates"):
        bind.execute(sa.text("DELETE FROM appraisal_bonus_rates WHERE effective_from=:e"),
                     {"e": ALIGN_EFFECTIVE})
        # 既有 2026-08-01 值還原（原 seed 值）
        for rg, gr, amt in [("ASSISTANT","OUTSTANDING",4500),("ASSISTANT","GOOD",3000),
                            ("STAFF","OUTSTANDING",5000),("STAFF","GOOD",3500),
                            ("COOK","OUTSTANDING",3500),("COOK","GOOD",2500)]:
            bind.execute(sa.text("UPDATE appraisal_bonus_rates SET base_amount=:a"
                " WHERE effective_from='2026-08-01' AND role_group=:rg AND grade=:gr"),
                {"a": amt, "rg": rg, "gr": gr})
    if _has_table(bind, "appraisal_scoring_rules"):
        bind.execute(sa.text("DELETE FROM appraisal_scoring_rules WHERE effective_from=:e"),
                     {"e": RULES_EFFECTIVE})
```

> 注意（CLAUDE.md）：含 backfill/seed 的 migration 合併前要手動 `alembic upgrade heads`；`op.execute` 內勿用裸 `:word` 字面冒號（這裡都走 bindparams，OK）。

- [ ] **Step 4: DB-level 紅→綠測試**（migration 套用後 rate 對齊）。在 `tests/test_appraisal_excel_align_p1.py` 加：

```python
def test_migration_seeds_aligned_rates(test_db_session):
    """套表後（conftest create_all + 本 migration seed）2025-08-01 對齊值存在。"""
    from models.appraisal import AppraisalBonusRate
    db = test_db_session
    # 模擬 migration seed（create_all 不跑 migration，故此測試直接驗 seed 邏輯的預期值表）
    # 實作者：若 conftest 走 create_all 不跑 data migration，改為對 ALIGNED 常數的單元驗證，
    # 並在 test_appraisal_calibrate_migration.py 風格的 migration 測試（若存在）驗 upgrade()。
```

> 實作者注意：conftest 用 `Base.metadata.create_all` **不跑 data migration**（research F）。故 seed 值無法靠 create_all 進 DB。驗證策略二選一：(a) 若 repo 有「跑單一 migration upgrade()」的測試 harness（看 `tests/test_appraisal_calibrate_migration.py`），照它驗 `upgrade()` 寫入對齊值；(b) 否則以 Step 1 的純函式測試 + 常數表 lock 對齊值，DB seed 由 `alembic upgrade heads` 在 dev/prod 驗。先讀該檔決定。

- [ ] **Step 5: Commit** — `git -C <worktree> add alembic/versions/<rev>_*.py tests/test_appraisal_excel_align_p1.py` → commit `feat(appraisal): 對齊 Excel 獎金3組+scoring rules 覆蓋114學年上`。

---

## Task 2: 新增特教生 SPED 手填項

**Files:**
- Modify: `models/appraisal.py`（`ScoreItemCode` 加 `SPED`）
- Test: `tests/test_appraisal_sped.py`
- （scoring rule 已在 Task 1 migration seed）

- [ ] **Step 1: 失敗測試** `tests/test_appraisal_sped.py`：

```python
"""SPED 特教生手填加分：count × +2，歸 MANUAL。"""
from decimal import Decimal
from models.appraisal import ScoreItemCode, AUTO_ITEM_CODES, MANUAL_ITEM_CODES


def test_sped_in_enum_and_manual():
    assert ScoreItemCode.SPED.value == "SPED"
    assert ScoreItemCode.SPED in MANUAL_ITEM_CODES
    assert ScoreItemCode.SPED not in AUTO_ITEM_CODES


def test_sped_per_unit_delta_plus2():
    from services.appraisal.rule_applier import apply_per_unit
    from models.appraisal import AppraisalScoringRule
    rule = AppraisalScoringRule(item_code="SPED", rule_type="PER_UNIT",
                                rule_config={"per_unit_delta": 2.0},
                                applies_to_role_groups=None)
    # 2 位特教生 → +4
    assert apply_per_unit(rule, Decimal("2"), None) == Decimal("4.0")
```

> 實作者：先讀 `services/appraisal/rule_applier.py` 的 `apply_per_unit` 簽名確認參數順序與回傳型別，必要時調整測試呼叫。

- [ ] **Step 2: 跑測試確認失敗** — `python3 -m pytest tests/test_appraisal_sped.py -v`（`AttributeError: SPED`）。

- [ ] **Step 3: 實作** — `models/appraisal.py`，在 `ScoreItemCode` 的 `OTHER` 前加一行（manual 區）：

```python
    SPED = "SPED"  # 特教生加分（手填 count × +2，對齊 Excel P 欄）
```

（`MANUAL_ITEM_CODES = frozenset(set(ScoreItemCode) - AUTO_ITEM_CODES)` 自動納入；`apply_per_unit` 自動處理 manual PER_UNIT。SPED 的 scoring rule 已在 Task 1 migration seed。）

- [ ] **Step 4: 跑測試通過** — `python3 -m pytest tests/test_appraisal_sped.py -v` → PASS。

- [ ] **Step 5: Commit** — `feat(appraisal): 新增特教生 SPED 手填加分項(+2/位)`。

---

## Task 3: 帶班人數 改自動（超編制 × +2）— 🚫 DEFERRED（不在本批次）

> **DEFERRED**：編制來源未定（`Classroom.capacity` 全 30 無效；年終 `ClassEnrollmentTarget.head_count_target` 是年度週期鍵，半年↔年度對應需業主/設計決定）。下方步驟保留供日後接續，本批次**不執行**。連帶 Task 6 的 CLASS_HEADCOUNT_BONUS 前端改動一併 defer。

**Files:**
- Modify: `models/appraisal.py`（`AUTO_ITEM_CODES` 加 `CLASS_HEADCOUNT_BONUS`）
- Modify: `services/appraisal/status_aggregator.py`（`ParticipantStatus.headcount_over_target` + Classroom.capacity）
- Modify: `services/appraisal/rule_applier.py`（`_apply_auto_item` 加 branch）
- Test: `tests/test_appraisal_headcount_auto.py`

- [ ] **Step 1: 失敗測試** `tests/test_appraisal_headcount_auto.py`（aggregator 算超編制 + rule 出 delta）。先讀 `tests/test_appraisal_status_aggregator.py` 抄 `_make_employee/_make_cycle/_make_classroom/_make_participant` helper 與 `test_db_session` 用法；`_make_classroom` 需傳 `capacity`：

```python
from decimal import Decimal
from datetime import date
# ...import helpers 同 test_appraisal_status_aggregator.py 慣例...
from services.appraisal.status_aggregator import aggregate_cycle_status


def test_headcount_over_target_computed(test_db_session):
    s = test_db_session
    # 班級 capacity=12，seed 14 位 active 學生 → 超編制 2
    # （照 aggregator 測試慣例 seed Classroom(capacity=12) + 14 Student active + participant 帶班）
    # ...seed...
    statuses = aggregate_cycle_status(s, cycle)
    st = next(x for x in statuses if x.employee_id == teacher.id)
    assert st.headcount_over_target == 2


def test_headcount_under_target_is_zero(test_db_session):
    # capacity=20，seed 17 active → max(0, 17-20)=0
    ...
    assert st.headcount_over_target == 0
```

加 rule_applier 測試：

```python
def test_class_headcount_auto_delta(test_db_session):
    from services.appraisal.rule_applier import compute_all_deltas
    # seed 超編制 2 的帶班老師 → CLASS_HEADCOUNT_BONUS delta = +4
    ...
    deltas = compute_all_deltas(s, cycle)
    assert deltas[(participant.id, "CLASS_HEADCOUNT_BONUS")].delta == Decimal("4.0")
```

- [ ] **Step 2: 跑測試確認失敗** — `AttributeError: headcount_over_target` / delta 為 0（仍 manual）。

- [ ] **Step 3: 實作**
  - `models/appraisal.py`：`AUTO_ITEM_CODES` frozenset 加 `ScoreItemCode.CLASS_HEADCOUNT_BONUS`（則自動移出 MANUAL）。
  - `status_aggregator.py`：
    - `ParticipantStatus` dataclass 加 `headcount_over_target: int = 0`。
    - 把既有 `session.query(Classroom).filter(Classroom.id.in_(classroom_ids))` 的結果由 `{c.id: c.name}` 擴成同時保留 `c.capacity`（例 `classroom_by_id = {c.id: c for c in ...}`）。
    - 對每位帶班 participant：`actual = final_count`（該班期末 active；retention 已算）；`cap = classroom_by_id[cid].capacity or 0`；`headcount_over_target = max(0, actual - cap)`；填入 ParticipantStatus。非帶班(classroom_id None)→ 0。
  - `rule_applier.py` `_apply_auto_item`：加 branch — 當 `code == "CLASS_HEADCOUNT_BONUS"`：`delta = apply_per_unit(rule, Decimal(status.headcount_over_target), role)`（PER_UNIT +2 × 超編制人數），raw=超編制人數，note=f"超編制 {n} 人"。

> 讀 `_apply_auto_item` 現有結構確認它怎麼 dispatch 各 auto code（retention/activity/attendance/disciplinary），照同模式加 headcount branch。

- [ ] **Step 4: 跑測試通過** → aggregator + rule 測試綠。

- [ ] **Step 5: Commit** — `feat(appraisal): 帶班人數改自動(超編制×+2,編制取 Classroom.capacity)`。

---

## Task 4: 到職未滿 N 月跳過不計

**Files:**
- Modify: `api/appraisal/__init__.py`（recompute 迴圈 gate）
- Modify: `config/`（可選：N 設定值）或在 recompute 用常數 `MIN_TENURE_MONTHS = Decimal("2")`
- Test: `tests/test_appraisal_tenure_gate.py`

- [ ] **Step 1: 失敗測試** `tests/test_appraisal_tenure_gate.py`：seed cycle + 2 participants（一個 hire_months_in_cycle=1.0、一個=5.0），呼叫 recompute 端點（或抽出的 recompute 函式），斷言 hire_months=1.0 者**無 summary**、=5.0 者**有 summary**。

```python
def test_under_tenure_skipped_no_summary(test_db_session, ...):
    # participant A hire_months_in_cycle=Decimal("1.0") → skip
    # participant B hire_months_in_cycle=Decimal("5.0") → summary 產生
    # 呼叫 recompute（看 api 測試慣例：用 TestClient 或直接呼叫函式）
    ...
    assert summary_count_for(A) == 0
    assert summary_count_for(B) == 1
```

> 讀既有 appraisal api 測試（`tests/test_appraisal_*api*` 或 recompute 測試）確認呼叫 recompute 的方式（authed TestClient vs 直接函式）。

- [ ] **Step 2: 跑測試確認失敗** — A 目前也會產 summary（無 gate）。

- [ ] **Step 3: 實作** — `api/appraisal/__init__.py` recompute 迴圈：

```python
from decimal import Decimal
MIN_TENURE_MONTHS = Decimal("2")  # 到職未滿此月數(cycle 結束時)不計考核(對齊 Excel)

# 迴圈內，compute_summary 之前：
for p in participants:
    if p.hire_months_in_cycle is not None and p.hire_months_in_cycle < MIN_TENURE_MONTHS:
        # 對齊 Excel「未滿/未簽約不計算考核」：跳過 + 清除既有 stale summary
        existing = session.query(AppraisalSummary).filter_by(
            cycle_id=cycle_id, participant_id=p.id).first()
        if existing is not None:
            session.delete(existing)
        continue
    # ...原 compute_summary 流程...
```

- [ ] **Step 4: 跑測試通過** → A 無 summary、B 有。

- [ ] **Step 5: Commit** — `feat(appraisal): 到職未滿2月跳過不計考核(對齊 Excel)`。

---

## Task 5: 逐人 Excel 對帳鎖 + auto-derivation DB 整合測試

**Files:**
- Modify/Create: `tests/test_appraisal_excel_reconcile_114.py`（純函式對帳鎖）
- Create: `tests/test_appraisal_auto_derive_integration.py`（DB-level sync→recompute）

兩個目的：(A) 純函式鎖對齊後 rate 逐人重現 Excel 數字（不回歸）；(B)（advisor #3）補 **DB-level 整合測試**驗 auto-derivation——目前所有 appraisal 測試非純函式即 Excel-parse，沒人跑過 live `sync→recompute`。先讀 `tests/test_appraisal_engine.py`/`test_appraisal_excel.py`/`test_appraisal_status_aggregator.py` 既有 case 避免重複。

> 註：不宣稱「reconcile 留校率 vs Excel 休學公式」——業主已選維持留校率（接受兩者語意分歧）。本 task 驗的是「對齊後 rate + 現行留校率 tier」對 live data 出合理值，非兩公式等價。

- [ ] **Step 1: 寫對帳測試** — 至少涵蓋：
  - 蔡宜倩 82.35 甲等 HEAD_TEACHER → 對齊後 4000×0.8235=3294.00（不變，回歸）
  - 助理職某員 優等 → 用對齊 rate 5500 驗 bonus（新行為）
  - 留校率 tier：陳品棻 100% → +6.0、王雅玲 ~95% → 對應 tier delta（對 Excel M 欄）
  - 蔡佩汶 57.1 丁等 → bonus 0（回歸）

```python
# 範例（純函式，hardcode 對齊 BonusRateLookup）
def test_assistant_outstanding_uses_aligned_5500():
    rates = BonusRateLookup(rates={("2025-08-01", RoleGroup.ASSISTANT, Grade.OUTSTANDING): Decimal("5500")})
    r = compute_summary(actual_enrollment=160, enrollment_target=160,
                        score_deltas=[], role_group=RoleGroup.ASSISTANT,
                        bonus_rates=rates, on_date=date(2025, 9, 15))
    assert r.grade == Grade.OUTSTANDING  # base 100
    assert r.bonus_amount == Decimal("5500.00")
```

- [ ] **Step 2: DB-level auto-derivation 整合測試** `tests/test_appraisal_auto_derive_integration.py`（advisor #3）。照 `tests/test_appraisal_status_aggregator.py` 的 `test_db_session` + seed helper 慣例：seed cycle + 帶班老師 participant + 班級 + 學生(期初/期末 active) + 幾筆 Attendance/LeaveRecord，呼叫 `sync_score_items`（或 `compute_all_deltas`）→ `recompute`，斷言推導出的 retention tier delta、attendance delta 為**合理具體值**（非全 0、非爆掉）。這是唯一驗 live 自動推導路徑的測試。

```python
def test_live_sync_recompute_produces_sane_deltas(test_db_session, ...):
    # seed 帶班老師 + 班級 + 期初10/期末9 active 學生(留校率90%) + 3 筆遲到
    # → sync_score_items → 斷言 RETURNING_RATE delta 對應 90% tier(-1.7)、LATE_EARLY=-0.75
    ...
```

> 實作者：先讀 `api/appraisal/__init__.py` 的 `sync_score_items` 端點與既有 aggregator 測試 seed，確認呼叫方式（TestClient vs 直接函式）與 retention 90% 對應 tier。

- [ ] **Step 3: 跑測試** → 全綠（純函式 lock + DB 整合，確認對齊未破壞既有 Excel 對帳且 live 路徑出合理值）。

- [ ] **Step 4: Commit** — `test(appraisal): 逐人 Excel 對帳鎖 + auto-derivation DB 整合測試`。

---

## Task 6: 前端 manual entry 同步（**前端 worktree/分支**）

**Files（ivy-frontend）:**
- Modify: `src/views/appraisal/composables/useManualEventEntry.ts`
- Modify: `src/views/appraisal/scoreItemLabels.ts`

> **本批次僅做 SPED**（CLASS_HEADCOUNT_BONUS 前端改動隨 Task 3 deferred——後端仍 manual，前端維持現狀）。

- [ ] **Step 1**（若有對應 vitest 先寫/調整）— 多數為常數清單，視既有測試覆蓋決定。
- [ ] **Step 2: 改 `useManualEventEntry.ts`**：
  - `MANUAL_ITEM_CODES` 陣列：**加 `'SPED'`**（不動 CLASS_HEADCOUNT_BONUS）。
  - `MANUAL_LABEL`：加 `SPED: '特教加分'`。
- [ ] **Step 3: 改 `scoreItemLabels.ts`**：
  - `ITEM_CODE_LABELS` 加 `SPED: '特教加分'`。（不動 AUTO_ITEM_CODES——CLASS_HEADCOUNT 仍 manual）
- [ ] **Step 4: typecheck + 既有 appraisal 前端測試** — `npm run typecheck` + `npx vitest run src/views/appraisal`（綠）。
- [ ] **Step 5: Commit**（前端 repo）— `feat(appraisal): manual entry 新增特教生 SPED 手填項`。

> 後端契約若改 response（本 P1 未改 schema，僅資料/規則），通常不需 `gen:api`；但若 SPED 出現在任何 OpenAPI enum/response，跑 `dump_openapi`+`gen:api` 同步 schema.d.ts。

---

## Task 7: 回歸 gate（controller 自跑）

- [ ] **Step 1: 跑既有 appraisal 全套件 + 新測試**（後端 worktree）：
  `python3 -m pytest tests/test_appraisal_engine.py tests/test_appraisal_excel.py tests/test_appraisal_status_aggregator.py tests/test_appraisal_rule_applier.py tests/test_appraisal_excel_align_p1.py tests/test_appraisal_sped.py tests/test_appraisal_headcount_auto.py tests/test_appraisal_tenure_gate.py tests/test_appraisal_excel_reconcile_114.py -q`
  Expected: 全綠；既有 Excel 對帳數字不回歸（蔡宜倩 3294、蔡佩汶 丁等 0）。
- [ ] **Step 2: 跑一次 migration upgrade 驗證**（dev DB 或臨時）— `alembic upgrade heads` 不報錯（含 backfill 規則）。

---

## Self-Review（writing-plans 完成後自查）
- **Spec coverage**：對應 spec §4 六項——①獎金3組(Task1)②帶班人數auto(Task3)③SPED(Task2)④請假不改(無task,刻意)⑤舊生率維持+對帳(Task5)⑥到職gate(Task4)；effective-date silent-0(Task1)；前端(Task6)。
- **Placeholder**：Task1 Step4 與 Task3/4 Step1 標明「先讀 X 檔確認慣例」——這是 codebase-specific 不可預先 paste 的部分(test harness/helper 簽名)，已具體指出讀哪個檔。其餘 step 皆具完整 code。
- **Type/命名一致**：`headcount_over_target`（aggregator 欄位）跨 Task3 aggregator/rule_applier/test 一致；`MIN_TENURE_MONTHS`（Task4）；對齊 rate 值跨 Task1/5 一致。
- **風險**：① effective-date 假設真實 114上 cycle base date≈2025-09-15；若實際用 2026 日期則 2025-08-01 仍 ≤ 之，安全(取 max ≤ date 那筆)。② 編制用 Classroom.capacity 非年終 head_count_target——若業主要年終編制需改 Task3 來源。③ SPED/CLASS_HEADCOUNT 的 catalog 表(若 UI 讀 catalog)可能需同步一筆——Task2/3 實作前 grep `appraisal_score_item_catalog` 確認是否需補。

## 後續（不在 P1）
- P0b 其餘 provenance provider；P2/P3 前端即時 grid+抽屜+轉帳名冊快照。
- 編制來源若改年終 head_count_target、SPED 中文標籤、N 月門檻值 → 業主可調。
