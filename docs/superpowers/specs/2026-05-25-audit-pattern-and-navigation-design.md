# Audit Pattern Tightening + Navigation Restructure — Design

- **Date**: 2026-05-25
- **Scope**: 跨前後端（ivy-backend + ivy-frontend）
- **Origin**: P2 audit checklist 6.1（D action 補進 pattern、軟刪 vs 真刪 explicit audit_summary）+ 6.2（AuditLog 等低頻 view 佔主導航版位、高風險待審紅點）
- **Status**: Design approved, awaiting implementation plan

---

## §1 架構總覽

P2 6.1 與 6.2 合併成單一 spec，理由：兩者共享同一資料源（`audit_log` 表）與同一語意主軸 — **「凡涉及破壞或繞權的操作都要顯眼」**。前端紅點直接依賴 6.1 補齊的 audit_summary 文字標記。

**三條主軸**：

### A. audit_summary 語意明確化（後端）

- middleware 對 HTTP DELETE 自動加「(不可復原)」尾綴 — endpoint 不需動。
- 軟刪 endpoint 顯式呼叫新 helper `utils/audit.mark_soft_delete(request, entity_type, entity_label)`，summary 形如「軟刪 員工 王小明」。
- 範圍：全部 D action endpoint（盤點預估 ~15 處軟刪 + middleware 已涵蓋的所有真刪）。
- 不動 AuditLog schema、不動 `action` 欄位值（軟刪在 DB 仍是 `action='UPDATE'`，靠 summary 文字辨識）。

### B. AuditLog 加 ack 機制（後端）

- 新增 2 欄：`acknowledged_at TIMESTAMP NULL` + `acknowledged_by INTEGER FK users.id NULL ON DELETE SET NULL`。
- 新增 3 endpoint：`GET /audit-logs/high-risk` / `POST /audit-logs/{id}/ack` / `POST /audit-logs/ack-all`。
- 「高風險」query 是 service-layer SQL filter（不是 audit log type）：`action IN (DELETE, BLOCKED_*)` ∪ `summary LIKE '%(不可復原)%'` ∪（`entity_type='user'` ∧ `summary LIKE '%role%|%permission%|%角色%|%權限%'`）。

### C. 前端 IA 重整 + 紅點（前端）

- 新增 2 個一級項目：**工作台**（合併既有「審核工作台」`/approvals` + 高風險 ack）、**報表**（5 個查詢類 + 既有 `/reports`）。
- 11 個超低頻分流：
  - 5 進「報表」新群：操作紀錄、修改紀錄、月度月報、政府申報、經營分析
  - 2 進「系統設定」：考核管理、報名時間設定
  - 1 留「系統設定」原位：一般設定
  - 3 留原處保操作脈絡：年終獎金、年終 payout、廠商付款
- 另外把既有「報表統計」`/reports`（非 11 超低頻內，本屬中低頻）併入新「報表」一級，IA 語意更聚合。
- 紅點 `pendingHighRiskAudit` prop 連到「工作台」一級項目；7 天時間窗 + 「全部標已讀」按鈕。

### Out of scope

- 不動 audit middleware 既有寫入機制（成熟、穩定，碰它風險高）
- 不動既有 30+ entity_type pattern
- 不做 audit 匯出/排程清理（既有 `EXPORT` action 已支援）
- 不重命名既有 router URL（除 `/approvals` 因 IA 合併必須改）
- 不做手機版 sidebar 改造（既有 responsive 沿用）
- 不做 audit detail drawer（沿用既有 AuditLog view 行內展開 changes JSON 的方式）

---

## §2 後端 audit pattern — 軟刪/真刪 explicit

### 2.1 helper 設計

新增於 `ivy-backend/utils/audit.py`：

```python
ENTITY_LABEL_ZH: dict[str, str] = {
    "employee": "員工", "user": "使用者", "salary_record": "薪資單",
    "vendor_payment": "廠商付款", "attachment": "附件", "guardian": "監護人",
    "student": "學生", "leave": "請假單", "overtime": "加班單",
    "shift": "班別", "appraisal": "考核",
    # ... 對齊既有 ENTITY_PATTERNS 30+ entity_type；implementation 階段補完
}


def mark_soft_delete(request: Request, entity_type: str, entity_label: str) -> None:
    """軟刪 endpoint 呼叫；middleware 寫入時用 request.state.audit_summary override 預設模板。"""
    label = ENTITY_LABEL_ZH.get(entity_type, entity_type)
    request.state.audit_summary = f"軟刪 {label} {entity_label}"
    request.state.audit_delete_kind = "soft"


def mark_hard_delete(request: Request, entity_type: str, entity_label: str) -> None:
    """非 HTTP DELETE 但內部 session.delete() 的情境（cascade 真刪）。
    HTTP DELETE 不需呼叫，middleware 自動處理。"""
    label = ENTITY_LABEL_ZH.get(entity_type, entity_type)
    request.state.audit_summary = f"真刪 {label} {entity_label} (不可復原)"
    request.state.audit_delete_kind = "hard"
```

### 2.2 middleware 改動

在 `AuditMiddleware.dispatch()` 寫入前過此函式：

```python
def _decorate_delete_summary(request: Request, summary: str) -> str:
    """HTTP DELETE 自動加尾綴；軟刪/已標 hard 維持原樣。"""
    if request.method == "DELETE" and not getattr(request.state, "audit_delete_kind", None):
        return f"{summary} (不可復原)"
    return summary
```

既有 `_build_summary` 模板不動。`request.state.audit_summary` 由 endpoint override 時也會過此函式（保險，但因 `audit_delete_kind` 已 set，不會重複加尾綴）。

### 2.3 既有軟刪 endpoint 改動清單

依探勘結果，初步預估點（implementation 階段需 grep 補完整清單）：

| 軟刪欄位 / 機制 | endpoint | 工作量 |
|---|---|---|
| `deleted_at = datetime.now()` | `api/attachments.py:246`、`api/students.py:1456` (guardian)、`api/portal/contact_book.py:827`、`api/contact_book.py:81` | 4 處加 1 行 |
| `is_active = False` | `api/employees.py:719` (停用員工)、`api/auth.py` (停用使用者) 等 | ~3 處加 1 行 |
| `lifecycle_status` 終態 | `utils/student_lifecycle.set_lifecycle_status` 已是中央化 → 在 transition orchestrator 內統一加一次 mark_soft_delete | 1 處 |

### 2.4 測試（pytest，新增 ~15 case）

- middleware 對 HTTP DELETE 自動加「(不可復原)」（3 case：純 delete / endpoint 已 set summary / 軟刪 endpoint 不受影響）
- `mark_soft_delete` 正確 set `request.state` （2 case）
- 既有軟刪 endpoint 寫出的 audit log summary 含「軟刪」（每個改動 endpoint 一個 integration case，~7-8 個）
- `mark_hard_delete` 用於非 HTTP DELETE 的真刪情境（cascade，2-3 case）

### 2.5 注意

- 軟刪在 DB 仍是 `action='UPDATE'`，靠 summary 文字「軟刪」/「(不可復原)」辨識。
- 既有 30+ entity_type pattern 與 `_build_summary` 模板維持不動。
- ENTITY_LABEL_ZH 與 ENTITY_PATTERNS key 必須對齊；implementation 階段 unit test 強制 `set(ENTITY_LABEL_ZH) >= set(ENTITY_PATTERNS)`，避免漏譯回退到英文 key。

---

## §3 後端高風險 audit ack 機制

### 3.1 AuditLog schema 變更

新 alembic migration `audrsk01_audit_acknowledged`：

```python
def upgrade() -> None:
    op.add_column("audit_log", sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("audit_log", sa.Column("acknowledged_by", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_audit_log_acknowledged_by", "audit_log", "users",
        ["acknowledged_by"], ["id"], ondelete="SET NULL",
    )
    op.create_index("ix_audit_log_ack_created", "audit_log", ["acknowledged_at", "created_at"])

def downgrade() -> None:
    op.drop_index("ix_audit_log_ack_created", "audit_log")
    op.drop_constraint("fk_audit_log_acknowledged_by", "audit_log", type_="foreignkey")
    op.drop_column("audit_log", "acknowledged_by")
    op.drop_column("audit_log", "acknowledged_at")
```

`acknowledged_by` `ON DELETE SET NULL`：審 audit 的 admin 帳號被刪不影響歷史記錄。
nullable column add 在 Postgres 11+ 是 metadata-only operation — 無鎖風險，audit_log 表大也安全。

### 3.2 「高風險」SQL filter（service layer 純函式）

新檔 `ivy-backend/services/audit_high_risk.py`：

```python
HIGH_RISK_ACTIONS = {
    "DELETE",
    "BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE",
}

def filter_high_risk(query, since: datetime, only_unack: bool = True):
    cond = sa.or_(
        AuditLog.action.in_(HIGH_RISK_ACTIONS),
        AuditLog.summary.like("%(不可復原)%"),  # 共用 §2 真刪尾綴 marker
        sa.and_(
            AuditLog.entity_type == "user",
            sa.or_(
                AuditLog.summary.like("%role%"),
                AuditLog.summary.like("%permission%"),
                AuditLog.summary.like("%角色%"),
                AuditLog.summary.like("%權限%"),
            ),
        ),
    )
    query = query.filter(AuditLog.created_at >= since).filter(cond)
    if only_unack:
        query = query.filter(AuditLog.acknowledged_at.is_(None))
    return query.order_by(AuditLog.created_at.desc())


def classify_risk_kind(row: AuditLog) -> Literal["hard_delete", "blocked", "permission_change"]:
    if row.action in {"BLOCKED_CREATE", "BLOCKED_UPDATE", "BLOCKED_DELETE"}:
        return "blocked"
    if row.action == "DELETE" or (row.summary and "(不可復原)" in row.summary):
        return "hard_delete"
    return "permission_change"
```

權限變更靠 summary substring LIKE 是因為現況 user 權限改動不是獨立 endpoint，是 `PATCH /users/{id}`。Implementation 階段須先 grep 確認既有改 role / permission 的 endpoint 在 summary 寫了什麼，必要時補 summary 模板（例如 `"修改 使用者 王小明 (role: hr → admin)"`）。

**False positive 風險**：substring `%role%` / `%permission%` 可能誤命中無關 summary（例如「修改使用者 role_template」）。緩解：summary 模板補上後限制只在 `entity_type='user'` AND `action='UPDATE'` 才比 LIKE，並在 implementation 階段補 pytest 反例驗 false positive 不發生。若仍有殘留可改為在 audit middleware 設第二個 marker（例如 `request.state.audit_permission_change = True` → summary 加 `"(權限變更)"` 尾綴，類比 §2 的 `"(不可復原)"`）。

### 3.3 新 endpoint（`api/audit.py` 加 3 個）

| Method | Path | Permission | 用途 |
|---|---|---|---|
| `GET` | `/audit-logs/high-risk?days=7&unack_only=true&limit=50` | `AUDIT_LOGS` | 紅點點進去看的列表 |
| `POST` | `/audit-logs/{audit_id}/ack` | `AUDIT_LOGS` | 單筆 ack |
| `POST` | `/audit-logs/ack-all?days=7` | `AUDIT_LOGS` | 全部標已讀 |

Response schema：

```python
class AuditLogHighRiskItem(BaseModel):
    id: int
    action: str
    entity_type: str
    entity_id: int | None
    summary: str
    username: str
    created_at: datetime
    acknowledged_at: datetime | None
    acknowledged_by: int | None
    risk_kind: Literal["hard_delete", "blocked", "permission_change"]


class HighRiskListResponse(BaseModel):
    items: list[AuditLogHighRiskItem]
    unack_count: int  # 紅點數字
    total: int
```

### 3.4 ack 寫入語意

- ack 不刪 audit log row，只 set `acknowledged_at = now()` + `acknowledged_by = current_user.id`。
- ack 過的 row 仍可在 `unack_only=false` 查到（grey 顯示）。
- ack 不可撤銷（無 unack endpoint）。
- ack 動作本身 **不寫新 audit log**（避免無限遞迴與雜訊）。
- 重複 ack 同筆 idempotent（保留第一次的 timestamp 與 user）。

### 3.5 測試（pytest，新增 ~20 case）

- migration up/down idempotent + audit_log row count 不變
- `filter_high_risk` 三類各一 + 邊界（HTTP DELETE / `mark_hard_delete` 標的 UPDATE / BLOCKED_DELETE / 純權限變更）
- `classify_risk_kind` 三類正確、`mark_hard_delete` row 歸 hard_delete 而非 permission_change
- `/audit-logs/high-risk` 權限守衛 + 時間窗 + unack_only flag + pagination
- `/audit-logs/{id}/ack` 重複 ack idempotent + 404 + 權限守衛
- `/audit-logs/ack-all` 時間窗界限 + 已 ack 不重寫 + count 回正確值
- ack 動作不產生新 audit log
- `unack_count` vs `total` 計算

### 3.6 已決開放問題（建議答案已 bake 進設計）

- **Q1: `mark_hard_delete` 標的非 HTTP DELETE 是否納入 high-risk？** → 是。靠 summary `"(不可復原)"` substring。`filter_high_risk` 已含此條件。
- **Q2: BLOCKED_* 量爆要不要 dedup？** → 先不做。觀察一週實際量再決定（5.3 部署後）。

---

## §4 前端 IA 重整 + 紅點

### 4.1 IA 取捨（已決）

採 **方案 A：合併**。既有「審核工作台」`/approvals` 與新「工作台」（高風險 ack）合併為單一一級「工作台」，內部 2 sub-tab（待簽核、高風險事件）。理由：「一個地方看所有待辦」優於字面分流的 IA 純粹度。

### 4.2 AdminSidebar 新結構

```
1. 儀表板               /
2. 工作台 ●            /workbench
   ├─ 待簽核 ●         /workbench/approvals       (既有 /approvals 改址 + 301 redirect)
   └─ 高風險事件 ●     /workbench/high-risk       (新)
3. 人事薪資 ▼
   ├─ 員工管理         /employees
   ├─ 薪資管理         /salary
   ├─ 年終獎金         /year_end/cycles
   ├─ 考核年終 payout   /year-end/appraisal-payout
   ├─ 出勤管理         /attendance
   ├─ 請假管理         /leaves
   ├─ 加班 / 會議      /overtime
   └─ 排班管理         /schedule
4. 學生與班級 ▼        (不動)
5. 園務統計 ▼
   ├─ 招生統計         /recruitment
   └─ 官網報名         /recruitment-ivykids
6. 園務行政 ▼          (不動)
7. 課後才藝 ▼
   ├─ 統計儀表板       /activity/dashboard
   ├─ 報名管理         /activity/registrations
   ├─ POS 收銀         /activity/pos
   ├─ 收款簽核         /activity/pos/approval
   ├─ 課程與用品       /activity/catalog
   ├─ 家長提問         /activity/inquiries
   └─ 點名管理         /activity/attendance
8. 報表 ▼              (新一級)
   ├─ 操作紀錄         /audit-logs                (從「系統設定」遷出)
   ├─ 修改紀錄         /activity/changes          (從「課後才藝」遷出)
   ├─ 月度月報         /admin/gov-reports/monthly (從「人事薪資」遷出)
   ├─ 政府申報匯出     /gov-reports               (從「人事薪資」遷出)
   ├─ 報表統計         /reports                   (從「園務統計」遷入此群)
   └─ 經營分析         /analytics                 (從「園務統計」遷出)
9. 系統設定 ▼
   ├─ 一般設定         /settings
   ├─ 考核管理         /appraisal-management      (從「人事薪資」遷入)
   └─ 報名時間設定     /activity/settings         (從「課後才藝」遷入)
```

**不動的 3 項**（金流/人事敏感，仍留原處保操作脈絡）：
- 年終獎金、年終 payout → 留「人事薪資」
- 廠商付款簽收 → 留「園務行政」

### 4.3 紅點實作

`AdminSidebar.vue` 既有 `pendingApprovals` / `pendingActivityInquiries` prop + `.menu-badge` 樣式沿用。新增：

```ts
const props = defineProps<{
  pendingApprovals?: number;
  pendingActivityInquiries?: number;
  pendingHighRiskAudit?: number;  // 新
}>();

const workbenchBadge = computed(() =>
  (props.pendingApprovals ?? 0) + (props.pendingHighRiskAudit ?? 0)
);
```

父元件（AdminLayout 或 App.vue）拉資料：

```ts
const { unackCount, refresh, stop } = useHighRiskAuditCount();
// 既有 pendingApprovals 來自 approvals store
```

`useHighRiskAuditCount`：
- 每 60 秒輪詢 `GET /audit-logs/high-risk?days=7&limit=1`，只取 `unack_count`
- `visibilitychange` 暫停 hidden tab
- `onScope` cleanup 自動 stop
- 共用 dedupe（避免多元件呼叫 N 次）

### 4.4 新元件清單

| 元件 | 路徑 | 用途 |
|---|---|---|
| `WorkbenchLayout.vue` | `src/views/workbench/` | 工作台 layout shell（2 sub-tab） |
| `WorkbenchApprovalsView.vue` | 同上 | 既有 `ApprovalsView.vue` 改路徑（內容 byte-identical 不動） |
| `WorkbenchHighRiskView.vue` | 同上 | 新：高風險列表 + 三類 risk_kind tag + 單筆 ack + 全部標已讀 |
| `useHighRiskAuditCount.ts` | `src/composables/` | 輪詢 unack_count |
| `api/audit.ts` | 既有檔 extend | 加 `getHighRiskAudits` / `ackAudit` / `ackAllAudits` 3 函式 |

### 4.5 路由 redirect

`router/index.ts`：

```ts
{ path: "/approvals", redirect: "/workbench/approvals" },
```

其他 11 個遷移項目 URL 不變，只動 sidebar 位置 → 外部書籤不受影響。

### 4.6 測試（vitest，新增 ~25 case）

- `useHighRiskAuditCount`：mount/unmount 啟停輪詢、visibilitychange 暫停、`unackCount` reactive
- `WorkbenchHighRiskView`：3 種 `risk_kind` tag 顯示、單筆 ack 按鈕、ack-all 按鈕、empty state、loading state
- `AdminSidebar`：`workbenchBadge` 計算（含 / 不含 / 部分）、新 IA 結構渲染、權限隱藏（無 `AUDIT_LOGS` 看不到「高風險事件」sub-tab）
- `api/audit.ts` 三個新函式對 `_generated/typed` 型別正確、AxiosResp 簽章

---

## §5 測試、migration、部署順序

### 5.1 開發順序

```
BE PR  (feat/audit-pattern-and-ack-2026-05-25-backend)
├─ commit 1: utils/audit.py 加 mark_soft_delete / mark_hard_delete + middleware
│            _decorate_delete_summary + ENTITY_LABEL_ZH dict + pytest (~10)
├─ commit 2: 既有軟刪 endpoint 加 mark_soft_delete call (~10-15 處) + integration test (~7)
└─ commit 3: alembic audrsk01 + AuditLog model 加 ack 欄位 + services/audit_high_risk
             + api/audit.py 加 3 endpoint + pytest (~20)

FE PR  (feat/audit-pattern-and-ack-2026-05-25-frontend)        (依賴 BE deploy)
├─ commit 1: OpenAPI regen (schema.d.ts) + api/audit.ts 加 3 函式
├─ commit 2: useHighRiskAuditCount composable + vitest
├─ commit 3: WorkbenchLayout / WorkbenchApprovalsView / WorkbenchHighRiskView
│            + router redirect /approvals → /workbench/approvals + vitest
└─ commit 4: AdminSidebar.vue IA 重整 + pendingHighRiskAudit prop + 父元件接 composable + vitest
```

### 5.2 部署順序

1. `alembic upgrade heads`（audrsk01）— nullable column add，無鎖
2. 後端 deploy（§2 + §3）— ack endpoint 線上
3. 前端 deploy（§4）— sidebar 新 IA + 紅點輪詢上線
4. dev DB smoke：手動軟刪 / 真刪 / 改 user role → 驗證 audit_summary + 高風險列表
5. 觀察一週 `unack_count` 量級；若 `BLOCKED_*` 爆量再評估 §3.6 Q2 dedup

### 5.3 向後相容

- 舊 audit_log row：`acknowledged_at` NULL → 自動歸「未 ack」；7 天時間窗會自然排除歷史事件。
- 舊 audit_summary（無「(不可復原)」尾綴）：`filter_high_risk` 用 `action IN (DELETE, BLOCKED_*)` 雙條件 OR，舊 row 仍 catch。
- `/approvals` 書籤 301 redirect 自動轉。

### 5.4 驗收 checklist

- [ ] 軟刪一個員工 → AuditLog 看到「軟刪 員工 XXX」、`action=UPDATE`
- [ ] 真刪一個 vendor payment → AuditLog 看到「刪除 廠商付款 #N (不可復原)」、`action=DELETE`
- [ ] 把一個 user role 改了 → 高風險列表能看到、`risk_kind=permission_change`
- [ ] 點某筆 ack → 該筆 grey 顯示、紅點 -1
- [ ] 「全部標已讀」→ 紅點歸零、該批 row 全部有 `acknowledged_at`
- [ ] sidebar 9 個一級項目新結構正確、`/approvals` 301 redirect 到 `/workbench/approvals`
- [ ] 沒 `AUDIT_LOGS` 權限的 admin 看不到「高風險事件」sub-tab，但仍能看「待簽核」sub-tab
- [ ] dev DB pytest + vitest 全綠零回歸
- [ ] OpenAPI drift check pass（兩 repo `openapi-drift` CI job）

### 5.5 工時預估

- BE PR：1.5–2 工作天（含 alembic、~30 pytest case）
- FE PR：2–2.5 工作天（含 IA 重整、~25 vitest case）
- 整合 + bug fix：0.5 天
- **總計：~1 工作週**

### 5.6 已知風險與緩解

| 風險 | 緩解 |
|---|---|
| `BLOCKED_*` 量爆 → 紅點長期高 | 觀察一週實際量；超量時加 dedup（同 user 同 entity 同 action 視為一筆） |
| 紅點輪詢 60s 增加後端負載 | admin 用戶量小（個位數）+ visibilitychange 暫停 → 可忽略 |
| 軟刪 endpoint 漏改 → audit log 看起來像 UPDATE | implementation 階段 grep 並補 ENTITY_LABEL_ZH unit test 強制對齊 ENTITY_PATTERNS |
| 11 個遷移後 user 找不到原本位置 | 路由 URL 不變（除 `/approvals`）→ 書籤可用；submenu hover 顯示新位置可額外加 release note |

---

## 附錄：所影響檔案清單

**後端（ivy-backend）**：
- `utils/audit.py`（既有，加 helper + middleware tweak + ENTITY_LABEL_ZH）
- `services/audit_high_risk.py`（新）
- `api/audit.py`（既有，加 3 endpoint）
- `models/audit.py`（既有，AuditLog 加 2 欄）
- `alembic/versions/audrsk01_audit_acknowledged.py`（新）
- ~10-15 個軟刪 endpoint（加 1 行 helper call）：`api/attachments.py`、`api/students.py`、`api/portal/contact_book.py`、`api/contact_book.py`、`api/employees.py`、`api/auth.py`、`utils/student_lifecycle.py` 等
- `tests/test_audit_*.py`（新增 ~35 case）

**前端（ivy-frontend）**：
- `src/components/layout/AdminSidebar.vue`（IA 重整 + `pendingHighRiskAudit` prop）
- `src/views/workbench/WorkbenchLayout.vue`（新）
- `src/views/workbench/WorkbenchApprovalsView.vue`（從 `ApprovalsView.vue` 改名/搬位）
- `src/views/workbench/WorkbenchHighRiskView.vue`（新）
- `src/composables/useHighRiskAuditCount.ts`（新）
- `src/api/audit.ts`（既有，加 3 函式）
- `src/api/_generated/schema.d.ts`（OpenAPI regen）
- `src/router/index.ts`（`/approvals` redirect + 工作台 routes）
- `src/layouts/AdminLayout.vue` 或 `App.vue`（接 composable）
- `tests/**/*.test.ts`（新增 ~25 case）
