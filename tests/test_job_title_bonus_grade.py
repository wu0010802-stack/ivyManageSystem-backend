"""job_titles.bonus_grade DB 化測試（2026-05-07 階段 2-D）

驗證：
1. festival._active_grade_map cache 注入後，get_position_grade 走 DB 值
2. None 注入回到 hardcode POSITION_GRADE_MAP fallback
3. snapshot/restore 把 grade_map 一起搬，避免歷史月切換污染 module cache
4. engine init 沒 load_from_db 時，festival 仍能用預設常數
"""

import pytest

from services.salary_engine import SalaryEngine
from services.salary import festival
from services.salary.constants import POSITION_GRADE_MAP


@pytest.fixture(autouse=True)
def reset_active_map():
    """每個 test 前後都把 module-level cache 清掉，避免互相污染"""
    festival.set_active_grade_map(None)
    yield
    festival.set_active_grade_map(None)


class TestActiveGradeMapInjection:
    def test_default_falls_back_to_constants(self):
        """未注入 cache 時：get_position_grade 走 hardcode POSITION_GRADE_MAP"""
        assert festival.get_position_grade("幼兒園教師") == "A"
        assert festival.get_position_grade("教保員") == "B"
        assert festival.get_position_grade("助理教保員") == "C"
        assert festival.get_position_grade("司機") is None

    def test_inject_overrides_constants(self):
        """注入新 mapping → 取代 hardcode"""
        festival.set_active_grade_map({"資深教保員": "A", "教保員": "A"})
        assert festival.get_position_grade("資深教保員") == "A"
        # 教保員從 B 變 A（DB 覆蓋）
        assert festival.get_position_grade("教保員") == "A"
        # 沒在新 map 裡的職稱不會再 fallback 到 hardcode（cache 是「整份取代」）
        assert festival.get_position_grade("助理教保員") is None

    def test_none_clears_cache_back_to_constants(self):
        festival.set_active_grade_map({"X": "A"})
        assert festival.get_position_grade("X") == "A"
        # 清空 → 走 hardcode
        festival.set_active_grade_map(None)
        assert festival.get_position_grade("X") is None
        assert festival.get_position_grade("幼兒園教師") == "A"

    def test_explicit_grade_map_param_wins(self):
        """caller 顯式傳 grade_map 時優先採用，與 cache 無關"""
        festival.set_active_grade_map({"幼兒園教師": "Z"})
        result = festival.get_position_grade(
            "幼兒園教師", grade_map={"幼兒園教師": "B"}
        )
        assert result == "B"


class TestEngineSnapshotIncludesGradeMap:
    def test_snapshot_contains_position_grade_map(self):
        engine = SalaryEngine(load_from_db=False)
        engine._position_grade_map = {"foo": "A"}
        snap = engine._snapshot_config_state()
        assert "position_grade_map" in snap
        assert snap["position_grade_map"] == {"foo": "A"}
        # 證明是 copy，後續異動不影響快照
        engine._position_grade_map["foo"] = "B"
        assert snap["position_grade_map"]["foo"] == "A"

    def test_restore_recovers_grade_map_and_injects_cache(self):
        engine = SalaryEngine(load_from_db=False)
        engine._position_grade_map = {"original": "A"}
        snap = engine._snapshot_config_state()
        # 模擬 config_for_month 期間切到歷史月，map 被換掉
        engine._position_grade_map = {"interim": "C"}
        festival.set_active_grade_map(engine._position_grade_map)
        # restore 後 engine + festival cache 都應回到原狀
        engine._restore_config_state(snap)
        assert engine._position_grade_map == {"original": "A"}
        assert festival._active_grade_map == {"original": "A"}

    def test_old_snapshot_without_position_grade_map_no_crash(self):
        """向後相容：舊 snapshot 沒這個 key 不應 KeyError"""
        engine = SalaryEngine(load_from_db=False)
        snap = engine._snapshot_config_state()
        snap.pop("position_grade_map", None)
        # 不應拋錯
        engine._restore_config_state(snap)


class TestEngineInitDoesNotPolluteFestivalCache:
    def test_init_without_load_keeps_festival_cache_none(self):
        """SalaryEngine(load_from_db=False) 不應主動寫入 festival._active_grade_map
        否則 test 之間會互相污染（除非 test 主動 reset）。"""
        # 確認本次 test 開始前 cache 已被 reset
        assert festival._active_grade_map is None
        SalaryEngine(load_from_db=False)
        # init 仍不應注入（grade_map 注入只在 _load_grade_map_from_db 中）
        assert festival._active_grade_map is None


class TestFallbackChainPriority:
    """fallback chain：caller 顯式 > module cache > hardcode"""

    def test_caller_param_wins_over_cache_and_hardcode(self):
        festival.set_active_grade_map({"X": "Z"})
        # caller 傳的 grade_map 應優先
        assert festival.get_position_grade("X", grade_map={"X": "Y"}) == "Y"

    def test_cache_wins_over_hardcode(self):
        # 重新指派 教保員 為 A
        festival.set_active_grade_map({"教保員": "A"})
        assert festival.get_position_grade("教保員") == "A"

    def test_hardcode_when_no_cache_and_no_param(self):
        # cache 已被 fixture reset
        assert festival.get_position_grade("教保員") == POSITION_GRADE_MAP["教保員"]
