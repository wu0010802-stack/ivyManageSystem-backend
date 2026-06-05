"""scripts/seed/employee_profile.py — 員工人事檔案模組示範資料 seed。

範圍（三張表，全在 models/employee.py）：
- employee_contracts   勞動合約（每位員工 1 筆）
- employee_educations  學歷（每位員工 1-2 筆，最高學歷標記 is_highest）
- employee_certificates 證照（每位員工依職務 1-2 筆：教師證/教保員證/保母證/CPR 等）

設計要點：
- 冪等：每筆插入前先 exists 查（以「自然鍵」過濾）；重跑必新增 0 筆、不刪改現有資料。
- 決定性：所有內容由 `employee.id` 推導（local random.Random(seed)），重跑產生相同內容，
  使 exists 查可靠命中。
- 涵蓋全部員工（含 inactive）：人事檔案不隨在職狀態消失，故掃全表。
- 日期界線：到職日可早於 2025；證照效期可跨未來；但所有「簽署/取得日期」不晚於 TODAY(2026-06-05)。

合法值（取自 model docstring，DB 無 CHECK 約束）：
- contract_type：正式 / 兼職 / 試用 / 臨時 / 續約
- degree：高中職 / 學士 / 碩士 / 博士 / 其他

執行：
    python3 -m scripts.seed.employee_profile
"""

from __future__ import annotations

import logging
import random
from datetime import date, timedelta

from scripts.seed._common import session_scope, TODAY  # noqa: F401
from models.employee import (
    Employee,
    EmployeeCertificate,
    EmployeeContract,
    EmployeeEducation,
)

logger = logging.getLogger(__name__)

# ===== 內容素材（繁中、合理）=====

# 幼教相關大學/科系（學士）
_UNIVERSITIES = [
    ("國立臺北教育大學", "幼兒與家庭教育學系"),
    ("國立臺中教育大學", "幼兒教育學系"),
    ("國立臺南大學", "幼兒教育學系"),
    ("臺北市立大學", "幼兒教育學系"),
    ("國立屏東大學", "幼兒教育學系"),
    ("輔仁大學", "兒童與家庭學系"),
    ("實踐大學", "家庭研究與兒童發展學系"),
    ("朝陽科技大學", "幼兒保育系"),
    ("樹德科技大學", "兒童與家庭服務系"),
    ("經國管理暨健康學院", "幼兒保育系"),
]

# 美術/才藝師相關（美師）學士科系
_ART_UNIVERSITIES = [
    ("國立臺灣藝術大學", "美術學系"),
    ("國立臺北藝術大學", "美術學系"),
    ("實踐大學", "媒體傳達設計學系"),
    ("中國文化大學", "美術學系"),
]

# 高中職（次高學歷或職員學歷）
_HIGH_SCHOOLS = [
    ("市立南港高級中學", None),
    ("私立復興高級商工職業學校", "美術科"),
    ("市立松山高級工農職業學校", "幼兒保育科"),
    ("私立育達高級中等學校", "幼兒保育科"),
    ("市立木柵高級工業職業學校", None),
]


def _is_art_teacher(emp: Employee) -> bool:
    return bool(emp.title) and "美" in emp.title


def _is_teacher(emp: Employee) -> bool:
    """班導師 / 副班導 / 美師 等帶班教學職務。"""
    t = emp.title or ""
    return any(k in t for k in ("班導", "副班", "師")) or _is_art_teacher(emp)


def _contract_for(emp: Employee, rng: random.Random) -> dict:
    """產生一筆合約資料。起始日 = 到職日（缺則 2024-08-01）。"""
    start = emp.hire_date or date(2024, 8, 1)
    # 正職多為不定期（end_date 空）；少數示範續約／試用以呈現多型別。
    contract_type = "正式"
    end = None
    remark = "不定期勞動契約"
    if not emp.is_active:
        # 離職員工：給定期合約，end_date 落在到職後一段時間（不晚於今天）。
        contract_type = "正式"
        end_candidate = start + timedelta(days=365 * 2)
        end = min(end_candidate, TODAY)
        remark = "定期勞動契約（已屆期）"
    salary = float(emp.base_salary) if emp.base_salary else 30000.0
    return {
        "contract_type": contract_type,
        "start_date": start,
        "end_date": end,
        "salary_at_contract": salary,
        "remark": remark,
    }


def _educations_for(emp: Employee, rng: random.Random) -> list[dict]:
    """1-2 筆學歷，最高學歷標記 is_highest=True。"""
    rows: list[dict] = []
    if _is_art_teacher(emp):
        school, major = rng.choice(_ART_UNIVERSITIES)
        degree = "學士"
    elif _is_teacher(emp):
        school, major = rng.choice(_UNIVERSITIES)
        degree = "學士"
    else:
        # 職員：學士或高中職皆可
        if rng.random() < 0.5:
            school, major = rng.choice(_UNIVERSITIES)
            degree = "學士"
        else:
            school, major = rng.choice(_HIGH_SCHOOLS)
            degree = "高中職"

    hire = emp.hire_date or date(2024, 8, 1)
    if degree == "學士":
        # 假設畢業於到職前 1-8 年（大學畢業約 22 歲後）
        grad_year = hire.year - rng.randint(1, 8)
    else:
        grad_year = hire.year - rng.randint(3, 12)
    grad = date(grad_year, 6, rng.choice([15, 20, 28]))
    if grad > TODAY:
        grad = TODAY

    rows.append(
        {
            "school_name": school,
            "major": major,
            "degree": degree,
            "graduation_date": grad,
            "is_highest": True,
            "remark": None,
        }
    )

    # 約 35% 員工再補一筆較低學歷（高中職），呈現多筆學歷
    if degree == "學士" and rng.random() < 0.35:
        hs_school, hs_major = rng.choice(_HIGH_SCHOOLS)
        hs_grad = date(grad_year - 4, 6, rng.choice([15, 20, 28]))
        if hs_grad > TODAY:
            hs_grad = TODAY
        rows.append(
            {
                "school_name": hs_school,
                "major": hs_major,
                "degree": "高中職",
                "graduation_date": hs_grad,
                "is_highest": False,
                "remark": None,
            }
        )
    return rows


def _certificates_for(emp: Employee, rng: random.Random) -> list[dict]:
    """依職務 1-2 筆證照。取得日不晚於 TODAY；到期日可空（永久）或跨未來。"""
    rows: list[dict] = []
    hire = emp.hire_date or date(2024, 8, 1)

    def _issued_after_hire(
        min_offset_days: int = 30, max_offset_days: int = 600
    ) -> date:
        d = hire + timedelta(days=rng.randint(min_offset_days, max_offset_days))
        if d > TODAY:
            # 退回到到職日與今天之間
            span = (TODAY - hire).days
            d = hire + timedelta(days=rng.randint(0, max(span, 0)))
        return min(d, TODAY)

    if _is_art_teacher(emp):
        # 美師：幼兒園教師證 或 教保員證 之一 + CPR
        if rng.random() < 0.5:
            issued = _issued_after_hire()
            rows.append(
                {
                    "certificate_name": "幼兒園教師證書",
                    "issuer": "教育部",
                    "certificate_number": f"幼教師字第{1000 + emp.id:05d}號",
                    "issued_date": issued,
                    "expiry_date": None,  # 教師證永久有效
                    "remark": None,
                }
            )
        else:
            issued = _issued_after_hire()
            rows.append(
                {
                    "certificate_name": "教保員資格證明",
                    "issuer": "教育部",
                    "certificate_number": f"教保字第{2000 + emp.id:05d}號",
                    "issued_date": issued,
                    "expiry_date": None,
                    "remark": None,
                }
            )
    elif _is_teacher(emp):
        # 班導/副班：幼兒園教師證 或 教保員證
        if rng.random() < 0.6:
            issued = _issued_after_hire()
            rows.append(
                {
                    "certificate_name": "幼兒園教師證書",
                    "issuer": "教育部",
                    "certificate_number": f"幼教師字第{1000 + emp.id:05d}號",
                    "issued_date": issued,
                    "expiry_date": None,
                    "remark": None,
                }
            )
        else:
            issued = _issued_after_hire()
            rows.append(
                {
                    "certificate_name": "教保員資格證明",
                    "issuer": "教育部",
                    "certificate_number": f"教保字第{2000 + emp.id:05d}號",
                    "issued_date": issued,
                    "expiry_date": None,
                    "remark": None,
                }
            )
        # 約半數副班導再補一張保母證
        if "副班" in (emp.title or "") and rng.random() < 0.5:
            issued = _issued_after_hire()
            rows.append(
                {
                    "certificate_name": "保母人員技術士證（丙級）",
                    "issuer": "勞動部勞動力發展署技能檢定中心",
                    "certificate_number": f"126-{3000 + emp.id:06d}",
                    "issued_date": issued,
                    "expiry_date": None,
                    "remark": None,
                }
            )
    else:
        # 職員：CPR 急救證（如保健/行政）
        issued = _issued_after_hire()
        rows.append(
            {
                "certificate_name": "CPR+AED 心肺復甦術急救證",
                "issuer": "中華民國紅十字會",
                "certificate_number": f"CPR-{emp.id:06d}",
                "issued_date": issued,
                "expiry_date": (
                    date(issued.year + 2, issued.month, issued.day)
                    if issued.day <= 28
                    else date(issued.year + 2, issued.month, 28)
                ),
                "remark": "效期 2 年，需定期回訓",
            }
        )

    # 全體再給一張 CPR（教師也需）以呈現多筆，約 60%
    already_has_cpr = any("CPR" in r["certificate_name"] for r in rows)
    if not already_has_cpr and rng.random() < 0.6:
        issued = _issued_after_hire()
        exp = (
            date(issued.year + 2, issued.month, issued.day)
            if issued.day <= 28
            else date(issued.year + 2, issued.month, 28)
        )
        rows.append(
            {
                "certificate_name": "CPR+AED 心肺復甦術急救證",
                "issuer": "中華民國紅十字會",
                "certificate_number": f"CPR-{emp.id:06d}",
                "issued_date": issued,
                "expiry_date": exp,
                "remark": "效期 2 年，需定期回訓",
            }
        )
    return rows


def step() -> None:
    """灌入員工人事檔案示範資料（冪等）。"""
    logger.info("=== 員工人事檔案 seed（合約 / 學歷 / 證照） ===")
    added_contract = 0
    added_education = 0
    added_certificate = 0

    with session_scope() as session:
        employees = session.query(Employee).order_by(Employee.id).all()
        logger.info("掃描員工 %d 位（含 inactive）", len(employees))

        for emp in employees:
            # 決定性 RNG：同一員工每次重跑產生相同內容
            rng = random.Random(emp.id * 7919 + 13)

            # --- 合約（自然鍵：employee_id + contract_type + start_date）---
            c = _contract_for(emp, rng)
            exists_c = (
                session.query(EmployeeContract)
                .filter_by(
                    employee_id=emp.id,
                    contract_type=c["contract_type"],
                    start_date=c["start_date"],
                )
                .first()
            )
            if not exists_c:
                session.add(
                    EmployeeContract(
                        employee_id=emp.id,
                        contract_type=c["contract_type"],
                        start_date=c["start_date"],
                        end_date=c["end_date"],
                        salary_at_contract=c["salary_at_contract"],
                        remark=c["remark"],
                    )
                )
                added_contract += 1

            # --- 學歷（自然鍵：employee_id + school_name + degree）---
            for e in _educations_for(emp, rng):
                exists_e = (
                    session.query(EmployeeEducation)
                    .filter_by(
                        employee_id=emp.id,
                        school_name=e["school_name"],
                        degree=e["degree"],
                    )
                    .first()
                )
                if exists_e:
                    continue
                session.add(
                    EmployeeEducation(
                        employee_id=emp.id,
                        school_name=e["school_name"],
                        major=e["major"],
                        degree=e["degree"],
                        graduation_date=e["graduation_date"],
                        is_highest=e["is_highest"],
                        remark=e["remark"],
                    )
                )
                added_education += 1

            # --- 證照（自然鍵：employee_id + certificate_name）---
            for cert in _certificates_for(emp, rng):
                exists_cert = (
                    session.query(EmployeeCertificate)
                    .filter_by(
                        employee_id=emp.id,
                        certificate_name=cert["certificate_name"],
                    )
                    .first()
                )
                if exists_cert:
                    continue
                session.add(
                    EmployeeCertificate(
                        employee_id=emp.id,
                        certificate_name=cert["certificate_name"],
                        issuer=cert["issuer"],
                        certificate_number=cert["certificate_number"],
                        issued_date=cert["issued_date"],
                        expiry_date=cert["expiry_date"],
                        remark=cert["remark"],
                    )
                )
                added_certificate += 1

    logger.info(
        "完成：合約 +%d、學歷 +%d、證照 +%d",
        added_contract,
        added_education,
        added_certificate,
    )
    print(
        f"[employee_profile] 新增 合約={added_contract} "
        f"學歷={added_education} 證照={added_certificate}"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    step()
