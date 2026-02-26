"""重置員工資料 - 插入正確的 25 位員工"""
from models.database import init_database, Employee

def reset_employees():
    engine, SessionLocal = init_database()
    db = SessionLocal()

    try:
        # 正確的 25 位員工資料
        employees_data = [
            # 園長 (1)
            {"name": "呂麗珍", "position": "園長"},
            # 幼兒園教師 (2)
            {"name": "林姿妙", "position": "幼兒園教師"},
            {"name": "林麗花", "position": "幼兒園教師"},
            # 教保員 (11)
            {"name": "郭攸秀", "position": "教保員"},
            {"name": "蔡佩汶", "position": "教保員"},
            {"name": "王雅玲", "position": "教保員"},
            {"name": "林慧慈", "position": "教保員"},
            {"name": "陳品蓁", "position": "教保員"},
            {"name": "呂宜凡", "position": "教保員"},
            {"name": "孔祥盈", "position": "教保員"},
            {"name": "郭碧婷", "position": "教保員"},
            {"name": "林家亘", "position": "教保員"},
            {"name": "蔡宜倩", "position": "教保員"},
            {"name": "林佳穎", "position": "教保員"},
            # 助理教保員 (2)
            {"name": "吳岱鎂", "position": "助理教保員"},
            {"name": "楊盼任", "position": "助理教保員"},
            # 司機 (2)
            {"name": "吳逸倫", "position": "司機"},
            {"name": "陳益超", "position": "司機"},
            # 廚工 (1)
            {"name": "王麗慧", "position": "廚工"},
            # 職員 (6)
            {"name": "吳逸喬", "position": "職員"},
            {"name": "陳紅伊", "position": "職員"},
            {"name": "王品嫻", "position": "職員"},
            {"name": "楊思瑜", "position": "職員"},
            {"name": "潘諭慧", "position": "職員"},
            {"name": "張庭滋", "position": "職員"},
        ]

        print("正在插入新員工資料...")
        for i, emp_data in enumerate(employees_data, 1):
            emp = Employee(
                employee_id=f"E{i:03d}",
                name=emp_data["name"],
                position=emp_data["position"],
                is_active=True
            )
            db.add(emp)
            print(f"  {i}. {emp_data['name']} ({emp_data['position']})")

        db.commit()
        print(f"\n完成！共插入 {len(employees_data)} 位員工。")

    except Exception as e:
        print(f"操作失敗: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    reset_employees()
