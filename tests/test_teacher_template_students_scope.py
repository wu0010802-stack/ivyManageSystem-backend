"""教師模板須含 own_class scoped 學生讀寫權（2026-06-15 運作探測 P2-3）。

Bug：api/portal/incidents.py、api/portal/assessments.py 以 require_permission
  (STUDENTS_READ / STUDENTS_WRITE) 守衛（不走 require_staff_permission，故不會被
  teacher 短路擋掉），但 teacher ROLE_TEMPLATE 與 DB roles.teacher 都缺 STUDENTS_READ
  → 全體教師（19/20 為 NULL-perm 走模板）讀/寫自己班的事件紀錄與學期評量被 403，
  兩頁對教師形同壞掉。

修：模板補 STUDENTS_READ:own_class / STUDENTS_WRITE:own_class（須 :own_class，
  bare code 會被 resolve 成 :all → 對 NULL-perm 教師提權為全園）。
  注意安全性：所有 teacher 可達的 STUDENTS_READ/WRITE 端點皆 own_class self-filter
  （assert_student_access / _get_teacher_student_ids），而主管理端點走
  require_staff_permission 對 teacher 一律 403，故不會洩漏全園。
"""

from utils.permissions import ROLE_TEMPLATES, Permission, has_permission


def test_teacher_template_has_scoped_students_read_write():
    teacher = ROLE_TEMPLATES["teacher"]
    assert "STUDENTS_READ:own_class" in teacher
    assert "STUDENTS_WRITE:own_class" in teacher
    # 不可有 bare code（會被 resolve 成 :all → 提權）
    assert "STUDENTS_READ" not in teacher
    assert "STUDENTS_WRITE" not in teacher


def test_teacher_template_passes_portal_permission_check():
    """portal incidents/assessments 走 require_permission(STUDENTS_READ/WRITE)，
    教師（resolve 自模板）的 :own_class grant 應通過 has_permission。"""
    teacher = ROLE_TEMPLATES["teacher"]
    assert has_permission(teacher, Permission.STUDENTS_READ)
    assert has_permission(teacher, Permission.STUDENTS_WRITE)
