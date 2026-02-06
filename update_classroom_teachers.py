"""更新班級老師資料"""
from models.database import init_database, Employee, Classroom

def update_classroom_teachers():
    engine, SessionLocal = init_database()
    db = SessionLocal()

    try:
        print("開始更新班級老師資料...")

        # 先新增美師（如果不存在）
        art_teachers = [
            {"name": "黃雍娟", "position": "美師"},
            {"name": "黃毓慧", "position": "美師"},
            {"name": "簡佩儀", "position": "美師"},
            {"name": "歐瑞煌", "position": "美師"},
        ]
        
        for at in art_teachers:
            existing = db.query(Employee).filter(Employee.name.like(f"{at['name']}%")).first()
            if not existing:
                count = db.query(Employee).count() + 1
                emp = Employee(
                    employee_id=f"E{count:03d}",
                    name=at["name"],
                    position=at["position"],
                    title="美師",
                    is_active=True
                )
                db.add(emp)
                db.flush()
                print(f"  新增美師: {at['name']}")

        db.commit()

        # 班級老師對應 (根據圖片)
        # 班名: (班導, 副班導, 美師)
        classroom_data = {
            "天堂鳥": ("林佳穎", None, "黃雍娟"),
            "茉莉": ("蔡宜倩", "張庭滋", "黃毓慧"),
            "玫瑰": ("陳品蓁", "王品嫻", "黃雍娟"),
            "薔薇": ("林慧慈", None, "黃毓慧"),
            "百合": ("蔡佩汶", None, "簡佩儀"),
            "櫻花": ("林姿妙", "吳岱鎂", "簡佩儀"),
            "芙蓉": ("郭碧婷", "楊盼任", "簡佩儀"),
            "向日葵": ("楊思瑜", None, "歐瑞煌"),
            "滿天星": ("林家亘", "潘諭慧", "歐瑞煌"),
            "牡丹": ("呂宜凡", None, "歐瑞煌"),
        }

        def find_employee(name):
            if not name:
                return None
            emp = db.query(Employee).filter(Employee.name.like(f"{name}%")).first()
            return emp

        for class_name, (head, asst, art) in classroom_data.items():
            classroom = db.query(Classroom).filter(Classroom.name == class_name).first()
            if classroom:
                head_emp = find_employee(head)
                asst_emp = find_employee(asst)
                art_emp = find_employee(art)

                classroom.head_teacher_id = head_emp.id if head_emp else None
                classroom.assistant_teacher_id = asst_emp.id if asst_emp else None
                classroom.art_teacher_id = art_emp.id if art_emp else None

                # 更新老師的 title
                if head_emp:
                    head_emp.title = "班導"
                    head_emp.classroom_id = classroom.id
                if asst_emp:
                    asst_emp.title = "副班導"
                    asst_emp.classroom_id = classroom.id
                if art_emp:
                    art_emp.title = "美師"

                print(f"  {class_name}: 班導={head or '無'}, 副班導={asst or '無'}, 美師={art}")
            else:
                print(f"  找不到班級: {class_name}")

        db.commit()
        print("\n班級老師資料更新完成！")

    except Exception as e:
        print(f"更新失敗: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    update_classroom_teachers()
