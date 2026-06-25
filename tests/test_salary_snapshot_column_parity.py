"""防漂移 + 回歸：SalarySnapshot 必須涵蓋 SalaryRecord 所有金額欄。

_copy_record_to_snapshot（services/finance/salary_snapshot_service）以兩表欄位
「交集」反射複製。SalarySnapshot model 漏欄 → 交集漏 → 該金額在快照遺失，稽核
重印歷史薪條時憑空消失（supplementary_health_employee / appraisal_year_end_bonus
/ unused_leave_payout 三欄即如此漏掉）。本測試把「PR checklist 提醒」升級為強制
契約，未來 SalaryRecord 新增 Money 欄而 SalarySnapshot 漏補時立即 fail。
"""

from datetime import date

from sqlalchemy import Float, Integer, Numeric
from sqlalchemy import inspect as sa_inspect

from models.salary import SalaryRecord, SalarySnapshot
from models.types import Money

# 結構欄豁免清單（顯式列舉，新增豁免需附理由）：
# - id：兩表各自的 PK，不是值欄位
# - employee_id / bonus_config_id / attendance_policy_id：FK 參照
#   （employee_id 快照另有同名欄；FK 仍屬結構引用非金額/數值「值」，
#    一併豁免讓本測試聚焦值欄位；交集反射複製會照常帶過去）
# - salary_year / salary_month：期間鍵
# - version：SalaryRecord 重算版號，Snapshot 端以 source_version 改名保存
#   （models/salary.py:428「拍攝當下 SalaryRecord.version」），非漏欄
_STRUCTURAL_EXEMPT = {
    "id",
    "employee_id",
    "bonus_config_id",
    "attendance_policy_id",
    "salary_year",
    "salary_month",
    "version",
}

# 真缺欄暫時豁免區（補欄需要 migration，屬另一批；放這裡必帶 TODO 註記）。
# 目前為空：SalaryRecord 所有數值「值欄位」皆已存在於 SalarySnapshot。
_KNOWN_MISSING_TODO: set[str] = set()


def test_snapshot_covers_all_salaryrecord_money_columns():
    rec_money = {
        c.name for c in sa_inspect(SalaryRecord).columns if isinstance(c.type, Money)
    }
    snap_cols = {c.name for c in sa_inspect(SalarySnapshot).columns}
    missing = rec_money - snap_cols
    assert not missing, (
        f"SalarySnapshot 漏複製 SalaryRecord 金額欄: {sorted(missing)}；"
        "請在 SalarySnapshot 補上對應 Money 欄（_copy_record_to_snapshot 依兩表交集反射複製）。"
    )


def test_snapshot_covers_all_salaryrecord_numeric_value_columns():
    """擴大防漂移：所有 Numeric/Float/Integer「值欄位」皆須存在於 SalarySnapshot。

    Money 是 TypeDecorator（impl=Numeric），isinstance(Numeric) 不為真，
    故與裸 Numeric/Float/Integer 取聯集才是完整數值欄集合。結構欄
    （PK/FK/期間鍵/版號）以顯式 allowlist 豁免；真缺欄（需 migration）
    放 _KNOWN_MISSING_TODO 並附 TODO 註記，不可改 Snapshot model 蒙混。
    """
    rec_numeric = {
        c.name
        for c in sa_inspect(SalaryRecord).columns
        if isinstance(c.type, (Money, Numeric, Float, Integer))
    }
    snap_cols = {c.name for c in sa_inspect(SalarySnapshot).columns}
    missing = rec_numeric - snap_cols - _STRUCTURAL_EXEMPT - _KNOWN_MISSING_TODO
    assert not missing, (
        f"SalarySnapshot 漏複製 SalaryRecord 數值欄: {sorted(missing)}；"
        "請補 SalarySnapshot 對應欄位（需 migration），或暫放 _KNOWN_MISSING_TODO 並附 TODO。"
    )


def test_structural_exempt_columns_actually_exist_on_record():
    """豁免清單防腐：列在 allowlist 的欄位必須真的存在於 SalaryRecord，
    避免欄位改名後豁免變死條目、靜默放行同名新欄。"""
    rec_cols = {c.name for c in sa_inspect(SalaryRecord).columns}
    stale = (_STRUCTURAL_EXEMPT | _KNOWN_MISSING_TODO) - rec_cols
    assert not stale, f"豁免清單含 SalaryRecord 不存在的欄位（死條目）: {sorted(stale)}"


def test_copy_record_to_snapshot_copies_independent_payout_columns(test_db_session):
    """端到端：三個獨立轉帳/拆分欄位的值確實被快照保存（非僅 model 有欄）。"""
    from models.database import Employee
    from services.finance.salary_snapshot_service import _copy_record_to_snapshot

    s = test_db_session
    emp = Employee(
        employee_id="A001",
        name="員工A",
        base_salary=30000,
        employee_type="regular",
        is_active=True,
        hire_date=date(2025, 1, 1),
    )
    s.add(emp)
    s.commit()

    rec = SalaryRecord(
        employee_id=emp.id,
        salary_year=2026,
        salary_month=6,
        base_salary=30000,
        gross_salary=30000,
        net_salary=30000,
        total_deduction=0,
        supplementary_health_employee=123.45,
        appraisal_year_end_bonus=678.90,
        unused_leave_payout=111.11,
    )
    s.add(rec)
    s.flush()

    snap = _copy_record_to_snapshot(rec, "month_end", "tester")

    assert snap.supplementary_health_employee == 123.45
    assert snap.appraisal_year_end_bonus == 678.90
    assert snap.unused_leave_payout == 111.11


def test_detail_schema_declares_every_payload_column():
    """schema↔payload 防漂移（設計審查 2026-06-25 QW5）：detail 端點以
    ``_PAYLOAD_COLUMNS`` 反射組 payload dict，但回傳走 ``SalarySnapshotDetailOut``
    response_model。任何 _PAYLOAD_COLUMNS 欄位未在 schema 宣告 → Pydantic 靜默
    丟棄 → detail API 少回該金額（值仍 persist，屬序列化契約漂移）。原漏宣告
    extra_allowance / extra_allowance_label / appraisal_year_end_bonus /
    supplementary_health_employee / unused_leave_payout 5 欄即如此。
    """
    from services.finance.salary_snapshot_service import _PAYLOAD_COLUMNS
    from schemas.salary_snapshots import SalarySnapshotDetailOut

    schema_fields = set(SalarySnapshotDetailOut.model_fields)
    dropped = set(_PAYLOAD_COLUMNS) - schema_fields
    assert not dropped, (
        "SalarySnapshotDetailOut 漏宣告 _PAYLOAD_COLUMNS 欄位，detail API 會靜默"
        f"少回這些值: {sorted(dropped)}；請在 schemas/salary_snapshots.py 補對應欄位。"
    )
