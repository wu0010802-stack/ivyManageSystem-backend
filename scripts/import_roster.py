from models.database import init_database, Employee, ClassGrade, Classroom, Student
from sqlalchemy.orm import Session
from datetime import datetime

def import_data():
    engine, SessionLocal = init_database()
    db = SessionLocal()

    try:
        print("開始匯入資料...")

        # 1. 建立年級
        grades = {
            "大班": ClassGrade(name="大班", sort_order=1),
            "中班": ClassGrade(name="中班", sort_order=2),
            "小班": ClassGrade(name="小班", sort_order=3),
            "幼幼班": ClassGrade(name="幼幼班", sort_order=4)
        }
        
        grade_map = {}
        for name, grade in grades.items():
            existing = db.query(ClassGrade).filter_by(name=name).first()
            if not existing:
                db.add(grade)
                db.flush()
                grade_map[name] = grade
            else:
                grade_map[name] = existing
        
        print("年級建立完成")

        # 2. 建立老師與教職員
        def get_or_create_employee(name, title=None, emp_id_prefix="T"):
            emp = db.query(Employee).filter(Employee.name.like(f"{name}%")).first()
            if not emp:
                count = db.query(Employee).count() + 1
                emp_id = f"{emp_id_prefix}{count:03d}"
                emp = Employee(
                    employee_id=emp_id,
                    name=name,
                    title=title,
                    is_active=True
                )
                db.add(emp)
                db.flush()
            return emp

        # 3. 班級資料定義
        class_data = [
            # 大班
            {"code": "114-11", "name": "天堂鳥", "grade": "大班", "head": "林佳穎Anny", "asst": "", "art": "黃雍娟Jessica"},
            {"code": "114-12", "name": "茉莉", "grade": "大班", "head": "蔡宜倩Mina", "asst": "", "art": "黃毓慧Ivy"},
            # 中班
            {"code": "114-21", "name": "玫瑰", "grade": "中班", "head": "陳品蓁Fenny", "asst": "張庭滋Gina", "art": "黃雍娟Jessica"},
            {"code": "114-22", "name": "薔薇", "grade": "中班", "head": "林慧慈Daisy", "asst": "王品婕Eve", "art": "黃毓慧Ivy"},
            # 小班
            {"code": "114-31", "name": "百合", "grade": "小班", "head": "蔡佩汶Peggy", "asst": "", "art": "簡佩儀Tiffany"},
            {"code": "114-32", "name": "櫻花", "grade": "小班", "head": "林姿妙Linda", "asst": "吳岱鎂Debby", "art": "簡佩儀Tiffany"},
            {"code": "114-33", "name": "芙蓉", "grade": "小班", "head": "郭碧婷Kirby", "asst": "楊盼任Mia", "art": "簡佩儀Tiffany"},
            # 幼幼班
            {"code": "114-41", "name": "向日葵", "grade": "幼幼班", "head": "楊思瑜Anna", "asst": "", "art": "歐瑞煌Johnny"},
            {"code": "114-42", "name": "滿天星", "grade": "幼幼班", "head": "林家宜Maryna", "asst": "", "art": "歐瑞煌Johnny"},
            {"code": "114-43", "name": "牡丹", "grade": "幼幼班", "head": "呂宜凡Eva", "asst": "潘諭慧Ilona", "art": "歐瑞煌Johnny"}
        ]

        class_map = {}
        for c in class_data:
            grade_obj = grade_map[c["grade"]]
            head = get_or_create_employee(c["head"], "班導師") if c["head"] else None
            asst = get_or_create_employee(c["asst"], "副班導") if c["asst"] else None
            art = get_or_create_employee(c["art"], "美師") if c["art"] else None
            
            classroom = db.query(Classroom).filter_by(name=c["name"]).first()
            if not classroom:
                classroom = Classroom(
                    name=c["name"],
                    class_code=c["code"],
                    grade_id=grade_obj.id,
                    head_teacher_id=head.id if head else None,
                    assistant_teacher_id=asst.id if asst else None,
                    art_teacher_id=art.id if art else None,
                    is_active=True
                )
                db.add(classroom)
                db.flush()
            else:
                # Update existing
                classroom.class_code = c["code"]
                classroom.grade_id = grade_obj.id
                classroom.head_teacher_id = head.id if head else None
                classroom.assistant_teacher_id = asst.id if asst else None
                classroom.art_teacher_id = art.id if art else None

            class_map[c["name"]] = classroom

        print("班級與老師建立完成")

        # 4. 學生名單
        # [name, tag]
        # tags: "新生", "不足齡", "特教生", "原住民" or None
        
        roster = {
            "天堂鳥": [
                ("吳宥諄", None), ("楊祤妍", None), ("張宸婕", None), ("葉語析", None), ("王思予", None), 
                ("柯安祺", None), ("邱已瑄", None), ("張郡軒", None), ("馬昀安", None), ("薛和溱", None),
                ("蔡欣紜", None), ("王妍喬", "不足齡"), ("林敬尹", None), ("潘有農", None), ("林宇哲", None),
                ("邱宇炘", None), ("江威逸", None), ("李婕安", None), ("劉品沂", None), ("高可威", None),
                ("吳嘉婷", None)
            ],
            "茉莉": [
                ("林晞妤", None), ("王睿承", None), ("林楷霏", None), ("李欣宸", None), ("曹允碩", None), 
                ("黃亦圻", None), ("吳杰恩", None), ("潘柔霏", None), ("賴子詮", None), ("鄭亦榆", None), 
                ("許 歆", None), ("蕭 瑜", None), ("孫楷倫", None), ("呂天樂", None)
            ],
            "玫瑰": [
                ("王品勻", None), ("王睿歆", None), ("黃沖澄", None), ("涂瑀禧", None), ("張宸愷", None), 
                ("陳品綸", None), ("陳品維", None), ("鄭立昇", None), ("葉沐希", None), ("唐于喬", "新生"), 
                ("王睿菲", None), ("唐得翰", None), ("鄧翊甫", None), ("黃莞淇", "新生"), ("陳向則", "新生"), 
                ("陳向甫", "新生"), ("徐瑞勛", "新生")
            ],
            "薔薇": [
                ("鄭承有", None), ("王正瀧", None), ("陳有覺", None), ("吳芫均", None), ("洪晟硯", None), 
                ("黃毅鋐", None), ("高愷均", None), ("陳韋廷", None), ("陳禾宸", None), ("袁 馳", None), 
                ("陳彥臻", None), ("孔宇飛", None), ("林詠恩", None), ("黃豐恩", None), ("林玗霏", None), 
                ("黃律嘉", None), ("孫圓艾", None), ("方詠琳", None), ("王妍慈", None), ("陳品鴻", None),
                ("陳宥安", None), ("鄭羽家", None), ("陳品芮", None), ("蔡秉霖", "新生"), ("高苡樂", "新生")
            ],
            "百合": [
                ("顏嘉余", None), ("程柏鈞", None), ("鄭宇程", None), ("王宥淨", None), ("林秉丞", None), 
                ("林承諺", None), ("許孟妘", "特教生"), ("鄒采恩", None), ("朱禕軒", None), ("陳語彤", None), 
                ("賴庭羿", None)
            ],
            "櫻花": [
                ("曾淳筑", None), ("王綁湘", None), ("高可旻", None), ("楊禹桓", None), ("林允澄", None), 
                ("廖令杰", None), ("黃羿閎", None), ("林千甯", None), ("陳禹菲", None), ("白旭岑", None), 
                ("歐愷睿", None), ("劉丞耘", None)
            ],
            "芙蓉": [
                ("李悅盈", None), ("陳巧亭", None), ("戴仲坤", None), ("許心玥", None), ("邱宇禎", None), 
                ("陳柏鈞", "新生"), ("林琛然", None), ("鍾愷紘", None), ("侯有成", None), ("黃霓萱", None), 
                ("呂芯嫻", None), ("吳庭嘉", None), ("黃子豪", None), ("馬竑任", None), ("謝瑞恩", None),
                ("劉品炘", None), ("黃承軒", None), ("吳駿炘", None), ("蔡秉澄", "新生")
            ],
            "向日葵": [
                ("吳以歆", None), ("鄭玄杰", None), ("黃翊睿", "不足齡"), ("陳則序", None), ("顏以媃", None), 
                ("朱緯悌", None), ("王輝沂", None), ("張紘熙", "不足齡"), ("黃硯曦", "不足齡"), ("陳禹誠", "不足齡"), 
                ("鄭家紘", "不足齡"), ("蔣采希", "不足齡"), ("吳柏霖", "不足齡"), ("黃維彤", None), ("楊恒宇", None), 
                ("洪苡真", "新生")
            ],
            "滿天星": [
                ("王玥甯", None), ("陳予姍", None), ("謝孟芯", None), ("孫圓函", None), ("吳宣諒", None), 
                ("楊浩平", "新生"), ("鄭允豪", "新生"), ("謝愷宸", "新生")
            ],
            "牡丹": [
                ("程柏諺", "不足齡"), ("范苡禎", "不足齡"), ("曾宥澄", None), ("涂芝語", "新生"), ("謝宥呈", "新生"), 
                ("林柔吟", "新生"), ("黃皓冧", "不足齡"), ("吳泫哲", "新生"), ("黃子睿", "新生"), ("吳禹斯", "特教生"), 
                ("黃丞言", "特教生"), ("林珈羽", "新生"), ("施依岑", "特教生"), ("張溪羽", None), ("陳緒騰", "不足齡"), 
                ("陳田禾", None)
            ]
        }

        print("開始匯入學生...")
        student_count = 0
        tag_map = {
            "新生": "new",          # 綠色
            "不足齡": "underage",   # 橘色
            "特教生": "special",    # 紫色
            "原住民": "indigenous"  # 藍色 (雖然名單中沒看到，但先預留)
        }

        for class_name, students in roster.items():
            classroom = class_map[class_name]
            for s_name, s_tag in students:
                # 簡單產生學號：2026 + 班級ID(2碼) + 序號(2碼)
                # 實務上可能需要更嚴謹的規則
                existing_s = db.query(Student).filter_by(name=s_name, classroom_id=classroom.id).first()
                if not existing_s:
                    # 找該班目前最大學號
                    count = db.query(Student).filter_by(classroom_id=classroom.id).count() + 1
                    student_id = f"S{classroom.class_code.replace('-', '')}{count:02d}"
                    
                    student = Student(
                        student_id=student_id,
                        name=s_name,
                        classroom_id=classroom.id,
                        status_tag=s_tag, # 直接存中文標籤，前端好顯示
                        is_active=True,
                        enrollment_date=datetime.now().date()
                    )
                    db.add(student)
                    student_count += 1
        
        db.commit()
        print(f"全部完成！共匯入 {student_count} 名新學生。")

    except Exception as e:
        print(f"匯入失敗: {e}")
        db.rollback()
    finally:
        db.close()

if __name__ == "__main__":
    import_data()
