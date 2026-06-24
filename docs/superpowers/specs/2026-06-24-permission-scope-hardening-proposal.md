# 權限 Scope 縱深防禦強化提案（#2 / #5 / U12 / U13）

> 狀態：**提案（proposal）— 待業主/設計裁定，尚未實作**
> 來源：2026-06-24 qa-loop 全域稽核 P2/P3 authz finding
> 性質：動到 `resolve_grant` 的 `bare = all` 語意與 ~30 處 scope-blind gate、
> 共 ~92 處 scope-aware 授權 caller。**屬高風險授權核心重構，不可倉促改碼**
> （見記憶 `feedback_scope_aware_authz_bare_all_semantics`）。本文先盤點、提選項、
> 標記需業主決策處，待裁定後再開 plan + TDD 落地。

---

## 0. 背景：scope-aware 授權的兩層語意

- `User.permission_names`（`ARRAY(Text)`）以字串集合表達權限；scope-aware code
  可帶後綴 `:own_class` / `:all`（`SCOPE_AWARE_CODES`，permscope01-04 起）。
- **bare code（無後綴）= 全域 scope（`all`）**：`resolve_grant` 把 bare `STUDENTS_READ`
  視同 `STUDENTS_READ:all`（向後相容既有全園角色）。
- gate（`require_staff_permission` / `require_permission`）是 **scope-blind**：
  對 `'STUDENTS_READ:own_class'` 仍回 True，只靠 role 字串擋 teacher/parent。
  scope 收斂須由端點內顯式呼叫 `is_unrestricted(code=...)` /
  `accessible_classroom_ids(code=...)` / `assert_all_scope(code=...)` 完成。

這個「gate 放行、scope 由端點自行收斂」的設計，使得**端點若忘了傳 `code=`**，
就會落回 role-based 判斷而非 grant-based，形成 fail-open 缺口（#2）。

---

## 1. Finding 摘要與機制

### #2（P2）scope-blind gate → supervisor `:own_class` override 被靜默忽略放行全園
- **點**：`api/student_attendance.py:283/284、471/472`、`api/student_leaves.py:53`
  等呼叫 `is_unrestricted(current_user)` / `accessible_classroom_ids(session, current_user)`
  **未傳 `code=`** → 落入 `utils/portfolio_access.py:52` 的 role-based 分支
  （`role in {admin,hr,supervisor}` → True 全放行）。
- **機制**：若管理者把使用者 role 維持 `supervisor` 卻把 `permission_names` 顯式
  覆寫成 `['STUDENTS_READ:own_class']`（DB-driven 角色允許 per-user override），
  其 `:own_class` scope 會被完全忽略，回全園出勤/請假。`code=` 參數正是
  2026-06 為修此類 fail-open 而加（`assert_all_scope` docstring `portfolio_access.py:71-89`），
  但這些端點未採用。
- **量化盤點（2026-06-24）**：`is_unrestricted(` 全 repo 58 處呼叫，**僅 28 帶 `code=`
  → 30 處 scope-blind**；`accessible_classroom_ids(` 17 處呼叫、**僅 7 帶 `code=`
  → 10 處 scope-blind**。

### #5（P3）`resolve_user_permissions` DB-first 分支零正規化 + 提權回歸測試覆蓋盲區
- **點**：`utils/permissions.py:780-788`。有傳 session 且 DB role 非空時
  `return list(role.permissions)`，**無 `validate_permission_names`、無 bare→scoped 調和**。
- **機制**：login（`api/auth.py:685`）、refresh（`api/auth.py:835`）皆傳 session，故
  此 DB 分支（而非 in-code `ROLE_TEMPLATES` fallback）才是 NULL-perm 帳號的 runtime
  有效路徑。2026-06-04 提權回歸測試（`tests/test_authz_escalation_fix_2026_06_04.py`）
  呼叫 `resolve_user_permissions(_FakeUser('teacher', None))` **不帶 session**，只斷言
  in-code fallback，**從未運行 prod 實際走的 DB-first 分支**。DB roles 任何漂移即直接
  流入 JWT，無守衛、無回歸覆蓋。

### U12（P3）add-only backfill 留下 bare+scoped 重複 → NULL-perm teacher 靜默全園
- **點**：`utils/permission_backfill.py:53` `sync_core_role_permissions()` 為 add-only、
  字串精確比對。
- **機制**：若 permscope01-04 backfill 被跳過（prod create_all + stamp head 缺口，
  見記憶 `reference_prod_create_all_stamp_skips_infra`），DB teacher role 保留 bare code，
  permbf01 再 append 出同時持 `STUDENTS_READ` 與 `STUDENTS_READ:own_class` 的角色，
  `resolve_grant` 取最寬 scope（bare=all）→ 全園。

### U13（P3）admin 編輯角色接受 bare scope-aware code → footgun 提權 NULL-perm 成員
- **點**：`utils/permissions.py` `validate_permission_names`（L748-759）。只在 base 不在
  `Permission.__members__`、或有 `:` 後綴但 scope ∉ {own_class,all} 時拒；**bare**
  scope-aware code 無條件通過。
- **機制**：`update_role`/`create_role` 只 gate on `validate_permission_names` +
  `_assert_can_grant`，admin 編輯 DB-driven teacher/custom 角色可存 bare `STUDENTS_READ`
  卻意圖 own class；`resolve_grant` 視 bare=all，該角色每個 NULL-perm 成員經 DB-first
  resolve 被靜默授全園。

**四者同根**：`bare = all` 語意 + DB-first resolve 無正規化 + gate scope-blind，
任一處 bare/scoped 漂移都會沿「放寬」方向流到 runtime。

---

## 2. 為何不直接修（風險）

1. **blast radius 大**：#2 要對 30+ 處 `is_unrestricted` / 10 處 `accessible_classroom_ids`
   逐一決定該傳哪個 `code=`，且每處的「全園 vs 自班」語意需逐端點確認（出勤、請假、
   今日用藥、彙總匯出語意不同）。共約 92 處 scope-aware caller 受波及。
2. **`bare = all` 是雙面語意**：bare 同時被「既有全園角色」與「漂移的 own_class 角色」
   使用。直接把 bare 改判為「受限」會打到所有合法全園角色（admin/hr/supervisor 的
   bare grant）；反之維持 bare=all 又留著 footgun。需業主對「DB 是否允許 bare
   scope-aware code 存在」做政策裁定。
3. **`is_unrestricted` 共用衝突**：同一函式既服務 role-based（無 code）又服務 grant-based
   （有 code），兩種語意混用是 #2 的根。重構需先決定「是否強制所有 caller 傳 code」。
4. **SQLite 測試盲區**：`permission_names`（PG `ARRAY(Text)`）與 scope 查詢的 PG 行為，
   pytest 走 SQLite 照不到（記憶 `feedback_sqlite_test_blindspot_pg_array_contains`）；
   驗證須另以真 PG（dev DB 或 supabase read-only）。

---

## 3. 提案選項（待裁定）

### A. #2 scope-blind gate — 三選一
- **A1（推薦，漸進）**：對「實際支援 row-level scope」的端點逐一補 `code=`
  （student_attendance / student_leaves / student_communications 等班級 scope 清單），
  其餘全園彙總端點改用 `assert_all_scope(code=...)` 顯式要求 `:all`。
  逐端點 TDD（每處一個「supervisor 被覆寫成 :own_class 應只見自班」的回歸測試）。
  - 優點：行為變更可控、每處有測試；缺點：工程量大（~30 處），需逐一裁定語意。
- **A2（一刀，高風險）**：讓 `is_unrestricted` / `accessible_classroom_ids` 在缺 `code=`
  時改為 fail-closed 或要求必填 `code`。
  - 優點：根除「忘了傳 code」；缺點：一次打到所有 caller，回歸面巨大，易誤擋。
- **A3（不動）**：判定「per-user 把 supervisor 覆寫成 :own_class」非實際營運情境，
  維持現狀並文件化。需業主確認此情境是否存在。

### B. #5 DB-first resolve 正規化 + 測試 — 兩者皆做
- **B1**：DB-first 分支回傳前跑 `validate_permission_names`（丟棄非法）+ 記 log 告警；
  視 U13 決策可加 bare→scoped 調和（見 C）。
- **B2**：補回歸測試覆蓋 **DB-first 路徑**（傳 session + seed DB role），補足
  `test_authz_escalation_fix_2026_06_04` 的盲區。**此項風險低、可獨立先做**。

### C. U13 寫入守衛 — 與 U12 一併
- **C1（推薦）**：`validate_permission_names` 對 `SCOPE_AWARE_CODES` 的 **bare** 形式
  在「角色寫入路徑」（create_role/update_role）拒絕或自動正規化為明確 scope，迫使
  admin 編輯時表態 `:own_class` / `:all`。需業主決定「拒絕」或「預設補 :all 並告警」。
- **C2（U12）**：`sync_core_role_permissions` 改為「scoped 變體存在時 strip bare 變體」，
  或 sync 採 replace 而非 append，消除 bare+scoped 重複。

---

## 4. 建議落地順序（待裁定後另開 plan）

1. **先做 B2**（補 DB-first 提權回歸測試）——零行為變更、純補網，立即降低盲區。
2. 業主裁定 A（建議 A1）與 C 的政策（bare scope-aware code 是否允許存在於 DB）。
3. 依裁定開 plan：B1 正規化 + C1/C2 寫入守衛 + A1 逐端點補 `code=`，全程 TDD，
   並以真 PG 驗證 `ARRAY`/scope 查詢路徑。

---

## 5. 需業主/設計決策清單

- [ ] D1：DB roles 是否允許存在 **bare** scope-aware code？（決定 C1 是「拒絕」或「補 :all」）
- [ ] D2：「per-user 把 supervisor 覆寫成 `:own_class`」是否為實際營運情境？（決定 A 是否需做、做到哪）
- [ ] D3：A 採漸進（A1）或一刀（A2）？
- [ ] D4：DB-first resolve 是否需做 bare→scoped 調和（而非僅丟棄非法）？

---

## 附錄：本次稽核已落地（非本提案範圍）

- #1 `student_leaves` scoped 清單已補終態學生過濾（commit `1b683382`）。
- 本檔已同步更新 workspace `CLAUDE.md` 跨端陷阱 #1 的過時敘述（#6）。
