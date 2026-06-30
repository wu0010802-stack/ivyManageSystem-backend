# 電子打卡（園內 Kiosk 即時打卡）設計

- 日期：2026-06-30
- 範圍：跨前後端（`ivy-backend` 主邏輯 + `ivy-frontend` kiosk 頁與 Portal/管理端 UI）
- 狀態：設計定案，待寫實作計畫

---

## 1. 背景與現況

系統已有成熟的考勤「資料層」，但**沒有讓員工現場即時打卡的入口**。

現況（盤點結果）：

- `Attendance` 表採「一天一筆、上班/下班雙時間戳」（`punch_in_time` / `punch_out_time`），定義於 `models/attendance.py:38-113`。
- 班別感知的遲到/早退/缺卡判定：`utils/attendance_shift_window.compute_status_for_employee_date()`（優先序 DailyShift → 週班別 → 員工自訂 → 預設 08:00/17:00）。
- 請假同步：`utils/attendance_leave_merge.merge_attendance_with_leave()`。
- 自我守衛：`utils/attendance_guards.require_not_self_attendance()` / `assert_no_self_in_batch()`（員工不可改自己考勤，即使有 `ATTENDANCE_WRITE`）。
- 薪資聯動、封存月份拒寫、稽核欄位（`confirmed_action/by/at`）皆已存在。

**目前考勤資料三條來源，全都不是現場即時打卡**：①管理端批次匯入 CSV/Excel（`api/attendance/upload.py`）②管理端手動補卡（`api/attendance/records.py` `POST /record`）③教師 Portal 補卡申請 → 主管審核。

本設計補上缺口：**園內公用裝置（kiosk）即時打卡**。

---

## 2. 目標與範圍

### In scope

1. 一台園內公用裝置（平板/電腦）固定放置，員工**選名單 + 輸入個人 PIN** 即時打卡。
2. 後端新增「名單查詢」「即時打卡」端點，受 **IP 白名單** + **PIN** 雙重防護，**不走個人 JWT**。
3. 即時打卡寫入既有 `Attendance` 表，重用既有 status 重算與請假同步。
4. PIN 管理：員工在 Portal 自設/改 PIN、管理端可重置。
5. `Attendance` 新增 `source` 欄位區分打卡來源（`kiosk` / `import` / `manual`）。
6. 前後端測試。

### Out of scope（follow-up）

- e2e critical-path 納入 kiosk 打卡（列為第 6 個 mutation，本次不做）。
- GPS / 地理圍欄 / 相機 / QR / LINE LIFF 打卡（本次採 IP 白名單 + PIN，不做定位）。
- NFC / RFID 實體卡。
- 打錯卡的更正：**重用既有 Portal 補卡申請流程**，不新做。

---

## 3. 設計決策摘要（澄清結果）

| 決策 | 選定 |
|------|------|
| 打卡場景/載體 | 園內公用裝置（kiosk）|
| 身分驗證 | 選名單 + 個人 PIN |
| 裝置授權/信任邊界 | 綁定園內網路 **IP 白名單**（第一道關）；PIN 為身分主防線（兩者疊加）|
| 上/下班判定 | 系統自動判定 + 顯示確認 |
| PIN 管理 | 員工在 Portal 自設/改 + 管理端重置 |
| PIN 長度 | **4–6 位數字可變**（員工自訂位數）|
| 重複打卡 | **first-in / last-out**：首次=上班（不可覆蓋）、之後每次=更新下班時間（末次為準，可覆蓋）|
| `source` 欄位 | 新增（`kiosk`/`import`/`manual`）|

---

## 4. 資料模型變更（Alembic migration）

### 4.1 `Employee` 新增欄位

- `punch_pin_hash`：`String`，nullable。PIN 經雜湊儲存，**明文不落庫**。重用後端現有的密碼雜湊工具（與 `User` 密碼相同的 hashing context；實作時定位現有 `verify`/`hash` 函式，禁止自寫雜湊）。
- `punch_pin_set_at`：`DateTime`，nullable。設定/重置時間，供稽核。

### 4.2 `Attendance` 新增欄位

- `source`：`String(20)`，nullable。打卡來源：`kiosk`（即時打卡）/ `manual`（管理端補卡）/ `import`（批次匯入）。
  - 新寫入路徑明確填值：kiosk 端點填 `'kiosk'`；`POST /record` 補卡填 `'manual'`；`upload*` 匯入填 `'import'`。
  - **不 backfill 歷史列**（YAGNI）；歷史列 `source` 維持 `NULL`，報表以「`NULL` 視為 legacy/未知來源」處理。

### 4.3 Migration 注意

- 兩欄皆 nullable + 無 server default → 對既有列零破壞，downgrade 直接 `drop_column`。
- 遵循專案 Alembic 慣例：single-head、可逆、繁中註解。

---

## 5. 後端 API

新子模組 `api/attendance/kiosk.py`（掛在既有 `api/attendance/` 子套件，前綴 `/api/attendance/kiosk`）。

### 5.1 `GET /api/attendance/kiosk/roster` — 打卡名單

- **守衛**：IP 白名單（見 §7.1）。**不需 JWT**。
- **回傳（最小揭露）**：員工清單，每筆只含 `employee_id`、顯示名（姓名）、`has_pin`（是否已設 PIN）、`today_state`（`none`/`in_only`/`done`，供裝置顯示「已上班」等標記，可選）。**不回電話/email/任何 PII**。
- 只列在職、可打卡員工（排除離職）。

### 5.2 `POST /api/attendance/kiosk/preview` — 打卡預判（確認用）

- **守衛**：IP 白名單 + PIN。
- **Request**：`{ employee_id, pin }`。
- **行為**：驗證 PIN（失敗計入限流，見 §7.2）。成功則依 §6 規則回傳「即將記為上班/下班」與時間，**不寫入**。
- **Response**：`{ employee_name, action: "punch_in"|"punch_out", will_overwrite: bool, current_punch_out?: time, server_time }`。
- 目的：前端確認畫面顯示用，避免員工誤打。

### 5.3 `POST /api/attendance/kiosk/punch` — 即時打卡（寫入）

- **守衛**：IP 白名單 + PIN。
- **Request**：`{ employee_id, pin }`。**請求體不接受任何時間戳**（時間一律取伺服器當前時間，杜絕改時間作弊）。
- **行為**：驗證 PIN → 依 §6 寫入當天 `Attendance`（first-in/last-out）→ 重算 status → merge 請假 → 填 `source='kiosk'`。
- **Response**：`{ employee_name, action, punch_time, status }`（供成功畫面顯示）。
- **self 特例**：本端點**不套** `require_not_self_attendance`（員工本就打自己卡），但反向鎖死：只能寫 PIN 驗證通過的 `employee_id`、只能寫伺服器當前時間、`punch_in` 不可覆蓋。

### 5.4 `PUT /api/portal/me/punch-pin` — 員工自設/改 PIN

- **守衛**：個人 JWT（Portal 既有登入）。**不受 kiosk IP 限制**（員工在家也能設）。
- **Request**：`{ pin }`（4–6 位數字；改 PIN 沿用 Portal 登入身分即可，不需舊 PIN）。
- 寫 `punch_pin_hash` + `punch_pin_set_at`。

### 5.5 `POST /api/employees/{id}/reset-punch-pin` — 管理端重置 PIN

- **守衛**：`ATTENDANCE_WRITE`（管理端）。
- **行為**：**清空 PIN**（`punch_pin_hash = NULL`、`punch_pin_set_at = NULL`），員工須到 Portal 重設後才能再打卡。管理端不設、也不回傳任何明文 PIN（避免明文 PIN 經手管理員）。
- kiosk roster 對清空 PIN 的員工回 `has_pin=false`，點選時提示「請先到教師入口設定打卡 PIN」。

> 端點路徑/歸屬（放 `api/attendance/` vs `api/portal/` vs `api/employees.py`）以實作計畫與既有慣例為準；本節定義行為契約。

---

## 6. 打卡核心邏輯（first-in / last-out）

打卡（或預判）當下，查當天該員工的 `Attendance` 列：

| 當天狀態 | 動作 | 寫入 |
|---------|------|------|
| 無列 或 `punch_in_time` 為空 | **上班** | 建列/填 `punch_in_time = now()` |
| 有 `punch_in_time`、`punch_out_time` 為空 | **下班** | 填 `punch_out_time = now()` |
| `punch_in_time`、`punch_out_time` 皆有 | **下班（覆蓋）** | 以 `now()` 覆蓋 `punch_out_time`（末次為準）；`will_overwrite=true` |

規則要點：

- **`punch_in_time` 一旦寫入不可由 kiosk 覆蓋**（首次即上班；打錯走補卡申請）。
- **`punch_out_time` 由每次後續打卡持續覆蓋**（最晚出為準）。中間值不保留（非事件流）。
- 寫入後一律呼叫 `compute_status_for_employee_date()` 重算遲到/早退/缺卡，並 `merge_attendance_with_leave()` 同步請假旗標。
- 跨夜班沿用既有修正（`punch_out < punch_in` → 下班改隔日，`records.py:307-309`）。
- 封存月份沿用既有拒寫守衛。

---

## 7. 安全設計（最關鍵）

### 7.1 IP 白名單（第一道關）

- 新增設定 `ATTENDANCE_KIOSK_ALLOWED_IPS`：CIDR 列表（逗號分隔），如 `203.0.113.10/32,198.51.100.0/24`。
- `roster`/`preview`/`punch` 三端點檢查 **真實 client IP**：重用 prod 已驗證可靠的 `TRUSTED_PROXY_IPS` 解析鏈（偽造 `X-Forwarded-For` 會被忽略，取真實公網 IP）。
- **Fail-closed**：未設定或空 → kiosk 端點**拒絕所有請求**（403，等同停用），不會意外全開。
- 不在白名單 → 403。

### 7.2 PIN 速率限制（必須）

- PIN 僅 4–6 位數字、空間小，**一定要限流**防暴力猜。
- per-employee + per-IP 計數：連續 N 次 PIN 失敗後鎖定該員工一段時間（時窗與次數於實作計畫定，如 5 次/15 分鐘）。
- 重用既有 in-memory 限流基建（注意測試污染：`conftest` autouse reset，見記憶 `feedback_inmemory_ratelimiter_test_pollution`）。

### 7.3 self 特例與反向鎖死

即時打卡端點**不套** `require_not_self_attendance`（員工本就記錄自己此刻的卡），改以反向限制保證最小權限：

1. 只能寫 PIN 驗證通過的那個 `employee_id`（無法指定他人）。
2. 只能寫**伺服器當前時間**，請求體不接受任意時間戳。
3. `punch_in_time` 不可覆蓋（防把上班時間改早）。
4. 不可改他人、不可刪除。

### 7.4 名單最小揭露

`roster` 只回 `employee_id` + 顯示名 + `has_pin`（+ 可選 `today_state`），**不回任何 PII**（電話/email/身分證等）。

### 7.5 PIN 雜湊與驗證

- PIN hash 用後端現有密碼 hashing context，明文不落庫、不回傳。
- 驗證走常數時間比對（沿用現有 `verify` 函式）。
- PIN 格式驗證：4–6 位純數字（前後端各驗）。

---

## 8. 前端（`ivy-frontend`）

### 8.1 Kiosk 打卡頁

- 新獨立路由（如 `/kiosk/punch`），**不掛個人登入**，受後端 IP 白名單守衛；為平板觸控設計（大按鈕、螢幕數字鍵盤、不需實體鍵盤）。
- 流程：名單網格（可搜尋/分組）→ 選員工 → PIN 數字鍵盤（4–6 位，因位數可變需「確認」鍵 + 刪除鍵，非滿位自動送出）→ 呼叫 `preview` 顯示「即將記為上班/下班（含覆蓋提示）」→ 確認 → 呼叫 `punch` → 成功畫面（姓名 + 上/下班 + 時間）→ 倒數自動返回名單。
- 失敗態：PIN 錯誤（顯示剩餘嘗試/鎖定）、IP 不允許、今日已完成（覆蓋提示已在 preview 處理）、未設 PIN（「請先到教師入口設定打卡 PIN」）。
- 新 API wrapper（`.ts`）：`src/api/attendanceKiosk.ts` 或併入 `attendance.ts`（實作計畫定），用 OpenAPI 型別。

### 8.2 Portal「設定打卡 PIN」

- 在現有 Profile 頁加區塊：設定/修改 PIN（4–6 位數字，二次輸入確認）。
- 用 `src/api/portal.ts` 既有模式。

### 8.3 管理端「重置打卡 PIN」

- 員工管理頁加動作按鈕，權限 `ATTENDANCE_WRITE`，呼叫 §5.5。

---

## 9. 測試策略

### 後端 pytest（新檔，重用既有 conftest fixtures）

- PIN 設定/驗證：hash 正確、自設、管理端重置（清空後不可打卡）。
- IP 白名單守衛：在/不在白名單 → 200/403；未設定 → fail-closed 403。
- self 特例邊界：① 不能寫他人 ② 請求體塞時間戳被忽略（一律用 server now）③ `punch_in` 不可覆蓋。
- 上/下班自動判定：無列→上班、有上班→下班、皆有→下班覆蓋（`will_overwrite`）。
- PIN 速率限制：連續錯誤鎖定（注意限流器測試污染 reset）。
- 封存月份拒寫、與請假 merge 同步、status 重算（遲到/早退）正確。
- `source='kiosk'` 正確寫入。

### 前端 vitest

- kiosk 流程元件（選員工 → PIN → 確認 → 成功 → 返回）。
- PIN 數字鍵盤元件（可變位數、刪除、確認）。
- Portal PIN 設定（二次確認、格式驗證）。
- 各失敗態提示（PIN 錯、IP 拒、未設 PIN）。

---

## 10. 設定與部署

- 新 env var `ATTENDANCE_KIOSK_ALLOWED_IPS`（後端）。prod 須設園內固定對外 IP 的 CIDR；未設則 kiosk 功能停用（fail-closed）。
- migration 隨後端部署跑（push origin/main 觸發 Zeabur 部署 + alembic upgrade，見 workspace CLAUDE.md 收尾紀律）。
- 前端 kiosk 路由須在生產可達；若要進一步限制，可於反代層再加一道內網限制（可選）。

---

## 11. 風險與前置

1. **依賴園內固定對外 IP**：若園內是浮動 IP / CGNAT，IP 白名單會失效 → 屆時 kiosk 端點 fail-closed 全擋。PIN 仍是身分主防線，但「裝置授權」需另尋方案（如裝置 token，列 follow-up）。**前置：確認園內網路有穩定對外 IP。**
2. **PIN 空間小**：4 位僅萬分之一，強依賴 §7.2 限流 + §7.1 IP 白名單；兩者缺一則暴力猜風險升高。
3. **first-in/last-out 的中間打卡不保留**：員工誤打導致下班時間被覆蓋為較早值的情境，靠 preview 覆蓋提示降低誤觸；真要保留完整事件流需改資料模型（out of scope）。
4. **kiosk 頁無登入**：任何能到達白名單 IP 的人都能看到名單（僅姓名）；PII 已最小揭露，殘餘風險為「姓名對園內網路內可見」，可接受。

---

## 12. 與既有跨端守則的對齊

- 後端先行（schema + router + pytest）→ 前端接上（`src/api/*.ts` + 頁面）→ 整合驗證 → 前後端分開 commit（workspace CLAUDE.md SOP）。
- 新權限：本設計**不新增** `Permission` enum 值（kiosk 端點走 IP+PIN 非 RBAC；管理端重置沿用 `ATTENDANCE_WRITE`）。
- 改 router/schema 後跑 OpenAPI codegen（`dump_openapi.py` + `gen:api`）更新前端型別。
- 新 PII 考量：roster 不回 PII；若日後 roster 加欄位需檢查 Sentry PII denylist 兩端同步。
