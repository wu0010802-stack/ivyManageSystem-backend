"""m12_appraisal:半年度考核資料。

上學期(FIRST)cycle = CLOSED:每位員工建 participant + score_items(綁
m00 落庫的 appraisal_score_item_catalog)+ 已評 summary(base_score / grade /
bonus_amount,status=SUPERVISOR_SIGNED)。下學期(SECOND)cycle = OPEN(進行中):
建 participant + 少量 score_items,**summary 未評滿**(僅一小部分有 DRAFT
summary,多數 participant 尚無 summary,模擬考核進行中)。

執行序保證(orchestrator m00 → m01 → ... → m12):
- m00 已落庫 ``appraisal_score_item_catalog``(15 項,code 對齊
  ``reference_data._APPRAISAL_CATALOG``)。
- m01 已寫入 ``ctx.employees`` / ``ctx.employees_by_role`` / ``ctx.classrooms``
  / ``ctx.users``(含 username='admin' 的 admin User)。

值域對齊(已逐欄 introspect models/appraisal.py):
- Semester(FIRST/SECOND)/ CycleStatus(OPEN/LOCKED/CLOSED)/ Grade
  (OUTSTANDING/GOOD/PASS/WARN/FAIL)/ SummaryStatus(DRAFT/SUPERVISOR_SIGNED/
  ACCOUNTING_SIGNED/FINALIZED)/ RoleGroup(SUPERVISOR/HEAD_TEACHER/ASSISTANT/
  STAFF/COOK)/ ScoreItemSign(POSITIVE/NEGATIVE/NEUTRAL)。
- 等第切點與獎金公式對齊 ``services/appraisal/engine.py``:
  優 ≥ 90 / 甲 80-89 / 乙 70-79 / 丙 60-69 / 丁 < 60;
  bonus = base_amount × (total_score / 100),PASS/WARN/FAIL 為 0。

金額一律走 ``utils.rounding.round_half_up``(禁 builtin round())。
本模組不 commit(由 orchestrator 跑完統一 commit)。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Optional

from ..context import SeedContext

if TYPE_CHECKING:  # pragma: no cover - 僅型別檢查
    pass


# ---------------------------------------------------------------------------
# role key(employees_by_role)→ RoleGroup。
# 決定論直接對映,不走 infer_role_group 的 title-string 啟發式(seed 求穩定)。
# ---------------------------------------------------------------------------
_ROLE_TO_ROLE_GROUP: dict[str, str] = {
    "supervisor": "SUPERVISOR",
    "admin": "STAFF",
    "accountant": "STAFF",
    "homeroom": "HEAD_TEACHER",
    "assistant": "ASSISTANT",
    "art": "HEAD_TEACHER",  # 才藝老師視同教師群(有班級績效適用性)
    "support": "COOK",
}

# 等第切點(對齊 services/appraisal/engine.py._GRADE_THRESHOLDS)。
# (門檻分數, Grade enum value);由高至低,第一個 ≤ 的即為等第。
_GRADE_THRESHOLDS: list[tuple[Decimal, str]] = [
    (Decimal("90"), "OUTSTANDING"),
    (Decimal("80"), "GOOD"),
    (Decimal("70"), "PASS"),
    (Decimal("60"), "WARN"),
]

# total_score 上下限(對齊 engine TOTAL_SCORE_MIN/MAX)。
_TOTAL_SCORE_MIN = Decimal("0")
_TOTAL_SCORE_MAX = Decimal("110")

# 不發獎金的等第。
_NO_BONUS_GRADES = frozenset({"PASS", "WARN", "FAIL"})

# 各 RoleGroup 的獎金基數(對齊 Excel:園長/主任群基數較高,廚工群較低)。
# 僅供 seed 推導 bonus_amount = base × total_score/100;非真引擎查表。
_ROLE_GROUP_BONUS_BASE: dict[str, Decimal] = {
    "SUPERVISOR": Decimal("8000"),
    "HEAD_TEACHER": Decimal("6000"),
    "ASSISTANT": Decimal("4000"),
    "STAFF": Decimal("5000"),
    "COOK": Decimal("3000"),
}

# 上學期已評 summary 的 base_score 候選(全 participant 共用該 cycle 的 base_score;
# 此處用單一決定論值模擬「9/15 全園註冊率」)。
_FIRST_CYCLE_BASE_SCORE = Decimal("78.5")

# 下學期(進行中)只讓前 N 位 participant 有 DRAFT summary(未評滿)。
_SECOND_CYCLE_GRADED_FRACTION = 0.25


def _grade_for_score(total_score: Decimal) -> str:
    """依 total_score 推導 Grade enum value(對齊 engine 切點)。"""
    for threshold, grade in _GRADE_THRESHOLDS:
        if total_score >= threshold:
            return grade
    return "FAIL"


def _participant_role_group(ctx: SeedContext, employee) -> str:
    """員工 → RoleGroup value。先用 employees_by_role 反查 role key。"""
    emp_id = getattr(employee, "id", None)
    for role_key, bucket in (ctx.employees_by_role or {}).items():
        if any(getattr(e, "id", None) == emp_id for e in bucket):
            return _ROLE_TO_ROLE_GROUP.get(role_key, "STAFF")
    return "STAFF"


def _catalog_rows(ctx: SeedContext) -> list:
    """查 m00 已落庫的 appraisal_score_item_catalog(依 display_order)。"""
    from models.appraisal import AppraisalScoreItemCatalog

    return (
        ctx.session.query(AppraisalScoreItemCatalog)
        .order_by(AppraisalScoreItemCatalog.display_order)
        .all()
    )


def _admin_user_id(ctx: SeedContext) -> Optional[int]:
    """取 username='admin' 的 User.id(供 created_by / signed_by)。"""
    admin = (ctx.users or {}).get("admin")
    return getattr(admin, "id", None) if admin is not None else None


def _score_delta_for_item(ctx: SeedContext, catalog_row, seq: int) -> Decimal:
    """為單一 catalog item 產生決定論 score_delta。

    依 sign 決定正負:NEGATIVE → 小幅扣分、POSITIVE → 小幅加分、
    NEUTRAL → 多為 0 偶有微調。值落 Numeric(5,2) 範圍。
    """
    sign = getattr(catalog_row, "sign", None)
    # sign 可能是 enum 物件或字串,統一取 value/str。
    sign_value = getattr(sign, "value", sign)
    rng = ctx.rng
    if sign_value == "NEGATIVE":
        # 多數為 0(未扣),少數扣 0.25~2 分。
        choice = rng.choice([0, 0, 0, -0.25, -0.5, -1, -2])
        return Decimal(str(choice))
    if sign_value == "POSITIVE":
        choice = rng.choice([0, 0, 2, 2, 4])
        return Decimal(str(choice))
    # NEUTRAL:多為 0,偶有 ±0.5/±1。
    choice = rng.choice([0, 0, 0, 0.5, -0.5, 1, -1])
    return Decimal(str(choice))


def _build_participant_score_items(
    ctx: SeedContext, cycle, participant, catalog_rows: list
) -> tuple[Decimal, int]:
    """為單一 participant 建 score_items(每個 catalog item 一筆)。

    回傳 (event_score_sum, 建立筆數)。
    """
    from models.appraisal import AppraisalScoreItem

    session = ctx.session
    event_sum = Decimal("0")
    n = 0
    for catalog_row in catalog_rows:
        seq = 1
        delta = _score_delta_for_item(ctx, catalog_row, seq)
        item = AppraisalScoreItem(
            participant_id=participant.id,
            cycle_id=cycle.id,
            catalog_id=getattr(catalog_row, "id", None),
            item_code=getattr(catalog_row, "code", "OTHER"),
            sequence_no=seq,
            score_delta=delta,
            raw_value=None,
            note=None,
            source_ref="seedgen",
            created_by=_admin_user_id(ctx),
        )
        session.add(item)
        event_sum += delta
        n += 1
    return event_sum, n


def _make_summary(
    ctx: SeedContext,
    cycle,
    participant,
    base_score: Decimal,
    event_sum: Decimal,
    role_group: str,
    status: str,
    signed: bool,
):
    """建立 AppraisalSummary(已評:含 grade / bonus_amount)。"""
    from models.appraisal import AppraisalSummary
    from utils.rounding import round_half_up

    # step3 total = clamp(base + event_sum, MIN, MAX)。
    total = base_score + event_sum
    if total < _TOTAL_SCORE_MIN:
        total = _TOTAL_SCORE_MIN
    elif total > _TOTAL_SCORE_MAX:
        total = _TOTAL_SCORE_MAX

    grade = _grade_for_score(total)

    # step5 bonus = base_amount × total/100;PASS/WARN/FAIL = 0。
    if grade in _NO_BONUS_GRADES:
        bonus = Decimal("0")
    else:
        base_amount = _ROLE_GROUP_BONUS_BASE.get(role_group, Decimal("0"))
        # round_half_up 回 int/float;轉回 Decimal 落 Numeric(10,2)。
        raw_bonus = base_amount * total / Decimal("100")
        bonus = Decimal(str(round_half_up(float(raw_bonus), 2)))

    signed_at = None
    signed_by = None
    if signed:
        signed_at = datetime(
            cycle.end_date.year, cycle.end_date.month, cycle.end_date.day
        )
        signed_by = _admin_user_id(ctx)

    summary = AppraisalSummary(
        participant_id=participant.id,
        cycle_id=cycle.id,
        base_score=base_score,
        event_score_sum=event_sum,
        total_score=total,
        grade=grade,
        bonus_amount=bonus,
        status=status,
        supervisor_signed_at=signed_at,
        supervisor_signed_by=signed_by,
        version=1,
    )
    ctx.session.add(summary)
    return summary


def _make_cycle(
    ctx: SeedContext,
    semester: str,
    start_date: date,
    end_date: date,
    base_calc_date: date,
    base_score: Decimal,
    status: str,
    enrollment_actual: int,
    enrollment_target: int,
):
    """建立 AppraisalCycle。"""
    from models.appraisal import AppraisalCycle

    cycle = AppraisalCycle(
        academic_year=ctx.config.academic_year,
        semester=semester,
        start_date=start_date,
        end_date=end_date,
        base_score_calc_date=base_calc_date,
        base_score=base_score,
        enrollment_target=enrollment_target,
        enrollment_actual=enrollment_actual,
        status=status,
        created_by=_admin_user_id(ctx),
    )
    ctx.session.add(cycle)
    ctx.session.flush()  # 取 cycle.id 供 participants/score_items FK。
    return cycle


def _make_participant(ctx: SeedContext, cycle, employee, role_group: str):
    """建立 AppraisalParticipant(綁 employee + classroom + role_group)。"""
    from models.appraisal import AppraisalParticipant

    participant = AppraisalParticipant(
        cycle_id=cycle.id,
        employee_id=employee.id,
        role_group=role_group,
        classroom_id=getattr(employee, "classroom_id", None),
        hire_months_in_cycle=Decimal("6"),
        is_excluded=False,
    )
    ctx.session.add(participant)
    return participant


def _appraisal_employees(ctx: SeedContext) -> list:
    """納入考核的員工:在職(is_active)的所有員工。"""
    out: list = []
    for emp in ctx.employees or []:
        if getattr(emp, "is_active", True):
            out.append(emp)
    return out


def seed(ctx: SeedContext) -> None:
    """建立上學期(CLOSED 已評)+ 下學期(OPEN 進行中)考核資料。"""
    session = ctx.session
    employees = _appraisal_employees(ctx)
    if not employees:
        return

    catalog_rows = _catalog_rows(ctx)
    if not catalog_rows:
        # m00 應已落庫 catalog;缺則跳過 score_items(仍建 cycle/participant)。
        catalog_rows = []

    roc_year = ctx.config.academic_year
    # 西元學年起年(114 → 2025)。上學期約 8~1 月,下學期約 2~7 月。
    greg_start = roc_year + 1911  # 2025
    greg_end = roc_year + 1912  # 2026

    enrollment_target = ctx.config.scale_profile["students"]
    # 上學期 base_score 已固定 78.5;反推 enrollment_actual 供稽核欄位(僅展示用)。
    enrollment_actual = int(
        Decimal(enrollment_target) * _FIRST_CYCLE_BASE_SCORE / Decimal("100")
    )

    cycle_counts = 0
    participant_counts = 0
    score_item_counts = 0
    summary_counts = 0

    # -------------------------------------------------------------------
    # 1) 上學期(FIRST)— CLOSED,全員已評,summary=SUPERVISOR_SIGNED。
    # -------------------------------------------------------------------
    first_cycle = _make_cycle(
        ctx,
        semester="FIRST",
        start_date=date(greg_start, 8, 1),
        end_date=date(greg_end, 1, 31),
        base_calc_date=date(greg_start, 9, 15),
        base_score=_FIRST_CYCLE_BASE_SCORE,
        status="CLOSED",
        enrollment_actual=enrollment_actual,
        enrollment_target=enrollment_target,
    )
    cycle_counts += 1

    for emp in employees:
        role_group = _participant_role_group(ctx, emp)
        participant = _make_participant(ctx, first_cycle, emp, role_group)
        session.flush()  # 取 participant.id 供 score_items / summary FK。
        participant_counts += 1

        event_sum, n_items = _build_participant_score_items(
            ctx, first_cycle, participant, catalog_rows
        )
        score_item_counts += n_items

        _make_summary(
            ctx,
            first_cycle,
            participant,
            base_score=_FIRST_CYCLE_BASE_SCORE,
            event_sum=event_sum,
            role_group=role_group,
            status="SUPERVISOR_SIGNED",
            signed=True,
        )
        summary_counts += 1

    # -------------------------------------------------------------------
    # 2) 下學期(SECOND)— OPEN(進行中),participant 已建但「未評滿」:
    #    僅前 N 位有 DRAFT summary,多數 participant 尚無 summary。
    # -------------------------------------------------------------------
    second_base_score = Decimal("0")  # 進行中尚未定 base(3/15 才結算)。
    second_cycle = _make_cycle(
        ctx,
        semester="SECOND",
        start_date=date(greg_end, 2, 1),
        end_date=date(greg_end, 7, 31),
        base_calc_date=date(greg_end, 3, 15),
        base_score=second_base_score,
        status="OPEN",
        enrollment_actual=None,
        enrollment_target=enrollment_target,
    )
    cycle_counts += 1

    n_graded = max(1, int(len(employees) * _SECOND_CYCLE_GRADED_FRACTION))
    for idx, emp in enumerate(employees):
        role_group = _participant_role_group(ctx, emp)
        participant = _make_participant(ctx, second_cycle, emp, role_group)
        session.flush()
        participant_counts += 1

        # 進行中:只有前 n_graded 位已開始評(建少量 score_items + DRAFT summary)。
        if idx < n_graded:
            event_sum, n_items = _build_participant_score_items(
                ctx, second_cycle, participant, catalog_rows
            )
            score_item_counts += n_items
            _make_summary(
                ctx,
                second_cycle,
                participant,
                base_score=second_base_score,
                event_sum=event_sum,
                role_group=role_group,
                status="DRAFT",
                signed=False,
            )
            summary_counts += 1
        # 其餘 participant:尚未評(無 score_items / 無 summary),模擬未評滿。

    ctx.log("appraisal_cycles", cycle_counts)
    ctx.log("appraisal_participants", participant_counts)
    ctx.log("appraisal_score_items", score_item_counts)
    ctx.log("appraisal_summaries", summary_counts)
