# CLAUDE.md

幼稚園考勤、薪資、招生管理系統。FastAPI + SQLAlchemy + PostgreSQL；前端為獨立 repo（Vue 3 + Element Plus）。

## 開發指令

```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8088   # 啟動後端
pytest tests/ -v                         # 跑全部測試
pytest tests/ -k "TestLeave"             # 跑特定 class
pytest tests/ --tb=short                 # 失敗時精簡輸出
```

CI（`.github/workflows/ci.yml`）：push/PR to main 自動跑 PostgreSQL service container + pytest。

環境變數參考 `.env.example`；`ENV=production` 時，缺 `DATABASE_URL` / `JWT_SECRET_KEY` 會直接 RuntimeError（dev 模式有 fallback）。

---

## 架構不變式

加新 router 或修改既有架構時，下列規則必須遵守：

- **服務注入**：`SalaryEngine`、`InsuranceService`、`LineService` 為 `main.py` 啟動時建立的 singleton。需要這些服務的 router 必須透過 `init_*_services()` 注入，**不可** 直接 import。
- **DB session**：新程式優先用 `session_scope()`（context manager，自動 commit/rollback/close）；`get_session()` 僅在需要手動管理時使用。
- **權限守衛**：所有路由必須有 `require_permission(Permission.XXX)`。`Permission` IntFlag 位元遮罩定義在 `utils/permissions.py`，**讀取放低位（如 `1 << 11`）、寫入放高位（如 `1 << 23`）**。
- **Schema 異動**：使用 Alembic（`alembic/versions/`），啟動時自動 `alembic upgrade heads`。
- **啟動邏輯**：seed / migration 放 `startup/`，**不要** 放回 `main.py`。
- **Rate Limiter**：`utils/rate_limit.py` 為 in-process 記憶體版，僅單 worker 部署有效；多實例需改 Redis-backed。
- **`portal.py`** 是教師自助入口（查自己的考勤/請假/加班/薪資），不具管理權限。新增管理功能不要放 portal。
- **`BonusConfig`** DB 模型在 `startup/seed.py` 以 `DBBonusConfig` 別名匯入,避免與 `api/salary.py` 同名 Pydantic schema 衝突。

---

## 業務不變式 — 薪資計算（`services/salary/`）

薪資邏輯拆成 package：`engine.py`（入口 `SalaryEngine`）、`totals.py`（gross/total 組合）、`festival.py`、`hourly.py`、`deduction.py`、`proration.py`、`severance.py`、`unused_leave_pay.py`、`insurance_salary.py`、`minimum_wage.py`、`breakdown.py`、`constants.py`。改薪資邏輯時先看 engine 入口再下鑽。

- **`gross_salary`（月薪應發）** = `base_salary + allowances + performance/special_bonus + supervisor_dividend + birthday_bonus + meeting_overtime_pay + overtime_work_pay`
  - **不含** `festival_bonus` / `overtime_bonus`（這兩項另行轉帳）
  - 含 `birthday_bonus`（壽星 $500）
- **`festival_bonus`** 僅在 2、6、9、12 月計入；`meeting_absence_deduction` 只從 `festival_bonus` 扣，**不進入** `total_deduction`。
- **`bonus_separate`** 旗標：當 `festival_bonus + overtime_bonus + supervisor_dividend > 0` 時為 True，表示有另行匯款。
- **`total_deduction`** = 勞保 + 健保 + 勞退 + 遲到/早退/請假扣款（**無** `meeting_absence_deduction`）。
- **時薪基準**：`base_salary / 30 / 8`，`MONTHLY_BASE_DAYS = 30` 定義在 `services/salary/constants.py`（依勞基法）。
- **加班費時薪基準**：`emp.base_salary` 僅底薪，**不含** 任何加給或獎金。
- **稽核追蹤**：`SalaryRecord` 必須記錄當下使用的 `bonus_config_id` 與 `attendance_policy_id`，確保可回溯。

---

## 業務不變式 — 招生模組附近幼兒園資料

三來源合併，**優先順序由高至低**：

| 來源 | 提供 |
|------|------|
| **`competitor_school` DB**（教育部爬蟲快取） | 電話、地址、類型、核定人數、月費、`has_penalty` |
| **kiang.github.io** | 月費備援、裁罰詳情文字 |
| **Google Places API** | 名稱、座標、評分、Maps 連結 |

關鍵規則：
- `has_penalty = false` → **不顯示** 裁罰（即使 kiang 有記錄）
- `has_penalty = true` → 顯示 kiang 的裁罰詳情
- 月費：DB 有值優先，DB null 才用 kiang
- 教育部爬蟲（`moe_kindergarten_scraper.py`）：用 `threading.Lock` 防重複執行；僅抓高雄市；同步狀態存於 `recruitment_sync_state` 表（`provider_name = 'moe_ece'`）

---

## 開發規範

### 回應語言
**一律繁體中文**（含程式碼註解、commit message、與使用者的對話）。

### TDD（Red → Green → Refactor）
- **修 bug 必先補回歸測試**：先寫能重現 bug 的失敗測試，再修程式碼讓它通過。
- 純商業邏輯（薪資、保險、日期/工時計算、Pydantic validator 邊界）**必須** 有單元測試；可用 `SalaryEngine(load_from_db=False)` 完全不碰 DB。
- FastAPI 路由與前端整合行為可後補測試（成本較高）。

### pytest
- 檔案：`tests/test_<模組>.py`；Class：`Test<功能群組>`；方法：`test_<情境>`，要可獨立閱讀（如 `test_cross_month_leave_raises_400`）。
- 共用 fixtures 放 `conftest.py`（已有 `engine`、`sample_employee`、`sample_attendance`），測試資料只填當下需要的欄位。
- pytest 設定統一在 `pyproject.toml`，**不需** `pytest.ini`。

### Git Commit（Conventional Commits）
```
<type>: <繁中簡述>

<選填：why 而非 what>
```
type：`feat` / `fix` / `refactor` / `test` / `docs` / `chore`

- 一個 commit 一件事；修 bug 與補測試分兩個 commit。
- Commit message 說明「為什麼」，程式碼本身說明「做了什麼」。
- 不 commit `.env` / `__pycache__` / `.pyc`。

### 程式碼品質
- 函式單一職責，超過 40 行考慮拆分；相同計算出現兩次就提取成函式。
- 禁止魔法數字：常數（如 `MONTHLY_BASE_DAYS = 30`）定義在模組頂部。
- 對外輸入必過 Pydantic 驗證（含 `ge`/`le` 邊界）。
- **不使用 `print()`**，一律 `logger = logging.getLogger(__name__)`。

---

## 服務模組

### services/analytics/

經營分析模組（招生漏斗 + 流失預警 — MVP）。
- `constants.py` — 閾值（A=3 連續缺勤 / C=30 on_leave / D=14 學費逾期）、漏斗 6 階段、學期起始日 proxy、`parse_roc_month` / `term_start_date` helpers
- `funnel_service.py` — 招生漏斗（雙源拼接 RecruitmentVisit + Student lifecycle）；`build_funnel`、`count_visit_side_stages`、`count_student_side_stages`、`slice_by_source`、`slice_by_grade`、`summarize_no_deposit_reasons`
- `churn_service.py` — A/C/D 三訊號 at-risk 偵測 + 12 月歷史趨勢；`detect_at_risk_students`、`build_churn_history`、`detect_signal_consecutive_absence`、`detect_signal_long_on_leave`、`detect_signal_fee_overdue`

對應 router：`api/analytics.py`，權限 `Permission.BUSINESS_ANALYTICS = 1 << 40`，預設只給 admin / supervisor 角色。
快取走 `report_cache_service`，三類別 + TTL：
- `analytics_funnel` — 30 min
- `analytics_churn_at_risk` — 5 min
- `analytics_churn_history` — 1 hr

實作說明：
- `RecruitmentVisit` 無 `student_id` FK，故 visit 端與 student 端用「招生年月 + 班別 + 來源」當共用維度（不直接 join）
- 學費逾期 D 訊號用「學期起始日 + 14 天」當 due_date proxy（`FeeItem` 無 `due_date` 欄位）
- 整班漏點名假缺勤過濾：若某天某班所有 active 學生皆無紀錄或皆「缺席」，視為老師當日漏點名

---

### 安全規範

- 檔案路徑操作必須做路徑穿越防護（參考 `_safe_attach_path()`）
- 使用者輸入不可直接拼接 SQL（已使用 ORM，維持此原則）
- 敏感操作（薪資匯出、核准）須記錄 `logger.warning` 稽核日誌
- 登入端點已有限流，新的高風險端點參考相同模式
- Rate Limiter（`utils/rate_limit.py`）支援兩個 backend：in-memory（預設）與 PG-based。多 worker 部署時設 `RATE_LIMIT_BACKEND=postgres`，由 `services/security_gc_scheduler.py` 自動清舊視窗
- JWT 黑名單（`utils/auth.is_token_revoked`）：使用者 logout 時 jti 寫入 `jwt_blocklist` 表；任何受保護端點透過 `get_current_user` / `verify_ws_token` 自動檢查
- 公開端點不可使用 `HTTPException(500, str(e))` 或 `HTTPException(500, f"...{e}")` —— 一律走 `utils/errors.raise_safe_500(e, context=...)`
- 升級依賴後必須跑 `pip-audit -r requirements.txt`；CI 會 enforce
- 完整資安發現清單見 `SECURITY_AUDIT.md`；新發現以 finding 格式追加並標記嚴重度
