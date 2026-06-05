"""scripts/seed_e2e_ci.py — CI 專用 e2e smoke 最小 seed。

在「空 DB（已 Base.metadata.create_all + alembic stamp heads）」上建立 e2e
critical-path smoke 所需的最小資料：

1. 一個 role=admin 帳號（permission_names=["*"]，employee_id=None）。
   e2e globalSetup 用它登入拿 storageState；admin.employee_id=None 確保不等於
   target 員工 id（attendance/leave 端點 self-guard 否則 422）。
2. 一個月薪（regular）、在職、**非 admin 本人**的員工，補齊薪資/工時欄位讓
   /salaries/simulate 能算（bypass_standard_base=True → 引擎直接吃 base_salary，
   不依賴 PositionSalaryConfig）。

冪等：以工號（Employee.employee_id）與 username 為鍵 upsert，重複執行只補缺/
重設密碼，不會重複建。

**不用 os.getenv**（ci.yml config-gate 禁 scripts/ 讀 env；改 argparse 收參數）。
target 員工的 PK id 印到 stdout 最後一行 `E2E_TEST_EMPLOYEE_ID=<id>`，由 workflow
的 shell 捕捉寫進 $GITHUB_ENV，供 globalSetup 讀。

用法（CI）：
    DATABASE_URL=... python scripts/seed_e2e_ci.py \
        --admin-username e2e_admin --admin-password <pw>
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import models.database  # noqa: F401  （註冊所有 model，供 session 操作）
from models.auth import User
from models.base import session_scope
from models.employee import Employee, EmployeeType
from utils.auth import hash_password

# log 走 stderr，讓 stdout 只留 E2E_TEST_EMPLOYEE_ID=<id> 給 workflow 解析
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("seed_e2e_ci")

# e2e 測試員工的固定工號（CI 空 DB 唯一，PK id 由 DB autoincrement 後回讀）
E2E_EMPLOYEE_NUMBER = "E2E-CI-0001"
E2E_EMPLOYEE_NAME = "E2E CI 測試員工"


def _seed_admin(session, username: str, password: str) -> None:
    user = session.query(User).filter_by(username=username).first()
    if user:
        user.password_hash = hash_password(password)
        user.role = "admin"
        user.permission_names = ["*"]
        user.employee_id = None  # 與 target 員工 id 不相等（self-guard）
        user.is_active = True
        user.must_change_password = False
        logger.info("重設 admin 帳號: %s", username)
        return
    session.add(
        User(
            username=username,
            password_hash=hash_password(password),
            role="admin",
            permission_names=["*"],
            employee_id=None,
            is_active=True,
            must_change_password=False,
        )
    )
    logger.info("建立 admin 帳號: %s", username)


def _seed_employee(session) -> int:
    """建/取月薪在職測試員工，回傳其 PK id。"""
    emp = session.query(Employee).filter_by(employee_id=E2E_EMPLOYEE_NUMBER).first()
    if emp is None:
        emp = Employee(employee_id=E2E_EMPLOYEE_NUMBER, name=E2E_EMPLOYEE_NAME)
        session.add(emp)
        logger.info("建立測試員工: %s (%s)", E2E_EMPLOYEE_NAME, E2E_EMPLOYEE_NUMBER)
    else:
        logger.info("沿用既有測試員工: %s (%s)", emp.name, E2E_EMPLOYEE_NUMBER)

    # 月薪 + 薪資/工時欄位（對齊 seed_dev_finalize：bypass_standard_base 讓引擎
    # 直接用 base_salary，不依賴 PositionSalaryConfig）
    emp.name = E2E_EMPLOYEE_NAME
    emp.employee_type = EmployeeType.REGULAR.value  # 月薪
    emp.position = emp.position or "職員"
    emp.base_salary = 36000
    emp.hourly_rate = 0
    emp.bypass_standard_base = True
    emp.insurance_salary_level = 36000
    emp.work_start_time = "08:00"
    emp.work_end_time = "17:00"
    emp.hire_date = emp.hire_date or date(2024, 1, 1)
    emp.dependents = 0
    emp.no_employment_insurance = False
    emp.pension_self_rate = 0.0
    emp.insurance_effective_date = emp.insurance_effective_date or emp.hire_date
    emp.is_active = True
    emp.resign_date = None

    session.flush()  # 取得 autoincrement PK id
    return int(emp.id)


def main() -> int:
    parser = argparse.ArgumentParser(description="CI e2e smoke 最小 seed")
    parser.add_argument("--admin-username", default="e2e_admin")
    parser.add_argument("--admin-password", default="e2e_admin_pw_ci_only")
    args = parser.parse_args()

    with session_scope() as session:
        _seed_admin(session, args.admin_username, args.admin_password)
        emp_id = _seed_employee(session)

    logger.info("完成。target 員工 PK id = %d", emp_id)
    # stdout 最後一行供 workflow shell 解析；其餘 log 全走 stderr
    print(f"E2E_TEST_EMPLOYEE_ID={emp_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
