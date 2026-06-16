"""bh-misc 批次修補回歸測試（2026-06-16）。

涵蓋四個 bug：
- #13 api/insurance.py POST /insurance/import 呼叫不存在的 service.import_table → 必 500。
       修法：刪除此 dead endpoint（級距維護已由 PUT /insurance/brackets 完整覆蓋）。
       回歸：斷言該路由已不存在於 router。
- #17 utils/kill_switch.py BYPASS_PATHS 把 admin 緊急登入路徑寫成 /auth/login、
       /auth/refresh，但實際掛載前綴為 /api/auth → 維護模式下 admin 無法登入自救。
       回歸：以真實掛載 path（auth router prefix + 路由）斷言命中 bypass。
- #29 api/auth.py CreateUserRequest.role / UpdateUserRequest.role 為自由 str，
       無白名單驗證 → 可寫入任意角色字串繞過以角色字串為準的安全閘。
       回歸：未知角色 → 422；已知核心角色 → 通過。
- #28 services/year_end/auto_derive/semester_dividend._activity_rate 才藝率分母用
       「現態 active 學生數」而非該學期在籍 → FIRST/SECOND 共用同一分母。
       本批次評估後判定乾淨改動風險過大（會牽動既有考核才藝率口徑一致性），
       改為加 TODO + 一個記錄「現行行為」的 characterization 測試（見 notes）。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ===========================================================================
# #13 — POST /insurance/import dead endpoint 已移除
# ===========================================================================


class TestInsuranceImportEndpointRemoved:
    def test_import_route_no_longer_registered(self):
        """/api/insurance/import 不應再註冊於 insurance router。

        此 endpoint 呼叫 service.import_table（不存在的方法）每次必 500；級距維護
        已由 PUT /insurance/brackets 完整覆蓋（含 finance-approve + stale-marking
        + reason 守衛），import 這條反缺守衛，應整條移除。
        """
        from api.insurance import router

        paths = {r.path for r in router.routes}
        assert "/api/insurance/import" not in paths, (
            "POST /insurance/import 為 dead endpoint（呼叫不存在的 import_table），"
            "應已移除"
        )

    def test_insurance_service_has_no_import_table(self):
        """坐實根因：InsuranceService 並無 import_table 方法（呼叫即 AttributeError → 500）。"""
        from services.insurance_service import InsuranceService

        assert not hasattr(InsuranceService, "import_table")

    def test_brackets_routes_still_present(self):
        """確認移除 import 後，級距維護路由（取代者）仍在。"""
        from api.insurance import router

        paths = {(r.path, frozenset(r.methods)) for r in router.routes}
        assert ("/api/insurance/brackets", frozenset({"PUT"})) in paths
        assert ("/api/insurance/brackets", frozenset({"GET"})) in paths


# ===========================================================================
# #17 — kill_switch BYPASS_PATHS 用完整掛載前綴
# ===========================================================================


class TestKillSwitchBypassFullPrefix:
    def test_bypass_paths_use_api_auth_prefix(self):
        """BYPASS_PATHS 必須含完整掛載前綴 /api/auth/login、/api/auth/refresh。"""
        from utils.kill_switch import KillSwitchMiddleware

        assert "/api/auth/login" in KillSwitchMiddleware.BYPASS_PATHS
        assert "/api/auth/refresh" in KillSwitchMiddleware.BYPASS_PATHS
        # 舊的錯誤前綴（永不命中真實掛載 path）不應殘留
        assert "/auth/login" not in KillSwitchMiddleware.BYPASS_PATHS
        assert "/auth/refresh" not in KillSwitchMiddleware.BYPASS_PATHS

    def test_real_mounted_auth_paths_match_bypass(self):
        """以 auth router 實際掛載 path 斷言命中 bypass（防前綴再次漂移）。

        auth router prefix = /api/auth；登入路由 /login、刷新路由 /refresh。
        實際掛載 path = prefix + route → /api/auth/login、/api/auth/refresh，
        必須在 BYPASS_PATHS 中，否則維護模式下 admin 無法登入自救。
        """
        from api.auth import router as auth_router
        from utils.kill_switch import KillSwitchMiddleware

        mounted = {r.path for r in auth_router.routes}
        assert "/api/auth/login" in mounted, "login 實際掛載 path 與假設不符"
        assert "/api/auth/refresh" in mounted, "refresh 實際掛載 path 與假設不符"

        for path in ("/api/auth/login", "/api/auth/refresh"):
            assert (
                path in KillSwitchMiddleware.BYPASS_PATHS
            ), f"{path} 為 admin 緊急登入路徑，必須在 BYPASS_PATHS（維護模式放行）"

    def test_maintenance_mode_lets_login_through(self, monkeypatch):
        """整合層：維護模式開啟時，掛 KillSwitch 的 app 仍放行 /api/auth/login。"""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        monkeypatch.setenv("MAINTENANCE_MODE", "1")
        monkeypatch.setenv("MAINTENANCE_MESSAGE", "維護中")

        from config import reset_for_tests

        reset_for_tests()

        from utils.kill_switch import KillSwitchMiddleware

        app = FastAPI()
        app.add_middleware(KillSwitchMiddleware)

        @app.post("/api/auth/login")
        async def _login():
            return {"ok": True}

        @app.post("/api/auth/refresh")
        async def _refresh():
            return {"ok": True}

        @app.post("/api/other")
        async def _other():
            return {"ok": True}

        client = TestClient(app)
        assert client.post("/api/auth/login").status_code == 200
        assert client.post("/api/auth/refresh").status_code == 200
        # 對照：非 bypass 路徑在維護模式仍 503
        assert client.post("/api/other").status_code == 503

        reset_for_tests()


# ===========================================================================
# #29 — Create/UpdateUserRequest.role 角色白名單驗證
# ===========================================================================


class TestUserRoleWhitelist:
    def test_create_user_rejects_unknown_role(self):
        """CreateUserRequest 帶未知角色字串 → ValidationError（API 層回 422）。"""
        from api.auth import CreateUserRequest

        with pytest.raises(ValidationError):
            CreateUserRequest(
                username="evil",
                password="whatever",
                role="superuser_god_mode",
            )

    def test_create_user_accepts_known_roles(self):
        """核心角色（hr/teacher/...）皆應通過。"""
        from api.auth import CreateUserRequest

        for role in (
            "admin",
            "hr",
            "supervisor",
            "principal",
            "accountant",
            "teacher",
            "parent",
        ):
            obj = CreateUserRequest(username="u", password="p", role=role)
            assert obj.role == role

    def test_create_user_default_role_is_valid(self):
        """未指定 role 時走預設（teacher），不應因驗證而爆。"""
        from api.auth import CreateUserRequest

        obj = CreateUserRequest(username="u", password="p")
        assert obj.role == "teacher"

    def test_update_user_rejects_unknown_role(self):
        """UpdateUserRequest 帶未知角色 → ValidationError。"""
        from api.auth import UpdateUserRequest

        with pytest.raises(ValidationError):
            UpdateUserRequest(role="root")

    def test_update_user_allows_none_role(self):
        """UpdateUserRequest.role 為 Optional；None（不改角色）仍合法。"""
        from api.auth import UpdateUserRequest

        obj = UpdateUserRequest(role=None)
        assert obj.role is None

    def test_update_user_accepts_known_role(self):
        from api.auth import UpdateUserRequest

        obj = UpdateUserRequest(role="hr")
        assert obj.role == "hr"


# ===========================================================================
# #28 — semester_dividend 才藝率分母（characterization：記錄現行行為）
# ===========================================================================
#
# 現行：分母用「現態 active 學生數」（lifecycle_status==active），FIRST/SECOND
# 兩列共用同一分母（對齊既有考核 status_aggregator 才藝率口徑）。
# 本批次判定乾淨改成「per-semester point-in-time 在籍」風險過大（牽動既有考核
# 才藝率一致性，且無現成 per-semester 在籍快照），故僅加 TODO + 此 characterization
# 測試釘住現行行為，待業主確認口徑後再實作（見 notes）。


_ACADEMIC_YEAR = 114


@pytest.fixture
def _div_session():
    # 先 import 全部用到的 model module，確保 metadata 完整建表
    from models.activity import ActivityCourse, ActivityRegistration  # noqa: F401
    from models.base import Base
    from models.classroom import Classroom, Student  # noqa: F401
    from models.config import BonusConfig  # noqa: F401
    from models.year_end import (  # noqa: F401
        ClassEnrollmentTarget,
        SpecialBonusItem,
        YearEndCycle,
    )

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


class TestActivityRateDenominatorCurrentBehavior:
    def test_first_and_second_share_current_active_denominator(self, _div_session):
        """characterization：FIRST 與 SECOND 才藝率分母皆為「現態 active 學生數」。

        班內現有 5 名 active 學生（分母固定 5）。上學期 4 名報名（4/5=0.8），
        下學期 1 名報名（1/5=0.2）。兩學期共用同一分母 5。
        （若日後改 per-semester point-in-time 在籍，此測試會 RED → 提醒同步更新口徑。）
        """
        from models.activity import ActivityCourse, ActivityRegistration
        from models.classroom import LIFECYCLE_ACTIVE, Classroom, Student
        from services.year_end.auto_derive.semester_dividend import _activity_rate

        s = _div_session
        cls = Classroom(name="天堂鳥", school_year=_ACADEMIC_YEAR, semester=1)
        s.add(cls)
        s.flush()

        students = []
        for i in range(5):
            st = Student(
                student_id=f"S{i:04d}",
                name=f"生{i}",
                classroom_id=cls.id,
                enrollment_school_year=113,
                enrollment_date=date(2025, 9, 1),
                lifecycle_status=LIFECYCLE_ACTIVE,
            )
            s.add(st)
            students.append(st)
        s.flush()

        course1 = ActivityCourse(
            name="美術", price=1000, school_year=_ACADEMIC_YEAR, semester=1
        )
        course2 = ActivityCourse(
            name="音樂", price=1000, school_year=_ACADEMIC_YEAR, semester=2
        )
        s.add_all([course1, course2])
        s.flush()

        # 上學期：前 4 名報名
        for st in students[:4]:
            s.add(
                ActivityRegistration(
                    student_name=st.name,
                    classroom_id=cls.id,
                    student_id=st.id,
                    match_status="matched",
                    school_year=_ACADEMIC_YEAR,
                    semester=1,
                    is_active=True,
                )
            )
        # 下學期：僅第 1 名報名
        s.add(
            ActivityRegistration(
                student_name=students[0].name,
                classroom_id=cls.id,
                student_id=students[0].id,
                match_status="matched",
                school_year=_ACADEMIC_YEAR,
                semester=2,
                is_active=True,
            )
        )
        s.commit()

        rate1, reg1, enrolled1 = _activity_rate(
            s, classroom_id=cls.id, academic_year=_ACADEMIC_YEAR, semester=1
        )
        rate2, reg2, enrolled2 = _activity_rate(
            s, classroom_id=cls.id, academic_year=_ACADEMIC_YEAR, semester=2
        )

        # 現行行為：兩學期分母皆 = 現態 active 學生數 5（共用）
        assert enrolled1 == 5
        assert enrolled2 == 5
        assert enrolled1 == enrolled2, "現行：FIRST/SECOND 共用同一現態分母"
        assert reg1 == 4 and reg2 == 1
        assert rate1 == Decimal(4) / Decimal(5)
        assert rate2 == Decimal(1) / Decimal(5)
