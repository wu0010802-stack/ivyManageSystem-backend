"""add missing FK indexes (batch v3)

補上 26 條外鍵欄位的索引，避免 JOIN 與 ON DELETE 級聯時全表掃描。
索引命名統一為 ix_<table>_<column>。

涵蓋表：
- announcement_parent_reads / announcement_parent_recipients / announcement_recipients
- contact_book_templates
- event_acknowledgments (student_id, signature_attachment_id)
- guardian_binding_codes (created_by, used_by_user_id)
- line_reply_contexts (thread_id)
- parent_communication_logs (recorded_by)
- parent_refresh_tokens (parent_token_id)
- registration_supplies (supply_id)
- salary_snapshots (salary_record_id, attendance_policy_id, bonus_config_id)
- student_allergies (created_by)
- student_contact_book_acks / student_contact_book_entries / student_contact_book_replies
- student_leave_requests (reviewed_by, applicant_user_id, applicant_guardian_id)
- student_medication_logs (administered_by)
- student_medication_orders (created_by)
- student_observations (recorded_by)
- students (recruitment_visit_id)

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2026-05-07
"""

from alembic import op

# revision identifiers, used by Alembic.
revision = "m8n9o0p1q2r3"
down_revision = "l7m8n9o0p1q2"
branch_labels = None
depends_on = None


# (table, column, index_name)
INDEXES = [
    ("announcement_parent_reads", "user_id", "ix_announcement_parent_reads_user_id"),
    (
        "announcement_parent_recipients",
        "guardian_id",
        "ix_announcement_parent_recipients_guardian_id",
    ),
    (
        "announcement_recipients",
        "employee_id",
        "ix_announcement_recipients_employee_id",
    ),
    (
        "contact_book_templates",
        "classroom_id",
        "ix_contact_book_templates_classroom_id",
    ),
    ("event_acknowledgments", "student_id", "ix_event_acknowledgments_student_id"),
    (
        "event_acknowledgments",
        "signature_attachment_id",
        "ix_event_acknowledgments_signature_attachment_id",
    ),
    ("guardian_binding_codes", "created_by", "ix_guardian_binding_codes_created_by"),
    (
        "guardian_binding_codes",
        "used_by_user_id",
        "ix_guardian_binding_codes_used_by_user_id",
    ),
    ("line_reply_contexts", "thread_id", "ix_line_reply_contexts_thread_id"),
    (
        "parent_communication_logs",
        "recorded_by",
        "ix_parent_communication_logs_recorded_by",
    ),
    (
        "parent_refresh_tokens",
        "parent_token_id",
        "ix_parent_refresh_tokens_parent_token_id",
    ),
    ("registration_supplies", "supply_id", "ix_registration_supplies_supply_id"),
    ("salary_snapshots", "salary_record_id", "ix_salary_snapshots_salary_record_id"),
    (
        "salary_snapshots",
        "attendance_policy_id",
        "ix_salary_snapshots_attendance_policy_id",
    ),
    ("salary_snapshots", "bonus_config_id", "ix_salary_snapshots_bonus_config_id"),
    ("student_allergies", "created_by", "ix_student_allergies_created_by"),
    (
        "student_contact_book_acks",
        "guardian_user_id",
        "ix_student_contact_book_acks_guardian_user_id",
    ),
    (
        "student_contact_book_entries",
        "created_by_employee_id",
        "ix_student_contact_book_entries_created_by_employee_id",
    ),
    (
        "student_contact_book_replies",
        "guardian_user_id",
        "ix_student_contact_book_replies_guardian_user_id",
    ),
    ("student_leave_requests", "reviewed_by", "ix_student_leave_requests_reviewed_by"),
    (
        "student_leave_requests",
        "applicant_user_id",
        "ix_student_leave_requests_applicant_user_id",
    ),
    (
        "student_leave_requests",
        "applicant_guardian_id",
        "ix_student_leave_requests_applicant_guardian_id",
    ),
    (
        "student_medication_logs",
        "administered_by",
        "ix_student_medication_logs_administered_by",
    ),
    (
        "student_medication_orders",
        "created_by",
        "ix_student_medication_orders_created_by",
    ),
    ("student_observations", "recorded_by", "ix_student_observations_recorded_by"),
    ("students", "recruitment_visit_id", "ix_students_recruitment_visit_id"),
]


def upgrade() -> None:
    for table, column, name in INDEXES:
        op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    for _table, _column, name in reversed(INDEXES):
        op.drop_index(name, table_name=_table)
