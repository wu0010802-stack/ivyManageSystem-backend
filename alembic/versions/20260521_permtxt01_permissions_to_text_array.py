"""permissions_to_text_array

Revision ID: permtxt01
Revises: 3be2e40aaa42
Create Date: 2026-05-21

把 users.permissions (bigint) 拆成 users.permission_names (text[])。
backfill 邏輯用 LEGACY_BITS 凍結快照，避免 import utils.permissions 抓到未來改過的版本。
同時 bump 所有 user 的 token_version，強制全員重登（舊 JWT 帶舊 permissions claim 即失效）。
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "permtxt01"
down_revision = "3be2e40aaa42"
branch_labels = None
depends_on = None


# 凍結快照——本檔自含，**不要**從 utils.permissions import LEGACY_PERMISSION_BITS。
# 一旦 prod 跑過本 migration，下表不准動。63 entries (bits 0-62)。
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


def _bigint_to_names(val):
    """純函式：把 bigint mask 拆成 name list。

    若 val 含 _LEGACY_BITS 外的 bit（例如 1<<63 或更高），raise RuntimeError——
    與 _names_to_bigint 對稱 fail-loud，避免 silently 丟失 prod 中異常 row 的資料。
    """
    if val is None:
        return None
    if val == -1:
        return ["*"]
    if val == 0:
        return []
    known_mask = 0
    for bit in _LEGACY_BITS.values():
        known_mask |= bit
    unknown = val & ~known_mask
    if unknown:
        raise RuntimeError(
            f"_bigint_to_names: bigint value {val} 含 LEGACY_BITS 範圍外的 bit (unknown_mask={unknown:#x})。"
            "請手動處理該 row 後重跑。"
        )
    return [name for name, bit in _LEGACY_BITS.items() if (val & bit) == bit]


def _names_to_bigint(names):
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

    # 2) backfill（逐 row 而非 set-based SQL：N<100 + pure-function 已單元測試覆蓋，
    #    set-based 在 PostgreSQL 中需要 unnest+CASE 反而難讀且難審）
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

    rows = bind.execute(sa.text("SELECT id, permission_names FROM users")).fetchall()
    for r in rows:
        val = _names_to_bigint(r.permission_names)
        bind.execute(
            sa.text("UPDATE users SET permissions = :val WHERE id = :id"),
            {"val": val, "id": r.id},
        )

    # 對稱於 upgrade：rollback 時也 bump token_version，
    # 強制全員重新登入避免新版簽出的 JWT (permission_names claim) 流入舊版 verify path。
    bind.execute(
        sa.text("UPDATE users SET token_version = COALESCE(token_version, 0) + 1")
    )

    op.drop_column("users", "permission_names")
