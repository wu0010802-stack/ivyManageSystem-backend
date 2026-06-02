# 個資法 Phase 2（後端）實作計畫

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development（recommended）或 executing-plans。每 task TDD。Steps 用 `- [ ]`。

**Goal:** 讓既有 consent/DSR 記錄真正生效——consent 混合強制（service_essential gate + granular 咽喉 point-of-use）+ DSR 可執行（opt-out 即時 / delete 走既有 GC / correct 手動 + admin queue），全程 `CONSENT_ENFORCEMENT_ENABLED` dark-launch flag 守護。

**Architecture:** flag-gated。granular scope 在單一發送咽喉檢 consent（一處）+ coverage 斷言測試防新繞過路徑；service_essential 用後端 dependency 檢「當期 policy 已簽」（degraded-on-read 快取）；DSR admin queue approve/reject，delete 複用 `student_lifecycle.transition`，correct 手動。

**Tech Stack:** FastAPI / SQLAlchemy / pytest。venv `./venv_sec/bin/python`。

**對應 spec:** `docs/superpowers/specs/2026-06-02-pdpa-phase2-enforcement-design.md`。**本 plan 只含後端 P2-1/2/3；前端 P2-4/5 為後端落地後另寫的 plan**（FE 依賴後端定案的 `X-Consent-Required` 信號與 admin queue 契約）。

**前置（執行時）:** 用 using-git-worktrees 從 `origin/main`（或當前整合線）開 worktree；本功能與既有 security 修補 branch 無檔案交集，可獨立分支。

**已驗證錨點:**
- `line_push` 咽喉：`services/line_service.py:440 LineService._push_to_user`（`push_to_user`/`push_text_to_user`/`push_flex_to_user` 皆經此）。
- lifecycle：`services/student_lifecycle.py:108 transition(...)`、`utils/student_lifecycle.py:31 set_lifecycle_status(...)`。
- consent 模型：`models/consent.py`（`ParentConsentLog`、`PolicyVersion`、`CONSENT_SCOPE_*`、`CONSENT_SCOPES`）。
- DSR 模型：`models/dsr.py`（`DsrRequest`、`DSR_REQUEST_TYPE_*`、`DSR_STATUS_*`）。
- 權限：`utils/permissions.py`（`Permission` enum / `PERMISSION_LABELS` / `ROLE_TEMPLATES`）。
- config 模式：`config/core.py CoreSettings`（bool 設定 + property）。
- 家長 ownership：`api/parent_portal/_shared.py:_get_parent_student_ids` / `_assert_student_owned`。

---

## P2-1：granular scope 咽喉強制 + flag + coverage 測試

### Task 1：`CONSENT_ENFORCEMENT_ENABLED` flag

**Files:** Modify `config/core.py`（或 `config/misc.py`，依既有 bool 設定歸屬）；Test `tests/test_consent_enforcement_flag.py`。

- [ ] **Step 1:** 寫測試：`settings.<...>.consent_enforcement_enabled` 預設 `False`；env `CONSENT_ENFORCEMENT_ENABLED=true` → `True`。
- [ ] **Step 2:** 跑測試確認 FAIL（屬性不存在）。
- [ ] **Step 3:** 在對應 Settings class 加 `consent_enforcement_enabled: bool = False`（pydantic-settings 自動讀同名 env，對齊既有欄位）。
- [ ] **Step 4:** 跑測試 PASS。
- [ ] **Step 5:** Commit `feat(consent): CONSENT_ENFORCEMENT_ENABLED dark-launch flag`。

### Task 2：`consent_check` 純函式（granular scope 查詢，含短 TTL 快取）

**Files:** Create `utils/consent_enforcement.py`；Test `tests/test_consent_check.py`。

- [ ] **Step 1:** 寫測試（用 in-memory/SQLite session + seed PolicyVersion + ParentConsentLog）：
  - `consent_check(session, user_id, "line_push")` 最新 log `consented=true` → `True`；`consented=false`（撤回）→ `False`；無 log → 預設（**granular 預設 False／需明確同意**，依 spec：撤回意願優先 → 無紀錄視為未同意）。
  - flag off（`CONSENT_ENFORCEMENT_ENABLED=false`）→ 一律 `True`（no-op）。
- [ ] **Step 2:** 跑測試確認 FAIL。
- [ ] **Step 3:** 實作：
```python
# utils/consent_enforcement.py
"""個資法 Phase 2：granular scope consent 查詢（單一咽喉用）。flag-gated。"""
from config import settings
from models.consent import ParentConsentLog

def _enabled() -> bool:
    # 依 Task 1 實際 settings 路徑
    return settings.<group>.consent_enforcement_enabled

def consent_check(session, user_id: int, scope: str) -> bool:
    """該 user 對 scope 的最新同意狀態。flag off → 一律放行。
    無紀錄 / 撤回 → False（granular 需明確同意，撤回意願優先）。"""
    if not _enabled():
        return True
    row = (
        session.query(ParentConsentLog.consented)
        .filter(ParentConsentLog.user_id == user_id, ParentConsentLog.scope == scope)
        .order_by(ParentConsentLog.consented_at.desc())
        .first()
    )
    return bool(row and row[0])
```
  - 短 TTL 快取（60s，`(user_id,scope)→(decision,ts)` module dict + `time.monotonic`）：point-of-use 用 fail-closed（查詢出錯回 False）；快取僅減 DB 壓力，非 fail-mode 主力（service_essential gate 的 degraded-on-read 在 P2-2）。
- [ ] **Step 4:** 跑測試 PASS。
- [ ] **Step 5:** Commit `feat(consent): consent_check granular 查詢 + 短 TTL 快取`。

### Task 3：`line_push` 咽喉強制（`_push_to_user`）

**Files:** Modify `services/line_service.py:440 _push_to_user`；Test `tests/test_line_push_consent_gate.py`。

- [ ] **Step 1:** 寫測試：mock 一個對「已撤回 line_push 的家長 LINE user」的 `push_to_user` → 應 **skip**（不呼叫實際 LINE API、回 False/記錄），同意者 → 正常送。先確認 `_push_to_user` 能取得 LINE user_id 並映射回本系統 user_id（查 `guardians`/`User.line_user_id` 對應；映射不到（如群組/非家長）→ 不套 consent gate，照送）。
- [ ] **Step 2:** 跑測試確認 FAIL。
- [ ] **Step 3:** 在 `_push_to_user` 送出前插入：映射 LINE user→本系統家長 user_id；若映射成功且 `not consent_check(session, uid, CONSENT_SCOPE_LINE_PUSH)` → skip（log info，不中斷業務）。query 出錯 fail-closed（skip）。非家長/映射不到 → 照送（不影響教師/群組通知）。
- [ ] **Step 4:** 跑測試 PASS + 抽跑既有 line_service / notification 測試零回歸。
- [ ] **Step 5:** Commit `feat(consent): line_push 在 _push_to_user 咽喉強制`。

### Task 4：`photo_publish` 咽喉強制（**先 locate**）

**Files:** locate 照片/作品廣播的單一發佈出口（候選：`services/contact_book_service.py`、`services/notification/_channels/`、portfolio 廣播路徑）；Test `tests/test_photo_publish_consent_gate.py`。

- [ ] **Step 1（locate）:** 追「把子女照片/作品推給家長」的實際 call chain，找單一發佈咽喉（grep `photo`/`portfolio`/`broadcast` + 讀實際路徑，**不靠 grep 推論**）。記錄咽喉函式於本 task。
- [ ] **Step 2:** 寫測試：撤回 photo_publish 的家長 → 其子女照片不入廣播；同意 → 正常。
- [ ] **Step 3:** 跑測試 FAIL。
- [ ] **Step 4:** 在咽喉插入 `consent_check(session, uid, CONSENT_SCOPE_PHOTO_PUBLISH)`（fail-closed）。
- [ ] **Step 5:** 跑測試 PASS + 既有零回歸。
- [ ] **Step 6:** Commit `feat(consent): photo_publish 廣播咽喉強制`。

### Task 5：`cross_border` 咽喉強制（**先 locate**）

**Files:** locate Supabase（US region）storage 上傳/signed-url 的單一出口（候選 `utils/supabase_storage.py`，方法名需實查）；Test `tests/test_cross_border_consent_gate.py`。

- [ ] **Step 1（locate）:** 追家庭 PII 物件上傳跨境的單一出口，記錄函式。
- [ ] **Step 2:** 寫測試：撤回 cross_border 的家庭 PII 物件 → 不上傳跨境（降級本地 `data/uploads_pending` 或阻擋，依既有 fallback 機制決定，於 task 註明）；同意 → 正常。
- [ ] **Step 3-5:** FAIL → 咽喉插 `consent_check(..., CONSENT_SCOPE_CROSS_BORDER_TRANSFER)`（fail-closed）→ PASS + 零回歸 → Commit。

### Task 6：chokepoint coverage 斷言測試（防回歸，最高價值）

**Files:** Test `tests/test_consent_chokepoint_coverage.py`。

- [ ] **Step 1:** 寫斷言測試：枚舉「對家長 user 的 LINE 發送 / 照片廣播 / 跨境上傳」entrypoint（用 AST 掃 `services/` 找呼叫 LINE SDK push / 廣播 / storage upload 的函式），斷言它們**都經咽喉**（`_push_to_user` / 已定 photo 咽喉 / storage 咽喉）或在明確 allow-list（教師/群組通知）。新增繞過咽喉的路徑 → fail。同 RA-HIGH-1 parity / RA-MED-4 「最弱 caller fail-open」防線。
- [ ] **Step 2:** 跑確認綠（現有路徑都經咽喉）。
- [ ] **Step 3:** Commit `test(consent): chokepoint coverage 斷言防新繞過路徑`。

---

## P2-2：service_essential gate + policy 升版重簽

### Task 7：當期 policy 解析 + 家長簽署判定

**Files:** Modify `utils/consent_enforcement.py`；Test `tests/test_current_policy_consent.py`。

- [ ] **Step 1:** 寫測試：`current_policy_version(session)` 回 `effective_at <= now` 最新 PolicyVersion；`has_signed_current_policy(session, user_id)` → 家長最新 `service_essential` 且 `consented=true` 的 `policy_version_id == 當期` → True；policy 升版（新增更新 effective PolicyVersion）後同一家長 → False（需重簽）。
- [ ] **Step 2-4:** FAIL → 實作兩函式 → PASS。
- [ ] **Step 5:** Commit `feat(consent): 當期 policy 解析 + 重簽判定`。

### Task 8：`require_current_consent` dependency（gate，degraded-on-read）

**Files:** Modify `utils/consent_enforcement.py`（加 FastAPI dependency）；掛到 `api/parent_portal/*` 資料讀寫端點（豁免：登入 / consent 簽署 / policy 查詢 / 公開）；Test `tests/test_consent_gate_dependency.py`。

- [ ] **Step 1:** 寫測試：flag on + 未簽當期 policy 的家長打受保護端點 → **403 + `X-Consent-Required` header（或 envelope code）**；簽當期 → 通過；flag off → 通過；**consent 查詢 DB 出錯（mock）→ 讀端點 degraded-on-read（60s 快取命中放行 / 無快取 fail-open 記 WARNING）、寫端點 fail-closed**。
- [ ] **Step 2:** 跑 FAIL。
- [ ] **Step 3:** 實作 dependency：flag off → no-op；on → `has_signed_current_policy`；未簽 raise 403 帶信號；查詢出錯依 method（GET=degraded/快取/fail-open+WARNING；非 GET=fail-closed）。
- [ ] **Step 4:** 掛 dependency 到家長 portal 資料端點（**逐一確認豁免清單**：`/auth/*`、`/me/consent*`、`/policies/current`、`data-export`？data-export 屬 §3 查閱權應放行）。
- [ ] **Step 5:** 跑 PASS + 既有 parent_portal 測試零回歸（flag 預設 off 故既有行為不變）。
- [ ] **Step 6:** Commit `feat(consent): require_current_consent gate + degraded-on-read fail-mode`。

---

## P2-3：DSR 執行（opt-out 即時 / admin queue / delete / correct）

### Task 9：`DSR_MANAGE` 權限

**Files:** Modify `utils/permissions.py`（`Permission` enum + `PERMISSION_LABELS` + 必要 `ROLE_TEMPLATES`）；Test `tests/test_dsr_manage_permission.py`。

- [ ] **Step 1:** 寫測試：`Permission.DSR_MANAGE.value == "DSR_MANAGE"`；在 `PERMISSION_LABELS`；admin wildcard 持有。
- [ ] **Step 2-4:** FAIL → 加 enum 值 + label +（指派給 admin/園長角色模板，依業務）→ PASS。
- [ ] **Step 5:** Commit `feat(dsr): DSR_MANAGE 權限`。（**跨端**：前端權限字串集合須在 P2-5 同步；plan 註記。）

### Task 10：opt-out 改即時（granular scope）

**Files:** Modify `api/parent_portal/dsr.py`（opt-out 端點）；Test `tests/test_opt_out_immediate.py`。

- [ ] **Step 1:** 寫測試：家長 `POST opt-out {scope: line_push}` → 直接寫 `ParentConsentLog consented=false`（不進 pending queue）→ `consent_check` 即回 False；`{scope: service_essential}` → **4xx 拒絕**（指引走 delete/withdrawal）；非法 scope → 422。
- [ ] **Step 2:** 跑 FAIL（現走 pending）。
- [ ] **Step 3:** 改 opt-out：granular scope → 寫 consent 撤回 log（即時）；`service_essential` → 4xx；非 `CONSENT_SCOPES` → 422。
- [ ] **Step 4:** 跑 PASS + 既有 dsr 測試零回歸。
- [ ] **Step 5:** Commit `feat(dsr): opt-out 改 granular scope 即時撤回（RA-MED-5）`。

### Task 11：admin DSR queue 端點（list / approve / reject）

**Files:** Create `api/admin/dsr_admin.py`（或既有 admin 套件）+ main.py 註冊；Test `tests/test_dsr_admin_queue.py`。

- [ ] **Step 1:** 寫測試：`GET /api/admin/dsr-requests?status=pending`（`DSR_MANAGE` gated，無權 403）回 pending list；`POST /{id}/reject {decision_note}` → status=rejected + decided_by/at + note + AuditLog；approve 在 Task 12/13 細化。ownership/合法性：approve 前重驗 `subject_entity` 與申請人關係。
- [ ] **Step 2-4:** FAIL → 實作 list + reject + approve 骨架（gated DSR_MANAGE）→ PASS。
- [ ] **Step 5:** Commit `feat(dsr): admin DSR queue list/reject 端點（RA-MED-5）`。

### Task 12：approve delete → 既有 lifecycle GC（保留法定資料）

**Files:** Modify `api/admin/dsr_admin.py`（approve delete 分支）；Test `tests/test_dsr_delete_execution.py`。

- [ ] **Step 1:** 寫測試：approve 一筆 `delete`（subject=student）→ 該 student 經 `student_lifecycle.transition` 轉終態（啟動既有 365d PII GC）；**出席/費用/薪資紀錄保留**（assert 未被刪）；法定保存期內可 reject 並記法源。**複用既有 GC，不建平行刪除**。
- [ ] **Step 2-4:** FAIL → approve delete 呼叫 `transition(...)`（對齊 RA-MED-6 修好的 lifecycle 路徑）→ PASS。
- [ ] **Step 5:** Commit `feat(dsr): approve delete 走既有 lifecycle GC（保留稅務/勞基法資料）`。

### Task 13：approve correct → 手動 + 稽核紀錄（不自動套用）

**Files:** Modify `api/admin/dsr_admin.py`（approve correct 分支）；Test `tests/test_dsr_correct_execution.py`。

- [ ] **Step 1:** 寫測試：approve 一筆 `correct` → status=approved + decided_by/at + decision_note；**系統不自動改 subject 任何欄位**（assert subject 欄位值不變）。request 為 §3 稽核紀錄；admin 用既有編輯工具手動更正。
- [ ] **Step 2-4:** FAIL → approve correct 僅標狀態 + note + AuditLog，不寫 subject → PASS。
- [ ] **Step 5:** Commit `feat(dsr): approve correct 為稽核紀錄、手動更正（不自動套用）`。

---

## 收尾

- [ ] 全 P2-1/2/3 focused 套件零回歸；`alembic heads` 單一（本 plan 無 migration，除非 Task 採微索引）。
- [ ] `SECURITY_AUDIT.md` 標 RA-MED-4 / RA-MED-5 已修（後端）+ 部署 gate（seed policy v1、`DSR_MANAGE` seed、`CONSENT_ENFORCEMENT_ENABLED` dark→刻意啟用 + LINE 廣播、`X-Consent-Required` 前端待接）。
- [ ] 向 user 報告 + **前端 P2-4/5 另寫 plan**（re-consent modal 接 `X-Consent-Required`、MeView 個資權利、admin DsrRequestsView/PolicyVersionsView、前端 `DSR_MANAGE` 權限字串同步）。

## Self-Review（plan vs spec 覆蓋）
- spec §3.1b granular 咽喉 → Task 3/4/5 ✅；§3.1c coverage → Task 6 ✅；§3.1e flag → Task 1 ✅
- §3.1a service_essential gate + §3.1d fail-mode → Task 7/8 ✅
- §3.2a opt-out 即時 → Task 10 ✅；§3.2b delete → Task 12 ✅；§3.2c correct → Task 13 ✅；§3.2d admin queue + DSR_MANAGE + ownership → Task 9/11 ✅
- §3.4 前端 → 明確劃為後續 plan ✅
- **locate-step（Task 4/5 咽喉）非 placeholder**：是「先追實際 call chain 定位、再套已定義的 `consent_check` pattern」的具體指令（咽喉位置須讀實際程式確認，符合本 workspace「不靠 grep 推論」紀律）。
