# 才藝 POS 移除非現金付款方式（Cash-only）設計

**日期**：2026-05-06
**狀態**：📝 Draft（待 brainstorming approve）
**前置稽核**：2026-05-06 對話內 POS 企業稽核 C1 項
**範圍**：跨前後端（ivy-backend + ivy-frontend）

---

## 0. 範圍與目標

### 問題陳述

POS checkout 現有 `payment_method` 三選一（`現金 / 轉帳 / 其他`），但業主**實際只收現金**——「轉帳/其他」是設計階段的錯誤殘留。系統尚未上線，沒有歷史資料負擔。

此錯誤造成兩個問題：
1. **舞弊面**：員工可將實收現金記成「轉帳」，使日結 `cash_variance` 失準（盤點對 `by_method[現金]=0`）→ 私吞現金
2. **UX 噪音**：櫃台每筆都要選付款方式，但永遠只有一個合法選項

### 範圍邊界

- ✅ **包含**：
  - 後端 `POSCheckoutRequest.payment_method` 強制 `Literal["現金"]` + router fallback 守衛
  - 後端 `delete_registration_payment` 等 refund 端點同步收口
  - 前端移除付款方式 dropdown 與 `POS_PAYMENT_METHODS` 常數
  - 前端收據預設顯示「現金」
  - 簽核 UI 移除 by_method 拆分顯示（保留 JSON 結構，未來可擴）
  - 連帶優化 1：`MIN_REFUND_REASON_LENGTH` 5 → 15
  - 連帶優化 2：簽核「現金盤點」金額 ≥ NT$3,000 必填
  - 連帶優化 3：相關註解清理（`by_method_json`、`_IDEMPOTENCY_WINDOW_SECONDS` 誤導註解）
- ❌ **不包含**：
  - DB schema 變更（`payment_method` 欄位保留）
  - 歷史資料 migration（系統未上線）
  - 其他稽核 finding（C2/C3/C4/C5、H 系列）
  - 銀行對帳、電子發票、第三方金流串接

### 非目標

- 不刪除 `payment_method` 欄位（保留結構讓未來擴充）
- 不改 `by_method_json` 欄位（保留 JSON 形式）
- 不引入新權限位元
- 不改 `ActivityPosDailyClose` 表結構

### 成功標準

1. POST `/activity/pos/checkout` 帶非「現金」`payment_method` → **422**（Pydantic schema 拒絕）
2. POST `/activity/registrations/{id}/payments` 同上 → **422 / 400**
3. 前端 POS 收銀畫面**無**付款方式選項；收據固定顯示「現金」
4. 日結簽核畫面 by_method 區塊不再出現「轉帳/其他」（即使 DB 有也不顯示——但 DB 不會有）
5. 退費備註 < 15 字 → 後端 400、前端送出前 warning
6. 當日 net_total ≥ NT$3,000 時，簽核必填 `actual_cash_count`，否則 400
7. 既有 `tests/test_activity_pos.py` 全綠 + 新增 cash-only 守衛測試全綠

---

## 1. 後端變更

### 1.1 `api/activity/pos.py`

**`POSCheckoutRequest.payment_method`** 從 `Literal["現金", "轉帳", "其他"]` 改為 `Literal["現金"] = "現金"`：

```python
payment_method: Literal["現金"] = Field(
    "現金",
    description="目前僅接受現金；保留欄位供未來擴充",
)
```

**Router fallback 守衛**（pos.py L453-456 改寫）：
```python
# Schema 已限制 Literal["現金"]，這裡是防呆——未來開發者誤改 Literal 時的二次防線
if body.payment_method != "現金":
    raise HTTPException(status_code=400, detail="目前 POS 僅支援現金交易")
```

**移除常數** `_VALID_METHODS = {"現金", "轉帳", "其他"}`（L139）—— Literal 已收口，不再需要。

### 1.2 `api/activity/_shared.py`

`MIN_REFUND_REASON_LENGTH`：`5` → `15`（L55）。錯誤訊息與 schema 描述同步更新。

### 1.3 後台與其他寫入端點

除 §1.1 的 POS checkout 外，**所有**寫入 `ActivityPaymentRecord.payment_method` 的位置都要收口。

**已知端點**（plan 階段以 `grep -nE "payment_method\s*=" api/activity/*.py` 釘住完整清單）：
- `POST /registrations/{id}/payments` —— 後台單筆繳費/退費（registrations.py 約 L2059-2199）
- `POST /registrations/{id}/confirm-refund` —— 離園退費（registrations.py 約 L1662-1700）
- 任何「批次補齊收入」/「補登繳費」路徑（_shared.py `BatchMarkPaidRequest` 周邊）

收口手法：
- Pydantic schema 欄位收口為 `Literal["現金"] = "現金"`
- 直接構造 `ActivityPaymentRecord(payment_method=...)` 處改為硬編 `"現金"` 字面值
- 共用 helper（若有）回傳值統一「現金」

### 1.4 `api/activity/pos_approval.py` — 簽核守衛

`DailyCloseCreate` 新增條件驗證：
```python
class DailyCloseCreate(BaseModel):
    note: Optional[str] = Field(None, max_length=500)
    actual_cash_count: Optional[int] = Field(None, ge=0, le=9_999_999)
```

`approve_daily_close` 在計算 `snap` 後加守衛。判定基準採 `by_method_net["現金"]`（非 `snap["net"]`）——語意更精確：盤點對的是「現金抽屜該有多少」，不是「會計淨額」。既然只收現金，兩者等值，但常數用 by_method_net 對未來擴充友善。

```python
CASH_COUNT_REQUIRED_THRESHOLD = 3000  # 模組常數
expected_cash = int(snap["by_method_net"].get("現金", 0))
if expected_cash >= CASH_COUNT_REQUIRED_THRESHOLD and body.actual_cash_count is None:
    raise HTTPException(
        status_code=400,
        detail=f"當日預期現金 NT${expected_cash:,} ≥ NT${CASH_COUNT_REQUIRED_THRESHOLD:,}，必須填寫實際現金盤點金額",
    )
```

### 1.5 註解清理

- `pos.py:70` 移除「冪等 key 視窗 600 秒」誤導註解（實際是全域唯一）
- `models/activity.py` `by_method_json` comment 改為「分付款方式 JSON（目前僅有現金，保留 JSON 結構供未來擴充）」
- `_shared.py` `compute_daily_snapshot` docstring 同步更新

### 1.6 測試

新增 `tests/test_pos_cash_only.py`：
- `test_checkout_rejects_non_cash` — POST 帶 `payment_method="轉帳"` → 422
- `test_checkout_rejects_other` — `payment_method="其他"` → 422
- `test_checkout_default_cash` — 不傳 `payment_method` → 預設「現金」成功
- `test_registration_payment_rejects_non_cash` — 後台端點同上
- `test_refund_reason_min_length_15` — 退費備註 14 字 → 400，15 字成功
- `test_daily_close_requires_cash_count_at_threshold` — net = NT$3,000 不傳 cash count → 400
- `test_daily_close_below_threshold_skips_cash_count` — net = NT$2,999 不傳 → 通過

修改既有測試：所有 `payment_method="轉帳"` 的測試 case 改為「現金」或刪除（這些 case 在新政策下無效）。

---

## 2. 前端變更

### 2.1 `src/constants/pos.js`

- 移除 `POS_PAYMENT_METHODS` 常數（L9-13）
- 保留 `CASH_METHOD = '現金'`（其他元件用）
- 增註解：`// 才藝 POS 僅收現金；若未來擴充，後端 schema 與此處需同步`

### 2.2 `src/composables/usePOSCheckout.js`

- 移除 `paymentMethod` ref（L70）—— 永遠是現金
- 移除 `paymentMethodOptions`（L108）
- `submit()` payload 直接寫死 `payment_method: '現金'`（L314）
- `reset()` 不再重設 `paymentMethod`（L258）
- export 物件移除 `paymentMethod`、`paymentMethodOptions`

### 2.3 `src/components/activity/POSPaymentPanel.vue`

- 移除「付款方式」radio/select 區塊
- 保留「備註」輸入框（其他用途）
- 退費模式時備註欄 placeholder 改為「退費原因（至少 15 字）」
- `usePOSCheckout.submit()` 內既有檢查（L275 區域）字數常數從 5 改為 15
- `canSubmit` computed property 增加條件：退費模式時 `notes.value.trim().length < 15` 即 false（**hard block，按鈕 disabled**），不只 warning

### 2.4 `src/components/activity/POSReceipt.vue`

收據顯示永遠是「現金」（移除動態取 `payment_method` 的邏輯，或保留但永遠 fallback「現金」）。

### 2.5 `src/components/activity/POSDailySummaryBar.vue` 與 `POSApprovalView.vue`

- **移除 by_method 拆分顯示**：既然只剩現金，顯示一個「現金總額」欄即可；既有「分付款方式」表格摺起或刪除
- **簽核盤點規則對齊後端**（`expected_cash = by_method["現金"]` 的 net）：
  - `expected_cash >= 3000` 時，`actual_cash_count` input 標紅、加 `required` aria-attr、必填提示
  - 未填則「送出簽核」按鈕 disabled（hard block）
  - `expected_cash < 3000` 時 input 維持 optional，按鈕不擋
- 後端 400 錯誤處理：若使用者透過直接呼叫 API（繞 UI）漏填，後端會 400，前端用 `ElMessageBox.alert` 顯示錯誤

### 2.6 `src/components/activity/POSCheckoutPanel.vue`

移除付款方式相關 props/template；確保整體高度收斂、不留空白區塊。

---

## 3. DB / Migration

**無 migration**。系統尚未上線，DB 中沒有 `payment_method != "現金"` 的真實資料。

部署前**驗證腳本**（一次性手動，不寫 alembic）：
```sql
-- 應該回傳 0 列；若有則代表測試資料殘留，部署前清掉
SELECT COUNT(*) FROM activity_payment_records
WHERE payment_method IS NOT NULL AND payment_method != '現金';
```

---

## 4. 連帶優化（已併入上述章節）

| # | 項目 | 位置 |
|---|------|------|
| 1 | 退費原因 5 → 15 字 | §1.2 + §2.3 |
| 2 | 現金盤點 ≥ NT$3,000 必填 | §1.4 + §2.5 |
| 3 | 註解清理 | §1.5 |

---

## 5. 風險與緩解

| 風險 | 緩解 |
|------|------|
| 開發者未來誤改 `Literal["現金"]` 開回三選 | Router fallback 守衛雙保險（§1.1）；測試會擋 |
| 前端 cache 殘留舊 `paymentMethod=轉帳` 觸發後端 422 | 影響範圍只有測試環境（未上線）；忽略 |
| `MIN_REFUND_REASON_LENGTH` 改大造成既有測試 fixture 失敗 | 一併修測試 fixture（§1.6） |
| §1.3 grep 漏抓寫入點 → 上線後 422 噴錯 | plan 階段強制以 grep 取得清單，code review 確認；測試新增「全端點 cash-only」覆蓋 |
| 簽核盤點門檻 NT$3,000 是否合理 | 由業主決定；首版採 NT$3,000，依實際使用調整 |
| `by_method_json` 結構保留導致未來啟用轉帳時格式不一致 | 文件註解明確說明 JSON 結構是「method → net_amount」契約 |

---

## 6. 實作順序

1. **後端 schema + router 守衛**（§1.1, §1.3, §1.4）+ 新測試（§1.6）→ 後端綠
2. **`MIN_REFUND_REASON_LENGTH`** 改 15 + 修既有測試（§1.2）→ 後端綠
3. **註解清理**（§1.5）
4. **前端常數 + composable 移除 paymentMethod**（§2.1, §2.2）
5. **前端 POSPaymentPanel / POSReceipt / POSCheckoutPanel UI 收斂**（§2.3, §2.4, §2.6）
6. **前端 POSApprovalView 盤點門檻**（§2.5）
7. **整合驗證**：`start.sh` 啟兩端，跑一輪「結帳 → 簽核」golden path

每步可獨立 commit；後端與前端各一筆 commit。

---

## 7. Out of scope（明確排除）

- C2 簽核者自簽自解 → 另一個 spec
- C3 前後端閾值對齊（NT$1,000 vs NT$10,000）→ 另一個 spec
- C4 退費並發拆單 → 另一個 spec
- C5 idempotent replay 過濾 voided → 另一個 spec
- 銀行對帳 / 發票 / 第三方金流 → 業務尚未需要

完工後，C1 從稽核報告移除，剩 C2/C3/C4/C5 + H 系列待排。
