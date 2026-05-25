# 員工離職 Checklist 與自助下載 — 設計

**日期：** 2026-05-25
**狀態：** Draft（待 user 審）
**範圍：** ivy-backend 為主、ivy-frontend Phase 3 配合
**前置：** brainstorm 對話於 2026-05-25 完成決策

---

## 1. 問題

辦理離職現況（`api/employees.py:760-823`）只做兩件事：

1. `Employee.is_active = False`
2. `User.is_active = False` + `token_version += 1`（離職員工 cookie 立刻失效）

實務缺口：

| # | 缺口 | 影響 |
|---|---|---|
| 1 | `services/appraisal/status_aggregator.py:463` 寫死 `Employee.is_active == True` | 離職員工**立刻**從當期考核 cycle dashboard 消失，已累積的指標、分數無管道評議 |
| 2 | 沒有統一「辦理離職」工作流 | admin 必須記得人工跑：標記考核、結算特休、開離職證明、追二代健保退保。漏一項即破壞合規 |
| 3 | 沒有離職證明 PDF | 違反勞基法 §19（雇主不得拒絕請求服務證明書） |
| 4 | 特休餘額無 snapshot | 員工離職後 leave_balances 仍維持運算，無基準對照「離職當下還剩多少」 |
| 5 | 離職員工取不到自己歷史資料 | `is_active=False` 立刻 401，無從下載過去薪資 / 出勤；申請信用卡、找下份工作要回頭請 admin 代查 |

實際**已存在**不需重做：薪資月中離職折算（`services/salary/proration.py`）、二代健保補充保費（`insurance_service.py:760`）、年度扣繳憑單（`api/gov_reports.py:714`，已涵蓋離職員工）。

## 2. 方案總覽

新建統一編排層 `services/offboarding/`，以單一 SQLAlchemy transaction 串接 5 個 step。失敗整筆 rollback 保證 DB 一致性。新表 `employee_offboarding_records` one-to-one 對應 Employee，存 checklist 完成狀態、特休 snapshot、PDF 路徑、自助下載 token hash。

新 router `api/offboarding.py` 取代既有 `api/employees.py:759-824` 離職邏輯，舊 endpoint 短期 deprecation passthrough。

員工自助下載走 magic-link：admin 於離職後手動產生 256-bit random token、複製貼至公司 email 寄員工；30 天 / 3 次下載上限後失效；token hash 存 DB（sha256），明文不留。

## 3. 分 Phase 實作

| Phase | 工作日 | 目標 | 內含 |
|---|---|---|---|
| **Phase 1** | 3-4 天 | dashboard 救火 + checklist 框架 | ① `employee_offboarding_records` 表 + migration `offb0001` ② orchestrator 殼 + step framework ③ snapshot_leave + mark_appraisal step ④ aggregator filter 改條件 ⑤ revoke_user 抽出 ⑥ 「辦理離職」endpoint 改走 orchestrator ⑦ NHI 退保旗欄與 PATCH endpoint |
| **Phase 2** | 3-4 天 | 文件 + 自助下載 | ① 離職證明 PDF (§19) + endpoint ② magic-link token 產 / 驗 / 撤 ③ ZIP 下載 endpoint（離職證明 + 過去 12 月薪資 + 出勤 CSV） ④ admin 重發 / 撤銷 token |
| **Phase 3** | 2 天 | 前端 + 整合測試 | ① EmployeeForm「辦理離職」改開一鍵 modal ② 新 OffboardingView 清單頁 ③ Playwright e2e: 一鍵 → 預覽 → 確認 → 下載證明 ④ 舊 `/employees/{id}/resign` 移除 |

各 Phase 可獨立 merge；Phase 2 前端 magic-link panel 在 Phase 3 才 ship，admin 後台暫無 UI（只能 curl / 等 Phase 3）。

## 4. 資料模型

### 4.1 新表 `employee_offboarding_records`

```python
class EmployeeOffboardingRecord(Base):
    __tablename__ = "employee_offboarding_records"

    employee_id = Column(Integer, ForeignKey("employees.id", ondelete="CASCADE"),
                         primary_key=True)
    resign_date = Column(Date, nullable=False)
    resign_reason = Column(Text, nullable=True)

    opened_at = Column(DateTime, nullable=False)
    opened_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=False)

    user_revoked_at = Column(DateTime, nullable=True)
    appraisal_marked_at = Column(DateTime, nullable=True)
    leave_snapshot_at = Column(DateTime, nullable=True)
    certificate_generated_at = Column(DateTime, nullable=True)

    leave_balance_snapshot = Column(JSONB, nullable=True)
    certificate_pdf_path = Column(Text, nullable=True)
    nhi_unenroll_submitted_at = Column(DateTime, nullable=True)

    magic_link_token_hash = Column(Text, nullable=True)
    magic_link_expires_at = Column(DateTime, nullable=True)
    magic_link_revoked_at = Column(DateTime, nullable=True)
    magic_link_download_count = Column(Integer, default=0, nullable=False)
    magic_link_last_used_at = Column(DateTime, nullable=True)

    closed_at = Column(DateTime, nullable=True)
    closed_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)

    __table_args__ = (
        Index("ix_offboarding_resign_date", "resign_date"),
        Index("ix_offboarding_open_status", "closed_at",
              postgresql_where=text("closed_at IS NULL")),
    )
```

**模型放在 `models/offboarding.py`** 新檔。`employee.py` 已 200+ 行，新模組擴充加 60 行語意不同 model 會模糊 Employee 邊界。

**`Employee.offboarding_record`** 反向：`relationship("EmployeeOffboardingRecord", uselist=False, backref="employee")` — one-to-one。

### 4.2 PK 與索引設計

- **PK = `employee_id`**：一人一筆，重新雇用不在此 spec scope（未來支援需另拆 history 表）。PK 自動防呆「同人開兩單」。
- **`ix_offboarding_resign_date`**：清單頁時間範圍查詢。
- **`ix_offboarding_open_status` partial index**：加速「未結案 checklist」查詢；workspace 慣例（見 `ix_guardians_user_active`）。

### 4.3 `leave_balance_snapshot` JSONB

```jsonc
{
  "snapshot_date": "2026-06-15",
  "special_leave_days_total": 14,
  "special_leave_days_used": 9,
  "special_leave_days_remaining": 5,
  "daily_wage": 1800,
  "payout_amount": 9000,
  "calc_rule_version": "labor_act_38_2026_v1"
}
```

JSONB 而非開欄位的理由：特休結算邏輯未來會調整（年資加碼、政府公告），snapshot 只在離職當下一次性算，無後續查詢條件需求。

### 4.4 為何 magic-link 欄位內嵌而非獨立 `offboarding_tokens` 表

每員工最多 1 個 active token，重發即覆寫；獨立表會多 JOIN 但無領域 / 查詢好處。

### 4.5 Migration `offb0001`

```python
def upgrade():
    op.create_table("employee_offboarding_records", ...)
    op.create_index("ix_offboarding_resign_date",
                    "employee_offboarding_records", ["resign_date"])
    op.create_index("ix_offboarding_open_status",
                    "employee_offboarding_records", ["closed_at"],
                    postgresql_where=text("closed_at IS NULL"))

def downgrade():
    op.drop_index("ix_offboarding_open_status")
    op.drop_index("ix_offboarding_resign_date")
    op.drop_table("employee_offboarding_records")
```

**既有離職員工不回填。** 歷史離職員工的 record 為空，aggregator filter 改後他們會出現在當期 cycle（fix 不是 bug，原本就該出現）。

## 5. Orchestrator 編排

### 5.1 主入口

```python
# services/offboarding/orchestrator.py
def process_offboarding(
    session: Session,
    employee_id: int,
    resign_date: date,
    resign_reason: str | None,
    operator_user_id: int,
) -> OffboardingResult:
    """
    一鍵離職主入口。所有 step 在同一 transaction，失敗整筆 rollback。
    呼叫端負責 session.commit()。
    """
```

**modal 不出現勾選項，固定全跑 5 step**（符合「一鍵」精神，admin 不必每次想哪該勾）。

### 5.2 Step 接口

```python
class StepResult(TypedDict):
    step: str
    status: Literal["completed", "skipped", "failed"]
    completed_at: datetime | None
    payload: dict | None
    error: str | None
```

### 5.3 5 個 step（固定順序）

| # | Step | 函式 | 失敗策略 |
|---|---|---|---|
| 1 | `mark_appraisal` | `steps.mark_appraisal.run(session, ctx)` | 純寫 `appraisal_marked_at` audit timestamp，不會失敗 |
| 2 | `snapshot_leave` | `steps.snapshot_leave.run(session, ctx)` | 失敗整筆 rollback，回 `422 LEAVE_BALANCE_NOT_FOUND` |
| 3 | `prefill_leave_payout` | `steps.snapshot_leave.prefill_salary(session, ctx, snapshot)` | 依賴 step 2；找離職當月 SalaryRecord，填 `unused_leave_payout = days * daily_wage`；無則新建 stale=True。失敗 rollback |
| 4 | `revoke_user` | `steps.revoke_user.run(session, ctx)` | 抽自 `api/employees.py:783-806`。resign_date > today 不撤（通知期保留 cookie） |
| 5 | `generate_certificate` | `steps.generate_certificate.run(session, ctx)` | 呼叫 PDF service 寫檔回 path。**檔案系統失敗 = 整筆 rollback**（DB 一致性優先，admin 重試即可） |

### 5.4 順序固定理由

- `mark_appraisal` 先：純 audit，無依賴
- `snapshot_leave` 必須在 `prefill_leave_payout` 前
- `revoke_user` 放第 4：前 step 失敗 rollback 時 user 帳號不會被無意義撤
- `generate_certificate` 最後：產出物失敗 rollback 才不留殭屍 PDF

### 5.5 不採 saga / event bus 理由

- 5 step 全在同 DB 內無對外不可逆操作 → transaction 已足夠
- 系統內無 event bus 基建 → 引入會增 1-2 週設計負擔，無對應收益
- 平行：SQLAlchemy session 非 thread-safe，5 step 總時間 < 200ms，平行無收益

### 5.6 既有 `_mark_employee_salary_stale` 行為保留

原 `api/employees.py:773-781` 觸發 stale 的邏輯在 step 3 `prefill_leave_payout` 內呼叫，行為一致。

## 6. API 端點

### 6.1 新 router `api/offboarding.py`

| Method | Path | Permission | Phase |
|---|---|---|---|
| `POST` | `/offboarding/{employee_id}/preview` | `EMPLOYEE_WRITE` | 1 |
| `POST` | `/offboarding/{employee_id}/process` | `EMPLOYEE_WRITE` | 1 |
| `GET`  | `/offboarding/{employee_id}` | `EMPLOYEE_READ` | 1 |
| `GET`  | `/offboarding/{employee_id}/certificate.pdf` | `EMPLOYEE_READ` 或 admin self | 1 |
| `PATCH` | `/offboarding/{employee_id}/nhi-unenroll` | `EMPLOYEE_WRITE` | 1 |
| `POST` | `/offboarding/{employee_id}/magic-link` | `EMPLOYEE_WRITE` | 2 |
| `DELETE` | `/offboarding/{employee_id}/magic-link` | `EMPLOYEE_WRITE` | 2 |
| `GET`  | `/offboarding/download` | 公開（IP rate limit） | 2 |
| `GET`  | `/offboarding/` | `EMPLOYEE_READ` | 3 |

### 6.2 Preview endpoint（modal 開啟時）

```jsonc
// POST /offboarding/{employee_id}/preview
// Request
{ "resign_date": "2026-06-15", "resign_reason": "個人因素" }

// Response 200
{
  "employee_id": 42,
  "employee_name": "王小明",
  "resign_date": "2026-06-15",
  "preview": {
    "user_account_will_be_revoked": true,
    "leave_snapshot": { "special_leave_days": 5, "daily_wage": 1800, "payout_amount": 9000 },
    "salary_record_target": { "year": 2026, "month": 6, "exists": true, "will_be_marked_stale": true },
    "appraisal_in_flight_cycles": [
      { "cycle_id": 12, "cycle_name": "2026 上半年", "current_score": 85.3 }
    ],
    "certificate_pdf_ready_to_generate": true
  },
  "warnings": [
    "員工有 1 個進行中考核 cycle，標旗後仍保留於評議名單需 admin 人工結算"
  ]
}
```

純讀，無 DB 寫入。

### 6.3 Process endpoint（modal「確認辦理」）

```jsonc
// POST /offboarding/{employee_id}/process
// Request
{ "resign_date": "2026-06-15", "resign_reason": "個人因素" }

// Response 200
{
  "employee_id": 42,
  "resign_date": "2026-06-15",
  "is_active": false,
  "user_account_revoked": true,
  "steps": [...],  // 5 個 StepResult
  "certificate_download_url": "/api/offboarding/42/certificate.pdf"
}
```

### 6.4 錯誤碼

| Status | Detail | 情境 |
|---|---|---|
| `400` | `RESIGN_DATE_BEFORE_HIRE` | resign_date < hire_date |
| `400` | `RESIGN_DATE_TOO_FAR_FUTURE` | resign_date > today + 90 天 |
| `404` | `EMPLOYEE_NOT_FOUND` | 員工不存在 |
| `409` | `ALREADY_OFFBOARDED` | `employee_offboarding_records.employee_id` 已存在 |
| `422` | `LEAVE_BALANCE_NOT_FOUND` | 員工無 `leave_balances` row |
| `500` | `CERTIFICATE_GENERATION_FAILED` | PDF 寫檔失敗 |

每個錯誤後端整筆 rollback。

### 6.5 舊 endpoint 處理

`POST /employees/{id}/resign` 保留路由作 deprecation passthrough（內部直接呼叫 `process_offboarding`），不 break 既有測試 / 前端呼叫。Phase 3 前端切完後在同 PR 拔掉。

## 7. 離職證明 PDF（§19）

### 7.1 內容

```
─────────────────────────────────────────────
            離職證明書
─────────────────────────────────────────────
扣繳義務人：常春藤幼兒園
統一編號：12345678
公司地址：...

茲證明：

姓名：王小明
身分證字號：A123456789       ← PDF 印完整（離職證明實務需供下家雇主驗證，不 mask）
到職日期：2025-08-01
離職日期：2026-06-15
擔任職務：教保員

特此證明。

                    負責人簽章：______________
                    證明日期：2026-06-15
─────────────────────────────────────────────
```

**不寫離職原因**（§19 明文禁記載對受僱人不利之事項，且實務上不利於員工後續求職）。

### 7.2 實作

```
services/employee_offboarding_certificate_pdf.py
    def generate(session, employee_id) -> Path
```

字型用 `utils/pdf_fonts.register_cjk_font()` 既有 helper（Noto Sans TC TTF），與其他 PDF service 統一。

PDF 檔放 `storage/offboarding_certificates/{employee_id}_{resign_date}.pdf`，路徑寫入 `certificate_pdf_path`。

## 8. Magic-link 自助下載（Phase 2）

### 8.1 Token 生命週期

```
admin 在離職員工 detail 頁按「產生下載連結」
  ↓
secrets.token_urlsafe(32)  → 256-bit url-safe random
  ↓
hash = sha256(token).hexdigest()
寫 employee_offboarding_records:
  magic_link_token_hash    = hash
  magic_link_expires_at    = now() + 30 days
  magic_link_revoked_at    = NULL
  magic_link_download_count = 0
回前端原 token（只此一次出現）admin 複製貼到 email
  ↓
員工點 https://ivy.../offboarding-download?token=<原 token>
  ↓
後端 hash 比對 + 檢查 expires/revoked/count
通過 → 串流 ZIP；download_count += 1，達 3 視同 revoked
```

### 8.2 安全強化

| 防線 | 機制 |
|---|---|
| Token 強度 | 256-bit url-safe random，brute force 不可行 |
| DB 外洩 | hash 存 DB，明文不留；丟失即 revoke 重發 |
| Token enumeration | IP rate limit 每分鐘 10 次（防陌生 IP 試誤；與「token 3 次下載上限」分屬不同 layer：rate limit 是全局 IP 控、3 次上限是合法持有者本人下載次數控） |
| URL log 洩漏 | middleware 過濾 query string `token=` 改 `token=***` |
| Browser 執行 | `X-Content-Type-Options: nosniff` + `Content-Disposition: attachment` |
| Session 殘留 | endpoint 不設 cookie / session，純無狀態 |
| Audit | 記下載 IP / User-Agent |
| 失敗回應 | 統一 410 Gone，不暴露具體原因（過期？revoke？） |

### 8.3 ZIP 內容

```
ivy-offboarding-{employee_name}-{resign_date}.zip
├── 離職證明.pdf
├── 薪資明細-2025-06-至-2026-05.pdf   ← 過去 12 月 SalaryRecord，串接 salary_slip.py
└── 出勤紀錄-2025-06-至-2026-05.csv   ← attendance 12 月匯出
```

員工到職不滿 12 月 → 從到職日起算。不補空白月。

### 8.4 Email 寄送

**系統不寄**，由 admin 手動複製 token 進公司 email。理由：

- 系統內無 email 寄發基建（SMTP / SendGrid 需另 1-2 週）
- admin 複製貼上 30 秒，不值得加基建
- email content 由 admin 視員工關係調整，模板化反而僵化

### 8.5 過期後 admin 仍可查歷史

`magic_link_expires_at`、`download_count`、`last_used_at` 保留在 record。前端顯示「30 天前產生過，已下載 2 次，2026-05-15 19:23 自 1.2.3.4 最後下載」。

## 9. 前端（Phase 3）

```
ivy-frontend/src/
├── api/offboarding.ts            # 6 個 wrapper，用 AxiosResp<'/offboarding/...'>
├── views/admin/
│   ├── OffboardingView.vue       # 清單頁（新菜單項）
│   └── EmployeeForm.vue          # 既有頁，「辦理離職」改開 OffboardingModal
├── components/offboarding/
│   ├── OffboardingModal.vue      # 一鍵 modal：date + reason + 預覽 + 確認
│   ├── OffboardingPreviewPanel.vue
│   ├── OffboardingStepsResult.vue
│   ├── MagicLinkPanel.vue        # 產 / 撤 / 顯示狀態
│   └── ChecklistCard.vue
└── stores/offboardingStore.ts    # Pinia store
```

**Magic-link token 顯示：** ElDialog 強迫 admin 複製到剪貼簿後永不重顯（onClose 後 token 從 state 清掉）。

**清單頁：** AdminSidebar「人事管理 → 離職管理」新菜單項，gated by `EMPLOYEE_READ`。

**Modal 流程：**
1. 開啟 → 自動呼叫 `/preview` 顯示「將要做的事」
2. admin 確認 → `/process`
3. 回應後顯示 5 step 結果 + 「下載證明 PDF」按鈕
4. 若含 failed → 紅字 + 「重試」按鈕（重叫 process）

## 10. Permission 與 audit

### 10.1 Permission

使用既有 `EMPLOYEE_READ`、`EMPLOYEE_WRITE` IntFlag bit，**不**新增 permission bit（避免 32-bit 邊界又加負擔）。

### 10.2 Audit log

| Action | 觸發時機 | meta |
|---|---|---|
| `OFFBOARDING_PROCESSED` | orchestrator 結束 | `{resign_date, steps_completed: [...]}` |
| `OFFBOARDING_MAGIC_LINK_GENERATED` | POST magic-link | `{expires_at}` |
| `OFFBOARDING_MAGIC_LINK_REVOKED` | DELETE magic-link | `{reason: "manual_revoke"}` |
| `OFFBOARDING_MAGIC_LINK_DOWNLOADED` | GET download 成功 | `{ip, user_agent}` |
| `OFFBOARDING_NHI_FLAG_UPDATED` | PATCH nhi-unenroll | `{submitted: true|false}` |

使用既有 `request.state.audit_*` middleware 機制。

### 10.3 Sentry PII denylist 同步

新欄位屬 PII / 機敏：

- 後端 `utils/sentry_init._PII_KEY_SUBSTRINGS` 加 `resign_reason`、`leave_balance_snapshot`、`certificate_pdf_path`
- 前端 `src/utils/sentry.PII_KEY_SUBSTRINGS` 同步

**注意：** `certificate_pdf_path` 是檔案路徑非個資，但路徑含員工姓名 → 一併加入。

## 11. 測試策略

### 11.1 測試金字塔

| 層 | 檔案 | case 估計 |
|---|---|---|
| Pure unit | `tests/test_offboarding_steps_*.py`（一 step 一檔） | 5 × 3-5 = ~20 |
| Orchestrator | `tests/test_offboarding_orchestrator.py` | ~10 |
| API endpoint | `tests/test_offboarding_api.py` | ~15 |
| Migration | `tests/test_offboarding_migration.py` | 3 |
| Aggregator regression | `tests/test_appraisal_aggregator.py` | +1 |
| Public download 安全 | `tests/test_offboarding_download_security.py` | 6 |
| 前端 vitest | `tests/views/OffboardingModal.test.ts` 等 | ~10 |
| E2E | `e2e/offboarding.spec.ts` | 1 |

### 11.2 回歸保護

- `_mark_employee_salary_stale` 行為不變
- `aggregate_all_active_employees_status` filter 改後：is_active=True 員工繼續出現 + 新加「離職於 cycle 內」員工也出現
- `User.token_version++` 與 `User.is_active=False` 行為不變（既有 `test_employee_offboard_revokes_user.py` 不改、必過）
- `api/gov_reports.py:/withholding` 年度匯出對離職員工不受影響（仍不篩 is_active）

### 11.3 公開 download endpoint 安全 case

1. 過期 token → 410（不暴露具體原因）
2. revoked token → 410
3. download_count = 3 後 → 410
4. token 試誤連 11 次 → 429（IP rate limit）
5. log 中 query string 不含 token 明文
6. response header 含 `nosniff` + `attachment`

## 12. OpenAPI / TS codegen

Phase 1、Phase 2 後端 router merge 後：

```bash
cd ivy-backend && python scripts/dump_openapi.py
cd ivy-frontend && npm run gen:api
```

CI `openapi-drift` job 自動把關。

Phase 3 前端開發前先確認 `schema.d.ts` 含 `/offboarding/*` 全部 path。

## 13. 跨前後端規範對齊

| 規範（workspace CLAUDE.md）| 本 spec 對齊 |
|---|---|
| 權限位元 > 32-bit BigInt | 不新增 permission bit，免擔心 |
| 新 router 需要服務注入 | `services/offboarding/orchestrator.py` 純函式無 singleton，無需 `main.py` 注入 |
| Migration 先行 | `offb0001` Phase 1 第一個 commit，frontend 不依賴可獨立 |
| Pydantic schema 異動 → 前端同步 | schema.d.ts 兩階段 regen |
| Sentry PII denylist 兩邊同步 | §10.3 已列 |
| 後端 logging.getLogger，不用 print | orchestrator / steps / endpoint 全用 logger |

## 14. 風險與緩解

| 風險 | 影響 | 緩解 |
|---|---|---|
| PDF 字型 / TTF 失敗 | 整筆離職 rollback 卡死 admin | Phase 1 第一週 dev 環境驗證 `register_cjk_font()` 行為；CI 加 PDF 生成 smoke test |
| Magic-link token 外洩 | 員工歷史薪資/出勤被陌生人拿到 | hash 不留明文 + IP rate limit + 30 天 / 3 次上限；admin 隨時可 DELETE |
| aggregator filter 改後 cycle 內混入大量歷史離職員工 | dashboard 爆增、admin 評議多餘工作 | filter 條件嚴格為 `(is_active=True) OR (resign_date BETWEEN cycle.start AND cycle.end)`，只含當期離職 |
| 既有舊離職員工無 record | 後台清單頁不顯示 | by design：清單頁標題明示「Phase 1 上線後新離職」；歷史離職員工照查 Employee.resign_date 直接看 |
| Phase 2 magic-link 前端未 ship 前 admin 無法用 | Phase 2 ship 後 UX 有空窗 | Phase 3 同 PR ship 前端 magic-link panel；Phase 2 上線後 admin 可 curl 暫用（已有 `EMPLOYEE_WRITE` admin 教學）|

## 15. 不在此 scope 的項目

- 重新雇用流程（重新建立 Employee + 處理歷史 offboarding_record）
- 二代健保自動退保（健保署 eMTC 對接）
- 離職面談記錄上傳 / 金流結清確認 workflow（admin 內部協作議題）
- 「截至離職日」薪資證明 PDF（年度扣繳憑單足夠，避免員工誤以為是正式扣繳憑單）
- Email 自動寄送 magic-link（需 SMTP / SendGrid 基建）
- ex-employee 永久 read-only login（攻擊面大、Phase 1-3 magic-link 已足）

## 16. 落地順序與 PR 切分

| PR | 內容 | 依賴 |
|---|---|---|
| PR-1 | Migration `offb0001` + Model + orchestrator 殼 + 5 step + test | - |
| PR-2 | `api/offboarding.py` 5 endpoint（preview/process/get/cert.pdf/nhi）+ test + aggregator filter 改 | PR-1 |
| PR-3 | 舊 `/employees/{id}/resign` 改 deprecation passthrough | PR-2 |
| PR-4 | Magic-link 3 endpoint + ZIP download + 安全 test | PR-2 |
| PR-5 | 前端 OffboardingModal + Store + EmployeeForm 接入 | PR-2 |
| PR-6 | 前端 OffboardingView 清單頁 + MagicLinkPanel | PR-4, PR-5 |
| PR-7 | E2E + 移除舊 endpoint passthrough | PR-3, PR-6 |
