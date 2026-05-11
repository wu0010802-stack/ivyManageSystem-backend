"""Phase 1 schema and permission tests for MOE reporting module."""

from utils.permissions import (
    Permission,
    PERMISSION_LABELS,
    ROLE_TEMPLATES,
    PERMISSION_GROUPS,
)


def test_gov_reports_view_permission_bit():
    assert Permission.GOV_REPORTS_VIEW.value == 1 << 50


def test_gov_reports_export_permission_bit():
    assert Permission.GOV_REPORTS_EXPORT.value == 1 << 51


def test_gov_reports_permissions_have_labels():
    assert PERMISSION_LABELS["GOV_REPORTS_VIEW"] == "政府申報資料 (檢視)"
    assert PERMISSION_LABELS["GOV_REPORTS_EXPORT"] == "政府申報匯出 (執行)"


def test_admin_role_has_gov_reports_permissions():
    admin_perms = ROLE_TEMPLATES["admin"]
    assert admin_perms & Permission.GOV_REPORTS_VIEW.value
    assert admin_perms & Permission.GOV_REPORTS_EXPORT.value


def test_hr_role_has_gov_reports_view_and_export():
    hr_perms = ROLE_TEMPLATES["hr"]
    assert hr_perms & Permission.GOV_REPORTS_VIEW.value
    assert hr_perms & Permission.GOV_REPORTS_EXPORT.value


def test_supervisor_role_has_view_but_not_export():
    supervisor_perms = ROLE_TEMPLATES["supervisor"]
    assert supervisor_perms & Permission.GOV_REPORTS_VIEW.value
    assert not (supervisor_perms & Permission.GOV_REPORTS_EXPORT.value)


def test_teacher_role_has_no_gov_reports_permissions():
    teacher_perms = ROLE_TEMPLATES["teacher"]
    assert not (teacher_perms & Permission.GOV_REPORTS_VIEW.value)
    assert not (teacher_perms & Permission.GOV_REPORTS_EXPORT.value)


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
