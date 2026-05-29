# 權限系統 Row-Level Scoping Phase 2 設計（擴張 12 條 scope-aware 權限）

- 日期：2026-05-29
- 前置：[Phase 1 spec](2026-05-29-permission-row-level-scoping-design.md)（已 ship 至 worktree 等 merge）+ [Phase 1 plan](../plans/2026-05-29-permission-row-level-scoping-phase1.md)
- 影響面：ivy-backend `utils/portfolio_access.py` 不動、~18 個 router/service file 加 `code=` 參數、4 個 alembic seed migration（每 family 一個）
- 不影響：frontend（`getPermissionScope` 已是通用 helper）、wildcard `*` 行為、Phase 1 已 ship 的所有檔案

---

## Why

Phase 1 把 `STUDENTS_READ/WRITE/LIFECYCLE_WRITE` 3 條權限的 row-level scoping **以及對應 `portfolio_access` bridge 基礎建設**做完。Bridge（`is_unrestricted(user, code=)` + `accessible_classroom_ids(session, user, code=)`）可接受任何 permission code 字串，所以 Phase 2-4 不需新增基礎建設，只需：

1. Seed `scope_options` for 剩下 12 條 scope-aware 權限
2. 把現有呼叫端從 `accessible_classroom_ids(session, user)` 改為 `accessible_classroom_ids(session, user, code=Permission.XXX.value)`
3. 對少數沒有走 portfolio_access 的 router 做客製化處理

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
Phase 2.4 CLASSROOM-ATTENDANCE PR
    ↓ alembic upgrade head (permscope05_classroom_attn)
```

**順序選擇理由：**

1. **PORTFOLIO 先做** — 全 8 文件都走 `portfolio_access`，migration 模式單純，是最安全的「驗證 Phase 1 bridge 設計合理」的測試
2. **HEALTH 第二** — `api/student_health.py` 是核心醫療 PII router，做完後 80% 醫療資料 scope 完備（除 gov_moe/iep.py）
3. **DISMISSAL 第三** — 只 1 個 file，但有自有 scope 邏輯需研究後才能寫 plan
4. **CLASSROOM-ATTENDANCE 最後** — 5 files 都自有 scope，是技術上最複雜的 family；前 3 個 family 做完已驗證 pattern，最後一個 family 風險最低

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

## Phase 2.2 HEALTH-MEDICATION（outline）

**File 清單：**

- `api/student_health.py`（755 行，9 處 portfolio_access calls）— 加 `code=`
- `api/students.py`（部分 endpoint 用 HEALTH_READ；Task 7 已 migrate list endpoint）— 加 `code=`
- `services/dashboard_query_service.py`（3 處 calls）— 加 `code=`
- `api/portal/class_hub.py` `api/portal/medications.py`（用 HEALTH_READ + MEDICATION_ADMINISTER；計數待 confirm）
- `api/gov_moe/iep.py`（408 行，**未使用** portfolio_access；自有 SPECIAL_NEEDS 邏輯需調查）

**Migration `permscope03_health_med_seed`**：5 條權限 seed

**研究項目（寫 plan 前必先回答）：**

1. `api/gov_moe/iep.py` 目前如何 scope IEP 文件存取？是否有 teacher restriction？
2. `api/portal/medications.py` 投藥 log 是否 hard-code 自班限制？

---

## Phase 2.3 DISMISSAL（outline）

**File：** `api/portal/dismissal_calls.py`（單一 portal endpoint）

**Migration `permscope04_dismissal_seed`**：`DISMISSAL_CALLS_READ` `DISMISSAL_CALLS_WRITE` seed

**研究項目：**

- portal endpoint 既有 scope 邏輯（用 `_get_employee` + 自寫 OR or 用 helper？）
- 是否有對應 admin endpoint（搜尋 `DISMISSAL_CALLS_*` admin router）

---

## Phase 2.4 CLASSROOM-ATTENDANCE（outline）

**Files：**

- `api/classrooms.py`（已知有自己的 head_teacher_id 三角 OR）
- `api/attendance/anomalies.py` `api/attendance/records.py` `api/attendance/reports.py` `api/exports.py`

**Migration `permscope05_classroom_attn_seed`**：`CLASSROOMS_READ` `ATTENDANCE_READ` seed

**研究項目（寫 plan 前必先回答）：**

1. `api/classrooms.py` 三角 OR 是 row filter 還是 projection（display teacher name）？
2. `ATTENDANCE_READ` 是員工考勤還是學生考勤？scope 語意「同班同事」對員工考勤是否合理？
3. `api/exports.py` 是匯出整體資料 — scope 怎麼套用？

---

## 風險與不確定性

1. **行為穩定性**：每 family 的 migration 都會 backfill teacher role 的對應 permissions（bare → `:own_class`）+ bump token_version。teacher 用戶每 Phase 2.x merge 後都會被踢出重 login。**緩解**：Phase 2.x 之間隔 1-2 週，避免短時間多次踢出。
2. **per-endpoint code= 對應錯誤**：endpoint 可能 require_permission(`PORTFOLIO_READ`) 但 scope check 寫 `code=Permission.STUDENTS_READ.value`，導致錯誤 scope 套用。**緩解**：每個 migration 必寫 integration test 證明對應的 perm 確實控制 scope。
3. **Phase 2.4 ATTENDANCE_READ 語意**：員工考勤的「自班」scope 不見得對應「同班同事」（業主可能期待「同園區同事」或「同部門」）。**緩解**：Phase 2.4 寫 plan 前先與 user 確認需求。
4. **自訂角色未滿足需求**：Phase 2 enable `:all` `:own_class` 表達能力，但目前無業主需求建「資深老師 :all」這類角色。可能 Phase 2 ship 後實際 customisation 用量 = 0，淪為「能力 over capability」。**緩解**：Phase 2 是 Phase 1 的「補完性」工作，主軸是 Phase 1 已 ship 的權限模型一致性。
5. **`api/gov_moe/iep.py` 為何不用 portfolio_access**：可能設計考量是 IEP 的 SPECIAL_NEEDS 是 sensitive 文件，scope 規則與普通 student access 不同（例如只有指定老師可看，而非全班三角 teacher）。Phase 2.2 寫 plan 前必詳查。

---

## Out of scope

- Phase 5 以後：自訂個別班指派（user_classroom_scope 中介表）、Policy engine
- `:all` 以外的新 scope value（如 `:own_grade` 跨年級）— 系統無對應業務概念
- Parent 端（走 LIFF 獨立路徑，無此 scope 需求）
- Frontend 改動（`getPermissionScope` 已是通用 helper，無 family-specific 邏輯）
