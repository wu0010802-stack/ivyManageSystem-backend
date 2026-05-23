# 學期切換 hook 後端設計（2026-05-22）

## 1. 背景與動機

幼稚園的「學期」目前在後端是**日期函式推算結果**，不是持久化的設定：

- `utils/academic.py:resolve_current_academic_term()` 以「今天月份 ≥ 8」推當前學期
- 沒有「目前學期」的單一 source of truth、沒有切換 hook
- 8/1 0:00 之後新建 `Classroom` 會自動掛新學期，但**舊 classroom 不會自動 carry-over**，導致 `Student.classroom_id` 還指向上學期的 row
- `leave_quotas` 以**西元年**為週期（year 欄位 + 1/1 重設），與**學年**（民國 8 月起）不對齊
- 招生漏斗 Phase A 已建 `AcademicTerm` 表 + `/academic-terms` CRUD + 半自動推進 scheduler（依 `start_date` 把報到學生 enrolled→active），但**沒有 emit 學期切換事件**，所以下游模組（classroom、leave quota、活動報名）無法接 hook

本設計補上：

1. `AcademicTerm` 加 `is_current` flag（admin 顯式翻牌）
2. `utils/academic.py` 切換成「優先讀 DB、找不到 fallback 日期推算」
3. `utils/term_events.py` 提供同步 hooks registry（`@on_term_changed` decorator + `fire_term_changed()`）
4. `POST /academic-terms/{id}/set-current` toggle endpoint
5. 三個 subscriber：classroom carry-over、leave_quota cutover、activity semester tag reset（placeholder）
6. `leave_quotas` 加 `school_year` 欄位（民國學年），cutover 由 `term.changed` 跨學年觸發

範圍對齊 user 在 brainstorming 選擇的 C（全包，含特休/補休對齊學年）+ A（接著在招生漏斗 worktree 上）。

## 2. 設計總覽

```
┌──────────────────────────────────────────────┐
│ POST /api/academic-terms/{id}/set-current    │  ← admin「正式開新學期」按鈕
└────────────────────┬─────────────────────────┘
                     │ same DB transaction (get_session_dep)
                     ▼
   ┌──────────────────────────┐
   │ AcademicTerm.is_current  │  ← UPDATE 舊 row.is_current=false
   │  toggle (partial unique  │     UPDATE 新 row.is_current=true
   │  index 保證 singleton)   │     session.flush()
   └────────────┬─────────────┘
                │
                ▼
   ┌──────────────────────────┐
   │ fire_term_changed(       │  ← utils/term_events.py
   │   old, new, session)     │     依序串註呼叫所有 @on_term_changed
   └────────────┬─────────────┘
                │  任一 subscriber raise → caller dep 觸發 rollback
                ▼
   ┌────────────────────────────────────────────────────────┐
   │ Subscriber 1: classroom_carry_over                      │
   │   if same school_year (1→2):                            │
   │     複製 active classroom rows → 新學期                  │
   │     UPDATE Student.classroom_id 指向新 row (active only)│
   │   else (跨學年 / 非典型): no-op + log                    │
   ├────────────────────────────────────────────────────────┤
   │ Subscriber 2: leave_quota_cutover                       │
   │   if 跨學年 (X-2 → X+1-1): 為每位 active 員工生 new row │
   │     - annual: hire_date → new.start_date 年資算         │
   │     - 法定其他: STATUTORY_QUOTA_HOURS                    │
   │     - compensatory: 舊 row 結餘 carry-over               │
   │   else: no-op + log                                     │
   ├────────────────────────────────────────────────────────┤
   │ Subscriber 3: activity_semester_tag_reset (placeholder) │
   │   logger.info 預留 hook，無實質動作                       │
   └────────────────────────────────────────────────────────┘
```

關鍵設計決策：

| 項目 | 決策 | 理由 |
|---|---|---|
| Current term source of truth | `AcademicTerm.is_current` partial unique index | 顯式 + 簡單 + admin 可控；避免日期推算的「假期落空」 |
| Fallback | 找不到 `is_current=true` → 日期推算 + warning log | DB 未 seed 不該系統當機；20+ caller / 19 既有 test 不破壞 |
| Event 機制 | 同步 in-process hooks registry、同 transaction 串註執行 | Subscriber 已知 3 個、atomic guarantee 重要；outbox/queue over-engineering |
| Classroom carry-over | 同學年 1→2 自動 / 跨學年手動 | 跨學年涉及升級換班，自動會出錯；同學年同班延續是直覺 |
| Leave quota cutover | 跨學年（下→新上）為員工生 new row；annual 按 hire_date→new.start_date 年資算；compensatory carry-over 結餘 | 對齊「8 月新學年」起點；員工特休保留勞基法 hire_date 年資邏輯 |
| Migration data | 不 seed `is_current=true`；舊 year-based row 保留為 legacy | 系統不中斷、admin 主動觸發；不假設舊資料語意 |

## 3. 資料模型

### 3.1 `AcademicTerm` 增量

既有欄位：`id, school_year, semester, start_date, end_date, created_at, updated_at`

追加：

```python
is_current = Column(
    Boolean,
    nullable=False,
    server_default=text("false"),
    comment="目前學期旗標；全表至多一筆 true",
)
```

Partial unique index（強制 singleton）：

```sql
CREATE UNIQUE INDEX uq_academic_terms_is_current_singleton
  ON academic_terms (is_current) WHERE is_current = true;
```

SQLAlchemy `__table_args__` 加 `Index("uq_academic_terms_is_current_singleton", "is_current", unique=True, postgresql_where=text("is_current = true"), sqlite_where=text("is_current = true"))`。

### 3.2 `LeaveQuota` 增量

既有欄位：`id, employee_id, year(西元), leave_type, total_hours, note, created_at, updated_at`，UniqueConstraint(employee_id, year, leave_type)，Index ix_leave_quota_year。

追加：

```python
school_year = Column(Integer, nullable=True, comment="民國學年；null = legacy year-based row")
```

Partial unique index（新 row 用 school_year 為鍵；舊 row school_year IS NULL 不參與）：

```sql
CREATE UNIQUE INDEX uq_leave_quotas_employee_school_year_type
  ON leave_quotas (employee_id, school_year, leave_type)
  WHERE school_year IS NOT NULL;

CREATE INDEX ix_leave_quotas_school_year
  ON leave_quotas (school_year) WHERE school_year IS NOT NULL;
```

舊 `year` 欄位保留為 legacy，凍結只讀。讀路徑先按 `school_year` 查、找不到 fallback 到 `year`。**Follow-up**：過渡完成後（下季）開獨立 PR 清掉 legacy fallback 與 `year` 欄位。

### 3.3 Migration

檔名：`alembic/versions/20260522_<hash>_academic_term_is_current_and_leave_quota_school_year.py`

`down_revision = "rfunnel01"`（招生漏斗 Phase A migration）。

**up**：

```python
def upgrade():
    # AcademicTerm.is_current
    op.add_column("academic_terms",
        sa.Column("is_current", sa.Boolean, nullable=False,
                  server_default=sa.text("false")))
    op.create_index("uq_academic_terms_is_current_singleton",
                    "academic_terms", ["is_current"], unique=True,
                    postgresql_where=sa.text("is_current = true"),
                    sqlite_where=sa.text("is_current = true"))

    # LeaveQuota.school_year
    op.add_column("leave_quotas",
        sa.Column("school_year", sa.Integer, nullable=True))
    op.create_index("uq_leave_quotas_employee_school_year_type",
                    "leave_quotas", ["employee_id", "school_year", "leave_type"],
                    unique=True,
                    postgresql_where=sa.text("school_year IS NOT NULL"),
                    sqlite_where=sa.text("school_year IS NOT NULL"))
    op.create_index("ix_leave_quotas_school_year",
                    "leave_quotas", ["school_year"],
                    postgresql_where=sa.text("school_year IS NOT NULL"),
                    sqlite_where=sa.text("school_year IS NOT NULL"))
```

**down**：對稱 drop 全部新增物件（過 `alembic-roundtrip-ci` workflow 檢查）。

**Idempotency**：用 `op.get_bind().dialect.has_index()` 包一層（與 workspace `alembic-symmetry-lint` branch 風格一致）— 重跑不炸。

**Data migration**：**不做**。Schema-only。admin 首次按「正式開新學期」才寫第一筆 `is_current=true` row；既有 `leave_quotas` row（`school_year=NULL`）一筆都不動，first term.changed 才生 new school_year-tagged row。

## 4. `utils/academic.py` 改寫

### 4.1 完整改寫後內容

```python
"""共用學年度計算工具。

- resolve_current_academic_term(target_date=None, session=None):
    優先查 AcademicTerm.is_current=true；找不到 fallback 到日期推算 + warning log。
    target_date 顯式傳值時跳過 DB 查詢（用於測試/歷史查詢）。
- default_current_academic_term_for_column():
    SQLAlchemy Column.default 專用、純日期推算、不查 DB（避免 INSERT 副作用）。
- resolve_academic_term_filters(school_year, semester, session=None):
    既有介面，多接 session 可選。
- _resolve_by_date(target_date): 私有純函式，原本日期推算邏輯。
- semester_int_to_enum / semester_enum_to_int: 不動。
"""

import logging
from datetime import date
from typing import Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


def _resolve_by_date(target_date: date) -> tuple[int, int]:
    """純日期推算學年/學期（民國年）。"""
    if target_date.month >= 8:
        return target_date.year - 1911, 1
    if target_date.month >= 2:
        return target_date.year - 1 - 1911, 2
    return target_date.year - 1 - 1911, 1


def resolve_current_academic_term(
    target_date: Optional[date] = None,
    session: Optional[Session] = None,
) -> tuple[int, int]:
    if target_date is not None:
        return _resolve_by_date(target_date)

    from models.academic_term import AcademicTerm
    from models.base import get_session

    sess = session
    owned = False
    if sess is None:
        sess = get_session()
        owned = True
    try:
        row = (
            sess.query(AcademicTerm)
            .filter(AcademicTerm.is_current.is_(True))
            .first()
        )
        if row:
            return row.school_year, row.semester
        logger.warning(
            "AcademicTerm.is_current 未設定，resolve_current_academic_term() "
            "fallback 到日期推算（請至 /academic-terms UI 設定當前學期）"
        )
        return _resolve_by_date(date.today())
    finally:
        if owned:
            sess.close()


def default_current_academic_term_for_column() -> tuple[int, int]:
    """SQLAlchemy Column.default 專用：純日期推算、不查 DB。"""
    return _resolve_by_date(date.today())


def resolve_academic_term_filters(
    school_year: Optional[int],
    semester: Optional[int],
    session: Optional[Session] = None,
) -> tuple[int, int]:
    if school_year is None and semester is None:
        return resolve_current_academic_term(session=session)
    if school_year is None or semester is None:
        raise HTTPException(
            status_code=400, detail="school_year 與 semester 需同時提供"
        )
    return school_year, semester


def semester_int_to_enum(sem_int: int):
    from models.appraisal import Semester
    if sem_int == 1:
        return Semester.FIRST
    if sem_int == 2:
        return Semester.SECOND
    raise ValueError(f"semester must be 1 or 2, got {sem_int}")


def semester_enum_to_int(sem) -> int:
    from models.appraisal import Semester
    if sem == Semester.FIRST or sem == "FIRST":
        return 1
    if sem == Semester.SECOND or sem == "SECOND":
        return 2
    raise ValueError(f"semester must be Semester enum, got {sem!r}")
```

### 4.2 Caller 影響

| Caller | 變動 |
|---|---|
| `models/classroom.py:_default_school_year/_default_semester` | 改 import `default_current_academic_term_for_column` — **不查 DB** |
| `api/student_change_logs.py`、`api/student_enrollment.py`、`api/classrooms.py`、`api/students.py`、`api/appraisal/__init__.py`、`api/activity/registrations_pending.py` | 不動。`session=None` 行為等同既有，helper 自己開短命 session（讀-only） |
| 19 個 test caller 透過顯式 `target_date` 走純日期推算 | 零行為變化 |

## 5. `utils/term_events.py`（新檔）

```python
"""term.changed 事件 hooks registry。

設計原則：
- 同步、in-process、單一 transaction：caller 持 session、未 commit；
  fire 後依序串註呼叫所有 handler，handler 都在 caller session 上寫；
  任一 handler raise → caller 觸發 transaction rollback
- 註冊順序穩定：handler 按 register 順序執行；register 在 module import 時跑，
  startup 顯式 import 一次保證順序
- testability：reset_handlers_for_tests() 清空、register_handler() 不靠 decorator
"""

import logging
from typing import Callable, Optional, Protocol

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


class TermLike(Protocol):
    id: int
    school_year: int
    semester: int


TermChangedHandler = Callable[..., None]
_HANDLERS: list[tuple[str, TermChangedHandler]] = []


def on_term_changed(name: str):
    def decorator(fn: TermChangedHandler) -> TermChangedHandler:
        register_handler(name, fn)
        return fn
    return decorator


def register_handler(name: str, fn: TermChangedHandler) -> None:
    if any(n == name for n, _ in _HANDLERS):
        raise RuntimeError(f"term.changed handler 已註冊：{name}")
    _HANDLERS.append((name, fn))


def fire_term_changed(
    *,
    old: Optional[TermLike],
    new: TermLike,
    session: Session,
) -> None:
    if not _HANDLERS:
        logger.info("term.changed fired but no handler registered")
        return
    for name, handler in _HANDLERS:
        logger.info(
            "term.changed handler 觸發：%s (old=%s, new=%s/%s)",
            name,
            f"{old.school_year}-{old.semester}" if old else None,
            new.school_year,
            new.semester,
        )
        handler(old=old, new=new, session=session)


def reset_handlers_for_tests() -> None:
    _HANDLERS.clear()


def list_handler_names() -> list[str]:
    return [n for n, _ in _HANDLERS]
```

### 5.1 Subscriber import 確保

`main.py` 既有 `on_startup()` 函式（line 188 附近）在 lifespan 中被呼叫。在 `on_startup()` 內加入：

```python
def on_startup():
    # ... 既有邏輯 ...

    # term.changed handlers 註冊（import-time 即觸發 @on_term_changed decorator）
    import services.term_subscribers.classroom_carry_over   # noqa: F401
    import services.term_subscribers.leave_quota_cutover    # noqa: F401
    import services.term_subscribers.activity_semester_tag  # noqa: F401

    from utils.term_events import list_handler_names
    logger.info("term.changed handlers: %s", list_handler_names())
```

順序 = import 順序 = 執行順序：classroom carry-over → leave quota cutover → activity tag reset。順序語意上的需求：classroom 遷移先於 quota cutover（前者寫 student.classroom_id，後者讀 employee；無相依），placeholder 最後。

## 6. Toggle Endpoint：`POST /academic-terms/{id}/set-current`

加到既有 `api/academic_terms.py`：

```python
@router.post("/{term_id}/set-current", response_model=AcademicTermOut)
def set_current_term(
    term_id: int,
    session: Session = Depends(get_session_dep),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
) -> AcademicTerm:
    new_term = session.query(AcademicTerm).filter(AcademicTerm.id == term_id).first()
    if not new_term:
        raise HTTPException(404, detail="學年學期設定不存在")

    old_term = (
        session.query(AcademicTerm)
        .filter(AcademicTerm.is_current.is_(True))
        .first()
    )
    if old_term and old_term.id == new_term.id:
        raise HTTPException(409, detail="已是目前學期，無需切換")

    if old_term:
        old_term.is_current = False
    new_term.is_current = True
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(500, detail="is_current singleton 違反，請聯絡管理員") from exc

    logger.info(
        "學期切換：%s → %s（操作者 user_id=%s）",
        f"{old_term.school_year}-{old_term.semester}" if old_term else "(none)",
        f"{new_term.school_year}-{new_term.semester}",
        current_user.get("user_id"),
    )

    fire_term_changed(old=old_term, new=new_term, session=session)

    session.refresh(new_term)
    return new_term
```

`SETTINGS_WRITE` 守衛與既有 create/update term 一致。

## 7. Subscriber 細節

### 7.1 `services/term_subscribers/classroom_carry_over.py`

行為矩陣：

| Old → New | 行為 |
|---|---|
| `None → 任何` | no-op + log info（初次設定） |
| same `school_year`、`1 → 2` | 複製 active classroom + 遷移 active student |
| `X-2 → (X+1)-1` | no-op + log info（跨學年由 admin 手動編班） |
| 其他（如 2→1 同年、跳級切換） | no-op + log warning |

`_carry_over_same_year`：

- 撈 `school_year=old.school_year AND semester=old.semester` 的 classroom rows
- 每筆 INSERT new Classroom（複製 name / grade_id / capacity / 三個 teacher_id / class_code / 其他欄位），school_year/semester 設為 new
- session.flush() 拿新 id；建 `old_to_new: dict[int, int]`
- 批次 UPDATE `Student.classroom_id` from old_id → new_id（filter `is_active=true`）
- log info 報告複製數與遷移數

**Edge cases**：
- inactive student 不遷移（留歷史紀錄）
- 班名衝突無 schema constraint 不會炸
- 撈 0 個 classroom 時 early return + log

### 7.2 `services/term_subscribers/leave_quota_cutover.py`

行為矩陣：

| Old → New | 行為 |
|---|---|
| `None → 任何` | no-op + log info |
| same `school_year`、`1 → 2` | no-op + log info |
| `X-2 → (X+1)-1` | 為每位 active 員工 INSERT new row(s) with school_year=X+1 |
| 其他 | no-op + log info |

`_cutover_for_all_active_employees`：

- 撈所有 `Employee.is_active=true`
- 對每位員工 + 每個 `QUOTA_LEAVE_TYPES`：
  - **annual**：`_calc_annual_leave_hours(emp.hire_date, year=new.start_date.year, reference_date=new.start_date)`，note 標 `年資 (基準 YYYY-MM-DD) 換算（依勞基法第38條）`
  - **其他法定**：`STATUTORY_QUOTA_HOURS[lt]`，note 標 `法定年度上限（學年制）`
- **compensatory**（不在 QUOTA_LEAVE_TYPES，但要 carry-over）：
  - 從上學年 row `total_hours` 扣已核准已用部分、加上 carry-over 結餘
  - 計算邏輯：`_calc_compensatory_balance(employee_id, old, new, session)`，回傳 `max(0, old_quota.total_hours - approved_used_in_old_term)`
  - **舊 quota 查詢 cold-start 相容**：第一次 toggle 時系統內全部是 legacy year-only row（`school_year=NULL`）。`_calc_compensatory_balance` 先找 `school_year == old.school_year` row，找不到 fallback 找 `school_year IS NULL AND year == old.start_date.year` legacy row；若都找不到才回 0。**避免** cutover 後所有員工補休 silently 歸零這個 P0 bug
  - `approved_used_in_old_term` 篩選條件：`LeaveRecord.employee_id == emp.id AND leave_type == "compensatory" AND is_approved == True AND start_date >= old.start_date AND start_date < new.start_date`
  - new row.total_hours = 結餘、note 標 `上學年結餘 X.X 小時 carry-over`
- **idempotency**：handler 內 pre-check `school_year=new.school_year` row 是否已存在；存在則 skip 該 leave_type（連按兩次 set-current 不會 double-insert）
- 寫法：sessions.add 為每筆 row、最後 session.flush() — caller 自己 commit

### 7.3 `_calc_annual_leave_hours` 介面擴充

```python
def _calc_annual_leave_hours(
    hire_date: date | None,
    year: int,
    reference_date: date | None = None,
) -> float:
    """特休時數，依勞基法第 38 條年資。

    reference_date 未提供時 fallback 為 date(year, 12, 31)（向後相容既有 caller）。
    leave_quota_cutover handler 顯式傳入 new_term.start_date。
    """
    if hire_date is None:
        return 0.0
    ref = reference_date or date(year, 12, 31)
    # 後續邏輯同既有
    ...
```

舊 caller `init_leave_quotas` 不傳 `reference_date`，行為零變化。

### 7.4 `services/term_subscribers/activity_semester_tag.py`

```python
@on_term_changed("activity_semester_tag_reset")
def handle(*, old, new, session):
    logger.info(
        "activity_semester_tag_reset: placeholder triggered for %s-%s → %s-%s "
        "(目前為 no-op，未來實作學期報名標籤更新)",
        old.school_year if old else None, old.semester if old else None,
        new.school_year, new.semester,
    )
```

存在意義：早把 hook 接好、未來實作不改 toggle endpoint；整合測試可驗證註冊機制。

## 8. 過渡期相容：`_check_quota` 讀路徑

`api/leaves_quota.py:_check_quota` / `_check_compensatory_quota` 原本按 `LeaveQuota.year=` 篩，cutover 後雙存（舊 year-based row + 新 school_year-based row）。讀路徑：先學年、後西元年 fallback。

```python
def _resolve_quota_row(
    session, employee_id: int, leave_type: str,
    *, target_date: date | None = None,
) -> LeaveQuota | None:
    school_year, _ = resolve_current_academic_term(
        target_date=target_date, session=session,
    )
    row = (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year == school_year,
            LeaveQuota.leave_type == leave_type,
        )
        .first()
    )
    if row:
        return row
    # fallback：legacy year-based row（過渡期）
    legacy_year = (target_date or date.today()).year
    return (
        session.query(LeaveQuota)
        .filter(
            LeaveQuota.employee_id == employee_id,
            LeaveQuota.school_year.is_(None),
            LeaveQuota.year == legacy_year,
            LeaveQuota.leave_type == leave_type,
        )
        .first()
    )
```

`_check_quota` / `_check_compensatory_quota` 改呼叫 `_resolve_quota_row()`，主體（remaining / pending / approved）不動。

**`_get_approved_hours_in_year` / `_get_pending_hours_in_year`** 仍按西元年 `start_date` 篩 — 過渡期已知 trade-off：新 quota 容量按學年算、已用時數按西元年累計，會有暫時錯位。**Follow-up**：legacy year 清除後改用 AcademicTerm.start_date/end_date 區間篩。

**`init_leave_quotas`** endpoint 保留不動（admin 手動補建個別員工）。**Follow-up**：下季開 PR 把它改寫成寫 school_year row。

## 9. 測試清單

### 9.1 Unit tests

**`tests/test_term_events.py`**（新檔）：
- `test_register_duplicate_raises`
- `test_fire_no_handlers`
- `test_fire_order_matches_registration`
- `test_fire_handler_raises_propagates`
- `test_reset_handlers_for_tests`

**`tests/test_academic_utils.py`**（擴充）：
- `test_resolve_uses_db_is_current_when_set`
- `test_resolve_fallback_to_date_when_no_current`（caplog 抓 warning）
- `test_resolve_target_date_skips_db_query`（mock session 驗證）
- `test_default_for_column_never_queries_db`（mock session 驗證）

**`tests/test_classroom_carry_over.py`**（新檔）：
- inactive student 不遷移
- 多 classroom 多 student 全遷移
- 上學期 0 classroom 時 no-op
- 班級欄位完整複製（head/assistant/art teacher、grade_id、capacity、class_code）

**`tests/test_leave_quota_cutover.py`**（新檔）：
- 員工 hire_date 跨多年的年資計算正確
- `hire_date is None` 員工 quota=0 + 對應 note
- inactive employee 不生 row
- compensatory carry-over 正確扣除已核准
- idempotent（同 school_year row 已存在則 skip）

### 9.2 Integration tests

**`tests/test_term_change_integration.py`**（新檔）：

| Test | 場景 |
|---|---|
| `test_initial_set_current_no_subscribers_run` | `old=None` 時 3 subscriber 全 no-op |
| `test_same_year_1_to_2_classroom_carry_over` | 114-1 → 114-2：classroom 複製、學生遷移、quota 不動 |
| `test_cross_year_2_to_1_leave_quota_cutover` | 114-2 → 115-1：classroom 不動、每員工生 new quota row |
| `test_cross_year_quota_compensatory_balance_carry_over` | 補休結餘正確 carry-over |
| `test_cross_year_annual_uses_new_term_start_date_as_ref` | 特休 reference = new.start_date |
| `test_set_current_to_same_term_returns_409` | 已是 current 的 term → 409 |
| `test_set_current_to_nonexistent_returns_404` | term_id 不存在 → 404 |
| `test_handler_raise_rolls_back_entire_transaction` | handler raise → is_current 不變、學生不遷移、quota 不建立 |
| `test_idempotent_toggle_does_not_double_insert_quotas` | 同方向重複跨學年切換不會 double-insert |
| `test_atypical_jump_113_2_to_115_1_logs_warning_no_op` | 跳級切換：classroom no-op + warning |
| `test_read_path_prefers_school_year_falls_back_to_year` | `_resolve_quota_row` 學年優先、缺則 fallback |

## 10. Migration cutover & rollout

生產環境步驟：

1. Merge worktree（招生漏斗 Phase A + 學期切換 hook）進 main
2. `alembic upgrade heads`
3. **不**自動 seed `is_current=true` —— admin 在前端 UI 上手動設一筆（前端 UI 為 follow-up）
4. 觀察一週：log 應有 `AcademicTerm.is_current 未設定` warning，提醒 admin 去設
5. Admin 設第一筆後：log 切換到「初次設定，subscriber 全 no-op」 — 正式生效

回滾：

1. `alembic downgrade -1`
2. Helper 自動回到日期推算（fallback 本來就是這條路徑）
3. 既有 quota row（school_year=NULL）完全不受影響
4. **唯一不可逆**：term.changed 跑過後產生的新 classroom row 跟 student.classroom_id 變更會留下 — 第一次 toggle 前建議 DB backup

## 11. Commit 拆分

| # | Commit | 內容 |
|---|---|---|
| 1 | `feat(db): academic_terms.is_current + leave_quotas.school_year migration` | Alembic migration + idempotent up/down |
| 2 | `feat(models): AcademicTerm.is_current + LeaveQuota.school_year` | Model 層追加欄位 + partial unique index |
| 3 | `refactor(utils): academic.resolve_current_academic_term 查 DB + fallback` | helper 拆分 + classroom.py 接 `default_current_academic_term_for_column` |
| 4 | `feat(utils): term_events hooks registry` | `utils/term_events.py` + unit test |
| 5 | `feat(api): POST /academic-terms/{id}/set-current toggle endpoint` | router 加 endpoint（不接 subscriber） |
| 6 | `feat(services): classroom_carry_over term.changed subscriber` | 新檔 + unit test |
| 7 | `feat(services): leave_quota_cutover term.changed subscriber` | 新檔 + `_calc_annual_leave_hours` 擴 `reference_date` |
| 8 | `feat(services): activity_semester_tag_reset placeholder subscriber` | 新檔 + main.py 註冊 |
| 9 | `refactor(leaves_quota): _check_quota 學年優先讀路徑` | `_resolve_quota_row` + 過渡 fallback |
| 10 | `test(integration): /academic-terms/{id}/set-current 端對端` | `test_term_change_integration.py` |
| 11 | `docs(spec): 學期切換 hook 設計 + plan` | spec / plan 文件入 repo |

## 12. Follow-up（不在本次範圍）

- `init_leave_quotas` 改寫成 school_year-based（下季）
- `_get_approved_hours_in_year` 改用 AcademicTerm.start_date/end_date 區間篩（legacy year 清除後）
- 前端 `/academic-terms` UI 加「正式開新學期」按鈕（**user 手測 + OpenAPI codegen + 前端 PR**，本次只動後端）
- `activity_semester_tag_reset` placeholder 補實質邏輯（看招生漏斗報名階段整合需求）
- AuditLog 寫入：`set-current` 操作是否列入稽核（middleware 已有，看 user 要求嚴格程度）
- 過渡完成後（下季）開 PR 清除 `leave_quotas.year` 欄位與 `_check_quota` 的 fallback 邏輯
