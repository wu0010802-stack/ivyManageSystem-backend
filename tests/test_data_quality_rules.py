"""tests/test_data_quality_rules.py — Ch2 data quality rules + schema."""

from sqlalchemy import inspect

from models.data_quality import DataQualityReport
from models.base import Base
from utils.permissions import Permission, PERMISSION_LABELS, ROLE_TEMPLATES


def test_data_quality_report_columns():
    cols = {c.name for c in DataQualityReport.__table__.columns}
    expected = {
        "id",
        "rule_code",
        "severity",
        "entity_type",
        "entity_id",
        "summary",
        "detected_at",
        "last_seen_at",
        "dedup_key",
        "status",
        "ack_by",
        "ack_at",
        "resolved_at",
        "resolution_note",
    }
    missing = expected - cols
    assert not missing, f"missing columns: {missing}"


def test_data_quality_report_registered_in_metadata():
    """CLAUDE.md #5：必須在 models/__init__.py 中央 import 才能進 metadata。"""
    assert "data_quality_reports" in Base.metadata.tables


def test_data_quality_report_has_partial_unique_index():
    """ix_dqr_dedup_open 應為 partial unique (status='open')。"""
    indexes = [idx for idx in DataQualityReport.__table__.indexes]
    open_idx = next(
        (idx for idx in indexes if idx.name == "ix_dqr_dedup_open"),
        None,
    )
    assert open_idx is not None
    assert open_idx.unique is True


def test_data_quality_permissions_defined():
    assert Permission.DATA_QUALITY_READ.value == "DATA_QUALITY_READ"
    assert Permission.DATA_QUALITY_WRITE.value == "DATA_QUALITY_WRITE"


def test_data_quality_permission_labels_present():
    assert "DATA_QUALITY_READ" in PERMISSION_LABELS
    assert "DATA_QUALITY_WRITE" in PERMISSION_LABELS


def test_principal_role_template_includes_data_quality():
    """admin 用 WILDCARD 自動覆蓋；principal 需明列。"""
    principal_perms = ROLE_TEMPLATES["principal"]
    assert "DATA_QUALITY_READ" in principal_perms
    assert "DATA_QUALITY_WRITE" in principal_perms


from datetime import date, timedelta


def test_employee_offboard_rule_detects(test_db_session):
    """Employee is_active=True 且 resign_date <= today → 1 條 violation。"""
    from models.employee import Employee
    from services.data_quality.rules.employee_offboard import EmployeeOffboardRule

    emp = Employee(
        employee_id="E999",
        name="測試離職",
        is_active=True,
        resign_date=date.today() - timedelta(days=1),
    )
    test_db_session.add(emp)
    test_db_session.commit()

    rule = EmployeeOffboardRule()
    violations = rule.check(test_db_session)

    assert len(violations) == 1
    v = violations[0]
    assert v.rule_code == "employee_active_but_offboarded"
    assert v.severity == "P1"
    assert v.entity_type == "employee"
    assert v.entity_id == str(emp.id)
    # summary 不含 PII（用 id 不用 name）
    assert "測試離職" not in v.summary
    assert str(emp.id) in v.summary


def test_employee_offboard_rule_skips_inactive(test_db_session):
    """is_active=False（已關旗標）→ 不偵測。"""
    from models.employee import Employee
    from services.data_quality.rules.employee_offboard import EmployeeOffboardRule

    emp = Employee(
        employee_id="E998",
        name="正常離職",
        is_active=False,
        resign_date=date.today() - timedelta(days=10),
    )
    test_db_session.add(emp)
    test_db_session.commit()

    rule = EmployeeOffboardRule()
    assert rule.check(test_db_session) == []


def test_employee_offboard_rule_dedup_key_stable():
    """Violation.dedup_key 為 sha256(rule_code:entity_type:entity_id)[:32]，
    同 (rule, entity_type, entity_id) 跨次呼叫應穩定。"""
    from services.data_quality._base import Violation

    v1 = Violation(
        rule_code="employee_active_but_offboarded",
        severity="P1",
        entity_type="employee",
        entity_id="42",
        summary="x",
    )
    v2 = Violation(
        rule_code="employee_active_but_offboarded",
        severity="P1",
        entity_type="employee",
        entity_id="42",
        summary="y",  # different summary, same dedup
    )
    assert v1.dedup_key == v2.dedup_key
    assert len(v1.dedup_key) == 32


def test_student_stale_active_detects(test_db_session):
    """學生 lifecycle_status 為終態（graduated/withdrawn/transferred）但 is_active 仍 True。"""
    from models.classroom import Student
    from services.data_quality.rules.student_stale_active import StudentStaleActiveRule

    s = Student(
        student_id="S999",
        name="畢業學生",
        is_active=True,
        lifecycle_status="graduated",
    )
    test_db_session.add(s)
    test_db_session.commit()

    rule = StudentStaleActiveRule()
    violations = rule.check(test_db_session)
    assert any(v.entity_id == str(s.id) for v in violations)
    v = next(v for v in violations if v.entity_id == str(s.id))
    assert v.rule_code == "student_active_but_lifecycle_terminal"
    assert v.severity == "P1"
    # PII 不外洩
    assert "畢業學生" not in v.summary


def test_student_stale_active_skips_active_in_school(test_db_session):
    """lifecycle_status=active（非終態）→ 不偵測。"""
    from models.classroom import Student
    from services.data_quality.rules.student_stale_active import StudentStaleActiveRule

    s = Student(
        student_id="S998",
        name="在校",
        is_active=True,
        lifecycle_status="active",
    )
    test_db_session.add(s)
    test_db_session.commit()

    rule = StudentStaleActiveRule()
    violations = rule.check(test_db_session)
    assert not any(v.entity_id == str(s.id) for v in violations)


def test_contact_book_orphan_student_detects(test_db_session):
    """SQLite 不 enforce FK，直接 INSERT 孤兒 row 觸發 rule。"""
    from datetime import date, datetime
    from sqlalchemy import text
    from services.data_quality.rules.contact_book_orphan import ContactBookOrphanRule

    now_iso = datetime.now().isoformat()
    test_db_session.execute(
        text(
            "INSERT INTO student_contact_book_entries "
            "(student_id, classroom_id, log_date, created_at, updated_at) "
            "VALUES (:sid, :cid, :d, :ts, :ts)"
        ),
        {
            "sid": 9999999,
            "cid": 1,
            "d": date.today().isoformat(),
            "ts": now_iso,
        },
    )
    test_db_session.commit()

    rule = ContactBookOrphanRule()
    violations = rule.check(test_db_session)
    assert any(v.entity_type == "contact_book_entry" for v in violations)
    v = next(v for v in violations if v.entity_type == "contact_book_entry")
    assert v.rule_code == "contact_book_orphan_student"
    assert v.severity == "P0"


def test_guardian_orphan_user_detects(test_db_session):
    """Guardian.user_id 指向不存在的 user → 觸發 rule。"""
    from datetime import datetime
    from sqlalchemy import text
    from services.data_quality.rules.guardian_orphan_user import GuardianOrphanRule

    now_iso = datetime.now().isoformat()
    test_db_session.execute(
        text(
            "INSERT INTO guardians "
            "(student_id, user_id, name, relation, "
            " is_primary, is_emergency, can_pickup, sort_order, "
            " created_at, updated_at) "
            "VALUES (:sid, :uid, :nm, :rel, 0, 0, 0, 0, :ts, :ts)"
        ),
        {
            "sid": 1,
            "uid": 9999999,
            "nm": "test guardian",
            "rel": "father",
            "ts": now_iso,
        },
    )
    test_db_session.commit()

    rule = GuardianOrphanRule()
    violations = rule.check(test_db_session)
    assert any(v.entity_type == "guardian" for v in violations)
    v = next(v for v in violations if v.entity_type == "guardian")
    assert v.rule_code == "guardian_orphan_user"
    assert v.severity == "P0"


def test_guardian_orphan_user_skips_null_user(test_db_session):
    """user_id IS NULL（未綁定 LIFF 帳號）→ 不視為孤兒。"""
    from datetime import datetime
    from sqlalchemy import text
    from services.data_quality.rules.guardian_orphan_user import GuardianOrphanRule

    now_iso = datetime.now().isoformat()
    test_db_session.execute(
        text(
            "INSERT INTO guardians "
            "(student_id, user_id, name, relation, "
            " is_primary, is_emergency, can_pickup, sort_order, "
            " created_at, updated_at) "
            "VALUES (:sid, NULL, :nm, :rel, 0, 0, 0, 0, :ts, :ts)"
        ),
        {"sid": 2, "nm": "unbound", "rel": "mother", "ts": now_iso},
    )
    test_db_session.commit()

    rule = GuardianOrphanRule()
    assert rule.check(test_db_session) == []


def test_salary_record_orphan_employee_detects(test_db_session):
    """SalaryRecord.employee_id 指向不存在的 employee → 觸發 rule。"""
    from sqlalchemy import text
    from services.data_quality.rules.salary_no_employee import SalaryOrphanRule

    test_db_session.execute(
        text(
            "INSERT INTO salary_records "
            "(employee_id, salary_year, salary_month, gross_salary) "
            "VALUES (:eid, :y, :m, :g)"
        ),
        {"eid": 9999999, "y": 2026, "m": 5, "g": 0},
    )
    test_db_session.commit()

    rule = SalaryOrphanRule()
    violations = rule.check(test_db_session)
    assert any(v.rule_code == "salary_record_orphan_employee" for v in violations)
    v = next(v for v in violations if v.rule_code == "salary_record_orphan_employee")
    assert v.severity == "P0"
    assert v.entity_type == "salary_record"
    assert "2026" in v.summary and "5" in v.summary


def test_run_all_rules_returns_list_of_violations(test_db_session):
    """run_all_rules 跑全部 5 rule，回傳合併 Violation list。"""
    from services.data_quality.engine import ALL_RULES, run_all_rules

    assert len(ALL_RULES) == 5
    rule_codes = {r.code for r in ALL_RULES}
    assert rule_codes == {
        "employee_active_but_offboarded",
        "student_active_but_lifecycle_terminal",
        "contact_book_orphan_student",
        "guardian_orphan_user",
        "salary_record_orphan_employee",
    }

    violations = run_all_rules(test_db_session)
    assert isinstance(violations, list)
    # 空 DB 預期 0 violation
    assert violations == []


def test_run_all_rules_swallows_per_rule_exception(test_db_session, monkeypatch):
    """單一 rule.check 拋例外不應阻斷其他 rule。"""
    from services.data_quality.engine import run_all_rules
    from services.data_quality.rules.employee_offboard import EmployeeOffboardRule

    def boom(self, session):
        raise RuntimeError("simulated rule crash")

    monkeypatch.setattr(EmployeeOffboardRule, "check", boom)

    violations = run_all_rules(test_db_session)
    # 不會 raise；其他 4 rule 仍跑
    assert isinstance(violations, list)
