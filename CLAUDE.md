# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 專案概述
幼稚園考勤與薪資管理系統，支援打卡記錄解析、自動薪資計算、勞健保扣繳、教師入口等功能。
另含**招生管理模組**：家長地址熱點圖、附近競爭幼兒園分析、市場情報、教育部資料同步。

## 技術棧
- FastAPI (Python 3), SQLAlchemy, PostgreSQL
- JWT (python-jose), PBKDF2 密碼雜湊
- 前端為獨立 repo（`ivyManageSystem-frontend`），Vue 3 + Vite + Element Plus + Pinia

---

## 開發指令

### 啟動後端
```bash
pip install -r requirements.txt
uvicorn main:app --reload --port 8088
```

### CI/CD

`.github/workflows/ci.yml`：push/PR 到 main 時自動執行 PostgreSQL service container + `pytest tests/ -v`。

---

## 環境變數（backend/.env）

| 變數 | 說明 |
|------|------|
| `DATABASE_URL` | PostgreSQL 連線字串。本地：`postgresql://yilunwu@localhost:5432/ivymanagement` |
| `ENV` | `development`（預設）或 `production`。production 模式下缺少 `DATABASE_URL` / `JWT_SECRET_KEY` 會直接拋出 RuntimeError |
| `JWT_SECRET_KEY` | JWT 簽名金鑰。開發模式下有 fallback 預設值，正式環境必須設定 |
| `CORS_ORIGINS` | 逗號分隔的允許來源，未設定時允許 localhost:5173 / 3000 |
| `GOOGLE_MAPS_API_KEY` | 招生模組地圖 / Geocoding / Places API 必填 |
| `RECRUITMENT_CAMPUS_LAT/LNG` | 本園座標，招生地圖中心點 |

---

## 架構重點

### 後端目錄結構
```
backend/
├── main.py              # App 建立、CORS、中間件、Router 註冊（~270 行）
├── startup/             # 啟動邏輯（從 main.py 拆分）
│   ├── seed.py          # 預設資料 seed（年級、職稱、設定、管理員等）
│   ├── migrations.py    # Alembic migration + 資料遷移
│   └── bootstrap.py     # 啟動編排（呼叫 seed + migration + 服務初始化）
├── api/                 # API Routers（40+ 個）
├── services/            # 商業邏輯服務
├── models/              # SQLAlchemy models + 連線管理
├── utils/               # 工具模組（auth、audit、rate_limit 等）
├── alembic/             # DB migration 版本
└── tests/               # pytest 測試（72+ 檔案）
```

### 後端服務注入模式
`SalaryEngine`、`InsuranceService`、`LineService` 在 `main.py` 啟動時建立為 singleton，再透過 `init_*_services()` 注入需要的 router：

```
main.py → init_salary_services(salary_engine, insurance_service, line_service)
        → init_config_services(salary_engine, line_service)
        → init_insurance_services(insurance_service)
        → init_overtimes_services(salary_engine)
        → init_leaves_services(salary_engine)
        → init_*_line_service(line_service)  # 多個 router 需要 LINE 通知
```

新增需要服務依賴的 router 時，必須遵循此模式。

### 資料庫連線
`models/base.py` 管理 singleton Engine（connection pool）。兩種取得 session 的方式：
- `get_session()` — 手動管理（使用後須 `.close()`）
- `session_scope()` — context manager，自動 commit/rollback/close（推薦新程式使用）

連線池參數：`pool_size=20, max_overflow=40, pool_recycle=1800, statement_timeout=30s`

Schema 異動使用 **Alembic**（`alembic/versions/`），啟動時自動執行 `alembic upgrade heads`。

### 中間件與可觀測性
| 中間件 | 檔案 | 功能 |
|--------|------|------|
| `RequestLoggingMiddleware` | `utils/request_logging.py` | request_id 關聯 + response time + 慢請求警告（> 2s） |
| `AuditMiddleware` | `utils/audit.py` | 自動記錄 POST/PUT/PATCH/DELETE 至 AuditLog 表 |
| `SecurityHeadersMiddleware` | `utils/security_headers.py` | nosniff、DENY、HSTS、CSP、Referrer-Policy |

其他可觀測性：
- `utils/slow_query_logger.py` — SQLAlchemy event listener，記錄 > 500ms 的 SQL 查詢
- `api/health.py` — `/health/live`（liveness）+ `/health/ready`（readiness，含 DB latency）

### 結構化日誌
- **生產環境**：JSON 格式輸出（`python-json-logger`），含 timestamp、level、module
- **開發環境**：可讀的純文字格式
- 設定位於 `main.py` 的 `_configure_logging()`

### Graceful Shutdown
`main.py` lifespan `finally` 區塊：
1. 關閉所有 WebSocket 連線（DismissalConnectionManager）
2. 釋放 DB 連線池（`engine.dispose()`）

### 權限系統
`utils/permissions.py` 定義 `Permission` IntFlag（位元遮罩），讀寫分離：
- 讀取：低位（如 `SALARY_READ = 1 << 11`）
- 寫入：高位（如 `SALARY_WRITE = 1 << 23`）

路由使用 `require_permission(Permission.SALARY_WRITE)` 做守衛（在 `utils/auth.py`）。

### 薪資計算邏輯（salary_engine.py）
- **`gross_salary`（月薪應發）** = `base_salary + allowances + performance/special_bonus + supervisor_dividend + birthday_bonus + meeting_overtime_pay + overtime_work_pay`
  - **不含** `festival_bonus` / `overtime_bonus`（這兩項獨立轉帳）
  - 含 `overtime_work_pay`（核准的加班費，與 `meeting_overtime_pay` 同屬當月應發）
  - 含 `birthday_bonus`（壽星 $500）
- **`festival_bonus`** 在發放月（2、6、9、12 月）才計入，`meeting_absence_deduction` 只從 `festival_bonus` 扣，**不進入** `total_deduction`
- **`bonus_separate`** 旗標：當 `festival_bonus + overtime_bonus + supervisor_dividend > 0` 時為 True，表示有另行匯款
- **`total_deduction`** = 勞保 + 健保 + 勞退 + 遲到/早退/請假扣款（無 `meeting_absence_deduction`）
- 時薪計算基準：`base_salary / 30 / 8`（MONTHLY_BASE_DAYS = 30，依勞基法）
- 加班費時薪基準：`emp.base_salary`（僅底薪，不含任何加給或獎金）

### 設定版本控制（稽核追蹤）
`SalaryRecord` 記錄計算時使用的 `bonus_config_id` 與 `attendance_policy_id`，確保薪資可回溯查核。

---

## 招生模組架構

### 資料來源與優先順序

附近幼兒園詳細資料由三個來源合併，**優先順序由高至低**：

| 優先 | 來源 | 提供欄位 | 說明 |
|------|------|---------|------|
| 1 | **`competitor_school` DB**（教育部爬蟲快取） | 電話、住址、類型、核定人數、月費、裁罰 flag | 由 `moe_kindergarten_scraper.py` 定期同步 |
| 2 | **kiang.github.io** | 月費備援、裁罰詳情文字 | 僅在 DB 無值時補充月費；`has_penalty=true` 時提供裁罰紀錄內容 |
| 3 | **Google Places API** | 名稱、座標、評分、Google Maps 連結 | 即時查詢，為地圖標記來源 |

**關鍵規則：**
- `competitor_school.has_penalty = false` → 不顯示裁罰（即使 kiang 有記錄）
- `competitor_school.has_penalty = true` → 顯示 kiang 的裁罰詳情
- 月費：DB 有值優先，DB 為 null 才用 kiang

### kiang.github.io 資料集
- `preschools.json` — 全台幼兒園 GeoJSON，包含經緯度、月費（教育部爬蟲沒有的欄位）
- `punish_all.json` — 裁罰紀錄，key 格式為 `負責人：姓名` / `行為人：姓名`

### 比對流程（usePreschoolGovData.js）
1. **並行查詢** Google Places 名稱 → DB（`findPreschoolFromDb`）與 kiang（`findPreschoolByName`，含 geo 距離加分）
2. **kiang 橋接**：DB 找不到時，用 kiang 的官方名稱（`p.title`）重試 DB 查詢（`findPreschoolFromDbByNames`）
3. **合併**：DB 欄位展開覆蓋 kiang；月費、裁罰依上述規則決定

### competitor_school 表重要欄位
```sql
school_name, school_type, owner_name, phone, address, district, city,
approved_capacity, approved_date, total_area_sqm,
monthly_fee,      -- 月費（需要 kiang 爬蟲同步，MOE 詳細頁無此欄位）
has_penalty,      -- 裁罰 boolean（由 moe_kindergarten_scraper 比對 punish_all.json 設定）
pre_public_type,  -- 準公共幼兒園
is_active, source_school_id, source_updated_at
```

### 後端 API 端點（招生相關）
| 端點 | 說明 |
|------|------|
| `GET /api/recruitment/gov-kindergartens` | 列出 competitor_school（支援分頁 / 篩選） |
| `POST /api/recruitment/gov-kindergartens/sync` | 觸發教育部爬蟲（背景執行） |
| `GET /api/recruitment/gov-kindergartens/sync-status` | 查詢同步進度 |
| `GET /api/recruitment/nearby-kindergartens` | Google Places 即時查詢（視野邊界） |

### 教育部爬蟲（moe_kindergarten_scraper.py）
- 爬取 `ap.ece.moe.edu.tw/webecems/pubSearch.aspx`，僅抓高雄市
- 同步完成後順帶 fetch `kiang.github.io/punish_all.json` 比對負責人，寫入 `has_penalty`
- 使用 `threading.Lock` 防止重複執行
- 同步狀態存於 `recruitment_sync_state` 表（`provider_name = 'moe_ece'`）

---

## 開發注意事項
- 回應語言：一律使用**繁體中文**
- `BonusConfig` DB 模型在 `startup/seed.py` 以 `DBBonusConfig` 匯入，避免與 `api/salary.py` 中同名 Pydantic schema 衝突
- 日誌使用 `logging.getLogger(__name__)`，不使用 `print()`
- `portal.py` 是教師自助入口，提供自己的考勤/請假/加班/薪資查詢，不具管理權限
- `api/dev.py` 包含開發/測試用端點，正式環境應留意是否需要關閉
- 啟動邏輯（seed、migration）在 `startup/` 目錄，不要放回 `main.py`
- pytest 設定統一在 `pyproject.toml`，不需 `pytest.ini`

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
- 不重複邏輯：相同計算出現兩次就提取成函式

**後端：**
- 所有對外輸入必須過 Pydantic 驗證（含 `ge`/`le` 邊界）
- 所有路由必須有 `require_permission()` 守衛
- 不使用 `print()`，一律 `logger = logging.getLogger(__name__)`
- 新路由若需要 `SalaryEngine` / `InsuranceService`，必須透過 `init_*_services()` 注入，不直接 import

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
- Rate Limiter（`utils/rate_limit.py`）為 in-process 記憶體版，僅適用單 worker 部署；多實例需改 Redis-backed 方案
