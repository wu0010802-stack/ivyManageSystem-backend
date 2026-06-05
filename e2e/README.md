# Playwright Smoke Tests

跨前後端的 critical path smoke：覆蓋 5 個 mutation（登入 / 打卡 / 送假 / 簽核 / 月底結薪試算）+ 前端主要頁面渲染。

## 為什麼放 workspace 而非 ivy-frontend？

- 這套測試**同時驅動前後端**，跟 `loadtest/` 一樣是跨 repo 的整合驗證。
- 放 frontend 會把 `@playwright/test`（含 chromium 下載 ~150MB）灌進 frontend 開發環境。
- **CI 整合是 follow-up**：目前只能本地跑。若日後要 gate PR，可以選擇：
  - 移到 `ivy-frontend/tests/e2e/`，frontend CI 順手跑（但需要在 CI 起後端）；
  - 或在 workspace 自建 monorepo workflow，分別 checkout 兩 repo。

## 前置條件

### 1. dev DB 內須有
- **一個 `role=admin` 的測試帳號**（建議在 dev DB 建 `e2e_admin`，**不要用個人 prod 帳號**）。
- **一個月薪、在職、不是 admin 本人** 的員工（attendance/leave 端點有 self-guard）。

### 2. 寫 `e2e/.env`

`.env.example` 範本被 secrets hook 擋住沒辦法寫入 repo。請手動建 `e2e/.env`：

```
E2E_BASE_URL=http://localhost:5173
E2E_API_URL=http://localhost:5173/api
E2E_ADMIN_USERNAME=<dev DB admin 帳號>
E2E_ADMIN_PASSWORD=<密碼>
E2E_TEST_EMPLOYEE_ID=<月薪、在職、非 admin 本人的 employee.id>
```

### 3. 啟動兩端 dev server

```
cd ~/Desktop/ivyManageSystem && ./start.sh
```

## 安裝 + 跑測試

```
cd ~/Desktop/ivyManageSystem/e2e
npm install
npx playwright install chromium

set -a; . ./.env; set +a
npm test              # headless
npm run test:headed   # 看瀏覽器
npm run test:ui       # Playwright UI mode（debug 用）
npm run report        # 失敗時看 trace / video
```

## 測試清單（6 specs / 8 test cases）

| Spec | Project | 內容 |
|------|---------|------|
| `health.spec.ts` | unauth | 未登入 `/api/auth/me` → 401 + 前端 root 載入 |
| `admin-login.spec.ts` | unauth | UI 登入流程 regression（正確 + 錯密碼 2 case）|
| `attendance-mutation.spec.ts` | auth-admin | POST `/attendance/record` → GET 驗 → DELETE 清 |
| `leave-flow.spec.ts` | auth-admin | POST `/leaves` → PUT `/leaves/{id}/approve` → DELETE |
| `salary-simulate.spec.ts` | auth-admin | POST `/salaries/simulate`（不寫 DB）|
| `admin-pages-render.spec.ts` | auth-admin | 4 個 admin 主頁載入無 console error |

`unauth` project 跑無 cookie 的測試；`auth-admin` project 預先用 `globalSetup` 拿 storage state（API login 一次），避免 login rate limit。

## 已知限制（**讀過再用**，不是 nice-to-have 細節）

- **login rate limit**：globalSetup 只登一次，所有 auth-admin tests 共用 storageState。`admin-login.spec.ts` 走 UI 登入計入 quota（一次正確 + 一次失敗）。觸限重啟後端清。
- **self-guard**：attendance/leave 不可寫 admin 自己的紀錄；globalSetup 已驗證 target ≠ admin.employee_id。
- **hourly 員工**：`/salaries/simulate` 422 hourly。globalSetup 已驗證 target 為月薪。
- **destructive 端點未測**：`/calculate`、`/calculate-async`、`/close` 有寫入或封存副作用，這套**不碰**。試算用 `/simulate`（`loadtest/README.md` 確認 no-write）。
- ⚠️ **attendance smoke 測的是「歷史校正路徑」不是「今日打卡」**：勞基法 5 年保存期擋 DELETE，所以 `preRetentionDate()=2018-06-15` 才能跑完整 CRUD。**真實的今日打卡寫入路徑（含 _assert_attendance_not_finalized）這套沒覆蓋**。若想覆蓋，要改成 POST→UPDATE-to-clear 模式（不 DELETE），或建一個 dedicated test employee 並對其用「絕不結算」的隔離 strategy。
- **leave 日期硬編碼 2027-01-18**：是 Monday + 非台灣國定假日。**會 drift**：當今日跨進 2027-01 後此日期會落入過去；當 today > 2032-01-18 此日期落入 5 年保存期。屆時需要更新 `futureWeekday()`。
- **simulate 對 employee 60 是「弱驗證」**：他沒考勤、沒薪資歷史，simulate 跑的是「白板月份」。Engine code path 有觸發到（assertion 驗 base_salary>0、gross_salary/net_pay 是 number），但 leave_deduction、overtime_pay 等分支沒走過。要強化得補 seed attendance/leave 資料。

## 結構

```
e2e/
├── package.json
├── playwright.config.ts          # 兩個 project (unauth / auth-admin) + storageState
├── tsconfig.json
├── fixtures/
│   ├── globalSetup.ts            # API login → storage-state.json + 驗證前置條件
│   └── smokeContext.ts           # 共享測試常數（target employee、未來日期、上月份）
└── tests/
    ├── health.spec.ts
    ├── admin-login.spec.ts
    ├── attendance-mutation.spec.ts
    ├── leave-flow.spec.ts
    ├── salary-simulate.spec.ts
    └── admin-pages-render.spec.ts
```

## Follow-ups（未做）

- 接 CI：兩 repo 互相 checkout 起後端 → 跑 smoke。需要 staging DB 或 docker-compose。
- 教師 portal smoke（`/portal/login`）：目前只測 admin。
- 家長端 smoke（LIFF mock 較複雜）。
- 增加 negative case：權限不足（403）、422 validation。
- 強化 attendance smoke：改測今日打卡路徑（POST + UPDATE-to-clear，不 DELETE）。
- 強化 simulate：seed attendance/leave 給 employee 60 後再 simulate，覆蓋 deduction/overtime 分支。

## dev DB 留下的種子資料

跑這套 smoke 前已在 dev DB 留兩筆：

```sql
-- 建 e2e_admin 帳號（無 employee_id 連動，純測試用）
SELECT id, username FROM users WHERE username = 'e2e_admin';   -- 目前 id=3

-- 建 E2E測試員工（target）
SELECT id, name FROM employees WHERE employee_id = 'E2E001';    -- 目前 id=60
```

不想保留時：

```sql
DELETE FROM users WHERE username = 'e2e_admin';
DELETE FROM employees WHERE employee_id = 'E2E001';
```

⚠️ 刪 employee 前要先確認他沒有任何 attendance/leave/salary 紀錄（smoke 跑完應該為 0；可用 `SELECT COUNT(*) FROM attendances WHERE employee_id = 60;` 等查證）。
