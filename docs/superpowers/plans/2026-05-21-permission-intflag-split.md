# Permission IntFlag → text[] 重構實施計畫

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 `users.permissions: bigint`（位元遮罩，已用到 1<<62 爆滿）改成 `users.permission_names: text[]`（字串集合），消除 64-bit 容量限制與前端 BigInt 心智負擔。

**Architecture:** 單一 alembic migration 切換儲存層（含 backfill + token_version bump）；`Permission` enum 改為 `str Enum`，位元值搬到 `LEGACY_PERMISSION_BITS` dict 僅供 migration 用；後端 `has_permission` 與前端 `hasPermission` 改 set/includes check；前端 4 個 `permissionMask*` helper 改名 + 改實作；前端啟動偵測舊 localStorage schema 自動清掉。

**Tech Stack:** FastAPI / Pydantic / SQLAlchemy + Alembic / PostgreSQL（後端）；Vue 3 + TypeScript + Pinia（前端）。對應 spec：`docs/superpowers/specs/2026-05-21-permission-intflag-split-design.md`。

---

## 前置：worktree 與分支

依 workspace SOP（CLAUDE.md「跨前後端變更流程」）：

```bash
# 後端 worktree
cd ~/Desktop/ivy-backend
git worktree add .claude/worktrees/permission-text-array-2026-05-21-backend \
    -b feat/permission-text-array-2026-05-21-backend

# 前端 worktree
cd ~/Desktop/ivy-frontend
git worktree add .claude/worktrees/permission-text-array-2026-05-21-frontend \
    -b feat/permission-text-array-2026-05-21-frontend
```

後端先做（Task 1–8），前端後做（Task 9–13），整合驗證最後（Task 14）。

---

## File Structure

### 後端

| 檔案 | 變更類型 | 責任 |
|------|---------|------|
| `utils/permissions.py` | 重寫 | `Permission(str, Enum)`、`LEGACY_PERMISSION_BITS`、`WILDCARD`、`has_permission`、`resolve_user_permissions` |
| `models/auth.py` | 修改 | `users.permissions` → `permission_names: ARRAY(Text)` |
| `utils/auth.py` | 修改 | JWT claim 命名 `permissions` → `permission_names` |
| `api/auth.py` | 修改 | login / me / refresh / token issue 改用 `resolve_user_permissions` + 新 response 欄位 |
| `api/users.py` | 修改 | user CRUD payload schema + response |
| 其他 routers | 修改 | grep-based 機械式 call signature 更新（~33 個 file） |
| `alembic/versions/<rev>_permissions_to_text_array.py` | 新建 | upgrade/downgrade + backfill + token_version bump |
| `tests/test_permissions_unit.py` | 新建 | `has_permission` / `resolve_user_permissions` / ROLE_TEMPLATES 完整性 |
| `tests/test_permission_migration_roundtrip.py` | 新建 | bigint ↔ text[] backfill round-trip |
| 既有 permission 測試 | 修改 | 機械式：`permissions=-1` → `permission_names=['*']` |

### 前端

| 檔案 | 變更類型 | 責任 |
|------|---------|------|
| `src/constants/permissions.ts` | 重寫 | `PERMISSION_VALUES` (number) → `PERMISSION_NAMES` (string const) |
| `src/utils/auth.ts` | 修改 | `hasPermission` set check、4 mask helper 改名、移除 BigInt、加 localStorage schema sniffer |
| 其他 call site | 修改 | grep-based（~6 個 file）`permissionMask*` / `PERMISSION_VALUES` / `userInfo.permissions` 替換 |
| `tests/utils/auth.test.ts` | 新建 | hasPermission 三情境 |
| `tests/utils/permissions.test.ts` | 新建 | 4 改名 helper |

---

## Task 1: 後端 — 新 `utils/permissions.py` + unit tests

**Files:**
- Modify: `utils/permissions.py`（重寫）
- Test: `tests/test_permissions_unit.py`（新建）

- [ ] **Step 1.1: 寫失敗測試 `tests/test_permissions_unit.py`**

```python
"""Unit tests for utils/permissions.py - new string-based API."""
from utils.permissions import (
    Permission,
    WILDCARD,
    LEGACY_PERMISSION_BITS,
    ROLE_TEMPLATES,
    has_permission,
    resolve_user_permissions,
    get_role_default_permissions,
    get_permission_list,
    get_permissions_definition,
)


def test_permission_enum_inherits_str():
    """Permission.X.value should be the same string."""
    assert Permission.EMPLOYEES_READ.value == "EMPLOYEES_READ"
    assert Permission.VENDOR_PAYMENT_WRITE.value == "VENDOR_PAYMENT_WRITE"
    # Inherits str
    assert isinstance(Permission.DASHBOARD, str)


def test_legacy_bits_snapshot_has_63_entries():
    """凍結快照：恰好 63 條（bit 0-62），與重構前 enum 數量對齊。"""
    assert len(LEGACY_PERMISSION_BITS) == 63
    # 每條都對應一個 Permission enum 值
    for name in LEGACY_PERMISSION_BITS:
        assert name in Permission.__members__


def test_legacy_bits_no_duplicate_bits():
    """所有 bit 值唯一。"""
    bits = list(LEGACY_PERMISSION_BITS.values())
    assert len(bits) == len(set(bits))


def test_legacy_bits_max_bit_is_62():
    """重構前最高位元 1<<62 (VENDOR_PAYMENT_WRITE)。"""
    assert max(LEGACY_PERMISSION_BITS.values()) == (1 << 62)


def test_has_permission_wildcard():
    assert has_permission(["*"], Permission.EMPLOYEES_READ) is True
    assert has_permission(["*"], "ANY_STRING") is True


def test_has_permission_hit():
    perms = ["EMPLOYEES_READ", "SALARY_WRITE"]
    assert has_permission(perms, Permission.EMPLOYEES_READ) is True
    assert has_permission(perms, "SALARY_WRITE") is True


def test_has_permission_miss():
    perms = ["EMPLOYEES_READ"]
    assert has_permission(perms, Permission.SALARY_WRITE) is False


def test_has_permission_none_input():
    """None 視為無權限。caller 必須先 resolve_user_permissions。"""
    assert has_permission(None, Permission.EMPLOYEES_READ) is False


def test_has_permission_empty_list():
    assert has_permission([], Permission.EMPLOYEES_READ) is False


def test_has_permission_accepts_str_or_enum():
    perms = ["EMPLOYEES_READ"]
    assert has_permission(perms, "EMPLOYEES_READ") is True
    assert has_permission(perms, Permission.EMPLOYEES_READ) is True


class _FakeUser:
    """Stand-in for SQLAlchemy User model in unit tests."""
    def __init__(self, role: str, permission_names):
        self.role = role
        self.permission_names = permission_names


def test_resolve_uses_role_default_when_null():
    u = _FakeUser(role="hr", permission_names=None)
    perms = resolve_user_permissions(u)
    assert "EMPLOYEES_READ" in perms
    assert "SALARY_READ" in perms


def test_resolve_returns_explicit_when_set():
    u = _FakeUser(role="hr", permission_names=["ONLY_ONE_PERM"])
    perms = resolve_user_permissions(u)
    assert perms == ["ONLY_ONE_PERM"]


def test_resolve_admin_role_default_is_wildcard():
    u = _FakeUser(role="admin", permission_names=None)
    perms = resolve_user_permissions(u)
    assert "*" in perms


def test_resolve_parent_role_default_is_empty():
    u = _FakeUser(role="parent", permission_names=None)
    perms = resolve_user_permissions(u)
    assert perms == []


def test_get_role_default_unknown_role_falls_back_to_teacher():
    """未知角色 fallback 為 teacher 預設。"""
    perms = get_role_default_permissions("xxxxx")
    assert perms == get_role_default_permissions("teacher")


def test_role_templates_all_use_valid_permission_names():
    """ROLE_TEMPLATES 內每個 perm name 都在 Permission enum 中（或 wildcard）。"""
    for role, perms in ROLE_TEMPLATES.items():
        for p in perms:
            assert p == WILDCARD or p in Permission.__members__, (
                f"ROLE_TEMPLATES[{role}] 含非法 perm: {p}"
            )


def test_get_permission_list_wildcard_expands_all():
    expanded = get_permission_list(["*"])
    assert len(expanded) == 63
    assert "EMPLOYEES_READ" in expanded


def test_get_permission_list_filters_unknown():
    perms = ["EMPLOYEES_READ", "BOGUS_NAME"]
    expanded = get_permission_list(perms)
    assert expanded == ["EMPLOYEES_READ"]


def test_get_permission_list_none_returns_empty():
    assert get_permission_list(None) == []


def test_get_permissions_definition_shape():
    defn = get_permissions_definition()
    assert "permissions" in defn
    assert "groups" in defn
    assert "roles" in defn
    assert "split_modules" in defn
    # value 應為字串（與 name 相同），不再是 int
    assert defn["permissions"]["EMPLOYEES_READ"]["value"] == "EMPLOYEES_READ"
    assert defn["permissions"]["EMPLOYEES_READ"]["label"] == "員工管理 (檢視)"


def test_get_permissions_definition_admin_role_is_wildcard():
    defn = get_permissions_definition()
    assert defn["roles"]["admin"]["permissions"] == ["*"]
```

- [ ] **Step 1.2: 跑測試確認失敗**

```bash
cd ~/Desktop/ivy-backend
pytest tests/test_permissions_unit.py -v
```

預期：ImportError / AttributeError（新 API 不存在）。

- [ ] **Step 1.3: 重寫 `utils/permissions.py`**

```python
"""
Permission definitions for fine-grained access control
（text[] 版本，2026-05-21 重構：脫離 64-bit IntFlag 容量限制）
"""

from enum import Enum
from typing import List, Dict

WILDCARD = "*"


class Permission(str, Enum):
    """權限識別字串（繼承 str：perm.value == "EMPLOYEES_READ"）。

    位元值已搬到 LEGACY_PERMISSION_BITS，僅 alembic migration 使用，
    runtime 不參考。
    """

    # --- 不拆分的模組 ---
    DASHBOARD = "DASHBOARD"
    APPROVALS = "APPROVALS"
    CALENDAR = "CALENDAR"
    SCHEDULE = "SCHEDULE"
    MEETINGS = "MEETINGS"
    REPORTS = "REPORTS"
    AUDIT_LOGS = "AUDIT_LOGS"

    # --- 讀寫分離模組 ---
    ATTENDANCE_READ = "ATTENDANCE_READ"
    ATTENDANCE_WRITE = "ATTENDANCE_WRITE"
    LEAVES_READ = "LEAVES_READ"
    LEAVES_WRITE = "LEAVES_WRITE"
    OVERTIME_READ = "OVERTIME_READ"
    OVERTIME_WRITE = "OVERTIME_WRITE"
    EMPLOYEES_READ = "EMPLOYEES_READ"
    EMPLOYEES_WRITE = "EMPLOYEES_WRITE"
    STUDENTS_READ = "STUDENTS_READ"
    STUDENTS_WRITE = "STUDENTS_WRITE"
    CLASSROOMS_READ = "CLASSROOMS_READ"
    CLASSROOMS_WRITE = "CLASSROOMS_WRITE"
    SALARY_READ = "SALARY_READ"
    SALARY_WRITE = "SALARY_WRITE"
    ANNOUNCEMENTS_READ = "ANNOUNCEMENTS_READ"
    ANNOUNCEMENTS_WRITE = "ANNOUNCEMENTS_WRITE"
    SETTINGS_READ = "SETTINGS_READ"
    SETTINGS_WRITE = "SETTINGS_WRITE"
    USER_MANAGEMENT_READ = "USER_MANAGEMENT_READ"
    USER_MANAGEMENT_WRITE = "USER_MANAGEMENT_WRITE"
    ACTIVITY_READ = "ACTIVITY_READ"
    ACTIVITY_WRITE = "ACTIVITY_WRITE"
    DISMISSAL_CALLS_READ = "DISMISSAL_CALLS_READ"
    DISMISSAL_CALLS_WRITE = "DISMISSAL_CALLS_WRITE"
    FEES_READ = "FEES_READ"
    FEES_WRITE = "FEES_WRITE"
    RECRUITMENT_READ = "RECRUITMENT_READ"
    RECRUITMENT_WRITE = "RECRUITMENT_WRITE"
    ACTIVITY_PAYMENT_APPROVE = "ACTIVITY_PAYMENT_APPROVE"

    # --- 學生生命週期 ---
    STUDENTS_LIFECYCLE_WRITE = "STUDENTS_LIFECYCLE_WRITE"
    GUARDIANS_READ = "GUARDIANS_READ"
    GUARDIANS_WRITE = "GUARDIANS_WRITE"
    RECRUITMENT_CONVERT = "RECRUITMENT_CONVERT"
    BUSINESS_ANALYTICS = "BUSINESS_ANALYTICS"

    # --- Portfolio ---
    PORTFOLIO_READ = "PORTFOLIO_READ"
    PORTFOLIO_WRITE = "PORTFOLIO_WRITE"
    PORTFOLIO_PUBLISH = "PORTFOLIO_PUBLISH"
    STUDENTS_HEALTH_READ = "STUDENTS_HEALTH_READ"
    STUDENTS_HEALTH_WRITE = "STUDENTS_HEALTH_WRITE"
    STUDENTS_MEDICATION_ADMINISTER = "STUDENTS_MEDICATION_ADMINISTER"
    STUDENTS_SPECIAL_NEEDS_READ = "STUDENTS_SPECIAL_NEEDS_READ"
    STUDENTS_SPECIAL_NEEDS_WRITE = "STUDENTS_SPECIAL_NEEDS_WRITE"

    # --- 家園溝通 ---
    PARENT_MESSAGES_WRITE = "PARENT_MESSAGES_WRITE"

    # --- 教育部申報 ---
    GOV_REPORTS_VIEW = "GOV_REPORTS_VIEW"
    GOV_REPORTS_EXPORT = "GOV_REPORTS_EXPORT"

    # --- 教職員考核 ---
    APPRAISAL_READ = "APPRAISAL_READ"
    APPRAISAL_EVENT_WRITE = "APPRAISAL_EVENT_WRITE"
    APPRAISAL_REVIEW = "APPRAISAL_REVIEW"
    APPRAISAL_ACCOUNTING = "APPRAISAL_ACCOUNTING"
    APPRAISAL_FINALIZE = "APPRAISAL_FINALIZE"
    APPRAISAL_RULE_WRITE = "APPRAISAL_RULE_WRITE"

    # --- 年終獎金 ---
    YEAR_END_READ = "YEAR_END_READ"
    YEAR_END_WRITE = "YEAR_END_WRITE"
    YEAR_END_FINALIZE = "YEAR_END_FINALIZE"

    # --- 廠商付款 ---
    VENDOR_PAYMENT_READ = "VENDOR_PAYMENT_READ"
    VENDOR_PAYMENT_WRITE = "VENDOR_PAYMENT_WRITE"


# 位元值凍結快照——僅供 alembic upgrade()/downgrade() backfill 使用。
# 一旦 migration 跑過 prod，請勿變更此表（保持歷史 migration 可重跑）。
LEGACY_PERMISSION_BITS: Dict[str, int] = {
    "DASHBOARD": 1 << 0,
    "APPROVALS": 1 << 1,
    "CALENDAR": 1 << 2,
    "SCHEDULE": 1 << 3,
    "ATTENDANCE_READ": 1 << 4,
    "LEAVES_READ": 1 << 5,
    "OVERTIME_READ": 1 << 6,
    "MEETINGS": 1 << 7,
    "EMPLOYEES_READ": 1 << 8,
    "STUDENTS_READ": 1 << 9,
    "CLASSROOMS_READ": 1 << 10,
    "SALARY_READ": 1 << 11,
    "ANNOUNCEMENTS_READ": 1 << 12,
    "REPORTS": 1 << 13,
    "AUDIT_LOGS": 1 << 14,
    "SETTINGS_READ": 1 << 15,
    "USER_MANAGEMENT_READ": 1 << 16,
    "ATTENDANCE_WRITE": 1 << 17,
    "LEAVES_WRITE": 1 << 18,
    "OVERTIME_WRITE": 1 << 19,
    "EMPLOYEES_WRITE": 1 << 20,
    "STUDENTS_WRITE": 1 << 21,
    "CLASSROOMS_WRITE": 1 << 22,
    "SALARY_WRITE": 1 << 23,
    "ANNOUNCEMENTS_WRITE": 1 << 24,
    "SETTINGS_WRITE": 1 << 25,
    "USER_MANAGEMENT_WRITE": 1 << 26,
    "ACTIVITY_READ": 1 << 27,
    "ACTIVITY_WRITE": 1 << 28,
    "DISMISSAL_CALLS_READ": 1 << 29,
    "DISMISSAL_CALLS_WRITE": 1 << 30,
    "FEES_READ": 1 << 31,
    "FEES_WRITE": 1 << 32,
    "RECRUITMENT_READ": 1 << 33,
    "RECRUITMENT_WRITE": 1 << 34,
    "ACTIVITY_PAYMENT_APPROVE": 1 << 35,
    "STUDENTS_LIFECYCLE_WRITE": 1 << 36,
    "GUARDIANS_READ": 1 << 37,
    "GUARDIANS_WRITE": 1 << 38,
    "RECRUITMENT_CONVERT": 1 << 39,
    "BUSINESS_ANALYTICS": 1 << 40,
    "PORTFOLIO_READ": 1 << 41,
    "PORTFOLIO_WRITE": 1 << 42,
    "PORTFOLIO_PUBLISH": 1 << 43,
    "STUDENTS_HEALTH_READ": 1 << 44,
    "STUDENTS_HEALTH_WRITE": 1 << 45,
    "STUDENTS_MEDICATION_ADMINISTER": 1 << 46,
    "STUDENTS_SPECIAL_NEEDS_READ": 1 << 47,
    "STUDENTS_SPECIAL_NEEDS_WRITE": 1 << 48,
    "PARENT_MESSAGES_WRITE": 1 << 49,
    "GOV_REPORTS_VIEW": 1 << 50,
    "GOV_REPORTS_EXPORT": 1 << 51,
    "YEAR_END_READ": 1 << 52,
    "APPRAISAL_RULE_WRITE": 1 << 53,
    "VENDOR_PAYMENT_READ": 1 << 54,
    "APPRAISAL_READ": 1 << 55,
    "APPRAISAL_EVENT_WRITE": 1 << 56,
    "APPRAISAL_REVIEW": 1 << 57,
    "APPRAISAL_ACCOUNTING": 1 << 58,
    "APPRAISAL_FINALIZE": 1 << 59,
    "YEAR_END_WRITE": 1 << 60,
    "YEAR_END_FINALIZE": 1 << 61,
    "VENDOR_PAYMENT_WRITE": 1 << 62,
}


# ---------------------------------------------------------------------------
# 讀寫配對映射（模組基礎名稱 → read/write 權限名稱）
# ---------------------------------------------------------------------------

SPLIT_MODULES: Dict[str, Dict[str, str]] = {
    "ATTENDANCE": {"read": "ATTENDANCE_READ", "write": "ATTENDANCE_WRITE"},
    "LEAVES": {"read": "LEAVES_READ", "write": "LEAVES_WRITE"},
    "OVERTIME": {"read": "OVERTIME_READ", "write": "OVERTIME_WRITE"},
    "EMPLOYEES": {"read": "EMPLOYEES_READ", "write": "EMPLOYEES_WRITE"},
    "STUDENTS": {"read": "STUDENTS_READ", "write": "STUDENTS_WRITE"},
    "CLASSROOMS": {"read": "CLASSROOMS_READ", "write": "CLASSROOMS_WRITE"},
    "SALARY": {"read": "SALARY_READ", "write": "SALARY_WRITE"},
    "ANNOUNCEMENTS": {"read": "ANNOUNCEMENTS_READ", "write": "ANNOUNCEMENTS_WRITE"},
    "SETTINGS": {"read": "SETTINGS_READ", "write": "SETTINGS_WRITE"},
    "USER_MANAGEMENT": {"read": "USER_MANAGEMENT_READ", "write": "USER_MANAGEMENT_WRITE"},
    "ACTIVITY": {"read": "ACTIVITY_READ", "write": "ACTIVITY_WRITE"},
    "DISMISSAL_CALLS": {"read": "DISMISSAL_CALLS_READ", "write": "DISMISSAL_CALLS_WRITE"},
    "FEES": {"read": "FEES_READ", "write": "FEES_WRITE"},
    "RECRUITMENT": {"read": "RECRUITMENT_READ", "write": "RECRUITMENT_WRITE"},
    "GUARDIANS": {"read": "GUARDIANS_READ", "write": "GUARDIANS_WRITE"},
    "APPRAISAL": {"read": "APPRAISAL_READ", "write": "APPRAISAL_EVENT_WRITE"},
    "YEAR_END": {"read": "YEAR_END_READ", "write": "YEAR_END_WRITE"},
    "VENDOR_PAYMENT": {"read": "VENDOR_PAYMENT_READ", "write": "VENDOR_PAYMENT_WRITE"},
}


# ---------------------------------------------------------------------------
# RBAC 角色模板（值為 permission name list）
# ---------------------------------------------------------------------------

ROLE_TEMPLATES: Dict[str, List[str]] = {
    "admin": [WILDCARD],
    "hr": [
        Permission.DASHBOARD.value,
        Permission.EMPLOYEES_READ.value,
        Permission.EMPLOYEES_WRITE.value,
        Permission.SALARY_READ.value,
        Permission.SALARY_WRITE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.LEAVES_WRITE.value,
        Permission.OVERTIME_READ.value,
        Permission.OVERTIME_WRITE.value,
        Permission.REPORTS.value,
        Permission.GOV_REPORTS_VIEW.value,
        Permission.GOV_REPORTS_EXPORT.value,
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_ACCOUNTING.value,
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
    ],
    "supervisor": [
        Permission.DASHBOARD.value,
        Permission.APPROVALS.value,
        Permission.CALENDAR.value,
        Permission.SCHEDULE.value,
        Permission.ATTENDANCE_READ.value,
        Permission.ATTENDANCE_WRITE.value,
        Permission.LEAVES_READ.value,
        Permission.LEAVES_WRITE.value,
        Permission.OVERTIME_READ.value,
        Permission.OVERTIME_WRITE.value,
        Permission.MEETINGS.value,
        Permission.STUDENTS_READ.value,
        Permission.STUDENTS_WRITE.value,
        Permission.STUDENTS_LIFECYCLE_WRITE.value,
        Permission.GUARDIANS_READ.value,
        Permission.GUARDIANS_WRITE.value,
        Permission.CLASSROOMS_READ.value,
        Permission.CLASSROOMS_WRITE.value,
        Permission.FEES_READ.value,
        Permission.FEES_WRITE.value,
        Permission.RECRUITMENT_READ.value,
        Permission.RECRUITMENT_WRITE.value,
        Permission.RECRUITMENT_CONVERT.value,
        Permission.BUSINESS_ANALYTICS.value,
        Permission.REPORTS.value,
        Permission.PORTFOLIO_READ.value,
        Permission.PORTFOLIO_WRITE.value,
        Permission.PORTFOLIO_PUBLISH.value,
        Permission.STUDENTS_HEALTH_READ.value,
        Permission.STUDENTS_HEALTH_WRITE.value,
        Permission.STUDENTS_MEDICATION_ADMINISTER.value,
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value,
        Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value,
        Permission.PARENT_MESSAGES_WRITE.value,
        Permission.GOV_REPORTS_VIEW.value,
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_REVIEW.value,
        Permission.APPRAISAL_FINALIZE.value,
        Permission.APPRAISAL_RULE_WRITE.value,
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.YEAR_END_FINALIZE.value,
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
    ],
    "teacher": [
        Permission.DASHBOARD.value,
        Permission.CALENDAR.value,
        Permission.ANNOUNCEMENTS_READ.value,
        Permission.DISMISSAL_CALLS_READ.value,
        Permission.DISMISSAL_CALLS_WRITE.value,
        Permission.PORTFOLIO_READ.value,
        Permission.PORTFOLIO_WRITE.value,
        Permission.STUDENTS_HEALTH_READ.value,
        Permission.STUDENTS_MEDICATION_ADMINISTER.value,
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value,
        Permission.PARENT_MESSAGES_WRITE.value,
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
    ],
    "parent": [],
}


ROLE_LABELS: Dict[str, str] = {
    "admin": "系統管理員",
    "hr": "人事管理員",
    "supervisor": "主管",
    "teacher": "教師",
    "parent": "家長",
}


PERMISSION_LABELS: Dict[str, str] = {
    "DASHBOARD": "儀表板",
    "APPROVALS": "審核工作台",
    "CALENDAR": "行事曆",
    "SCHEDULE": "排班管理",
    "MEETINGS": "園務會議",
    "REPORTS": "報表統計",
    "AUDIT_LOGS": "操作紀錄",
    "ATTENDANCE_READ": "出勤管理 (檢視)",
    "ATTENDANCE_WRITE": "出勤管理 (編輯)",
    "LEAVES_READ": "請假管理 (檢視)",
    "LEAVES_WRITE": "請假管理 (編輯)",
    "OVERTIME_READ": "加班管理 (檢視)",
    "OVERTIME_WRITE": "加班管理 (編輯)",
    "EMPLOYEES_READ": "員工管理 (檢視)",
    "EMPLOYEES_WRITE": "員工管理 (編輯)",
    "STUDENTS_READ": "學生管理 (檢視)",
    "STUDENTS_WRITE": "學生管理 (編輯)",
    "CLASSROOMS_READ": "班級管理 (檢視)",
    "CLASSROOMS_WRITE": "班級管理 (編輯)",
    "SALARY_READ": "薪資管理 (檢視)",
    "SALARY_WRITE": "薪資管理 (編輯)",
    "ANNOUNCEMENTS_READ": "公告管理 (檢視)",
    "ANNOUNCEMENTS_WRITE": "公告管理 (編輯)",
    "SETTINGS_READ": "系統設定 (檢視)",
    "SETTINGS_WRITE": "系統設定 (編輯)",
    "USER_MANAGEMENT_READ": "帳號管理 (檢視)",
    "USER_MANAGEMENT_WRITE": "帳號管理 (編輯)",
    "ACTIVITY_READ": "課後才藝 (檢視)",
    "ACTIVITY_WRITE": "課後才藝 (編輯)",
    "DISMISSAL_CALLS_READ": "接送通知 (檢視)",
    "DISMISSAL_CALLS_WRITE": "接送通知 (操作)",
    "FEES_READ": "學費管理 (檢視)",
    "FEES_WRITE": "學費管理 (編輯)",
    "RECRUITMENT_READ": "招生統計 (檢視)",
    "RECRUITMENT_WRITE": "招生統計 (編輯)",
    "ACTIVITY_PAYMENT_APPROVE": "才藝課收款簽核",
    "STUDENTS_LIFECYCLE_WRITE": "學生生命週期 (狀態轉移)",
    "GUARDIANS_READ": "監護人資料 (檢視)",
    "GUARDIANS_WRITE": "監護人資料 (編輯)",
    "RECRUITMENT_CONVERT": "招生轉化為學生",
    "BUSINESS_ANALYTICS": "經營分析",
    "PORTFOLIO_READ": "成長歷程 (檢視)",
    "PORTFOLIO_WRITE": "成長歷程 (編輯)",
    "PORTFOLIO_PUBLISH": "學期報告 (發佈)",
    "STUDENTS_HEALTH_READ": "健康資訊 (檢視)",
    "STUDENTS_HEALTH_WRITE": "健康資訊 (編輯)",
    "STUDENTS_MEDICATION_ADMINISTER": "餵藥執行與紀錄",
    "STUDENTS_SPECIAL_NEEDS_READ": "特殊需求 (檢視)",
    "STUDENTS_SPECIAL_NEEDS_WRITE": "特殊需求 (編輯 / IEP)",
    "PARENT_MESSAGES_WRITE": "家長訊息 (發送/回覆)",
    "GOV_REPORTS_VIEW": "政府申報資料 (檢視)",
    "GOV_REPORTS_EXPORT": "政府申報匯出 (執行)",
    "APPRAISAL_READ": "考核資料 (檢視)",
    "APPRAISAL_EVENT_WRITE": "考核事件 (登錄)",
    "APPRAISAL_REVIEW": "考核簽核 (主管第一階)",
    "APPRAISAL_ACCOUNTING": "考核核數字 (會計第二階)",
    "APPRAISAL_FINALIZE": "考核核定 (最高主管第三階)",
    "APPRAISAL_RULE_WRITE": "考核扣分規則設定 (Phase 1 calibrate)",
    "YEAR_END_READ": "年終結算 (檢視)",
    "YEAR_END_WRITE": "年終結算 (編輯)",
    "YEAR_END_FINALIZE": "年終核定 (最高主管)",
    "VENDOR_PAYMENT_READ": "廠商付款簽收 (檢視)",
    "VENDOR_PAYMENT_WRITE": "廠商付款簽收 (編輯/簽收)",
}


# PERMISSION_GROUPS：前端 UI 結構不變（已是字串名稱）。複製原檔 PERMISSION_GROUPS 內容
# 整段（前端設定頁需要的 group → permissions/split_permissions 結構）。
# （此處省略 ~140 行 PERMISSION_GROUPS dict，從原 utils/permissions.py 整段搬移：
#  「首頁 / 考勤管理 / 人事教務 / 教職員考核 / 園務行政 / 成長歷程 / 教務 / 系統」
#  7 個 group，逐字保留。）
PERMISSION_GROUPS: List[Dict] = [
    # ... 整段從原檔搬移；結構為 string 不需改動 ...
]


def get_role_default_permissions(role: str) -> List[str]:
    """取得角色的預設權限。未知角色 fallback 為 teacher。"""
    return list(ROLE_TEMPLATES.get(role, ROLE_TEMPLATES["teacher"]))


def has_permission(
    user_perms: List[str] | None,
    required,  # Permission | str
) -> bool:
    """單一權限檢查。

    user_perms 應為已 resolve 完的最終 list。
    若 caller 傳 None，視為「無權限」回 False；不在 helper 內 fallback role。
    """
    if user_perms is None:
        return False
    if WILDCARD in user_perms:
        return True
    name = required.value if isinstance(required, Permission) else required
    return name in user_perms


def resolve_user_permissions(user) -> List[str]:
    """把 DB 欄位 + role 預設合成最終 permission 集合。

    user.permission_names 為 NULL → 用 ROLE_TEMPLATES[user.role]
    否則直接用 user.permission_names。
    """
    if user.permission_names is None:
        return list(ROLE_TEMPLATES.get(user.role, []))
    return list(user.permission_names)


def get_permission_list(user_perms: List[str] | None) -> List[str]:
    """供 audit log / debug 用：把使用者實際擁有的權限名稱展開。
    遇到 wildcard 展開為所有 enum names。
    """
    if user_perms is None:
        return []
    if WILDCARD in user_perms:
        return [p.value for p in Permission]
    return [p for p in user_perms if p in Permission.__members__]


def get_permissions_definition() -> Dict:
    """取得完整權限定義供前端使用。"""
    permissions = {
        perm.value: {
            "value": perm.value,
            "label": PERMISSION_LABELS.get(perm.value, perm.value),
        }
        for perm in Permission
    }
    roles = {
        role: {
            "permissions": perms,
            "label": ROLE_LABELS.get(role, role),
        }
        for role, perms in ROLE_TEMPLATES.items()
    }
    return {
        "permissions": permissions,
        "groups": PERMISSION_GROUPS,
        "roles": roles,
        "split_modules": SPLIT_MODULES,
    }
```

**注意：** `PERMISSION_GROUPS` 整段（~140 行）從原 `utils/permissions.py` 第 365-502 行**逐字複製**——結構為字串 key 不需改動。實作時可在 git history 拿原檔內容（`git show HEAD:utils/permissions.py | sed -n '365,502p'`）。

- [ ] **Step 1.4: 跑測試確認通過**

```bash
cd ~/Desktop/ivy-backend
pytest tests/test_permissions_unit.py -v
```

預期：22 個 test 全綠。

- [ ] **Step 1.5: 跑全套 pytest 看連帶傷害**

```bash
pytest tests/ 2>&1 | tail -30
```

預期：許多 test fail（簽章變了）。這是預期狀態，下面 Task 3–8 會逐步修復；Task 8 收尾全綠。

- [ ] **Step 1.6: 不 commit**

本 task 不 commit，等 Task 8 一起 commit 後端整批變更。原因：utils/permissions.py 與 router 守衛簽章必須同步 commit，否則 commit-by-commit 上 git history 會有中間「broken」狀態。

---

## Task 2: 後端 — Alembic migration 與 round-trip 測試

**Files:**
- Create: `alembic/versions/<rev>_permissions_to_text_array.py`
- Test: `tests/test_permission_migration_roundtrip.py`

- [ ] **Step 2.1: 確認 alembic head**

```bash
cd ~/Desktop/ivy-backend
alembic heads
```

記下目前 head revision id（例如 `abc12345`）作為新 migration 的 `down_revision`。

- [ ] **Step 2.2: 產生 migration 檔**

```bash
alembic revision -m "permissions_to_text_array" --rev-id=permtxt01
```

檔案出現在 `alembic/versions/<timestamp>_permissions_to_text_array.py`。

- [ ] **Step 2.3: 寫 migration 內容**

開啟產生的檔案，覆寫成下列內容（保留 alembic 自動產的 revision id / down_revision）：

```python
"""permissions_to_text_array

Revision ID: permtxt01
Revises: <填入 alembic heads 給的 id>
Create Date: 2026-05-21

把 users.permissions (bigint) 拆成 users.permission_names (text[])。
backfill 邏輯用 LEGACY_BITS 凍結快照，避免 import utils.permissions 抓到未來改過的版本。
同時 bump 所有 user 的 token_version，強制全員重登（舊 JWT 帶舊 permissions claim 即失效）。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "permtxt01"
down_revision = "<填入 alembic heads 給的 id>"
branch_labels = None
depends_on = None


# 凍結快照——本檔自含，**不要**從 utils.permissions import LEGACY_PERMISSION_BITS。
# 一旦 prod 跑過本 migration，下表不准動。
_LEGACY_BITS = {
    "DASHBOARD": 1 << 0,
    "APPROVALS": 1 << 1,
    "CALENDAR": 1 << 2,
    "SCHEDULE": 1 << 3,
    "ATTENDANCE_READ": 1 << 4,
    "LEAVES_READ": 1 << 5,
    "OVERTIME_READ": 1 << 6,
    "MEETINGS": 1 << 7,
    "EMPLOYEES_READ": 1 << 8,
    "STUDENTS_READ": 1 << 9,
    "CLASSROOMS_READ": 1 << 10,
    "SALARY_READ": 1 << 11,
    "ANNOUNCEMENTS_READ": 1 << 12,
    "REPORTS": 1 << 13,
    "AUDIT_LOGS": 1 << 14,
    "SETTINGS_READ": 1 << 15,
    "USER_MANAGEMENT_READ": 1 << 16,
    "ATTENDANCE_WRITE": 1 << 17,
    "LEAVES_WRITE": 1 << 18,
    "OVERTIME_WRITE": 1 << 19,
    "EMPLOYEES_WRITE": 1 << 20,
    "STUDENTS_WRITE": 1 << 21,
    "CLASSROOMS_WRITE": 1 << 22,
    "SALARY_WRITE": 1 << 23,
    "ANNOUNCEMENTS_WRITE": 1 << 24,
    "SETTINGS_WRITE": 1 << 25,
    "USER_MANAGEMENT_WRITE": 1 << 26,
    "ACTIVITY_READ": 1 << 27,
    "ACTIVITY_WRITE": 1 << 28,
    "DISMISSAL_CALLS_READ": 1 << 29,
    "DISMISSAL_CALLS_WRITE": 1 << 30,
    "FEES_READ": 1 << 31,
    "FEES_WRITE": 1 << 32,
    "RECRUITMENT_READ": 1 << 33,
    "RECRUITMENT_WRITE": 1 << 34,
    "ACTIVITY_PAYMENT_APPROVE": 1 << 35,
    "STUDENTS_LIFECYCLE_WRITE": 1 << 36,
    "GUARDIANS_READ": 1 << 37,
    "GUARDIANS_WRITE": 1 << 38,
    "RECRUITMENT_CONVERT": 1 << 39,
    "BUSINESS_ANALYTICS": 1 << 40,
    "PORTFOLIO_READ": 1 << 41,
    "PORTFOLIO_WRITE": 1 << 42,
    "PORTFOLIO_PUBLISH": 1 << 43,
    "STUDENTS_HEALTH_READ": 1 << 44,
    "STUDENTS_HEALTH_WRITE": 1 << 45,
    "STUDENTS_MEDICATION_ADMINISTER": 1 << 46,
    "STUDENTS_SPECIAL_NEEDS_READ": 1 << 47,
    "STUDENTS_SPECIAL_NEEDS_WRITE": 1 << 48,
    "PARENT_MESSAGES_WRITE": 1 << 49,
    "GOV_REPORTS_VIEW": 1 << 50,
    "GOV_REPORTS_EXPORT": 1 << 51,
    "YEAR_END_READ": 1 << 52,
    "APPRAISAL_RULE_WRITE": 1 << 53,
    "VENDOR_PAYMENT_READ": 1 << 54,
    "APPRAISAL_READ": 1 << 55,
    "APPRAISAL_EVENT_WRITE": 1 << 56,
    "APPRAISAL_REVIEW": 1 << 57,
    "APPRAISAL_ACCOUNTING": 1 << 58,
    "APPRAISAL_FINALIZE": 1 << 59,
    "YEAR_END_WRITE": 1 << 60,
    "YEAR_END_FINALIZE": 1 << 61,
    "VENDOR_PAYMENT_WRITE": 1 << 62,
}


def _bigint_to_names(val: int | None) -> list[str] | None:
    """純函式：把 bigint mask 拆成 name list。"""
    if val is None:
        return None
    if val == -1:
        return ["*"]
    if val == 0:
        return []
    return [name for name, bit in _LEGACY_BITS.items() if (val & bit) == bit]


def _names_to_bigint(names: list[str] | None) -> int | None:
    """純函式：把 name list 組回 bigint mask。

    遇到 _LEGACY_BITS 不認得的 name 直接 raise，避免 silently drop。
    """
    if names is None:
        return None
    if "*" in names:
        return -1
    unknown = [n for n in names if n not in _LEGACY_BITS]
    if unknown:
        raise RuntimeError(
            f"downgrade 遇到 LEGACY_BITS 不認得的權限名稱: {unknown}。"
            "請手動處理（移除或更新 LEGACY_BITS）後重跑。"
        )
    val = 0
    for n in names:
        val |= _LEGACY_BITS[n]
    return val


def upgrade():
    bind = op.get_bind()

    # 1) 加新欄
    op.add_column(
        "users",
        sa.Column(
            "permission_names",
            postgresql.ARRAY(sa.Text()),
            nullable=True,
            comment="權限名稱集合（NULL=依角色預設；['*']=全部；[]=無）",
        ),
    )

    # 2) backfill
    rows = bind.execute(sa.text("SELECT id, permissions FROM users")).fetchall()
    for r in rows:
        names = _bigint_to_names(r.permissions)
        bind.execute(
            sa.text("UPDATE users SET permission_names = :names WHERE id = :id"),
            {"names": names, "id": r.id},
        )

    # 3) bump 所有 user token_version，強制全員重登
    bind.execute(
        sa.text("UPDATE users SET token_version = COALESCE(token_version, 0) + 1")
    )

    # 4) drop 舊欄
    op.drop_column("users", "permissions")


def downgrade():
    bind = op.get_bind()

    op.add_column(
        "users",
        sa.Column(
            "permissions",
            sa.BigInteger(),
            nullable=True,
            comment="功能模組權限位元遮罩 (-1=全部權限, NULL=使用角色預設)",
        ),
    )

    rows = bind.execute(
        sa.text("SELECT id, permission_names FROM users")
    ).fetchall()
    for r in rows:
        val = _names_to_bigint(r.permission_names)
        bind.execute(
            sa.text("UPDATE users SET permissions = :val WHERE id = :id"),
            {"val": val, "id": r.id},
        )

    op.drop_column("users", "permission_names")
```

- [ ] **Step 2.4: 寫 round-trip 測試**

```python
# tests/test_permission_migration_roundtrip.py
"""驗證 alembic migration 的 backfill 純函式 round-trip 正確性。"""
import importlib.util
from pathlib import Path

import pytest

# 動態 load migration 模組（檔名含 timestamp，import path 不穩）
_MIGRATION_PATH = next(
    Path("alembic/versions").glob("*_permissions_to_text_array.py")
)
_spec = importlib.util.spec_from_file_location("perm_migration", _MIGRATION_PATH)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)

bigint_to_names = _mod._bigint_to_names
names_to_bigint = _mod._names_to_bigint


def test_null_roundtrip():
    assert bigint_to_names(None) is None
    assert names_to_bigint(None) is None


def test_minus_one_to_wildcard():
    assert bigint_to_names(-1) == ["*"]
    assert names_to_bigint(["*"]) == -1


def test_zero_to_empty():
    assert bigint_to_names(0) == []
    assert names_to_bigint([]) == 0


def test_single_bit_roundtrip():
    # EMPLOYEES_READ = 1 << 8
    val = 1 << 8
    names = bigint_to_names(val)
    assert names == ["EMPLOYEES_READ"]
    assert names_to_bigint(names) == val


def test_combined_bits_roundtrip():
    # EMPLOYEES_READ (1<<8) | SALARY_WRITE (1<<23)
    val = (1 << 8) | (1 << 23)
    names = bigint_to_names(val)
    assert set(names) == {"EMPLOYEES_READ", "SALARY_WRITE"}
    assert names_to_bigint(names) == val


def test_high_bit_roundtrip():
    # VENDOR_PAYMENT_WRITE = 1 << 62（最高位元）
    val = 1 << 62
    names = bigint_to_names(val)
    assert names == ["VENDOR_PAYMENT_WRITE"]
    assert names_to_bigint(names) == val


def test_downgrade_aborts_on_unknown_name():
    with pytest.raises(RuntimeError, match="LEGACY_BITS 不認得"):
        names_to_bigint(["TOTALLY_NEW_PERMISSION"])


def test_legacy_bits_has_63_entries():
    """快照保護：本表已凍結，數量不應變動。"""
    assert len(_mod._LEGACY_BITS) == 63


def test_legacy_bits_max_is_62():
    assert max(_mod._LEGACY_BITS.values()) == (1 << 62)
```

- [ ] **Step 2.5: 跑 migration round-trip 測試**

```bash
pytest tests/test_permission_migration_roundtrip.py -v
```

預期：9 個 test 全綠。

- [ ] **Step 2.6: 本機 dev DB 跑 migration 驗證**

```bash
# 確認 dev DB 連線（postgres MCP 用的同一個）
echo $DATABASE_URL  # 或檢查 alembic.ini

# 跑 migration
alembic upgrade head

# 驗證 schema
psql ivymanagement -c "\d users" | grep permission
# 預期：permission_names | text[] (出現)，permissions 已不存在
```

- [ ] **Step 2.7: 不 commit**

本 task 不 commit（等 Task 8）。

---

## Task 3: 後端 — 更新 `models/auth.py`

**Files:**
- Modify: `models/auth.py:42-47`

- [ ] **Step 3.1: 改 column**

把：

```python
permissions = Column(
    BigInteger,
    nullable=True,
    default=None,
    comment="功能模組權限位元遮罩 (-1=全部權限, NULL=使用角色預設; parent 恆為 0)",
)
```

改成：

```python
permission_names = Column(
    ARRAY(Text),
    nullable=True,
    default=None,
    comment="權限名稱集合（NULL=依角色預設；['*']=全部；[]=無；其他=顯式 perm names）",
)
```

並在 import 區加：

```python
from sqlalchemy import Text
from sqlalchemy.dialects.postgresql import ARRAY
```

移除不再用到的 `BigInteger` import（如果其他欄位沒用到）。

- [ ] **Step 3.2: 確認 model load 不炸**

```bash
python -c "from models.auth import User; print(User.permission_names)"
```

預期：印出 column 物件，不報錯。

- [ ] **Step 3.3: 不 commit**

---

## Task 4: 後端 — 更新 `utils/auth.py`（JWT claim 改名）

**Files:**
- Modify: `utils/auth.py`（grep `permissions` 找 JWT claim 處）

- [ ] **Step 4.1: grep JWT claim 引用**

```bash
grep -n '"permissions"\|payload\.get("permissions"\|\.permissions\b' utils/auth.py
```

預期會找到 token issue 與 verify 兩處（也可能在 decode_token_allow_expired）。

- [ ] **Step 4.2: 改 JWT claim 命名**

每個出現 `"permissions"` 作為 JWT payload key 的地方改成 `"permission_names"`。

範例（issue token 處）：

```python
to_encode = {
    "user_id": user.id,
    "role": user.role,
    "permission_names": resolve_user_permissions(user),  # 從 utils.permissions
    "token_version": user.token_version,
    "exp": ...,
    "jti": ...,
}
```

範例（verify 處）—— 若有將 payload.permissions 寫進 request.state 之類的，rename。

- [ ] **Step 4.3: 加 import**

```python
from utils.permissions import resolve_user_permissions
```

- [ ] **Step 4.4: 驗證**

```bash
python -c "from utils.auth import decode_token; print('ok')"
```

預期：no error。

- [ ] **Step 4.5: 不 commit**

---

## Task 5: 後端 — 更新 `api/auth.py`

**Files:**
- Modify: `api/auth.py`（login / me / refresh / change_password / reset_password）

- [ ] **Step 5.1: grep response shape**

```bash
grep -n '"permissions"\|user\.permissions\b' api/auth.py
```

- [ ] **Step 5.2: 替換**

每處 `user.permissions` 用 `resolve_user_permissions(user)` 取代；response dict key 從 `"permissions"` 改成 `"permission_names"`。

範例：

```python
# 舊
return {
    "user_id": user.id,
    "username": user.username,
    "role": user.role,
    "permissions": user.permissions,
    ...
}

# 新
return {
    "user_id": user.id,
    "username": user.username,
    "role": user.role,
    "permission_names": resolve_user_permissions(user),
    ...
}
```

- [ ] **Step 5.3: 跑相關測試**

```bash
pytest tests/test_auth*.py -v 2>&1 | tail -30
```

預期：可能還有測試 fail（測試端尚未改 key 名）；只需確認沒有 import error / runtime error 即可。

- [ ] **Step 5.4: 不 commit**

---

## Task 6: 後端 — 更新 `api/users.py`

**Files:**
- Modify: `api/users.py`（user CRUD payload schema + response）

- [ ] **Step 6.1: grep**

```bash
grep -n '"permissions"\|permissions\s*:\s*int\|permissions\s*:\s*Optional\[int\]\|user\.permissions\b' api/users.py
```

- [ ] **Step 6.2: 改 Pydantic schema**

把 user create/update 的 schema 內 `permissions: int | None` 改成 `permission_names: list[str] | None`。

範例：

```python
# 舊
class UserCreate(BaseModel):
    username: str
    password: str
    role: str
    permissions: int | None = None

# 新
class UserCreate(BaseModel):
    username: str
    password: str
    role: str
    permission_names: list[str] | None = None
```

response model 同樣處理。

- [ ] **Step 6.3: 改 endpoint 實作**

把 `user.permissions = body.permissions` 改成 `user.permission_names = body.permission_names`。

回傳處用 `resolve_user_permissions(user)` 展開（或視語意決定回原始 `user.permission_names`：list endpoint 通常回原始值，me endpoint 回 resolve 後的）。

- [ ] **Step 6.4: 跑 users 測試**

```bash
pytest tests/test_users*.py -v 2>&1 | tail -30
```

- [ ] **Step 6.5: 不 commit**

---

## Task 7: 後端 — 其餘 routers 機械式 grep 替換

**Files:**
- Modify: 33 個 router file（以下命令找出來的）

- [ ] **Step 7.1: 找出所有引用點**

```bash
cd ~/Desktop/ivy-backend
grep -rn 'has_permission\|current_user\[?\.permissions\b\|permissions\s*=\s*-1\|require_permission\|Permission\.ALL\|\.permissions\s*&\b' api/ utils/ models/ --include="*.py" -l
```

把這些檔案列入待改清單。

- [ ] **Step 7.2: 逐檔處理**

每個檔案的處理動作（grep 結果引導）：

1. `current_user["permissions"]` / `current_user.permissions` → `current_user["permission_names"]`（在 JWT payload 或 request.state.user 中對應的 key）
2. `has_permission(perms_int, Permission.X)` → 確認 `perms_int` 來源：若是 `current_user["permissions"]`，先改名為 `permission_names`；helper 簽章從 int 變 list[str]，無需 caller side cast（直接 pass list）
3. `Permission.ALL` → 移除使用點；改檢查 `WILDCARD in perms`
4. `perms & Permission.X.value` 之類的 bitwise → 改 `Permission.X.value in perms`
5. `require_permission(Permission.X)` decorator / dependency → 簽章不變（仍接 `Permission` enum），內部實作改為 set check

- [ ] **Step 7.3: 跑全套 pytest 查還有多少破**

```bash
pytest tests/ 2>&1 | grep -E "^FAILED|^ERROR" | head -30
```

預期：剩下的 fail 都集中在「測試 fixture / setup 仍用舊 schema」(`permissions=-1` 等)，這些在 Task 8 處理。

- [ ] **Step 7.4: 不 commit**

---

## Task 8: 後端 — 修復既有測試 + 全套綠 + 單一 commit

**Files:**
- Modify: 所有引用舊 `permissions=` 的測試（grep 找）

- [ ] **Step 8.1: 找出所有舊測試引用**

```bash
cd ~/Desktop/ivy-backend
grep -rn 'permissions\s*=\s*-1\|permissions\s*=\s*0\|permissions\s*=\s*1\s*<<\|permissions\s*=\s*[0-9]\|user\.permissions\b\|"permissions":\s*-1\|"permissions":\s*[0-9]' tests/ --include="*.py" -l
```

- [ ] **Step 8.2: 機械替換**

每個檔案，把測試 fixture 中：

- `permissions=-1` → `permission_names=["*"]`
- `permissions=0` → `permission_names=[]`
- `permissions=1 << 8` → `permission_names=["EMPLOYEES_READ"]`（依 bit 編號對照 LEGACY_BITS）
- `permissions=(1 << 8) | (1 << 23)` → `permission_names=["EMPLOYEES_READ", "SALARY_WRITE"]`
- `"permissions": -1`（JWT payload mock）→ `"permission_names": ["*"]`
- `user.permissions = X` → `user.permission_names = list_form_of_X`

技巧：若 bit 編號難對照，先寫個一次性小 script 用 LEGACY_BITS 反查；或在 PR 自己加一個 helper `_bits_to_names_for_tests`。

- [ ] **Step 8.3: 跑全套 pytest**

```bash
pytest tests/ 2>&1 | tail -10
```

預期：4486/0 全綠（main 基準；3 條 pre-existing `test_audit_router` fail 不計）。

- [ ] **Step 8.4: 後端整批 commit**

```bash
cd ~/Desktop/ivy-backend/.claude/worktrees/permission-text-array-2026-05-21-backend

git add utils/permissions.py models/auth.py utils/auth.py \
        api/ alembic/versions/*permissions_to_text_array* \
        tests/test_permissions_unit.py \
        tests/test_permission_migration_roundtrip.py \
        tests/

git commit -m "refactor(permissions): bigint mask → text[] permission_names

Permission IntFlag 已用到 1<<62 撞 PostgreSQL bigint 上限。
改為 text[] permission_names 徹底脫離 64-bit 容量限制：

- Permission(str, Enum)：以字串為值，位元搬到 LEGACY_PERMISSION_BITS dict
- users.permissions: bigint → users.permission_names: ARRAY(Text)
- has_permission / resolve_user_permissions 走 set check
- ROLE_TEMPLATES 值型別: int → list[str]，admin 從 -1 改 ['*']
- JWT claim: permissions (int) → permission_names (list[str])
- Migration 帶 token_version bump，部署後強制全員重登
- 63 條 enum 對應 LEGACY_PERMISSION_BITS 凍結快照（migration 自含 inline）

Spec: docs/superpowers/specs/2026-05-21-permission-intflag-split-design.md
"
```

- [ ] **Step 8.5: 確認 commit**

```bash
git log -1 --stat | head -30
```

---

## Task 9: 前端 — 重寫 `src/constants/permissions.ts`

**Files:**
- Modify: `src/constants/permissions.ts`

- [ ] **Step 9.1: 改寫**

替換成：

```ts
// 權限名稱常數（純 string；前後端對齊 utils/permissions.py Permission(str, Enum)）
export const PERMISSION_NAMES = {
  DASHBOARD: 'DASHBOARD',
  APPROVALS: 'APPROVALS',
  CALENDAR: 'CALENDAR',
  SCHEDULE: 'SCHEDULE',
  MEETINGS: 'MEETINGS',
  REPORTS: 'REPORTS',
  AUDIT_LOGS: 'AUDIT_LOGS',
  ATTENDANCE_READ: 'ATTENDANCE_READ',
  ATTENDANCE_WRITE: 'ATTENDANCE_WRITE',
  LEAVES_READ: 'LEAVES_READ',
  LEAVES_WRITE: 'LEAVES_WRITE',
  OVERTIME_READ: 'OVERTIME_READ',
  OVERTIME_WRITE: 'OVERTIME_WRITE',
  EMPLOYEES_READ: 'EMPLOYEES_READ',
  EMPLOYEES_WRITE: 'EMPLOYEES_WRITE',
  STUDENTS_READ: 'STUDENTS_READ',
  STUDENTS_WRITE: 'STUDENTS_WRITE',
  CLASSROOMS_READ: 'CLASSROOMS_READ',
  CLASSROOMS_WRITE: 'CLASSROOMS_WRITE',
  SALARY_READ: 'SALARY_READ',
  SALARY_WRITE: 'SALARY_WRITE',
  ANNOUNCEMENTS_READ: 'ANNOUNCEMENTS_READ',
  ANNOUNCEMENTS_WRITE: 'ANNOUNCEMENTS_WRITE',
  SETTINGS_READ: 'SETTINGS_READ',
  SETTINGS_WRITE: 'SETTINGS_WRITE',
  USER_MANAGEMENT_READ: 'USER_MANAGEMENT_READ',
  USER_MANAGEMENT_WRITE: 'USER_MANAGEMENT_WRITE',
  ACTIVITY_READ: 'ACTIVITY_READ',
  ACTIVITY_WRITE: 'ACTIVITY_WRITE',
  DISMISSAL_CALLS_READ: 'DISMISSAL_CALLS_READ',
  DISMISSAL_CALLS_WRITE: 'DISMISSAL_CALLS_WRITE',
  FEES_READ: 'FEES_READ',
  FEES_WRITE: 'FEES_WRITE',
  RECRUITMENT_READ: 'RECRUITMENT_READ',
  RECRUITMENT_WRITE: 'RECRUITMENT_WRITE',
  ACTIVITY_PAYMENT_APPROVE: 'ACTIVITY_PAYMENT_APPROVE',
  STUDENTS_LIFECYCLE_WRITE: 'STUDENTS_LIFECYCLE_WRITE',
  GUARDIANS_READ: 'GUARDIANS_READ',
  GUARDIANS_WRITE: 'GUARDIANS_WRITE',
  RECRUITMENT_CONVERT: 'RECRUITMENT_CONVERT',
  BUSINESS_ANALYTICS: 'BUSINESS_ANALYTICS',
  PORTFOLIO_READ: 'PORTFOLIO_READ',
  PORTFOLIO_WRITE: 'PORTFOLIO_WRITE',
  PORTFOLIO_PUBLISH: 'PORTFOLIO_PUBLISH',
  STUDENTS_HEALTH_READ: 'STUDENTS_HEALTH_READ',
  STUDENTS_HEALTH_WRITE: 'STUDENTS_HEALTH_WRITE',
  STUDENTS_MEDICATION_ADMINISTER: 'STUDENTS_MEDICATION_ADMINISTER',
  STUDENTS_SPECIAL_NEEDS_READ: 'STUDENTS_SPECIAL_NEEDS_READ',
  STUDENTS_SPECIAL_NEEDS_WRITE: 'STUDENTS_SPECIAL_NEEDS_WRITE',
  PARENT_MESSAGES_WRITE: 'PARENT_MESSAGES_WRITE',
  GOV_REPORTS_VIEW: 'GOV_REPORTS_VIEW',
  GOV_REPORTS_EXPORT: 'GOV_REPORTS_EXPORT',
  APPRAISAL_READ: 'APPRAISAL_READ',
  APPRAISAL_EVENT_WRITE: 'APPRAISAL_EVENT_WRITE',
  APPRAISAL_REVIEW: 'APPRAISAL_REVIEW',
  APPRAISAL_ACCOUNTING: 'APPRAISAL_ACCOUNTING',
  APPRAISAL_FINALIZE: 'APPRAISAL_FINALIZE',
  APPRAISAL_RULE_WRITE: 'APPRAISAL_RULE_WRITE',
  YEAR_END_READ: 'YEAR_END_READ',
  YEAR_END_WRITE: 'YEAR_END_WRITE',
  YEAR_END_FINALIZE: 'YEAR_END_FINALIZE',
  VENDOR_PAYMENT_READ: 'VENDOR_PAYMENT_READ',
  VENDOR_PAYMENT_WRITE: 'VENDOR_PAYMENT_WRITE',
} as const

export type PermissionName = typeof PERMISSION_NAMES[keyof typeof PERMISSION_NAMES]

// ROUTE_PERMISSION_RULES / PUBLIC_ROUTES / PUBLIC_ROUTE_PREFIXES / TEACHER_PORTAL_ROUTES
// 整段從原檔第 80-158 行**逐字複製**——已是字串，不動。
// 實作技巧：`git show HEAD:src/constants/permissions.ts | sed -n '80,158p'`
export const ROUTE_PERMISSION_RULES = [
  /* 80 行 path/permission 規則，逐字複製 */
]

export const PUBLIC_ROUTES = ['/login', '/change-password', '/portal/login', '/profile']
export const PUBLIC_ROUTE_PREFIXES = ['/public/']
export const TEACHER_PORTAL_ROUTES = [
  '/portal',
  '/portal/attendance',
  '/portal/leave',
  '/portal/overtime',
  '/portal/schedule',
  '/portal/anomalies',
  '/portal/students',
  '/portal/calendar',
  '/portal/salary',
  '/portal/announcements',
  '/portal/profile',
]
```

**注意：** ROUTE_PERMISSION_RULES、PUBLIC_ROUTES、TEACHER_PORTAL_ROUTES 整段照原檔複製，**不變**。

`PERMISSION_VALUES` 完全移除（caller side 改用 `PERMISSION_NAMES`，雖然兩者 key/value 同字串）。

- [ ] **Step 9.2: 跑 typecheck 看哪些檔還依賴 `PERMISSION_VALUES`**

```bash
cd ~/Desktop/ivy-frontend
npm run typecheck 2>&1 | head -50
```

預期：多個 error 指向 `PERMISSION_VALUES` 未匯出。這些檔案在 Task 11 修。

- [ ] **Step 9.3: 不 commit**

---

## Task 10: 前端 — `src/utils/auth.ts` 重寫 + 新增測試

**Files:**
- Modify: `src/utils/auth.ts`
- Create: `tests/utils/auth.test.ts`
- Create: `tests/utils/permissions-helpers.test.ts`

- [ ] **Step 10.1: 先寫測試**

`tests/utils/auth.test.ts`：

```ts
import { describe, it, expect, beforeEach } from 'vitest'
import {
  hasPermission,
  hasWritePermission,
  setUserInfo,
  clearAuth,
} from '@/utils/auth'

describe('hasPermission', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
  })

  it('returns false when no userInfo', () => {
    expect(hasPermission('EMPLOYEES_READ')).toBe(false)
  })

  it('returns false for teacher role regardless of permissions', () => {
    setUserInfo({ role: 'teacher', permission_names: ['*'] })
    expect(hasPermission('EMPLOYEES_READ')).toBe(false)
  })

  it('returns true for wildcard permission_names', () => {
    setUserInfo({ role: 'admin', permission_names: ['*'] })
    expect(hasPermission('EMPLOYEES_READ')).toBe(true)
    expect(hasPermission('NONEXISTENT_PERMISSION')).toBe(true)
  })

  it('returns true when name is in permission_names', () => {
    setUserInfo({ role: 'hr', permission_names: ['EMPLOYEES_READ', 'SALARY_WRITE'] })
    expect(hasPermission('EMPLOYEES_READ')).toBe(true)
    expect(hasPermission('SALARY_WRITE')).toBe(true)
  })

  it('returns false when name is not in permission_names', () => {
    setUserInfo({ role: 'hr', permission_names: ['EMPLOYEES_READ'] })
    expect(hasPermission('SALARY_WRITE')).toBe(false)
  })

  it('returns false when permission_names is null', () => {
    setUserInfo({ role: 'hr', permission_names: null })
    expect(hasPermission('EMPLOYEES_READ')).toBe(false)
  })

  it('returns false when permission_names is empty array', () => {
    setUserInfo({ role: 'parent', permission_names: [] })
    expect(hasPermission('EMPLOYEES_READ')).toBe(false)
  })
})

describe('hasWritePermission', () => {
  beforeEach(() => {
    localStorage.clear()
  })

  it('checks <MODULE>_WRITE', () => {
    setUserInfo({ role: 'hr', permission_names: ['EMPLOYEES_WRITE'] })
    expect(hasWritePermission('EMPLOYEES')).toBe(true)
    expect(hasWritePermission('SALARY')).toBe(false)
  })
})

describe('localStorage schema sniffer', () => {
  it('清掉舊版 userInfo（含 permissions 不含 permission_names）', () => {
    localStorage.setItem('userInfo', JSON.stringify({
      role: 'admin',
      permissions: -1,  // 舊版 schema
    }))
    // dynamic import 觸發 module-level sniff
    return import('@/utils/auth').then(() => {
      expect(localStorage.getItem('userInfo')).toBeNull()
    })
  })
})
```

`tests/utils/permissions-helpers.test.ts`：

```ts
import { describe, it, expect } from 'vitest'
import {
  permissionsHave,
  permissionsAdd,
  permissionsRemove,
  permissionsCombine,
} from '@/utils/auth'

describe('permissionsHave', () => {
  it('wildcard 一律 true', () => {
    expect(permissionsHave(['*'], 'ANY')).toBe(true)
  })

  it('命中', () => {
    expect(permissionsHave(['EMPLOYEES_READ'], 'EMPLOYEES_READ')).toBe(true)
  })

  it('miss', () => {
    expect(permissionsHave(['EMPLOYEES_READ'], 'SALARY_WRITE')).toBe(false)
  })

  it('空 array', () => {
    expect(permissionsHave([], 'X')).toBe(false)
  })
})

describe('permissionsAdd', () => {
  it('加入新項', () => {
    expect(permissionsAdd(['A'], 'B')).toEqual(['A', 'B'])
  })

  it('已存在不重複', () => {
    expect(permissionsAdd(['A', 'B'], 'A')).toEqual(['A', 'B'])
  })
})

describe('permissionsRemove', () => {
  it('移除存在', () => {
    expect(permissionsRemove(['A', 'B'], 'A')).toEqual(['B'])
  })

  it('不存在不報錯', () => {
    expect(permissionsRemove(['A'], 'B')).toEqual(['A'])
  })
})

describe('permissionsCombine', () => {
  it('多 array 合併去重', () => {
    expect(permissionsCombine([['A', 'B'], ['B', 'C']]).sort()).toEqual(['A', 'B', 'C'])
  })
})
```

- [ ] **Step 10.2: 跑測試確認失敗**

```bash
npm run test -- tests/utils/auth.test.ts tests/utils/permissions-helpers.test.ts
```

預期：test fail（新 API 不存在 / shape 不對）。

- [ ] **Step 10.3: 重寫 `src/utils/auth.ts`**

替換現有檔案的權限段，保留檔頭 import / shallowRef / SESSION 邏輯不動。**新版** `hasPermission` 與 helpers：

```ts
// 取代舊 hasPermission
export function hasPermission(permissionName: string): boolean {
  const userInfo = getUserInfo()
  if (!userInfo) return false
  if (userInfo['role'] === 'teacher') return false

  const perms = userInfo['permission_names'] as string[] | null | undefined
  if (perms == null) return false
  if (perms.includes('*')) return true
  return perms.includes(permissionName)
}

export function hasWritePermission(moduleName: string): boolean {
  return hasPermission(`${moduleName}_WRITE`)
}

// 取代 4 個 mask helper（改名 + 改實作）
export function permissionsHave(perms: string[] | null | undefined, name: string): boolean {
  if (!perms) return false
  if (perms.includes('*')) return true
  return perms.includes(name)
}

export function permissionsAdd(perms: string[], name: string): string[] {
  return Array.from(new Set([...perms, name]))
}

export function permissionsRemove(perms: string[], name: string): string[] {
  return perms.filter((p) => p !== name)
}

export function permissionsCombine(arrays: string[][]): string[] {
  return Array.from(new Set(arrays.flat()))
}

// 移除 _toBig + 4 個 permissionMaskHas/Add/Remove/Combine + 所有 BigInt 邏輯
```

並在 module top-level（`_readFromStorage` 之後）加：

```ts
// 啟動時偵測舊 schema localStorage，清掉避免類型錯亂
const _stored = _readFromStorage()
if (_stored && 'permissions' in _stored && !('permission_names' in _stored)) {
  localStorage.removeItem(USER_INFO_KEY)
  _userInfoRef.value = null
}
```

- [ ] **Step 10.4: 跑測試確認通過**

```bash
npm run test -- tests/utils/auth.test.ts tests/utils/permissions-helpers.test.ts
```

預期：全綠。

- [ ] **Step 10.5: 不 commit**

---

## Task 11: 前端 — Call site grep + 機械替換

**Files:**
- Modify: 約 6 個 .ts/.vue 檔（grep 結果為準）

- [ ] **Step 11.1: 找出所有 call site**

```bash
cd ~/Desktop/ivy-frontend
grep -rn 'permissionMaskHas\|permissionMaskAdd\|permissionMaskRemove\|permissionMaskCombine\|PERMISSION_VALUES\|userInfo\.permissions\b\|userInfo\[.permissions.\]' src/ --include="*.ts" --include="*.vue" -l
```

預期 ~6 個 file。

- [ ] **Step 11.2: 逐檔改名**

每檔做以下機械替換：

| 舊 | 新 |
|---|---|
| `permissionMaskHas(mask, value)` | `permissionsHave(perms, name)` |
| `permissionMaskAdd(mask, value)` | `permissionsAdd(perms, name)` |
| `permissionMaskRemove(mask, value)` | `permissionsRemove(perms, name)` |
| `permissionMaskCombine(values)` | `permissionsCombine(arrays)` |
| `PERMISSION_VALUES.X` | `PERMISSION_NAMES.X`（仍是字串） |
| `PERMISSION_VALUES[name]` | `PERMISSION_NAMES[name]` |
| `userInfo.permissions` | `userInfo.permission_names` |
| `userInfo['permissions']` | `userInfo['permission_names']` |

注意：caller 端參數 type 從 `number` 改 `string[]`，呼叫處可能需要從 store / userInfo 取 `permission_names` 而非 `permissions`。

- [ ] **Step 11.3: 跑 typecheck**

```bash
npm run typecheck
```

預期：0 error。

- [ ] **Step 11.4: 跑全套 vitest**

```bash
npm run test 2>&1 | tail -10
```

預期：2349/2349 全綠（main 基準）。

- [ ] **Step 11.5: 跑 build**

```bash
npm run build 2>&1 | tail -5
```

預期：build 成功。

---

## Task 12: 前端 — 整批 commit

- [ ] **Step 12.1: 確認 diff**

```bash
cd ~/Desktop/ivy-frontend/.claude/worktrees/permission-text-array-2026-05-21-frontend
git status
git diff --stat
```

預期：~10 個檔（constants/permissions.ts、utils/auth.ts、6 callers、2 新 test）。

- [ ] **Step 12.2: 一次 commit**

```bash
git add src/constants/permissions.ts src/utils/auth.ts \
        src/components/ src/composables/ src/views/ \
        tests/utils/auth.test.ts tests/utils/permissions-helpers.test.ts

git commit -m "refactor(permissions): bigint mask → string[] permission_names

對齊後端 utils/permissions.py 重構：

- PERMISSION_VALUES (number) → PERMISSION_NAMES (string const)
- hasPermission 走 includes，移除 BigInt 邏輯
- permissionMaskHas/Add/Remove/Combine → permissionsHave/Add/Remove/Combine
  (簽章從 number 改 string[])
- 啟動偵測舊 localStorage schema 自動清除，避免類型錯亂
- 新增 tests/utils/auth.test.ts + permissions-helpers.test.ts

Spec: ivy-backend/docs/superpowers/specs/2026-05-21-permission-intflag-split-design.md
"
```

- [ ] **Step 12.3: 確認 commit**

```bash
git log -1 --stat | head -30
```

---

## Task 13: 整合驗證

- [ ] **Step 13.1: 啟動兩端 dev server**

```bash
cd ~/Desktop/ivyManageSystem && ./start.sh
# tail -f .scratch/logs/backend.log &
```

確認後端 listening on 8088、前端 listening on 5173。

- [ ] **Step 13.2: admin 登入 smoke**

開 http://localhost:5173，用 admin 帳號登入：
- 確認 sidebar 所有選單可見
- 點任一頁面 → 載入無 500 / 401

- [ ] **Step 13.3: hr 登入 smoke**

切到 hr 帳號（或在 user 設定頁建一個）：
- sidebar 只看到 ROLE_TEMPLATES['hr'] 內的選單
- 試圖訪問 `/students`（無權）→ redirect login or 拒絕

- [ ] **Step 13.4: teacher 登入 smoke**

teacher 帳號：
- 自動轉 `/portal`
- 試圖訪問 `/employees` → hard redirect

- [ ] **Step 13.5: user 設定頁勾權限驗證**

以 admin 進「系統設定 / 帳號管理」：
- 選一個 hr user，勾掉「員工管理 (檢視)」
- save
- 切到該 user 登入 → 員工管理頁面不可見

- [ ] **Step 13.6: 跨版本 localStorage 驗證**

開 DevTools / Application / Storage / Local Storage：
- 手動把 userInfo 改成舊 schema（`{"role":"admin","permissions":-1}`，刪掉 permission_names）
- F5 reload
- 預期：自動清掉、redirect /login

- [ ] **Step 13.7: 後端 alembic upgrade 完整重跑驗證**

確認 dev DB 仍能 downgrade + upgrade 不報錯：

```bash
cd ~/Desktop/ivy-backend
alembic downgrade -1
# 確認 users.permissions 出現、users.permission_names 消失
psql ivymanagement -c "\d users" | grep -E "permission"

alembic upgrade head
psql ivymanagement -c "\d users" | grep -E "permission"
# 確認 permission_names 回來、permissions 消失
```

- [ ] **Step 13.8: 完成**

所有 smoke 通過後，本實作完成。merge 前再走一輪：
- 兩 repo PR 開出
- CI 過
- user 接手 review + merge

---

## 失敗情境處理

### 如果 Task 1–8 過程中 pytest 一直紅

通常是測試 fixture 殘留舊 `permissions=` 設值。檢查：

```bash
grep -rn 'permissions\s*=' tests/ --include="*.py" | grep -v 'permission_names\|comment\|"#'
```

把 `permissions=X` 全換成 `permission_names=`。

### 如果 Task 7 漏改某個 router

跑全套 pytest 時會抓到，因為對應 endpoint 測試會 fail。看 traceback 指向哪行，補改。

### 如果 alembic upgrade 在 dev DB 卡住

通常是 prior heads 不是預期值。`alembic current` 看當前位置，必要時 `alembic stamp head` 後手動 SQL fix。**不要動 prod**。

### 如果整合驗證時 user 重登後仍看到舊權限

檢查：
- 後端是否真的 issue 新 `permission_names` claim（curl `/api/auth/me` 看 response）
- 前端 userInfo 是否更新（DevTools localStorage）
- token_version migration 是否真的執行（`SELECT id, token_version FROM users LIMIT 5`）

---

## Out of scope（不在本 plan 內）

- LEGACY_PERMISSION_BITS 從 utils/permissions.py 移除（prod 穩定一陣後 followup）
- Permission.__str__ / __repr__ 加工（不需要）
- GIN index on permission_names（無 SQL query 需求）
- ROLE_TEMPLATES 進 DB（與本重構無關）
