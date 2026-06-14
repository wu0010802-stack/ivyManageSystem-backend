"""m06_salary:跑真實薪資引擎(process_bulk_salary_calculation)逐 closed 月。

職責
----
對每個 ``ctx.closed_months()``,用 production 同路徑的薪資引擎
(``SalaryEngine.process_bulk_salary_calculation``,即 ``api.salary.calculate`` 的
``RuntimeSalaryEngine``)結算「月薪在職員工」薪資,寫入 ``salary_records``。
當月(in_progress)**不算**——留待手測按「計算薪資」。

設計要點
--------
1. **production 同路徑**:bulk 引擎內部自行 ``_get_db_session()`` 開新 session、
   單次 commit、結束 close;故它讀的是「已 commit」的前序資料。orchestrator 在
   每模組跑完才 commit,因此進引擎前先把 ``ctx.session`` flush+commit 一次,確保
   m00~m05 的考勤/設定/員工資料對引擎的獨立 session 可見。
2. **preflight fail-loud**:開跑前 assert 引擎前置(保險級距 / 當年 position 設定 /
   獎金設定 / 該月考勤)齊備,缺則 raise 明確訊息,把「離線編排脆弱性」轉成清楚訊號。
3. **月薪在職判定**:沿用 ``_active_employees_in_month_filter``(hire ≤ 月底 且
   resign IS NULL or ≥ 月初),並排除 ``employee_type == 'hourly'`` 的才藝時薪老師
   (時薪薪資由業主在 UI 填明細,bulk 結算對其無意義且易 422)。
4. **fallback 階梯**:若某月 bulk 整批 raise,記錄並改走引擎純函式
   ``calculate_salary()`` 逐人寫單筆 ``SalaryRecord``(仍內部一致,見 spec §7.1)。
"""

from __future__ import annotations

import logging
from datetime import date

from ..context import SeedContext

logger = logging.getLogger(__name__)

# 引擎 fallback 用的「月基準工作日」(與 services/salary/constants.MONTHLY_BASE_DAYS 對齊;
# 此處只在 bulk raise 的退化路徑用到,不影響主路徑)。
_FALLBACK_WORKING_DAYS = 22


def _monthly_active_employee_filter(year: int, month: int):
    """月薪在職員工 filter:沿用 production 的在職判定 + 排除 hourly 才藝老師。

    production 入口(api.salary.calculate)用 ``_active_employees_in_month_filter``
    判定「該月任一天在職」;此處再 ``AND employee_type != 'hourly'`` 排除時薪老師
    (bulk 結算對時薪無意義)。回傳 SQLAlchemy filter,供 ctx.session 查詢用。
    """
    from sqlalchemy import and_

    from api.salary import _active_employees_in_month_filter
    from models.database import Employee

    return and_(
        _active_employees_in_month_filter(year, month),
        Employee.employee_type != "hourly",
    )


def _preflight(ctx: SeedContext, months: list[tuple[int, int]]) -> None:
    """薪資引擎前置完整性檢查;缺任一前置即 raise(fail-loud,印出缺哪張表)。

    檢查項(對齊 spec §7.1 前置 + 計畫 Task 3.4):
      1. ``insurance_brackets`` > 0(健保/勞保級距)。
      2. ``position_salary_configs`` 含「當年 config_year」(period-aware resolver 需要;
         以 closed 月份的西元年判定,確保歷史重算撿得到該年度設定)。
      3. ``bonus_configs`` > 0(節慶/超額/紅利設定)。
      4. 每個 closed 月皆有員工考勤(否則引擎讀不到出勤,結果失真)。
    """
    from models.database import (
        Attendance,
        BonusConfig,
        InsuranceBracket,
        PositionSalaryConfig,
    )

    session = ctx.session

    brackets = session.query(InsuranceBracket).count()
    if brackets <= 0:
        raise RuntimeError(
            "m06_salary preflight 失敗:insurance_brackets 為空——"
            "請先跑 m00_config 灌入保險級距(reference_data.insurance_brackets())。"
        )

    bonus = session.query(BonusConfig).count()
    if bonus <= 0:
        raise RuntimeError(
            "m06_salary preflight 失敗:bonus_configs 為空——"
            "請先跑 m00_config 灌入獎金設定。"
        )

    # period-aware resolver:每個 closed 月份對應的西元年都要有 position_salary_configs。
    needed_years = sorted({y for (y, _m) in months})
    for cfg_year in needed_years:
        has_year = (
            session.query(PositionSalaryConfig.id)
            .filter(PositionSalaryConfig.config_year == cfg_year)
            .first()
        )
        if has_year is None:
            raise RuntimeError(
                f"m06_salary preflight 失敗:position_salary_configs 缺 config_year={cfg_year}"
                "——period-aware resolver 會 fail-loud;請確認 m00_config 已建 2025/2026 兩套。"
            )

    # 每個 closed 月皆需有員工考勤(引擎逐日讀出勤計扣款/超額)。
    for year, month in months:
        import calendar as _cal

        _, last_day = _cal.monthrange(year, month)
        start = date(year, month, 1)
        end = date(year, month, last_day)
        att = (
            session.query(Attendance.id)
            .filter(
                Attendance.attendance_date >= start,
                Attendance.attendance_date <= end,
            )
            .first()
        )
        if att is None:
            raise RuntimeError(
                f"m06_salary preflight 失敗:{year}-{month:02d} 無任何員工考勤——"
                "請先跑 m03_attendance 產生該月考勤。"
            )


def _run_bulk_for_month(ctx: SeedContext, year: int, month: int) -> int:
    """以 production bulk 引擎結算單一 closed 月;回傳成功寫入筆數。

    bulk 引擎自管 session/commit/close,讀「已 commit」資料。若整批 raise,
    交由 caller 走 fallback。注意:engine 內部對個別員工失敗會收進 errors 而不
    整批 raise;此處把 errors 記 log 但不視為致命。
    """
    from api.salary.calculate import RuntimeSalaryEngine
    from models.database import Employee

    employee_ids = [
        e.id
        for e in (
            ctx.session.query(Employee)
            .filter(_monthly_active_employee_filter(year, month))
            .all()
        )
    ]
    if not employee_ids:
        logger.warning("m06_salary:%s-%02d 無月薪在職員工,略過。", year, month)
        return 0

    engine = RuntimeSalaryEngine(load_from_db=True)
    results, errors = engine.process_bulk_salary_calculation(employee_ids, year, month)
    if errors:
        for err in errors:
            logger.warning(
                "m06_salary:%s-%02d 員工 %s 計算失敗:%s",
                year,
                month,
                err.get("employee_name", "?"),
                err.get("error", "?"),
            )
    return len(results)


def _fallback_single_for_month(ctx: SeedContext, year: int, month: int) -> int:
    """bulk 整批失敗時的退化路徑:用引擎純函式逐人寫單筆 SalaryRecord。

    走 ``SalaryEngine.calculate_salary()``(純函式,不查 DB)取得 breakdown,
    再用引擎 canonical 的 ``_fill_salary_record``(同 production 的 breakdown→record
    欄位對映,正確處理 ``health_insurance`` → ``health_insurance_employee`` 等命名差異)
    落入新建的 ``SalaryRecord``。仍內部一致,但不重現節慶/超額的「期間累積」覆寫
    (發放月語意),作為最後手段(見 spec §7.1)。寫入 ``ctx.session``(由 orchestrator
    commit)。回傳寫入筆數。
    """
    from models.database import Employee, SalaryRecord
    from services.salary.engine import SalaryEngine, _fill_salary_record

    working_days = _FALLBACK_WORKING_DAYS

    engine = SalaryEngine(load_from_db=True)
    session = ctx.session

    employees = (
        session.query(Employee)
        .filter(_monthly_active_employee_filter(year, month))
        .all()
    )

    written = 0
    for emp in employees:
        # 已存在該月 record 則跳過(避免與 bulk 部分成功重覆 / 違反唯一鍵)。
        existing = (
            session.query(SalaryRecord.id)
            .filter(
                SalaryRecord.employee_id == emp.id,
                SalaryRecord.salary_year == year,
                SalaryRecord.salary_month == month,
            )
            .first()
        )
        if existing is not None:
            continue

        # emp_dict 的 key 必須對齊 calculate_salary 內部 employee.get(...) 讀取的名稱
        # (見 engine.py 扣款段:investor 投保薪資來源 insurance_salary=投保級距、
        #  labor/health/pension_insured_salary=分項投保;非 *_insurance_salary)。
        emp_dict = {
            "name": emp.name,
            "employee_id": emp.employee_id,
            "employee_type": emp.employee_type,
            "base_salary": float(emp.base_salary or 0),
            "hourly_rate": float(emp.hourly_rate or 0),
            "insurance_salary": emp.insurance_salary_level,  # 投保級距(可 None)
            "pension_self_rate": float(emp.pension_self_rate or 0.0),
            "dependents": int(emp.dependents or 0),
            "extra_dependents_quarterly": int(emp.extra_dependents_quarterly or 0),
            "health_exempt": bool(emp.health_exempt),
            "no_employment_insurance": bool(emp.no_employment_insurance),
            "labor_insured_salary": emp.labor_insured_salary,
            "health_insured_salary": emp.health_insured_salary,
            "pension_insured_salary": emp.pension_insured_salary,
        }
        breakdown = engine.calculate_salary(
            emp_dict, year, month, working_days=working_days
        )

        record = SalaryRecord(
            employee_id=emp.id,
            salary_year=year,
            salary_month=month,
        )
        # canonical 對映器:同 production 路徑把 breakdown 各欄落入 record(含命名差異
        # 與 gross/total/net 重算),確保內部一致。session 傳入供其 payout plugin 用。
        _fill_salary_record(record, breakdown, engine, session=session)
        session.add(record)
        written += 1

    return written


def seed(ctx: SeedContext) -> None:
    """跑薪資引擎產生 closed 月薪資(production 同路徑 + fallback)。"""
    months = ctx.closed_months()
    if not months:
        logger.info("m06_salary:無 closed 月份,略過。")
        return

    # 進引擎前 commit ctx.session:bulk 引擎用自己的獨立 session 讀「已 commit」資料,
    # 確保 m00~m05 的設定/考勤/員工對它可見(orchestrator 雖每模組 commit,此處再保險)。
    if ctx.session is not None:
        ctx.session.commit()

    _preflight(ctx, months)

    total_written = 0
    for year, month in months:
        try:
            n = _run_bulk_for_month(ctx, year, month)
            logger.info(
                "m06_salary:%s-%02d bulk 寫入 %d 筆 salary_records。", year, month, n
            )
        except Exception as exc:  # noqa: BLE001 - 整批失敗才走 fallback
            logger.error(
                "m06_salary:%s-%02d bulk 整批失敗,改走純函式 fallback:%s",
                year,
                month,
                exc,
                exc_info=True,
            )
            n = _fallback_single_for_month(ctx, year, month)
            # fallback 寫進 ctx.session,需自行 commit 讓下個月引擎的獨立 session 看得到。
            if ctx.session is not None:
                ctx.session.commit()
            logger.info(
                "m06_salary:%s-%02d fallback 寫入 %d 筆 salary_records。",
                year,
                month,
                n,
            )
        total_written += n

    ctx.log("salary_records", total_written)
