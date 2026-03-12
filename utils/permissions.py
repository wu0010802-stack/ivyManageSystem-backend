"""
Permission definitions for fine-grained access control
位元遮罩權限系統（讀寫分離版）
"""

from enum import IntFlag
from typing import List, Dict


class Permission(IntFlag):
    """功能模組權限位元定義"""
    # --- 不拆分的模組 (原位保留) ---
    DASHBOARD = 1 << 0          # 儀表板
    APPROVALS = 1 << 1          # 審核工作台
    CALENDAR = 1 << 2           # 行事曆
    SCHEDULE = 1 << 3           # 排班管理
    MEETINGS = 1 << 7           # 園務會議
    REPORTS = 1 << 13           # 報表統計
    AUDIT_LOGS = 1 << 14        # 操作紀錄

    # --- 讀寫分離模組：READ 保留原位，WRITE 使用高位 ---
    ATTENDANCE_READ = 1 << 4          # 出勤管理 (檢視)
    ATTENDANCE_WRITE = 1 << 17        # 出勤管理 (編輯)
    LEAVES_READ = 1 << 5             # 請假管理 (檢視)
    LEAVES_WRITE = 1 << 18           # 請假管理 (編輯)
    OVERTIME_READ = 1 << 6           # 加班管理 (檢視)
    OVERTIME_WRITE = 1 << 19         # 加班管理 (編輯)
    EMPLOYEES_READ = 1 << 8         # 員工管理 (檢視)
    EMPLOYEES_WRITE = 1 << 20       # 員工管理 (編輯)
    STUDENTS_READ = 1 << 9          # 學生管理 (檢視)
    STUDENTS_WRITE = 1 << 21        # 學生管理 (編輯)
    CLASSROOMS_READ = 1 << 10       # 班級管理 (檢視)
    CLASSROOMS_WRITE = 1 << 22      # 班級管理 (編輯)
    SALARY_READ = 1 << 11           # 薪資管理 (檢視)
    SALARY_WRITE = 1 << 23          # 薪資管理 (編輯)
    ANNOUNCEMENTS_READ = 1 << 12    # 公告管理 (檢視)
    ANNOUNCEMENTS_WRITE = 1 << 24   # 公告管理 (編輯)
    SETTINGS_READ = 1 << 15         # 系統設定 (檢視)
    SETTINGS_WRITE = 1 << 25        # 系統設定 (編輯)
    USER_MANAGEMENT_READ = 1 << 16  # 帳號管理 (檢視)
    USER_MANAGEMENT_WRITE = 1 << 26 # 帳號管理 (編輯)

    # 全部權限
    ALL = 0xFFFFFFFFFFFFFFFF


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
}

# READ → WRITE 位元對照（供遷移用）
_RW_PAIRS: List[tuple] = [
    (Permission.ATTENDANCE_READ, Permission.ATTENDANCE_WRITE),
    (Permission.LEAVES_READ, Permission.LEAVES_WRITE),
    (Permission.OVERTIME_READ, Permission.OVERTIME_WRITE),
    (Permission.EMPLOYEES_READ, Permission.EMPLOYEES_WRITE),
    (Permission.STUDENTS_READ, Permission.STUDENTS_WRITE),
    (Permission.CLASSROOMS_READ, Permission.CLASSROOMS_WRITE),
    (Permission.SALARY_READ, Permission.SALARY_WRITE),
    (Permission.ANNOUNCEMENTS_READ, Permission.ANNOUNCEMENTS_WRITE),
    (Permission.SETTINGS_READ, Permission.SETTINGS_WRITE),
    (Permission.USER_MANAGEMENT_READ, Permission.USER_MANAGEMENT_WRITE),
]


# ---------------------------------------------------------------------------
# RBAC 角色模板
# ---------------------------------------------------------------------------

ROLE_TEMPLATES: Dict[str, int] = {
    "admin": -1,  # 全部權限
    "hr": (
        Permission.DASHBOARD |
        Permission.EMPLOYEES_READ | Permission.EMPLOYEES_WRITE |
        Permission.SALARY_READ | Permission.SALARY_WRITE |
        Permission.ATTENDANCE_READ | Permission.ATTENDANCE_WRITE |
        Permission.LEAVES_READ | Permission.LEAVES_WRITE |
        Permission.OVERTIME_READ | Permission.OVERTIME_WRITE |
        Permission.REPORTS
    ),
    "supervisor": (
        Permission.DASHBOARD |
        Permission.APPROVALS |
        Permission.CALENDAR |
        Permission.SCHEDULE |
        Permission.ATTENDANCE_READ | Permission.ATTENDANCE_WRITE |
        Permission.LEAVES_READ | Permission.LEAVES_WRITE |
        Permission.OVERTIME_READ | Permission.OVERTIME_WRITE |
        Permission.MEETINGS |
        Permission.STUDENTS_READ | Permission.STUDENTS_WRITE |
        Permission.CLASSROOMS_READ | Permission.CLASSROOMS_WRITE |
        Permission.REPORTS
    ),
    "teacher": (
        Permission.DASHBOARD |
        Permission.CALENDAR |
        Permission.ANNOUNCEMENTS_READ  # 教師僅可檢視公告
    ),
}

# 角色名稱對照表
ROLE_LABELS: Dict[str, str] = {
    "admin": "系統管理員",
    "hr": "人事管理員",
    "supervisor": "主管",
    "teacher": "教師",
}


def get_role_default_permissions(role: str) -> int:
    """取得角色的預設權限"""
    return ROLE_TEMPLATES.get(role, ROLE_TEMPLATES["teacher"])


# 權限名稱對照表 (供前端使用)
PERMISSION_LABELS: Dict[str, str] = {
    # 不拆分的模組
    "DASHBOARD": "儀表板",
    "APPROVALS": "審核工作台",
    "CALENDAR": "行事曆",
    "SCHEDULE": "排班管理",
    "MEETINGS": "園務會議",
    "REPORTS": "報表統計",
    "AUDIT_LOGS": "操作紀錄",
    # 讀寫分離模組
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
}

# 權限分組 (供前端 UI 使用)
# permissions: 不拆分的單一權限
# split_permissions: 讀寫配對（module=顯示名稱, read/write=權限 key）
PERMISSION_GROUPS: List[Dict] = [
    {
        "name": "首頁",
        "permissions": ["DASHBOARD", "APPROVALS"],
    },
    {
        "name": "考勤管理",
        "permissions": ["CALENDAR", "SCHEDULE", "MEETINGS"],
        "split_permissions": [
            {"module": "出勤管理", "read": "ATTENDANCE_READ", "write": "ATTENDANCE_WRITE"},
            {"module": "請假管理", "read": "LEAVES_READ", "write": "LEAVES_WRITE"},
            {"module": "加班管理", "read": "OVERTIME_READ", "write": "OVERTIME_WRITE"},
        ],
    },
    {
        "name": "人事教務",
        "permissions": [],
        "split_permissions": [
            {"module": "員工管理", "read": "EMPLOYEES_READ", "write": "EMPLOYEES_WRITE"},
            {"module": "學生管理", "read": "STUDENTS_READ", "write": "STUDENTS_WRITE"},
            {"module": "班級管理", "read": "CLASSROOMS_READ", "write": "CLASSROOMS_WRITE"},
            {"module": "薪資管理", "read": "SALARY_READ", "write": "SALARY_WRITE"},
        ],
    },
    {
        "name": "園務行政",
        "permissions": ["REPORTS", "AUDIT_LOGS"],
        "split_permissions": [
            {"module": "公告管理", "read": "ANNOUNCEMENTS_READ", "write": "ANNOUNCEMENTS_WRITE"},
        ],
    },
    {
        "name": "系統",
        "permissions": [],
        "split_permissions": [
            {"module": "系統設定", "read": "SETTINGS_READ", "write": "SETTINGS_WRITE"},
            {"module": "帳號管理", "read": "USER_MANAGEMENT_READ", "write": "USER_MANAGEMENT_WRITE"},
        ],
    },
]


def get_permission_value(name: str) -> int:
    """根據權限名稱取得位元值"""
    try:
        return Permission[name].value
    except KeyError:
        return 0


def has_permission(user_permissions: int, required: Permission) -> bool:
    """檢查使用者是否擁有指定權限"""
    if user_permissions == -1:  # -1 表示全部權限
        return True
    return (user_permissions & required.value) == required.value


def get_permission_list(permissions_mask: int) -> List[str]:
    """將位元遮罩轉換為權限名稱列表"""
    if permissions_mask == -1:
        return list(PERMISSION_LABELS.keys())

    result = []
    for perm in Permission:
        if perm == Permission.ALL:
            continue
        if (permissions_mask & perm.value) == perm.value:
            result.append(perm.name)
    return result


def get_permissions_definition() -> Dict:
    """取得完整權限定義供前端使用"""
    permissions = {}
    for perm in Permission:
        if perm == Permission.ALL:
            continue
        permissions[perm.name] = {
            "value": perm.value,
            "label": PERMISSION_LABELS.get(perm.name, perm.name),
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
