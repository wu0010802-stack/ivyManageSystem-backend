# Approval Status Enum Cutover — 破壞性變更通知

**Effective date**：feat/approval-status-enum-p1 PR merge + prod alembic upgrade 之後
**Affected branch**：`main`（Backend feat/approval-status-enum-p1-2026-05-26-backend）
**Audience**：Frontend team、Data team、任何下游消費 audit log JSON 的服務

---

## 一句話 TL;DR

`LeaveRecord` / `OvertimeRecord` / `PunchCorrectionRequest` 三張表的審核狀態欄位從 `is_approved Boolean nullable` 改為 `status String(20) NOT NULL`；audit log JSON 與 API response 不再含 `is_approved` key，請改讀 `status`。

---

## What changed

### 1. Schema（alembic apvstat01 + apvstat02）

| 表 | 舊欄位 | 新欄位 |
|---|---|---|
| `leave_records` | `is_approved Boolean nullable` | `status String(20) NOT NULL default 'pending'` |
| `overtime_records` | 同上 | 同上 |
| `punch_correction_requests` | 同上 | 同上 |

值對應：

| 舊 `is_approved` | 新 `status` |
|---|---|
| `NULL`  | `'pending'`  |
| `TRUE`  | `'approved'` |
| `FALSE` | `'rejected'` |

CHECK constraint `ck_<table>_status` 限制 `status IN ('pending','approved','rejected')`，寫入其他值會 400。

### 2. Index 變動

- 新增 6 條 status-prefixed index（apvstat01）+ 1 條 `ix_punch_correction_status` (apvstat02)
- 刪除 6 條 is_approved-prefixed index：`ix_leave_emp_approved` / `ix_leave_approved_start_date` / `ix_leave_emp_type_approved` / `ix_overtime_emp_approved` / `ix_overtime_approved_date` / `ix_punch_correction_approval`

下游若有 raw SQL 直接 hint 這些 index 名稱會炸（罕見）。

### 3. API response / audit log JSON（**破壞性**）

**Before**（P3 期間 dual-write）：

```json
{
  "id": 42,
  "is_approved": true,
  "status": "approved",
  ...
}
```

**After**（P4 之後）：

```json
{
  "id": 42,
  "status": "approved",
  ...
}
```

`is_approved` key 從以下路徑消失：

- `/api/leaves/*`、`/api/portal/leaves/*`
- `/api/overtimes/*`、`/api/portal/overtimes/*`
- `/api/punch-corrections/*`、`/api/portal/punch-corrections/*`
- audit log JSON `before` / `after` snapshot（`AuditLog.payload`）

### 4. 不受影響

- `api/activity/pos_approval.py`（POS 日結審核）— 用獨立 `ApprovalLog` 表 + `is_approved` key 名稱，**保留不動**。POS 是不同的 approval domain。
- `Permission.LEAVES_APPROVE` / `Permission.OVERTIMES_APPROVE` enum 沒變。
- 既有業務語意（pending = 未審、approved = 核准、rejected = 駁回）沒變。

---

## 下游 action items

### Frontend（已處理）

P3 branch（feat/approval-status-enum-p3-2026-05-26-frontend）已將 9 個 view 從 `row.is_approved === null/true/false` 切到 `row.status === 'pending'/'approved'/'rejected'`。merge 同步即可。

`src/constants/approvalStatus.ts` 提供 `APPROVAL_STATUS` 常量 + `APPROVAL_STATUS_LABELS` 中文標籤；template 內仍可直接寫字串字面值，看哪邊讀起來清楚就用哪個。

### Data team / ETL / BI

1. **任何讀 `leave_records.is_approved` / `overtime_records.is_approved` / `punch_correction_requests.is_approved` 的 SQL**：column drop 後查不到，請改 `status` 欄位 + 上述值對應。
2. **解析 `audit_logs.payload` JSON 的 ETL**：`is_approved` key 從這三類 audit log 消失。若 ETL 依賴此 key，需 fallback 到 `status` key（同筆 row 必有）。
3. **歷史 audit log 不會 backfill**：apvstat02 之前產生的 audit log 仍含 `is_approved`；之後產生的只含 `status`。需要兼容讀取兩種 schema。

### Backend integration（已處理）

`models.approval.ApprovalStatus` enum 是 source of truth：

```python
from models.approval import ApprovalStatus

leave.status = ApprovalStatus.APPROVED.value  # 'approved'
```

P2 期間用的 dual-write event listener（`_sync_status_from_is_approved` 等）在 P4 step 3 一併刪除。

---

## Rollback

- Alembic `downgrade -1`（從 apvstat02）會 re-add `is_approved` column nullable + 反向 backfill + re-add 6 個舊 index。
- 但 P3 frontend / P4 backend response 已不寫 `is_approved`，rollback 後 column 會永遠 stale。
- 真要 rollback 必須兩端同時退（FE main 退到 P3 merge 之前 + BE alembic downgrade 兩次）。

---

## Timeline

| Phase | What | 狀態 |
|---|---|---|
| P1 | apvstat01 add status column + dual-write listener | merged 到 feat branch |
| P2 | 35 module bidirectional sync via listener | merged 到 feat branch |
| P3 | Frontend 切換到 `status` | merged 到 feat branch（待 PR） |
| P4 | apvstat02 drop is_approved + remove listener | merged 到 feat branch |

合併到 main 與 prod alembic upgrade 之後，本通知正式生效。

---

## 相關 commits

- `e5094d9` feat(approval): add ApprovalStatus enum + P1 listener helper
- `f965575` feat(db): add status column (alembic apvstat01)
- `116732e` + `f12288b` refactor(api): admin + secondary/portal routers switch is_approved writes to status enum (P2)
- `8054c80` refactor: remove is_approved from production response/audit dicts (P4 step 2)
- `48b7898` feat(approval): drop is_approved column + remove dual-write listener (P4 step 3)
