"""tests/test_engine_config_swap_lock.py — engine 設定切換 / 重載 race condition
回歸測試（2026-05-06）。

問題情境：SalaryEngine.config_for_month 取 _config_swap_lock 序列化歷史月份
重算的 swap，但 load_config_from_db / set_bonus_config 沒拿同一把鎖：

  T1: config_for_month 進入 → snapshot OLD,apply 該月歷史設定,yield
  T2: PUT /api/config/* → load_config_from_db 直接寫入 NEW
  T1: yield 結束 → finally _restore_config_state(OLD) **整個蓋掉 NEW**
  → engine 卡在 OLD,直到下次有人觸發 reload 才恢復

修法：load_config_from_db / set_bonus_config 都拿同一把 RLock，T2 必須等 T1
restore 完才能寫入 → reload 永不被覆蓋。

涵蓋：
- 並發 reload 必須等 config_for_month 退出才能寫入（用 event 驗證 reload thread
  在 yield 期間 block 住，restore 後才完成）
- 最終 engine 反映新版本，而不是被 restore 蓋回舊版本
"""

from __future__ import annotations

import os
import sys
import threading
import time
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.database import (
    AttendancePolicy,
    BonusConfig as DBBonusConfig,
    InsuranceRate,
)


@pytest.fixture
def engine_with_db(tmp_path):
    """準備一個 SalaryEngine + 帶 OLD config 的 DB；產出 engine 後可由測試自由
    寫 NEW config 進 DB 觀察 race。"""
    db_path = tmp_path / "swaplock.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory

    Base.metadata.create_all(db_engine)

    # 種一份 OLD config
    with session_factory() as session:
        session.add(
            DBBonusConfig(
                is_active=True,
                version=1,
                config_year=2026,
                head_teacher_ab=1000,
                head_teacher_c=900,
                assistant_teacher_ab=800,
                assistant_teacher_c=700,
                principal_festival=5000,
                director_festival=4000,
                leader_festival=3000,
                principal_dividend=5000,
                director_dividend=4000,
                leader_dividend=3000,
                vice_leader_dividend=1500,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                overtime_head_normal=400,
                overtime_head_baby=450,
                overtime_assistant_normal=100,
                overtime_assistant_baby=150,
                school_wide_target=160,
            )
        )
        session.add(
            AttendancePolicy(
                is_active=True,
                version=1,
                festival_bonus_months=3,
            )
        )
        session.add(
            InsuranceRate(
                is_active=True,
                version=1,
                rate_year=2025,
                labor_rate=0.105,
                health_rate=0.0517,
            )
        )
        session.commit()

    from services.salary.engine import SalaryEngine

    eng = SalaryEngine(load_from_db=True)

    yield eng, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


def _seed_new_bonus(session_factory, *, head_teacher_ab=2000):
    """把現有 active bonus 停用、寫一份 NEW bonus 進去。"""
    with session_factory() as session:
        old = (
            session.query(DBBonusConfig)
            .filter(DBBonusConfig.is_active == True)
            .order_by(DBBonusConfig.id.desc())
            .first()
        )
        if old:
            old.is_active = False
        session.add(
            DBBonusConfig(
                is_active=True,
                version=2,
                config_year=2026,
                head_teacher_ab=head_teacher_ab,
                head_teacher_c=900,
                assistant_teacher_ab=800,
                assistant_teacher_c=700,
                principal_festival=5000,
                director_festival=4000,
                leader_festival=3000,
                principal_dividend=5000,
                director_dividend=4000,
                leader_dividend=3000,
                vice_leader_dividend=1500,
                driver_festival=1000,
                designer_festival=1000,
                admin_festival=2000,
                overtime_head_normal=400,
                overtime_head_baby=450,
                overtime_assistant_normal=100,
                overtime_assistant_baby=150,
                school_wide_target=160,
                created_at=datetime.now(),
            )
        )
        session.commit()


class TestLoadConfigFromDbBlocksOnSwapLock:
    def test_reload_waits_for_config_for_month_restore(self, engine_with_db):
        """reload thread 必須等 config_for_month 退出（restore 完）才能寫入；
        最終 engine state 反映 NEW，不會被 OLD snapshot 覆蓋。

        驗證流程：
          1. engine 啟動載入 OLD（head_teacher_ab=1000）
          2. T1 進 config_for_month yield 期間,DB 補入 NEW（=2000）
          3. T2 trigger reload（被 lock 阻塞,event 證明）
          4. T1 退出 → restore 完 → lock 釋放
          5. T2 完成 reload → 最終讀到 head_teacher_ab=2000（NEW）
        """
        eng, sf = engine_with_db

        # 確認 engine 啟動時讀的是 OLD（head_teacher_ab=1000）
        assert eng._bonus_base["head_teacher"]["A"] == 1000

        inside_yield = threading.Event()
        can_exit_yield = threading.Event()
        reload_done = threading.Event()

        def calc_thread():
            with sf() as session:
                with eng.config_for_month(session, 2026, 3):
                    inside_yield.set()
                    # 等主測試指示退出（給 reload thread 時間嘗試取 lock）
                    can_exit_yield.wait(timeout=5)

        def reload_thread():
            eng.load_config_from_db()
            reload_done.set()

        t_calc = threading.Thread(target=calc_thread, daemon=True)
        t_calc.start()
        assert inside_yield.wait(timeout=5), "config_for_month yield 沒進到位"

        # yield 期間補一份 NEW 進 DB
        _seed_new_bonus(sf, head_teacher_ab=2000)

        # 啟動 reload thread → 應該被 _config_swap_lock 阻擋
        t_reload = threading.Thread(target=reload_thread, daemon=True)
        t_reload.start()

        # 給 reload thread 一段時間嘗試取 lock；若 lock 沒生效這段時間內它應已寫入 NEW
        time.sleep(0.2)
        assert (
            not reload_done.is_set()
        ), "load_config_from_db 應被 _config_swap_lock 阻塞,卻在 yield 期間完成"

        # 放行 calc thread → restore OLD → lock 釋放 → reload 終於能寫
        can_exit_yield.set()
        t_calc.join(timeout=5)
        assert reload_done.wait(timeout=5), "reload thread 沒在 lock 釋放後完成"
        t_reload.join(timeout=5)

        # 最終 engine 應反映 NEW（head_teacher_ab=2000）；若 restore 蓋掉 NEW，
        # 此處會讀到 OLD=1000。
        assert eng._bonus_base["head_teacher"]["A"] == 2000

    def test_reload_runs_immediately_when_no_swap_active(self, engine_with_db):
        """無 config_for_month 進行中時,reload 不該被任何 lock 阻塞,直接寫入 NEW。"""
        eng, sf = engine_with_db
        assert eng._bonus_base["head_teacher"]["A"] == 1000

        _seed_new_bonus(sf, head_teacher_ab=3000)
        eng.load_config_from_db()
        assert eng._bonus_base["head_teacher"]["A"] == 3000


class TestSetBonusConfigTakesSwapLock:
    """set_bonus_config 與 load_config_from_db 同樣寫 _bonus_base 等屬性,
    必須拿 _config_swap_lock,否則同樣會被 config_for_month 的 restore 蓋掉。
    """

    def test_set_bonus_config_blocked_by_active_swap(self, engine_with_db):
        eng, sf = engine_with_db
        assert eng._bonus_base["head_teacher"]["A"] == 1000

        inside_yield = threading.Event()
        can_exit_yield = threading.Event()
        set_done = threading.Event()

        def calc_thread():
            with sf() as session:
                with eng.config_for_month(session, 2026, 3):
                    inside_yield.set()
                    can_exit_yield.wait(timeout=5)

        def set_thread():
            eng.set_bonus_config(
                {
                    "bonusBase": {
                        "headTeacherAB": 9999,
                        "headTeacherC": 8888,
                        "assistantTeacherAB": 7777,
                        "assistantTeacherC": 6666,
                    }
                }
            )
            set_done.set()

        t_calc = threading.Thread(target=calc_thread, daemon=True)
        t_calc.start()
        assert inside_yield.wait(timeout=5)

        t_set = threading.Thread(target=set_thread, daemon=True)
        t_set.start()
        time.sleep(0.2)
        assert not set_done.is_set(), "set_bonus_config 應被 _config_swap_lock 阻塞"

        can_exit_yield.set()
        t_calc.join(timeout=5)
        assert set_done.wait(timeout=5)
        t_set.join(timeout=5)

        # 最終 engine 反映 set_bonus_config 寫入的值，沒被 restore 蓋掉
        assert eng._bonus_base["head_teacher"]["A"] == 9999
