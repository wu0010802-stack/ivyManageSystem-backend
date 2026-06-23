"""tests/test_activity_attendance_batch_partial_rollback_2026_06_23.py

回歸護欄（2026-06-23 audit）：批次點名「同批先更新既有列、後撞併發插入衝突」
的情境是**安全**的——稍早既有列的 in-place 更新不會因稍後新增列的 savepoint
回退而丟失。本檔將此不變量釘成回歸測試，防止未來重構打破。

背景：一份 code review 擔心以下順序會靜默丟失更新——
  - regA：已有 attendance 列且在 existing_map 中 → 走 existing 更新分支（in-place 改）
  - regB：existing_map 看不到（stale snapshot）但 DB 已有衝突列 → 走 insert 分支
    → begin_nested 內 flush 把 regA 的 pending UPDATE 一併送進 savepoint
    → regB INSERT 撞 uq_activity_attendance_session_reg → ROLLBACK TO SAVEPOINT
       回退 savepoint 內送出的 UPDATE
  擔心 regA 的更新就此丟失，但回應仍報 updated=N。

實測證偽：SQLAlchemy 的 unit-of-work 在 savepoint 回退後會把 regA 還原為
dirty（in-memory 屬性變更仍 pending），於外層 session.commit() 時**重新 flush**
→ regA 的 UPDATE 被重送並落地。此為 ORM 層行為，PostgreSQL 與 SQLite 一致，
故無需修補。本測試固定此行為。

沿用 test_activity_attendance_batch_race.py 的整合測試慣例（TestClient +
monkeypatch base_module._SessionFactory）。本檔額外用「部分 stale」session：
existing_map 查詢看得到 regA、看不到 regB（只隱藏 regB），以精準觸發
「先更新 regA、後插入 regB 撞衝突」的順序，並斷言 regA 的更新仍存活。
"""

import os
import sys

import pytest
from sqlalchemy.orm import Session as _SASession
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.activity import (
    ActivityAttendance,
    ActivityCourse,
    ActivityRegistration,
    RegistrationCourse,
)
from tests.test_activity_attendance_batch_race import (  # noqa: F401  (client 為 fixture)
    _pre_insert_attendance,
    _setup_scene,
    client,
)
from tests.test_activity_pos import _login
from utils.academic import resolve_current_academic_term


def _make_partial_stale_factory(real_sf, hide_reg_id: int):
    """回傳 session factory：existing_map（首次 ActivityAttendance 查詢）回傳
    結果剔除 hide_reg_id，模擬「該列在快照建立後才由併發請求落地」。其餘
    ActivityAttendance 查詢（例如 except fallback 的 .one()）正常走真實 DB。"""

    class PartialStaleSession(_SASession):
        _stale_done: bool = False

        def query(self, *entities, **kwargs):
            q = super().query(*entities, **kwargs)
            if (
                not self._stale_done
                and len(entities) == 1
                and entities[0] is ActivityAttendance
            ):
                self._stale_done = True

                class _Wrap:
                    def __init__(self, inner):
                        self._inner = inner

                    def filter(self, *a, **kw):
                        self._inner = self._inner.filter(*a, **kw)
                        return self

                    def all(self):
                        return [
                            a
                            for a in self._inner.all()
                            if a.registration_id != hide_reg_id
                        ]

                return _Wrap(q)
            return q

    return sessionmaker(bind=real_sf.kw["bind"], class_=PartialStaleSession)


def test_batch_existing_update_not_lost_when_later_insert_conflicts(
    client, monkeypatch
):
    """同批稍早的既有列更新，不應因稍後新增列撞唯一約束的 savepoint 回退而丟失。"""
    c, sf = client
    _, session_id, regA = _setup_scene(sf)

    # 額外建一個 enrolled 報名 regB（同課同場次）
    with sf() as s:
        sy, sem = resolve_current_academic_term()
        course_id = (
            s.query(ActivityCourse).filter(ActivityCourse.name == "圍棋").first().id
        )
        reg2 = ActivityRegistration(
            student_name="小華",
            birthday="2019-03-03",
            class_name="大班",
            is_active=True,
            school_year=sy,
            semester=sem,
        )
        s.add(reg2)
        s.flush()
        s.add(
            RegistrationCourse(
                registration_id=reg2.id,
                course_id=course_id,
                status="enrolled",
                price_snapshot=1500,
            )
        )
        s.flush()
        regB = reg2.id
        s.commit()

    # regA：既有 attendance（is_present=True），稍後端點要改成 False（走更新分支）
    _pre_insert_attendance(sf, session_id, regA, is_present=True)
    # regB：併發已落地的衝突列（is_present=True）——existing_map 將看不到它（走插入→衝突）
    _pre_insert_attendance(sf, session_id, regB, is_present=True)

    stale_sf = _make_partial_stale_factory(sf, hide_reg_id=regB)
    monkeypatch.setattr(base_module, "_SessionFactory", stale_sf)

    _login(c)
    resp = c.put(
        f"/api/activity/attendance/sessions/{session_id}/records",
        json={
            "records": [
                {"registration_id": regA, "is_present": False, "notes": "改A"},
                {"registration_id": regB, "is_present": False, "notes": "改B"},
            ]
        },
    )
    assert resp.status_code == 200, resp.text

    with sf() as s:
        att_a = (
            s.query(ActivityAttendance)
            .filter_by(session_id=session_id, registration_id=regA)
            .one()
        )
        assert att_a.is_present is False, (
            "regA 的更新不應因 regB 衝突列的 savepoint 回退而丟失，"
            f"實際 is_present={att_a.is_present}"
        )
        att_b = (
            s.query(ActivityAttendance)
            .filter_by(session_id=session_id, registration_id=regB)
            .one()
        )
        assert att_b.is_present is False
