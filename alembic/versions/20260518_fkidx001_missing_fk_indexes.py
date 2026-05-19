"""2026-05-18 補齊 single-column FK 缺失索引

PG 不會自動為 FK 欄位建索引；下列 44 條 FK 為 leading column 無覆蓋，會造成：
  1. parent 表 DELETE/UPDATE 時對子表 seq scan（含 ON DELETE CASCADE / SET NULL）
  2. WHERE col = X 與 JOIN 走 seq scan，prod 資料長大後性能崩

清單由 pg_constraint × pg_attribute × pg_index 三方比對得出（single-col FK，
且不存在任何索引以該欄為 indkey[0]）。命名沿用既有 `ix_fk_<table>_<col>` 慣例
（見 20260512_b1c2d3e4f5a6 bug_sweep_fk_index）；長度均 < 63 char PG identifier limit。

不做：
  - 不動既有索引（含 dev `idx_scan=0` 的 audit/competitor/student_sid 等，dev 流量
    沒打到不代表 prod 沒用，無 prod stat 不丟）
  - 不改 FK ondelete 策略（與本次目標分離）
  - 不建 partial index：本次保持單純 btree，後續若確認某欄高度 NULL skew
    （例如 disciplinary_actions.applied_to_salary_id / appraisal_summaries.rejected_by）
    再另開 migration 改 partial。

idempotent：每條都先查 inspector 確認既無同名索引，也無任何索引以該欄為 indkey[0]
才建（雙保險防併存遷移踩到）。downgrade 對稱：只刪本 migration 建出的同名索引。

註：因使用 `inspect(bind)`，本檔不支援 `alembic upgrade ... --sql` offline 模式
（MockConnection 不支援 inspector）。Repo-wide 慣例同此（見 20260512_b1c2d3e4f5a6），
部署走 online `alembic upgrade head` 不受影響。

Revision ID: fkidx001
Revises: mfc00001
Create Date: 2026-05-18

"""

from alembic import op
from sqlalchemy import inspect

revision = "fkidx001"
down_revision = "mfc00001"
branch_labels = None
depends_on = None


# (table, fk_column) — 44 條缺索引清單
FK_INDEXES: list[tuple[str, str]] = [
    # appraisal 體系（人員 / cycle / 簽核流程）
    ("appraisal_bonus_rates", "created_by"),
    ("appraisal_cycles", "created_by"),
    ("appraisal_manual_event_counts", "entered_by"),
    ("appraisal_manual_event_counts", "participant_id"),
    ("appraisal_participants", "classroom_id"),
    ("appraisal_participants", "employee_id"),
    ("appraisal_score_items", "catalog_id"),
    ("appraisal_score_items", "created_by"),
    ("appraisal_scoring_rules", "created_by"),
    ("appraisal_summaries", "accounting_signed_by"),
    ("appraisal_summaries", "finalized_by"),
    ("appraisal_summaries", "rejected_by"),
    ("appraisal_summaries", "supervisor_signed_by"),
    ("appraisal_summary_log", "actor_id"),
    # 班級招生目標
    ("class_enrollment_targets", "assistant_employee_id"),
    ("class_enrollment_targets", "classroom_id"),
    ("class_enrollment_targets", "head_teacher_employee_id"),
    # 懲處→薪資扣款 cascade 路徑
    ("disciplinary_actions", "applied_to_salary_id"),
    # 年度結算
    ("employee_year_end_snapshot", "classroom_id"),
    ("employee_year_end_snapshot", "employee_id"),
    ("year_end_cycles", "created_by"),
    ("year_end_settlements", "accounting_signed_by"),
    ("year_end_settlements", "employee_id"),
    ("year_end_settlements", "finalized_by"),
    ("year_end_settlements", "rejected_by"),
    ("year_end_settlements", "snapshot_id"),
    ("year_end_settlements", "supervisor_signed_by"),
    # 在學證明
    ("enrollment_certificates", "issued_by_user_id"),
    # 政府資料 snapshot 來源
    ("insurance_brackets", "source_snapshot_id"),
    ("minimum_wage_history", "source_snapshot_id"),
    ("minimum_wage_staging", "source_snapshot_id"),
    # Phase 2 月度統計 / 月度固定成本 / 廠商付款
    ("monthly_enrollment_snapshots", "classroom_id"),
    ("monthly_fixed_costs", "created_by_id"),
    ("monthly_fixed_costs", "updated_by_id"),
    ("vendor_payments", "created_by_id"),
    # 特殊紅利
    ("special_bonus_items", "classroom_id"),
    ("special_bonus_items", "created_by"),
    # 學費範本來源
    ("student_fee_records", "source_template_id"),
    # 學生 portfolio / 成長報告 / IEP
    ("student_growth_reports", "generated_by"),
    ("student_iep_records", "approved_by_employee_id"),
    ("student_iep_records", "created_by_employee_id"),
    ("student_measurements", "created_by"),
    ("student_milestones", "created_by"),
    ("student_milestones", "parent_acknowledged_by"),
]


def _index_name(table: str, column: str) -> str:
    """ix_fk_<table>_<col> — 與 20260512 bug_sweep_fk_index 同慣例。"""
    return f"ix_fk_{table}_{column}"


def _column_has_leading_index(inspector, table: str, column: str) -> bool:
    """檢查是否已有任何索引以 column 為 leading column（含 unique constraint 生的）。"""
    for ix in inspector.get_indexes(table):
        cols = ix.get("column_names") or []
        if cols and cols[0] == column:
            return True
    # SA inspector 不會把 PK / unique constraint 列在 get_indexes，但這些都是
    # 多欄組合的話也不能省略 FK 索引；單欄 PK 不會出現在 FK list（FK 不指向自己）
    # 所以這裡僅檢查 get_indexes 已足夠。
    return False


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    for table, column in FK_INDEXES:
        if table not in existing_tables:
            # 表還不存在（例如 down-stream branch 未跑），略過
            continue
        idx_name = _index_name(table, column)
        existing_names = {ix["name"] for ix in inspector.get_indexes(table)}
        if idx_name in existing_names:
            continue
        if _column_has_leading_index(inspector, table, column):
            # 已被別名索引覆蓋（例如其他 migration 用了不同命名），不重複建
            continue
        op.create_index(idx_name, table, [column])


def downgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    existing_tables = set(inspector.get_table_names())

    # 逆序刪除（與 upgrade 對稱，雖然順序不影響正確性，便於 review）
    for table, column in reversed(FK_INDEXES):
        if table not in existing_tables:
            continue
        idx_name = _index_name(table, column)
        existing_names = {ix["name"] for ix in inspector.get_indexes(table)}
        if idx_name in existing_names:
            op.drop_index(idx_name, table_name=table)
