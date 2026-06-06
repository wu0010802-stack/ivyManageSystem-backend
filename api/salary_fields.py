"""Shared salary display helpers for API responses."""


def calculate_display_bonus_total(record) -> float:
    """Display-only bonus total used by portal/history/reporting surfaces.

    `bonus_amount` is excluded because it already aggregates separate fields in
    the persisted record and would double-count festival/overtime bonuses.

    `supervisor_dividend` 亦刻意**不**納入本合計：前端歷史頁將主管紅利列為
    獨立欄位顯示；若加入此合計會與頁面的「主管紅利」欄位造成雙計視覺。
    如需包含主管紅利的完整現金流，應使用 `SalaryRecord.bonus_amount`
    （= festival + overtime + supervisor_dividend）。
    """
    return (
        (getattr(record, "festival_bonus", 0) or 0)
        + (getattr(record, "overtime_bonus", 0) or 0)
        + (getattr(record, "performance_bonus", 0) or 0)
        + (getattr(record, "special_bonus", 0) or 0)
    )


def _coalesce_float(record, name) -> float:
    """讀欄位並 coalesce None→0、轉 float（避免 Decimal/float 混算 TypeError）。"""
    return float(getattr(record, name, 0) or 0)


# 進帳收入欄位（計入 gross_salary）；label 對齊官方薪條用語。
_HISTORY_INCOME_FIELDS = [
    ("base_salary", "底薪"),
    ("performance_bonus", "績效獎金"),
    ("special_bonus", "特別獎金"),
    ("supervisor_dividend", "主管紅利"),  # ⚠ 進實發；欄位註解「獨立轉帳」具誤導性
    ("overtime_pay", "加班費"),
    ("meeting_overtime_pay", "園務會議加班"),
    ("birthday_bonus", "生日禮金"),
    ("hourly_total", "時薪總計"),
    ("extra_allowance", "額外加給"),
]
# 另行轉帳欄位（不進 gross/net，獨立金流）。
_HISTORY_SEPARATE_FIELDS = [
    ("festival_bonus", "節慶獎金"),
    ("overtime_bonus", "超額獎金"),
    ("appraisal_year_end_bonus", "考核年終獎金"),
    ("unused_leave_payout", "特休未休折現"),
]
# 扣款欄位（合計 = total_deduction）。
_HISTORY_DEDUCTION_FIELDS = [
    ("labor_insurance_employee", "勞保"),
    ("health_insurance_employee", "健保"),
    ("pension_employee", "勞退自提"),
    ("late_deduction", "遲到扣款"),
    ("early_leave_deduction", "早退扣款"),
    ("missing_punch_deduction", "未打卡扣款"),
    ("leave_deduction", "請假扣款"),
    ("absence_deduction", "曠職扣款"),
    ("other_deduction", "其他扣款"),
]


def build_history_breakdown(record) -> dict:
    """從 SalaryRecord persisted 欄位組出歷史薪條三區明細（純展示，不重算）。

    正確性守衛：
    - income_subtotal/deduction_subtotal/net 一律取 persisted 值當權威。
    - income 區補「其他（未分類）」吸收 gross 與已知收入欄位差額（seed/邊角），
      使收入各項 + other == gross。
    - supplementary_health_employee 已併入 health_insurance_employee，僅作健保下
      informational 子列，不進扣款合計（避免 double-count）。
    - meeting_absence_deduction 已在 engine 內從 festival 扣抵，不另列。
    """
    gross = _coalesce_float(record, "gross_salary")
    total_deduction = _coalesce_float(record, "total_deduction")
    net = _coalesce_float(record, "net_salary")

    income = []
    known_income_sum = 0.0
    for key, label in _HISTORY_INCOME_FIELDS:
        amount = _coalesce_float(record, key)
        line = {"key": key, "label": label, "amount": amount}
        if key == "extra_allowance":
            note = getattr(record, "extra_allowance_label", None)
            if note:
                line["note"] = note
        income.append(line)
        known_income_sum += amount
    other_income = round(gross - known_income_sum, 2)
    if other_income != 0:
        income.append(
            {"key": "other_income", "label": "其他（未分類）", "amount": other_income}
        )

    separate_transfer = [
        {"key": key, "label": label, "amount": _coalesce_float(record, key)}
        for key, label in _HISTORY_SEPARATE_FIELDS
    ]
    separate_subtotal = round(sum(item["amount"] for item in separate_transfer), 2)

    deductions = []
    for key, label in _HISTORY_DEDUCTION_FIELDS:
        line = {"key": key, "label": label, "amount": _coalesce_float(record, key)}
        if key == "health_insurance_employee":
            supp = _coalesce_float(record, "supplementary_health_employee")
            if supp:
                line["children"] = [
                    {
                        "key": "supplementary_health_employee",
                        "label": "其中：二代健保補充保費",
                        "amount": supp,
                        "informational": True,
                    }
                ]
        deductions.append(line)

    return {
        "income": income,
        "income_subtotal": gross,
        "separate_transfer": separate_transfer,
        "separate_subtotal": separate_subtotal,
        "deductions": deductions,
        "deduction_subtotal": total_deduction,
        "net_salary": net,
    }
