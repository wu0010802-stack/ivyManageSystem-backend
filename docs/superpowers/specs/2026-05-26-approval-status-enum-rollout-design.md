# 審核狀態欄位 nullable boolean → ApprovalStatus enum 漸進遷移設計

**Spec 日期**：2026-05-26
**範圍**：LeaveRecord / OvertimeRecord / PunchCorrectionRequest 三表的 `is_approved: Boolean nullable` 欄位升級為 `status: String + CHECK constraint` + Python 端 `ApprovalStatus(str, enum.Enum)`。
**Rollout 形態**：4-PR 漸進遷移（schema → backend → frontend → cleanup），每階段可獨立 ship 與回滾。

---

## 1. 背景與動機

### 1.1 既有問題

三個審核相關表（`leave_records` / `overtime_records` / `punch_correction_requests`）皆以 nullable boolean 表達三態語意：

```python
is_approved = Column(Boolean, nullable=True, default=None,
    comment="是否核准 (None=待審核, True=核准, False=駁回)")
```

- **讀側**：三個 model 各自定義了 `approval_status` @property 回傳 `'pending' / 'approved' / 'rejected'`，已部分緩解。
- **寫側 & SQL filter 側**：仍大量散佈 `is_approved == True` / `.is_(None)` / `.in_([None, True])` 等直接比對 nullable bool，是教科書 anemic anti-pattern。
- 字串/enum/nullable boolean 三套並存（`LeaveRecord.substitute_status: String(20)` / `AppraisalSummary.status: SummaryStatus` enum / `is_approved: Boolean?`）使審核語意不一致。

### 1.2 為何不是 surgical fix

初始 critique 暗示小型 refactor，實際盤點規模顯著超過：

| 面向 | 數量 |
|---|---|
| Model 受影響 | 3 |
| Backend non-test callsites | ~190 |
| Test files 含 `is_approved` | 43（~240 references） |
| Frontend `.vue` / `.ts` 檔 | 9（admin LeaveView/OvertimeView + 對應 portal + LeaveCalendar.vue） |
| DB index 使用 `is_approved` | 6（leave 4 個 / overtime 2 個 / punch 0 個） |
| Pydantic input 含此 field | 0（input contract 是 `ApproveRequest.approved: bool`） |
| `schema.d.ts` 含此 field type | 0（後端缺 `response_model=`，前端拿 unknown） |

因此採用 **4-PR rollout**，每階段都可獨立 ship、跑 prod 一段時間驗證後再進下一階段。

### 1.3 設計目標

1. 任何階段都保持 prod 可運作、可回滾。
2. 不打破既有 Pydantic input contract（`ApproveRequest.approved: bool`）。
3. 不打破既有 response 形狀（雙寫期間 `is_approved` 與 `status` 並存）。
4. P1+P2 期間 callsite 可用任一寫法（透過 event listener 自動同步）。
5. P3 完成後前端 `schema.d.ts` 真的拿得到 `status` 型別（同步補 `response_model=`）。
6. P4 完成後 schema 乾淨，無 legacy column / index / listener。

---

## 2. Scope & Non-goals

### Scope（本 spec 處理）

- `LeaveRecord.is_approved` / `OvertimeRecord.is_approved` / `PunchCorrectionRequest.is_approved` 三欄位升級為 `status` (String) + `ApprovalStatus(str, enum.Enum)`。
- 三表的 `approval_status` @property 改寫為 `return self.status`（read-side bridge）。
- 對應 6 個 DB index 重建（建立 `status` 版本、最後階段 drop 舊版）。
- 全部 SQL filter / setter / response dict / log 寫入路徑遷移。
- 前端 admin + portal 9 個檔的 template 比對改用字串。
- 補 `response_model=` 讓 `schema.d.ts` 含 `status` field type。

### Non-goals（**不**處理）

- `LeaveRecord.substitute_status: String(20)`（代理人狀態，獨立 state machine，非審核語意）。
- `AppraisalSummary.status: SummaryStatus`（已是 enum，不重複造輪）。
- `ApproveRequest.approved: bool` input contract（保留，避免動到前端 approve dialog）。
- 任何 model 升級為「rich domain model」加 `finalize()` / `approve()` instance method 的更大範圍 refactor（critique 提到但本 spec 排除，scope 控管）。
- 任何 transition table / sign_workflow pattern 推廣（同上，控管）。
- Audit log 既有事件名稱（保留 `is_approved` key 在 audit JSON，避免破壞既有 audit 解析工具）。

---

## 3. 核心設計決策

### 3.1 共用單一 ApprovalStatus enum

三表審核語意完全相同（pending / approved / rejected），不分表獨立定義。

```python
# models/approval.py (新檔)
import enum

class ApprovalStatus(str, enum.Enum):
    """共用審核狀態 enum，由 LeaveRecord / OvertimeRecord / PunchCorrectionRequest 三表使用。"""
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
```

對齊 codebase 既有 enum convention（`Semester(str, enum.Enum)` / `CycleStatus(str, enum.Enum)` / `SummaryStatus(str, enum.Enum)`）— `str, enum.Enum` mixin 使 ORM 存取為純 string、JSON 序列化自然、Pydantic 自動 coerce。

### 3.2 新 column 命名 `status`（避免衝突）

三表既有 `approval_status` @property 已佔用該 attribute 名。新 column 命名為 `status`：

```python
status = Column(
    String(20),
    nullable=False,
    server_default="pending",
    comment="審核狀態：pending / approved / rejected",
)
```

`approval_status` @property 在 P1 同步重寫為：

```python
@property
def approval_status(self) -> str:
    """Read-side bridge：保留既有 caller 不必改動，內部走新 column。"""
    return self.status
```

→ 任何 `record.approval_status` 既有 caller（含 audit log / export / template）零改動就拿到新 SoT。

### 3.3 Storage type 採 String + CHECK，不用 Postgres ENUM

對齊 codebase 既有 enum 存儲慣例（AppraisalStatus / Semester / CycleStatus 都用 `String(20)`）。理由：

- Postgres `ENUM` type 加新值要 `ALTER TYPE ... ADD VALUE` migration，DDL 開銷大。
- App layer 已 enforce enum，DB CHECK constraint 是 defense-in-depth 即可。
- SQLite 沒有原生 ENUM type，跨 dialect test 直接用 String 簡單。

Migration 加 CHECK：

```sql
ALTER TABLE leave_records
  ADD CONSTRAINT ck_leave_records_status
  CHECK (status IN ('pending','approved','rejected'));
```

三表各加一個 CHECK。SQLite 在 `CREATE TABLE` 時支援 CHECK，alembic batch_mode 處理。

### 3.4 雙寫機制：SQLAlchemy attribute event，**分階段方向切換**

**為何用 event listener 而非散落 dual-write**：
- 一處 hook 蓋掉 callsite ~30 個 setter 路徑，零遺漏。
- 已 grep 確認 application code **零 raw SQL UPDATE** 寫 `is_approved`（所有寫入走 ORM），listener 必然觸發。
- alembic migration 內的 `op.execute` 是 schema 動作不是 runtime 寫入，不影響。

**為何不一次雙向同步**：避免「同一 transaction 內前後兩次寫入互相覆蓋」的歧義。改用**分階段方向**：

| 階段 | Listener 方向 | 理由 |
|---|---|---|
| P1 上線後 ~ P2 中段 | `is_approved` setter → 同步 `status` | callsite 還在用 `leave.is_approved = ...`，listener 確保新 column 跟上 |
| P2 完成 ~ P4 前 | `status` setter → 同步 `is_approved` | callsite 全切到 status，舊 column 由 listener 維護給前端 readonly 用 |
| P4 | 移除 listener | column 拆除 |

實作位置：`models/approval.py` 內定義 `register_approval_status_listeners()`，於 `models/__init__.py` 末尾呼叫。

```python
# models/approval.py
from sqlalchemy import event

_BOOL_TO_STATUS = {
    True: ApprovalStatus.APPROVED.value,
    False: ApprovalStatus.REJECTED.value,
    None: ApprovalStatus.PENDING.value,
}
_STATUS_TO_BOOL = {
    ApprovalStatus.APPROVED.value: True,
    ApprovalStatus.REJECTED.value: False,
    ApprovalStatus.PENDING.value: None,
}

def _register_p1_listener(cls):
    """P1+P2 期間：is_approved → status 單向"""
    @event.listens_for(cls.is_approved, "set", propagate=False)
    def _sync_status(target, value, oldvalue, initiator):
        expected = _BOOL_TO_STATUS[value]
        if target.status != expected:
            target.status = expected

def _register_p2_listener(cls):
    """P2 完成後：status → is_approved 單向"""
    @event.listens_for(cls.status, "set", propagate=False)
    def _sync_is_approved(target, value, oldvalue, initiator):
        expected = _STATUS_TO_BOOL[value]
        if target.is_approved != expected:
            target.is_approved = expected
```

**Idempotency guard**：listener 內先比對「目的欄位是否已對齊」再寫，避免無限觸發。`propagate=False` 防止 inheritance 多次掛載。

**P1 → P2 listener 切換**：以 feature flag 控制（`settings.approval_status_writer = "is_approved" | "status"`），P2 PR 同時改 callsite 與切換 flag。或者 P2 完成 PR 包含「移除 P1 listener + 註冊 P2 listener」，純 code 切換無 schema 動作。

### 3.5 Backfill 規則

P1 migration 內以 frozen mapping backfill：

```sql
UPDATE leave_records SET status = CASE
  WHEN is_approved IS TRUE  THEN 'approved'
  WHEN is_approved IS FALSE THEN 'rejected'
  ELSE 'pending'
END;
```

三表各跑一次。frozen-snapshot pattern（permtxt01 style）：mapping 寫死在 migration 內，**不要** import `ApprovalStatus` enum，避免日後 enum 改動影響歷史 migration。

### 3.6 Pydantic Response Model 補完（P3）

目前 `api/leaves.py` / `api/overtimes.py` / `api/punch_corrections.py` 多數 endpoint **缺 `response_model=`**，導致 `schema.d.ts` 不含 `is_approved` field type。

P3 階段補 `LeaveResponseSchema`、`OvertimeResponseSchema`、`PunchCorrectionResponseSchema` 三個 Pydantic class，含 `status: ApprovalStatus` field。同時保留 `is_approved: bool | None` 過渡 field 一段時間（P4 移除）。

例：

```python
class LeaveResponseSchema(BaseModel):
    id: int
    employee_id: int
    leave_type: str
    start_date: date
    end_date: date
    # ... 其他欄位
    is_approved: bool | None  # 過渡，P4 移除
    status: ApprovalStatus    # 新 SoT
    approved_by: str | None
    rejection_reason: str | None
    model_config = {"from_attributes": True}
```

---

## 4. 4-PR Rollout 計畫

每個 PR 在 prod 跑 **至少 3 天** 觀察無 regression 再進下一階段。

### PR P1：Schema + Listener + Backfill（schema 變動）

**Migration**：`apvstat01_add_approval_status_column.py`

- 三表各加 `status` column（String(20), NOT NULL, default 'pending'）。
- 三表各加 CHECK constraint `status IN ('pending','approved','rejected')`。
- backfill `is_approved → status` 三條 UPDATE（frozen mapping）。
- 新增 6 個對應 status 的 index（與舊 is_approved 版本並存）：
  - `ix_leave_emp_status` / `ix_leave_status_start_date` / `ix_leave_emp_type_status`
  - `ix_leave_status_date`（取代 `ix_leave_approval_date`）
  - `ix_overtime_emp_status` / `ix_overtime_status_date`
- downgrade：drop 6 新 index、drop 3 CHECK、drop 3 column。

**Code**：

- `models/approval.py` 新檔：`ApprovalStatus` enum + listener register fn。
- `models/__init__.py` 末尾呼叫 `register_p1_listeners(LeaveRecord, OvertimeRecord, PunchCorrectionRequest)`。
- 三 model 加 `status = Column(...)` field 宣告。
- 三 model `approval_status` @property 改寫為 `return self.status`。
- 三 model `__table_args__` 加新 status 版 Index（與舊 is_approved 版並存）。
- 新增測試 `tests/test_approval_status_p1_dual_write.py`：
  - 寫 `is_approved=True` → 讀到 `status=='approved'`
  - 寫 `is_approved=None` → 讀到 `status=='pending'`
  - `approval_status` property 仍回三字串
  - Idempotency：setter 設成相同值不觸發無謂 UPDATE
  - 三表各跑一遍

**驗收**：

- 全套 `pytest` 零 regression（baseline 5103/0）。
- prod migration 跑完，三表 `SELECT count(*) WHERE status IS NULL` = 0。
- prod 觀察 3 天，所有審核操作正常，`status` 與 `is_approved` 永遠同步（可加一個 monitoring query 定期 diff）。

**回滾**：alembic downgrade 一行，schema 還原。listener code 隨 PR revert 移除。Backfill 之後 `is_approved` 仍是 SoT，新 column 沒用就 drop。

### PR P2：Backend Callsite 切到 status（純 code 變動，無 schema）

**Code 改造範圍**：

- `api/leaves.py` / `api/overtimes.py` / `api/punch_corrections.py` 主流 router。
- `api/portal/leaves.py` / `api/portal/overtimes.py` / `api/portal/punch_corrections.py`。
- `api/leaves_quota.py`（多處 SQL filter）。
- `api/calendar_admin.py`（leave 顯示）。
- `api/exports.py`（`_approval_label` 函式）。
- `api/attendance/reports.py` / `api/portal/attendance.py` / `api/portal/anomalies.py`。
- `utils/attendance_leave_merge.py` / `utils/leave_quota_helpers.py`。
- `services/employee_leave_attendance_sync.py`（leave 退審/重套邏輯，注意 leaves.py:934 snapshot 比對也要改）。
- `scripts/fix_partial_leave_times.py` / `scripts/preview_backfill.py` / `scripts/seed_test_data_*.py`。
- 43 個 test files（240 references）。

**Pattern 對照表**：

| 舊寫法 | 新寫法 |
|---|---|
| `LeaveRecord.is_approved == True` | `LeaveRecord.status == ApprovalStatus.APPROVED.value` |
| `LeaveRecord.is_approved == False` | `LeaveRecord.status == ApprovalStatus.REJECTED.value` |
| `LeaveRecord.is_approved.is_(None)` | `LeaveRecord.status == ApprovalStatus.PENDING.value` |
| `LeaveRecord.is_approved.in_([None, True])` | `LeaveRecord.status.in_([ApprovalStatus.PENDING.value, ApprovalStatus.APPROVED.value])` |
| `leave.is_approved = True` | `leave.status = ApprovalStatus.APPROVED.value` |
| `leave.is_approved = data.approved` | `leave.status = ApprovalStatus.APPROVED.value if data.approved else ApprovalStatus.REJECTED.value` |
| `leave.is_approved = None` | `leave.status = ApprovalStatus.PENDING.value` |
| `was_approved = leave.is_approved is True` | `was_approved = leave.status == ApprovalStatus.APPROVED.value` |
| Response dict `"is_approved": x.is_approved` | **保留 + 加** `"status": x.status`（過渡，P4 移除舊 key） |
| `old_sync_snapshot["is_approved"] = leave.is_approved` | `old_sync_snapshot["status"] = leave.status` |

**Listener 切換**：本 PR 同時移除 P1 listener、註冊 P2 listener（方向反轉為 status → is_approved），確保前端 P3 之前 `is_approved` field 仍正確（前端還在讀）。

**驗收**：

- 全套 pytest 零 regression。
- grep `is_approved` 在 backend 應只剩：
  - 三 model 的 column 宣告（過渡）
  - listener 內部
  - response dict 中的 `"is_approved":` 過渡 key
  - audit log JSON 中的 `is_approved` key（保留兼容 audit parser）
  - 三 model 既有 6 個 is_approved index（P4 才 drop）
- prod 觀察 3 天，前端表現正常。

**回滾**：純 code revert，listener 切回 P1 方向。

### PR P3：Frontend + response_model 補完

**Backend**：

- `api/leaves.py` / `api/overtimes.py` / `api/punch_corrections.py` 加 `response_model=` 與對應 Response Pydantic schema（含 `status: ApprovalStatus` + `is_approved: bool | None` 過渡 field）。
- 重新 dump `openapi.json` 與 frontend `npm run gen:api` 更新 `schema.d.ts`。

**Frontend**（9 個檔）：

- `src/views/LeaveView.vue` / `OvertimeView.vue`
- `src/views/portal/PortalOvertimeView.vue` / `src/components/portal/PortalLeaveList.vue`
- `src/views/leave/LeaveCalendar.vue`（含 `is_approved: boolean | null` type annotation）
- `src/views/portal/components/attendance/AttendanceCardsView.vue` / `AttendanceTableView.vue`
- `src/views/activity/POSApprovalView.vue`
- 對應 test files

**Template pattern 改造**：

```vue
<!-- 舊 -->
<el-tag v-if="row.is_approved === true" type="success">已核准</el-tag>
<el-tag v-else-if="row.is_approved === false" type="danger">已駁回</el-tag>
<template v-if="row.is_approved === null">待審</template>

<!-- 新 -->
<el-tag v-if="row.status === 'approved'" type="success">已核准</el-tag>
<el-tag v-else-if="row.status === 'rejected'" type="danger">已駁回</el-tag>
<template v-if="row.status === 'pending'">待審</template>
```

新增 `src/constants/approvalStatus.ts`：

```typescript
export const APPROVAL_STATUS = {
  PENDING: 'pending',
  APPROVED: 'approved',
  REJECTED: 'rejected',
} as const

export type ApprovalStatus = typeof APPROVAL_STATUS[keyof typeof APPROVAL_STATUS]

export const APPROVAL_STATUS_LABELS: Record<ApprovalStatus, string> = {
  pending: '待審',
  approved: '已核准',
  rejected: '已駁回',
}
```

**驗收**：

- `npm run typecheck` 零 error。
- `npm run gen:api:check` 不漂移。
- `vitest` 全綠。
- 手測 admin LeaveView / OvertimeView / portal 對應頁面、approve / reject / 駁回顯示、calendar 顏色、export PDF 標籤。
- prod 觀察 3 天。

**回滾**：純前端 revert + schema.d.ts 還原。

### PR P4：Cleanup（schema 變動）

**Migration**：`apvstat02_drop_is_approved.py`

- 移除三表的 `is_approved` column。
- 移除 6 個 `is_approved` 版 index。
- downgrade：重建 column + index + backfill from `status`（frozen mapping）。

**Code**：

- 三 model 移除 `is_approved` column 宣告。
- 三 model 移除舊 `is_approved` 版 index。
- 移除 `models/approval.py` 內 P2 listener 註冊 fn。
- 三 model `approval_status` @property 保留（內部已是 `return self.status`），或一併刪除（caller 改用 `status` 直接），by callsite review 決定。
- Pydantic Response schema 移除 `is_approved: bool | None` 過渡 field。
- Response dict 移除 `"is_approved":` 過渡 key。
- audit log key 是否一併改名 by 觀察 audit parser tooling 決定（**保守做法**：保留 audit JSON 內 `"is_approved"` key 不動，避免破壞既有 dashboard / Sentry filter）。
- 前端 schema.d.ts regen，移除 `is_approved` field。

**驗收**：

- 全套 pytest 零 regression。
- prod migration 跑完，三表 schema 內無 `is_approved` column。
- grep `is_approved` 全 repo 應只剩：
  - audit log 內 historical record 的 JSON（read-only）
  - 過渡期間留下的 audit key（如保留）
- 前端 `grep is_approved` 應為 0。

**回滾**：alembic downgrade 重建 column + backfill，code revert listener 與 model field。**這是 4 PR 中回滾最痛的一步**，因此 P4 上線前 P1+P2+P3 必須在 prod 各跑 3+ 天確認穩定。

---

## 5. 風險與已知陷阱

### 5.1 Event listener 觸發時機

`leaves.py:934` 有 transition snapshot 比對：

```python
old_sync_snapshot["is_approved"] = leave.is_approved  # P1 時值
# ...
_apply_leave_update_and_revoke(leave, data, current_user, leave_id)
# 此時 setter 被 caller 呼叫，listener 在 flush 階段觸發

if old_sync_snapshot["is_approved"] is True and leave.is_approved is None:
    sync.revert(...)
```

**驗證過**：
- snapshot 在 setter **之前** 完成 dict 取值。
- listener `set` event 在 setter 呼叫**當下**觸發（不是 flush 階段），但只更新 mirror column。
- 比對 `leave.is_approved` 仍 yield 正確新值（setter 已寫入）。
- listener 寫 mirror column **不影響** `is_approved` 自己的值。

→ snapshot+compare 邏輯在 P1+P2 期間正確運作。P2 改寫後 snapshot key 同步改為 `"status"`、比對改 `ApprovalStatus.APPROVED.value`，邏輯等價。

### 5.2 SQLAlchemy `set` event 在哪個 attribute 觸發

`event.listens_for(cls.is_approved, "set")` 只在 Python 端設值時觸發（`record.is_approved = x`）。**不會** 在以下情境觸發：

- Bulk update `session.query(LeaveRecord).update({"is_approved": True})` — bypass ORM instance event。
- Raw SQL `op.execute(...)`。
- ORM `Session.bulk_update_mappings()` / `bulk_save_objects()`。
- `Session.execute(sa.update(LeaveRecord).values(...))` Core-style update。

**已完整盤點（spec 撰寫當下執行 grep）**：

| 路徑類型 | 命中 |
|---|---|
| `op.execute` 寫三表 `is_approved` | 0（已於 §5 確認） |
| `bulk_update_mappings` / `bulk_save_objects` | 0 |
| `Session.execute(update(...))` Core update | 0 |
| `query(<Model>).update({...})` production code | 0 |
| `query(<Model>).update({...})` test code | **1**（`tests/test_leave_bonus_skip.py:143`） |

→ Production code 零 bulk update bypass，listener 策略完全成立。
→ 唯一一筆 test fixture bulk update 需於 P2 PR 連同其他 callsite 一起改寫成 instance-level loop：

```python
# 舊
session.query(LeaveRecord).update({LeaveRecord.is_approved: None})

# 新（P2）
for lv in session.query(LeaveRecord).all():
    lv.status = ApprovalStatus.PENDING.value
```

**P1 PR 不需要額外 grep 驗證**，本 spec 已完成 audit。

### 5.3 P1→P2 listener 切換時機

P1 listener 移除與 P2 listener 註冊 **必須在同一 PR**（不能拆兩個 PR），否則中間版本兩個 column 都不會自動同步。

### 5.4 audit log key 保留策略（拍板：永久保留 `is_approved`）

`api/leaves.py:1188` 等處在 audit JSON 寫入：

```python
"before": {"is_approved": (True if was_approved else None)},
```

**最終決策（不再 defer）**：audit JSON 的 `is_approved` key **永久保留**，視為與業務 column 解耦的 audit schema。理由：

- Historical audit row 永遠帶舊 key，新舊並存只會更亂。
- audit JSON 已是 PII-aware scrubbed 後的快照，欄位命名與 ORM column 對齊只是巧合，沒有契約。
- 任何 audit parser / Sentry filter / BI query 零改動。

具體做法：

- P2 PR：新增寫 audit 時繼續使用 `"is_approved": True/False/None` 三值（從 `status` derive）。
- P4 PR：column 拆掉後 audit code 仍寫 `"is_approved"` key（從 `status` derive）。

```python
# P2 起的 audit write pattern
_STATUS_TO_BOOL = {"approved": True, "rejected": False, "pending": None}
audit_payload = {
    "before": {"is_approved": _STATUS_TO_BOOL.get(was_status)},
    "after": {"is_approved": _STATUS_TO_BOOL.get(leave.status)},
}
```

此決策同樣套用 OvertimeRecord / PunchCorrectionRequest 的 audit log。

### 5.5 與 permtxt01 的 differences

| 面向 | permtxt01 | 本 spec |
|---|---|---|
| Frozen-snapshot mapping | ✓ 必要 | ✓ 沿用 |
| token_version bump | ✓ 必要（影響 JWT claim） | ✗ 不必要（不影響 auth） |
| 強制全員重登 | ✓ | ✗ |
| 雙寫期 | 短期（同 PR 切換） | 長期跨 4 PR |
| 回滾複雜度 | 高（drop column 同 PR） | 分階段，P1+P4 才動 schema |

### 5.6 SQLite test fixture

Codebase 既有 enum migration 都用 `batch_alter_table` 跨 SQLite + Postgres dialect。本 spec 沿用此 pattern。CHECK constraint 在 SQLite 是 CREATE TABLE 時宣告，batch_mode 自動重建表處理。

### 5.7 P1 migration `NOT NULL + server_default` row count escape hatch

P1 migration 用 `ADD COLUMN status String(20) NOT NULL DEFAULT 'pending'` + 後續 `UPDATE` backfill。三表現有 row 數量級（dev <10K）下這條路徑無風險。

**Escape hatch**：若日後某表 row count 接近或超過 ~100K，需改用三段式以避免長 lock：

```python
# P1 migration 三段式（大表才需要）
op.add_column("leave_records", sa.Column("status", sa.String(20), nullable=True))
op.execute("UPDATE leave_records SET status = CASE WHEN is_approved IS TRUE THEN 'approved' WHEN is_approved IS FALSE THEN 'rejected' ELSE 'pending' END")
op.alter_column("leave_records", "status", nullable=False, server_default="pending")
```

P1 PR 撰寫時實際 row count 檢查 prod DB（從 supabase MCP 或 monitoring）後決定走哪條路徑。

### 5.8 規模感現實check

本 spec 描述的工作量明顯超出最初 critique 「nullable boolean is anti-pattern」的 surgical 印象。實際 reviewer 對 spec 應有的期待：

- P1 PR：~500 行 diff（migration + model + listener + 30 test）。
- P2 PR：~1500 行 diff（190 callsite + 240 test ref）。
- P3 PR：~600 行 diff（9 vue + Pydantic response + schema.d.ts regen）。
- P4 PR：~300 行 diff（migration + cleanup）。

**這個 refactor 的 ROI 主要在 P3 之後**：前端拿到 typed `status: 'pending' | 'approved' | 'rejected'`，IDE/typecheck 真正擋住誤比 nullable bool 的 bug。P1+P2 本身對 prod behavior 是 no-op，是為了 P3+P4 鋪路。

---

## 6. 測試策略

### 6.1 P1 新增測試（`tests/test_approval_status_p1_dual_write.py`）

- 三表各：
  - `is_approved=True` 設值 → `status == 'approved'`
  - `is_approved=False` 設值 → `status == 'rejected'`
  - `is_approved=None` 設值 → `status == 'pending'`
- `approval_status` @property 仍回三字串。
- Idempotency：同值重複設定不觸發無謂 UPDATE（assert query count）。
- 建構時 default：新建 row 未指定 `is_approved` / `status` → backfill 後 `status == 'pending'`。
- Bulk update（若 §5.2 發現） bypass listener 的回歸測試（assert 雙欄不同步以觸發 alert）。

### 6.2 P2 新增測試

- Setter 改向後：`status='approved'` → `is_approved == True`。
- SQL filter 用 `ApprovalStatus.APPROVED.value` 與舊 `is_approved == True` 回傳同 result set（過渡期）。
- `services.employee_leave_attendance_sync.revert` / `reapply` 在新 setter pattern 下仍觸發。

### 6.3 P3 新增測試

- `vitest` 三個 vue 元件 snapshot test：`status='pending' / 'approved' / 'rejected'` 三種顯示。
- `gen:api:check` CI gate 不漂移。

### 6.4 P4 新增測試

- Schema 內無 `is_approved` column（migration 後 `inspect` check）。
- `approval_status` @property（若保留）仍 functional。
- 前端 `grep -r 'is_approved' src/` 應為 0（CI gate 可加）。

### 6.5 Regression

每階段 baseline 5103 pytest 全綠是必要條件。pre-existing 14 fails（test_audit_router 等）不變。

---

## 7. 驗收清單

### P1 收尾條件

- [ ] Migration `apvstat01` upgrade / downgrade 跑過本機 SQLite + dev Postgres。
- [ ] 三表 prod 跑完 `SELECT count(*) FROM <tbl> WHERE status IS NULL` = 0。
- [ ] `tests/test_approval_status_p1_dual_write.py` 30+ test 全綠。
- [ ] 全套 pytest 零 regression（5103/0）。
- [ ] prod 觀察 3 天無 audit log mismatch、無 frontend 顯示異常。
- [ ] 確認 §5.2 bulk update grep 結果為 0 或已處理。

### P2 收尾條件

- [ ] Backend grep `is_approved` 結果只剩 §4.P2 驗收列出的合法剩餘。
- [ ] 全套 pytest 零 regression。
- [ ] prod 觀察 3 天，response payload 含 `status` 與 `is_approved` 兩 key，值永遠同步。
- [ ] Listener 切換成 status → is_approved 方向。

### P3 收尾條件

- [ ] `npm run typecheck` / `npm run gen:api:check` / `vitest` 全綠。
- [ ] 前端 grep `is_approved` 改用 `row.status` 完成。
- [ ] 手測 8 個前端頁面（admin Leave/Overtime + portal 對應 + LeaveCalendar + POSApproval）的審核顯示與 approve/reject 流程。
- [ ] prod 觀察 3 天。

### P4 收尾條件

- [ ] Migration `apvstat02` upgrade / downgrade 跑過。
- [ ] 三表 prod 無 `is_approved` column。
- [ ] 全套 pytest 零 regression。
- [ ] Backend + frontend grep `is_approved` 結果為 0（audit log historical JSON 除外）。
- [ ] prod 觀察 7 天（cleanup PR 觀察期較長，因回滾痛）。

---

## 8. Follow-ups（不在本 spec 範圍）

- `LeaveRecord.substitute_status: String(20)` 升級為 `SubstituteStatus(str, enum.Enum)`（獨立 state machine，可獨立 spec）。
- Critique 提到的 `SalaryRecord.finalize()` / `LeaveRecord.approve()` rich-domain-model 升級 — 經評估 ROI 不成立，**不做**。Service-as-orchestrator pattern 對本 codebase 是 right call。
- AppraisalSummary `sign_workflow` transition table pattern 推廣到 LeaveRecord — 經評估 over-engineering，**不做**（現行 approve endpoint 已足夠表達兩態轉移）。
- 若日後新增第四態 `cancelled` / `revoked`，`ApprovalStatus` enum 加值 + DB CHECK constraint 同步加 + frozen mapping migration 加 backfill 規則即可。

---

## 9. 參考

- `models/leave.py:66-110`（既有 `is_approved` + `approval_status` property）
- `models/overtime.py:32-47, 71-86`
- `models/attendance.py`（PunchCorrectionRequest）
- `alembic/versions/20260521_permtxt01_permissions_to_text_array.py`（frozen-snapshot pattern 參考）
- `alembic/versions/20260318_k7l8m9n0o1p2_add_leave_approval_status_index.py`（既有 is_approved 索引）
- CLAUDE.md §1（權限欄位歷史遷移先例）
