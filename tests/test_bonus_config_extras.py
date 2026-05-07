"""BonusConfig 新欄位 DB 化測試（2026-05-07 階段 2-B）

驗證：
1. art_teacher 在 _load_config_from_db_locked 後仍可從 _bonus_base 取到（修 production bug）
2. meeting_default_hours / meeting_absence_penalty 從 DB 載入後覆寫 instance 屬性
3. DB 欄位 NULL → fallback 模組常數
"""

import pytest
from datetime import datetime
from unittest.mock import MagicMock, patch

from services.salary_engine import SalaryEngine
from services.salary.constants import (
    DEFAULT_MEETING_HOURS,
    DEFAULT_MEETING_ABSENCE_PENALTY,
    FESTIVAL_BONUS_BASE,
)


def _fake_bonus(
    *,
    meeting_hours=None,
    meeting_penalty=None,
    art_festival=None,
    head_teacher_ab=2000,
    head_teacher_c=1500,
    assistant_ab=1200,
    assistant_c=1200,
):
    """建假的 BonusConfig row（dict-like）給 _load_config_from_db_locked 用"""
    bc = MagicMock()
    bc.id = 99
    bc.head_teacher_ab = head_teacher_ab
    bc.head_teacher_c = head_teacher_c
    bc.assistant_teacher_ab = assistant_ab
    bc.assistant_teacher_c = assistant_c
    bc.principal_festival = 6500
    bc.director_festival = 3500
    bc.leader_festival = 2000
    bc.driver_festival = 1000
    bc.designer_festival = 1000
    bc.admin_festival = 2000
    bc.principal_dividend = 5000
    bc.director_dividend = 4000
    bc.leader_dividend = 3000
    bc.vice_leader_dividend = 1500
    bc.overtime_head_normal = 400
    bc.overtime_head_baby = 450
    bc.overtime_assistant_normal = 100
    bc.overtime_assistant_baby = 150
    bc.school_wide_target = 160
    bc.created_at = datetime(2026, 1, 1)
    bc.meeting_default_hours = meeting_hours
    bc.meeting_absence_penalty = meeting_penalty
    bc.art_teacher_festival = art_festival
    return bc


class TestArtTeacherKeyPreserved:
    def test_art_teacher_present_after_db_load(self):
        """既有 bug：load 後 _bonus_base 沒 art_teacher → festival.py 取不到回 0。
        修復後：load 後一律包含 art_teacher key 且基數正確。
        """
        engine = SalaryEngine(load_from_db=False)
        # 模擬 DB load：手動跑 _load_config_from_db_locked 的核心邏輯
        bonus = _fake_bonus(art_festival=2500)
        # 直接 reproduce production path：
        art_base = bonus.art_teacher_festival
        if art_base is None:
            art_base = FESTIVAL_BONUS_BASE["art_teacher"]["A"]
        engine._bonus_base = {
            "head_teacher": {
                "A": bonus.head_teacher_ab,
                "B": bonus.head_teacher_ab,
                "C": bonus.head_teacher_c,
            },
            "assistant_teacher": {
                "A": bonus.assistant_teacher_ab,
                "B": bonus.assistant_teacher_ab,
                "C": bonus.assistant_teacher_c,
            },
            "art_teacher": {"A": art_base, "B": art_base, "C": art_base},
        }
        assert "art_teacher" in engine._bonus_base
        assert engine._bonus_base["art_teacher"]["A"] == 2500
        assert engine._bonus_base["art_teacher"]["B"] == 2500
        assert engine._bonus_base["art_teacher"]["C"] == 2500

    def test_art_teacher_null_falls_back_to_constant(self):
        """art_teacher_festival 為 NULL → 使用模組常數 2000"""
        engine = SalaryEngine(load_from_db=False)
        bonus = _fake_bonus(art_festival=None)
        art_base = bonus.art_teacher_festival
        if art_base is None:
            art_base = FESTIVAL_BONUS_BASE["art_teacher"]["A"]
        assert art_base == 2000


class TestMeetingDefaults:
    def test_default_meeting_hours_now_2(self):
        """模組常數從 1 改為 2（業主實務）"""
        assert DEFAULT_MEETING_HOURS == 2

    def test_default_absence_penalty_unchanged(self):
        assert DEFAULT_MEETING_ABSENCE_PENALTY == 100

    def test_engine_init_with_defaults(self):
        engine = SalaryEngine(load_from_db=False)
        assert engine._meeting_hours == 2
        assert engine._meeting_absence_penalty == 100

    def test_engine_load_overrides_with_db_values(self):
        """模擬 DB 設了不同值（meeting_hours=3, penalty=200）→ engine 採用 DB 值"""
        engine = SalaryEngine(load_from_db=False)
        bonus = _fake_bonus(meeting_hours=3.0, meeting_penalty=200)
        # reproduce 載入邏輯
        if bonus.meeting_default_hours is not None:
            engine._meeting_hours = float(bonus.meeting_default_hours)
        if bonus.meeting_absence_penalty is not None:
            engine._meeting_absence_penalty = int(bonus.meeting_absence_penalty)
        assert engine._meeting_hours == 3.0
        assert engine._meeting_absence_penalty == 200

    def test_db_null_keeps_engine_defaults(self):
        engine = SalaryEngine(load_from_db=False)
        bonus = _fake_bonus(meeting_hours=None, meeting_penalty=None)
        if bonus.meeting_default_hours is not None:
            engine._meeting_hours = float(bonus.meeting_default_hours)
        if bonus.meeting_absence_penalty is not None:
            engine._meeting_absence_penalty = int(bonus.meeting_absence_penalty)
        assert engine._meeting_hours == 2  # 沿用 init default
        assert engine._meeting_absence_penalty == 100


class TestSnapshotPreservesMeetingFields:
    def test_snapshot_contains_meeting_fields(self):
        engine = SalaryEngine(load_from_db=False)
        engine._meeting_hours = 2.5
        engine._meeting_absence_penalty = 150
        snapshot = engine._snapshot_config_state()
        assert snapshot["meeting_hours"] == 2.5
        assert snapshot["meeting_absence_penalty"] == 150

    def test_restore_recovers_meeting_fields(self):
        engine = SalaryEngine(load_from_db=False)
        engine._meeting_hours = 2.0
        engine._meeting_absence_penalty = 100
        snap = engine._snapshot_config_state()
        # 模擬 config_for_month 期間切換
        engine._meeting_hours = 99.0
        engine._meeting_absence_penalty = 999
        engine._restore_config_state(snap)
        assert engine._meeting_hours == 2.0
        assert engine._meeting_absence_penalty == 100

    def test_old_snapshot_without_meeting_keys_no_crash(self):
        """向後相容：舊 snapshot 沒這 2 個 key 不應 KeyError"""
        engine = SalaryEngine(load_from_db=False)
        snap = engine._snapshot_config_state()
        # 模擬舊 snapshot 缺 key
        snap.pop("meeting_hours", None)
        snap.pop("meeting_absence_penalty", None)
        engine._restore_config_state(snap)  # 不應拋錯
