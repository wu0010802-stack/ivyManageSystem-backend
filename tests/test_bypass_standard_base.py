"""Employee.bypass_standard_base 旗標測試（2026-05-07 議題 A 選項 3）

驗證：
1. 預設 False 時走 PositionSalaryConfig 標準化（鎖定既有行為）
2. True 時直接回傳 emp.base_salary（即使個人 base > 標準）
3. 時薪制（base=0）即使 bypass=True 仍回 0
4. 領導職（主任/園長）兩種旗標下行為一致（director/principal=None 時走 raw）
"""

from unittest.mock import MagicMock

import pytest

from services.salary_engine import SalaryEngine


@pytest.fixture
def engine():
    e = SalaryEngine(load_from_db=False)
    # 顯式設職位標準，確保測試不依賴 DB
    e._position_salary_standards = {
        "head_teacher_a": 39240,
        "head_teacher_b": 37160,
        "head_teacher_c": 33000,
        "assistant_teacher_a": 35240,
        "assistant_teacher_b": 33000,
        "assistant_teacher_c": 29500,
        "admin_staff": 37160,
        "english_teacher": 32500,
        "art_teacher": 30000,
        "designer": 30000,
        "nurse": 29800,
        "driver": 30000,
        "kitchen_staff": 29700,
        "director": None,
        "principal": None,
    }
    return e


def _emp(
    *, base, title, position, bonus_grade=None, bypass=False, employee_type="regular"
):
    e = MagicMock()
    e.base_salary = base
    e.title = title
    e.position = position
    e.bonus_grade = bonus_grade
    e.employee_type = employee_type
    e.bypass_standard_base = bypass
    e.job_title_rel = None
    return e


class TestDefaultBehaviorPreserved:
    """既有 standardize 行為（bypass=False 時）必須維持"""

    def test_b_grade_homeroom_matches_standard(self, engine):
        # 教保員(B 級) + 班導 → head_teacher_b = 37160；個人 raw 也 37160
        e = _emp(base=37160, title="教保員", position="班導")
        assert engine._resolve_standard_base(e) == 37160

    def test_a_grade_homeroom_with_bonus_above_standard(self, engine):
        # 幼兒園教師(A) + 班導，個人 raw 46499 > standard 39240
        # 預設行為：被覆寫成 39240
        e = _emp(base=46499, title="幼兒園教師", position="班導")
        assert engine._resolve_standard_base(e) == 39240

    def test_admin_staff_above_standard_overridden(self, engine):
        # 行政 raw 39351 > admin_staff standard 37160 → 覆寫
        e = _emp(base=39351, title="教保員", position="行政")
        assert engine._resolve_standard_base(e) == 37160


class TestBypassFlagShortCircuit:
    """bypass_standard_base=True 時短路使用 raw"""

    def test_bypass_returns_raw_when_above_standard(self, engine):
        e = _emp(base=46499, title="幼兒園教師", position="班導", bypass=True)
        assert engine._resolve_standard_base(e) == 46499

    def test_bypass_returns_raw_when_below_standard(self, engine):
        # 即使個人 raw < standard，bypass 仍信任 raw（業主刻意調低）
        e = _emp(base=35000, title="幼兒園教師", position="班導", bypass=True)
        assert engine._resolve_standard_base(e) == 35000

    def test_bypass_for_admin_returns_raw(self, engine):
        e = _emp(base=39351, title="教保員", position="行政", bypass=True)
        assert engine._resolve_standard_base(e) == 39351


class TestEdgeCases:
    def test_hourly_zero_base_returns_zero_regardless_of_bypass(self, engine):
        # 時薪制：即使 bypass=True，base=0 仍回 0
        e = _emp(base=0, title="助理教保員", position="班導", bypass=True)
        assert engine._resolve_standard_base(e) == 0

    def test_director_with_bypass_returns_raw(self, engine):
        # 主任 director=None → 兩種旗標下都走 raw
        e_off = _emp(base=42360, title="教保員", position="主任", bypass=False)
        e_on = _emp(base=42360, title="教保員", position="主任", bypass=True)
        assert engine._resolve_standard_base(e_off) == 42360
        assert engine._resolve_standard_base(e_on) == 42360

    def test_director_with_standard_set_respects_bypass(self, engine):
        # 主任 director 有設標準時：bypass=False 走標準 / bypass=True 走 raw
        engine._position_salary_standards["director"] = 50000
        e_off = _emp(base=42360, title="教保員", position="主任", bypass=False)
        e_on = _emp(base=42360, title="教保員", position="主任", bypass=True)
        assert engine._resolve_standard_base(e_off) == 50000
        assert engine._resolve_standard_base(e_on) == 42360

    def test_missing_attr_defaults_to_false(self, engine):
        """getattr fallback：若 emp 物件沒這個屬性（舊測試 fixture）視為 False"""
        e = MagicMock(
            spec=["base_salary", "title", "position", "bonus_grade", "job_title_rel"]
        )
        e.base_salary = 46499
        e.title = "幼兒園教師"
        e.position = "班導"
        e.bonus_grade = None
        e.job_title_rel = None
        # 這個 MagicMock 沒 bypass_standard_base 屬性 → 應 fallback 走標準
        assert engine._resolve_standard_base(e) == 39240
