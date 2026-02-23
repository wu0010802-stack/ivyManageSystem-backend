"""
Permission definitions for fine-grained access control
位元遮罩權限系統
"""

from enum import IntFlag
from typing import List, Dict


class Permission(IntFlag):
    """功能模組權限位元定義"""
    DASHBOARD = 1 << 0          # 儀表板
    APPROVALS = 1 << 1          # 審核工作台
    CALENDAR = 1 << 2           # 行事曆
    SCHEDULE = 1 << 3           # 排班管理
    ATTENDANCE = 1 << 4         # 出勤管理
    LEAVES = 1 << 5             # 請假管理
    OVERTIME = 1 << 6           # 加班管理
    MEETINGS = 1 << 7           # 園務會議
    EMPLOYEES = 1 << 8          # 員工管理
    STUDENTS = 1 << 9           # 學生管理
    CLASSROOMS = 1 << 10        # 班級管理
    SALARY = 1 << 11            # 薪資管理
    ANNOUNCEMENTS = 1 << 12     # 公告管理
    REPORTS = 1 << 13           # 報表統計
    AUDIT_LOGS = 1 << 14        # 操作紀錄
    SETTINGS = 1 << 15          # 系統設定
    USER_MANAGEMENT = 1 << 16   # 帳號管理

    # 全部權限
    ALL = 0xFFFFFFFFFFFFFFFF


# ---------------------------------------------------------------------------
# RBAC 角色模板
# ---------------------------------------------------------------------------

ROLE_TEMPLATES: Dict[str, int] = {
    "admin": -1,  # 全部權限
    "hr": (
        Permission.DASHBOARD |
        Permission.EMPLOYEES |
        Permission.SALARY |
        Permission.ATTENDANCE |
        Permission.LEAVES |
        Permission.OVERTIME |
        Permission.REPORTS
    ),
    "supervisor": (
        Permission.DASHBOARD |
        Permission.APPROVALS |
        Permission.CALENDAR |
        Permission.SCHEDULE |
        Permission.ATTENDANCE |
        Permission.LEAVES |
        Permission.OVERTIME |
        Permission.MEETINGS |
        Permission.STUDENTS |
        Permission.CLASSROOMS |
        Permission.REPORTS
    ),
    "teacher": (
        Permission.DASHBOARD |
        Permission.CALENDAR |
        Permission.LEAVES |
        Permission.OVERTIME |
        Permission.ANNOUNCEMENTS
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
    "DASHBOARD": "儀表板",
    "APPROVALS": "審核工作台",
    "CALENDAR": "行事曆",
    "SCHEDULE": "排班管理",
    "ATTENDANCE": "出勤管理",
    "LEAVES": "請假管理",
    "OVERTIME": "加班管理",
    "MEETINGS": "園務會議",
    "EMPLOYEES": "員工管理",
    "STUDENTS": "學生管理",
    "CLASSROOMS": "班級管理",
    "SALARY": "薪資管理",
    "ANNOUNCEMENTS": "公告管理",
    "REPORTS": "報表統計",
    "AUDIT_LOGS": "操作紀錄",
    "SETTINGS": "系統設定",
    "USER_MANAGEMENT": "帳號管理",
}

# 權限分組 (供前端 UI 使用)
PERMISSION_GROUPS: List[Dict] = [
    {
        "name": "首頁",
        "permissions": ["DASHBOARD", "APPROVALS"],
    },
    {
        "name": "考勤管理",
        "permissions": ["CALENDAR", "SCHEDULE", "ATTENDANCE", "LEAVES", "OVERTIME", "MEETINGS"],
    },
    {
        "name": "人事教務",
        "permissions": ["EMPLOYEES", "STUDENTS", "CLASSROOMS", "SALARY"],
    },
    {
        "name": "園務行政",
        "permissions": ["ANNOUNCEMENTS", "REPORTS", "AUDIT_LOGS"],
    },
    {
        "name": "系統",
        "permissions": ["SETTINGS", "USER_MANAGEMENT"],
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
    }
