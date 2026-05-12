"""bug sweep 2026-05-12: FK ondelete + indexes + nullable cleanups

修補 2026-05-11 batch 內幾條 FK 策略與欄位設計問題：

1. art_teacher_payroll_entries.employee_id: CASCADE → RESTRICT
   原 CASCADE 會在硬刪員工時抹掉外師薪資歷史，違反金流流水保留原則。
2. disciplinary_actions.employee_id: CASCADE → RESTRICT
   同理，懲處扣款流水不該因刪員工而消失。
3. student_iep_records.{created_by,approved_by}_employee_id: 明示 ondelete="SET NULL"
   原無明示（NO ACTION），改成 SET NULL 讓教師離職時 IEP 紀錄保留、僅清作者欄位。
4. special_education_subsidies.employee_id: 明示 ondelete="RESTRICT"
   特教加給流水不該因刪員工而消失；欄位 NOT NULL 故不能用 SET NULL。
5. special_education_subsidies 加 (employee_id) 與 (period_start, period_end) index
   Phase 4 報表查詢熱欄位避免全表掃描。
6. monthly_enrollment_snapshots: 計數類欄位改 nullable=False
   原 nullable=True + server_default=0 語意混亂；改 NOT NULL 避免 Phase 2 報表
   程式漏判 NULL（表還是空殼，改 NOT NULL 不會壞既有資料）。

Revision ID: b1c2d3e4f5a6
Revises: f0ac312f781c
Create Date: 2026-05-12

"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect

revision = "b1c2d3e4f5a6"
down_revision = "f0ac312f781c"
branch_labels = None
depends_on = None


# PG 自動命名 FK 為 {table}_{column}_fkey；若不存在則 fallback 用 inspect 找。
def _find_fk_name(bind, table: str, column: str) -> str | None:
    inspector = inspect(bind)
    for fk in inspector.get_foreign_keys(table):
        if fk.get("constrained_columns") == [column]:
            return fk.get("name")
    return None


def _swap_fk_ondelete(
    table: str,
    column: str,
    ref_table: str,
    ref_column: str,
    new_ondelete: str,
) -> None:
    """Drop 既有 FK 再以新的 ondelete 重建。對 PG 是常見模式。"""
    bind = op.get_bind()
    fk_name = _find_fk_name(bind, table, column)
    if fk_name:
        op.drop_constraint(fk_name, table, type_="foreignkey")
    op.create_foreign_key(
        f"fk_{table}_{column}",
        table,
        ref_table,
        [column],
        [ref_column],
        ondelete=new_ondelete,
    )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 1. art_teacher_payroll_entries.employee_id CASCADE → RESTRICT
    if "art_teacher_payroll_entries" in tables:
        _swap_fk_ondelete(
            "art_teacher_payroll_entries",
            "employee_id",
            "employees",
            "id",
            "RESTRICT",
        )

    # 2. disciplinary_actions.employee_id CASCADE → RESTRICT
    if "disciplinary_actions" in tables:
        _swap_fk_ondelete(
            "disciplinary_actions",
            "employee_id",
            "employees",
            "id",
            "RESTRICT",
        )

    # 3. student_iep_records.{created_by,approved_by}_employee_id 明示 SET NULL
    if "student_iep_records" in tables:
        _swap_fk_ondelete(
            "student_iep_records",
            "created_by_employee_id",
            "employees",
            "id",
            "SET NULL",
        )
        _swap_fk_ondelete(
            "student_iep_records",
            "approved_by_employee_id",
            "employees",
            "id",
            "SET NULL",
        )

    # 4. special_education_subsidies.employee_id 明示 RESTRICT + 補 index
    if "special_education_subsidies" in tables:
        _swap_fk_ondelete(
            "special_education_subsidies",
            "employee_id",
            "employees",
            "id",
            "RESTRICT",
        )
        existing_indexes = {
            ix["name"] for ix in inspector.get_indexes("special_education_subsidies")
        }
        if "ix_special_ed_subsidies_employee" not in existing_indexes:
            op.create_index(
                "ix_special_ed_subsidies_employee",
                "special_education_subsidies",
                ["employee_id"],
            )
        if "ix_special_ed_subsidies_period" not in existing_indexes:
            op.create_index(
                "ix_special_ed_subsidies_period",
                "special_education_subsidies",
                ["period_start", "period_end"],
            )

    # 5. monthly_enrollment_snapshots 計數欄位 nullable=False
    # 表是 Phase 2 shell，現無資料；先把 NULL 回填 0 再加 NOT NULL，避免之後若
    # 透過其他途徑寫入 NULL 仍能成功而導致報表崩。
    if "monthly_enrollment_snapshots" in tables:
        count_columns = [
            "total_count",
            "male_count",
            "female_count",
            "disadvantaged_count",
            "disability_count",
            "indigenous_count",
            "foreign_count",
            "expected_attendance_days",
            "actual_attendance_days",
            "attendance_rate",
        ]
        for col in count_columns:
            op.execute(
                f"UPDATE monthly_enrollment_snapshots SET {col} = 0 WHERE {col} IS NULL"
            )
            op.alter_column(
                "monthly_enrollment_snapshots",
                col,
                existing_type=sa.Integer(),
                nullable=False,
                existing_server_default="0",
            )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    tables = set(inspector.get_table_names())

    # 5. monthly_enrollment_snapshots 計數欄位回 nullable=True
    if "monthly_enrollment_snapshots" in tables:
        count_columns = [
            "total_count",
            "male_count",
            "female_count",
            "disadvantaged_count",
            "disability_count",
            "indigenous_count",
            "foreign_count",
            "expected_attendance_days",
            "actual_attendance_days",
            "attendance_rate",
        ]
        for col in count_columns:
            op.alter_column(
                "monthly_enrollment_snapshots",
                col,
                existing_type=sa.Integer(),
                nullable=True,
                existing_server_default="0",
            )

    # 4. special_education_subsidies index 回滾 + FK 回滾
    if "special_education_subsidies" in tables:
        existing_indexes = {
            ix["name"] for ix in inspector.get_indexes("special_education_subsidies")
        }
        if "ix_special_ed_subsidies_period" in existing_indexes:
            op.drop_index(
                "ix_special_ed_subsidies_period",
                table_name="special_education_subsidies",
            )
        if "ix_special_ed_subsidies_employee" in existing_indexes:
            op.drop_index(
                "ix_special_ed_subsidies_employee",
                table_name="special_education_subsidies",
            )
        # 回滾為原 migration 的「未明示 ondelete」（PG 預設 NO ACTION）
        fk_name = _find_fk_name(bind, "special_education_subsidies", "employee_id")
        if fk_name:
            op.drop_constraint(
                fk_name, "special_education_subsidies", type_="foreignkey"
            )
        op.create_foreign_key(
            None,
            "special_education_subsidies",
            "employees",
            ["employee_id"],
            ["id"],
        )

    # 3. student_iep_records 兩 FK 回未明示
    if "student_iep_records" in tables:
        for col in ("approved_by_employee_id", "created_by_employee_id"):
            fk_name = _find_fk_name(bind, "student_iep_records", col)
            if fk_name:
                op.drop_constraint(fk_name, "student_iep_records", type_="foreignkey")
            op.create_foreign_key(
                None, "student_iep_records", "employees", [col], ["id"]
            )

    # 2. disciplinary_actions.employee_id 回 CASCADE
    if "disciplinary_actions" in tables:
        _swap_fk_ondelete(
            "disciplinary_actions",
            "employee_id",
            "employees",
            "id",
            "CASCADE",
        )

    # 1. art_teacher_payroll_entries.employee_id 回 CASCADE
    if "art_teacher_payroll_entries" in tables:
        _swap_fk_ondelete(
            "art_teacher_payroll_entries",
            "employee_id",
            "employees",
            "id",
            "CASCADE",
        )
