"""scripts/seed/special_ed.py — 特殊教育模組 dev DB 示範資料（全 114 學年）。

灌三張表的示範資料，供手測「特殊教育」模組使用：
- student_iep_records（StudentIEPRecord）：個別化教育計畫 IEP
- student_disability_documents（StudentDisabilityDocument）：身心障礙證明文件
- special_education_subsidies（SpecialEducationSubsidy）：特教加給／助理鐘點費

設計重點
--------
- **冪等**：每筆插入前先 exists 查；重跑只會新增 0 筆，絕不刪改現有資料。
  IEP 以 (student_id, school_year, semester) 唯一鍵判存在；文件以
  (student_id, doc_type, file_path) 判存在；補助以 (employee_id, subsidy_type,
  period_start) 判存在。
- **日期界線**：所有日期落在 2025-08-01 ~ 2026-06-05（不生未來；TODAY=2026-06-05）。
- **學年語意**：本模組 school_year 用「教育部西元學年」，114 學年 → 西元 2025
  （見 models/gov_moe.py StudentIEPRecord.school_year 註解，與系統民國 school_year
  語意不同）。上學期 semester=1、下學期 semester=2。

只動上述三張表，不改其他任何檔。
"""

from __future__ import annotations

import logging
from datetime import date
from decimal import Decimal

from scripts.seed._common import (  # noqa: F401
    session_scope,
    get_active_students,
    get_admin_user,
    rand_date_between,
    TERM1,
    TERM2,
    TODAY,
)
from models.employee import Employee
from models.gov_moe import (
    StudentIEPRecord,
    StudentDisabilityDocument,
    SpecialEducationSubsidy,
)

logger = logging.getLogger(__name__)

# 114 學年 → 教育部西元學年 2025（gov_moe 模組專用語意）
MOE_SCHOOL_YEAR = 2025

# 取樣特教生數量（從 active 學生前段取，量小且穩定，重跑命中相同學生）
SAMPLE_SIZE = 6

# 障礙類別示範值（依特殊教育法常見類別；繁中）
DISABILITY_PROFILES = [
    {"type": "發展遲緩", "level": "輕度"},
    {"type": "自閉症", "level": "中度"},
    {"type": "語言障礙", "level": "輕度"},
    {"type": "智能障礙", "level": "中度"},
    {"type": "注意力缺陷過動症", "level": "輕度"},
    {"type": "聽覺障礙", "level": "輕度"},
]


def _iep_payload(profile: dict) -> dict:
    """依障礙類別產出一份 IEP 內容（繁中目標／評量／服務內容）。"""
    dtype = profile["type"]
    return {
        "current_status": (
            f"幼生現階段評估：{dtype}（{profile['level']}）。"
            "認知、語言、社會互動與精細動作各領域均較同齡幼兒發展遲緩，"
            "需透過個別化教學與相關專業服務介入支持。"
        ),
        "long_term_goals": (
            "本學期長期目標：提升幼生於團體活動中的參與度與口語表達能力，"
            "增進與同儕互動之社會技巧，並強化生活自理與情緒調節能力。"
        ),
        "short_term_goals": [
            {
                "domain": "語言溝通",
                "goal": "能以完整語句表達基本需求",
                "criterion": "10 次中 8 次達成",
            },
            {
                "domain": "社會互動",
                "goal": "能參與小組活動並輪流",
                "criterion": "連續 2 週每日達成",
            },
            {
                "domain": "生活自理",
                "goal": "能自行完成如廁與盥洗",
                "criterion": "獨立完成達 80%",
            },
        ],
        "mid_term_evaluation": (
            "期中評量：語言溝通目標達成度約 60%，已能主動以短句表達需求；"
            "社會互動需持續引導，輪流概念漸建立。"
        ),
        "final_evaluation": (
            "期末評量：三項短期目標達成度分別為 80%／70%／85%，"
            "整體進步明顯，建議下學期延續並調整社會互動目標難度。"
        ),
        "iep_team_members": [
            {"role": "班級導師", "name": "班導"},
            {"role": "特教巡迴輔導教師", "name": "巡輔老師"},
            {"role": "家長", "name": "家長代表"},
            {"role": "語言治療師", "name": "治療師"},
        ],
        # meeting_dates：依 API schema 為 dict（IEP 會議日期，落在學年內）
        "meeting_dates": {
            "initial": str(rand_date_between(TERM1[0], date(2025, 9, 30))),
            "mid_term": str(rand_date_between(date(2025, 11, 1), TERM1[1])),
            "final": str(rand_date_between(TERM2[0], TODAY)),
        },
    }


def step() -> None:
    """灌特教三張表示範資料（冪等）。重跑新增 0 筆。"""
    iep_added = 0
    doc_added = 0
    subsidy_added = 0

    with session_scope() as session:
        students = get_active_students(session, limit=SAMPLE_SIZE)
        if len(students) < SAMPLE_SIZE:
            logger.warning(
                "active 學生不足 %d 名（實際 %d），特教取樣將縮減",
                SAMPLE_SIZE,
                len(students),
            )

        admin = get_admin_user(session)
        creator_emp_id = getattr(admin, "employee_id", None)

        # 補助綁在「員工」（特教加給／助理鐘點費領取人）；取前兩名在職員工示範。
        subsidy_emps = (
            session.query(Employee)
            .filter(Employee.is_active == True)  # noqa: E712
            .order_by(Employee.id)
            .limit(2)
            .all()
        )

        special_student_ids: list[int] = []

        # ===== 1) IEP record + 身障證明文件（每名特教生各一筆）=====
        for idx, stu in enumerate(students):
            profile = DISABILITY_PROFILES[idx % len(DISABILITY_PROFILES)]
            special_student_ids.append(stu.id)

            # --- IEP record（唯一鍵 student_id + school_year + semester）---
            iep_exists = (
                session.query(StudentIEPRecord)
                .filter(
                    StudentIEPRecord.student_id == stu.id,
                    StudentIEPRecord.school_year == MOE_SCHOOL_YEAR,
                    StudentIEPRecord.semester == 2,
                )
                .first()
            )
            if not iep_exists:
                content = _iep_payload(profile)
                session.add(
                    StudentIEPRecord(
                        student_id=stu.id,
                        school_year=MOE_SCHOOL_YEAR,
                        semester=2,  # 下學期（資料截至 2026-06-05，仍在下學期內）
                        status="approved",
                        current_status=content["current_status"],
                        long_term_goals=content["long_term_goals"],
                        short_term_goals=content["short_term_goals"],
                        mid_term_evaluation=content["mid_term_evaluation"],
                        final_evaluation=content["final_evaluation"],
                        iep_team_members=content["iep_team_members"],
                        meeting_dates=content["meeting_dates"],
                        created_by_employee_id=creator_emp_id,
                        approved_by_employee_id=creator_emp_id,
                    )
                )
                iep_added += 1

            # --- 身障證明文件（鑑定證明；以 student_id + doc_type + file_path 判存在）---
            cert_no = f"特鑑字第114{stu.id:04d}號"
            file_path = f"/uploads/disability/114/student_{stu.id}_cert.pdf"
            issued = rand_date_between(TERM1[0], date(2025, 9, 30))
            # 效期：開立後約 3 年（鑑定證明常見效期），但本欄僅供顯示，不限未來。
            expiry = date(issued.year + 3, issued.month, issued.day)
            doc_exists = (
                session.query(StudentDisabilityDocument)
                .filter(
                    StudentDisabilityDocument.student_id == stu.id,
                    StudentDisabilityDocument.doc_type == "鑑定證明",
                    StudentDisabilityDocument.file_path == file_path,
                )
                .first()
            )
            if not doc_exists:
                session.add(
                    StudentDisabilityDocument(
                        student_id=stu.id,
                        doc_type="鑑定證明",
                        file_path=file_path,
                        issued_date=issued,
                        expiry_date=expiry,
                        notes=(
                            f"障礙類別：{profile['type']}（{profile['level']}）；"
                            f"鑑定證明文號：{cert_no}。"
                        ),
                    )
                )
                doc_added += 1

        # ===== 2) 特教補助（綁員工；teacher_extra 加給 + assistant_hourly 鐘點）=====
        if subsidy_emps and special_student_ids:
            # (a) 特教教師加給 teacher_extra：上學期期間定額。
            emp0 = subsidy_emps[0]
            p_start_a = TERM1[0]
            p_end_a = min(TERM1[1], TODAY)
            sub_a_exists = (
                session.query(SpecialEducationSubsidy)
                .filter(
                    SpecialEducationSubsidy.employee_id == emp0.id,
                    SpecialEducationSubsidy.subsidy_type == "teacher_extra",
                    SpecialEducationSubsidy.period_start == p_start_a,
                )
                .first()
            )
            if not sub_a_exists:
                session.add(
                    SpecialEducationSubsidy(
                        subsidy_type="teacher_extra",
                        employee_id=emp0.id,
                        related_student_ids=special_student_ids[:3],
                        period_start=p_start_a,
                        period_end=p_end_a,
                        hours_or_rate=None,
                        amount_requested=Decimal("3000"),
                        amount_approved=Decimal("3000"),
                        status="approved",
                        notes="特教教師加給（上學期）；服務 3 名身障幼生。",
                    )
                )
                subsidy_added += 1

            # (b) 特教助理員鐘點 assistant_hourly：下學期期間，含時數。
            emp1 = subsidy_emps[1] if len(subsidy_emps) > 1 else subsidy_emps[0]
            p_start_b = TERM2[0]
            p_end_b = min(TERM2[1], TODAY)
            hours = Decimal("40.5")
            sub_b_exists = (
                session.query(SpecialEducationSubsidy)
                .filter(
                    SpecialEducationSubsidy.employee_id == emp1.id,
                    SpecialEducationSubsidy.subsidy_type == "assistant_hourly",
                    SpecialEducationSubsidy.period_start == p_start_b,
                )
                .first()
            )
            if not sub_b_exists:
                # 鐘點費 200 元/時 × 40.5 時 = 8100
                amount = (Decimal("200") * hours).quantize(Decimal("1"))
                session.add(
                    SpecialEducationSubsidy(
                        subsidy_type="assistant_hourly",
                        employee_id=emp1.id,
                        related_student_ids=special_student_ids[3:],
                        period_start=p_start_b,
                        period_end=p_end_b,
                        hours_or_rate=hours,
                        amount_requested=amount,
                        amount_approved=amount,
                        status="approved",
                        notes="特教助理員鐘點費（下學期）；每小時 200 元。",
                    )
                )
                subsidy_added += 1

    logger.info(
        "特教 seed 完成：IEP +%d、身障文件 +%d、特教補助 +%d",
        iep_added,
        doc_added,
        subsidy_added,
    )
    print(
        f"[special_ed] IEP +{iep_added} / disability_docs +{doc_added} "
        f"/ subsidies +{subsidy_added}"
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    step()
