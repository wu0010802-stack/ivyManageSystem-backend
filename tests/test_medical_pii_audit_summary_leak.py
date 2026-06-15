"""兒童醫療特種個資不得原文寫入 audit_summary / log（SEC-002 / 資安掃描 2026-06-15 P2）。

StudentAllergy.allergen 與 StudentMedicationOrder.medication_name 以 EncryptedText
(Fernet) at-rest 加密，明確為防 DB dump/SQLi/DBA（spec 2026-05-28 §3.2）。但
create_allergy / create_medication_order 把明文 allergen / medication_name 塞進
request.state.audit_summary（→ AuditMiddleware 原文寫入 audit_logs.summary 明文欄）
與 logger.info，完全繞過欄位級加密的威脅模型，構成個資法 §6 暴險。

修法：audit_summary / log 改記 alg_id / order_id / 變更欄位名清單（仿安全的
update_allergy 路徑 student_health.py:388 fields=list(data.keys())），不嵌原文。
"""

from datetime import date

from starlette.requests import Request

from api.parent_portal.medications import (
    ParentMedicationOrderCreate,
    create_medication_order as parent_create_medication_order,
)
from api.student_health import (
    AllergyCreate,
    MedicationOrderCreate,
    create_allergy,
    create_medication_order,
)
from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
from models.database import Guardian, User
from models.portfolio import StudentAllergy

SECRET_ALLERGEN = "花生過敏SECRET_PII_XYZ"
SECRET_MED = "管制藥名SECRET_PII_XYZ"

_ADMIN = {
    "user_id": 1,
    "username": "admin",
    "role": "admin",
    "employee_id": 1,
    "permission_names": ["*"],
}


def _seed_student(session) -> int:
    stu = Student(
        student_id="MEDPII01",
        name="醫療生",
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(stu)
    session.commit()
    return stu.id


def _fresh_request() -> Request:
    return Request({"type": "http", "headers": [], "method": "POST", "path": "/"})


def test_create_allergy_does_not_leak_allergen_to_audit_summary(
    test_db_session, caplog
):
    sid = _seed_student(test_db_session)
    req = _fresh_request()
    payload = AllergyCreate(
        allergen=SECRET_ALLERGEN,
        severity="severe",
        reaction_symptom="呼吸困難",
        first_aid_note="施打腎上腺素",
        active=True,
    )
    with caplog.at_level("INFO"):
        create_allergy(sid, payload, req, _ADMIN)

    summary = getattr(req.state, "audit_summary", "")
    assert summary, "audit_summary 應有被設定（確認測試打到正確路徑）"
    assert SECRET_ALLERGEN not in summary, "過敏原原文不得寫入 audit_summary"
    assert SECRET_ALLERGEN not in caplog.text, "過敏原原文不得寫入應用 log"


def test_create_medication_order_does_not_leak_name_to_audit_summary(
    test_db_session, caplog
):
    sid = _seed_student(test_db_session)
    req = _fresh_request()
    payload = MedicationOrderCreate(
        order_date=date(2026, 3, 10),
        medication_name=SECRET_MED,
        dose="1 顆",
        time_slots=["12:00"],
        note=None,
    )
    with caplog.at_level("INFO"):
        create_medication_order(sid, payload, req, _ADMIN)

    summary = getattr(req.state, "audit_summary", "")
    assert summary, "audit_summary 應有被設定（確認測試打到正確路徑）"
    assert SECRET_MED not in summary, "藥名原文不得寫入 audit_summary"
    assert SECRET_MED not in caplog.text, "藥名原文不得寫入應用 log"


# 用同一個 secret token 當藥名與過敏原，使 find_allergy_conflicts 命中（觸發
# warning_note 的 [a.allergen ...] 路徑），一次涵蓋家長端 medication_name 與
# 衝突 allergen 兩個洩漏點。
SECRET_PARENT_MED = "PARENTSECRET_PII_XYZ"


def test_parent_create_medication_order_does_not_leak_to_audit_summary(test_db_session):
    session = test_db_session
    user = User(
        username="p_medpii",
        password_hash="!LINE",
        role="parent",
        permission_names=[],
        is_active=True,
        line_user_id="UMEDPII",
        token_version=0,
    )
    session.add(user)
    session.flush()
    cls = Classroom(name="家長班", is_active=True)
    session.add(cls)
    session.flush()
    stu = Student(
        student_id="PMEDPII01",
        name="家長生",
        classroom_id=cls.id,
        is_active=True,
        lifecycle_status=LIFECYCLE_ACTIVE,
    )
    session.add(stu)
    session.flush()
    session.add(
        Guardian(
            student_id=stu.id,
            user_id=user.id,
            name="家長",
            relation="父親",
            is_primary=True,
        )
    )
    session.add(
        StudentAllergy(
            student_id=stu.id,
            allergen=SECRET_PARENT_MED,  # 與藥名相同 → find_allergy_conflicts 命中
            severity="severe",
            active=True,
        )
    )
    session.flush()

    req = _fresh_request()
    payload = ParentMedicationOrderCreate(
        student_id=stu.id,
        order_date=date(2026, 3, 10),
        medication_name=SECRET_PARENT_MED,
        dose="1 顆",
        time_slots=["12:00"],
        note=None,
        acknowledge_allergy_warning=True,  # 確認後放行，進到 audit_summary 組裝
    )

    parent_create_medication_order(
        payload, req, {"user_id": user.id, "role": "parent"}, session
    )

    summary = getattr(req.state, "audit_summary", "")
    assert summary, "audit_summary 應有被設定（確認測試打到正確路徑）"
    assert (
        SECRET_PARENT_MED not in summary
    ), "家長端藥名與衝突過敏原原文皆不得寫入 audit_summary"
