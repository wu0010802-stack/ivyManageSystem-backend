"""drop redundant indexes (28 條)

清理兩類冗餘索引：

1. 完全重複（UNIQUE 約束已提供同樣的 btree）：
   - salary_calc_jobs.ix_salary_calc_jobs_job_id ← salary_calc_jobs_job_id_key
   - student_fee_payments.ix_fee_payments_idk ← uq_student_fee_payments_idk
   - insurance_brackets.ix_bracket_year_amount ← uq_bracket_year_amount

2. 前綴重疊（被多列索引完全涵蓋，Postgres 可用後者服務所有前者能服務的查詢）：
   - audit_logs.ix_audit_entity ← ix_audit_entity_created
   - audit_logs.ix_audit_user ← ix_audit_user_created
   - student_fee_records.ix_fee_records_student ← uq_student_fee_item / ix_fee_records_student_period
   - student_fee_payments.ix_fee_payments_record ← ix_fee_payments_record_date
   - attendances.ix_attendance_date ← ix_attendance_anomaly
   - recruitment_visits.ix_recruitment_visits_{source,month,referrer} ← ix_rv_*_grade / ix_recruitment_month_grade
   - activity_attendances.ix_activity_attendances_session_id ← uq_activity_attendance_session_reg
   - activity_sessions.ix_activity_sessions_course_id ← uq_activity_session_course_date
   - salary_records.ix_salary_ym ← ix_salary_ym_needs_recalc / ix_salary_ym_finalized
   - student_attendances.ix_student_attendance_student ← uq_student_attendance_date
   - activity_registrations.ix_activity_registrations_active ← ix_activity_regs_active_paid
   - activity_registrations.ix_activity_regs_student_id ← ix_activity_regs_student_term
   - registration_supplies.ix_reg_supply_reg ← uq_reg_supply
   - recruitment_ivykids_records.ix_recruitment_ivykids_records_month ← ix_recruitment_ivykids_month_source
   - student_change_logs.ix_student_change_logs_term ← ix_change_logs_term_event_date
   - guardians.ix_guardians_student ← ix_guardians_student_active
   - salary_snapshots.ix_salary_snapshot_ym ← ix_salary_snapshot_ym_type
   - competitor_tag.ix_competitor_tag_school_id ← uq_competitor_tag_school_code
   - event_acknowledgments.ix_event_ack_event ← uq_event_ack
   - parent_notification_preferences.ix_parent_notif_pref_user ← uq_parent_notif_pref_triple
   - student_contact_book_acks.ix_contact_book_ack_entry ← uq_contact_book_ack_entry_guardian
   - parent_refresh_tokens.ix_parent_refresh_tokens_user_id ← ix_parent_refresh_user_family
   - sync_raw_data.ix_sync_raw_data_job_log_id ← idx_sync_raw_job_type_key

刻意保留（看似重複但實際不同）：
- students.ix_student_sid 與 employees.ix_employee_eid 為 GIN(gin_trgm_ops)，支援
  LIKE '%xxx%' 子字串模糊搜尋，UNIQUE btree 無法替代。
- 各 *_key 主 UNIQUE 約束本身保留（為 schema 完整性）。

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-05-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "o0p1q2r3s4t5"
down_revision = "n9o0p1q2r3s4"
branch_labels = None
depends_on = None


# (index_name, table_name, recreate_sql_for_downgrade)
DROPS = [
    # ── 完全重複：UNIQUE 已涵蓋 ────────────────────────────────────────
    (
        "ix_salary_calc_jobs_job_id",
        "salary_calc_jobs",
        "CREATE INDEX ix_salary_calc_jobs_job_id ON public.salary_calc_jobs USING btree (job_id)",
    ),
    (
        "ix_fee_payments_idk",
        "student_fee_payments",
        "CREATE INDEX ix_fee_payments_idk ON public.student_fee_payments USING btree (idempotency_key)",
    ),
    (
        "ix_bracket_year_amount",
        "insurance_brackets",
        "CREATE INDEX ix_bracket_year_amount ON public.insurance_brackets USING btree (effective_year, amount)",
    ),
    # ── 前綴重疊：被多列索引涵蓋 ──────────────────────────────────────
    (
        "ix_audit_entity",
        "audit_logs",
        "CREATE INDEX ix_audit_entity ON public.audit_logs USING btree (entity_type, entity_id)",
    ),
    (
        "ix_audit_user",
        "audit_logs",
        "CREATE INDEX ix_audit_user ON public.audit_logs USING btree (user_id)",
    ),
    (
        "ix_fee_records_student",
        "student_fee_records",
        "CREATE INDEX ix_fee_records_student ON public.student_fee_records USING btree (student_id)",
    ),
    (
        "ix_fee_payments_record",
        "student_fee_payments",
        "CREATE INDEX ix_fee_payments_record ON public.student_fee_payments USING btree (record_id)",
    ),
    (
        "ix_attendance_date",
        "attendances",
        "CREATE INDEX ix_attendance_date ON public.attendances USING btree (attendance_date)",
    ),
    (
        "ix_recruitment_visits_source",
        "recruitment_visits",
        "CREATE INDEX ix_recruitment_visits_source ON public.recruitment_visits USING btree (source)",
    ),
    (
        "ix_recruitment_visits_month",
        "recruitment_visits",
        "CREATE INDEX ix_recruitment_visits_month ON public.recruitment_visits USING btree (month)",
    ),
    (
        "ix_recruitment_visits_referrer",
        "recruitment_visits",
        "CREATE INDEX ix_recruitment_visits_referrer ON public.recruitment_visits USING btree (referrer)",
    ),
    (
        "ix_activity_attendances_session_id",
        "activity_attendances",
        "CREATE INDEX ix_activity_attendances_session_id ON public.activity_attendances USING btree (session_id)",
    ),
    (
        "ix_activity_sessions_course_id",
        "activity_sessions",
        "CREATE INDEX ix_activity_sessions_course_id ON public.activity_sessions USING btree (course_id)",
    ),
    (
        "ix_salary_ym",
        "salary_records",
        "CREATE INDEX ix_salary_ym ON public.salary_records USING btree (salary_year, salary_month)",
    ),
    (
        "ix_student_attendance_student",
        "student_attendances",
        "CREATE INDEX ix_student_attendance_student ON public.student_attendances USING btree (student_id)",
    ),
    (
        "ix_activity_registrations_active",
        "activity_registrations",
        "CREATE INDEX ix_activity_registrations_active ON public.activity_registrations USING btree (is_active)",
    ),
    (
        "ix_activity_regs_student_id",
        "activity_registrations",
        "CREATE INDEX ix_activity_regs_student_id ON public.activity_registrations USING btree (student_id)",
    ),
    (
        "ix_reg_supply_reg",
        "registration_supplies",
        "CREATE INDEX ix_reg_supply_reg ON public.registration_supplies USING btree (registration_id)",
    ),
    (
        "ix_recruitment_ivykids_records_month",
        "recruitment_ivykids_records",
        "CREATE INDEX ix_recruitment_ivykids_records_month ON public.recruitment_ivykids_records USING btree (month)",
    ),
    (
        "ix_student_change_logs_term",
        "student_change_logs",
        "CREATE INDEX ix_student_change_logs_term ON public.student_change_logs USING btree (school_year, semester)",
    ),
    (
        "ix_guardians_student",
        "guardians",
        "CREATE INDEX ix_guardians_student ON public.guardians USING btree (student_id)",
    ),
    (
        "ix_salary_snapshot_ym",
        "salary_snapshots",
        "CREATE INDEX ix_salary_snapshot_ym ON public.salary_snapshots USING btree (salary_year, salary_month)",
    ),
    (
        "ix_competitor_tag_school_id",
        "competitor_tag",
        "CREATE INDEX ix_competitor_tag_school_id ON public.competitor_tag USING btree (school_id)",
    ),
    (
        "ix_event_ack_event",
        "event_acknowledgments",
        "CREATE INDEX ix_event_ack_event ON public.event_acknowledgments USING btree (event_id)",
    ),
    (
        "ix_parent_notif_pref_user",
        "parent_notification_preferences",
        "CREATE INDEX ix_parent_notif_pref_user ON public.parent_notification_preferences USING btree (user_id)",
    ),
    (
        "ix_contact_book_ack_entry",
        "student_contact_book_acks",
        "CREATE INDEX ix_contact_book_ack_entry ON public.student_contact_book_acks USING btree (entry_id)",
    ),
    (
        "ix_parent_refresh_tokens_user_id",
        "parent_refresh_tokens",
        "CREATE INDEX ix_parent_refresh_tokens_user_id ON public.parent_refresh_tokens USING btree (user_id)",
    ),
    (
        "ix_sync_raw_data_job_log_id",
        "sync_raw_data",
        "CREATE INDEX ix_sync_raw_data_job_log_id ON public.sync_raw_data USING btree (job_log_id)",
    ),
]


def upgrade() -> None:
    for name, table, _recreate in DROPS:
        op.execute(f'DROP INDEX IF EXISTS public."{name}"')


def downgrade() -> None:
    for _name, _table, recreate in reversed(DROPS):
        op.execute(recreate)
