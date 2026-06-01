# 批次加班建立（Batch Overtime Create）設計

- 日期：2026-06-01
- 範圍：跨前後端（ivy-backend + ivy-frontend）
- 模組：加班（overtime）
- 對應 CLAUDE.md：跨端 SOP、`api-contract` skill

## 背景與目標

學校常有活動（例：校慶、運動會、招生說明會），多位員工同時出席而需登記加班。
目前只能由管理端在加班管理頁面（`OvertimeView.vue`）**逐筆**建立，一場活動十幾位員工
要重複填十幾次表單，繁瑣且易漏。

本功能讓 HR／主管在管理端**一次為多位員工建立加班記錄**：共用同一組活動資訊
（日期、加班類型、起訖時間、原因、補休政策），每位員工的**時數可個別微調**。

### 非目標（Out of Scope）

- 教師自助端（`api/portal/overtimes.py`）**不**新增批次功能；portal 維持只能申請本人單筆。
- 不改既有單筆建立、修改、刪除、批次核准、Excel 匯入端點的對外行為。
- 不做批次「修改／刪除」；本次只做批次「建立」。

## 關鍵決策（已與使用者確認）

| 決策 | 選擇 | 理由 |
|------|------|------|
| 時數模式 | **共用日期/類型/原因，時數可逐人微調** | 活動共用時段，但有人提早離開／待更久 |
| 使用場景 | **僅管理端**（`OVERTIME_WRITE`） | 「學校活動、主管登記」情境；portal 有 self-guard 不適用 |
| 驗證失敗處理 | **全部或全無（all-or-nothing）** | 結果可預測、不漏人；使用者修正後重送 |
| 初始狀態 | **`pending` 待審核** | 與單筆建立一致、較安全；薪資/補休在「核准」時才生效 |

## 架構決策：為何需要專用後端端點

「全部或全無」這個約束直接決定架構：

- ❌ **前端迴圈呼叫單筆 `POST /overtimes` N 次**：無法達成跨 N 個獨立 HTTP 請求的原子性。
  第 4 位驗證失敗時，前 3 位已寫進 DB，留下髒資料 → **否決**。
- ❌ **後端端點但部分成功**：與「全部或全無」矛盾 → **否決**。
- ✅ **後端專用批次端點 + 兩階段提交**：全員驗證通過才一次 commit，沿用既有
  `POST /overtimes/batch-approve` 的成熟 pattern → **採用**。

## 後端設計

### 新端點

```
POST /overtimes/batch-create
權限：require_staff_permission(Permission.OVERTIME_WRITE)
Rate limiter：沿用 batch-approve 的限流（防濫用）
```

### Request schema（新增 Pydantic）

```python
class BatchOvertimeEmployeeItem(BaseModel):
    employee_id: int
    hours: float          # 0 < hours <= MAX_OVERTIME_HOURS，逐人可不同

class BatchOvertimeCreate(BaseModel):
    overtime_date: date                       # 共用
    overtime_type: str                        # "weekday" | "weekend" | "holiday"，共用
    start_time: Optional[str] = None          # "HH:MM"，共用，選填（代表活動時段）
    end_time: Optional[str] = None            # "HH:MM"，共用，選填
    reason: Optional[str] = None              # 共用
    use_comp_leave: bool = False              # 共用（整場活動的補休政策）
    employees: List[BatchOvertimeEmployeeItem]  # 逐人時數
```

驗證細節：
- `employees` 不可為空。
- `employee_id` 重複時**當作驗證錯誤**回報（在 Phase 1 加入 `errors`），不靜默吞掉、不只建立一筆。
  搭配「全部或全無」→ 整批不建立，使用者移除重複後重送。
- `hours` 上限沿用既有 `MAX_OVERTIME_HOURS` 常數。

### Response

成功（HTTP 200）：
```json
{ "message": "已建立 2 筆加班記錄", "created_ids": [101, 102] }
```

驗證失敗（HTTP 422，整批不建立）：
```json
{
  "detail": "批次建立失敗，請修正下列項目後重送",
  "errors": [
    { "employee_id": 2, "name": "王小明", "reason": "超出當月加班上限（已 44h + 3h > 46h）" }
  ]
}
```

> 注意：422 的 body 為自訂結構（`detail` + `errors`），與 FastAPI 預設 422 validation error 形狀不同；前端需特別處理此回傳。實作時以 `JSONResponse(status_code=422, ...)` 或自訂 exception 帶出，避免被當成欄位驗證錯誤。

### 兩階段提交流程

**Phase 1 — 全員驗證（不寫 DB）：**

1. 檢查 `employees` 內 `employee_id` 是否有重複，有則記錄為 error。
2. 一次撈出所有 `employee_id` 對應的 `Employee`（避免 N+1）。
3. **逐人**跑完整驗證鏈，並**蒐集「所有」失敗**（**不**在第一個失敗就中止——這是「全部或全無 + 不漏人」價值的關鍵）：
   - 員工是否存在
   - 解析共用 `start_time`／`end_time` → `start_dt`／`end_dt`（每人共用同一時段）
   - `_check_overtime_overlap`（該員工自己既有加班的時間重疊）
   - `_check_employee_has_conflicting_leave`（跨類：加班 vs 請假衝突）
   - `_check_monthly_overtime_cap`（含本筆 hours）
   - `_check_quarterly_overtime_cap`（含本筆 hours）
   - `_check_overtime_type_calendar`（加班類型與日曆一致）
4. 若 `errors` 非空 → 直接回 **422**，**完全不寫入**。

> **封存月份檢查刻意省略**：單筆 `create_overtime`（L588-691）**不**呼叫
> `assert_months_not_finalized`——`pending` 記錄不影響已封存薪資，封存守衛在「核准」路徑
> （L1108-1146）才生效。批次建立**與單筆完全對齊**，同樣不在建立階段檢查封存，避免改動
> 既有單筆對外行為。

**Phase 2 — 一次提交（errors 為空時）：**

5. 為每位員工建立 `OvertimeRecord`：
   - `overtime_pay`：`use_comp_leave=True` 時為 `0.0`，否則
     `calculate_overtime_pay(emp.base_salary, item.hours, overtime_type)`（逐人 base_salary + 逐人 hours）。
   - `status = ApprovalStatus.PENDING.value`
   - `comp_leave_granted = False`
6. `session.add_all(records)` → **單次 `session.commit()`**。
7. 寫 audit log（彙總：建立 N 筆、活動日期/類型、employee_ids、各 hours）。

### 驗證重用：抽出共用 helper（避免漂移）

單筆 `create_overtime`（`api/overtimes.py` L588-691）的驗證鏈與本端點完全相同。
為避免「驗證漂移」（CLAUDE.md 跨端陷阱 #4、記憶反覆提醒），**抽出共用純驗證 helper**，
單筆與批次共用：

```python
def _validate_overtime_for_employee(
    session, emp, overtime_date, overtime_type, start_dt, end_dt, hours
) -> None:
    """跑完整驗證鏈；任一不通過則 raise HTTPException。供單筆與批次共用。"""
    # overlap / cross-leave / monthly cap / quarterly cap / type-calendar / finalized
```

批次端點在 Phase 1 對每人呼叫此 helper，但**捕捉**其 `HTTPException` 轉成 `errors` 條目
（取 `detail` 當 reason），而非讓它中止——藉此蒐集所有失敗。單筆端點則直接讓它 raise。

> 重構原則：只抽出驗證邏輯，**不改**單筆端點的對外行為與既有測試。若抽 helper 會牽動既有
> 單筆測試，採「helper 內容 = 既有檢查鏈原樣搬移」，單筆端點改為呼叫 helper。

### 副作用對齊（重要）

- **不**觸發薪資重算、**不**標記薪資 stale。`pending` 記錄不影響薪資；薪資與補休配額
  只在「核准」時才生效（與單筆建立一致）。
  **不**採用 `api/meetings.py` 批次會議那種「建立即計入」的 pattern。
- **不**發補休配額（`_grant_comp_leave_quota` 只在核准流程跑）。

## 前端設計

### `src/api/overtimes.ts`

新增：
```ts
import type { ApiBody, AxiosResp } from './_generated/typed'
export const batchCreateOvertimes = (payload: ApiBody<'/overtimes/batch-create', 'post'>) =>
  api.post('/overtimes/batch-create', payload)
```
（型別於 OpenAPI regen 後自 `schema.d.ts` 下放。）

### `OvertimeView.vue` + 新 dialog 元件

- 在加班管理頁工具列加「**批次加班**」按鈕（權限 `OVERTIME_WRITE` gate）。
- 開啟批次 dialog，複用 `src/components/overtime/MeetingManagementPanel.vue` 的多選員工模式：
  - **上半（共用欄位）**：日期（`el-date-picker`）、加班類型（`el-select`）、起訖時間（兩個
    `HH:MM` 輸入，選填）、原因（`el-input textarea`）、補休切換（`el-switch`）、**預設時數**。
  - **下半（員工清單）**：可滑動列表，每位員工一列含勾選框；勾選後顯示**可編輯時數輸入框**，
    預設帶入「預設時數」，可逐人改。全選/反選切換、已選人數統計。
- 送出：組 `{ ...共用欄位, employees: 已勾選.map(e => ({ employee_id, hours })) }`。
- 失敗（422）：解析回傳 `errors`，以清單（員工姓名 + 原因）顯示給使用者，**提示整批未建立**，
  使用者修正後重送。
- 成功：toast「已建立 N 筆」，關閉 dialog、刷新列表。

## 測試

### 後端 pytest（`tests/test_overtimes*.py`）

- 全通過 → 建立 N 筆、皆 `pending`、`created_ids` 長度正確。
- **全部或全無**：故意讓 1 人超月上限 → **整批不建立**（DB count 不變）、422 帶該員工 error。
- **蒐集所有失敗**：讓 2 人各自不同原因失敗 → `errors` 同時含這 2 人（驗證不在第一個失敗就中止）。
- `use_comp_leave=True` → 各筆 `overtime_pay=0`、`comp_leave_granted=False`、**不**發補休配額。
- 逐人時數 → 各筆 `hours`/`overtime_pay` 依各自 base_salary 正確。
- 重複 `employee_id` → 回報重複錯誤（不靜默建立兩筆）。
- 權限：無 `OVERTIME_WRITE` → 403。
- **不觸發薪資重算**：建立後當月薪資未被標記 stale（與單筆一致）。
- 單筆端點既有測試仍全綠（驗證 helper 抽出後對外行為不變）。

### 前端 vitest

- dialog 組 payload 正確（共用欄位 + 逐人 hours）。
- 全選/反選、預設時數帶入、逐人改時數。
- 422 errors 清單渲染。

## 實作順序（api-contract SOP）

1. **後端先行**：抽 `_validate_overtime_for_employee` helper → 新 schema → 新端點 → pytest 綠。
2. **OpenAPI regen**：`python scripts/dump_openapi.py` → 前端 `npm run gen:api`（只 commit `schema.d.ts`）。
3. **前端接上**：`overtimes.ts` + dialog 元件 + `OvertimeView.vue` 按鈕 → vitest 綠 + typecheck。
4. **整合驗證**：`start.sh` 起兩端，實際點一次批次建立。
5. **分開 commit**：後端一筆、前端一筆，訊息描述同一功能。

## 風險與注意

- **422 自訂 body 形狀**與 FastAPI 預設 validation error 不同，前端攔截器需分辨（避免被當欄位錯誤吞掉）。
- 抽 helper 時務必保持單筆端點行為不變，先確認既有單筆測試覆蓋面再重構。
- 共用 `start_time`/`end_time` + 逐人 `hours` 會有「顯示上時段相同但時數不同」的小落差；
  屬可接受簡化（`hours` 才是薪資/上限的依據；起訖時間為選填的活動時段標記）。
