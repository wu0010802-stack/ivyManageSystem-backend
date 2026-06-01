# 權限系統 Row-Level Scoping Phase 2 設計（擴張 12 條 scope-aware 權限）

- 日期：2026-05-29
- 前置：[Phase 1 spec](2026-05-29-permission-row-level-scoping-design.md)（已 ship 至 worktree 等 merge）+ [Phase 1 plan](../plans/2026-05-29-permission-row-level-scoping-phase1.md)
- 影響面：ivy-backend `utils/portfolio_access.py` 不動、~18 個 router/service file 加 `code=` 參數、4 個 alembic seed migration（每 family 一個）
- 不影響：frontend（`getPermissionScope` 已是通用 helper）、wildcard `*` 行為、Phase 1 已 ship 的所有檔案

---

## Why

Phase 1 把 `STUDENTS_READ/WRITE/LIFECYCLE_WRITE` 3 條權限的 row-level scoping **以及對應 `portfolio_access` bridge 基礎建設的前 2 個 helper（`is_unrestricted` + `accessible_classroom_ids`）**做完。剩下 3 個高頻 helper（`assert_student_access` / `filter_student_ids_by_access` / `student_ids_in_scope`）尚未擴展 `code=` 參數 — Phase 2.2 第一個 task 即為補完此擴展，之後 Phase 2.3+ 可直接複用。

Phase 2-4 整體只需：

1. 補完 bridge helper（一次性，Phase 2.2 Task 1）
2. Seed `scope_options` for 剩下 12 條 scope-aware 權限
3. 把現有呼叫端從 `accessible_classroom_ids(session, user)` 改為 `accessible_classroom_ids(session, user, code=Permission.XXX.value)`
4. 對沒走 portfolio_access 的 router 做客製化（iep.py 移除自有 scope、portal/dismissal_calls.py 加 gate）

**實際業務價值：**

- admin 可在 Settings 建立「資深老師」「實習老師」自訂角色，分別配 `:all` 與 `:own_class`，現有 router 即時生效
- 跨園區 / 跨班雙導師等未來業務情境可表達（雖然目前無此需求）
- DB-driven 權限系統（2026-05-25 (b)）的 wire format 完整支援

---

## 涵蓋的 12 條權限（4 family）

| Family | 權限 code | 涵蓋 router 數 | 都走 portfolio_access? |
|---|---|---|---|
| **PORTFOLIO**（Phase 2.1） | `PORTFOLIO_READ` `PORTFOLIO_WRITE` `PORTFOLIO_PUBLISH` | 8 | ✅ 全部 |
| **HEALTH-MEDICATION**（Phase 2.2） | `STUDENTS_HEALTH_READ` `STUDENTS_HEALTH_WRITE` `STUDENTS_SPECIAL_NEEDS_READ` `STUDENTS_SPECIAL_NEEDS_WRITE` `STUDENTS_MEDICATION_ADMINISTER` | 5+ | 部分（`api/gov_moe/iep.py` 不走）|
| **DISMISSAL**（Phase 2.3） | `DISMISSAL_CALLS_READ` `DISMISSAL_CALLS_WRITE` | 1 | ❌ 自有 scope（待調查） |
| **CLASSROOM-ATTENDANCE**（Phase 2.4） | `CLASSROOMS_READ` `ATTENDANCE_READ` | 5 | ❌ 全部自有 scope（待調查） |

實際 router/file 清單見各 Phase 2.x plan。

---

## Phase 2 部署順序

每個 Phase 2.x 是獨立 PR，獨立 release，獨立 rollback：

```
[Phase 1 merge 完成 + alembic upgrade head]
    ↓
Phase 2.1 PORTFOLIO PR
    ↓ alembic upgrade head (permscope02_portfolio)
Phase 2.2 HEALTH-MEDICATION PR
    ↓ alembic upgrade head (permscope03_health_med)
Phase 2.3 DISMISSAL PR
    ↓ alembic upgrade head (permscope04_dismissal)
Phase 2.4a CLASSROOMS_READ PR（待 user 確認選項 D 後）
    ↓ alembic upgrade head (permscope05a_classrooms)
Phase 2.4b ATTENDANCE_READ PR（阻塞於業務決策 — 員工考勤 scope 語意）
    ↓ alembic upgrade head (permscope05b_attendance)
```

**順序選擇理由：**

1. **PORTFOLIO 先做** — 全 8 文件都走 `portfolio_access`，migration 模式單純，是最安全的「驗證 Phase 1 bridge 設計合理」的測試
2. **HEALTH 第二** — `api/student_health.py` 是核心醫療 PII router；第一個 task 補完 3 個 bridge helper 的 `code=` 擴展（Phase 2.3+ 可直接複用）；順帶修 `iep.py` lifecycle 過濾 latent bug
3. **DISMISSAL 第三** — portal endpoint 模式不同於 portfolio_access（用 portal-local helper），但改造模式簡單（4 endpoint 加 gate）
4. **CLASSROOM-ATTENDANCE 最後 — 業務空白阻塞** — 調查發現現行 codebase 完全無 row scope，需業主決策後才能寫 plan；建議拆 2.4a CLASSROOMS_READ（容易）+ 2.4b ATTENDANCE_READ（待業務決策）

**rollback 策略**：每 family 的 migration `downgrade()` 必須能單獨回退，不依賴後續 migration 存在。Phase 2.x 之間無強耦合。

---

## Phase 2.1 PORTFOLIO（細節）

完整 implementation plan：[2026-05-29-permission-row-level-scoping-phase2.1-portfolio.md](../plans/2026-05-29-permission-row-level-scoping-phase2.1-portfolio.md)

**重點：**

- Migration `permscope02_portfolio_seed`：seed `scope_options = ['own_class', 'all']` for 3 PORTFOLIO 權限；backfill teacher role permissions（bare `PORTFOLIO_READ` → `PORTFOLIO_READ:own_class`）；bump teacher token_version
- 8 file migration：以 `accessible_classroom_ids(session, user, code=...)` 取代 `accessible_classroom_ids(session, user)`，每個 endpoint 用其要求的權限 code
- 預估 ~40-50 個 call sites（每 file 2-7 個）
- 預估 ~10-15 個新 integration test 確認 admin（`*`）/ hr（bare `PORTFOLIO_READ` = `:all`）/ teacher（`:own_class`）三種情境

**已知不確定：**

- `api/portal/students.py` 同時引用 PORTFOLIO_READ 與 STUDENTS_READ — 需 per-endpoint 判定該用哪個 code
- `api/portal/contact_book*.py` `api/contact_book_ws.py` 是 contact book 路由不是 portfolio router 但用 PORTFOLIO_READ；需確認 PORTFOLIO_READ 對它們的 scope 語意正確

---

## Phase 2.2 HEALTH-MEDICATION

完整 implementation plan：[2026-05-29-permission-row-level-scoping-phase2.2-health-medication.md](../plans/2026-05-29-permission-row-level-scoping-phase2.2-health-medication.md)

**調查結果（2026-05-29）：**

| File | Scope 模式 | Phase 2.2 修改 |
|------|-----------|---------------|
| `api/student_health.py`（756 行） | ✅ 已走 `portfolio_access` bridge（8 處 `assert_student_access` / `student_ids_in_scope`） | 加 `code=` 至 8 處 |
| `services/dashboard_query_service.py` | ✅ 已走 bridge（L333 `student_ids_in_scope`） | 加 `code=` 至 1 處 |
| `api/portal/medications.py`（205 行） | ❌ 自有 `_get_teacher_classroom_ids`（L54-70）+ 缺 lifecycle 終態過濾 | 改 `accessible_classroom_ids(code=)` + 補 lifecycle 過濾 |
| `api/portal/class_hub.py` | ❓ 委派下游 service — 需 pre-flight 確認 | TBD（Task 6 verify） |
| **`api/gov_moe/iep.py`**（409 行） | ❌ 自有 `_student_ids_in_scope` (L76-105) + **缺 lifecycle 過濾**（Phase 1 latent bug） | delegate 至 portfolio_access — 順帶修 lifecycle bug |

**Latent bug 發現**：`iep.py:_student_ids_in_scope` 用 `Employee.classroom_id` 而非 `Classroom` 三角 OR（與全系統不一致 — teacher 換班時可能 stale），且未過濾 lifecycle 終態學生（已退學/畢業學生的 IEP teacher 仍可看到 — audit 2026-05-07 P0 #5 漏網）。Phase 2.2 delegate 後一次修完。

**業務決策點（Task 7 必先 user 確認）**：iep.py 既有「主任 / 園長走 None（全放行）」hard-code，與 portfolio_access role-based 不同。是否改用自訂角色 + `:all` scope 表達（推薦），還是維持 hard-code（保守）。

**Migration `permscope03_health_med`**：5 條權限 seed（仿 permscope01 結構）+ backfill teacher role bare → :own_class

---

## Phase 2.3 DISMISSAL

完整 implementation plan：[2026-05-29-permission-row-level-scoping-phase2.3-dismissal.md](../plans/2026-05-29-permission-row-level-scoping-phase2.3-dismissal.md)

**調查結果（2026-05-29）：**

| File | Scope 模式 | Phase 2.3 修改 |
|------|-----------|---------------|
| `api/portal/dismissal_calls.py`（250 行，4 endpoint） | ❌ 用 portal-local `_shared.py:_get_teacher_classroom_ids` — **非** `utils/portfolio_access` | 加 `is_unrestricted(code=)` gate（不重構 helper） |
| `api/portal/_shared.py:_get_teacher_classroom_ids` | helper 不動 — 與 portfolio_access 三角 OR 等價 | 保留 |
| `api/dismissal_calls.py`（admin） | 走 `require_staff_permission` 無 row filter — 與 Phase 2 無關 | 不動 |

**4 endpoint**：`GET /dismissal-calls`（L65 READ） / `GET /dismissal-calls/pending-count`（L98 READ） / `POST .../acknowledge`（L210 WRITE） / `POST .../complete`（L231 WRITE）

**Migration `permscope04_dismissal`**：2 條權限 seed + backfill teacher role

**Phase 2.3 與 2.2 無強耦合可平行 ship**，但 migration `down_revision` 需在 permscope03 之後（避免 dual head）。

---

## Phase 2.4 CLASSROOM-ATTENDANCE — **業務空白，待 user 決策**

詳細決策清單：[2026-05-29-permission-row-level-scoping-phase2.4-classroom-attendance-questions.md](../plans/2026-05-29-permission-row-level-scoping-phase2.4-classroom-attendance-questions.md)

**調查結果（2026-05-29）— 與原 outline 假設嚴重不符：**

| File | 行數 | 既有 row scope? |
|------|------|---------------|
| `api/classrooms.py` | 1279 | **❌ 完全無** — 所有 endpoint 回全校；三角 OR 只用於 projection（mask health 欄位）不是 row filter |
| `api/attendance/records.py` | 522 | **❌ 完全無** — 全員考勤對所有 ATTENDANCE_READ 持有人可見 |
| `api/attendance/anomalies.py` | 402 | **❌ 完全無**（同上） |
| `api/attendance/reports.py` | 564 | **❌ 完全無**（同上） |
| `api/exports.py` | 1291 | partial — 個人月報有 `enforce_self_or_full_salary` self-guard，非 row scope |

**關鍵業務空白**：員工考勤的「自班 scope」目前**沒有定義**。`ATTENDANCE_READ:own_class` 對員工考勤的合理詮釋至少 4 種（同班同事 / 同園 / 同部門 / 都不對）— 需與業主決策。

**推薦路徑（選項 D Hybrid）**：
- Phase 2.4a 先做 `CLASSROOMS_READ`（已有 `accessible_classroom_ids` helper 可重用；業務語意明確）
- Phase 2.4b 留待業主確認員工考勤 scope 語意後再寫 plan

**進度阻塞**：Phase 2.4 plan 寫不出非 placeholder 版本 — questions doc 已寫，待 user 在四個選項中選定後產出 2.4a / 2.4b 完整 TDD plan。

---

## 風險與不確定性

1. **行為穩定性**：每 family 的 migration 都會 backfill teacher role 的對應 permissions（bare → `:own_class`）+ bump token_version。teacher 用戶每 Phase 2.x merge 後都會被踢出重 login。**緩解**：Phase 2.x 之間隔 1-2 週，避免短時間多次踢出。
2. **per-endpoint code= 對應錯誤**：endpoint 可能 require_permission(`PORTFOLIO_READ`) 但 scope check 寫 `code=Permission.STUDENTS_READ.value`，導致錯誤 scope 套用。**緩解**：每個 migration 必寫 integration test 證明對應的 perm 確實控制 scope。
3. **Phase 2.4 ATTENDANCE_READ 業務空白**：員工考勤目前**完全沒有** row scope，業主對「自班」期待 4 種詮釋皆有可能。**緩解**：拆 Phase 2.4a + 2.4b；2.4b 阻塞於業務決策。
4. **iep.py 業務行為變更**：Phase 2.2 Task 7 移除「主任/園長 hard-code 全放行」改用 `:all` scope 表達，業主需重新配置主任角色（Task 7 Step 1 與 user 確認）。
5. **自訂角色未滿足需求**：Phase 2 enable `:all` `:own_class` 表達能力，但目前無業主需求建「資深老師 :all」這類角色。可能 Phase 2 ship 後實際 customisation 用量 = 0，淪為「能力 over capability」。**緩解**：Phase 2 是 Phase 1 的「補完性」工作，主軸是 Phase 1 已 ship 的權限模型一致性 + 順帶修 `iep.py` lifecycle latent bug。
6. **iep.py latent lifecycle bug**：current 寫法不過濾 `_TEACHER_BLOCKED_LIFECYCLE`（graduated/withdrawn/transferred），teacher 可看到已轉出/畢業學生的 IEP — Phase 2.2 delegate 至 portfolio_access 後自動修復。

---

## Out of scope

- Phase 5 以後：自訂個別班指派（user_classroom_scope 中介表）、Policy engine
- `:all` 以外的新 scope value（如 `:own_grade` 跨年級）— 系統無對應業務概念
- Parent 端（走 LIFF 獨立路徑，無此 scope 需求）
- Frontend 改動（`getPermissionScope` 已是通用 helper，無 family-specific 邏輯）
