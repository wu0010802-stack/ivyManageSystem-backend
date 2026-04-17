# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## 專案概述
幼稚園考勤與薪資管理系統，支援打卡記錄解析、自動薪資計算、勞健保扣繳、教師入口等功能。

## 技術棧
- FastAPI (Python 3), SQLAlchemy, PostgreSQL
- JWT (python-jose), PBKDF2 密碼雜湊
- 前端為獨立 repo（`ivyManageSystem-frontend`）

---

## 開發指令

### 啟動後端（port 8088）
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8088
```

### 推送至遠端
本 repo 為獨立 git repo，直接 `git push origin main` 即可。

---

## 環境變數（backend/.env）

| 變數 | 說明 |
|------|------|
| `DATABASE_URL` | PostgreSQL 連線字串。本地開發若未設定，預設 `postgresql://localhost:5432/ivymanagement` |
| `ENV` | `development`（預設）或 `production`。production 模式下缺少 `DATABASE_URL` / `JWT_SECRET_KEY` 會直接拋出 RuntimeError |
| `JWT_SECRET_KEY` | JWT 簽名金鑰。開發模式下有 fallback 預設值，正式環境必須設定 |
| `CORS_ORIGINS` | 逗號分隔的允許來源，未設定時允許 localhost:5173 / 3000 |

---

## 架構重點

### 後端服務注入模式
`SalaryEngine` 與 `InsuranceService` 在 `main.py` 啟動時建立為 singleton，再透過 `init_*_services()` 注入需要的 router：

```
main.py → init_salary_services(salary_engine, insurance_service)
        → init_config_services(salary_engine)
        → init_insurance_services(insurance_service)
        → init_overtimes_services(salary_engine)
        → init_leaves_services(salary_engine)
```

新增需要服務依賴的 router 時，必須遵循此模式。

### 資料庫連線
`models/database.py` 管理 singleton Engine（connection pool）。兩種取得 session 的方式：
- `get_session()` — 手動管理（使用後須 `.close()`）
- `session_scope()` — context manager，自動 commit/rollback/close（推薦新程式使用）

schema 異動透過 `_add_column_if_missing()` 在啟動時自動執行 `ALTER TABLE`，不使用 migration 框架。

### 權限系統
`utils/permissions.py` 定義 `Permission` IntFlag（位元遮罩），讀寫分離：
- 讀取：低位（如 `SALARY_READ = 1 << 11`）
- 寫入：高位（如 `SALARY_WRITE = 1 << 23`）

路由使用 `require_permission(Permission.SALARY_WRITE)` 做守衛（在 `utils/auth.py`）。

### 薪資計算邏輯（salary_engine.py）
- **`gross_salary`（月薪應發）** = `base_salary + allowances + performance/special_bonus + supervisor_dividend + meeting_overtime_pay`
  - **不含** `festival_bonus` / `overtime_bonus`（這兩項獨立轉帳）
- **`festival_bonus`** 在發放月（2、6、9、12 月）才計入，`meeting_absence_deduction` 只從 `festival_bonus` 扣，**不進入** `total_deduction`
- **`bonus_separate`** 旗標：當 `festival_bonus + overtime_bonus > 0` 時為 True，表示有另行匯款
- **`total_deduction`** = 勞保 + 健保 + 勞退 + 遲到/早退/請假扣款（無 `meeting_absence_deduction`）
- 時薪計算基準：`base_salary / 30 / 8`（MONTHLY_BASE_DAYS = 30，依勞基法）
- 加班費時薪基準：`emp.base_salary`（僅底薪，不含任何加給或獎金）

### 設定版本控制（稽核追蹤）
`SalaryRecord` 記錄計算時使用的 `bonus_config_id` 與 `attendance_policy_id`，確保薪資可回溯查核。

---

## 開發注意事項
- 回應語言：一律使用**繁體中文**
- `BonusConfig` DB 模型在 `main.py` 以 `DBBonusConfig` 匯入，避免與 `api/salary.py` 中同名 Pydantic schema 衝突
- 日誌使用 `logging.getLogger(__name__)`，不使用 `print()`
- `portal.py` 是教師自助入口，提供自己的考勤/請假/加班/薪資查詢，不具管理權限
- `api/dev.py` 包含開發/測試用端點，正式環境應留意是否需要關閉

---

## 開發規範

### TDD（測試驅動開發）

**核心原則：Red → Green → Refactor**

1. **先寫失敗測試**，再實作讓測試通過，最後重構
2. **修 bug 必先補回歸測試**：先寫一個能重現 bug 的測試（此時應失敗），再修程式碼讓它通過
3. **純商業邏輯（薪資計算、保險計算）** 必須有對應的單元測試；資料庫查詢邏輯可用整合測試替代

**哪些情境適合 TDD：**
- `salary_engine.py` 的計算邏輯（可用 `SalaryEngine(load_from_db=False)` 完全不碰 DB）
- Pydantic validator 的邊界條件（如請假時數、跨月檢查）
- 日期計算、工時計算等純函式

**哪些情境可以後補測試：**
- FastAPI 路由（需要 DB，成本較高）
- 前端元件整合行為

---

### 後端測試（pytest）

**執行指令：**
```bash
cd backend
pytest tests/ -v                    # 執行全部
pytest tests/test_salary_engine.py  # 只跑特定檔案
pytest tests/ -k "TestLeave"        # 只跑特定 class
pytest tests/ --tb=short            # 失敗時精簡輸出
```

**測試目錄結構：**
```
backend/tests/
├── conftest.py              # 共用 fixtures（engine, sample_employee…）
├── test_salary_engine.py    # 薪資計算邏輯
├── test_insurance_service.py
├── test_auth.py
└── test_<新模組>.py         # 新功能對應新檔案
```

**命名規則：**
- 檔案：`test_<模組名>.py`
- Class：`Test<功能群組>`（如 `TestLeaveValidation`、`TestMeetingDeduction`）
- 方法：`test_<情境描述>`，描述要能獨立閱讀（如 `test_cross_month_leave_raises_400`）

**Fixture 使用原則：**
- 共用 fixtures 放 `conftest.py`（已有 `engine`、`sample_employee`、`sample_attendance`）
- `SalaryEngine(load_from_db=False)` 用於純邏輯測試，不依賴 DB
- 測試資料只放測試需要的最小欄位，其餘保持 `conftest.py` 預設值

**測試結構範例：**
```python
class TestHourlyWorkHours:
    def test_deducts_lunch_break_when_spanning_noon(self, engine):
        """08:00–17:00 應得 8 小時（扣除午休 1 小時）"""
        ...

    def test_no_deduction_when_leaving_before_noon(self, engine):
        """08:00–12:00 不跨午休，工時應為 4 小時"""
        ...

    def test_partial_overlap_with_lunch(self, engine):
        """08:00–12:30 僅扣 0.5 小時午休"""
        ...
```

---

### Git Commit 規範

使用 Conventional Commits 格式：

```
<type>: <簡短描述（繁體中文）>

<選填：詳細說明，包含 why 而非只有 what>
```

| Type | 用途 |
|------|------|
| `feat` | 新功能 |
| `fix` | Bug 修正 |
| `refactor` | 重構（不改行為） |
| `test` | 新增或修改測試 |
| `docs` | 文件更新 |
| `chore` | 維護性雜項 |

**原則：**
- 一個 commit 只做一件事；修 bug 與補測試分成兩個 commit
- Commit message 說明「為什麼」，程式碼本身說明「做了什麼」
- 不 commit `.env`、`__pycache__`、`.pyc`

---

### 程式碼品質規範

**通用：**
- 函式單一職責：超過 40 行考慮拆分
- 禁止魔法數字：薪資計算常數（如 `MONTHLY_BASE_DAYS = 30`）統一定義在模組頂部
- 不重複邏輯：相同計算出現兩次就提取成函式（例：午休扣除邏輯在 `salary_engine.py` 與 `salary.py` 各一份是已知技術債）

**後端：**
- 所有對外輸入必須過 Pydantic 驗證（含 `ge`/`le` 邊界）
- 所有路由必須有 `require_permission()` 守衛
- 不使用 `print()`，一律 `logger = logging.getLogger(__name__)`
- 新路由若需要 `SalaryEngine` / `InsuranceService`，必須透過 `init_*_services()` 注入，不直接 import

---

### 安全規範

- 檔案路徑操作必須做路徑穿越防護（參考 `_safe_attach_path()`）
- 使用者輸入不可直接拼接 SQL（已使用 ORM，維持此原則）
- 敏感操作（薪資匯出、核准）須記錄 `logger.warning` 稽核日誌
- 登入端點已有限流，新的高風險端點參考相同模式
