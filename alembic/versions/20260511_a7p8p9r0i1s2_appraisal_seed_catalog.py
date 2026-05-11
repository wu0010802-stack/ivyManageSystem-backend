"""appraisal: seed 29 條懲處事由目錄

來源：第八篇 員工懲處事由 115.01.01 + 第六篇考核辦法第五條第（十）（十一）款。

29 條 = 5 大類懲處（管教不當 8 / 餵藥 3 / 幼兒意外 3 / 員工爭執 3 / 人員疏失 5）
     + 功類 3（嘉獎/小功/大功）
     + 特別辦法 4（主管推薦/特教生/種子講師/才藝班全期）

Revision ID: a7p8p9r0i1s2
Revises: a1p2p3r4i5s6
Create Date: 2026-05-11
"""

import sqlalchemy as sa
from alembic import op

revision = "a7p8p9r0i1s2"
down_revision = "a1p2p3r4i5s6"
branch_labels = None
depends_on = None

CATALOG = [
    # (code, category, subcategory, description,
    #  default_event_type, default_score_delta, severity_max, display_order)
    # === MISCONDUCT 管教不當 ===
    (
        "MISCONDUCT_LEAVE_CLASSROOM",
        "MISCONDUCT",
        "離開教室",
        "離開教室時未委請其他老師代為看顧，將幼生獨留在教室",
        "WARNING",
        -2.0,
        1,
        10,
    ),
    (
        "MISCONDUCT_PARENT_COMPLAINT_WITHDRAWAL",
        "MISCONDUCT",
        "親師溝通無果致退學",
        "家長抱怨老師教保不力，親師溝通無果，導致家長決定讓幼生休退學",
        "WARNING",
        -2.0,
        1,
        11,
    ),
    (
        "MISCONDUCT_INTIMIDATION_VOLUME",
        "MISCONDUCT",
        "恐嚇-音量過大",
        "對孩子說話的音量大至樓上(下)都知悉",
        "WARNING",
        -2.0,
        1,
        12,
    ),
    (
        "MISCONDUCT_INTIMIDATION_VERBAL",
        "MISCONDUCT",
        "恐嚇-言語",
        "言語恐嚇孩子（如：你再說話把你趕出去等等）",
        "MINOR_DEMERIT",
        -3.0,
        1,
        13,
    ),
    (
        "MISCONDUCT_INTIMIDATION_ISOLATION",
        "MISCONDUCT",
        "隔離至教室外",
        "為處罰而隔離孩子到教室以外的空間",
        "MINOR_DEMERIT",
        -3.0,
        1,
        14,
    ),
    (
        "MISCONDUCT_PHYSICAL_HARM",
        "MISCONDUCT",
        "身心痛苦/侵害",
        "讓幼兒身心遭遇痛苦或侵害（最重大過乙次）",
        "WARNING",
        -2.0,
        3,
        15,
    ),
    (
        "MISCONDUCT_CORPORAL_PUNISHMENT",
        "MISCONDUCT",
        "體罰",
        "依家長知情/反應遞增；幼生無受傷且家長不知道→扣考核分數；公開申訴→記大過",
        "SCORE_ADJUST",
        -3.0,
        5,
        16,
    ),
    (
        "MISCONDUCT_VIOLENCE",
        "MISCONDUCT",
        "暴力管教",
        "以暴力管教小孩（依勞基法 12.1.4 終止僱傭）",
        "MAJOR_DEMERIT",
        -6.0,
        1,
        17,
    ),
    # === MEDICATION 餵藥 ===
    (
        "MEDICATION_SELF_SERVE",
        "MEDICATION",
        "讓幼生自行服藥",
        "未依規定協助餵藥，讓幼生自行服藥",
        "ORAL_WARNING",
        0.0,
        1,
        20,
    ),
    (
        "MEDICATION_WRONG_NO_SYMPTOM",
        "MEDICATION",
        "餵錯藥-無症狀",
        "未依指示餵藥或餵錯藥，幼生無症狀；視家長反應遞增",
        "ORAL_WARNING",
        0.0,
        3,
        21,
    ),
    (
        "MEDICATION_WRONG_WITH_SYMPTOM",
        "MEDICATION",
        "餵錯藥-有症狀",
        "未依指示餵藥或餵錯藥，幼生有症狀；視告知方式與家長反應遞增",
        "SCORE_ADJUST",
        -3.0,
        5,
        22,
    ),
    # === ACCIDENT 幼兒意外 ===
    (
        "ACCIDENT_MINOR_NO_SUTURE",
        "ACCIDENT",
        "輕傷無縫合",
        "幼生意外送醫無縫合",
        "SCORE_ADJUST",
        -1.0,
        5,
        30,
    ),
    (
        "ACCIDENT_MINOR_WITH_SUTURE",
        "ACCIDENT",
        "輕傷有縫合",
        "幼生意外送醫有縫合（依情節 1-10 分）",
        "SCORE_ADJUST",
        -5.0,
        5,
        31,
    ),
    (
        "ACCIDENT_SEVERE",
        "ACCIDENT",
        "重傷需就醫",
        "幼生受重傷（需就醫）",
        "WARNING",
        -2.0,
        5,
        32,
    ),
    # === DISPUTE 員工爭執 ===
    (
        "DISPUTE_VERBAL_RESOLVED",
        "DISPUTE",
        "口角和解",
        "口頭吵架未影響校譽，雙方和解",
        "ORAL_WARNING",
        0.0,
        1,
        40,
    ),
    (
        "DISPUTE_VERBAL_DAMAGE",
        "DISPUTE",
        "口角影響校譽",
        "口頭吵架有影響校譽",
        "SCORE_ADJUST",
        -2.0,
        1,
        41,
    ),
    (
        "DISPUTE_PHYSICAL",
        "DISPUTE",
        "肢體衝突",
        "肢體衝突，依是否影響校譽/退學遞增",
        "SCORE_ADJUST",
        -2.0,
        4,
        42,
    ),
    # === NEGLIGENCE 人員疏失 ===
    (
        "NEGLIGENCE_ACCOUNTING",
        "NEGLIGENCE",
        "行政會計疏失",
        "薪資核算錯誤、加退保延誤、教育局報聘辭聘錯誤",
        "WARNING",
        -2.0,
        1,
        50,
    ),
    (
        "NEGLIGENCE_KITCHEN",
        "NEGLIGENCE",
        "廚房疏失",
        "餐點量不足、食材剩餘、地板濕滑致受傷",
        "WARNING",
        -2.0,
        3,
        51,
    ),
    (
        "NEGLIGENCE_DRIVER",
        "NEGLIGENCE",
        "司機疏失",
        "違反交通條例、行車事故之虞、車內遺留幼兒等",
        "MINOR_DEMERIT",
        -3.0,
        4,
        52,
    ),
    (
        "NEGLIGENCE_DRESS_CODE",
        "NEGLIGENCE",
        "未依規定穿著",
        "員工未依規定穿著服裝",
        "ORAL_WARNING",
        0.0,
        1,
        53,
    ),
    (
        "NEGLIGENCE_DOC_LATE",
        "NEGLIGENCE",
        "文件未按時繳交",
        "員工文件未按時繳交",
        "ORAL_WARNING",
        0.0,
        1,
        54,
    ),
    # === MERIT 功類 ===
    ("MERIT_COMMENDATION", "MERIT", "嘉獎", "嘉獎", "COMMENDATION", 2.0, 1, 60),
    ("MERIT_MINOR", "MERIT", "小功", "小功", "MINOR_MERIT", 3.0, 1, 61),
    ("MERIT_MAJOR", "MERIT", "大功", "大功", "MAJOR_MERIT", 6.0, 1, 62),
    # === SPECIAL 特別辦法 ===
    (
        "SPECIAL_RECOMMENDATION",
        "SPECIAL",
        "主管推薦優異",
        "單位主管呈報表現優異人員（有具體行為），經執行長及總園長核定",
        "SCORE_ADJUST",
        2.0,
        1,
        70,
    ),
    (
        "SPECIAL_SPECIAL_NEEDS",
        "SPECIAL",
        "班級特教生",
        "班級有政府核定有補助園所的特教生（幼生在園需超過 4 個月）",
        "SCORE_ADJUST",
        2.0,
        1,
        71,
    ),
    (
        "SPECIAL_SEED_INSTRUCTOR",
        "SPECIAL",
        "內部種子講師",
        "經機構檢定為內部種子講師",
        "SCORE_ADJUST",
        2.0,
        1,
        72,
    ),
    (
        "SPECIAL_ART_CLASS_FULL_TERM",
        "SPECIAL",
        "才藝班全期授課",
        "參與課後才藝課全期授課者（未帶班人員除外）",
        "SCORE_ADJUST",
        2.0,
        1,
        73,
    ),
]


def upgrade() -> None:
    bind = op.get_bind()
    for row in CATALOG:
        bind.execute(
            sa.text(
                "INSERT INTO appraisal_penalty_catalog "
                "(code, category, subcategory, description, "
                " default_event_type, default_score_delta, severity_max, "
                " display_order, is_active) "
                "VALUES (:code, :cat, :sub, :desc, :evt, :score, :sev, :ord, true) "
                "ON CONFLICT (code) DO NOTHING"
            ),
            dict(
                zip(
                    ["code", "cat", "sub", "desc", "evt", "score", "sev", "ord"],
                    row,
                )
            ),
        )


def downgrade() -> None:
    bind = op.get_bind()
    codes = tuple(row[0] for row in CATALOG)
    bind.execute(
        sa.text("DELETE FROM appraisal_penalty_catalog WHERE code = ANY(:codes)"),
        {"codes": list(codes)},
    )
