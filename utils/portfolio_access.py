"""Portfolio 模組共用存取檢查 helper。

職責：集中學生/班級存取權限檢查，避免每個 router 重寫相同邏輯。

規則：
- admin / hr / supervisor 不受班級限制；可看終態學生（已退學/畢業/轉出）以查歷史
- teacher 只能存取自己擔任導師（head_teacher / assistant_teacher / art_teacher）的班級
  - 終態學生（lifecycle_status in graduated/withdrawn/transferred）對 teacher 立即失效
    （audit 2026-05-07 P0 #5）；要查歷史走 admin/hr/supervisor
- 未分班的學生（classroom_id = NULL）：非 admin 角色不能存取
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from fastapi import HTTPException

from models.classroom import (
    LIFECYCLE_GRADUATED,
    LIFECYCLE_TRANSFERRED,
    LIFECYCLE_WITHDRAWN,
    Classroom,
    Student,
)
from utils.permissions import Permission, has_permission

_UNRESTRICTED_ROLES = frozenset({"admin", "hr", "supervisor"})

# 終態：學生已離校（退學/轉出/畢業）。對 teacher 不可見；管理角色仍可看
# （事後查紀錄、家長申請成績單等用途）。
_TEACHER_BLOCKED_LIFECYCLE = frozenset(
    {LIFECYCLE_GRADUATED, LIFECYCLE_TRANSFERRED, LIFECYCLE_WITHDRAWN}
)


def is_unrestricted(current_user: dict, code: str | None = None) -> bool:
    """管理角色不受班級限制。

    若提供 code（permission code 字串）：以 PermissionGrant.scope == 'all' 為準
        — admin/wildcard 持有者 → True
        — 持有 `<code>:all` 的使用者 → True（自訂角色可表達跨班存取）
        — 持有 `<code>:own_class` 或未持有 → False
    若未提供 code：回退到既有 role-based 判斷（admin/hr/supervisor 為 True）
        — 保留向後相容，~30 處既有呼叫端無需改動
    """
    if code is not None:
        from utils.permissions import resolve_grant

        grant = resolve_grant(current_user, code)
        return grant is not None and grant.scope == "all"
    return current_user.get("role", "") in _UNRESTRICTED_ROLES


def require_unrestricted_role(
    current_user: dict, *, action_label: str = "此操作"
) -> None:
    """限定 admin/hr/supervisor。teacher 等其他角色一律 403。

    用於學生主資料寫入端點（PUT/DELETE /students、bulk-transfer），避免
    teacher 改家長電話、把學生轉到其他班這類敏感動作（policy: 只 admin/hr/
    supervisor 可寫，audit 2026-05-07 P0 #3 #4）。
    """
    if not is_unrestricted(current_user):
        raise HTTPException(
            status_code=403,
            detail=f"{action_label}僅限 admin/hr/supervisor 角色執行",
        )


def assert_all_scope(
    current_user: dict, code: str, *, action_label: str = "此操作"
) -> None:
    """要求 caller 對 ``code`` 持有 ``:all`` scope（或 wildcard / 管理角色），
    否則 403。

    用於「全園彙總 / 跨班匯出」型端點：這類端點語意上回傳全校資料，無法以
    per-row 過濾收斂。gate（``require_*_permission``）是 scope-blind，會放行
    持有 ``code:own_class`` 的自訂角色 → 若不額外檢查即 fail-open（回全園）。

    本守衛確保只有 ``code:all`` / wildcard 持有者可進；持 ``code:own_class``
    的角色一律 403。對標準管理角色（admin wildcard、bare ``code`` 等價 ``:all``）
    **零行為變更**。
    """
    if not is_unrestricted(current_user, code=code):
        raise HTTPException(
            status_code=403,
            detail=f"{action_label}需要全園範圍權限（{code}:all）",
        )


def accessible_classroom_ids(
    session, current_user: dict, code: str | None = None
) -> list[int]:
    """回傳該 user 有權存取的班級 id 清單。

    管理角色（或持有 :all scope 者）回傳 [] 搭配 is_unrestricted() == True
    表示「全放行」。其他角色回傳所擔任的班級 id 清單；若無任何班級則回傳空 list。

    Args:
        code: 若提供，以 PermissionGrant.scope == 'all' 判斷是否 unrestricted；
              否則回退到 role-based 判斷（向後相容）
    """
    if is_unrestricted(current_user, code=code):
        return []
    emp_id = current_user.get("employee_id")
    if not emp_id:
        return []
    classrooms = (
        session.query(Classroom.id)
        .filter(
            # qa-loop #12：過濾已停用班級，與 portal _get_teacher_classroom_ids 對齊，
            # 避免持 :own_class 的教師經 admin 側 scoped 端點看到停用班級學生。
            Classroom.is_active == True,  # noqa: E712
            (Classroom.head_teacher_id == emp_id)
            | (Classroom.assistant_teacher_id == emp_id)
            | (Classroom.art_teacher_id == emp_id),
        )
        .all()
    )
    return [c.id for c in classrooms]


def assert_student_access(
    session, current_user: dict, student_id: int, code: str | None = None
) -> Student:
    """檢查 user 是否可存取該學生；不可則 403。回傳 Student 物件。

    - admin/hr/supervisor：一律放行（含終態學生，供事後查歷史）
    - teacher：僅可存取自己班級且 lifecycle 非終態（graduated/withdrawn/transferred）
      的學生；未分班學生一律禁
    - 學生不存在：raise 404

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容 ~30 個既有 caller）。
    """
    student = session.query(Student).filter(Student.id == student_id).first()
    if not student:
        raise HTTPException(status_code=404, detail="學生不存在")
    if is_unrestricted(current_user, code=code):
        return student
    # teacher 路徑：終態學生立即失效（audit 2026-05-07 P0 #5）
    if student.lifecycle_status in _TEACHER_BLOCKED_LIFECYCLE:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    if not student.classroom_id:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    allowed = accessible_classroom_ids(session, current_user, code=code)
    if student.classroom_id not in allowed:
        raise HTTPException(status_code=403, detail="您無權存取此學生")
    return student


def filter_student_ids_by_access(
    session,
    current_user: dict,
    candidate_ids: Iterable[int],
    code: str | None = None,
) -> set[int]:
    """把一批 student_id 過濾掉該 user 無權存取的。用於 list 端點。

    對 teacher：除班級限制外，亦排除 lifecycle 終態學生
    （graduated/withdrawn/transferred；audit 2026-05-07 P0 #5）。

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容 ~30 個既有 caller）。
    """
    if is_unrestricted(current_user, code=code):
        return set(candidate_ids)
    allowed_classrooms = accessible_classroom_ids(session, current_user, code=code)
    if not allowed_classrooms:
        return set()
    rows = (
        session.query(Student.id)
        .filter(
            Student.id.in_(list(candidate_ids)),
            Student.classroom_id.in_(allowed_classrooms),
            ~Student.lifecycle_status.in_(_TEACHER_BLOCKED_LIFECYCLE),
        )
        .all()
    )
    return {r.id for r in rows}


def has_perm(current_user: dict, permission: Permission) -> bool:
    """檢查 caller 是否持有指定 permission name；委派 utils.permissions.has_permission，
    可正確處理 ["*"]（wildcard 全權限）。"""
    perms = current_user.get("permission_names")
    return has_permission(perms, permission)


def can_view_student_health(current_user: dict) -> bool:
    """是否可檢視學生健康欄位（allergy / medication）。"""
    return has_perm(current_user, Permission.STUDENTS_HEALTH_READ)


def can_view_student_special_needs(current_user: dict) -> bool:
    """是否可檢視學生特殊需求欄位（special_needs）。"""
    return has_perm(current_user, Permission.STUDENTS_SPECIAL_NEEDS_READ)


def can_view_student_pii(current_user: dict) -> bool:
    """Caller 是否可看學生 PII（生日、學號、班級分配）。需 STUDENTS_READ。

    用於跨 router 的次要端點（例：activity/registrations、activity/pos）對學生 PII
    遮罩判斷，避免「ACTIVITY_READ 等次要 perm 拿到學生 PII」型 IDOR
    （F-026 / F-027 / F-028）。
    """
    return has_perm(current_user, Permission.STUDENTS_READ)


def can_view_guardian_pii(current_user: dict) -> bool:
    """Caller 是否可看家長聯絡 PII（電話、Email）。需 GUARDIANS_READ。

    用於 activity/registrations 等次要 router 對家長聯絡資料遮罩判斷
    （F-026）。"""
    return has_perm(current_user, Permission.GUARDIANS_READ)


def mask_student_health_fields(
    student_dict: dict[str, Any], current_user: dict
) -> dict[str, Any]:
    """依 caller 權限遮罩學生健康欄位。

    - 缺 STUDENTS_HEALTH_READ：將 allergy / medication 設為 None
    - 缺 STUDENTS_SPECIAL_NEEDS_READ：將 special_needs 設為 None

    回傳新 dict（不修改原物件）；若 dict 不含對應 key 則維持不變。
    """
    result = dict(student_dict)
    if not can_view_student_health(current_user):
        if "allergy" in result:
            result["allergy"] = None
        if "medication" in result:
            result["medication"] = None
    if not can_view_student_special_needs(current_user):
        if "special_needs" in result:
            result["special_needs"] = None
    return result


def emit_batch_medical_access_log(
    session,
    current_user: dict,
    request,
    student_ids: Iterable[int],
    *,
    reason: str = "批次端點回出醫療欄位（無顯式理由）",
) -> bool:
    """批次端點實際回出 ≥1 名學生的醫療內容時，補寫一筆 §6 batch medical_access_log。

    BE-3-medical-log（OWASP A09）：清單 / 班級彙總 / 今日用藥等端點會一次回出多名
    學生的解密醫療欄位（allergy / medication / dose / note），須與 detail 端點一致
    留下特種個資取用稽核軌跡。

    呼叫端責任：``student_ids`` 只傳「實際回出非空醫療內容」的學生 id 集合
    （已遮罩 / 無內容者勿傳），本 helper 不重查 DB。

    行為：
    - caller 無 STUDENTS_HEALTH_READ（mask 後不會回出醫療）→ 不寫，回 False
    - ``student_ids`` 為空（無任何學生有醫療內容）→ 不寫噪音，回 False
    - 否則寫一筆 field_name=bundle、student_id=None（批次涉及多生）、reason 記涉及人數，回 True

    僅 ``session.add``，不 commit（沿用 caller 的交易）。
    """
    from models.medical_access_log import (
        MEDICAL_ACCESS_PASSIVE,
        MEDICAL_FIELD_BUNDLE,
        MedicalAccessLog,
    )
    from utils.request_ip import get_client_ip

    if not can_view_student_health(current_user):
        return False
    ids = [sid for sid in student_ids if sid is not None]
    if not ids:
        return False

    session.add(
        MedicalAccessLog(
            user_id=current_user.get("user_id"),
            student_id=None,  # 批次涉及多名學生，逐生由 reason 記人數
            field_name=MEDICAL_FIELD_BUNDLE,
            # 批次端點為被動顯示（清單/班級彙總/今日用藥），對齊 medacctype01 的 access_type 語意
            access_type=MEDICAL_ACCESS_PASSIVE,
            reason=f"{reason}（涉及 {len(ids)} 名學生）",
            ip_address=get_client_ip(request) if request is not None else None,
        )
    )
    return True


def get_owned_resource_or_403(
    session,
    model: Any,
    resource_id: int,
    *,
    owner_check: Callable[[Any], bool],
    detail: str = "查無此資料或無權存取",
) -> Any:
    """通用 helper：以 id fetch resource，若不存在或 owner_check 失敗，
    一律 raise 403 + generic detail（不揭露存在性）。

    用於遮蔽「resource 不存在」與「resource 存在但非自己」兩種失敗
    回應差異（IDOR enumeration oracle）。

    Args:
        session: SQLAlchemy session
        model: ORM model class（須有 ``id`` 欄位）
        resource_id: PK of resource to fetch
        owner_check: callable(resource) -> bool. True 表示通過。
        detail: error message（預設遮蔽存在性）

    Returns:
        通過檢查的 resource 物件。

    Raises:
        HTTPException(403): resource 不存在或 ownership 失敗。
    """
    resource = session.query(model).filter(model.id == resource_id).first()
    if resource is None or not owner_check(resource):
        raise HTTPException(status_code=403, detail=detail)
    return resource


def student_ids_in_scope(
    session, current_user: dict, code: str | None = None
) -> list[int] | None:
    """回傳 user 所有可存取的 student_id 清單；管理角色回傳 None（表無限制）。

    用於彙總端點（例：今日用藥）的 WHERE student_id IN (...) 子句。
    對 teacher：排除 lifecycle 終態學生（audit 2026-05-07 P0 #5）。

    Args:
        code: 若提供，以 PermissionGrant.scope 判斷 unrestricted；
              否則回退到 role-based 判斷（向後相容 ~30 個既有 caller）。
    """
    if is_unrestricted(current_user, code=code):
        return None
    allowed_classrooms = accessible_classroom_ids(session, current_user, code=code)
    if not allowed_classrooms:
        return []
    rows = (
        session.query(Student.id)
        .filter(
            Student.classroom_id.in_(allowed_classrooms),
            ~Student.lifecycle_status.in_(_TEACHER_BLOCKED_LIFECYCLE),
        )
        .all()
    )
    return [r.id for r in rows]
