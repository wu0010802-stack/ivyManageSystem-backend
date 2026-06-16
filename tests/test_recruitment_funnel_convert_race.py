"""tests/test_recruitment_funnel_convert_race.py

Bug #20 回歸：deposited→enrolled 並發轉換 race 時，底層
`convert_recruitment_to_student` 會拋 `RecruitmentConversionError`
（既有/並發報名已建立 Student）。

修補前：該錯誤未被 `transition_visit` / API 捕捉 → 冒泡成 HTTP 500。
修補後：應包裝成 `RecruitmentFunnelError`（caller catch → 友善 409/400），
        且帶可辨識的 code 讓 API 把並發衝突映射為 409。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.base import Base
from models.classroom import Classroom, Student
from models.recruitment import RecruitmentVisit
import models.student_log  # noqa: F401 — 註冊 student_change_logs 進 metadata
import models.fees  # noqa: F401 — 註冊 student_fee_records 進 metadata
import models.portfolio  # noqa: F401 — 註冊 portfolio 表進 metadata
from services.recruitment_conversion import (
    convert_recruitment_to_student,
    RecruitmentConversionError,
)
from services.recruitment_funnel import (
    _do_convert,
    transition_visit,
    RecruitmentFunnelError,
)


@pytest.fixture
def session():
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()
    engine.dispose()


@pytest.fixture
def classroom(session):
    c = Classroom(name="小班-甲", school_year=114, semester=1, class_code="A")
    session.add(c)
    session.flush()
    return c


def _make_deposited_visit(session) -> RecruitmentVisit:
    v = RecruitmentVisit(
        month="115.03",
        child_name="測試幼生",
        has_deposit=True,
        enrolled=False,
    )
    session.add(v)
    session.flush()
    return v


def test_do_convert_wraps_conversion_error_as_funnel_error(session, classroom):
    """模擬並發 race：第一筆已轉換建立 Student；第二筆對同一 visit 再呼叫
    `_do_convert`，底層 `convert_recruitment_to_student` 的重複轉化守衛會拋
    `RecruitmentConversionError`。dispatch 層必須把它包成 `RecruitmentFunnelError`
    （否則 API 不 catch → 500）。
    """
    visit = _make_deposited_visit(session)

    # 第一筆轉換成功（模擬先到的並發請求已建立 Student）
    convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        classroom_id=classroom.id,
        recorded_by=1,
    )
    session.flush()

    # 第二筆（後到的並發請求）仍以為自己在 deposited 階段，走 _do_convert
    with pytest.raises(RecruitmentFunnelError) as exc:
        _do_convert(
            session,
            visit,
            classroom_id=classroom.id,
            actor_user_id=2,
        )
    # 並發衝突須有可辨識 code，API 才能映射成 409
    assert exc.value.code == "CONVERT_CONFLICT"


def test_transition_visit_convert_race_raises_funnel_not_conversion(session, classroom):
    """以 transition_visit orchestrator 驗：並發轉換 race 冒出來的型別是
    `RecruitmentFunnelError`（API try 區塊有 catch），而非 `RecruitmentConversionError`
    （未被 catch → 500）。"""
    visit = _make_deposited_visit(session)
    convert_recruitment_to_student(
        session,
        recruitment_visit_id=visit.id,
        classroom_id=classroom.id,
        recorded_by=1,
    )
    session.flush()

    # 直接呼叫 _do_convert 模擬「derive_stage 仍判定 deposited」的 race 視窗
    # （正常路徑 derive_stage 會因 Student 已存在而判 enrolled；此處針對 dispatch
    #  層的錯誤包裝行為做契約測試）。
    with pytest.raises(RecruitmentFunnelError):
        _do_convert(session, visit, classroom_id=classroom.id, actor_user_id=2)

    # 反向確認：底層服務本身仍拋的是 RecruitmentConversionError（行為未被改壞）
    with pytest.raises(RecruitmentConversionError):
        convert_recruitment_to_student(
            session,
            recruitment_visit_id=visit.id,
            classroom_id=classroom.id,
            recorded_by=3,
        )
