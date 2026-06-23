"""tests/test_activity_student_id_dup_invariant_2026_06_23.py

Code review P1（2026-06-23）：公開/待審路徑沒有一致擋「同 student_id + 學期」重複報名。

不變量：同一在籍學生（student_id NOT NULL）同學期至多一筆 is_active 報名。

DB partial unique index `uq_activity_regs_student_term_active` 的鍵是
(student_name, birthday, school_year, semester, parent_phone) —— 含 parent_phone、
不含 student_id。`_match_student_with_parent_phone` 會同時比對 Student.parent_phone
與 Student.emergency_contact_phone（任一相符即匹配同一 student_id）。因此同一學生用
兩支官方電話報名 → 解析到同一 student_id、但 parent_phone 不同 → 不撞 unique index、
也躲過以 phone 為鍵的 existing 去重 → 同 student_id 同學期長出兩筆 active 報名
（容量重複佔用、在籍人頭灌水、POS 對帳分裂）。

家長登入 register（api/parent_portal/activity.py）與後台 match
（api/activity/registrations_pending.py）已守此不變量（advisory lock + student_id
檢查）；本測試覆蓋仍漏的三條路徑：
  1. 公開 public_register
  2. 公開 public_update（pending → matched 轉態）
  3. 後台 rematch（未改 name/birthday 時跳過既有 name+birthday 去重）

DB 隔離：SQLite + activity_client（monkeypatch base_module，不碰 dev PG）。
SQLite 下 advisory lock 為 no-op，但 sequential check-then-act 的去重仍有效，
故能在 SQLite 重現「第二筆同學生報名應被擋」。
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import ActivityRegistration, Student

from tests.test_activity_regressions import (  # noqa: F401
    activity_client,
    _create_admin,
    _create_classroom,
    _create_course,
    _current_term,
    _login,
)

NAME = "王小明"
BDAY = "2020-01-01"
PHONE_A = "0911111111"  # Student.parent_phone
PHONE_B = "0922222222"  # Student.emergency_contact_phone


def _seed_student_two_phones(sf):
    """建一個在籍學生（active 班級），含兩支官方電話。回傳 (student_id, classroom_id)。"""
    with sf() as s:
        classroom = _create_classroom(s, "海豚班")
        _create_course(s, "圍棋", 1200)
        stu = Student(
            student_id="S001",
            name=NAME,
            birthday=date(2020, 1, 1),
            classroom_id=classroom.id,
            parent_phone=PHONE_A,
            emergency_contact_phone=PHONE_B,
            is_active=True,
        )
        s.add(stu)
        s.commit()
        return stu.id, classroom.id


def _public_register(client, *, phone, name=NAME, birthday=BDAY, courses=("圍棋",)):
    return client.post(
        "/api/activity/public/register",
        json={
            "name": name,
            "birthday": birthday,
            "parent_phone": phone,
            "class": "海豚班",
            "courses": [{"name": n, "price": "1"} for n in courses],
            "supplies": [],
        },
    )


def _active_regs_for_student(sf, student_id):
    with sf() as s:
        return (
            s.query(ActivityRegistration)
            .filter(
                ActivityRegistration.student_id == student_id,
                ActivityRegistration.is_active.is_(True),
            )
            .count()
        )


# ── 路徑 1：公開 public_register ────────────────────────────────────────────────


def test_public_register_second_phone_same_student_blocked(activity_client):
    """同一學生用第二支官方電話公開報名 → 須擋（不可長出第二筆同 student_id active 報名）。"""
    client, sf = activity_client
    student_id, _ = _seed_student_two_phones(sf)

    r1 = _public_register(client, phone=PHONE_A)
    assert r1.status_code == 201, r1.text
    # 第一筆應已 matched（綁到 student_id）
    assert _active_regs_for_student(sf, student_id) == 1

    r2 = _public_register(client, phone=PHONE_B)
    assert r2.status_code == 400, (
        "同一學生第二支官方電話報名應被擋為 400，"
        f"卻得 {r2.status_code}（長出重複 active 報名）：{r2.text}"
    )
    assert _active_regs_for_student(sf, student_id) == 1


# ── 路徑 2：公開 public_update（pending → matched）────────────────────────────────


def test_public_update_pending_to_matched_dup_student_blocked(activity_client):
    """pending 報名透過改電話 re-match 到已有 active 報名的同學生 → 須擋。"""
    client, sf = activity_client
    student_id, _ = _seed_student_two_phones(sf)

    # reg1：phone A → matched active
    r1 = _public_register(client, phone=PHONE_A)
    assert r1.status_code == 201, r1.text
    assert _active_regs_for_student(sf, student_id) == 1

    # reg2：用不匹配的電話報名 → pending（student_id NULL）
    r2 = _public_register(client, phone="0900000000")
    assert r2.status_code == 201, r2.text
    reg2_id = r2.json()["id"]
    token2 = r2.json()["query_token"]

    # public_update reg2：改電話成 PHONE_B（學生 emergency 電話）→ 會 re-match 到同學生
    res = client.post(
        "/api/activity/public/update",
        json={
            "id": reg2_id,
            "name": NAME,
            "birthday": BDAY,
            "parent_phone": "0900000000",  # 舊號（驗身分）
            "query_token": token2,
            "new_parent_phone": PHONE_B,  # 新號，將 re-match 到同學生
            "class": "海豚班",
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
        },
    )
    assert res.status_code == 400, (
        "pending 報名 re-match 到已有 active 報名的同學生應被擋為 400，"
        f"卻得 {res.status_code}（長出重複 active 報名）：{res.text}"
    )
    assert _active_regs_for_student(sf, student_id) == 1


# ── 路徑 3：後台 rematch（未改 name/birthday）─────────────────────────────────────


def test_rematch_no_field_change_dup_student_blocked(activity_client):
    """後台對 pending 報名直接 rematch（未改欄位）解析到已有 active 報名的同學生 → 須擋。"""
    client, sf = activity_client
    student_id, classroom_id = _seed_student_two_phones(sf)
    sy, sem = _current_term()

    # reg1：phone A → matched active
    r1 = _public_register(client, phone=PHONE_A)
    assert r1.status_code == 201, r1.text

    # reg2：ORM 直接種一筆 pending（phone B 會匹配同學生，但目前 student_id NULL）
    with sf() as s:
        _create_admin(s)
        reg2 = ActivityRegistration(
            student_name=NAME,
            birthday=BDAY,
            parent_phone=PHONE_B,
            class_name=None,
            school_year=sy,
            semester=sem,
            student_id=None,
            classroom_id=None,
            is_active=True,
            pending_review=True,
            match_status="pending",
        )
        s.add(reg2)
        s.commit()
        reg2_id = reg2.id

    assert _login(client).status_code == 200
    # 不帶欄位變更的 rematch → field_changed=False → 直接重跑比對
    res = client.post(
        f"/api/activity/registrations/{reg2_id}/rematch",
        json={},
    )
    assert res.status_code == 400, (
        "rematch 未改欄位但解析到已有 active 報名的同學生應被擋為 400，"
        f"卻得 {res.status_code}（長出重複 active 報名）：{res.text}"
    )
    assert _active_regs_for_student(sf, student_id) == 1
