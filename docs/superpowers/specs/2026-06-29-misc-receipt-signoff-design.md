# 雜項收款簽收（misc_receipts）設計

- 日期：2026-06-29
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 狀態：設計待 user review

## 1. 背景與目標

系統現有「**廠商付款簽收**」（`vendor_payments`）是**支出側（AP）** 工具：園所付錢給廠商（清潔用品、教具、食材等），登錄付款單 → 收集廠商簽收（簽名圖或紙本照片）→ 留稽核痕跡。狀態只有 `pending` / `signed` 兩態，終態鎖定，財報把它當**支出**聚合。

本案要把這套成熟的「**金額 + 簽收 + 簽名 + 附件**」機制，鏡像成一個**收入側**模組，記錄學費 / 活動以外的**雜項進帳**（場地租金、捐款、補助款、二手義賣、退費回收等），由繳款方簽收 / 我方留收據佐證，並納入財報的「收入」聚合。

**核心定位**：獨立的收入側模組，與「廠商付款簽收」鏡像對稱。**廠商付款模組完全不動**，避免回歸風險。

## 2. 已確認的設計決策

| # | 決策點 | 結論 |
|---|---|---|
| 1 | 實現形態 | **全新獨立模組**（新表 `misc_receipts` + 新頁面 + 新權限），不污染 `vendor_payments` |
| 2 | 機制 | 復用「金額 + 簽收 + 簽名 + 附件」，pending→signed 兩態，終態鎖定 |
| 3 | 款項類別 | **加 `category` 枚舉欄位**（一組預設類別，見 §4） |
| 4 | 財報聯動 | **納入財報「收入」聚合**，與廠商付款對稱（刷新同一財務快取） |
| 5 | 收據 | **復用簽收即可**，不另做正式收據號 / PDF |
| 6 | 共用策略 | 後端純邏輯 helper 安全抽共用；前端 UI 元件各自獨立一份（付款側零回歸風險）。詳見 §9 |

YAGNI 排除（已與 user 確認）：❌ 正式收據生成（收據號 / PDF）；❌ 審批 / 駁回流程；❌ 退款流程。

## 3. 系統定位圖

```
收入側 (Revenue)                         支出側 (Expense)
├── 學費 fees (StudentFeeRecord/Payment)
├── 活動 POS (activity)
└── 雜項收款 misc_receipts  ← 本案 ★      └── 廠商付款 vendor_payments
         │                                         │
         └──────────── finance_cache（共用財務快取）────────────┘
              收入聚合 +misc_receipts            支出聚合 vendor_payments
```

雜項收款是收入側「第三類」進帳，與學費 / 活動**不重複計算**，因此納入財報安全。

## 4. 資料模型 `misc_receipts`

由 `models/vendor_payment.py` 平移，於收入語義處調整。表名 **`misc_receipts`**。

| 欄位 | 型別 | 約束 / 說明 | 對應 vendor_payments |
|---|---|---|---|
| `id` | Integer | PK | 同 |
| `receipt_date` | Date | NOT NULL, indexed（收款日期） | ← payment_date |
| `payer_name` | String(120) | NOT NULL, indexed（繳款方 / 來源名稱） | ← vendor_name |
| `category` | String(20) | NOT NULL, indexed（款項類別，見下） | ★ 新增 |
| `amount` | Numeric(12,2) | NOT NULL，CheckConstraint `amount > 0` | 同 |
| `payment_method` | String(20) | NOT NULL，enum `cash/bank_transfer/check/linepay/other` | 同（復用常數） |
| `description` | String(255) | 項目 / 說明 | 同 |
| `receipt_number` | String(60) | 收據 / 單據號 | ← invoice_number |
| `notes` | Text | 備註 | 同 |
| `attachments` | JSONB（sqlite fallback JSON） | NOT NULL default `[]`，每筆 `{key, filename, size, mime_type, uploaded_at, uploaded_by_id}`，≤5 張 | 同（復用結構） |
| `status` | String(16) | NOT NULL default `pending`，enum `pending` / `signed` | 同 |
| `signer_id` | Integer FK→employees.id (SET NULL) | indexed，內部經手員工 | 同 |
| `signed_at` | DateTime | 簽收時間 | 同 |
| `signature_kind` | String(16) | `drawn`(手寫) / `photo`(紙本照片) / NULL | 同 |
| `signature_key` | String(255) | storage 內簽名圖 key | 同 |
| `created_by_id` | Integer FK→employees.id (SET NULL) | 建立人 | 同 |
| `created_at` / `updated_at` | DateTime | 台北時間 | 同 |

**索引**：`ix_misc_receipts_status_date (status, receipt_date)`、`ix_misc_receipts_category (category)`、`receipt_date`、`payer_name`、`signer_id` 各自 indexed。

**常數**（model 內，仿 vendor_payment.py `PAYMENT_METHODS/PAYMENT_STATUSES/SIGNATURE_KINDS`）：
- `PAYMENT_METHODS = cash/bank_transfer/check/linepay/other`（直接復用付款側同一份）
- `RECEIPT_STATUSES = pending/signed`
- `SIGNATURE_KINDS = drawn/photo`
- `RECEIPT_CATEGORIES`（款項類別，存英文 snake_case，前端映射中文標籤）：

| 值 | 中文標籤 |
|---|---|
| `rent` | 場地租金 |
| `donation` | 捐款 |
| `subsidy` | 補助款 |
| `secondhand_sale` | 二手義賣 |
| `refund_recovery` | 退費回收 |
| `other` | 其他 |

> 類別為**業主可後續調整**的軟枚舉：DB 存字串 + 應用層白名單校驗（仿 payment_method）。新增類別只需改白名單常數 + 前端標籤表，不需 migration。

## 5. 簽收語義（機制平移）

與付款側完全對稱：
- 繳款方上傳紙本收據照片 **或** 當場手寫簽名 → 確認款項已交付。
- `signer_id` 記錄**內部經手員工**（與付款側一致），`signature_key` 指向的簽名圖本身才是繳款方 / 佐證筆跡。
- 流程：建立（status=`pending`，記 `created_by_id`）→ 可選上傳單據附件（僅 pending）→ 簽收 POST `/sign`（寫 `signature_kind/signature_key/signer_id/signed_at`，status→`signed`）。
- **終態鎖定**：已簽收（`signed`）不可編輯 / 刪除 / 增刪附件，皆回 409（理由同付款側：已有簽名佐證且已計入財報收入）。

## 6. 後端 API 契約

新 router `api/misc_receipts.py`，prefix `/api`，tag `misc-receipts`，全部需 staff 權限，無公開端點。端點與 `vendor_payments.py` 一一對稱：

| Method | Path | 權限 | 說明 |
|---|---|---|---|
| GET | `/api/misc-receipts` | MISC_RECEIPT_READ | 分頁列表，篩選 start_date/end_date/payer_name/**category**/status/payment_method |
| GET | `/api/misc-receipts/summary` | MISC_RECEIPT_READ | 區間跨狀態彙總（KPI 卡，可按 category 分組） |
| GET | `/api/misc-receipts/{id}` | MISC_RECEIPT_READ | 單筆明細 |
| POST | `/api/misc-receipts` | MISC_RECEIPT_WRITE | 建立，status 強制 `pending`，記 `created_by_id` |
| PUT | `/api/misc-receipts/{id}` | MISC_RECEIPT_WRITE | 編輯（僅 pending，已簽收 409） |
| DELETE | `/api/misc-receipts/{id}` | MISC_RECEIPT_WRITE | 刪除（僅 pending，已簽收 409） |
| POST | `/api/misc-receipts/{id}/sign` | MISC_RECEIPT_WRITE | 簽收：base64 簽名圖 → storage → status=`signed` |
| GET | `/api/misc-receipts/{id}/signature` | MISC_RECEIPT_READ | 取簽名圖 (FileResponse) |
| POST | `/api/misc-receipts/{id}/attachments` | MISC_RECEIPT_WRITE | 上傳附件（僅 pending，≤5 張） |
| DELETE | `/api/misc-receipts/{id}/attachments` | MISC_RECEIPT_WRITE | 刪附件（僅 pending） |
| GET | `/api/misc-receipts/{id}/attachments/download` | MISC_RECEIPT_READ | 下載附件 (FileResponse) |

- **Pydantic schemas**：request（inline）`MiscReceiptBase/Create/Update` + `MiscReceiptSignRequest`（`signature_kind: Literal["drawn","photo"]` + `signature_data: str`）；response 於 `schemas/misc_receipts.py`：`MiscReceiptOut/ListOut/SummaryOut/AttachmentMetaOut`。皆有 `response_model=`（避免前端 codegen 出 `unknown`）。
- **日期守衛**：收款日禁未來日 + 回補上限 90 天（沿用付款側 `validate_payment_date` 邏輯；上限可在實作時與 user 確認是否放寬）。
- **註冊**：model import + `app.include_router` 於 `main.py`；若需服務注入照 workspace 慣例走 `init_*` singleton。

## 7. 權限（字串集合，依 workspace CLAUDE.md）

權限為 **str Enum**（非 bit / BigInt）。新增：
- 後端 `utils/permissions.py`：`Permission.MISC_RECEIPT_READ` / `Permission.MISC_RECEIPT_WRITE`，中文標籤「雜項收款 (檢視)」/「雜項收款 (編輯/簽收)」。
- **`permission_definitions` seed**（migration）：新增兩個權限碼 + `scope_options`。⚠ **prod 已知缺口**：prod DB 曾以 `create_all + stamp head` 建立，`permission_definitions` seed 不全；非 wildcard admin 若缺此 seed 會 403。本案 migration 必須補上 seed。
- **角色→權限映射**：DB `roles` 表（單一事實來源）給 admin / 財務相關角色掛上這兩個權限；in-code `ROLE_TEMPLATES` fallback 同步加（避免空 DB lockout）。
- 前端 `src/constants/permissions.ts`：`PERMISSION_NAMES` 加兩個碼。

## 8. 財報聯動

- `services/finance_report_service.py`：現以 `vendor_payments` rows 聚合**支出**。本案新增 `misc_receipts` 作為**收入**資料源，與學費 / 活動收入並列（互不重複）。
- 任何建立 / 編輯 / 刪除 / 簽收都呼叫 `_invalidate_finance_cache()`，與付款側一致。
- **實作注意**：確認財報的收入聚合口徑（已簽收才計入？或含 pending？）—— 建議與付款側對齊：財報聚合**所有 rows（含 pending）** 或**僅 signed**，依現行 vendor_payments 口徑決定，實作時讀 `finance_report_service` 對齊，spec review 時若 user 有偏好可指定。

## 9. 共用 vs 複製策略

| 層 | 策略 | 理由 |
|---|---|---|
| 後端純邏輯 helper（日期守衛、簽名 base64 解碼 / storage、附件 meta 處理、檔案下載） | **抽共用** | 無副作用、可單測，抽出避免兩份邏輯漂移 |
| 後端 router / schema / model | **獨立** | 表與業務語義不同 |
| 前端 UI 元件（簽收彈窗、頁面） | **各自獨立一份** | 付款側已上線，抽共用 UI 會動到它、帶回歸風險；重複的 UI 模板可接受 |

> 若 user 偏好前端 UI 也抽共用元件，spec review 時調整本節。

## 10. 前端

- `src/api/miscReceipt.ts`（對稱 `vendorPayment.ts`）：封裝全部端點 + `CATEGORY_OPTIONS` / `categoryLabel` + 復用 `PAYMENT_METHOD_OPTIONS`。型別用 `import type { ApiBody, ApiQuery, AxiosResp } from './_generated/typed'`。
- `src/views/MiscReceiptView.vue`：KPI 卡 + 類別 / 狀態分段篩選 + 表格 + 新增 / 編輯 / 檢視 Dialog + 附件管理。
- `src/components/MiscReceiptSignDialog.vue`：兩個 tab（上傳紙本照片主路徑 / 當場手寫 canvas 次路徑），提交壓成 dataURL 調 `signMiscReceipt`。
- 路由 `src/router/index.ts`：path `/misc-receipts`，name `misc-receipts`，需 `MISC_RECEIPT_READ`。
- 側邊欄 `AdminSidebar.vue`：新增入口，與「廠商付款簽收」並列。
- **OpenAPI codegen**：後端改完跑 `dump_openapi.py` → 前端 `npm run gen:api`，commit `schema.d.ts`。

## 11. 測試

- **後端 pytest**（`tests/test_misc_receipts.py`，掛 `test_db_session` fixture 避免打 dev DB）：
  - model：`amount > 0` CHECK、category / status / payment_method 白名單。
  - 端點權限：READ / WRITE 守衛、無權 403。
  - 簽收流程：pending→signed、signature 寫入。
  - 終態鎖定：signed 後 PUT / DELETE / 附件操作回 409。
  - 財報聚合：misc_receipts 計入收入、快取失效。
  - 日期守衛：未來日 / 逾回補上限 422。
- **前端 vitest**：`miscReceipt.ts` api 封裝、頁面 / 元件純邏輯（category 標籤、簽收 dataURL 壓縮）。
- 純計算 / 聚合邏輯必補測試（修 bug 先補回歸測試）。

## 12. Migration

- 新 migration（slug 仿 `mscrcpt01`）：
  - 建 `misc_receipts` 表（含索引、`amount > 0` CHECK、FK SET NULL）。
  - `permission_definitions` seed：`MISC_RECEIPT_READ` / `MISC_RECEIPT_WRITE` + scope_options。
  - DB `roles` 映射更新（admin / 財務角色加權限）。
- downgrade 完整（drop 表 + 移除 seed + 還原角色映射）。single-head、roundtrip 對稱（workspace 有 CI 護欄）。

## 13. 實作注意事項

1. **共用 checkout 風險**：後端 main 工作樹常有平行 session WIP（含 staged 刪除）。commit 一律用 pathspec 精確提交自己的檔案；移動 ref 用 `git merge` 讀 live tip，勿 `branch -f` 配過時 SHA。實作走獨立 worktree + feature 分支隔離。
2. **前後端分開 commit**：後端一筆、前端一筆，訊息描述同一功能。
3. **prod 部署**：push origin/main 即觸發 Zeabur 部署 + 跑 migration（後端已上線）。push 含 migration 的後端前確認 prod 前置（permission_definitions seed 必須在前端拉新欄位前合併並 `alembic upgrade heads`）。
4. **收尾 DoD**：完成 = push + CI 綠 + worktree remove，非僅併 local main。

## 14. 開放問題（spec review 時請 user 確認）

1. 類別清單 `場地租金 / 捐款 / 補助款 / 二手義賣 / 退費回收 / 其他` 是否足夠 / 要增減改名？
2. 財報收入口徑：聚合所有 rows（含 pending）還是僅 signed？（建議與付款側 `vendor_payments` 現行口徑對齊。）
3. 收款日回補上限是否沿用 90 天，或放寬（捐款 / 補助可能補登較久以前）？
4. 共用策略 §9（前端 UI 獨立 vs 抽共用）是否同意？
