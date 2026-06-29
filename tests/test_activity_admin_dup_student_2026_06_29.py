"""tests/test_activity_admin_dup_student_2026_06_29.py

稽核（2026-06-29）：後台 create / edit 報名兩條路徑只用 name+birthday 去重，
解析 student_id 後未守「同 student_id 同學期至多一筆 active 報名」不變量。

情境：既有報名姓名有錯字，但已人工綁定到正確 student_id（student_name 仍是錯字）；
再以正確姓名建立／編輯報名時，name+birthday 去重抓不到那筆錯字報名，而
_match_student_id 以正確姓名解析到同一在籍學生 → 同 student_id 同學期長出第二筆
active 報名（容量重複佔用、在籍人頭灌水、POS 對帳分裂）。

家長／公開 register、公開 update、後台 rematch 路徑已守此不變量
（find_active_dup_for_student，見 test_activity_student_id_dup_invariant_2026_06_23.py）；
本測試覆蓋仍漏的後台兩條：
  1. 後台 admin_create_registration（POST /api/activity/registrations）
  2. 後台 update_registration_basic（PUT /api/activity/registrations/{id}）

DB 隔離：SQLite + activity_client（monkeypatch base_module，不碰 dev PG）。
SQLite 下 advisory lock 為 no-op，但 sequential check-then-act 去重仍有效。
"""

import os
import sys
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import ActivityRegistration, Student

from tests.test_activity_regressions import (  # noqa: F401
    _create_admin,
    _create_classroom,
    _create_course,
    _current_term,
    _login,
    activity_client,
)

CORRECT_NAME = "王小明"
TYPO_NAME = "王小眀"  # 錯字（同生日、不同字）
BDAY = "2020-01-01"


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


def _seed_student_and_typo_reg(sf):
    """建在籍學生（正確姓名）+ 一筆 student_name 為錯字、但已綁定該生的 active 報名。

    回傳 (student_id, classroom_name)。
    """
    sy, sem = _current_term()
    with sf() as s:
        _create_admin(s)
        classroom = _create_classroom(s, "海豚班")
        _create_course(s, "圍棋", 1200)
        stu = Student(
            student_id="S001",
            name=CORRECT_NAME,
            birthday=date(2020, 1, 1),
            classroom_id=classroom.id,
            is_active=True,
        )
        s.add(stu)
        s.flush()
        # 既有報名：student_name 是錯字，但已人工綁定正確 student_id
        reg1 = ActivityRegistration(
            student_name=TYPO_NAME,
            birthday=BDAY,
            class_name=classroom.name,
            classroom_id=classroom.id,
            school_year=sy,
            semester=sem,
            student_id=stu.id,
            is_active=True,
            match_status="manual",
        )
        s.add(reg1)
        s.commit()
        return stu.id, classroom.name


# ── 路徑 1：後台 admin_create_registration ─────────────────────────────────────


def test_admin_create_dup_student_via_typo_blocked(activity_client):
    """後台以正確姓名新增報名，解析到已有錯字 active 報名的同學生 → 須擋為 400。"""
    client, sf = activity_client
    student_id, classroom_name = _seed_student_and_typo_reg(sf)
    assert _login(client).status_code == 200
    assert _active_regs_for_student(sf, student_id) == 1

    res = client.post(
        "/api/activity/registrations",
        json={
            "name": CORRECT_NAME,
            "birthday": BDAY,
            "class": classroom_name,
            "courses": [{"name": "圍棋"}],
            "supplies": [],
        },
    )
    assert res.status_code == 400, (
        "後台新增解析到已有 active 報名的同學生應被擋為 400，"
        f"卻得 {res.status_code}（長出重複 active 報名）：{res.text}"
    )
    assert _active_regs_for_student(sf, student_id) == 1


# ── 路徑 2：後台 update_registration_basic ─────────────────────────────────────


def test_admin_edit_dup_student_via_typo_blocked(activity_client):
    """後台編輯校外生報名，改成正確姓名解析到已有錯字 active 報名的同學生 → 須擋為 400。"""
    client, sf = activity_client
    student_id, classroom_name = _seed_student_and_typo_reg(sf)
    sy, sem = _current_term()
    # reg2：校外生（student_id=None），改名成正確姓名後會 re-match 到同學生
    with sf() as s:
        reg2 = ActivityRegistration(
            student_name="陳小華",
            birthday="2019-05-05",
            class_name=classroom_name,
            school_year=sy,
            semester=sem,
            student_id=None,
            is_active=True,
            match_status="unmatched",
        )
        s.add(reg2)
        s.commit()
        reg2_id = reg2.id

    assert _login(client).status_code == 200

    res = client.put(
        f"/api/activity/registrations/{reg2_id}",
        json={
            "name": CORRECT_NAME,
            "birthday": BDAY,
            "class": classroom_name,
        },
    )
    assert res.status_code == 400, (
        "後台編輯改身分解析到已有 active 報名的同學生應被擋為 400，"
        f"卻得 {res.status_code}（長出重複 active 報名）：{res.text}"
    )
    assert _active_regs_for_student(sf, student_id) == 1
