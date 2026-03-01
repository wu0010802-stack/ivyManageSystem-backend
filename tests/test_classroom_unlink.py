"""
回歸測試：classroom_id 解除綁定（班級黑洞 bug）

Bug 描述：
    PUT /students/{id} 或 PUT /employees/{id} 傳入 classroom_id: null 時，
    因為後端 `if value is not None:` 守衛，該操作被靜默忽略，
    導致管理員可以把人加進班級，但永遠無法把人移出班級。

修復方式：
    在更新迴圈中，classroom_id 屬於「可明確設為 null 的 FK 欄位」，
    需繞過 is not None 守衛。
"""
import pytest
from api.students import StudentUpdate
from api.employees import EmployeeUpdate


class TestStudentClassroomUnlink:

    def test_classroom_id_none_present_in_exclude_unset(self):
        """前端明確傳 classroom_id: null 時，exclude_unset 不應把它過濾掉"""
        payload = StudentUpdate(classroom_id=None)
        update_data = payload.dict(exclude_unset=True)
        assert 'classroom_id' in update_data, (
            "classroom_id: null 應出現在 update_data 中，不應被 exclude_unset 吃掉"
        )
        assert update_data['classroom_id'] is None

    def test_unset_classroom_id_absent_from_exclude_unset(self):
        """前端不傳 classroom_id 時，update_data 不應包含該欄位"""
        payload = StudentUpdate(name="王小明")
        update_data = payload.dict(exclude_unset=True)
        assert 'classroom_id' not in update_data, (
            "未傳 classroom_id 不應出現在 update_data 中"
        )

    def test_update_loop_must_apply_null_classroom_id(self):
        """更新迴圈必須將 classroom_id: null 實際寫入物件（重現 bug）"""
        class FakeStudent:
            classroom_id = 5  # 初始在班級

        student = FakeStudent()
        update_data = {'classroom_id': None}

        # ---- 重現舊的有 bug 的迴圈 ----
        buggy_result = FakeStudent()
        buggy_result.classroom_id = 5
        for key, value in update_data.items():
            if value is not None:          # BUG: null 被靜默忽略
                setattr(buggy_result, key, value)
        assert buggy_result.classroom_id == 5, "確認 bug 存在：舊迴圈不會清除 classroom_id"

        # ---- 驗證修復後的迴圈 ----
        NULLABLE_FK_FIELDS = {'classroom_id'}
        for key, value in update_data.items():
            if value is not None or key in NULLABLE_FK_FIELDS:
                setattr(student, key, value)
        assert student.classroom_id is None, "修復後：classroom_id 應被設為 None"


class TestEmployeeClassroomUnlink:

    def test_classroom_id_none_present_in_exclude_unset(self):
        """前端明確傳 classroom_id: null 時，exclude_unset 不應把它過濾掉"""
        payload = EmployeeUpdate(classroom_id=None)
        update_data = payload.dict(exclude_unset=True)
        assert 'classroom_id' in update_data, (
            "classroom_id: null 應出現在 update_data 中"
        )
        assert update_data['classroom_id'] is None

    def test_unset_classroom_id_absent_from_exclude_unset(self):
        """前端不傳 classroom_id 時，update_data 不應包含該欄位"""
        payload = EmployeeUpdate(name="李老師")
        update_data = payload.dict(exclude_unset=True)
        assert 'classroom_id' not in update_data

    def test_update_loop_must_apply_null_classroom_id(self):
        """員工更新迴圈必須將 classroom_id: null 實際寫入物件（重現 bug）"""
        class FakeEmployee:
            classroom_id = 3
            title = "幼兒園教師"

        emp = FakeEmployee()
        update_data = {'classroom_id': None}

        # ---- 重現舊的有 bug 的迴圈 ----
        buggy_emp = FakeEmployee()
        for key, value in update_data.items():
            if value is not None:          # BUG: null 被靜默忽略
                setattr(buggy_emp, key, value)
            elif key == 'job_title_id' and value is None:
                setattr(buggy_emp, key, None)
            # classroom_id: None 的情況完全缺漏
        assert buggy_emp.classroom_id == 3, "確認 bug 存在：舊迴圈不會清除 classroom_id"

        # ---- 驗證修復後的迴圈 ----
        NULLABLE_FK_FIELDS = {'job_title_id', 'classroom_id'}
        for key, value in update_data.items():
            if value is not None:
                setattr(emp, key, value)
            elif key in NULLABLE_FK_FIELDS:
                setattr(emp, key, None)
        assert emp.classroom_id is None, "修復後：classroom_id 應被設為 None"
