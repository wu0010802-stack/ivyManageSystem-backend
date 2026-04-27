"""SalaryEngine.load_config_from_db 是否真正載入 InsuranceRate。

回歸測試：原本 load_config_from_db 雖然 import InsuranceRate 但未查詢，
導致後台改費率不會影響 InsuranceService。本測試確保此鏈路被接通。
"""

import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.config import InsuranceRate
from models.database import Base
from services.salary_engine import SalaryEngine


@pytest.fixture
def db_factory(tmp_path):
    db_path = tmp_path / "insurance-rate-loading.sqlite"
    db_engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=db_engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = db_engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(db_engine)

    yield session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    db_engine.dispose()


class TestLoadConfigFromDbInsuranceRate:
    def test_load_uses_db_labor_rate_for_government_calc(self, db_factory):
        """DB 中的 labor_rate 必須影響 InsuranceService.labor_government 計算。"""
        with db_factory() as session:
            session.add(
                InsuranceRate(
                    rate_year=2026,
                    labor_rate=0.20,  # 預設 0.125 → 改高至 20%
                    labor_employee_ratio=0.20,
                    labor_employer_ratio=0.70,
                    health_rate=0.0517,
                    health_employee_ratio=0.30,
                    health_employer_ratio=0.60,
                    pension_employer_rate=0.06,
                    average_dependents=0.56,
                    is_active=True,
                )
            )
            session.commit()

        engine = SalaryEngine(load_from_db=True)
        # InsuranceService 應已採用 DB 中的 0.20 而非模組常數 0.125
        assert engine.insurance_service.labor_rate == pytest.approx(0.20)

        result = engine.insurance_service.calculate(salary=30000, dependents=0)
        # amount=30300, government_ratio = 1 - 0.20 - 0.70 = 0.10
        assert result.labor_government == pytest.approx(
            round(30300 * 0.20 * 0.10), abs=1
        )

    def test_load_falls_back_to_defaults_when_no_record(self, db_factory):
        """DB 無 InsuranceRate 紀錄時，沿用模組常數預設費率（不報錯）。"""
        engine = SalaryEngine(load_from_db=True)
        # 預設 labor_rate = 0.125
        assert engine.insurance_service.labor_rate == pytest.approx(0.125)

    def test_inactive_record_is_ignored(self, db_factory):
        """is_active=False 的紀錄不應被載入。"""
        with db_factory() as session:
            session.add(
                InsuranceRate(
                    rate_year=2026,
                    labor_rate=0.30,  # 故意設高，但 inactive
                    labor_employee_ratio=0.20,
                    labor_employer_ratio=0.70,
                    health_rate=0.0517,
                    health_employee_ratio=0.30,
                    health_employer_ratio=0.60,
                    pension_employer_rate=0.06,
                    average_dependents=0.56,
                    is_active=False,
                )
            )
            session.commit()

        engine = SalaryEngine(load_from_db=True)
        # 因為 inactive，仍用預設 0.125
        assert engine.insurance_service.labor_rate == pytest.approx(0.125)

    def test_employee_employer_amounts_unchanged_after_rate_load(self, db_factory):
        """關鍵：員工/雇主負擔金額仍由公告級距表決定（A 部分覆蓋方案）。"""
        with db_factory() as session:
            session.add(
                InsuranceRate(
                    rate_year=2026,
                    labor_rate=0.30,  # 大幅調整
                    labor_employee_ratio=0.40,
                    labor_employer_ratio=0.50,
                    health_rate=0.10,
                    health_employee_ratio=0.30,
                    health_employer_ratio=0.60,
                    pension_employer_rate=0.06,
                    average_dependents=0.56,
                    is_active=True,
                )
            )
            session.commit()

        engine = SalaryEngine(load_from_db=True)
        result = engine.insurance_service.calculate(salary=30000, dependents=0)
        # 級距表 amount=30300 對應的官方公告值
        assert result.labor_employee == 758
        assert result.health_employee == 470
