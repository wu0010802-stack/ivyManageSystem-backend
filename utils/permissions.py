"""
Permission definitions for fine-grained access control
（text[] 版本，2026-05-21 重構：脫離 64-bit IntFlag 容量限制）
"""

import logging
from enum import Enum
from typing import List, Dict, NamedTuple, Optional

WILDCARD = "*"

_logger = logging.getLogger(__name__)


class Permission(str, Enum):
    """權限識別字串（繼承 str：perm.value == "EMPLOYEES_READ"）。

    位元值已搬到 LEGACY_PERMISSION_BITS，僅 alembic migration 使用，
    runtime 不參考。
    """

    DASHBOARD = "DASHBOARD"
    APPROVALS = "APPROVALS"
    CALENDAR = "CALENDAR"
    SCHEDULE = "SCHEDULE"
    MEETINGS = "MEETINGS"
    REPORTS = "REPORTS"
    AUDIT_LOGS = "AUDIT_LOGS"

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

    STUDENTS_LIFECYCLE_WRITE = "STUDENTS_LIFECYCLE_WRITE"
    GUARDIANS_READ = "GUARDIANS_READ"
    GUARDIANS_WRITE = "GUARDIANS_WRITE"
    RECRUITMENT_CONVERT = "RECRUITMENT_CONVERT"
    BUSINESS_ANALYTICS = "BUSINESS_ANALYTICS"

    PORTFOLIO_READ = "PORTFOLIO_READ"
    PORTFOLIO_WRITE = "PORTFOLIO_WRITE"
    PORTFOLIO_PUBLISH = "PORTFOLIO_PUBLISH"
    STUDENTS_HEALTH_READ = "STUDENTS_HEALTH_READ"
    STUDENTS_HEALTH_WRITE = "STUDENTS_HEALTH_WRITE"
    STUDENTS_MEDICATION_ADMINISTER = "STUDENTS_MEDICATION_ADMINISTER"
    STUDENTS_SPECIAL_NEEDS_READ = "STUDENTS_SPECIAL_NEEDS_READ"
    STUDENTS_SPECIAL_NEEDS_WRITE = "STUDENTS_SPECIAL_NEEDS_WRITE"
    STUDENTS_IEP_APPROVE = "STUDENTS_IEP_APPROVE"

    PARENT_MESSAGES_WRITE = "PARENT_MESSAGES_WRITE"

    GOV_REPORTS_VIEW = "GOV_REPORTS_VIEW"
    GOV_REPORTS_EXPORT = "GOV_REPORTS_EXPORT"

    APPRAISAL_READ = "APPRAISAL_READ"
    APPRAISAL_EVENT_WRITE = "APPRAISAL_EVENT_WRITE"
    APPRAISAL_REVIEW = "APPRAISAL_REVIEW"
    APPRAISAL_ACCOUNTING = "APPRAISAL_ACCOUNTING"
    APPRAISAL_FINALIZE = "APPRAISAL_FINALIZE"
    APPRAISAL_RULE_WRITE = "APPRAISAL_RULE_WRITE"

    YEAR_END_READ = "YEAR_END_READ"
    YEAR_END_WRITE = "YEAR_END_WRITE"
    YEAR_END_FINALIZE = "YEAR_END_FINALIZE"

    VENDOR_PAYMENT_READ = "VENDOR_PAYMENT_READ"
    VENDOR_PAYMENT_WRITE = "VENDOR_PAYMENT_WRITE"

    # DB-driven 自訂權限/角色 CRUD 守衛（(b) 子專案）
    ROLES_MANAGE = "ROLES_MANAGE"

    # 教師端後台檢視：預覽（唯讀）/ 代為操作（可寫）
    PORTAL_PREVIEW = "PORTAL_PREVIEW"
    PORTAL_IMPERSONATE = "PORTAL_IMPERSONATE"

    # 個資法 DSR（個人資料主體權利）申請審核
    DSR_MANAGE = "DSR_MANAGE"

    # 資料品質報告（PR-B 2026-05-29）
    DATA_QUALITY_READ = "DATA_QUALITY_READ"
    DATA_QUALITY_WRITE = "DATA_QUALITY_WRITE"


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
    "USER_MANAGEMENT": {
        "read": "USER_MANAGEMENT_READ",
        "write": "USER_MANAGEMENT_WRITE",
    },
    "ACTIVITY": {"read": "ACTIVITY_READ", "write": "ACTIVITY_WRITE"},
    "DISMISSAL_CALLS": {
        "read": "DISMISSAL_CALLS_READ",
        "write": "DISMISSAL_CALLS_WRITE",
    },
    "FEES": {"read": "FEES_READ", "write": "FEES_WRITE"},
    "RECRUITMENT": {"read": "RECRUITMENT_READ", "write": "RECRUITMENT_WRITE"},
    "GUARDIANS": {"read": "GUARDIANS_READ", "write": "GUARDIANS_WRITE"},
    "APPRAISAL": {"read": "APPRAISAL_READ", "write": "APPRAISAL_EVENT_WRITE"},
    "YEAR_END": {"read": "YEAR_END_READ", "write": "YEAR_END_WRITE"},
    "VENDOR_PAYMENT": {
        "read": "VENDOR_PAYMENT_READ",
        "write": "VENDOR_PAYMENT_WRITE",
    },
}


# ---------------------------------------------------------------------------
# RBAC 角色模板（text[] 版本：list[str]，admin 為 ["*"] wildcard）
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
        # 教職員考核：人事/會計（核數字）
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_ACCOUNTING.value,
        # 年終獎金：人事可檢視與編輯（會計核數字流程）
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        # 廠商付款：HR 兼採購行政
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
        # Portfolio (supervisor 含發佈與健康編輯權限)
        Permission.PORTFOLIO_READ.value,
        Permission.PORTFOLIO_WRITE.value,
        Permission.PORTFOLIO_PUBLISH.value,
        Permission.STUDENTS_HEALTH_READ.value,
        Permission.STUDENTS_HEALTH_WRITE.value,
        Permission.STUDENTS_MEDICATION_ADMINISTER.value,
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value,
        Permission.STUDENTS_SPECIAL_NEEDS_WRITE.value,
        Permission.STUDENTS_IEP_APPROVE.value,  # 主任以上批核/結案 IEP（取代 supervisor_role 旁路）
        # 家園溝通平台
        Permission.PARENT_MESSAGES_WRITE.value,
        # 教育部申報模組：主管可檢視（不可匯出）
        Permission.GOV_REPORTS_VIEW.value,
        # 教職員考核：主管全程權限（評分+簽核+核定）
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
        Permission.APPRAISAL_REVIEW.value,
        Permission.APPRAISAL_FINALIZE.value,
        Permission.APPRAISAL_RULE_WRITE.value,
        # 年終獎金：主管全程權限
        Permission.YEAR_END_READ.value,
        Permission.YEAR_END_WRITE.value,
        Permission.YEAR_END_FINALIZE.value,
        # 廠商付款：主管全程權限
        Permission.VENDOR_PAYMENT_READ.value,
        Permission.VENDOR_PAYMENT_WRITE.value,
    ],
    "teacher": [
        Permission.DASHBOARD.value,
        Permission.CALENDAR.value,
        Permission.ANNOUNCEMENTS_READ.value,
        # class-scoped：教師僅限自班，須帶 :own_class（對齊 DB roles 表 + permscope01-04）。
        # 此處若用 bare code，resolve_grant 會解析成 :all → is_unrestricted 放行全園 →
        # 對 permission_names=NULL（走本模板）的教師形同關閉 row-level scoping（提權）。
        Permission.DISMISSAL_CALLS_READ.value + ":own_class",
        Permission.DISMISSAL_CALLS_WRITE.value + ":own_class",
        Permission.PORTFOLIO_READ.value + ":own_class",
        Permission.PORTFOLIO_WRITE.value + ":own_class",
        # portal 事件紀錄/學期評量走 require_permission(STUDENTS_READ/WRITE)；須 :own_class
        # （bare 會 resolve 成 :all → 對 NULL-perm 教師提權全園）。端點皆 own_class
        # self-filter，主管理端點走 require_staff_permission 對 teacher 一律 403。
        Permission.STUDENTS_READ.value + ":own_class",
        Permission.STUDENTS_WRITE.value + ":own_class",
        Permission.STUDENTS_HEALTH_READ.value + ":own_class",
        Permission.STUDENTS_MEDICATION_ADMINISTER.value + ":own_class",
        Permission.STUDENTS_SPECIAL_NEEDS_READ.value + ":own_class",
        Permission.PARENT_MESSAGES_WRITE.value,
        Permission.APPRAISAL_READ.value,
        Permission.APPRAISAL_EVENT_WRITE.value,
    ],
    # 家長角色：恆無任何 Permission；資源存取一律由 user_id → guardians 過濾
    "parent": [],
}


# 角色名稱對照表
ROLE_LABELS: Dict[str, str] = {
    "admin": "系統管理員",
    "hr": "人事管理員",
    "supervisor": "主管",
    "teacher": "教師",
    "parent": "家長",
}

# principal 角色：supervisor 全部 + 薪資審視 + 稽核 + 政府報表匯出 + 預覽教師端 + 資料品質報告
ROLE_TEMPLATES["principal"] = ROLE_TEMPLATES["supervisor"] + [
    Permission.SALARY_READ.value,
    Permission.AUDIT_LOGS.value,
    Permission.GOV_REPORTS_EXPORT.value,
    Permission.PORTAL_PREVIEW.value,  # 園長可預覽老師教師端（唯讀）
    Permission.DATA_QUALITY_READ.value,
    Permission.DATA_QUALITY_WRITE.value,
    # C13：園長須真正涵蓋教師全部能力，PORTAL_PREVIEW 守衛（permissions_subset）才
    # 不會把合法的園長→教師預覽誤擋。supervisor 已含 STUDENTS/PORTFOLIO/HEALTH 的
    # bare（全園）形式（superset 教師 :own_class），唯這 3 條原本是教師獨有缺口：
    Permission.ANNOUNCEMENTS_READ.value,
    Permission.DISMISSAL_CALLS_READ.value,  # bare（全園）superset 教師 :own_class
    Permission.DISMISSAL_CALLS_WRITE.value,
]
ROLE_LABELS["principal"] = "園長"

# accountant 角色：純財務，13 條，不含 EMPLOYEES_WRITE
ROLE_TEMPLATES["accountant"] = [
    Permission.DASHBOARD.value,
    Permission.REPORTS.value,
    Permission.GOV_REPORTS_VIEW.value,
    Permission.EMPLOYEES_READ.value,  # 要看誰可申報薪資（不含 WRITE）
    Permission.SALARY_READ.value,
    Permission.SALARY_WRITE.value,
    Permission.FEES_READ.value,
    Permission.FEES_WRITE.value,
    Permission.VENDOR_PAYMENT_READ.value,
    Permission.VENDOR_PAYMENT_WRITE.value,
    Permission.YEAR_END_READ.value,
    Permission.YEAR_END_WRITE.value,  # 不含 FINALIZE（簽核屬 supervisor/principal）
    Permission.APPRAISAL_ACCOUNTING.value,  # 核考核獎金數字
]
ROLE_LABELS["accountant"] = "會計"

# 角色說明（給前端 SettingsUsersTab 卡片顯示）
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "admin": "唯一能改帳號、系統設定",
    "principal": "業務全包 + 薪資審視，不動帳號",
    "supervisor": "教務管理、招生轉換、考核全程",
    "hr": "員工資料、薪資發放、年終、廠商付款",
    "accountant": "純財務（薪資/學費/廠商/年終）",
    "teacher": "公告、考勤、放學接送、學生檔案",
    "parent": "家長端登入，無管理端權限",
}


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
    # Portfolio
    "PORTFOLIO_READ": "成長歷程 (檢視)",
    "PORTFOLIO_WRITE": "成長歷程 (編輯)",
    "PORTFOLIO_PUBLISH": "學期報告 (發佈)",
    "STUDENTS_HEALTH_READ": "健康資訊 (檢視)",
    "STUDENTS_HEALTH_WRITE": "健康資訊 (編輯)",
    "STUDENTS_MEDICATION_ADMINISTER": "餵藥執行與紀錄",
    "STUDENTS_SPECIAL_NEEDS_READ": "特殊需求 (檢視)",
    "STUDENTS_SPECIAL_NEEDS_WRITE": "特殊需求 (編輯 / IEP)",
    "STUDENTS_IEP_APPROVE": "IEP 批核 / 結案",
    # 家園溝通平台
    "PARENT_MESSAGES_WRITE": "家長訊息 (發送/回覆)",
    # 教育部申報模組
    "GOV_REPORTS_VIEW": "政府申報資料 (檢視)",
    "GOV_REPORTS_EXPORT": "政府申報匯出 (執行)",
    # 教職員考核
    "APPRAISAL_READ": "考核資料 (檢視)",
    "APPRAISAL_EVENT_WRITE": "考核事件 (登錄)",
    "APPRAISAL_REVIEW": "考核簽核 (主管第一階)",
    "APPRAISAL_ACCOUNTING": "考核核數字 (會計第二階)",
    "APPRAISAL_FINALIZE": "考核核定 (最高主管第三階)",
    "APPRAISAL_RULE_WRITE": "考核扣分規則設定 (Phase 1 calibrate)",
    # 年終獎金結算
    "YEAR_END_READ": "年終結算 (檢視)",
    "YEAR_END_WRITE": "年終結算 (編輯)",
    "YEAR_END_FINALIZE": "年終核定 (最高主管)",
    # 廠商付款簽收
    "VENDOR_PAYMENT_READ": "廠商付款簽收 (檢視)",
    "VENDOR_PAYMENT_WRITE": "廠商付款簽收 (編輯/簽收)",
    # DB-driven 自訂權限/角色 CRUD 守衛 ((b) 子專案)
    "ROLES_MANAGE": "角色與權限管理",
    "PORTAL_PREVIEW": "預覽教師端",
    "PORTAL_IMPERSONATE": "代為操作教師端",
    # 個資法 DSR 申請審核
    "DSR_MANAGE": "個資權利請求管理",
    # 資料品質報告 (PR-B 2026-05-29)
    "DATA_QUALITY_READ": "資料品質報告 — 檢視",
    "DATA_QUALITY_WRITE": "資料品質報告 — 處理",
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
            {
                "module": "出勤管理",
                "read": "ATTENDANCE_READ",
                "write": "ATTENDANCE_WRITE",
            },
            {"module": "請假管理", "read": "LEAVES_READ", "write": "LEAVES_WRITE"},
            {"module": "加班管理", "read": "OVERTIME_READ", "write": "OVERTIME_WRITE"},
        ],
    },
    {
        "name": "人事教務",
        "permissions": [
            "ACTIVITY_PAYMENT_APPROVE",
            "STUDENTS_LIFECYCLE_WRITE",
            "RECRUITMENT_CONVERT",
        ],
        "split_permissions": [
            {
                "module": "員工管理",
                "read": "EMPLOYEES_READ",
                "write": "EMPLOYEES_WRITE",
            },
            {"module": "學生管理", "read": "STUDENTS_READ", "write": "STUDENTS_WRITE"},
            {
                "module": "監護人資料",
                "read": "GUARDIANS_READ",
                "write": "GUARDIANS_WRITE",
            },
            {
                "module": "班級管理",
                "read": "CLASSROOMS_READ",
                "write": "CLASSROOMS_WRITE",
            },
            {"module": "薪資管理", "read": "SALARY_READ", "write": "SALARY_WRITE"},
            {"module": "課後才藝", "read": "ACTIVITY_READ", "write": "ACTIVITY_WRITE"},
            {
                "module": "接送通知",
                "read": "DISMISSAL_CALLS_READ",
                "write": "DISMISSAL_CALLS_WRITE",
            },
            {"module": "學費管理", "read": "FEES_READ", "write": "FEES_WRITE"},
            {
                "module": "招生統計",
                "read": "RECRUITMENT_READ",
                "write": "RECRUITMENT_WRITE",
            },
        ],
    },
    {
        "name": "教職員考核",
        "permissions": [
            "APPRAISAL_REVIEW",
            "APPRAISAL_ACCOUNTING",
            "APPRAISAL_FINALIZE",
            "YEAR_END_FINALIZE",
        ],
        "split_permissions": [
            {
                "module": "考核資料",
                "read": "APPRAISAL_READ",
                "write": "APPRAISAL_EVENT_WRITE",
            },
            {
                "module": "年終結算",
                "read": "YEAR_END_READ",
                "write": "YEAR_END_WRITE",
            },
        ],
    },
    {
        "name": "園務行政",
        "permissions": [
            "REPORTS",
            "AUDIT_LOGS",
            "BUSINESS_ANALYTICS",
            "PARENT_MESSAGES_WRITE",
            "GOV_REPORTS_VIEW",
            "GOV_REPORTS_EXPORT",
        ],
        "split_permissions": [
            {
                "module": "公告管理",
                "read": "ANNOUNCEMENTS_READ",
                "write": "ANNOUNCEMENTS_WRITE",
            },
            {
                "module": "廠商付款簽收",
                "read": "VENDOR_PAYMENT_READ",
                "write": "VENDOR_PAYMENT_WRITE",
            },
        ],
    },
    {
        "name": "成長歷程 / 教務",
        "permissions": [
            "PORTFOLIO_PUBLISH",
            "STUDENTS_MEDICATION_ADMINISTER",
        ],
        "split_permissions": [
            {
                "module": "成長歷程",
                "read": "PORTFOLIO_READ",
                "write": "PORTFOLIO_WRITE",
            },
            {
                "module": "健康資訊",
                "read": "STUDENTS_HEALTH_READ",
                "write": "STUDENTS_HEALTH_WRITE",
            },
            {
                "module": "特殊需求",
                "read": "STUDENTS_SPECIAL_NEEDS_READ",
                "write": "STUDENTS_SPECIAL_NEEDS_WRITE",
            },
        ],
    },
    {
        "name": "系統",
        "permissions": [],
        "split_permissions": [
            {"module": "系統設定", "read": "SETTINGS_READ", "write": "SETTINGS_WRITE"},
            {
                "module": "帳號管理",
                "read": "USER_MANAGEMENT_READ",
                "write": "USER_MANAGEMENT_WRITE",
            },
        ],
    },
]


# ---------------------------------------------------------------------------
# Runtime helpers（text[] 版本）
# ---------------------------------------------------------------------------


def get_role_default_permissions(session, role_code: str) -> List[str]:
    """從 DB roles 表拉指定 role 的預設 permissions。

    fallback：未知 role 回 teacher 預設（既有行為）。
    """
    from models.permission_models import Role

    role = session.query(Role).filter_by(code=role_code).first()
    if role is None:
        teacher_role = session.query(Role).filter_by(code="teacher").first()
        return list(teacher_role.permissions) if teacher_role else []
    return list(role.permissions)


# Canonical scope-aware permission codes（對齊 DB permission_definitions.scope_options，
# permscope01-04 seed）。has_permission 只對這些 code 認 ":scope" 後綴；其餘 code 的
# scope 後綴 fail-closed（避免「想授本班、實際授全域」的 footgun，RA-HIGH-1）。
# 新增 scope-aware 權限時務必同步更新此集合 + 對應 alembic seed migration
# （check_scope_options_sanity 會在 startup 比對 DB 與此集合，漂移時 log WARNING）。
SCOPE_AWARE_CODES = frozenset(
    {
        "STUDENTS_READ",
        "STUDENTS_WRITE",
        "STUDENTS_HEALTH_READ",
        "STUDENTS_HEALTH_WRITE",
        "STUDENTS_LIFECYCLE_WRITE",
        "STUDENTS_MEDICATION_ADMINISTER",
        "STUDENTS_SPECIAL_NEEDS_READ",
        "STUDENTS_SPECIAL_NEEDS_WRITE",
        "PORTFOLIO_READ",
        "PORTFOLIO_WRITE",
        "PORTFOLIO_PUBLISH",
        "DISMISSAL_CALLS_READ",
        "DISMISSAL_CALLS_WRITE",
    }
)


def has_permission(
    user_perms: List[str] | None,
    required: "Permission | str",
) -> bool:
    """單一權限檢查。

    user_perms 應為已 resolve 完的最終 list（從 resolve_user_permissions 取得）。
    若 caller 傳 None，視為「無權限」回 False；不在 helper 內 fallback role。
    required 接受 Permission enum 或 str。

    Scope-aware（2026-06-01 latent fix）：認 `:scope` 後綴的 entry。
    例：`['STUDENTS_HEALTH_READ:own_class']` 持有 `STUDENTS_HEALTH_READ` perm
    （只是限自班 scope）。對齊 frontend `hasPermission` 行為。

    RA-HIGH-1 fail-closed（2026-06-02）：只對 SCOPE_AWARE_CODES 認 `:scope`
    後綴；非 scope-aware code 帶 scope 後綴（例 `SALARY_READ:own_class`）視為
    「無此權限」回 False，避免誤把「想授本班、實際授全域」的設定當成全域放行。
    """
    if user_perms is None:
        return False
    if WILDCARD in user_perms:
        return True
    name = required.value if isinstance(required, Permission) else required
    if name in user_perms:
        return True
    if name in SCOPE_AWARE_CODES:
        scope_prefix = f"{name}:"
        return any(p.startswith(scope_prefix) for p in user_perms)
    return False


def _operator_covers_token(operator_perms: List[str], token: str) -> bool:
    """操作者權限集是否「涵蓋」目標的單一權限 token（含 scope 維度）。

    C13 越權預覽防護用：冒充時要求目標每一條權限都在操作者已有的範圍內，
    避免操作者藉冒充取得自己原本沒有的權限或更廣的 scope。

    規則：
        - wildcard 操作者涵蓋一切。
        - 目標 token 為 bare（無 scope 後綴 = 全域 scope）：操作者必須持相同
          bare code（或 wildcard）。**僅持 `:own_class`/`:all` scope 不足**——
          否則等於把「限自班」升級成「全域」(bare vs :own_class 升級)。
        - 目標 token 帶 `:own_class`：操作者持同 code 之 bare（superset）、
          `:all`、`:own_class`（或 wildcard）皆涵蓋。
        - 目標 token 帶 `:all`：操作者持同 code 之 bare、`:all`（或 wildcard）
          涵蓋；僅持 `:own_class` 不足。
    """
    if WILDCARD in operator_perms:
        return True
    base, sep, scope = token.partition(":")
    if not sep:
        # bare token → 全域 scope，操作者須持完全相同的 bare code
        return token in operator_perms
    # 帶 scope 後綴
    if base in operator_perms:
        # 操作者持 bare = 全域 scope，superset 任何 scope
        return True
    if token in operator_perms:
        # 操作者持完全相同 scope token
        return True
    if scope == "own_class":
        # own_class 也被操作者的 :all 涵蓋
        return f"{base}:all" in operator_perms
    return False


def permissions_subset(
    target_perms: List[str] | None,
    operator_perms: List[str] | None,
) -> bool:
    """目標權限集是否為操作者權限集的子集（scope-aware）。

    用於冒充/impersonate 守衛：唯有 target ⊆ operator 才允許切換身份，否則
    操作者會藉冒充越權預覽（principal 無 EMPLOYEES_READ 卻冒充 HR 讀全園個資）。
    """
    if operator_perms is not None and WILDCARD in operator_perms:
        return True
    if not target_perms:
        return True
    operator_perms = operator_perms or []
    return all(_operator_covers_token(operator_perms, t) for t in target_perms)


def validate_permission_names(names: List[str]) -> List[str]:
    """驗證 permission_names 每筆格式合法（RA-HIGH-1b）。

    規則：
        - wildcard '*' 視為合法（admin 授全權）
        - base code 必須是合法 Permission（在 Permission.__members__ 中）
        - 帶 scope 後綴（'CODE:scope'）者：base 必須在 SCOPE_AWARE_CODES，
          且 scope 值 ∈ {own_class, all}
    回傳非法項清單（空 list = 全部合法）；caller 收到非空即可 raise 422。
    """
    invalid: List[str] = []
    for n in names:
        if n == WILDCARD:
            continue
        base, sep, scope = n.partition(":")
        if base not in Permission.__members__:
            invalid.append(n)
            continue
        if sep:  # 帶 ':' 後綴
            if base not in SCOPE_AWARE_CODES or scope not in ("own_class", "all"):
                invalid.append(n)
    return invalid


def resolve_user_permissions(user, session=None) -> List[str]:
    """從 User 物件取出最終權限清單。

    - permission_names 為 list → 原樣回傳（已 override role 預設）
    - permission_names is None → 套用 role 預設模板：
        * 有傳 session 且 DB roles 表有 seed 該 role（非空）→ 以 **DB 為單一事實
          來源**。
        * 否則（無 session / DB 未 seed / role 不存在）→ fallback in-code
          ``ROLE_TEMPLATES``（保底不 lockout、向下相容）。

    Why（系統設計審查 2026-06-14, top#5）：原本 NULL-perm 帳號一律走 in-code
    ``ROLE_TEMPLATES``，與 DB 角色 scope 漂移時會【靜默提權】——只改 DB roles 的
    scope（例如把某 scope-aware code 從 ``:all`` 收成 ``:own_class``）而忘了同步
    in-code 模板，這批 seed/遺留/未明確設權限的帳號 runtime 完全感知不到變更，方向
    還是「放寬」（2026-06-04 滲透測試 #2 教師被提權成全園 scope 即此）。改為「有
    session 時以 DB 為準」消除此漂移；roles 表查詢只 SELECT 不寫，空表回 None 走
    fallback（不丟例外、不中止交易、不 lockout）。
    """
    if user.permission_names is not None:
        return list(user.permission_names)
    if session is not None:
        from models.permission_models import Role

        role = session.query(Role).filter_by(code=user.role).first()
        if role is not None and role.permissions:
            return list(role.permissions)
    return list(ROLE_TEMPLATES.get(user.role, []))


def get_permission_list(user_perms: List[str] | None) -> List[str]:
    """展開權限清單為合法權限名稱列表。

    - None → []
    - 含 wildcard "*" → 全部 63 個 Permission name
    - 否則 → 過濾掉非法名稱
    """
    if user_perms is None:
        return []
    if WILDCARD in user_perms:
        return [p.value for p in Permission]
    return [p for p in user_perms if p in Permission.__members__]


def get_permissions_definition(session) -> Dict:
    """取得完整權限定義（從 DB permission_definitions + roles 兩表拉，取代 in-code dict）。

    runtime 從 DB 拉確保 admin runtime 改動立即生效。in-code dict 保留供 alembic
    rolesdb01 seed 用，但 runtime 不再參考。
    """
    from models.permission_models import PermissionDefinition, Role

    perm_defs = (
        session.query(PermissionDefinition)
        .order_by(PermissionDefinition.group_name, PermissionDefinition.code)
        .all()
    )
    role_defs = session.query(Role).order_by(Role.is_core.desc(), Role.code).all()

    permissions = {
        p.code: {"value": p.code, "label": p.label, "is_core": p.is_core}
        for p in perm_defs
    }

    # 動態組 groups：依 group_name 分群，對齊 SPLIT_MODULES 為 split_permissions
    split_codes = set()
    for sp in SPLIT_MODULES.values():
        split_codes.add(sp["read"])
        split_codes.add(sp["write"])

    groups_map: Dict[str, Dict] = {}
    for p in perm_defs:
        if p.group_name not in groups_map:
            groups_map[p.group_name] = {
                "name": p.group_name,
                "permissions": [],
                "split_permissions": [],
            }
        if p.code not in split_codes:
            groups_map[p.group_name]["permissions"].append(p.code)

    # 把 SPLIT_MODULES 的 read/write 配對加進對應 group
    for module_key, sp in SPLIT_MODULES.items():
        read_def = next((p for p in perm_defs if p.code == sp["read"]), None)
        if read_def and read_def.group_name in groups_map:
            module_label = PERMISSION_LABELS.get(sp["read"], sp["read"]).replace(
                " (檢視)", ""
            )
            groups_map[read_def.group_name]["split_permissions"].append(
                {
                    "module": module_label,
                    "read": sp["read"],
                    "write": sp["write"],
                }
            )

    groups = list(groups_map.values())

    roles = {
        r.code: {
            "label": r.label,
            "description": r.description or "",
            "permissions": list(r.permissions),
            "is_core": r.is_core,
        }
        for r in role_defs
    }

    return {
        "permissions": permissions,
        "groups": groups,
        "roles": roles,
        "split_modules": SPLIT_MODULES,
    }


# === PermissionGrant + resolve_grant ===


class PermissionGrant(NamedTuple):
    code: str
    scope: Optional[str]  # "all" | "own_class" | None (no scope_options)


# scope ranking: higher index = broader
_SCOPE_BREADTH = {"own_class": 0, "all": 1}


def resolve_grant(user, code: str) -> Optional[PermissionGrant]:
    """解析使用者對特定權限 code 的 grant（含 scope 限定詞）。

    解析規則：
        wildcard '*'              → ('all')
        bare 'STUDENTS_READ'      → ('all')        # 向後相容
        'STUDENTS_READ:own_class' → ('own_class')
        同時含 bare 與 scoped     → 取較寬鬆者（'all'）
        多個 scoped 並存          → 取最寬鬆者
        permission_names 為 None / 空 → None
        所有 scope 皆無效字串     → None（fail-closed，避免誤升權）

    Args:
        user: 可為 SQLAlchemy model 物件（有 .permission_names 屬性）或 dict
              （get_current_user 回傳的 JWT payload dict）
        code: 權限 enum 字串值（如 'STUDENTS_READ'）

    Returns:
        PermissionGrant(code, scope) 或 None
    """
    if isinstance(user, dict):
        perm_names = user.get("permission_names", [])
    else:
        perm_names = getattr(user, "permission_names", None)
    names = perm_names or []
    if WILDCARD in names:
        return PermissionGrant(code, "all")

    found_scopes: list[str] = []
    for n in names:
        if n == code:
            found_scopes.append("all")
        elif n.startswith(f"{code}:"):
            scope = n.split(":", 1)[1]
            found_scopes.append(scope)

    if not found_scopes:
        return None

    # pick broadest valid scope; fail-closed if all scopes are invalid strings
    valid = [s for s in found_scopes if s in _SCOPE_BREADTH]
    if not valid:
        return None
    broadest = max(valid, key=lambda s: _SCOPE_BREADTH[s])
    return PermissionGrant(code, broadest)


def require_scoped_permission(code: "Permission"):
    """FastAPI dependency；同 require_permission 但額外暴露 user 的 grant scope。

    回傳:
        callable，呼叫後回 (user, PermissionGrant) tuple

    使用方式:
        @router.get('/students')
        def list_students(
            scoped=Depends(require_scoped_permission(Permission.STUDENTS_READ))
        ):
            user, grant = scoped
            clause = student_scope.filter_clause(user, grant.scope)
            ...

    若使用者未持有該權限，raise 403。
    """
    # local import 避循環：utils.auth → utils.permissions
    from fastapi import Depends, HTTPException
    from utils.auth import get_current_user

    def dep(user=Depends(get_current_user)):
        grant = resolve_grant(user, code.value)
        if grant is None:
            raise HTTPException(
                status_code=403,
                detail=f"missing permission: {code.value}",
            )
        return user, grant

    return dep


# === Startup sanity warning for missing scope_options ===

# Prefixes that imply a permission SHOULD support scope_options.
# Phase 1 only includes STUDENTS_*. Phases 2-4 expand this list.
_SCOPE_AWARE_PREFIXES = ("STUDENTS_",)
_SCOPE_AWARE_EXACT: tuple = ()


def check_scope_options_sanity(seed: dict) -> None:
    """startup sanity warning — 若 seed 中某 permission code 名稱看起來像
    scope-aware（前綴匹配 _SCOPE_AWARE_PREFIXES 或精確匹配 _SCOPE_AWARE_EXACT），
    但 DB permission_definitions.scope_options 為 NULL/空，則 log WARNING。

    用途：未來 Phase 2-4 新增 scope-aware 權限時，若 migration 忘了補
    scope_options seed，此檢查能在 startup 立刻發出警告（不擋啟動）。

    Args:
        seed: dict[str, list[str] | None]，鍵為 permission code，值為 scope_options
    """
    for code, opts in seed.items():
        looks_scope_aware = (
            any(code.startswith(p) for p in _SCOPE_AWARE_PREFIXES)
            or code in _SCOPE_AWARE_EXACT
        )
        if looks_scope_aware and not opts:
            _logger.warning(
                "permission %r looks scope-aware but scope_options is empty/NULL "
                "in permission_definitions; consider adding a migration",
                code,
            )

    # RA-HIGH-1：防 SCOPE_AWARE_CODES（has_permission/validate_permission_names 用的
    # canonical 集合）與 DB scope_options 漂移。只比對「seed 中實際出現」的 code
    # （部分 seed 不誤報，對齊既有單元測試以 partial seed 呼叫的慣例）：
    #   - seed code 有 scope_options 但不在 SCOPE_AWARE_CODES → code 集合漏列
    #   - seed code 在 SCOPE_AWARE_CODES 但 scope_options 空 → DB seed 漏補
    # 啟動處傳入完整 seed 時即可偵測雙向漂移；不擋啟動，只 log WARNING。
    present = set(seed.keys())
    db_scope_aware = {code for code, opts in seed.items() if opts}
    missing_in_code = db_scope_aware - SCOPE_AWARE_CODES
    missing_in_db = (SCOPE_AWARE_CODES & present) - db_scope_aware
    if missing_in_code or missing_in_db:
        _logger.warning(
            "SCOPE_AWARE_CODES 與 DB scope_options 漂移："
            "DB 有 scope_options 但 code 集合缺 %r；code 集合列為 scope-aware 但 DB "
            "scope_options 空 %r。請同步 utils.permissions.SCOPE_AWARE_CODES 與 "
            "permscope alembic seed。",
            sorted(missing_in_code),
            sorted(missing_in_db),
        )


def _permission_names_contains(perm: str):
    """SQLAlchemy filter expression：User.permission_names 顯式含 perm（PostgreSQL 用）。

    `permission_names` 欄是 ``JSON().with_variant(ARRAY(Text), "postgresql")``。
    **不可**用 ``User.permission_names.contains([perm])``——`with_variant` 只換
    DDL/bind 型別、不換 Python comparator，`.contains()` 走基底型別 JSON 的
    comparator 生成畸形 ``permission_names LIKE '%' || ARRAY[...]::TEXT[] || '%'``，
    真實 PostgreSQL 報 ``malformed array literal`` 並中止整個交易（2026-06-06
    教師 portal 送假/加班/補打卡全 500 的根因）。
    改用 ``cast`` 取得 ARRAY comparator → 生成 ``CAST(... AS TEXT[]) @> ARRAY[...]``。
    """
    from sqlalchemy import Text, cast
    from sqlalchemy.dialects.postgresql import ARRAY

    from models.database import User  # 延遲匯入避免循環

    return cast(User.permission_names, ARRAY(Text)).contains([perm])


def list_active_user_ids_with_permission(session, perm: str) -> list[int]:
    """列出 is_active 且 permission_names **顯式**含 perm 的 user id（SQLite/PG 通用）。

    語意：僅匹配顯式列出 perm 的帳號；``permission_names`` 為 NULL（走角色模板）
    或萬用 ``'*'`` 的帳號**不**匹配（SQLite 與 PostgreSQL 兩分支一致，沿用既有
    行為——本函式只修「PG 畸形查詢中止交易」的崩潰，不改既有匹配語意）。
    """
    from models.database import User  # 延遲匯入避免循環

    is_sqlite = session.bind.dialect.name == "sqlite"
    if is_sqlite:
        users = session.query(User).filter(User.is_active.is_(True)).all()
        return [
            u.id for u in users if u.permission_names and perm in u.permission_names
        ]
    rows = (
        session.query(User.id)
        .filter(User.is_active.is_(True), _permission_names_contains(perm))
        .all()
    )
    return [r[0] for r in rows]


def find_permission_definition_drift(db_codes) -> Dict[str, List[str]]:
    """偵測 in-code Permission enum 與 DB permission_definitions 的漂移（純函式）。

    - ``missing_in_db``：enum 有、DB 沒有 → 功能對非 wildcard admin 鎖死、admin UI
      無法授權（典型成因：新增 Permission 後未補 backfill migration，如
      rolesdb01 seed 早於後續 6 碼新增）。
    - ``missing_label``：enum 有、PERMISSION_LABELS 沒有 → seed/UI 缺標籤。

    Args:
        db_codes: DB permission_definitions 既有的 code 集合（呼叫端查 DB 後傳入，
            保持本函式無 I/O 易測）。
    """
    db = set(db_codes)
    enum_codes = {p.value for p in Permission}
    return {
        "missing_in_db": sorted(enum_codes - db),
        "missing_label": sorted(enum_codes - set(PERMISSION_LABELS)),
    }


def check_permission_definition_drift(session) -> List[str]:
    """查 DB 後比對 enum，回傳 DB 缺漏的權限碼（startup 用，順帶 logging.WARNING）。

    僅讀取與記錄，不修改 DB（修補走 backfill migration）。DB 尚未建表（早期環境）
    時回空清單不阻擋啟動。
    """
    from sqlalchemy import text

    try:
        rows = session.execute(text("SELECT code FROM permission_definitions")).all()
    except Exception:  # 表不存在 / 連線問題 → 不阻擋啟動
        return []
    drift = find_permission_definition_drift({r[0] for r in rows})
    missing = drift["missing_in_db"]
    if missing:
        _logger.warning(
            "permission_definitions 與 Permission enum 漂移：DB 缺 %d 碼 %s；"
            "請跑 backfill migration（非 wildcard admin 會對這些功能 403、admin UI 無法授權）",
            len(missing),
            missing,
        )
    return missing
