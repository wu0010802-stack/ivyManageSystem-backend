# 全面資安 re-audit 執行計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development（recommended）or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 對 4/17 後新增的攻擊面做平行唯讀 deep-trace 審查 + 全系統標準類別 regression 輕掃，產出 severity-ranked、經動態重現驗證的 finding 清單，寫進 `SECURITY_AUDIT.md`，作為 triage 與後續修補的依據。

**Architecture:** Phase 0 基線 manifest → Phase 1 平行唯讀 audit lane（L1–L6 + 標準輕掃 + 新基建 regression）→ Phase 2 自動化工具 → Phase 3 統整去重分級 → Phase 4 對 High/Critical 候選做動態重現驗證 → Phase 5 寫報告。**本計畫只做「體檢 + 出報告」；修補是 triage 後另寫的 plan。**

**Tech Stack:** FastAPI / SQLAlchemy / Alembic / PostgreSQL（後端）、Vue 3 / TS（前端）、pytest / vitest、pip-audit / npm audit / bandit；唯讀 audit 用 `Explore` / `general-purpose` / 專用 agent（`migration-reviewer` / `finance-correctness-reviewer` / `cross-repo-parity-checker`）；動態重現用 `start.sh` dev server（:8088）+ 本機 dev DB（postgres MCP）。

**對應 spec:** `docs/superpowers/specs/2026-06-02-security-reaudit-design.md`

**產出工作區:** 各 lane 的原始 finding 寫到 workspace `~/Desktop/ivyManageSystem/.scratch/security-reaudit/L<n>-findings.md`；最終統整寫進 `ivy-backend/SECURITY_AUDIT.md`。

---

## Phase 0 — 基線 manifest（fan-out 前必做）

### Task 0: 確認被審 branch 上實際存在哪些防護

**Files:**
- Read: `ivy-backend/`（git 狀態）、`alembic/versions/`、`api/parent_portal/`、`utils/`、`config/medical.py`
- Create: `~/Desktop/ivyManageSystem/.scratch/security-reaudit/phase0-manifest.md`

- [ ] **Step 1: 記錄被審 commit 基準**

Run（在 ivy-backend）：`git branch --show-current && git rev-parse HEAD && git status --short`
記錄：審的是 `main` 的哪個 commit；working tree 有哪些未 commit 的 M/untracked（這些**不在**審查範圍，避免審 dirty WIP）。

- [ ] **Step 2: 逐項確認新防護的程式碼是否真的在 main（非 worktree）**

對下列每一項，確認檔案存在於 main working tree 且非僅在 `.claude/worktrees/`：
```bash
cd ~/Desktop/ivy-backend
for f in utils/readonly_guard.py api/parent_portal/auth.py api/parent_portal/dsr.py \
  api/parent_portal/consent.py api/parent_portal/data_export.py config/medical.py \
  utils/medical_encryption.py models/medical_access_log.py models/permission_models.py \
  api/permissions_admin.py utils/rate_limit.py utils/rate_limit_db.py models/security.py; do
  test -f "$f" && echo "PRESENT $f" || echo "MISSING $f"; done
ls alembic/versions/ | grep -iE "consent01|dsrreq01|medacc01|permscope|portalimp|pdevsetup|rolesdb"
```
Expected：核心防護皆 PRESENT。**任何 MISSING 或只在 worktree 的項目 → 該 lane 改審「宣稱已落地但實際缺席」，並在報告列為 finding。**

- [ ] **Step 3: 確認動態重現環境可用**

Run：`cd ~/Desktop/ivyManageSystem && ./start.sh`（另開終端），確認 :8088 `/docs` 可達、本機 dev DB（postgres MCP）可連。記錄一個 `role=admin` 帳號與一個非 admin 員工（self-guard 用）。

- [ ] **Step 4: 寫 manifest 並 commit 不需要**

把 Step 1–3 結果寫進 `phase0-manifest.md`（scratch，不 commit）。Phase 1 各 lane 以此 manifest 為準，只審 PRESENT 的程式碼。

---

## Phase 1 — 平行唯讀 deep-trace audit lane

> 每個 lane 派一個唯讀 agent，**追實際程式路徑、讀完整檔案，不靠 grep 推論落差**。每個 agent 的輸出寫成 `~/Desktop/ivyManageSystem/.scratch/security-reaudit/L<n>-findings.md`，格式：`位置 / 描述 / 攻擊情境 / 暫定嚴重度 / 重現方式建議`。L1–L6 + 標準輕掃 + 新基建 regression 可**全部平行 dispatch**（彼此無依賴）。

### Task 1 (L1): 教師端冒充雙模式

**Agent:** `general-purpose`（唯讀）
**Files（entry points，須往下追）:**
- `utils/readonly_guard.py`（global readonly middleware）
- `api/auth.py`（impersonate token 簽發 / 3 claim）、`utils/auth.py`（token 驗證、claim 解析）
- `api/guardians_admin.py`（`create_device_setup_code` — **已知種子 finding**）
- `api/audit.py`、`utils/audit.py`、`models/audit.py`（audit 歸屬欄位 portalimp01）

- [ ] **Step 1: 追三種 token claim 的簽發與驗證路徑**

回答：readonly 預覽 token vs write 代操作 token 如何區分？哪個 endpoint 簽發 write token、是否真的只限 admin（非園長）？token claim 能否被竄改/降級攻擊（readonly→write）？

- [ ] **Step 2: 追 `readonly_guard` middleware 的覆蓋面**

列出 middleware 攔截的 method/path 規則。找**未覆蓋的 mutation 路徑**：WebSocket？檔案上傳？非 REST 端點？sub-app？確認 readonly token 持有者真的無法觸發任何 write。

- [ ] **Step 3: 追 audit 歸屬**

確認被冒充情境下所有 mutation 都記錄「真正操作的 admin」。**驗證已知缺口**：`create_device_setup_code` 是否漏 admin stamp（直寫 audit 未帶冒充者）。掃其他「直寫 audit」的 caller 是否同樣漏 stamp。

- [ ] **Step 4: 追「禁止冒充 admin / 停用 / 離職」防護是否在新雙模式仍成立**

- [ ] **Step 5: 寫 `L1-findings.md`**

### Task 2 (L2): 家長 fallback 登入（device-trust）

**Agent:** `general-purpose`（唯讀）
**Files:**
- `api/parent_portal/auth.py`（setup code 產生 / device-trust 換 token / 30d rolling）
- `models/parent_binding.py`、`api/guardians_admin.py`（admin 產生 setup code）
- alembic `pdevsetup01`

- [ ] **Step 1: 追設定碼的產生與熵**

設定碼怎麼產（`secrets` vs 可預測）？長度/字元集？有效期？一次性還是可重用？能否枚舉/暴力破解（rate limit？嘗試鎖定？）。

- [ ] **Step 2: 追 device token 的生命週期**

30d rolling window 如何續期？token 竊取後可用多久？能否撤銷？綁定哪些裝置指紋（可偽造？）。

- [ ] **Step 3: 追授權邊界**

device-trust 登入後 `_get_parent_student_ids` 是否仍嚴格綁定該家長的學生（跨家庭 IDOR）？與 PII retention GC（終態 365d 抹除）互動是否產生越權或資訊洩漏？

- [ ] **Step 4: 寫 `L2-findings.md`**

### Task 3 (L3): 權限 row-level scoping

**Agent:** `general-purpose`（唯讀）
**Files:**
- `utils/permissions.py`（`Permission` enum、scope wire format `<CODE>:<scope>`）
- `utils/auth.py`（`has_permission` / `require_permission` scope-aware 路徑）
- `utils/portfolio_access.py`、`accessible_*_ids` scope helper

- [ ] **Step 1: 追 `<CODE>:<scope>` 的解析與比對**

`own_class` vs `all` 如何解析？解析失敗 / 畸形字串（`CODE:`、`CODE:all:own_class`、大小寫、空白）時 fail-open 還是 fail-closed？能否構造繞過？

- [ ] **Step 2: 追 scope 在實際 endpoint 的 enforce**

抽樣數個 `own_class` 端點，確認 scope 真的縮限到本班資料、沒有只擋 read 卻漏 write 的端點。確認 `has_permission` scope-aware 改動沒有讓既有純字串比對的 hot path 漏判。

- [ ] **Step 3: 寫 `L3-findings.md`**

### Task 4 (L4): DB-driven 自訂角色

**Agent:** `general-purpose`（唯讀）
**Files:**
- `api/permissions_admin.py`（role/permission CRUD endpoint）
- `models/permission_models.py`（Role / permission 表）

- [ ] **Step 1: 追角色建立/指派的授權**

誰能建/改/刪自訂角色、指派給 user？能否自我提權（建一個含 `USER_MANAGEMENT_WRITE` / `SALARY_READ` 的角色指派給自己）？是否有 blast-radius 守衛？

- [ ] **Step 2: 追 cascade delete + token_version race**

刪角色時：bump token_version → array_remove → delete pd 的順序是否正確（race 窗口讓舊 token 仍帶已撤銷權限）？SQLite/PG 雙路徑是否一致。

- [ ] **Step 3: 寫 `L4-findings.md`**

### Task 5 (L5): 個資法 DSR / consent / 醫療加密

**Agent:** `general-purpose`（唯讀）
**Files:**
- `api/parent_portal/dsr.py`（delete/correct/opt-out）、`api/parent_portal/data_export.py`、`api/parent_portal/consent.py`
- `config/medical.py`、`utils/medical_encryption.py`、`models/medical_access_log.py`、`api/students.py`（醫療 reason gate）

- [ ] **Step 1: 追 DSR / data-export 的家庭綁定**

每個 DSR 端點是否嚴格綁定請求家長自己的學生？能否刪/改/匯出**別家**學生資料（跨家庭 IDOR）？opt-out 是否真的停止處理。

- [ ] **Step 2: 追醫療欄位加密與解密 access control**

Fernet key 來源與管理（env？硬編碼？test key 外洩到 prod？）；解密路徑是否一律經 reason gate + 寫 `medical_access_log`；有無繞過 gate 直接讀明文的 caller（report / export / portal）。

- [ ] **Step 3: 追 consent gating**

未同意 / consent 版本過期時，PII 寫入/讀取是否真的被擋（fail-open 還是 fail-closed）。

- [ ] **Step 4: 寫 `L5-findings.md`**

### Task 6 (L6): 批次加班 / 自動配號 / lifecycle / 才藝跨班

**Agent:** `general-purpose`（唯讀）
**Files:**
- `api/overtimes.py`（批次加班建立）
- `api/students.py` + `api/employees.py`（學號/工號 before_flush 自動配號）
- `services/student_lifecycle.py` + `utils/student_lifecycle.py`（lifecycle transition）
- `api/portal/activity.py`（才藝任何老師跨班點名）

- [ ] **Step 1: 批次加班** — authz（僅管理端？）、all-or-nothing 422 是否可被用來探測/DoS、逐人時數有無 mass-assignment。
- [ ] **Step 2: 自動配號** — 學號/工號是否可枚舉推測他人？seq 配號在並發下是否 race（重號/跳號）。
- [ ] **Step 3: lifecycle** — transition endpoint authz；preview 端點是否洩漏未授權資訊。
- [ ] **Step 4: 才藝跨班** — 確認「任何非家長老師可點任意場次」是 by-design 且未順帶開放他班**敏感** PII（只該開點名名冊）。
- [ ] **Step 5: 寫 `L6-findings.md`**

### Task 7 (標準類別輕掃): 全系統 OWASP regression

**Agent:** `Explore`（very thorough，唯讀）+ `find-bugs` skill 視需要
**Scope:** AuthN/AuthZ guard 覆蓋率、IDOR 五威脅模型、Injection（`text()` 拼接 / path traversal / command）、XSS（`v-html`）、Secret 管理（`git grep -i "service_role\|SUPABASE_SERVICE\|SECRET"` 應只在 .env.example/docs）、檔案上傳、CSRF（SameSite/Origin）、Mass-assignment。

- [ ] **Step 1: 跑 IDOR 五威脅模型抽查**，重點放在 4/17 後新增的 router（git log 對照）。
- [ ] **Step 2: 搜 `text(` 拼接、`os.system`/`subprocess`、路徑組合，逐一判定是否有 user input 注入。**
- [ ] **Step 3: 確認新 router 都有 permission 守衛**（對照 `main.py` router 註冊）。
- [ ] **Step 4: 寫 `standard-sweep-findings.md`**

### Task 8 (新基建 regression): 4/17 後安全基建

**Agent:** `general-purpose`（唯讀）
**Files:** `utils/rate_limit.py`、`utils/rate_limit_db.py`、`models/security.py`、`utils/auth.py`（jwt blocklist）、`utils/cookie.py`、`api/activity/public.py`（honeypot）

- [ ] **Step 1: PostgresLimiter** — 確認 DB 失敗 **fail-open** 的範圍（**已知種子**）：是否打掛 DB 即全面關閉限流（含登入暴力）？fail-open 是否該縮限到非 auth 端點。
- [ ] **Step 2: JWT blocklist** — logout 寫 jti、`get_current_user` / `verify_ws_token` 查詢是否真的擋掉已撤銷 token；GC 是否誤刪未過期 jti。
- [ ] **Step 3: cookie SameSite / honeypot** — Strict 是否破壞 LIFF；honeypot silent reject 是否有 side channel。
- [ ] **Step 4: 寫 `infra-regression-findings.md`**

### Task 9 (專用 agent 平行): migration / finance / cross-repo parity

- [ ] **Step 1:** 派 `migration-reviewer` 審 4/17 後新 alembic（consent01 / dsrreq01 / medacc01 / permscope* / portalimp01 / pdevsetup01 / rolesdb01 / studnum01 等）的可逆性與破壞性 DDL → `migration-findings.md`
- [ ] **Step 2:** 派 `finance-correctness-reviewer` 審薪資 engine / 健保補充費 / 考核年終獎金的金額正確性 + authz → `finance-findings.md`
- [ ] **Step 3:** 派 `cross-repo-parity-checker` 審 PII denylist + 權限字串集合前後端同步漂移 → `parity-findings.md`

---

## Phase 2 — 自動化工具補強

### Task 10: CVE + SAST

- [ ] **Step 1: 後端 CVE**

Run：`cd ~/Desktop/ivy-backend && pip-audit -r requirements.txt`（fresh resolve）
記錄任何 CVE 與修補版本。

- [ ] **Step 2: 前端 CVE**

Run：`cd ~/Desktop/ivy-frontend && npm audit --production --audit-level=moderate`

- [ ] **Step 3: Python SAST（best-effort）**

Run：`cd ~/Desktop/ivy-backend && pip install bandit >/dev/null 2>&1 && bandit -r api utils services models -ll -q || echo "bandit unavailable, skip"`
（`semgrep --config auto` 若可裝則加跑；裝不起來則記錄 skip，不阻塞。）

- [ ] **Step 4: 寫 `tooling-findings.md`**（CVE 視為已知 finding 直接列；SAST 結果須人工複核去誤報）

---

## Phase 3 — 統整 / 去重 / 分級

### Task 11: 合併所有 lane finding

- [ ] **Step 1:** 讀 `.scratch/security-reaudit/*-findings.md` 全部，合併、去重（同位置同類型合一筆）。
- [ ] **Step 2:** 按 spec §6 severity rubric 分級（Critical/High/Medium/Low/Info），標注 lane 來源。
- [ ] **Step 3:** 產出待驗證清單：所有 **High/Critical** 候選列入 Phase 4 動態重現。

---

## Phase 4 — 動態重現驗證（關鍵紀律）

### Task 12: 對 High/Critical 候選逐一實際重現

> **不是第二個 read-based agent 附和。** 必須用下列其一實際重現：curl 打跑起來的 dev server / 查本機 dev DB / 寫一個會失敗的測試。重現不出來 → 標「unverified / 需人工複核」，不當 confirmed 出貨。只用唯讀/試算路徑，**不碰** `/calculate` `/close`。

- [ ] **Step 1: 對每個 High/Critical 候選選定重現手段**

範例（越權型）：取一個低權限 / 跨家庭帳號的 token，`curl` 目標端點，預期「應被擋」；若回 200 + 他人資料 → confirmed。
範例（fail-open 型）：在本機暫時讓 limiter DB 查詢拋錯，觀察限流是否失效（用測試或 monkeypatch，不改 prod 行為）。
範例（scope 繞過）：寫一個 pytest 構造畸形 `<CODE>:<scope>` 字串，斷言應 fail-closed。

- [ ] **Step 2: 逐一執行重現，記錄結果**

每筆標記 `confirmed（附重現指令/測試）` 或 `unverified（附嘗試過程與卡點）`。

- [ ] **Step 3: 把重現用的 failing test 暫存**

confirmed 的 finding 若寫了 pytest 重現，存到 `.scratch/security-reaudit/repro/`，作為後續 fix plan 的回歸測試起點（TDD：修補時這些 test 從紅轉綠）。

---

## Phase 5 — 寫報告

### Task 13: 擴充 SECURITY_AUDIT.md

**Files:**
- Modify: `ivy-backend/SECURITY_AUDIT.md`（新增「## 2026-06-02 re-audit」章節）

- [ ] **Step 1: 寫 re-audit 章節**

包含：總評、findings 清單（每筆：位置 / 描述 / 攻擊情境 / 嚴重度 / **驗證狀態（confirmed-動態重現 / unverified）** / 建議修法）、前端 finding 獨立子章節、CVE 表、未發現重大風險的項目、建議修復優先順序表。

- [ ] **Step 2: Commit（只 add SECURITY_AUDIT.md，不碰 user WIP）**

```bash
cd ~/Desktop/ivy-backend
git add SECURITY_AUDIT.md
git commit -m "docs(security): 2026-06-02 全面 re-audit findings 報告"
```

- [ ] **Step 3: 向 user 報告 + triage gate**

呈現 severity 摘要表，請 user 決定修補範圍與優先序。**修補是 triage 後另寫的 fix plan**（從 `origin/main` 開 branch、TDD、前後端分開 commit）—— 不在本計畫內。

---

## Self-Review（plan vs spec 覆蓋檢查）

- spec §2.1 深審 6 攻擊面 → Task 1–6 ✅
- spec §2.2 標準類別輕掃 → Task 7 ✅
- spec §2.3 新基建 regression → Task 8 ✅
- spec §4 Phase 0 manifest → Task 0 ✅
- spec §4 專用 agent → Task 9 ✅
- spec §2 工具 → Task 10 ✅
- spec §5 動態重現驗證紀律 → Task 12 ✅
- spec §6 產出/分級 → Task 11 + Task 13 ✅
- spec §7 修補流程 → 明確標為 triage 後另寫 plan（不在本計畫）✅
- spec §8 種子 finding → Task 1 Step 3 + Task 8 Step 1 ✅
