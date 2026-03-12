"""
為所有尚未有帳號的員工批次建立使用者帳號

Username：工號（employee_id）
密碼：Abc12345（首次登入強制修改）
角色：teacher（預設）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.database import get_session, Employee, User
from utils.auth import hash_password
from utils.permissions import get_role_default_permissions

DEFAULT_PASSWORD = "Abc12345"
DEFAULT_ROLE = "teacher"


def main():
    session = get_session()
    try:
        # 查詢所有在職員工
        employees = session.query(Employee).filter(Employee.is_active == True).all()

        # 取得已有帳號的 employee_id 集合
        existing_user_emp_ids = {
            u.employee_id
            for u in session.query(User.employee_id).filter(User.employee_id != None).all()
        }

        created = []
        skipped = []

        for emp in employees:
            if emp.id in existing_user_emp_ids:
                skipped.append(f"  [略過] {emp.name}（工號 {emp.employee_id}）— 帳號已存在")
                continue

            # 確保工號不重複（作為 username）
            username = emp.employee_id
            if session.query(User).filter(User.username == username).first():
                skipped.append(f"  [略過] {emp.name}（工號 {emp.employee_id}）— username 已被使用")
                continue

            user = User(
                employee_id=emp.id,
                username=username,
                password_hash=hash_password(DEFAULT_PASSWORD),
                role=DEFAULT_ROLE,
                permissions=get_role_default_permissions(DEFAULT_ROLE),
                must_change_password=True,
            )
            session.add(user)
            created.append(f"  [建立] {emp.name}（工號 {emp.employee_id}，username: {username}）")

        session.commit()

        print(f"\n=== 批次建立帳號完成 ===")
        print(f"建立：{len(created)} 筆，略過：{len(skipped)} 筆\n")

        if created:
            print("✅ 已建立：")
            for line in created:
                print(line)

        if skipped:
            print("\n⚠️  略過：")
            for line in skipped:
                print(line)

        print(f"\n預設密碼：{DEFAULT_PASSWORD}（首次登入強制修改）")

    except Exception as e:
        session.rollback()
        print(f"❌ 發生錯誤：{e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()
