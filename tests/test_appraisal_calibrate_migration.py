"""Migration aprcal001 驗收：兩表存在 + 14 default rules + source_ref rename。"""

from datetime import date
from sqlalchemy import inspect

import pytest

from models.appraisal import AppraisalScoringRule, ScoreItemCode


def test_two_tables_exist(test_db_session):
    inspector = inspect(test_db_session.bind)
    tables = set(inspector.get_table_names())
    assert "appraisal_scoring_rules" in tables
    assert "appraisal_manual_event_counts" in tables


@pytest.fixture
def migrated_db(test_db_session):
    """模擬 migration INSERT 14 條 default rules（conftest 用 create_all 沒跑 alembic）。

    直接 import 真正的 DEFAULT_RULES，避免 list 走樣（P1-4 後改為 4-tuple
    含 applies_to_role_groups）。
    """
    # 用 importlib 載入 migration module（檔名以日期前綴）
    import importlib.util
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    mig_path = (
        repo_root / "alembic" / "versions" / "20260517_aprcal001_appraisal_calibrate.py"
    )
    spec = importlib.util.spec_from_file_location("aprcal001", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    for code, rtype, cfg, role_groups in mod.DEFAULT_RULES:
        test_db_session.add(
            AppraisalScoringRule(
                item_code=code,
                effective_from=date(2026, 1, 1),
                rule_type=rtype,
                rule_config=cfg,
                applies_to_role_groups=role_groups,
            )
        )
    test_db_session.flush()
    return test_db_session


def test_14_default_rules_inserted(migrated_db):
    rules = migrated_db.query(AppraisalScoringRule).all()
    codes = {r.item_code for r in rules}
    expected = {c.value for c in ScoreItemCode}
    assert codes == expected, f"missing={expected - codes}"
    # 全部 effective_from = 2026-01-01
    assert all(str(r.effective_from) == "2026-01-01" for r in rules)


def test_class_performance_rules_limited_to_teaching_roles(migrated_db):
    """P1-4：AFTER_CLASS_RATE / RETURNING_RATE_* 三條 default 規則必須限定
    給教學職（HEAD_TEACHER / ASSISTANT），不可為 NULL（=全部 role）。
    """
    teaching = {"HEAD_TEACHER", "ASSISTANT"}
    for code in ("AFTER_CLASS_RATE", "RETURNING_RATE_0315", "RETURNING_RATE_0915"):
        rule = migrated_db.query(AppraisalScoringRule).filter_by(item_code=code).first()
        assert rule is not None, f"{code} 未 seed"
        assert (
            rule.applies_to_role_groups is not None
        ), f"{code} applies_to_role_groups 不可為 NULL（廚工/行政會吃到虛假扣分）"
        assert (
            set(rule.applies_to_role_groups) == teaching
        ), f"{code} 應限 {teaching}，實際 {rule.applies_to_role_groups}"


def test_other_rules_not_role_restricted(migrated_db):
    """非班級績效類規則不應加上 role 限制（會誤殺廚工的請假/遲到扣分）。"""
    unrestricted = [
        "LATE_EARLY",
        "MISSING_PUNCH",
        "LEAVE",
        "REWARD_PUNISH",
        "SCHOOL_MEETING_ABSENCE",
        "OTHER",
    ]
    for code in unrestricted:
        rule = migrated_db.query(AppraisalScoringRule).filter_by(item_code=code).first()
        assert rule is not None, f"{code} 未 seed"
        assert (
            rule.applies_to_role_groups is None
        ), f"{code} 不應加 role 限制，實際 {rule.applies_to_role_groups}"
