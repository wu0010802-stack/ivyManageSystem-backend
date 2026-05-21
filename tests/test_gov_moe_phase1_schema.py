"""Phase 1 schema and permission tests for MOE reporting module."""

from utils.permissions import (
    Permission,
    PERMISSION_LABELS,
    ROLE_TEMPLATES,
    PERMISSION_GROUPS,
    WILDCARD,
    has_permission,
)


def test_gov_reports_view_permission_name():
    assert Permission.GOV_REPORTS_VIEW.value == "GOV_REPORTS_VIEW"


def test_gov_reports_export_permission_name():
    assert Permission.GOV_REPORTS_EXPORT.value == "GOV_REPORTS_EXPORT"


def test_gov_reports_permissions_have_labels():
    assert PERMISSION_LABELS["GOV_REPORTS_VIEW"] == "政府申報資料 (檢視)"
    assert PERMISSION_LABELS["GOV_REPORTS_EXPORT"] == "政府申報匯出 (執行)"


def test_admin_role_has_gov_reports_permissions():
    admin_perms = ROLE_TEMPLATES["admin"]
    # admin 角色為 wildcard ["*"]：has_permission 經 WILDCARD 快徑回 True
    assert has_permission(admin_perms, Permission.GOV_REPORTS_VIEW)
    assert has_permission(admin_perms, Permission.GOV_REPORTS_EXPORT)
    assert admin_perms == [WILDCARD]


def test_hr_role_has_gov_reports_view_and_export():
    hr_perms = ROLE_TEMPLATES["hr"]
    assert has_permission(hr_perms, Permission.GOV_REPORTS_VIEW)
    assert has_permission(hr_perms, Permission.GOV_REPORTS_EXPORT)


def test_supervisor_role_has_view_but_not_export():
    supervisor_perms = ROLE_TEMPLATES["supervisor"]
    assert has_permission(supervisor_perms, Permission.GOV_REPORTS_VIEW)
    assert not has_permission(supervisor_perms, Permission.GOV_REPORTS_EXPORT)


def test_teacher_role_has_no_gov_reports_permissions():
    teacher_perms = ROLE_TEMPLATES["teacher"]
    assert not has_permission(teacher_perms, Permission.GOV_REPORTS_VIEW)
    assert not has_permission(teacher_perms, Permission.GOV_REPORTS_EXPORT)


from models.classroom import Student
from models.employee import Employee


def test_student_has_id_number_field():
    assert hasattr(Student, "id_number")


def test_student_has_disability_fields():
    for f in (
        "nationality",
        "household_address",
        "is_disadvantaged",
        "low_income_status",
        "indigenous_status",
        "disability_type",
        "disability_level",
        "disability_cert_no",
        "disability_cert_expiry",
    ):
        assert hasattr(Student, f), f"Student missing field: {f}"


def test_employee_has_staff_role_category_field():
    for f in ("staff_role_category", "teacher_cert_no", "teacher_cert_type"):
        assert hasattr(Employee, f), f"Employee missing field: {f}"


def test_disability_document_model_importable():
    from models.gov_moe import StudentDisabilityDocument

    assert StudentDisabilityDocument.__tablename__ == "student_disability_documents"


def test_iep_record_model_importable():
    from models.gov_moe import StudentIEPRecord

    assert StudentIEPRecord.__tablename__ == "student_iep_records"


def test_special_subsidy_model_importable():
    from models.gov_moe import SpecialEducationSubsidy

    assert SpecialEducationSubsidy.__tablename__ == "special_education_subsidies"


def test_monthly_snapshot_model_importable():
    from models.gov_moe import MonthlyEnrollmentSnapshot

    assert MonthlyEnrollmentSnapshot.__tablename__ == "monthly_enrollment_snapshots"


# ---------------------------------------------------------------------------
# Migration schema verification (via SQLAlchemy metadata — no live DB needed)
# ---------------------------------------------------------------------------


def test_migration_added_student_columns():
    from models.base import Base
    import models.database  # noqa: F401 — ensure all models are registered

    cols = {c.name for c in Base.metadata.tables["students"].columns}
    assert "id_number" in cols
    assert "disability_cert_expiry" in cols


def test_migration_added_employee_columns():
    from models.base import Base
    import models.database  # noqa: F401

    cols = {c.name for c in Base.metadata.tables["employees"].columns}
    assert "staff_role_category" in cols
    assert "teacher_cert_no" in cols


def test_migration_created_disability_documents_table():
    from models.base import Base
    import models.database  # noqa: F401

    assert "student_disability_documents" in Base.metadata.tables


def test_migration_created_shell_tables():
    from models.base import Base
    import models.database  # noqa: F401

    names = set(Base.metadata.tables)
    assert {
        "student_iep_records",
        "special_education_subsidies",
        "monthly_enrollment_snapshots",
    }.issubset(names)


# ---------------------------------------------------------------------------
# Pydantic round-trip tests (Task 5)
# ---------------------------------------------------------------------------

from datetime import date
from api.students import StudentCreate


def test_student_create_accepts_new_fields():
    payload = {
        "student_id": "TEST001",
        "name": "測試幼生",
        "id_number": "A123456789",
        "nationality": "本國",
        "is_disadvantaged": True,
        "low_income_status": "low",
        "disability_type": "自閉症",
        "disability_level": "中度",
        "disability_cert_no": "TPE-2026-001",
        "disability_cert_expiry": "2027-12-31",
    }
    obj = StudentCreate(**payload)
    assert obj.id_number == "A123456789"
    assert obj.disability_cert_expiry == date(2027, 12, 31)


from api.employees import EmployeeCreate


def test_employee_create_accepts_new_fields():
    payload = {
        "employee_id": "E001",
        "name": "測試教師",
        "employee_type": "regular",
        "staff_role_category": "teacher_certified",
        "teacher_cert_no": "EC-2020-001",
        "teacher_cert_type": "幼教師證",
    }
    obj = EmployeeCreate(**payload)
    assert obj.staff_role_category == "teacher_certified"
