"""delete_employee 軟刪除（設為離職）須撤銷連動 User 帳號。

qa-loop round2（2026-06-29）P1：DELETE /api/employees/{id}（docstring 寫「軟刪除，設為
離職」）只設 Employee.is_active=False + resign_date，完全不碰連動 User；但登入與所有權限守衛
（utils/auth.py）一路只檢 User.is_active，從不看 Employee.is_active → 走 DELETE 路徑離職的
員工 User 仍 active，可繼續登入 / refresh / 以原角色全權存取管理端 API。canonical
process_offboarding 會跑 revoke_user step，DELETE 路徑卻不會 → 兩條離職路徑撤權不一致。

修法：delete_employee 對 resign_date<=today 的軟刪除，呼叫共用 revoke_active_user_account
（與 offboarding revoke_user step 同一口徑）撤 User：is_active=False + token_version++ +
撤所有 staff_refresh family。
"""

from __future__ import annotations

from unittest.mock import MagicMock

from models.database import Employee, User
from models.staff_refresh_token import StaffRefreshToken
from utils.auth import hash_password
from utils.taipei_time import now_taipei_naive
import api.employees as emp_api


def _mk_employee_with_user(session, emp_active=True):
    emp = Employee(employee_id="DELU01", name="離職測試員", is_active=emp_active)
    session.add(emp)
    session.flush()
    user = User(
        username="deluser",
        password_hash=hash_password("p"),
        role="hr",
        is_active=True,
        token_version=0,
        employee_id=emp.id,
        must_change_password=False,
    )
    session.add(user)
    session.flush()
    session.add(
        StaffRefreshToken(
            user_id=user.id,
            token_hash="a" * 64,
            expires_at=now_taipei_naive(),
        )
    )
    session.commit()
    return emp, user


def test_delete_employee_revokes_linked_user(test_db_session):
    """軟刪除在職員工 → 連動 User 停用 + token_version bump + refresh family 撤銷。"""
    session = test_db_session
    emp, user = _mk_employee_with_user(session)
    uid = user.id

    emp_api.delete_employee(
        emp.id, request=MagicMock(), current_user={"user_id": 999, "role": "admin"}
    )

    session.expire_all()
    refreshed = session.query(User).filter(User.id == uid).first()
    assert refreshed.is_active is False, "離職員工的 User 帳號應被停用"
    assert refreshed.token_version == 1, "token_version 應 bump 使既有 cookie 立即失效"
    fam = (
        session.query(StaffRefreshToken)
        .filter(StaffRefreshToken.user_id == uid)
        .first()
    )
    assert fam.revoked_at is not None, "應撤銷該 user 的 staff_refresh family"


def test_delete_employee_already_inactive_user_no_crash(test_db_session):
    """員工已離職（無 active User）→ 不應因撤 User 失敗，照常完成。"""
    session = test_db_session
    emp = Employee(employee_id="DELU02", name="無帳號員", is_active=True)
    session.add(emp)
    session.commit()

    res = emp_api.delete_employee(
        emp.id, request=MagicMock(), current_user={"user_id": 999, "role": "admin"}
    )
    assert res["id"] == emp.id
