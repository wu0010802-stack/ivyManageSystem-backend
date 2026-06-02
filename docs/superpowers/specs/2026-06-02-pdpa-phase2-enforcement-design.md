# 個資法 Phase 2 — Consent 強制 + DSR 執行設計

**日期**：2026-06-02
**狀態**：Approved（待 user review → writing-plans）
**範圍**：ivy-backend（FastAPI）+ ivy-frontend（Vue 3，含家長 LIFF + admin + 員工 portal）
**關係**：完成 `docs/superpowers/specs/2026-05-28-consent-dsr-rights-design.md` 的 **Phase 2**（enforcement + 執行層）。Phase 1（models + 記錄端點 + data-export）已落地。
**對應 finding**：`SECURITY_AUDIT.md` §2026-06-02 re-audit 的 **RA-MED-4**（consent 完全 fail-open）+ **RA-MED-5**（DSR 無 admin 決議端點，opt-out/delete/correct 永不執行）。

> ⚠️ 本文是 **spec（設計）**，非實作。實作須另經 writing-plans 產出 plan 並由 user 明確同意（原估 ~2 週 / 多 PR）。

---

## 1. 背景

2026-05-28 P0c sprint 落地了 consent / DSR 的**資料層與記錄層**（`ParentConsentLog`、`PolicyVersion`、`DsrRequest` 三表 + 家長記錄/查詢端點 + data-export），但 **enforcement / 執行層 deferred**。2026-06-02 re-audit 確認此缺口：

- **RA-MED-4**：consent 完全 fail-open——全 codebase 無任何讀寫被 consent / policy 版本擋下，`main.py` middleware stack 無 consent gate。consent 只是一本帳本。
- **RA-MED-5**：DSR 無 admin 決議端點——`dsr_requests` 永遠停 pending，opt-out 不停止處理、delete 不刪、correct 不更正。

本 Phase 2 讓既有記錄**真正生效**：consent 後端強制 + DSR 可被 admin 決議並執行。

---

## 2. 目標 / 非目標

### 目標
1. **Consent 後端強制（混合）**：`service_essential` 作為家長 portal gate（檢當期 policy 已簽）；`line_push`/`photo_publish`/`cross_border` 在**單一咽喉點** point-of-use 強制。
2. **Policy 升版重簽**：policy 升版後，家長當期 consent 失效 → 強制重簽。
3. **DSR 可執行**：opt-out 即時自助（granular scope）；delete / correct 進 admin 決議 queue。
4. **Dark-launch**：`CONSENT_ENFORCEMENT_ENABLED` flag，deploy dark，刻意啟用。
5. 前端：LIFF re-consent modal、MeView 個資權利、admin DSR queue + policy 管理。

### 非目標
1. **schema 大改**：三表已存在；本 phase 僅可能加 1 個小欄/索引（見 §3.5），不重建。
2. **既有家長追溯同意**：法律不允許；policy 升版走重簽。
3. **員工 DSR delete**：勞基法 §80 員工資料保存 5 年，v1 只做 export（沿用 Phase 1 非目標）。
4. **撤回的歷史 cascade**：撤 `photo_publish` 只停未來廣播，不硬刪已發佈/已下載照片。
5. **自動套用 correct new_value**：correct 由 admin 手動更正，request 僅為 §3 稽核紀錄（不建套用引擎）。

---

## 3. 設計

### 3.1 Consent 強制（混合）

#### (a) `service_essential` — gate（契約基礎，檢當期 policy 簽署，**不可撤回**）
- 法律基礎是**就學契約**而非同意；在學兒童不可「撤回 service_essential」（會自鎖 portal，矛盾）。故 gate 檢查的是「家長**已簽當期生效 policy 版本**」，而非「可否使用服務」。
- 實作：家長 portal 端點的後端 dependency `require_current_consent`（掛在 `api/parent_portal/*` 的資料讀寫端點；公開/登入/consent 簽署/policy 查詢端點豁免）。
- 判定：家長最新一筆 `service_essential` `ParentConsentLog`（`consented=true`）的 `policy_version_id` == 當期生效 `PolicyVersion`（`effective_at <= now` 最新者）→ 通過；否則 → **403 + `X-Consent-Required` 信號 / envelope code**，前端 LIFF 攔截彈 re-consent modal。
- `service_essential` 的「終止服務」需求走 DSR delete/withdrawal 審查，不走 opt-out。

#### (b) granular scope — point-of-use **單一咽喉點**（RA-MED-4 最高價值）
強制放在**每個 scope 的唯一發送咽喉**，非散落各 caller（散落=最弱 caller 靜默 fail-open，正是要修的 bug 類）：

| Scope | 咽喉點 | 行為 |
|-------|--------|------|
| `line_push` | `services/line_service.LineService` 的單一 push 出口（push_text / push_flex / 對家長 user 的推播都收斂於此） | 對家長 user 發送前查 line_push consent；未同意 → skip + 記錄（不拋錯中斷業務） |
| `photo_publish` | 照片/作品廣播 service 的單一發佈出口 | 未同意 → 該家長子女照片不納入廣播 |
| `cross_border` | storage 層（Supabase US region 上傳/signed URL）的單一出口 | 未同意 → 該家庭 PII 物件不上傳跨境（評估降級到本地或阻擋） |

實作為純函式 `consent_check(user_id, scope) -> bool`（查最新 consent log，含短 TTL 快取見 §4）。

#### (c) Coverage 斷言測試（RA-MED-4 防回歸，同 RA-HIGH-1 parity 模式）
一個測試枚舉「所有對家長 user 的 LINE 發送 / 照片廣播 / 跨境上傳 entrypoint」，斷言它們**都經咽喉**；新增繞過咽喉的發送路徑即 fail。防「第 6 個 caller 靜默 fail-open」。

#### (d) Gate fail-mode（RA-MED-3/RA-MED-2 教訓：明定不留 silent default）
- `service_essential` gate 的 consent 查詢出錯時 → **degraded-on-read：用短 TTL（如 60s）快取的 consent 決策**；無快取且 DB 失敗時，**fail-open 並記 WARNING**（避免一次 DB 抖動鎖死全體家長），但對**寫入端點 fail-closed**（寧可擋寫不可漏同意寫入）。
- granular point-of-use 查詢出錯時 → **fail-closed**（寧可漏發一則通知/一張照片，不可違反撤回意願）。

#### (e) Dark-launch flag（RA-MED-4 部署安全，TRUSTED_PROXY_IPS 教訓）
- `CONSENT_ENFORCEMENT_ENABLED`（env，預設 `false`）。為 false 時 gate / point-of-use 全 no-op（維持現狀，純記錄）。
- 啟用流程（**進部署 gate，非 merge**）：deploy dark → seed `PolicyVersion` v1 + 上傳政策文件 → LINE 廣播預告 → 刻意設 `true` → 監測重簽率。

### 3.2 DSR 執行

#### (a) opt-out — 即時自助（僅 granular scope）
- `POST /api/parent/me/opt-out`（或沿用既有路徑）對 `photo_publish`/`line_push`/`cross_border`：直接寫 `ParentConsentLog consented=false` → point-of-use 立即停止該 scope。**不進 admin queue**（撤回 consent 是家長權利，不需核准）。
- `service_essential` **拒絕** opt-out（回 4xx 並指引走 delete/withdrawal 審查）。

#### (b) delete — admin queue 審查 → 既有 lifecycle GC
- `delete` request 進 queue。admin approve → 走**既有 `student_lifecycle` 終態 transition → 既有 365d PII retention GC**（複用 RA-MED-6/7 已審路徑，**不建平行刪除**）。
- **抹除**：Guardian PII（phone/email/name/user_id，由既有 GC）。**保留**：出席 / 費用 / 薪資紀錄（稅務 / 勞基法 7 年法定保存）。
- 法定保存期內或資料涉未結帳務 → admin 可 **reject** 並於 `decision_note` 註明法源（個資法 §11 但書）。
- 家長 submit 後收 LINE 通知預期處理時間（沿用原 spec）。

#### (c) correct — admin queue，手動更正
- approve → admin 用**既有學生/家長編輯工具**手動更正 + 填 `decision_note`。系統**不自動套用** `new_value`（避免套用引擎 / 欄位白名單 / IDOR 風險）。request 為 §3 稽核紀錄。

#### (d) admin queue 端點 + 權限
- `GET /api/admin/dsr-requests`（list，filter status）、`POST /api/admin/dsr-requests/{id}/approve`、`/reject`。
- 新權限 `DSR_MANAGE`（後端 `Permission` enum + `PERMISSION_LABELS` + 必要 `ROLE_TEMPLATES` + 前端權限字串集合**同步**；遵守 workspace 跨端陷阱 #1）。
- 執行時**重驗 ownership**（submit 端綁定已於 RA-MED-5-IDOR 修；admin 決議端點亦核 `subject_entity` 與申請人關係，且 approve delete/opt-out 自動化動作前再次確認 subject 合法）。
- approve/reject 寫 `decided_at`/`decided_by`/`decision_note` + AuditLog。

### 3.3 Endpoints 總覽（既有 vs 新增）
- 既有（Phase 1）：`GET /me/consents`、`POST /me/consent`、`POST /me/delete-request`、`POST /me/correct-request`、`POST /me/opt-out`（現走 pending）、`GET /me/data-export`、`GET /portal/my-data-export`。
- **新增 / 改動**：`require_current_consent` dependency（gate）；`consent_check` 咽喉強制；opt-out 改即時（granular）；`GET /api/admin/dsr-requests` + approve/reject；`GET /api/policies/current`（公開，若 Phase 1 未含）。

### 3.4 前端（phase 後段）
- 家長 LIFF：`LoginView` / 攔截器處理 `X-Consent-Required` → re-consent modal（簽當期 policy v）；`MeView` 個資權利區（下載 / 刪除申請 / 更正申請 / 同意紀錄 + 撤回 granular scope，service_essential 不顯示撤回）。
- Admin：`DsrRequestsView`（queue approve/reject + decision_note）、`PolicyVersionsView`（上傳新版 → 觸發重簽）。
- 權限：admin 端 gated by `DSR_MANAGE`（前端權限字串同步）。

### 3.5 資料模型
原則上**不改 schema**。可選微調（plan 階段定）：consent 查詢效能若不足，加複合索引（`ParentConsentLog` 已有 `ix_pcl_user_scope_time`，多半夠）。若需記錄「政策 gate 觸發重簽」事件可複用既有 log，不新增表。

---

## 4. 錯誤處理 / fail-mode（彙整，明定不留 silent default）
- service_essential gate：degraded-on-read（短 TTL 快取）+ 讀路徑 DB 全失敗 fail-open 記 WARNING / 寫路徑 fail-closed。
- granular point-of-use：fail-closed（漏發 > 違反撤回）。
- flag off：全 no-op。
- opt-out 對 service_essential：4xx 明確拒絕。
- admin approve delete 走既有 GC 的既有錯誤處理，不另建。

---

## 5. 測試策略
- **chokepoint coverage 斷言**（§3.1c）：核心防回歸測試。
- consent gate：未簽當期 policy → 403 + 信號；簽當期 → 通過；policy 升版 → 重簽偵測；flag off → 不擋。
- gate fail-mode：consent 查詢出錯 → 讀 degraded（快取）/ 寫 fail-closed（mock DB error）。
- point-of-use：未同意 line_push → push 被 skip；未同意 photo_publish → 不入廣播；同意 → 正常。
- opt-out：granular 即時生效（撤回後 point-of-use 立刻擋）；service_essential opt-out → 4xx。
- DSR admin：approve delete → lifecycle 終態 + GC 排程（保留項不動）；approve correct → 稽核紀錄 + 不自動改欄位；reject → status + note；`DSR_MANAGE` authz（無權 403）；ownership 重驗。
- 前端：re-consent modal、MeView DSR 操作、admin queue、policy 上傳觸發重簽。
- 既有套件零回歸；新增測試全綠。

---

## 6. Rollout / 部署 gate
分階段 PR（見 §7）。**部署 gate**（push/deploy 時）：
1. prod 上傳政策文件 + seed `PolicyVersion` v1。
2. `DSR_MANAGE` 權限 seed（指派給負責的 admin/園長角色）。
3. `CONSENT_ENFORCEMENT_ENABLED` 先 false（dark）→ 確認重簽流程 + 咽喉強制無誤 → 配合 LINE 廣播預告 → 刻意設 true。
4. 監測重簽率（< 80% 發提醒），watch 漏發通知/照片的 fail-closed 誤擋。

## 7. Phase / PR 邊界（供 writing-plans 拆解）
- **P2-1（BE consent 強制）**：`consent_check` 咽喉（line_push/photo_publish/cross_border）+ coverage 測試 + `CONSENT_ENFORCEMENT_ENABLED` flag。
- **P2-2（BE service_essential gate）**：`require_current_consent` dependency + policy-bump 重簽 + fail-mode + 快取。
- **P2-3（BE DSR 執行）**：opt-out 即時 + admin queue 端點 + `DSR_MANAGE` + delete→lifecycle GC + correct 手動 + ownership 重驗。
- **P2-4（FE 家長）**：re-consent modal + MeView 個資權利。
- **P2-5（FE admin）**：DsrRequestsView + PolicyVersionsView。
每 PR 各自 CI 綠、前後端契約同步（OpenAPI codegen + 權限字串 + PII denylist）。

## 8. Risk & trade-offs
- 啟用 gate = 真實 backend lockout（dark-launch flag + LINE 廣播協調緩解）。
- point-of-use fail-closed 可能誤擋通知/照片（監測 + 快取緩解）。
- service_essential 不可撤回 = 刻意（契約基礎）；對外溝通須清楚「停止服務 = 退園/刪除流程」。
- correct 手動 = admin 工作量，但避開自動套用的安全風險（已取捨）。

## 9. 驗收條件
1. flag on + 家長未簽當期 policy → portal 資料端點 403 + 前端彈 re-consent；簽後通過。
2. policy 升版 → 既有家長下次進 portal 被攔重簽。
3. 撤回 line_push → 該家長不再收 LINE 推播（point-of-use 即時生效）；撤回 photo_publish → 子女照片不入新廣播。
4. service_essential opt-out → 4xx 拒絕。
5. admin approve delete → 學生 lifecycle 終態 + 365d GC 啟動，出席/費用/薪資保留；reject → note 記法源。
6. admin approve correct → §3 稽核紀錄 + decision_note，欄位未被系統自動改。
7. `DSR_MANAGE` 無權者打 admin queue → 403；前端對應隱藏。
8. chokepoint coverage 測試存在且綠；flag off 時全 no-op；既有套件零回歸。
