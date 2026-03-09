"""更新員工職稱分類 - 修正版"""
from models.database import init_database, Employee

def update_positions():
    engine, SessionLocal = init_database()
    db = SessionLocal()

    try:
        print("開始更新員工職稱分類...")

        # 先清除所有 position
        db.query(Employee).update({Employee.position: None})
        db.flush()

        # 正確的職稱分類對照表 (根據圖片)
        position_data = {
            "園長": ["呂麗珍"],
            "幼兒園教師": ["林姿妙", "林麗花"],
            "教保員": ["郭攸秀", "蔡佩汶", "王雅玲", "林慧慈", "陳品蓁", "呂宜凡", "孔祥盈", "郭碧婷", "林家亘", "蔡宜倩", "林佳穎"],
            "助理教保員": ["吳岱鎂", "楊盼任"],
            "司機": ["吳逸倫", "陳益超"],
            "廚工": ["王麗慧"],
            "職員": ["吳逸喬", "陳紅伊", "王品嫻", "楊思瑜", "潘諭慧", "張庭滋"]
        }

        def get_or_create(name, position):
            # 只用名字的前幾個字匹配（忽略英文名）
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
                print(f"  更新: {emp.name} -> {position}")
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
