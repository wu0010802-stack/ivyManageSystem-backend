"""m05_fees:學生費用紀錄(unpaid/partial/paid 三態)、繳費流水、退費、減免調整。

依 m00 落庫的 `fee_templates`(年級×學年×學期×費目)為 ctx.students_active 產生
`student_fee_records`,並依該記錄所屬期間的封存狀態決定繳費狀態:

- closed 學期(114-1,month 2025-08~2026-01):多數 `paid`(附 StudentFeePayment 流水),
  少量 `partial`,極少 `unpaid`。
- 進行中學期(114-2,只生 ≤ ctx.config.today 的月份/單據):`unpaid` 為主,少量 `partial`。

並建立少量 `student_fee_refunds`(對已繳記錄退款)與 `student_fee_adjustments`
(同胞優惠/請假扣款等減免)。三種 status 都保證出現。

對齊 production 寫入站點(`api/fees/generation.py`):
- period 格式 `"{民國學年}-{學期}"`(如 114-1 / 114-2)
- monthly 費目逐月展開,target_month 格式 YYYY-MM;其餘費目 target_month=NULL
- fee_item_name = f"{範本名}{f' ({target_month})' if target_month else ''}"
- status 白名單 unpaid/partial/paid;amount_paid = Σpayments − Σrefunds
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import inspect as sa_inspect

from utils.rounding import round_half_up

from ..context import SeedContext

# fee_type → 是否為逐月展開的月費(對齊 generation.py 的 monthly 判定)。
_MONTHLY_FEE_TYPE = "monthly"

# 繳費方式選項(對齊 StudentFeePayment.payment_method 註解語意)。
_PAYMENT_METHODS = ["現金", "轉帳", "其他"]

# 減免類型(對齊 models/fees.py ADJUSTMENT_TYPE_* 常數值域)。
_ADJUSTMENT_TYPES = ["sibling_discount", "leave_deduction", "prepayment", "other"]


def _semester_months(western_year_start: int, semester: int) -> list[str]:
    """民國學年對應之西元起始年 + 學期 → 該學期月份 list[YYYY-MM]。

    對齊 `api/fees/generation.py:_semester_months`:
        上學期(1):本年 8~12 月 + 隔年 1 月
        下學期(2):隔年 2~7 月

    Args:
        western_year_start: 學年起始西元年(民國 114 → 2025)。
        semester: 1=上,2=下。
    """
    if semester == 1:
        return [f"{western_year_start}-{m:02d}" for m in range(8, 13)] + [
            f"{western_year_start + 1}-01"
        ]
    return [f"{western_year_start + 1}-{m:02d}" for m in range(2, 8)]


def _month_tuple(target_month: str) -> tuple[int, int]:
    """'YYYY-MM' → (year, month)。"""
    y, m = target_month.split("-")
    return int(y), int(m)


def seed(ctx: SeedContext) -> None:
    """建立學費紀錄 / 繳費流水 / 退費 / 減免。"""
    session = ctx.session
    rng = ctx.rng
    cfg = ctx.config

    # 延後 import 業務 model,避免單元測試 import 期間連鎖載入。
    from models.fees import (
        StudentFeeAdjustment,
        StudentFeePayment,
        StudentFeeRecord,
        StudentFeeRefund,
    )

    academic_year = cfg.academic_year  # 民國學年,如 114
    western_year_start = academic_year + 1911  # 114 → 2025

    closed = set(ctx.closed_months())  # set[(year, month)]
    cur_y, cur_m = ctx.current_month()  # 進行中月份

    # 學年內已封存或進行中的月份(含 today 當月);未開始者不生。
    def _month_visible(target_month: str) -> bool:
        ym = _month_tuple(target_month)
        return ym in closed or ym == (cur_y, cur_m)

    def _month_is_closed(target_month: str) -> bool:
        return _month_tuple(target_month) in closed

    # 載入 m00 落庫的 fee_templates(本學年、啟用中),以 (grade_id, semester) 索引。
    from models.fees import FeeTemplate

    template_rows = (
        session.query(FeeTemplate)
        .filter(
            FeeTemplate.school_year == academic_year,
            FeeTemplate.is_active.is_(True),
        )
        .all()
    )
    if not template_rows:
        ctx.log("student_fee_records", 0)
        return

    templates_by_grade_sem: dict[tuple[int, int], list[FeeTemplate]] = {}
    for tpl in template_rows:
        templates_by_grade_sem.setdefault((tpl.grade_id, tpl.semester), []).append(tpl)

    # 班級 id → grade_id 對照(從 ctx.classrooms registry,避免重查)。
    grade_by_classroom: dict[int, int | None] = {}
    for room in ctx.classrooms:
        grade_by_classroom[room.id] = getattr(room, "grade_id", None)

    today = cfg.today

    records: list[StudentFeeRecord] = []
    adjustments: list[StudentFeeAdjustment] = []
    # payment/refund 在 record flush 取得 id 後才建立(model 無 relationship,
    # 只能用 record_id);此處先暫存 (record, kwargs) 待 flush 後連結。
    pending_payments: list[tuple[StudentFeeRecord, dict]] = []
    pending_refunds: list[tuple[StudentFeeRecord, dict]] = []

    # ------------------------------------------------------------------
    # 1) 逐學生 × 學期 × 費目產生 records,並依封存狀態決定繳費狀態。
    # ------------------------------------------------------------------
    paid_records_for_refund: list[StudentFeeRecord] = []

    for student in ctx.students_active:
        classroom_id = getattr(student, "classroom_id", None)
        if classroom_id is None:
            continue
        grade_id = grade_by_classroom.get(classroom_id)
        if grade_id is None:
            continue
        classroom_name = None
        for room in ctx.classrooms:
            if room.id == classroom_id:
                classroom_name = room.name
                break

        for semester in (1, 2):
            sem_templates = templates_by_grade_sem.get((grade_id, semester), [])
            if not sem_templates:
                continue
            period_str = f"{academic_year}-{semester}"

            for tpl in sem_templates:
                if tpl.fee_type == _MONTHLY_FEE_TYPE:
                    months = _semester_months(western_year_start, semester)
                    target_months: list[str | None] = [
                        tm for tm in months if _month_visible(tm)
                    ]
                else:
                    # 非月費(註冊/雜費/材料/保險...)於學期初開單。
                    # 上學期一律已開(整學期 closed);下學期只在進行中後開單。
                    first_month = _semester_months(western_year_start, semester)[0]
                    target_months = [None] if _month_visible(first_month) else []

                for tm in target_months:
                    record_name = f"{tpl.name}{f' ({tm})' if tm else ''}"
                    offset = getattr(tpl, "due_date_offset_days", 14) or 14

                    # due_date:月費以該月 1 日 + offset;非月費以學期首月 1 日 + offset。
                    if tm is not None:
                        my, mm = _month_tuple(tm)
                        base_day = date(my, mm, 1)
                        is_closed_period = _month_is_closed(tm)
                    else:
                        fm = _semester_months(western_year_start, semester)[0]
                        my, mm = _month_tuple(fm)
                        base_day = date(my, mm, 1)
                        # 非月費:整個上學期視為 closed;下學期視當月狀態。
                        is_closed_period = semester == 1 or _month_is_closed(fm)
                    due_date_val = base_day + timedelta(days=offset)

                    amount_due = int(tpl.amount)

                    rec = StudentFeeRecord(
                        student_id=student.id,
                        student_name=student.name,
                        classroom_name=classroom_name,
                        fee_item_name=record_name,
                        amount_due=amount_due,
                        amount_paid=0,
                        status="unpaid",
                        period=period_str,
                        due_date=due_date_val,
                        fee_type=tpl.fee_type,
                        source_template_id=tpl.id,
                        target_month=tm,
                        notes="",
                    )

                    # 繳費狀態決策:
                    # - closed 期間:~88% paid、~9% partial、~3% unpaid
                    # - 進行中當月:~70% unpaid、~30% partial(極少 paid)
                    roll = rng.random()
                    if is_closed_period:
                        if roll < 0.88:
                            status = "paid"
                        elif roll < 0.97:
                            status = "partial"
                        else:
                            status = "unpaid"
                    else:
                        if roll < 0.70:
                            status = "unpaid"
                        elif roll < 0.97:
                            status = "partial"
                        else:
                            status = "paid"

                    pay_day = min(due_date_val, today)
                    method = rng.choice(_PAYMENT_METHODS)

                    if status == "paid":
                        rec.status = "paid"
                        rec.amount_paid = amount_due
                        rec.payment_date = pay_day
                        rec.payment_method = method
                        pending_payments.append(
                            (
                                rec,
                                {
                                    "amount": amount_due,
                                    "payment_date": pay_day,
                                    "payment_method": method,
                                    "operator": "seed_accountant",
                                    "notes": "",
                                },
                            )
                        )
                        paid_records_for_refund.append(rec)
                    elif status == "partial":
                        # 部分繳:繳 30%~70%(整數,round_half_up)。
                        ratio = rng.choice([0.3, 0.5, 0.6, 0.7])
                        partial_amt = int(round_half_up(amount_due * ratio))
                        partial_amt = max(1, min(partial_amt, amount_due - 1))
                        rec.status = "partial"
                        rec.amount_paid = partial_amt
                        rec.payment_date = pay_day
                        rec.payment_method = method
                        pending_payments.append(
                            (
                                rec,
                                {
                                    "amount": partial_amt,
                                    "payment_date": pay_day,
                                    "payment_method": method,
                                    "operator": "seed_accountant",
                                    "notes": "分期收款",
                                },
                            )
                        )
                    else:
                        rec.status = "unpaid"
                        rec.amount_paid = 0

                    records.append(rec)

    # ------------------------------------------------------------------
    # 2) 少量退費:對部分 paid 記錄退款(amount_paid 維持 snapshot,退費走獨立表)。
    #    退費筆數 ≈ paid 記錄的 5%(至少 1 筆,若有 paid 記錄)。
    # ------------------------------------------------------------------
    if paid_records_for_refund:
        n_refund = max(1, len(paid_records_for_refund) // 20)
        refund_targets = rng.sample(
            paid_records_for_refund,
            min(n_refund, len(paid_records_for_refund)),
        )
        for rec in refund_targets:
            # 退部分金額(20%~50%),原因為退課/溢繳。
            ratio = rng.choice([0.2, 0.3, 0.5])
            refund_amt = max(1, int(round_half_up(rec.amount_due * ratio)))
            pending_refunds.append(
                (
                    rec,
                    {
                        "amount": refund_amt,
                        "reason": rng.choice(["退課退費", "溢繳退還", "轉班調整"]),
                        "refunded_by": "seed_accountant",
                        "calc_method": "manual",
                        "notes": "",
                    },
                )
            )

    # ------------------------------------------------------------------
    # 3) 少量減免(adjustments):同胞優惠 / 請假扣款 等,獨立表,正金額相減。
    #    依在籍學生抽樣 ~8% 給一筆減免(下學期 period)。
    # ------------------------------------------------------------------
    adj_period = f"{academic_year}-{cur_sem(today, western_year_start)}"
    if ctx.students_active:
        n_adj = max(1, len(ctx.students_active) * 8 // 100)
        adj_students = rng.sample(
            ctx.students_active, min(n_adj, len(ctx.students_active))
        )
        for student in adj_students:
            adj_type = rng.choice(_ADJUSTMENT_TYPES)
            amount = rng.choice([500, 1000, 1500, 2000, 3000])
            reason_map = {
                "sibling_discount": "同胞就學優惠",
                "leave_deduction": "長假退費折抵",
                "prepayment": "預繳折抵",
                "other": "其他減免",
            }
            adjustments.append(
                StudentFeeAdjustment(
                    student_id=student.id,
                    period=adj_period,
                    adjustment_type=adj_type,
                    amount=amount,
                    reason=reason_map[adj_type],
                    created_by="seed_accountant",
                    notes="",
                )
            )

    # ------------------------------------------------------------------
    # 4) 落庫。先 flush records 取得 id,再以 record_id 建 payment/refund
    #    (model 無 relationship,只能用 FK 欄位)。
    # ------------------------------------------------------------------
    session.add_all(records)
    session.flush()  # 取得 record.id,供 payment/refund FK

    payments = [
        StudentFeePayment(record_id=rec.id, **kw) for rec, kw in pending_payments
    ]
    refunds = [StudentFeeRefund(record_id=rec.id, **kw) for rec, kw in pending_refunds]

    session.add_all(payments)
    session.add_all(refunds)
    session.add_all(adjustments)
    session.flush()

    ctx.log("student_fee_records", len(records))
    ctx.log("student_fee_payments", len(payments))
    ctx.log("student_fee_refunds", len(refunds))
    ctx.log("student_fee_adjustments", len(adjustments))


def cur_sem(today: date, western_year_start: int) -> int:
    """依 today 判定當前學期(8~1 月為上學期=1,2~7 月為下學期=2)。"""
    if today.month >= 8 or today.month == 1:
        return 1
    return 2


# 自驗用:確認所有 INSERT 的 model 欄位名都存在(避免 unexpected kwarg)。
def _introspect_columns() -> dict[str, set[str]]:  # pragma: no cover - 開發期自驗
    from models.fees import (
        StudentFeeAdjustment,
        StudentFeePayment,
        StudentFeeRecord,
        StudentFeeRefund,
    )

    out: dict[str, set[str]] = {}
    for model in (
        StudentFeeRecord,
        StudentFeePayment,
        StudentFeeRefund,
        StudentFeeAdjustment,
    ):
        cols = {c.key for c in sa_inspect(model).mapper.column_attrs}
        out[model.__tablename__] = cols
    return out
