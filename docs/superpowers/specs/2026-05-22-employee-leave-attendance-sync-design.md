# 員工請假 → 考勤同步重構 Design

**日期**:2026-05-22
**範圍**:後端 ivy-backend(純後端,前端無 schema 變更)
**目標**:讓 `AttendanceRecord` 成為員工出勤的唯一 SoT,消除「多處 join LeaveRecord 才不出鬼影」的補丁文化

---

## §1 範圍與目標

### 問題

員工請假 `POST /leaves/{id}/approve` 流程僅做三件事:
1. salary recalc cache invalidate
2. LINE 通知
3. 寫 ApprovalLog + force_overlap comment

**完全不寫 AttendanceRecord**。意味著員工請假被核准後,任何讀 `AttendanceRecord` 的視圖(今日打卡看板、個人考勤查詢、月報、未來新加的 attendance 視圖)若沒主動 `join LeaveRecord` 補丁,就會看到該日 LATE/ABSENT 的鬼影。

目前 prod 已有兩個消費端在做這個 join:
- `api/attendance/reports.py:348-364`(月報)
- `services/salary/engine.py:_build_breakdown_for_month()`(薪資扣款 / 全勤獎金)

學生端的 `services/student_leave_service.apply_attendance_for_leave()` 早就解了這個問題:approve 即同步寫 `StudentAttendance`,reject/cancel 即反寫。員工端因為支援半天 + 小時假,從未對齊。

### 做什麼(in-scope)

1. 新增 `services/employee_leave_attendance_sync.py`,提供 `apply / revert / reapply` 三個純函式,**所有 leave-driven 寫入經此一處**
2. **新增 `utils/attendance_leave_merge.py`,提供 `merge_attendance_with_leave(att, session)` 純函式;讓 3 個既有的「非 sync 寫入路徑」(admin 手動 / Excel upload / 補卡重算)全部成為 leave-aware**(§3.5 詳述)
3. `models/attendance.py` 加 `leave_record_id` (FK, ON DELETE SET NULL)、`partial_leave_hours` (Numeric(4,2))、`status` enum 加 `LEAVE`;補上 `UNIQUE (employee_id, attendance_date)`
4. `api/leaves.py` 5 個進入點(approve / reject / update 退審 / update 改關鍵欄 / delete)全部呼叫 sync
5. 寫 Alembic migration:**schema 變更 + 去重檢查 + 全量 backfill(近 12 個月 approved)** 於同一個 migration;unique constraint 用 `CREATE UNIQUE INDEX CONCURRENTLY` 避免 lock 阻塞(§2 詳述)
6. **拆月報 leave_map join**(`api/attendance/reports.py:348-364`)— consumer 改信任 AttendanceRecord
7. **薪資引擎改讀 `AttendanceRecord` (status=LEAVE / partial_leave_hours)** 計算扣款 / 全勤獎金 / 加班互斥
8. 完整回歸測試:approve→attendance / reject→revert / update 各分支 / delete→revert / salary engine 對等性 / 月報 SoT 切換對等性 / **3 個 attendance 寫入路徑與 leave 共存** (§8)

### 不做(out-of-scope)

- **不**新增 revert/cancel 端點(撤銷仍走 update 退審 + delete)
- **不**改學生端 `apply_attendance_for_leave`(語義不同,維持現狀)
- **不**動 LINE 通知 / ApprovalLog / force_overlap comment 等其他 approve 副作用
- **不**支援「同日多筆部分請假」(同日第二筆部分請假在 approve 時 422 阻擋,follow-up 再評估)
- **不**在這支 PR 開新欄給 morning/afternoon 標記(`partial_leave_hours` 量化處理)
- **不**改 `update_leave` / `delete_leave` 既有的 salary cache 處理(沿用 `lock_and_premark_stale` 標記 lazy recompute — 新讀取源切換後天然成立,§6 詳述)

### Rollout 順序(同 PR 內依序)

```
1. Schema migration (add cols + LEAVE enum + dup-detect → fail-loud)
2. Hardening:LeaveCreate/Update validator 加「leave_hours<8 → start_time/end_time 必填」
3. Pre-flight backfill data fix:既有 leave_hours<8 + start_time=None 的 row 強制補時段(SOP 預跑)
4. CREATE UNIQUE INDEX CONCURRENTLY → ADD CONSTRAINT USING INDEX
5. Backfill approved leaves (idempotent, dry-run gated)
6. attendance 3 路徑改呼叫 merge helper
7. Switch salary engine reader (with parity assertion in test)
8. Remove leave_map from monthly report
```

任一步 fail 整個 migration revert。

---

## §2 資料模型 (`models/attendance.py` + Alembic)

### AttendanceRecord 欄位新增

```python
class AttendanceRecord(Base):
    __tablename__ = "attendance_records"

    id                   = Column(Integer, primary_key=True)
    employee_id          = Column(Integer, ForeignKey("employees.id"), index=True)
    attendance_date      = Column(Date, nullable=False, index=True)
    punch_in_time        = Column(Time, nullable=True)
    punch_out_time       = Column(Time, nullable=True)
    status               = Column(SqlEnum(AttendanceStatus), nullable=False)
    late_minutes         = Column(Integer, default=0)
    early_leave_minutes  = Column(Integer, default=0)
    remark               = Column(String, nullable=True)
    confirmed_action     = Column(String, nullable=True)
    confirmed_by         = Column(Integer, ForeignKey("employees.id"), nullable=True)
    confirmed_at         = Column(DateTime, nullable=True)

    # ── 新增 ──────────────────────────────────────────────────────
    leave_record_id      = Column(
        Integer,
        ForeignKey("leave_records.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    partial_leave_hours  = Column(Numeric(4, 2), nullable=True)
    # ────────────────────────────────────────────────────────────

    __table_args__ = (
        UniqueConstraint("employee_id", "attendance_date",
                         name="uq_attendance_employee_date"),
    )
```

### AttendanceStatus enum 加值

```python
class AttendanceStatus(enum.Enum):
    NORMAL       = "NORMAL"
    LATE         = "LATE"
    EARLY_LEAVE  = "EARLY_LEAVE"
    MISSING      = "MISSING"
    ABSENT       = "ABSENT"
    LEAVE        = "LEAVE"        # ← 新增:全天請假
```

### 欄位語義對照

| 情境 | status | punch_in/out | leave_record_id | partial_leave_hours | late_minutes |
|---|---|---|---|---|---|
| 一般打卡 | NORMAL/LATE/EARLY_LEAVE | 有 | NULL | NULL | 計算值 |
| **全天請假** | **LEAVE** | NULL | leave.id | NULL | 0 |
| **半天請假 + 有打卡** | NORMAL/LATE | 有 | leave.id | 4.0 | 計算值(扣請假時段) |
| **半天請假 + 沒打卡** | ABSENT | NULL | leave.id | 4.0 | 0 |
| **小時請假 + 有打卡** | NORMAL/LATE | 有 | leave.id | 1.5 | 計算值(扣請假時段) |

**半天/小時遲到分鐘**:`compute_late_minutes_with_leave(punch_in, leave.start_time/end_time)` — 員工 09:00 上班、08:00–10:00 請小時假、09:30 打卡 → 遲到 0 分鐘。獨立放在 `utils/attendance_calc.py` 補一個純函式,sync service 呼叫它。

### Migration 順序

`alembic/versions/empleavesync_attendance_sync.py`:

```python
def upgrade():
    # 1. 加新欄(nullable;不 lock 表)
    op.add_column("attendance_records",
        sa.Column("leave_record_id", sa.Integer(), nullable=True))
    op.add_column("attendance_records",
        sa.Column("partial_leave_hours", sa.Numeric(4, 2), nullable=True))
    op.create_foreign_key("fk_attendance_leave", "attendance_records",
        "leave_records", ["leave_record_id"], ["id"], ondelete="SET NULL")

    # 2. enum 加 LEAVE 值(Postgres 必須在 autocommit_block)
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE attendancestatus ADD VALUE IF NOT EXISTS 'LEAVE'")

    # 3. 去重偵測(fail-loud)
    conn = op.get_bind()
    dups = conn.execute(text("""
        SELECT employee_id, attendance_date, COUNT(*) c
        FROM attendance_records
        GROUP BY employee_id, attendance_date HAVING COUNT(*) > 1
    """)).fetchall()
    if dups:
        raise RuntimeError(
            f"偵測到 {len(dups)} 組 (employee_id, attendance_date) 重複,"
            f"請先跑 scripts/dedupe_attendance.py 清理再 upgrade。前 5 筆: {dups[:5]}"
        )

    # 4. Online 加 unique constraint(CREATE UNIQUE INDEX CONCURRENTLY → ADD CONSTRAINT USING INDEX)
    #    用 CONCURRENTLY 避免 AccessExclusiveLock 阻塞 prod 流量
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS uq_attendance_employee_date
            ON attendance_records (employee_id, attendance_date)
        """)
    op.execute("""
        ALTER TABLE attendance_records
        ADD CONSTRAINT uq_attendance_employee_date
        UNIQUE USING INDEX uq_attendance_employee_date
    """)
    with op.get_context().autocommit_block():
        op.execute("""
            CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_attendance_leave_record_id
            ON attendance_records (leave_record_id)
        """)

    # 5. Pre-flight validator:阻擋既有部分請假缺 start_time/end_time(否則 _apply_partial 會炸)
    bad_leaves = conn.execute(text("""
        SELECT id, employee_id, start_date, leave_hours
        FROM leave_records
        WHERE is_approved = true
          AND end_date >= CURRENT_DATE - INTERVAL '12 months'
          AND (start_time IS NULL OR end_time IS NULL)
          AND (leave_hours IS NOT NULL AND leave_hours < 8)
    """)).fetchall()
    if bad_leaves:
        raise RuntimeError(
            f"偵測到 {len(bad_leaves)} 筆已核可的部分請假缺少 start_time/end_time,"
            f"請先跑 scripts/fix_partial_leave_times.py 補時段或回到 pending 重審。"
            f"前 5 筆: {bad_leaves[:5]}"
        )

    # 6. Backfill(env IVY_SKIP_BACKFILL=1 可跳)
    if not os.getenv("IVY_SKIP_BACKFILL"):
        _run_backfill(conn)

def downgrade():
    op.drop_constraint("uq_attendance_employee_date", "attendance_records",
        type_="unique")
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_attendance_leave_record_id")
    op.drop_constraint("fk_attendance_leave", "attendance_records",
        type_="foreignkey")
    op.drop_column("attendance_records", "partial_leave_hours")
    op.drop_column("attendance_records", "leave_record_id")
    # 注意:Postgres 無法 drop enum value,LEAVE 殘留是預期
```

### 配套工具

- `scripts/dedupe_attendance.py` — dry-run / `--apply` 保留最早建立的、其他刪除前寫進 audit log
- `scripts/preview_backfill.py` — 部署前列出 backfill 規模、衝突數、預估影響(詳見 §5)
- `scripts/fix_partial_leave_times.py` — 部署前掃既有部分請假缺 start_time/end_time 的 row,列表並可選 `--apply` 回到 pending 退審(讓 admin 重新審核補時段)

### 踩雷提醒

- **enum 加值**:`ALTER TYPE ADD VALUE` 不能跑在 transaction 內 → 用 `autocommit_block()`
- **去重 fail-loud**:同日兩筆 attendance 可能是真資料(早上打、下午補打卡),不自動刪
- **downgrade 留 LEAVE enum 值**:Postgres 限制,但 row 已清(透過 sync.revert 全跑一遍),enum 留著無害
- **`CREATE INDEX CONCURRENTLY` 不能在 transaction 內**:所以用 `autocommit_block()` 包,且這個 migration 不可在 `--sql` offline mode 下跑(Alembic 會抱怨)
- **`ADD CONSTRAINT USING INDEX`**:這條本身 lock 很短(只是把既存 index 轉成 unique constraint),不需要 CONCURRENTLY
- **Online migration 預期影響**:寫入路徑暫短停滯數百 ms 可接受;若 attendance_records > 100 萬 row,部署窗口要選 off-peak

---

## §3 Service API (`services/employee_leave_attendance_sync.py`)

### 公開介面

```python
"""員工請假 → 考勤同步單一進入點。

對齊學生端 services/student_leave_service 的設計理念,但因員工請假支援半天/小時,
寫入策略採「並存模式」:全天 upsert status=LEAVE;半天/小時保留打卡並標記 leave_record_id。
"""

from datetime import date, timedelta
from decimal import Decimal
from typing import Iterable

from sqlalchemy.orm import Session

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord
from utils.attendance_calc import compute_late_minutes_with_leave


# ── 公開 API ──────────────────────────────────────────────────────

def apply(session: Session, leave_id: int) -> list[date]:
    """把 approved leave 寫入 AttendanceRecord。Idempotent。

    回傳實際 upsert 的日期列表(供 audit log)。
    Pre-condition: leave 必須是 is_approved=True;否則 raise LeaveNotApproved。
    """

def revert(session: Session, leave_id: int) -> list[date]:
    """把 leave 之前寫入的 AttendanceRecord 反寫。Idempotent。

    全天 → 刪該 row(若無打卡)/退回 ABSENT(若有打卡 — 邊緣情境)。
    半天/小時 → NULL out leave_record_id + partial_leave_hours,重算 late_minutes。
    回傳實際反寫的日期列表。
    """

def reapply(session: Session, leave_id: int,
            old_snapshot: dict | None = None) -> tuple[list[date], list[date]]:
    """update_leave 改了關鍵欄(日期/時段/leave_type/hours)時呼叫。

    內部組合:revert(舊範圍) → apply(新範圍)。
    old_snapshot 由 caller 在 commit 前抓:{start_date, end_date, start_time, end_time, leave_type, leave_hours}。
    回傳 (反寫日期, 新寫日期)。
    """
```

### 內部分派

```python
def _is_full_day(leave: LeaveRecord) -> bool:
    """全天 = start_time/end_time 都 NULL 且 leave_hours 是 None 或 >= 8。

    舊資料可能 leave_hours=8.0 + start_time=None,也視為全天。
    """
    return leave.start_time is None and leave.end_time is None and (
        leave.leave_hours is None or leave.leave_hours >= 8
    )


def _iter_dates(leave: LeaveRecord) -> Iterable[date]:
    d = leave.start_date
    while d <= leave.end_date:
        yield d
        d += timedelta(days=1)


def _apply_full_day(session, leave, d) -> None:
    """全天:upsert status=LEAVE,清打卡,leave_record_id 寫入。"""

def _apply_partial(session, leave, d) -> None:
    """半天/小時:UPSERT 不覆蓋 punch_in/punch_out;
    leave_record_id + partial_leave_hours 寫入;
    late_minutes/early_leave_minutes 用 compute_late_minutes_with_leave() 重算。

    同日已有其他 leave_record_id → raise LeaveAttendanceConflict。
    """
```

### Idempotency 機制(三個保證)

1. **apply 重複呼叫安全**:`SELECT ... FOR UPDATE` 鎖該 (employee_id, date) → 若已存在 row 且 leave_record_id == 當前 leave.id → no-op
2. **apply 換 leave 衝突**:若 row 已存在 leave_record_id ≠ 當前 leave.id → raise `LeaveAttendanceConflict`
3. **revert 安全**:revert 前 `SELECT WHERE leave_record_id = leave.id`,只動屬於這筆 leave 的 row;不依賴 leave 物件 start/end

### 例外型別

```python
class LeaveAttendanceConflict(Exception):
    """同日已有其他 leave_record_id 寫入 attendance(§1 同日多筆部分請假)。"""

class LeaveNotApproved(ValueError):
    """apply() 被呼叫時 leave 還沒 approved。"""

class LeavePartialTimeMissing(ValueError):
    """部分請假(leave_hours<8)缺 start_time/end_time,無法算 overlap。

    雙保險之一:LeaveCreate/Update validator 是第一道(§1 in-scope step 2),
    sync 入口再擋一次,避免 admin 直接 SQL 改 row 繞過 validator。
    """
```

caller 必須 catch `LeaveAttendanceConflict` / `LeavePartialTimeMissing` 回 422 給前端。

### Sync 入口防護

```python
def _assert_leave_time_consistent(leave: LeaveRecord) -> None:
    """半天/小時假必須有 start_time/end_time,否則 _apply_partial 會炸。"""
    if not _is_full_day(leave):
        if leave.start_time is None or leave.end_time is None:
            raise LeavePartialTimeMissing(
                f"leave_id={leave.id} 是部分請假(leave_hours={leave.leave_hours})"
                f"但缺 start_time/end_time"
            )
```

`apply` / `reapply` 在進入逐日迴圈前必須先呼叫 `_assert_leave_time_consistent(leave)`。

### 與既有層的邊界

- **不**讀環境變數、不發 LINE 通知、不寫 ApprovalLog — 那些是 caller 的責任
- **不** session.commit / session.flush — caller 管 transaction
- 內部只用 ORM,不下 raw SQL(migration 用 raw 是另一回事)
- **apply 對 unapproved leave fail-loud**:這擋住 implementer 偷把 sync 提早(pending 階段就寫)
- **半天 + 沒打卡 = ABSENT**:_apply_partial 寫的時候,若 punch_in/punch_out 都 NULL → status=ABSENT(不寫 LEAVE),consumer 靠 `partial_leave_hours IS NOT NULL` 判斷「請假沒到 vs 全曠職」

---

## §3.5 Attendance 寫入路徑 inventory + leave-aware merge helper

### 問題

`AttendanceRecord` 不只有 sync 在寫 — Explore 盤點出**還有 3 個既有寫入路徑**:

| 路徑 | 檔案:行號 | Trigger |
|---|---|---|
| Admin 手動新增/編輯 | `api/attendance/records.py:214-327` `create_or_update_attendance_record` | 後端管理在報表畫面手動修打卡 |
| Excel 批次匯入 | `api/attendance/upload.py:88-400` `upload_attendance` | 後端管理用打卡機 dump 上傳 |
| 補卡核准重算 | `utils/attendance_calc.py:94-118` `apply_attendance_status` | 員工補卡 → admin 核可後重算 status |

若這 3 條沒改,任何路徑 upsert 就會把 sync 寫入的 `leave_record_id` / `partial_leave_hours` / `status=LEAVE` 蓋掉 → 月底結薪該筆**靜默漏錢**。比目前「鬼影遲到」嚴重得多。

### 解法:共用 helper `utils/attendance_leave_merge.py`

```python
"""寫 AttendanceRecord 前合併當日有效 leave 資訊。

設計理念:寫入端負責 leave-awareness。不依靠 leave 端 trigger reapply 的隱性合約。
"""

from typing import Optional
from sqlalchemy.orm import Session

from models.attendance import AttendanceRecord, AttendanceStatus
from models.leave import LeaveRecord
from utils.attendance_calc import compute_late_minutes_with_leave


def merge_attendance_with_leave(att: AttendanceRecord, session: Session) -> None:
    """In-place 把當日有效 leave 的 leave_record_id / partial_leave_hours /
    late_minutes 等欄合進 att。

    決策表(att.attendance_date 當日是否有 approved leave):
    1. 無 leave            → 不動,保留 caller 算好的 status/late_minutes
    2. 全天 leave + 無打卡  → status=LEAVE,清打卡,leave_record_id 寫入
    3. 全天 leave + 有打卡  → 邊緣情境(該日員工臨時上班),保留打卡,leave_record_id 寫入,
                            status 由打卡計算決定,partial_leave_hours=0(表示請了但人來了)
    4. 部分 leave + 有打卡  → 保留打卡,partial_leave_hours 寫入,
                            late_minutes 用 compute_late_minutes_with_leave 重算
    5. 部分 leave + 無打卡  → status=ABSENT,leave_record_id + partial_leave_hours 寫入
    6. 同日多筆 leave      → 取最早 id(避免不確定行為),audit log 記一筆 warning

    純函式對 session 只讀:呼叫端管 transaction。
    """
    leaves = session.query(LeaveRecord).filter(
        LeaveRecord.employee_id == att.employee_id,
        LeaveRecord.start_date <= att.attendance_date,
        LeaveRecord.end_date >= att.attendance_date,
        LeaveRecord.is_approved == True,
    ).order_by(LeaveRecord.id).all()

    if not leaves:
        # case 1
        att.leave_record_id = None
        att.partial_leave_hours = None
        return

    leave = leaves[0]
    if len(leaves) > 1:
        # case 6 — 不該發生(§1 規範擋住),但既有資料可能有
        pass  # plan 階段補 audit log

    att.leave_record_id = leave.id

    if _is_full_day(leave):
        if att.punch_in_time is None and att.punch_out_time is None:
            # case 2
            att.status = AttendanceStatus.LEAVE
            att.partial_leave_hours = None
            att.late_minutes = 0
            att.early_leave_minutes = 0
        else:
            # case 3
            att.partial_leave_hours = 0  # 全天請假但人來了
            # status/late_minutes 用打卡計算的(caller 已算好)
    else:
        # 部分請假
        att.partial_leave_hours = leave.leave_hours
        if att.punch_in_time is None and att.punch_out_time is None:
            # case 5
            att.status = AttendanceStatus.ABSENT
            att.late_minutes = 0
        else:
            # case 4
            att.late_minutes = compute_late_minutes_with_leave(
                att.punch_in_time, leave.start_time, leave.end_time,
                employee_schedule=...,  # 員工排班上下班時間,plan 階段補 fetch
            )
```

### 3 個 caller 改動(各加一行)

```python
# api/attendance/records.py:214 create_or_update_attendance_record
record = AttendanceRecord(...)
apply_punch_calculations(record, punch_in, punch_out)    # 既有
merge_attendance_with_leave(record, session)             # ⚫ 新增
session.merge(record)

# api/attendance/upload.py:88-400 upload_attendance(批次)
for row in excel_rows:
    record = build_attendance_record(row)
    merge_attendance_with_leave(record, session)         # ⚫ 新增
    session.merge(record)

# utils/attendance_calc.py:94-118 apply_attendance_status
def apply_attendance_status(record, session):  # 加 session 參數
    # 既有 status/late_minutes 計算
    ...
    merge_attendance_with_leave(record, session)         # ⚫ 新增
```

### 為何不直接讓 sync 把 leaves 一網打盡

sync.apply 在 leave 被 approve 那一刻寫入 attendance。但 admin 之後又改打卡(case 3)、Excel 匯入又補了該天打卡(case 4)→ sync 沒有 hook 點知道「attendance 變了,要不要重算 leave overlap」。

讓 attendance **寫入端**主動 merge,等於把「leave-aware 計算」從「leave 端 trigger 推給 attendance」改成「attendance 端讀 leave pull」,概念簡單、不需要新 hook。

### Sync 與 merge helper 的職責分工

| | sync.apply/revert/reapply | merge_attendance_with_leave |
|---|---|---|
| **入口** | leave 生命週期事件(approve/reject/update/delete) | attendance 寫入事件(admin / upload / 補卡) |
| **動誰** | 批量寫多日 attendance | 單筆 attendance(in-place) |
| **session** | 寫(透過 caller 的 session) | 純讀(寫由 caller 的 session.merge 接手) |
| **leave 不存在時** | 不適用(sync 一定有 leave_id 為前提) | no-op,只清 leave_record_id/partial_leave_hours |
| **誰擁有 punch_in/out** | 全天:清掉;半天:保留 | 全交給 caller 設好 |

merge helper 可以呼叫 `_is_full_day` 與 `compute_late_minutes_with_leave`,**但不可以呼叫 sync** — 兩者並列、不互呼。

### Pre-flight check(plan 必驗)

implementer 在開工前必須跑這個 grep,確認沒有第 4、5 個 attendance 寫入路徑被遺漏:

```bash
grep -rn "AttendanceRecord" ivy-backend/api/ ivy-backend/services/ ivy-backend/utils/ \
  | grep -v "test_\|reports.py\|salary/engine.py\|leaves.py" \
  | grep -iE "add\(|merge\(|insert|update_|upsert|put_|new AttendanceRecord"
```

結果若超出上述 3 處 → 在 plan 列補 hook 任務,不可預設無事。

---

## §4 5 條 Hook 點 (`api/leaves.py`)

### 進入點清單

| # | Endpoint | Trigger | Sync 動作 | Caller 額外責任 |
|---|---|---|---|---|
| 1 | `POST /leaves/{id}/approve` | `data.approved=True` | `apply(leave_id)` | 既有 salary recalc + LINE + ApprovalLog 不動 |
| 2 | `POST /leaves/{id}/approve` | `data.approved=False`(reject) | `revert(leave_id)`(no-op,because 還沒 apply 過) | 既有 |
| 3 | `PATCH /leaves/{id}` (退審) | `is_approved: True→None` | `revert(leave_id)` | 自動退審原本就會清薪資 cache |
| 4 | `PATCH /leaves/{id}` (改關鍵欄) | start_date/end_date/start_time/end_time/leave_type/leave_hours 任一改變,且 `is_approved=True` | `reapply(leave_id, old_snapshot)` | snapshot 必須在欄位寫回 model 之前抓 |
| 5 | `DELETE /leaves/{id}` | 若 `is_approved=True` 才需要 | `revert(leave_id)`,然後正常刪除 leave;FK ON DELETE SET NULL 是保險 | 既有 |

**為何沒有「PATCH None→True approve 」路徑?**
Explore 確認 `LeaveUpdate` Pydantic schema(`api/leaves.py:259-307`)**不含 `is_approved` 欄位**;PATCH 即使傳 is_approved 也會被 Pydantic 忽略。核可只能走 POST /approve endpoint(Hook 1)。Spec 鎖死這條約束 — 若 implementer 因需求變動要在 LeaveUpdate 加 is_approved,**必須先擴 spec**,因為這條會打開新 hook 點。

**為何 update/delete 路徑既有 `lock_and_premark_stale` 不需動?**
Explore 確認:approve 路徑真正 recompute salary(L1479-1504);update/delete 路徑只 `lock_and_premark_stale`(L888 / L1043-1045)讓下次查詢 lazy recompute。切到新讀取源後,lazy recompute 自然讀 attendance 拿到新值 — 既有機制天然成立,**§4 不動 salary cache 邏輯**。

### update_leave 範例(Hook 3/4 — 最易踩)

```python
@router.patch("/{leave_id}")
def update_leave(leave_id: int, data: LeaveUpdateRequest, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()

    # ① 在改動前 snapshot(reapply 需要)
    old_snapshot = {
        "start_date": leave.start_date,
        "end_date": leave.end_date,
        "start_time": leave.start_time,
        "end_time": leave.end_time,
        "leave_type": leave.leave_type,
        "leave_hours": leave.leave_hours,
        "is_approved": leave.is_approved,
    }

    # ② 套用 patch(既有邏輯,可能觸發自動退審)
    for field, value in data.dict(exclude_unset=True).items():
        setattr(leave, field, value)

    # ③ Sync 分派
    key_fields_changed = any(
        old_snapshot[k] != getattr(leave, k)
        for k in ("start_date", "end_date", "start_time",
                  "end_time", "leave_type", "leave_hours")
    )

    if old_snapshot["is_approved"] is True and leave.is_approved is None:
        sync.revert(session, leave_id)
    elif old_snapshot["is_approved"] is True and leave.is_approved is True \
         and key_fields_changed:
        sync.reapply(session, leave_id, old_snapshot)

    session.flush()
```

### approve_leave 範例(Hook 1/2)

```python
@router.post("/{leave_id}/approve")
def approve_leave(leave_id: int, data: ApproveRequest, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    was_approved = (leave.is_approved is True)

    leave.is_approved = data.approved
    # 既有:寫 ApprovalLog、force_overlap comment

    try:
        if data.approved is True and not was_approved:
            sync.apply(session, leave_id)
        elif data.approved is False and was_approved:
            sync.revert(session, leave_id)
    except (sync.LeaveAttendanceConflict, sync.LeavePartialTimeMissing) as e:
        raise HTTPException(422, detail=str(e))

    session.flush()
    # 既有:salary recalc(L1479-1504)、LINE_NOTIFY、close-month guard 全保留
```

### delete_leave(Hook 5)

```python
@router.delete("/{leave_id}")
def delete_leave(leave_id: int, ...):
    leave = session.query(LeaveRecord).filter_by(id=leave_id).first()
    if leave.is_approved is True:
        sync.revert(session, leave_id)
    session.delete(leave)
    session.flush()
```

### 不動清單(reviewer 必對勾)

- ✅ `process_salary_calculation` 呼叫點不動(§6 才換讀取源)
- ✅ LINE_NOTIFY block 不動
- ✅ ApprovalLog 不動
- ✅ force_overlap comment 不動
- ✅ Quota 檢查不動
- ❌ **不**在 sync 失敗時 swallow exception — sync 拋出 = 整個 endpoint 422 退回

---

## §5 Backfill 細節

### 流程(migration 內 `_run_backfill`)

```python
def _run_backfill(conn):
    from services.employee_leave_attendance_sync import (
        apply, LeaveAttendanceConflict, LeaveNotApproved,
    )
    from sqlalchemy.orm import Session

    session = Session(bind=conn)
    leaves = session.execute(text("""
        SELECT id FROM leave_records
        WHERE is_approved = true
          AND end_date >= CURRENT_DATE - INTERVAL '12 months'
        ORDER BY id
    """)).fetchall()

    total = len(leaves)
    ok, skipped, conflicts, errors = 0, 0, [], []

    print(f"[backfill] 開始,共 {total} 筆 approved leave 在近 12 個月內")

    for idx, (lid,) in enumerate(leaves, 1):
        sp = session.begin_nested()  # SAVEPOINT 單筆隔離
        try:
            dates = apply(session, lid)
            sp.commit()
            ok += 1
            if not dates:
                skipped += 1
        except LeaveAttendanceConflict as e:
            sp.rollback()
            conflicts.append((lid, str(e)))
        except Exception as e:
            sp.rollback()
            errors.append((lid, type(e).__name__, str(e)[:200]))

        if idx % 100 == 0:
            print(f"[backfill] 進度 {idx}/{total} ok={ok} "
                  f"skipped={skipped} conflicts={len(conflicts)} errors={len(errors)}")

    print(f"[backfill] 完成:ok={ok} skipped={skipped} "
          f"conflicts={len(conflicts)} errors={len(errors)}")

    if errors:
        raise RuntimeError(
            f"backfill 有 {len(errors)} 筆失敗,migration 整支 rollback。"
            f"前 5 筆: {errors[:5]}"
        )

    if conflicts:
        print(f"[backfill] WARN 同日衝突 {len(conflicts)} 筆,沿用既有 attendance 不覆寫")
        # audit_logs 實際欄名 = (action, entity_type, entity_id, summary, changes, created_at)
        # user_id / username / ip_address 對 migration backfill 留 NULL
        for lid, msg in conflicts:
            session.execute(text("""
                INSERT INTO audit_logs (action, entity_type, entity_id, summary, created_at)
                VALUES ('UPDATE', 'leave_records', :lid, :msg, NOW())
            """), {"lid": str(lid), "msg": f"leave_attendance_backfill_conflict: {msg}"})
```

> **欄名確認**(Explore 從 `models/audit.py:12-36`):
> - `action` = `String(20)`(CREATE/UPDATE/DELETE),必填 → 用 'UPDATE'(語義上 attendance 沒被改但這條 audit 代表「決議不覆寫」)
> - `entity_type` = `String(50)`,必填
> - `entity_id` = `String(50)`(不是 Integer!),nullable
> - `summary` = `Text`,nullable;`changes` = `Text` (JSON),nullable
> - `user_id` / `username` / `ip_address`:nullable,migration 留 NULL

### Pre-flight Dry-run

`scripts/preview_backfill.py` — 不入 migration、純 SELECT:

```
近 12 月 approved leave: 1432 筆
全天:1108 筆 / 半天:240 筆 / 小時:84 筆
預估衝突(同日多筆部分):3 筆 → 列出 leave_id 與員工讓人工 pre-resolve
預估覆寫既有 AttendanceRecord:567 筆(LATE/ABSENT 變 LEAVE 或 leave_record_id 補上)
預估新建 AttendanceRecord:289 筆(全天請假當天員工沒打卡的)
```

### 容錯三層

1. **單筆 SAVEPOINT**:單筆失敗不污染整個 migration transaction
2. **fail-loud on errors**:`errors` list 非空 → 整個 migration rollback
3. **conflicts 放行 + audit**:既有資料若有「同日多筆」就保留現狀 + 寫 audit log,不擋 migration

### 逃生口

```bash
IVY_SKIP_BACKFILL=1 alembic upgrade head
```

SOP 預設**不**用這旗標;用必須在 PR 說明寫清楚理由。

---

## §6 薪資引擎切換 (`services/salary/engine.py`)

### 切換前

```python
leaves = session.query(LeaveRecord).filter(
    LeaveRecord.employee_id == employee_id,
    LeaveRecord.is_approved == True,
    LeaveRecord.start_date <= last_day,
    LeaveRecord.end_date >= first_day,
).all()

for lv in leaves:
    overlap_hours = compute_overlap_hours(lv, first_day, last_day)  # 複雜
    deduction = daily_rate * lv.deduction_ratio * (overlap_hours / 8)
    ...
```

### 切換後

```python
attendances = session.query(
    AttendanceRecord, LeaveRecord
).join(
    LeaveRecord, AttendanceRecord.leave_record_id == LeaveRecord.id
).filter(
    AttendanceRecord.employee_id == employee_id,
    AttendanceRecord.attendance_date.between(first_day, last_day),
    AttendanceRecord.leave_record_id.isnot(None),
).all()

for att, lv in attendances:
    hours = Decimal(8) if att.status == AttendanceStatus.LEAVE else att.partial_leave_hours
    deduction = daily_rate * lv.deduction_ratio * (hours / 8)
    breakdown.leave_deduction += deduction
    breakdown.leave_days.append({
        "date": att.attendance_date,
        "hours": hours,
        "leave_type": lv.leave_type,
    })
```

**關鍵改善**:
- ❌ 砍掉 `compute_overlap_hours()` — overlap 已在 sync.apply 寫入時算好
- ✅ join key 由「date range overlap」變成「FK」— 真的拆補丁
- ✅ `LeaveRecord` 仍 join(deduction_ratio / leave_type 屬 leave 領域,不複製到 attendance)

### 全勤獎金邏輯

```python
has_imperfect = session.query(AttendanceRecord).filter(
    AttendanceRecord.employee_id == employee_id,
    AttendanceRecord.attendance_date.between(first_day, last_day),
    or_(
        AttendanceRecord.leave_record_id.isnot(None),
        AttendanceRecord.status.in_([
            AttendanceStatus.LATE, AttendanceStatus.EARLY_LEAVE,
            AttendanceStatus.MISSING, AttendanceStatus.ABSENT,
        ]),
    )
).first()
breakdown.perfect_attendance_bonus = 0 if has_imperfect else bonus_amount
```

### 加班互斥

`tests/test_leave_overtime_conflict.py` 既有測試 — 切換後仍要綠。若加班計算還在 join LeaveRecord overlap,implementer 在 PR 列 grep 結果證明已切或註明保留。

### Cache invalidation:既有 stale-marking 機制天然成立

- `api/leaves.py` approve 路徑(L1479-1504):**真正 recompute** salary → 接新讀取源後 recompute 拿到 attendance 內的 LEAVE row,正確
- `api/leaves.py` update / delete 路徑(L888 / L1043-1045):`lock_and_premark_stale` 標記 lazy,下次查詢觸發 recompute → 同樣讀新 attendance,正確
- sync.apply / revert / reapply **不需主動** invalidate cache — 因為 caller(api/leaves.py)既有的 stale 機制已涵蓋

implementer 在 PR 不要「順手」在 sync 加 cache invalidation,會雙重 invalidate 浪費效能。

### Parity Test 策略

```python
# tests/test_salary_engine_parity.py(新增)
@pytest.mark.parametrize("employee_id,year,month", [
    # 30+ 案例:全勤 / 全天請假橫跨月份邊界 / 半天請假 + 打卡 /
    # 半天請假 + 缺打卡 / 小時請假 / 全月零打卡 / 月底跨月 / 補休 ...
])
def test_breakdown_matches_legacy(employee_id, year, month):
    new = engine._build_breakdown_for_month(employee_id, year, month)
    old = engine._build_breakdown_for_month_legacy(employee_id, year, month)
    assert new.leave_deduction == old.leave_deduction
    assert new.perfect_attendance_bonus == old.perfect_attendance_bonus
    assert new.overtime_pay == old.overtime_pay
    assert sorted(new.leave_days, key=...) == sorted(old.leave_days, key=...)
```

**Legacy 函式策略**:
- PR 內保留 `_build_breakdown_for_month_legacy()` 純函式拷貝,只給 parity test 用
- Production 路徑全切到新版
- Merge 後一週確認無異 → follow-up PR 刪 _legacy + parity test

### Fixture 來源

用既有 production-like seed(`tests/conftest.py` 既有員工/薪資 fixture)+ 補刻意 edge case:
- 跨月 leave(2026-04-30 ~ 2026-05-02,算 2026-05 月)
- 半天 4hr + 同日打卡遲到 30 分

避免「自造 fixture 把 bug 一起造進去」 — 既有 fixture 是真實壓力測試。

---

## §7 月報 leave_map join 移除 (`api/attendance/reports.py:348-364`)

### 拆掉前

```python
attendances = session.query(AttendanceRecord).filter(...)

# 補丁區
leaves = session.query(LeaveRecord).filter(
    LeaveRecord.is_approved == True, ...
).all()

leave_map = {}
for lv in leaves:
    d = max(lv.start_date, start_date)
    while d <= min(lv.end_date, end_date):
        leave_map[d] = lv
        d += timedelta(days=1)

for d in dates:
    if d in leave_map:
        row.status_label = lookup(leave_map[d].leave_type)
    else:
        row.status_label = lookup(att.status)
```

### 拆掉後

```python
attendances = session.query(
    AttendanceRecord, LeaveRecord
).outerjoin(
    LeaveRecord, AttendanceRecord.leave_record_id == LeaveRecord.id
).filter(
    AttendanceRecord.employee_id == employee_id,
    AttendanceRecord.attendance_date.between(start_date, end_date),
).order_by(AttendanceRecord.attendance_date).all()

for att, lv in attendances:
    if att.status == AttendanceStatus.LEAVE:
        row.status_label = leave_type_label(lv.leave_type) + "(全天)"
    elif att.leave_record_id is not None:
        row.status_label = (
            f"{status_label(att.status)} / "
            f"{leave_type_label(lv.leave_type)} "
            f"{att.partial_leave_hours}hr"
        )
    else:
        row.status_label = status_label(att.status)
```

### 收斂效果

- 砍掉 `leave_map` 字典與 `while d <= ...` 迴圈(-18 行)
- 移除「date overlap 計算」邏輯
- 報表 query 變成單一 `outerjoin`

### Parity Test

```python
# tests/test_attendance_report_parity.py(新增)
@pytest.mark.parametrize("employee_id,start,end", [...])
def test_monthly_report_matches_legacy(employee_id, start, end):
    new = build_monthly_report(employee_id, start, end)
    old = build_monthly_report_legacy(employee_id, start, end)
    assert new == old  # 逐 row 比對 date/status_label/punch_in/late_minutes/leave_label
```

跟 §6 同 cadence(merge 後一週 follow-up 刪)。

### Grep Gate(PR description 必貼)

```bash
grep -rn "LeaveRecord" ivy-backend/api/ ivy-backend/services/ \
  | grep -v "salary/engine.py" \
  | grep -v "leaves.py" \
  | grep -v "audit"
```

若有命中:case-by-case 判斷是真的需要直讀 LeaveRecord 還是補丁。PR review 時對勾。

### 前端

`ivy-frontend/src/api/attendance.ts` + 報表頁面**不需改動** — 後端 response 結構不變。

Implementer 手動驗證:
1. 啟動 `start.sh`
2. 開「月度出勤報表」頁面,挑一個有半天請假的員工看顯示
3. 截圖貼到 PR

---

## §8 測試矩陣

### Unit Tests — Service 層 (`tests/test_employee_leave_attendance_sync.py`)

| # | 案例 | 預期 |
|---|---|---|
| U-1 | apply 全天請假 3 天 + 無既有 attendance | 建 3 筆 status=LEAVE / punch=NULL |
| U-2 | apply 全天請假 + 既有 ABSENT row | row 更新為 LEAVE,leave_record_id 寫入 |
| U-3 | apply 半天 + 既有 LATE row | LATE 保留 / partial_leave_hours=4 / late_minutes 重算 |
| U-4 | apply 半天 + 無 punch | ABSENT / partial_leave_hours=4 / leave_record_id 寫入 |
| U-5 | apply 小時 1.5hr + 既有 NORMAL row | NORMAL / partial_leave_hours=1.5 |
| U-6 | apply 對 unapproved leave | raise `LeaveNotApproved` |
| U-7 | apply idempotent(重跑兩次) | 第二次 no-op |
| U-8 | apply 同日已有其他 leave_record_id | raise `LeaveAttendanceConflict` |
| U-9 | revert 全天 + 無 punch | row 刪除 |
| U-10 | revert 全天 + 有 punch | 退回 NORMAL,清 leave_record_id |
| U-11 | revert 半天 | 保留 punch/status/late,清 leave_record_id + partial_leave_hours |
| U-12 | revert idempotent | 第二次 no-op |
| U-13 | reapply 改日期 5/1-5/3 → 5/2-5/4 | 5/1 還原;5/4 新建;5/2/5/3 保留 |
| U-14 | reapply 改 leave_hours 8→4(全天→半天) | revert→apply 路徑;打卡若有則保留 |
| U-15 | apply 對部分請假但缺 start_time/end_time | raise `LeavePartialTimeMissing` |

### Unit Tests — `merge_attendance_with_leave` (`tests/test_attendance_leave_merge.py` 新檔)

| # | 案例 | 預期 |
|---|---|---|
| M-1 | 當日無 approved leave | att.leave_record_id=None,partial_leave_hours=None |
| M-2 | 當日全天 leave + 無打卡 | status=LEAVE,清打卡,leave_record_id 寫入 |
| M-3 | 當日全天 leave + 有打卡(臨時上班) | status 用打卡計算,leave_record_id 寫入,partial_leave_hours=0 |
| M-4 | 當日部分 leave + 有打卡 | 保留 punch,partial_leave_hours 寫入,late_minutes 用 leave-aware 重算 |
| M-5 | 當日部分 leave + 無打卡 | status=ABSENT,leave_record_id + partial_leave_hours 寫入 |
| M-6 | 當日多筆 approved leave(異常資料) | 取最早 id;audit log warning |
| M-7 | helper 不修改 session(只讀) | session.dirty 為空(in-place 改 att 物件而已) |

### Integration Tests — 3 條 attendance 寫入路徑 (`tests/test_attendance_writes_leave_aware.py` 新檔)

| # | 案例 | 預期 |
|---|---|---|
| W-1 | Admin 透過 `create_or_update_attendance_record` 寫入「員工 X、有 approved leave 的某日」 | row 寫入後 leave_record_id 對齊 |
| W-2 | Admin 重複編輯該 row(更新打卡時間) | leave_record_id 保留(未被覆蓋) |
| W-3 | Excel upload 該日 row | leave_record_id 對齊;late_minutes 用 leave-aware 計算 |
| W-4 | 補卡核准重算 `apply_attendance_status` | leave_record_id 保留 |
| W-5 | 上述四條 row 寫入後跑 salary engine | 扣款 = 與 sync 直接寫入相同(merge 與 sync 必須產同一結果) |

### Unit Tests — `compute_late_minutes_with_leave` (`tests/test_attendance_calc.py`)

| # | 案例 | 預期 |
|---|---|---|
| C-1 | 09:00 上班、09:30 打卡、無請假 | late=30 |
| C-2 | 09:00 上班、09:30 打卡、請假 09:00-10:00 | late=0 |
| C-3 | 09:00 上班、09:30 打卡、請假 08:00-10:00 | late=0 |
| C-4 | 09:00 上班、09:30 打卡、請假 09:00-09:15 | late=15 |
| C-5 | 09:00 上班、10:00 打卡、請假 09:00-09:30 | late=30 |
| C-6 | 18:00 下班、17:30 打卡、請假 17:30-18:00 | early_leave=0 |

### Integration Tests — Hook 點 (`tests/test_leaves_attendance_sync.py`)

| # | 案例 | 預期 |
|---|---|---|
| I-1 | POST approve approved=True | AttendanceRecord 寫入(逐日驗證) |
| I-2 | POST approve approved=False(reject) | 無 AttendanceRecord 建立 |
| I-3 | I-1 之後 POST 同 endpoint approved=False | AttendanceRecord 反寫(全天 row 刪) |
| I-4 | I-1 之後 PATCH 改 end_date(延長) | 新增日 寫入,既有不重複 |
| I-5 | I-1 之後 PATCH 改 leave_hours 8→4 | 從 LEAVE 變半天標記 |
| I-6 | I-1 之後 PATCH is_approved=True→None(退審) | AttendanceRecord 反寫 |
| I-7 | I-1 之後 DELETE | AttendanceRecord 反寫;leave row 刪 |
| I-8 | Approve 觸發 `LeaveAttendanceConflict` | 422 / AttendanceRecord 無變化 |
| I-9 | Approve 連點兩次 | 只寫一次,第二次 no-op |
| I-10 | Approve 時 sync 拋例外 | 422 / LeaveRecord 不變(整 transaction rollback) |

### Parity Tests

§6 salary engine 30+ 案例 / §7 月報 30+ 案例,parametrize on 既有 fixture + 邊緣 edge case。

### 既有測試不可破

- `tests/test_leaves.py` — 全綠
- `tests/test_leave_overtime_conflict.py` — 全綠
- `tests/test_salary_engine.py` — 全綠
- `tests/test_attendance_reports.py` — 全綠
- 全套 backend pytest baseline(預估 ~4600 tests)— 0 regression

### Migration Tests (`tests/test_migration_empleavesync.py`)

| # | 案例 | 預期 |
|---|---|---|
| M-1 | upgrade on clean DB | 完成、無報錯 |
| M-2 | upgrade on DB with dups | fail-loud,提示跑 dedupe |
| M-3 | upgrade with approved leaves | backfill 完成,row count 符合預期 |
| M-4 | upgrade 中斷後重跑 | idempotent,結果相同 |
| M-5 | downgrade | schema 還原(LEAVE enum 殘留是預期) |

### 前端手動驗證

| # | 場景 | 預期 |
|---|---|---|
| F-1 | 月度出勤報表,全天請假員工 | 「特休(全天)」/「事假(全天)」 |
| F-2 | 月度出勤報表,半天請假員工 | 「準時 / 特休 4hr」 |
| F-3 | 月度出勤報表,遲到+半天請假員工 | 「遲到 X 分 / 病假 4hr」late 重算過 |
| F-4 | 員工個人考勤查詢頁(若有)| 同樣語義一致 |

Implementer 截圖入 PR。

---

## §9 風險 / Rollout / Rollback

### 風險清單

| # | 風險 | 機率 | 影響 | 緩解 |
|---|---|---|---|---|
| R-1 | Backfill 漏資料 → salary 切換後扣款短少/重複 | 中 | 高 | parity test 30+ 案例 + pre-flight preview + migration row count assert |
| R-2 | 同日多筆部分請假已存在於 prod | 低-中 | 中 | preview script 列衝突 leave_id,staging 內 pre-resolve |
| R-3 | `(employee_id, date)` 既有 dup row | 低 | 中 | migration fail-loud + `dedupe_attendance.py` 預清 |
| R-4 | `ALTER TYPE ADD VALUE` 在 transaction block 內 fail | 低 | 高 | migration 用 `autocommit_block()` |
| R-5 | Salary engine 切換後算出來不一致 | 中 | 高 | _legacy 保留 + parity test gate + merge 後一週 follow-up 才刪 |
| R-6 | 月報切換後顯示異常 | 中 | 中 | _legacy 保留 + parity test + 前端手動截圖驗 |
| R-7 | sync.apply 對「半天 + 沒打卡」寫成 ABSENT 但實際有口頭請假 | 低 | 低 | ABSENT + partial_leave_hours 非 NULL → UI 仍顯示請假 |
| R-8 | LINE 通知 / approval log / quota 既有副作用被誤改 | 中 | 中 | §4 列「不動清單」+ reviewer 對勾 |
| R-9 | Backfill 時間過長阻塞 migration | 低 | 中 | 預估 ~5ms × 1400 筆 = 7s,可接受;超 60s SOP 改 `IVY_SKIP_BACKFILL=1` |
| R-10 | 加班計算原本也 join LeaveRecord overlap 未列入此 PR | 低 | 中 | implementer grep `services/salary/` 確認所有 LeaveRecord 讀取點切換或註明保留 |
| R-11 | Online migration:UNIQUE constraint 加上去阻塞 prod 流量 | 中 | 高 | `CREATE UNIQUE INDEX CONCURRENTLY` + `ADD CONSTRAINT USING INDEX`(§2 已寫);off-peak 部署 |
| R-12 | 3 個既有 attendance 寫入路徑覆蓋 sync 寫入 | 高(如不修) | **高(漏錢)** | §3.5 共用 helper + W-1~W-5 整合測試 + pre-flight grep gate |
| R-13 | 部分請假缺 start_time/end_time(prod 既有資料) | 中 | 中 | migration step 5 fail-loud + `scripts/fix_partial_leave_times.py` + Sync `LeavePartialTimeMissing` 雙保險 |

### Rollout 順序(同 PR 分兩個 commit 塊)

**Step A — Schema + Service + Hook**
1. Migration up:加欄、加 enum、加 unique constraint、跑 backfill
2. sync service 與 5 條 hook 上線
3. `_legacy` 路徑仍是 salary / 月報的 production 讀取源

**Step B — Consumer Cutover**
1. Salary engine 主路徑切到新讀法
2. 月報 query 切到 outerjoin 新讀法
3. _legacy 保留供 parity test 用

### Rollback 路徑

| 情境 | Rollback |
|---|---|
| Migration 階段失敗 | `alembic downgrade -1` 退一版(配合應用 rollback) |
| Salary engine 切換後算錯 | _legacy 還在 → hot-fix commit 切回呼叫 _legacy |
| 月報顯示異常 | 同上,_legacy report 還在 |
| Sync 對 prod 寫入造成 attendance 異常 | `revert(leave_id)` 全量(dry-run 先列影響);必要時直接 `UPDATE ... WHERE leave_record_id IS NOT NULL` + 重 backfill |

### Deploy SOP(PR description 必貼)

```
1. Staging 部署(預演)
   - 跑 scripts/preview_backfill.py 列出衝突數
   - 若衝突 > 0,人工 pre-resolve 寫 RFC
   - 跑 scripts/dedupe_attendance.py --dry-run 確認 dup 是 0
   - 跑 scripts/fix_partial_leave_times.py --dry-run 確認部分請假無缺時段
   - alembic upgrade head(全程不停服,CONCURRENTLY 加 index)
   - 跑 parity test:pytest tests/test_salary_engine_parity.py tests/test_attendance_report_parity.py
   - 跑 W-1~W-5 整合測試:pytest tests/test_attendance_writes_leave_aware.py
   - 點過月報 UI 三個樣本

2. Prod 部署(off-peak,non-stop)
   - 部署前 4 小時跑同樣 pre-flight scripts(時間差中可能新增資料)
   - 部署中:應用 rolling restart;migration 內 CONCURRENTLY 不阻塞 DML
   - backfill 計時觀察(預估 < 60s);若超 60s 不要中斷(let it finish)
   - 部署完成後 24hr 觀察 audit log(尋找 leave_attendance_backfill_conflict)

3. Merge 後 7 天追蹤
   - 確認 salary engine 月底結薪正常(下一個 close-month cycle)
   - 確認 attendance 寫入 3 路徑無 leave_record_id 被誤覆蓋(audit query)
   - Follow-up PR 刪除 _legacy 與 parity test
```

### Follow-ups(PR description「Post-merge cleanup」段)

- [ ] 刪除 `_build_breakdown_for_month_legacy()` + 對應 parity test(merge + 7 天後)
- [ ] 刪除 `build_monthly_report_legacy()` + 對應 parity test
- [ ] 評估「同日多筆部分請假」是否要支援(association table follow-up)
- [ ] 評估 backfill 視窗從 12 個月延伸到全歷史(視 prod row 數)
- [ ] 若加班計算未在此 PR 切換,follow-up plan 補上

---

## 附錄:Implementer 必看的踩雷點

1. **`old_snapshot` 必須在 model 寫回前抓**(§4)— 否則 reapply 拿到的是新值,revert 不到舊範圍
2. **enum 加值用 `autocommit_block()`**(§2)— Postgres `ALTER TYPE ADD VALUE` 不能在 transaction 內
3. **conflicts 放行不擋 / errors fail-loud**(§5)— migration 容錯分流不能混
4. **_legacy 函式 PR 內保留 + follow-up 刪**(§6 §7)— 不是「兩套程式長期共存」
5. **Grep gate**(§3.5 / §7)— PR description 必貼兩次 grep(attendance 寫入路徑 + LeaveRecord 補丁殘留)
6. **不動清單**(§4)— LINE / approval log / quota / force_overlap comment 一個字不改
7. **apply 對 unapproved leave fail-loud**(§3)— 擋住 implementer 偷把 sync 提早
8. **半天 + 沒打卡 = ABSENT(非 LEAVE)**(§3)— consumer 靠 partial_leave_hours 區分
9. **3 個 attendance 寫入路徑必加 merge helper 呼叫**(§3.5)— 不做 = silent payroll bleed,比鬼影遲到嚴重
10. **`CREATE UNIQUE INDEX CONCURRENTLY` 用 autocommit_block 包**(§2)— 否則 Alembic transaction 內會炸
11. **`LeaveUpdate` schema 不含 is_approved**(§4)— 因此沒有 PATCH None→True 路徑;若 implementer 想擴 schema 必須先擴 spec
12. **sync 不主動 invalidate salary cache**(§6)— 既有 `lock_and_premark_stale` 配新讀取源天然成立
13. **部分請假缺 start_time/end_time 必擋**(§3 + §1 validator + migration step 5)— 三道防線

---

**Spec 版本**:1.1
**最後更新**:2026-05-22(advisor review 後補 §3.5 + 5 處修正)
**核可流程**:9 節逐節核可 + advisor catch blocker(2026-05-22 by user)
**1.1 修補項目**:
- §1 in-scope/out-of-scope/Rollout 加 helper、cache 註解、CONCURRENTLY、validator step
- §2 migration 改用 CREATE UNIQUE INDEX CONCURRENTLY + pre-flight bad_leaves check
- §3 加 `LeavePartialTimeMissing` 例外 + `_assert_leave_time_consistent` guard
- **§3.5 全新**:Attendance 寫入路徑 inventory + `merge_attendance_with_leave` helper(解 blocker)
- §4 補註 PATCH 不含 is_approved + lock_and_premark_stale 沿用
- §5 _run_backfill audit_logs 改用對的欄名(entity_type/entity_id/summary)
- §6 加 cache invalidation 既有機制天然成立
- §8 加 §3.5 對應 unit + integration 測試(M-1~M-7 / W-1~W-5)
- §9 風險加 R-11/R-12/R-13;Deploy SOP 加 fix_partial_leave_times step + non-stop deploy 流程
- 附錄踩雷點從 8 條擴成 13 條
