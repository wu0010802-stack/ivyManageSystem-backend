"""scripts/seed/health.py — 健康/生長量測/用藥/過敏 模組 114 學年示範資料 seed。

灌四張表：
- student_measurements      身高/體重量測（每學期一筆，取樣約 60 名學生）
- student_medication_orders 當日用藥醫囑（約 8 名學生，每人 1-2 筆）
- student_medication_logs   餵藥執行紀錄（每張 order 配 1-3 筆）
- student_allergies         長期過敏資訊（約 8 名學生）

冪等契約
--------
每筆插入前先 exists 查；重跑必須新增 0 筆、不刪改現有資料。
- measurements / orders / logs：走 ORM exists 查（純文字欄位，無加密）。
- allergies：見下方「加密處理」說明，exists 查與 INSERT 皆走 raw SQL。

加密處理（student_allergies）
----------------------------
StudentAllergy.allergen / reaction_symptom / first_aid_note 為 `EncryptedText`
（utils/medical_field_type）TypeDecorator，ORM bind 時會呼叫 encrypt_medical()，
而 encrypt 需要 env `MEDICAL_FIELD_ENCRYPTION_KEY`（dev DB 未設）。因此：

  1. 用 ORM 物件「寫入」這三欄會在 bind 階段 raise RuntimeError（無金鑰）。
  2. 用 ORM「filter_by(allergen=...)」也會 raise（WHERE bind 同樣走 encrypt）。

但讀取路徑 decrypt_medical() 對「非 Fernet token（含中文等非 ASCII）」會原樣回傳
（migration-window legacy plaintext passthrough，design by RA-MED-10），**不需金鑰**。

故本 seed 對 allergies 採「legacy plaintext」格式：以 raw SQL 直接寫入中文明文，
ORM 讀回（不 filter 加密欄、只讀 column）即得正確明文。此為系統設計容許的
migration-window 格式，dev 環境無金鑰時仍可正確讀回（已實測 round-trip 通過）。
severity 本就維持明文（DB 排序需求），照常寫入。

執行
----
    cd ~/Desktop/ivy-backend
    python3 -m scripts.seed.health

共用 helper 來自 scripts.seed._common。
"""

from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta

from sqlalchemy import text

from scripts.seed._common import (
    session_scope,
    get_active_students,
    get_admin_user,
    TERM1,
    TERM2,
    TODAY,
)
from scripts.seed._common import get_active_employees
from models.portfolio import (
    StudentAllergy,
    StudentMeasurement,
    StudentMedicationOrder,
    StudentMedicationLog,
    MEDICATION_SOURCE_TEACHER,
)

logger = logging.getLogger(__name__)

# 固定亂數種子，讓抽樣/數值在多次重跑間一致（搭配 exists 查確保 0 新增）
_RNG = random.Random(11402)

# ── 量測：取樣學生數、合理數值範圍 ────────────────────────────────────────
_MEASUREMENT_SAMPLE = 60  # 取前 60 名 active 學生
_HEIGHT_RANGE = (95.0, 120.0)  # cm
_WEIGHT_RANGE = (15.0, 25.0)  # kg

# ── 用藥：醫囑樣本（藥名 / 劑量 / 時段 / 備註）────────────────────────────
_MEDICATION_TEMPLATES = [
    ("退燒藥（普拿疼水劑）", "5ml", ["08:30", "12:30"], "發燒超過 38.5 度時服用"),
    ("抗過敏藥（勝克敏液）", "2.5ml", ["08:30"], "餐後服用"),
    ("止咳化痰藥水", "5ml", ["08:30", "12:30", "15:30"], "搖勻後服用"),
    ("腸胃藥（表飛鳴）", "1 包", ["12:30"], "午餐後配溫開水"),
    ("氣喘吸入劑", "1 次", ["08:30", "15:30"], "使用後漱口"),
]

# ── 過敏：樣本（過敏原 / 嚴重度 / 反應症狀 / 急救處置）────────────────────
# 嚴重度 severity 為明文列舉：mild / moderate / severe
_ALLERGY_TEMPLATES = [
    (
        "海鮮（蝦蟹貝類）",
        "severe",
        "皮膚紅疹、嘴唇腫脹",
        "立即停止進食、給予抗組織胺並通知家長",
    ),
    ("花生", "severe", "蕁麻疹、呼吸急促", "禁食含花生製品，必要時送醫"),
    ("蛋（蛋白）", "moderate", "口周紅疹、輕微嘔吐", "避免含蛋食品，觀察症狀"),
    ("牛奶（乳製品）", "moderate", "腹瀉、腹脹", "改用無乳製品餐點"),
    ("塵蟎", "mild", "打噴嚏、鼻塞", "保持環境清潔、定期清洗寢具"),
    ("芒果", "mild", "嘴角接觸性紅疹", "避免食用芒果及加工品"),
    ("堅果（腰果、核桃）", "severe", "喉嚨緊縮、蕁麻疹", "嚴格禁食堅果，備抗組織胺"),
    ("小麥（麩質）", "moderate", "腹痛、皮膚癢", "提供無麩質餐點"),
]


def _pick_employee_id(employees) -> int | None:
    """取一個 employee.id 當 measurements.created_by（FK 指向 employees）。"""
    if not employees:
        return None
    return _RNG.choice(employees).id


def _seed_measurements(session, students, employees) -> int:
    """每位取樣學生於上學期/下學期各一筆身高體重量測。冪等：(student_id, measured_on)。"""
    added = 0
    sample = students[:_MEASUREMENT_SAMPLE]
    for stu in sample:
        for term_start, term_end in (TERM1, TERM2):
            # 量測日期落在學期區間內，且不超過今天
            upper = min(term_end, TODAY)
            if term_start > upper:
                continue
            # 決定性日期/數值:純函式 (student, term)，重跑相同 → exists 命中 → 冪等。
            # 不可用 _common.rand_date_between(走 global random，每次不同 → 重複插入)。
            rr = random.Random(f"meas:{stu.id}:{term_start.isoformat()}")
            span = (upper - term_start).days
            measured_on = (
                term_start + timedelta(days=rr.randint(0, span))
                if span > 0
                else term_start
            )

            exists = (
                session.query(StudentMeasurement.id)
                .filter(
                    StudentMeasurement.student_id == stu.id,
                    StudentMeasurement.measured_on == measured_on,
                )
                .first()
            )
            if exists:
                continue

            height = round(rr.uniform(*_HEIGHT_RANGE), 1)
            weight = round(rr.uniform(*_WEIGHT_RANGE), 1)
            created_by = (
                employees[rr.randrange(len(employees))].id if employees else None
            )
            session.add(
                StudentMeasurement(
                    student_id=stu.id,
                    measured_on=measured_on,
                    height_cm=height,
                    weight_kg=weight,
                    created_by=created_by,
                )
            )
            added += 1
    return added


def _seed_medication(session, students, admin_user) -> tuple[int, int]:
    """約 8 名學生各 1-2 筆 order，每筆配 1-3 筆 log。

    冪等：
    - order：(student_id, order_date, medication_name)
    - log：(order_id, scheduled_time) 且 correction_of IS NULL（原始 log）
    回傳 (新增 order 數, 新增 log 數)。
    """
    added_orders = 0
    added_logs = 0
    actor_id = admin_user.id if admin_user else None
    # 取第 30~38 名學生避開量測樣本頭段，純為分散資料（非必要）
    med_students = students[30:38]

    for idx, stu in enumerate(med_students):
        n_orders = 1 + (idx % 2)  # 1 或 2 筆
        for o in range(n_orders):
            template = _MEDICATION_TEMPLATES[(idx + o) % len(_MEDICATION_TEMPLATES)]
            med_name, dose, slots, note = template
            # 決定性 order_date:純函式 (student, order index)，重跑相同 → 冪等。
            rr = random.Random(f"med:{stu.id}:{o}")
            _lo, _hi = TERM2[0], min(TERM2[1], TODAY)
            _span = (_hi - _lo).days
            order_date = (
                _lo + timedelta(days=rr.randint(0, _span)) if _span > 0 else _lo
            )

            order = (
                session.query(StudentMedicationOrder)
                .filter(
                    StudentMedicationOrder.student_id == stu.id,
                    StudentMedicationOrder.order_date == order_date,
                    StudentMedicationOrder.medication_name == med_name,
                )
                .first()
            )
            if order is None:
                order = StudentMedicationOrder(
                    student_id=stu.id,
                    order_date=order_date,
                    medication_name=med_name,
                    dose=dose,
                    time_slots=list(slots),
                    note=note,
                    created_by=actor_id,
                    source=MEDICATION_SOURCE_TEACHER,
                )
                session.add(order)
                session.flush()  # 取得 order.id 供 log FK
                added_orders += 1

            # 每張 order 為部分時段建立「已餵藥」log（1~min(3, slots)）
            n_logs = min(len(slots), 1 + ((idx + o) % 3))
            for slot in slots[:n_logs]:
                log_exists = (
                    session.query(StudentMedicationLog.id)
                    .filter(
                        StudentMedicationLog.order_id == order.id,
                        StudentMedicationLog.scheduled_time == slot,
                        StudentMedicationLog.correction_of.is_(None),
                    )
                    .first()
                )
                if log_exists:
                    continue

                hh, mm = slot.split(":")
                administered_at = datetime(
                    order_date.year,
                    order_date.month,
                    order_date.day,
                    int(hh),
                    int(mm),
                )
                session.add(
                    StudentMedicationLog(
                        order_id=order.id,
                        scheduled_time=slot,
                        administered_at=administered_at,
                        administered_by=actor_id,
                        skipped=False,
                        note="依醫囑完成餵藥",
                    )
                )
                added_logs += 1

    return added_orders, added_logs


def _seed_allergies(session, students) -> int:
    """約 8 名學生各一筆過敏。

    走 raw SQL（plaintext 寫入加密欄；dev 無金鑰時 ORM bind 會 raise）。
    冪等：raw SQL count on (student_id, allergen)。
    """
    added = 0
    actor_id = (
        # created_by 指向 users.id；用 admin user 當建立者
        None
    )
    admin = get_admin_user(session)
    if admin is not None:
        actor_id = admin.id

    # 取第 50~58 名學生（避開量測/用藥樣本，純分散）
    allergy_students = students[50:58]

    for stu, template in zip(allergy_students, _ALLERGY_TEMPLATES):
        allergen, severity, reaction, first_aid = template

        existing = session.execute(
            text(
                "SELECT count(*) FROM student_allergies "
                "WHERE student_id = :sid AND allergen = :al"
            ),
            {"sid": stu.id, "al": allergen},
        ).scalar()
        if existing:
            continue

        session.execute(
            text(
                "INSERT INTO student_allergies "
                "(student_id, allergen, severity, reaction_symptom, first_aid_note, "
                " active, created_by, created_at, updated_at) "
                "VALUES (:sid, :al, :sev, :rs, :fa, true, :cb, now(), now())"
            ),
            {
                "sid": stu.id,
                "al": allergen,
                "sev": severity,
                "rs": reaction,
                "fa": first_aid,
                "cb": actor_id,
            },
        )
        added += 1

    return added


def step() -> None:
    """灌健康模組示範資料（冪等）。重跑新增 0 筆。"""
    with session_scope() as session:
        students = get_active_students(session)
        employees = get_active_employees(session)
        admin_user = get_admin_user(session)

        n_meas = _seed_measurements(session, students, employees)
        n_orders, n_logs = _seed_medication(session, students, admin_user)
        n_allergy = _seed_allergies(session, students)

    logger.info(
        "health seed 完成：measurements +%d, orders +%d, logs +%d, allergies +%d",
        n_meas,
        n_orders,
        n_logs,
        n_allergy,
    )
    print(
        f"[health seed] student_measurements +{n_meas}, "
        f"student_medication_orders +{n_orders}, "
        f"student_medication_logs +{n_logs}, "
        f"student_allergies +{n_allergy}"
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    step()
