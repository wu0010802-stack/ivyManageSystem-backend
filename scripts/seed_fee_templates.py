"""scripts/seed_fee_templates.py — 種入 4 年級 × 2 學期 × 3 費用類型 = 24 筆 FeeTemplate。

學期：民國 114-2（114 學年下學期，西元 2026 春）、115-1（115 學年上學期，西元 2026 秋）。
費用類型：registration / miscellaneous / monthly。
金額：
- 大/中/小班：registration=19000 / miscellaneous=3000 / monthly=13000
- 幼幼班：registration=17000 / miscellaneous=3000 / monthly=10800
月費 breakdown：
- 大/中/小班 13000：{tuition: 8500, meal: 3000, transport: 1500}
- 幼幼 10800：{tuition: 6300, meal: 3000, transport: 1500}
- registration / miscellaneous breakdown 為 NULL（不參與比例退費）。

冪等：(grade_id, school_year, semester, fee_type) 已存在則 skip。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import session_scope  # noqa: E402
from models.classroom import ClassGrade  # noqa: E402
from models.fees import FeeTemplate  # noqa: E402

# (school_year, semester) 兩學期
TERMS = [(114, 2), (115, 1)]

# 年級金額配置
# 大/中/小班共用一組金額；幼幼班單獨配置
STANDARD_AMOUNTS = {
    "registration": 19000,
    "miscellaneous": 3000,
    "monthly": 13000,
}
STANDARD_MONTHLY_BREAKDOWN = {"tuition": 8500, "meal": 3000, "transport": 1500}

NURSERY_AMOUNTS = {
    "registration": 17000,
    "miscellaneous": 3000,
    "monthly": 10800,
}
NURSERY_MONTHLY_BREAKDOWN = {"tuition": 6300, "meal": 3000, "transport": 1500}

# 對應到 ClassGrade.name；找不到視為 skip
STANDARD_GRADES = ("大班", "中班", "小班")
NURSERY_GRADE = "幼幼班"

# 名稱對應（人類可讀）
FEE_NAMES = {
    "registration": "註冊費",
    "miscellaneous": "雜費",
    "monthly": "月費",
}


def _resolve_amounts(grade_name: str):
    if grade_name == NURSERY_GRADE:
        return NURSERY_AMOUNTS, NURSERY_MONTHLY_BREAKDOWN
    if grade_name in STANDARD_GRADES:
        return STANDARD_AMOUNTS, STANDARD_MONTHLY_BREAKDOWN
    return None, None


def seed() -> dict:
    summary = {"created": 0, "skipped": 0, "missing_grade": []}

    with session_scope() as session:
        grades = {g.name: g for g in session.query(ClassGrade).all()}

        target_grades = [*STANDARD_GRADES, NURSERY_GRADE]
        for grade_name in target_grades:
            grade = grades.get(grade_name)
            if grade is None:
                summary["missing_grade"].append(grade_name)
                print(f"[warn] 找不到年級 '{grade_name}'，已跳過。")
                continue

            amounts, monthly_breakdown = _resolve_amounts(grade_name)
            if amounts is None:
                summary["missing_grade"].append(grade_name)
                continue

            for school_year, semester in TERMS:
                for fee_type, amount in amounts.items():
                    breakdown = monthly_breakdown if fee_type == "monthly" else None
                    existing = (
                        session.query(FeeTemplate)
                        .filter(
                            FeeTemplate.grade_id == grade.id,
                            FeeTemplate.school_year == school_year,
                            FeeTemplate.semester == semester,
                            FeeTemplate.fee_type == fee_type,
                        )
                        .first()
                    )
                    if existing is not None:
                        summary["skipped"] += 1
                        print(
                            f"[skip] {grade_name} {school_year}-{semester} {fee_type}"
                        )
                        continue

                    tpl = FeeTemplate(
                        grade_id=grade.id,
                        school_year=school_year,
                        semester=semester,
                        fee_type=fee_type,
                        name=f"{grade_name}{FEE_NAMES[fee_type]}",
                        amount=amount,
                        breakdown=breakdown,
                        due_date_offset_days=14,
                        is_active=True,
                        created_by="seed_script",
                        updated_by="seed_script",
                    )
                    session.add(tpl)
                    summary["created"] += 1
                    print(
                        f"[create] {grade_name} {school_year}-{semester} {fee_type} amount={amount}"
                    )

    return summary


def main() -> None:
    print("[info] 開始種入 FeeTemplate...")
    summary = seed()
    print("[done] 摘要：")
    print(f"  - created: {summary['created']}")
    print(f"  - skipped: {summary['skipped']}")
    if summary["missing_grade"]:
        print(f"  - missing_grades: {summary['missing_grade']}")


if __name__ == "__main__":
    main()
