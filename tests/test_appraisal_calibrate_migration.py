"""Migration aprcal001 驗收：兩表存在 + 14 default rules + source_ref rename。"""

from datetime import date
from sqlalchemy import inspect, text

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


def _run_migration_downgrade_source_ref_only(bind):
    """跑 aprcal001.downgrade() 的 source_ref reverse rename 區段。

    走 mock op.get_bind 讓 migration 真正 downgrade() 的 UPDATE 在
    SQLite 上執行；drop_index/drop_table 那段我們略過（讓 op 變 no-op），
    因為本測試只關心 source_ref reverse 是否完整。
    """
    import importlib.util
    import pathlib
    from unittest.mock import MagicMock, patch

    repo_root = pathlib.Path(__file__).resolve().parent.parent
    mig_path = (
        repo_root / "alembic" / "versions" / "20260517_aprcal001_appraisal_calibrate.py"
    )
    spec = importlib.util.spec_from_file_location("aprcal001_dg", mig_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    fake_op = MagicMock()
    fake_op.get_bind.return_value = bind
    # drop_index / drop_table no-op（測試不需真的 drop scoring_rules）
    fake_op.drop_index = MagicMock()
    fake_op.drop_table = MagicMock()

    with patch.object(mod, "op", fake_op):
        mod.downgrade()


def test_downgrade_restores_returning_rate_0915_source_ref(test_db_session):
    """P1-24：downgrade 必須同時還原 _0915 和 _0315 source_ref。

    aprcal001 upgrade 雖只有 REPLACE `_0315`，但 migration 後新版
    sync_score_items 會寫入 `auto:returning_rate_0915:N` rows（item_code
    `RETURNING_RATE_0915` 對應）。若 prod 緊急 downgrade，這些 rows 不還原
    舊代碼會變孤兒。
    """
    # 直接 raw insert 模擬 post-migration 三類 source_ref（SQLite 不強制
    # FK 約束，可省略 participant/cycle/catalog row 建置）。session.execute
    # 比 engine.execute 在 SA 2.x 更穩，且 op.get_bind 在 alembic 中也是
    # connection 不是 engine — 用 session 自己的連線最像真的。
    session = test_db_session
    session.execute(text("""
        INSERT INTO appraisal_score_items
            (id, participant_id, cycle_id, item_code, sequence_no,
             score_delta, source_ref)
        VALUES
            (1001, 1, 1, 'RETURNING_RATE_0315', 1, -1.7, 'auto:returning_rate_0315:42'),
            (1002, 1, 1, 'RETURNING_RATE_0915', 1, -1.7, 'auto:returning_rate_0915:42'),
            (1003, 1, 1, 'LATE_EARLY',          1, -0.5, 'auto:late_early:42')
    """))
    session.commit()

    # 跑真正 migration downgrade() 的 source_ref reverse rename，
    # 把 session 的 connection 注入給 op.get_bind 用。
    _run_migration_downgrade_source_ref_only(session.connection())

    # 三筆都應退回舊單一 `auto:returning_rate:` / `auto:attendance:`
    rows = session.execute(
        text(
            "SELECT id, source_ref FROM appraisal_score_items "
            "WHERE id IN (1001, 1002, 1003) ORDER BY id"
        )
    ).fetchall()
    by_id = {r[0]: r[1] for r in rows}
    assert by_id[1001] == "auto:returning_rate:42", "_0315 應還原"
    assert by_id[1002] == "auto:returning_rate:42", "_0915 應還原（P1-24 修補）"
    assert by_id[1003] == "auto:attendance:42", "late_early 應還原"
