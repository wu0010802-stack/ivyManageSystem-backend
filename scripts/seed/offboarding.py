"""scripts/seed/offboarding.py — 員工離職 + 獎懲 模組示範資料 seed。

灌兩張表：
- employee_offboarding_records（離職紀錄：離職日 / 原因 / 交接時間戳 / 假別結算快照 / 結算狀態）
- disciplinary_actions（獎懲紀錄：警告 / 小過 / 大過）

【冪等契約】每筆插入前先 exists 查；重跑必須新增 0 筆、不刪改現有資料。

【安全約束】
- 直接 INSERT employee_offboarding_records 不會 cascade 改 employee.is_active
  （flip 邏輯在 services/offboarding/orchestrator.py:110，僅 /process API 走，本腳本不呼叫）。
- disciplinary_actions 與 employee.is_active 無關。
- 離職紀錄優先掛在「已 is_active=False」的既有員工；額外示範另建 1 名
  is_active=False 的示範離職員工。**絕不改動任何既有在職員工的狀態**。

【說明】DisciplinaryAction model 僅支援懲處類型（warning/minor/major），無「嘉獎/表揚」
正向獎勵欄位（見 models/disciplinary.py ACTION_TYPES）；故本腳本只灌合法的輕度懲處示範，
其中以 deduction_amount=0 的「警告」表達口頭提醒（不實扣款）。
"""

from __future__ import annotations

from datetime import date, datetime

from scripts.seed._common import (  # noqa: F401
    session_scope,
    get_active_employees,
    get_admin_user,
    rand_date_between,
    TODAY,
)
from models.employee import Employee
from models.offboarding import EmployeeOffboardingRecord
from models.disciplinary import (
    DisciplinaryAction,
    ACTION_TYPE_WARNING,
    ACTION_TYPE_MINOR,
)

# 日期界線：離職 / 獎懲一律落在學年內、不生未來
LOWER = date(2025, 8, 1)
UPPER = date(2026, 6, 5)  # == TODAY

# 示範離職員工工號（E099 與既有名冊不衝突）
DEMO_RESIGNED_EMPLOYEE_NUMBER = "E099"


def _ensure_demo_resigned_employee(session) -> Employee:
    """取得（或冪等建立）1 名 is_active=False 的示範離職員工。

    僅在既有 2 名離職員工之外多補一筆離職示範用；建立的是新員工，
    **不會**動到任何既有在職員工狀態。
    """
    emp = (
        session.query(Employee)
        .filter(Employee.employee_id == DEMO_RESIGNED_EMPLOYEE_NUMBER)
        .first()
    )
    if emp is not None:
        return emp

    emp = Employee(
        employee_id=DEMO_RESIGNED_EMPLOYEE_NUMBER,
        name="示範離職教師",
        title="教師",
        employee_type="regular",
        is_active=False,  # 一建立即離職，不影響在職員工數
        hire_date=date(2023, 8, 1),
        resign_date=date(2026, 1, 31),
        resign_reason="生涯規劃（進修深造）",
        base_salary=34000,
        gender="女",
    )
    session.add(emp)
    session.flush()  # 取得 emp.id 供 offboarding FK 使用
    return emp


def _seed_offboarding(session, admin_user_id: int) -> int:
    """為已離職員工建立離職 checklist 紀錄（冪等，key = employee_id 為 PK / one-to-one）。

    來源：既有 is_active=False 員工（依 id 排序取前 2 名）+ 1 名示範離職員工。
    回傳新增筆數。
    """
    added = 0

    # 既有已離職員工（最多取 2 名；絕不碰在職員工）
    existing_resigned = (
        session.query(Employee)
        .filter(Employee.is_active == False)  # noqa: E712
        .filter(Employee.employee_id != DEMO_RESIGNED_EMPLOYEE_NUMBER)
        .order_by(Employee.id)
        .limit(2)
        .all()
    )

    # 額外示範離職員工
    demo_emp = _ensure_demo_resigned_employee(session)

    # 每筆離職紀錄的示範內容（依序套用到拿到的離職員工身上）
    # 結算狀態：closed_at 有值 = 已結算；無值 = 處理中
    plans = [
        {
            "resign_date": date(2026, 1, 15),
            "resign_reason": "生涯規劃，轉換跑道。離職面談已完成，交接順利。",
            "closed": True,  # 已完成結算
            "leave_payout_days": 3.5,
        },
        {
            "resign_date": date(2026, 5, 30),
            "resign_reason": "家庭因素（搬遷至外縣市），需照顧家人。",
            "closed": False,  # 結算處理中
            "leave_payout_days": 1.0,
        },
        {
            "resign_date": date(2026, 1, 31),
            "resign_reason": "生涯規劃（進修深造），返校攻讀研究所。",
            "closed": True,  # 已完成結算
            "leave_payout_days": 5.0,
        },
    ]

    targets = list(existing_resigned) + [demo_emp]

    for emp, plan in zip(targets, plans):
        exists = (
            session.query(EmployeeOffboardingRecord)
            .filter(EmployeeOffboardingRecord.employee_id == emp.id)
            .first()
        )
        if exists is not None:
            continue

        resign_date = plan["resign_date"]
        # 安全界線（防呆，理論上 plan 已在範圍內）
        if resign_date < LOWER:
            resign_date = LOWER
        if resign_date > UPPER:
            resign_date = UPPER

        opened_at = datetime(resign_date.year, resign_date.month, resign_date.day, 9, 0)
        # 交接 / 結算各步驟時間戳（皆在離職日當天或之後，且不超過今天）
        snapshot_at = datetime(
            resign_date.year, resign_date.month, resign_date.day, 10, 30
        )

        record = EmployeeOffboardingRecord(
            employee_id=emp.id,
            resign_date=resign_date,
            resign_reason=plan["resign_reason"],
            opened_at=opened_at,
            opened_by_user_id=admin_user_id,
            # 交接 / 結算流程步驟（已完成的填時間戳，表達已交接 / 已快照）
            user_revoked_at=opened_at,
            leave_snapshot_at=snapshot_at,
            leave_balance_snapshot={
                "special_leave_remaining_days": plan["leave_payout_days"],
                "note": "離職特休結算快照（示範資料）",
            },
        )
        if plan["closed"]:
            closed_at = datetime(
                resign_date.year, resign_date.month, resign_date.day, 17, 0
            )
            record.appraisal_marked_at = opened_at
            record.certificate_generated_at = closed_at
            record.closed_at = closed_at
            record.closed_by_user_id = admin_user_id

        session.add(record)
        added += 1

    return added


def _seed_disciplinary(session, admin_user_id: int) -> int:
    """建立獎懲（懲處）示範紀錄（冪等，key = employee_id + action_date + action_type + reason）。

    DisciplinaryAction 僅支援 warning/minor/major（無正向獎勵欄位）；
    本函式灌：1 筆口頭警告（deduction_amount=0，不實扣）+ 1 筆警告（小額）+ 1 筆小過。
    全部掛在「在職」員工上 —— 懲處紀錄與 is_active 無關，不會改動員工狀態。
    回傳新增筆數。
    """
    added = 0

    active_emps = get_active_employees(session)
    if not active_emps:
        return 0

    # 取前幾名在職員工承載示範懲處（依 id 穩定排序，重跑命中相同對象）
    plans = []
    if len(active_emps) >= 1:
        plans.append(
            {
                "employee": active_emps[0],
                "action_date": date(2025, 11, 12),
                "action_type": ACTION_TYPE_WARNING,
                "deduction_amount": 0,  # 口頭警告，不實扣款
                "reason": "上班時間多次使用手機處理私務，口頭警告提醒專注工作。",
            }
        )
    if len(active_emps) >= 2:
        plans.append(
            {
                "employee": active_emps[1],
                "action_date": date(2026, 3, 5),
                "action_type": ACTION_TYPE_WARNING,
                "deduction_amount": 500,
                "reason": "未依規定提前完成教學日誌繳交，書面警告。",
            }
        )
    if len(active_emps) >= 3:
        plans.append(
            {
                "employee": active_emps[2],
                "action_date": date(2026, 4, 18),
                "action_type": ACTION_TYPE_MINOR,
                "deduction_amount": 1500,
                "reason": "未經請假無故缺席園務會議一次，記小過並扣減當期獎金。",
            }
        )

    for plan in plans:
        emp = plan["employee"]
        action_date = plan["action_date"]
        # 安全界線（不生未來、不早於學年起點）
        if action_date < LOWER or action_date > UPPER:
            continue

        exists = (
            session.query(DisciplinaryAction)
            .filter(
                DisciplinaryAction.employee_id == emp.id,
                DisciplinaryAction.action_date == action_date,
                DisciplinaryAction.action_type == plan["action_type"],
                DisciplinaryAction.reason == plan["reason"],
            )
            .first()
        )
        if exists is not None:
            continue

        session.add(
            DisciplinaryAction(
                employee_id=emp.id,
                action_date=action_date,
                action_type=plan["action_type"],
                deduction_amount=plan["deduction_amount"],
                reason=plan["reason"],
                created_by="seed",
                updated_by="seed",
            )
        )
        added += 1

    return added


def step() -> None:
    """主入口：冪等灌離職 + 獎懲示範資料。"""
    with session_scope() as session:
        admin = get_admin_user(session)
        if admin is None:
            raise RuntimeError("找不到 admin user，無法填 opened_by / 作為操作人")
        admin_user_id = admin.id

        off_added = _seed_offboarding(session, admin_user_id)
        disc_added = _seed_disciplinary(session, admin_user_id)

    print(
        f"[seed.offboarding] employee_offboarding_records 新增 {off_added} 筆、"
        f"disciplinary_actions 新增 {disc_added} 筆"
    )


if __name__ == "__main__":
    step()
