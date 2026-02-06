"""更新員工職稱分類"""
from models.database import init_database, Employee

def update_positions():
    engine, SessionLocal = init_database()
    db = SessionLocal()

    try:
        print("開始更新員工職稱分類...")

        # 職稱分類對照表 (根據圖片)
        position_data = {
            "園長": ["呂麗珍"],
            "幼兒園教師": ["呂姿妙", "林麗花", "郭攸秀", "蔡佩汶", "王品玲", "林慧慈", "陳晨晞"],
            "教保員": ["呂宜凡", "孔祥盈", "林誓翎", "郭碧婷", "林家宜", "蔡宜倩", "林佳穎", "呂伐賢"],
            "助理教保員": ["吳泷倫", "楊盼任", "陳益超"],
            "司機": ["吳禹喬"],
            "廚工": ["王麗慧", "陳紅伊", "王品嫻", "楊思瑜"],
            "職員": ["潘諭慧", "楊恩慧", "張庭滋"]
        }

        # 先建立缺少的員工
        def get_or_create(name, position):
            emp = db.query(Employee).filter(Employee.name.like(f"{name}%")).first()
            if not emp:
                count = db.query(Employee).count() + 1
                emp = Employee(
                    employee_id=f"E{count:03d}",
                    name=name,
                    position=position,
                    is_active=True
                )
                db.add(emp)
                db.flush()
                print(f"  新增: {name} ({position})")
            else:
                emp.position = position
                print(f"  更新: {name} -> {position}")
            return emp

        for position, names in position_data.items():
            for name in names:
                get_or_create(name, position)

        db.commit()
        print("員工職稱分類更新完成！")

    except Exception as e:
        print(f"更新失敗: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    update_positions()
