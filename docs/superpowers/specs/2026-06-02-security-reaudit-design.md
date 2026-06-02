# 全面資安體檢（re-audit）設計

**日期**：2026-06-02
**狀態**：Approved（待執行）
**範圍**：ivy-backend（FastAPI）+ ivy-frontend（Vue 3）跨前後端
**方法論**：平行唯讀 deep-trace audit + 自動化工具補強 + 對抗式「動態重現」驗證
**對應 tracker**：`ivy-backend/SECURITY_AUDIT.md`（canonical findings 文件）

---

## 1. 背景與目標

上次全面資安體檢為 **2026-04-17**（見 `SECURITY_AUDIT.md`），當時所有 finding（HIGH/MEDIUM/LOW）與後續 IDOR 全面盤查（46 筆）皆已修補。

但自 4/17 後，系統新增了**大量高敏感的攻擊面尚未經過系統性資安審查**，包括：教師端後台冒充雙模式、家長無 LINE fallback 登入（device-trust）、權限 row-level scoping、DB-driven 自訂角色、個資法 DSR/consent/醫療加密、批次加班、學號/工號自動配號、lifecycle 晉升、才藝跨班點名等。同時 4/17 後新落地的安全基建（PostgresLimiter、JWT blocklist、cookie SameSite、honeypot）也從未被 re-audit。

**目標**：對新攻擊面做 deep-trace 審查 + 對全系統標準類別做 regression 輕掃，產出 severity-ranked finding 清單，triage 後分批修補。

---

## 2. 範圍

### 2.1 深審攻擊面（4/17 後新增，逐一追實際程式路徑）

| Lane | 攻擊面 | 重點威脅 |
|------|--------|---------|
| L1 | 教師端**冒充雙模式** | readonly 預覽 vs write 代操作 3 token claim、`readonly_guard` middleware 繞過路徑（未覆蓋的 method/path）、audit 歸屬、冒充 admin 提權 |
| L2 | 家長**fallback 登入**（device-trust） | 設定碼暴力枚舉/可預測性、device token 竊取與重放、30d rolling window 濫用、帳號接管、與 PII retention GC 的互動 |
| L3 | **權限 row-level scoping** | `<CODE>:<scope>`（own_class/all）wire format scope 混淆/繞過、`has_permission` scope-aware 路徑、提權 |
| L4 | **DB-driven 自訂角色/權限** | 透過自訂角色提權、role 指派授權、cascade delete + token_version race |
| L5 | **個資法 DSR / consent / 醫療加密** | DSR 端點 IDOR（刪/改/匯出別家資料）、`parent_portal/data_export` 越家庭、醫療欄位解密 access control + reason gate、加密金鑰管理（Fernet key） |
| L6 | **批次加班 / 自動配號 / lifecycle / 才藝跨班** | batch authz 與 all-or-nothing 行為、學號/工號枚舉、seq 配號 race、lifecycle transition authz、才藝跨班點名 scope（確認 by-design 非過廣） |

### 2.2 標準類別全系統輕掃（catch regression）

AuthN/AuthZ guard 覆蓋率、IDOR 五威脅模型（員工互查 / 跨班教師 / 家長跨家庭 / 未認證公開 / 高權限欄位級）、Injection（SQLi via `text()` / path traversal / command）、XSS + CSP、Secret 管理（無硬編碼 key、service role key 處理）、依賴 CVE、檔案上傳（magic bytes / 大小 / 命名）、CSRF（SameSite / Origin）、Mass-assignment（Pydantic schema 過度暴露）、PII logging（Sentry denylist 前後端同步）。

### 2.3 4/17 後新基建 regression（從未 re-audit）

- **PostgresLimiter**：DB 失敗 **fail-open** → 打掛 DB 即關閉限流的 DoS 放大
- **JWT blocklist**：logout 寫 jti、`get_current_user` / `verify_ws_token` 查詢路徑正確性
- **cookie SameSite=Strict** + LIFF fallback env var 的安全影響
- **honeypot / 時序檢查**：silent reject 路徑

### 2.4 Out of scope

- 動態滲透測試（DAST/fuzz）、網路邊界（Nginx/Cloudflare）、磁碟/備份加密、雲端 IAM 設定
- 4/17 已驗證且未變動的程式（僅在 regression 輕掃中順帶覆蓋，不重做深審）
- 具體修法的設計（findings 出來後另寫 fix plan）

---

## 3. 威脅模型（actors）

1. **未認證公開使用者**（家長公開端點、報名查詢、LIFF 前置）
2. **家長**（LIFF / fallback device-trust 登入）— 跨家庭存取為主要威脅
3. **教師**（portal）— 跨班、自我資料越權
4. **一般員工 / 低權限管理者** — 提權、橫向越權
5. **被冒充情境下的 admin/園長** — readonly 越界成 write、audit 歸屬遺失
6. **持有自訂角色的使用者** — 透過 scope/role 組合提權

---

## 4. 執行架構

### Phase 0 — 基線 manifest（fan-out 前必做）

產出「**這個被審 branch 上實際存在哪些防護**」的精確清單，標出任何只在 worktree / 未合併、或記憶宣稱已落地但實際缺席的東西。**不審查不確定存在的程式碼。** 已知待精確核對項：`utils/medical_encryption.py` / `medical_field_type.py` / `scripts/encrypt_medical_fields.py` 是否完整在 main（grep 顯示疑似只在 worktree）。

### Phase 1 — 平行唯讀 deep-trace audit

按 §2.1 lane 拆成多個唯讀 agent 平行跑，每個 agent **追實際程式路徑、讀完整檔案，不靠 grep 推論落差**。專用 agent 各就位：

- `migration-reviewer` — alembic 安全性（downgrade、破壞性 DDL、partial unique、FK 策略、多 head）
- `finance-correctness-reviewer` — 薪資 engine / 健保補充費 / 考核年終獎金（金額正確性非舞弊，順帶看 authz）
- `cross-repo-parity-checker` — PII denylist + 權限字串集合前後端同步漂移
- `Explore` / `general-purpose` — 其餘 lane

### Phase 2 — 自動化工具補強

- `pip-audit -r requirements.txt`（後端 CVE）、`npm audit --production`（前端 CVE）
- `bandit -r .`（Python SAST）、`semgrep`（若可裝）— 工具只當**輔助**，業務邏輯型越權仰賴 Phase 1 deep-trace

### Phase 3 — 統整 / 去重 / 分級

合併各 lane finding，去重，按 §6 severity rubric 分級。

---

## 5. 驗證紀律（關鍵）

**「對抗式驗證」必須是動態重現，不是第二個 read-based agent 附和** —— 後者正是過去 grep 前提錯（3/4、4/x）的失敗模式。

- **High / Critical 候選 finding** 必須以下列其一**實際重現**才算 confirmed：
  - `curl` 打**跑起來的本機 dev server**（`start.sh`，:8088）
  - 查**本機 dev DB**（postgres MCP / psql）
  - 寫一個**會失敗的測試**證明漏洞存在
- 重現不出來的 → 標記 **「unverified / 需人工複核」**，不當 confirmed finding 出貨，但仍列入報告供人工判斷。
- 重現操作只用唯讀 / 試算路徑；**不碰** `/calculate` `/close` 等有封存副作用的端點（對齊 e2e 限制）。

---

## 6. 產出與追蹤

- **單一 findings 文件**：擴充既有 `ivy-backend/SECURITY_AUDIT.md`，新增「2026-06-02 re-audit」章節；前端 finding 放在**明確標示的「前端」子章節**，不拆散到兩 repo、不留在非 git 的 workspace 根目錄。
- 每筆 finding 格式：**位置 / 描述 / 攻擊情境 / 嚴重度 / 驗證狀態（confirmed-動態重現 / unverified）/ 建議修法**。
- **Severity rubric**：
  - **Critical** — 未認證或跨租戶可直接讀寫他人敏感資料 / 提權到 admin / 繞過加密
  - **High** — 需低門檻條件的越權、PII 外洩、authz 守衛缺失
  - **Medium** — 需特定前提的資訊洩漏、防禦深度缺口
  - **Low / Info** — 強化建議、理論風險

---

## 7. 修補流程（triage 後）

本 spec **只定流程，不預先寫死具體修法**（修補 gate 在 triage）。findings 出來後另寫 fix plan。修補時遵循：

1. **Triage**：與 user 確認哪些修、優先序（預設 Critical/High 先）。
2. **分支**：從 **`origin/main`** 開 branch（**不**從 local main，避免夾帶未 push 的 WIP）；動任何破壞性操作前先檢查 working tree（`git status` / 必要時 `stash -u`）。
3. **TDD**：每個修補先補一個能重現漏洞的回歸測試，再修到綠。
4. **跨端**：前後端**分開 commit**（不同 repo），Conventional Commits，繁體中文。
5. **驗證**：修補後對應 repo 測試套件「相對 baseline 無新增 fail」；含 backfill 的 migration 合併前手動 `alembic upgrade heads`。
6. **回填 tracker**：`SECURITY_AUDIT.md` 對應 finding 標記狀態 + 對應檔案。

---

## 8. 已知種子 finding（直接餵進對應 lane 驗證，不等重新發現）

- **L1**：`create_device_setup_code` 直寫 audit 但**漏 admin stamp**（冒充歸屬缺口，記憶已標「待修」）。
- **§2.3**：PostgresLimiter **fail-open on DB failure**（DoS DB → 限流關閉）。

---

## 9. 風險與注意事項

- 體檢只做靜態 deep-trace + 有限動態重現，**非完整滲透測試**；完整合規仍建議委外 DAST/pentest。
- 動態重現需 `start.sh` 起兩端 dev server + 本機 dev DB；觸發登入限流時重啟後端可清。
- 前端已全 TS-only；finding 涉及前端修補須遵守 `ivy-frontend/CLAUDE.md` 的 TS-strict 規範。
