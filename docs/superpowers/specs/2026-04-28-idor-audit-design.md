# IDOR 全面盤查與修補（IDOR Audit & Hardening）設計

**日期**：2026-04-28
**狀態**：✅ Approved（透過互動 brainstorming 逐節確認）
**前置文件**：`SECURITY_AUDIT.md`、`security_best_practices_report.md`、`activity_fee_security_report.md`

---

## 0. 範圍與目標

對 ivy-backend 全部 API 路由做 IDOR（Insecure Direct Object Reference）盤查，找出所有可透過「直接帶 id」越權存取資源的端點，產出 audit report 後分批修補。

### 威脅模型（全部納入）

| 代號 | 對抗對象 | 範例風險 |
|---|---|---|
| **a** | 員工 A vs 員工 B | 用 `employee_id` 抓他人薪資、補打卡、加班、請假、節慶獎金、人事檔案 |
| **b** | 班導/副班導 vs 不屬於自己班的學生 | 用 `student_id` 看別班的健康、聯絡簿、評估、事件、家長資料 |
| **c** | 家長 A vs 別家小孩 | parent_portal 全系列（出缺、活動、繳費、聯絡簿、portfolio） |
| **d** | 未登入或匿名 vs 公開 router | `activityPublic`、`recruitment` 公開報名、`line_webhook` 內部端點被外部觸發 |
| **e** | 內部高權限角色之間 | 會計 vs HR vs 園長 — Permission bitflag 大致擋住，但**欄位級**可能漏 |

### 範圍邊界
- ✅ **包含**：`ivy-backend/api/` 全部 router、其依賴的 `services/` 與 `repositories/` 中與 ownership 判斷相關之程式碼
- ✅ **包含**：必要的 `utils/idor_guards.py` 共用 helper 抽出（YAGNI：只抽**新發現**的重複 pattern）
- ✅ **包含**：Critical / High 等級 finding 的 pytest 回歸測試
- ❌ **不包含**：前端權限檢查（純 UX，非安全邊界）
- ❌ **不包含**：認證機制（authn）— 僅檢查 authz
- ❌ **不包含**：SQL injection / XSS / CSRF — 已由 `SECURITY_AUDIT.md` 涵蓋
- ❌ **不包含**：Permission bitflag 重新設計
- ❌ **不包含**：既有 `utils/finance_guards`、`utils/portfolio_access` 介面重構（已穩，不動）

### 成功標準
1. Audit 報告涵蓋全部 50+ API 模組，每筆 finding 有：位置、威脅模型代號、級別、PoC、建議修法
2. 全部 Critical 等級 finding 修補完成且有 pytest 回歸測試（受害身分越權應拿 403）
3. 全部 High 等級 finding 修補完成且有 pytest 回歸測試
4. Medium / Low 等級 finding 業主排程，可分次處理
5. 既有測試套件全綠
6. `SECURITY_AUDIT.md` 新增 IDOR section，連結至本 audit 與每筆 finding 的修補狀態

---

## 1. 工作分階段

### Phase 1：盤查（Audit）

**輸入**：整份 `ivy-backend/api/`
**輸出**：`docs/superpowers/audits/2026-04-28-idor-findings.md`
**不動程式碼**

採 **grep + 人工複審混合法**：

#### Step 1-1：靜態 pattern 掃描

針對下列 pattern grep 並彙整：

| Pattern | 目的 | 工具 |
|---|---|---|
| `def \w+\([^)]*_id:` 在 `api/` 下 | 找出所有接 `*_id` 參數的 endpoint | grep |
| `session.get\(\w+, \w*_id\)` 之後沒有額外 filter | 直接撈 ORM 物件、無 ownership 檢查 | grep + 人工 |
| `Depends(get_current_user)` 但函式體內沒見 `current_user` 與 resource owner 比對 | 隱藏越權 | 人工 |
| `include_in_schema=False` / 公開 router | 找未掛 auth 的端點 | grep `prefix=` 與 `Depends` |
| `query.filter(Model.id == ...)` 但沒帶 `employee_id` / `student_id` / 班級 scope | 範圍過寬 | 人工 |

#### Step 1-2：逐模組複審

依下列順序讀全部 router 檔（含 services/repositories 中的 fetch helper）：

1. `parent_portal/*`（c — 公開面向，最危險）
2. `portal/*`（a + b — 教師端，混合風險）
3. `salary.py`、`overtimes.py`、`leaves.py`、`punch_corrections.py`、`employees.py`、`employees_docs.py`（a — 財務人事）
4. `students.py`、`student_*.py`、`portfolio/*`、`classrooms.py`、`meetings.py`（b — 學生資料）
5. `activity/*`、`activityPublic`、`recruitment/*`（c + d — 公開報名）
6. `fees.py`、`insurance.py`、`gov_reports.py`、`reports.py`、`exports.py`（e — 高權限欄位級）
7. 其他：`announcements.py`、`audit.py`、`approvals.py`、`approval_settings.py`、`attachments.py`、`shifts.py`、`events.py`、`notifications.py`、`bonus_preview.py`、`analytics.py`、`config.py`、`dev.py`、`line_webhook.py`、`dismissal_calls.py`、`dismissal_ws.py`

#### Step 1-3：威脅模型對照與 PoC 撰寫

每個確認的 finding 標威脅模型代號（a/b/c/d/e），並寫一句話的攻擊情境（例：「老師甲帶班 A，用 `GET /portal/students/{id}/health` 帶班 B 學生 id，可拿到不屬於他的健康紀錄」）。

### Phase 2：修補（Fix）

依等級分批，每批一個 commit：

1. **Critical batch**：parent_portal 全部 c 類 + 員工跨員工財務（a 的子集）
2. **High batch**：跨班教師存取（b） + 員工敏感欄位（a 剩餘）
3. **Medium batch**：同角色細節資料外洩
4. **Low batch**：低敏感資料

每批 commit 訊息格式：

```
fix(security): block <area> IDOR (<級別>, F-NNN ~ F-MMM)

- 處理 finding F-NNN 至 F-MMM
- 新增 idor_guards.<helper> 統一 ownership 判斷
- 補回歸測試 tests/security/test_idor_<area>.py
```

---

## 2. 風險分級規則

| 級別 | 條件 | 是否強制補測試 |
|---|---|---|
| **Critical** | 跨家庭 / 跨員工 PII 或財務外洩；或未認證即可存取需認證資料 | ✅ 強制 |
| **High** | 跨班教師存取學生敏感資料；員工敏感欄位（薪資、身分證、勞保、銀行帳號）跨人 | ✅ 強制 |
| **Medium** | 同角色同範疇但不該看的細節（例：員工看同事請假明細） | ⚪ 視成本決定（傾向都補） |
| **Low** | 低敏感資料；或現有 Permission bitflag 已大致擋住但欄位有疏漏 | ⚪ 可選 |

---

## 3. Audit 報告格式

輸出至 `docs/superpowers/audits/2026-04-28-idor-findings.md`。

### 3.1 每筆 finding 結構

```markdown
### F-### [<級別>] <模組>: <一句描述>

- **位置**：`api/portal/foo.py:123` `GET /portal/foo/{foo_id}`
- **威脅模型**：a / b / c / d / e
- **PoC**：<一句話的攻擊情境，含受害者與攻擊者身分>
- **根因**：<為何漏掉，例：直接 `session.get(Foo, foo_id)` 未檢查 owner>
- **建議修法**：<一句話，例：用 `assert_employee_self_or_perm()` 守門>
- **是否需新測試**：yes / no
- **修補狀態**：⏳ Pending / 🔧 In Progress / ✅ Fixed (commit hash)
```

### 3.2 報告末尾

- **統計表**：按級別 × 威脅模型 × 模組分布
- **修補進度表**：每筆 finding 的狀態
- **`SECURITY_AUDIT.md` 連結**

---

## 4. 共用 helper 設計

新增 `ivy-backend/utils/idor_guards.py`，只在發現**重複出現** ≥ 2 次的 pattern 時才抽：

```python
# 員工自查 / 高權限管理者
def assert_employee_self_or_perm(
    session: Session,
    current_user: dict,
    target_employee_id: int,
    perm: Permission,
) -> Employee:
    """員工只能查自己；持有 perm 才可查他人。回傳目標 Employee。
    無權限時 raise HTTPException(403)。"""

# 教師對學生（班導/副班導 scope）
def assert_teacher_owns_student(
    session: Session,
    current_user: dict,
    student_id: int,
    *,
    semester_id: int | None = None,
    bypass_perm: Permission | None = None,
) -> Student:
    """老師只能存取自己班的學生；持 bypass_perm 可越過班級限制。
    semester_id 預設取當前學期。"""

# 家長對學生
def assert_parent_owns_student(
    session: Session,
    current_user: dict,
    student_id: int,
) -> Student:
    """家長只能存取已綁定的學生（ParentBinding）。"""

# 通用：根據 owner 欄位比對
def assert_owns_resource(
    session: Session,
    current_user: dict,
    model: type,
    resource_id: int,
    *,
    owner_field: str = "employee_id",
    bypass_perm: Permission | None = None,
):
    """通用守衛：檢查 model.owner_field == 當前員工 id。"""
```

**設計約束**：
- 全部 raise `HTTPException(403)`（不洩漏 404 vs 403 細節，避免 enumeration）
- helper 內部一律重新從 DB 讀取 resource（**不信任** caller 傳進來的物件，避免 TOCTOU）
- 既有 `finance_guards`、`portfolio_access` 不動

---

## 5. 測試策略

### 5.1 測試檔案配置

新建 `tests/security/test_idor_<area>.py`，以威脅模型 × 模組分類：

```
tests/security/
├── test_idor_parent_portal.py    # c
├── test_idor_portal.py           # a + b（教師端）
├── test_idor_employee_financial.py  # a（薪資/加班/請假/補打卡）
├── test_idor_student_data.py     # b
├── test_idor_activity.py         # c + d
└── test_idor_admin_endpoints.py  # e
```

### 5.2 每個 finding 對應的測試形態

```python
def test_F123_parent_cannot_access_other_family_attendance(client, db_session):
    # arrange: 建立兩個家庭、各一個學生
    # act: 家長 A 用家長 B 小孩的 student_id 呼叫 endpoint
    # assert: HTTP 403
```

遵循 `feedback_test_conventions.md`：auth cache 重置順序、SQLite in-memory、不假設假別數量。

### 5.3 既有測試影響

修補若改動回傳行為（例：從原本 200 改 403），更新對應既有測試的固定身分；不放鬆守衛只為了讓舊測試過。

---

## 6. SECURITY_AUDIT.md 整合

完成所有 Critical + High 後，於 `SECURITY_AUDIT.md` 新增：

```markdown
## IDOR 全面盤查（2026-04-28）

詳見 `docs/superpowers/audits/2026-04-28-idor-findings.md`。

- Critical：N 筆，全部已修復
- High：N 筆，全部已修復
- Medium：N 筆，X 筆已修、Y 筆排程中
- Low：N 筆，待業主排程
```

---

## 7. 風險與緩解

| 風險 | 緩解 |
|---|---|
| 修補可能改動既有 API 回傳（例：原本回 200 變 403），影響前端 | 每筆修補前先確認前端呼叫者是否會踩到；若踩到，前端同步調整為「沒權限就不顯示入口」 |
| 50+ 模組讀完 context 龐大，可能遺漏 | grep 自動列表 → 逐 hit 打勾，避免漏掉 |
| Critical/High 修補太多，commit 過大難 review | 每個威脅模型 × 級別獨立 commit；commit 訊息列出 finding 編號區段 |
| TOCTOU：helper 讀過一次 resource，後續又重讀 | helper 直接回傳已驗證的 ORM 物件，caller 重用 |

---

## 8. 時程

- **Phase 1（audit）**：本次 session 完成，產出 `2026-04-28-idor-findings.md`
- **Phase 2（fix）**：audit 交付後另起 implementation plan，分批修補
  - 預期：Critical / High 在後續 1–2 個 session 完成
  - Medium / Low：業主排程

---

## 9. Out of Scope（明確排除）

- ❌ Permission bitflag 重新設計
- ❌ Frontend `hasPermission` 強化（純 UX 層）
- ❌ Authn（登入機制、JWT、session）
- ❌ SQL injection / XSS / CSRF
- ❌ 既有 `utils/finance_guards`、`utils/portfolio_access` 重構
- ❌ Rate limiting（已在 `SECURITY_AUDIT.md` 涵蓋）
- ❌ Audit log 欄位擴充（已由 `project_audit_integration` 處理）
