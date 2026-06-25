"""tests/test_audit_log_gc.py — P0b audit_log retention GC 測試。

Refs: docs/superpowers/specs/2026-05-28-audit-pii-redact-retention-design.md §4.3
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from models.audit import AuditLog
from models.base import Base
from utils.audit_log_gc import (
    _AUTH_DAYS,
    _FALLBACK_DAYS,
    _FINANCE_DAYS,
    _STUDENT_DAYS,
    _retention_days_for,
    cleanup_audit_logs,
)
from utils.taipei_time import now_taipei_naive

# ── retention 對照表 ──


def test_retention_days_finance():
    assert _retention_days_for("salary") == _FINANCE_DAYS
    assert _retention_days_for("fee") == _FINANCE_DAYS
    # 設計審查 2026-06-25 Finding D：原斷言用 "salary_record"，但該值從不被
    # AuditMiddleware 發出為 AuditLog.entity_type（僅 data_quality Violation /
    # seedgen 假資料用），是死值已移除。改用實際會發出的金流 entity_type。
    assert _retention_days_for("year_end_settlement") == _FINANCE_DAYS
    assert _retention_days_for("vendor_payment") == _FINANCE_DAYS


def test_retention_days_auth():
    assert _retention_days_for("auth") == _AUTH_DAYS


def test_retention_days_student():
    assert _retention_days_for("student") == _STUDENT_DAYS
    assert _retention_days_for("employee") == _STUDENT_DAYS
    assert _retention_days_for("attendance") == _STUDENT_DAYS
    assert _retention_days_for("medical") == _STUDENT_DAYS


def test_retention_days_fallback():
    assert _retention_days_for("unknown_type") == _FALLBACK_DAYS
    assert _retention_days_for("calendar_event") == _FALLBACK_DAYS


def test_retention_days_finance_real_emitted_money_types():
    """設計審查 2026-06-25 Finding D：_FINANCE_TYPES 原塞了從不發出的死值
    （year_end / salary_record / payslip / bonus / fee_record / appraisal_year_end），
    卻漏了 ENTITY_PATTERNS 實際會發出的金流 entity_type → 這些落 3 年 fallback
    而非稅捐稽徵法 §30 要求的 7 年金流保留。

    本測試鎖定「實際會發出且屬金流（牽動薪資/獎金/付款）」的 entity_type
    必須走 7 年 _FINANCE_DAYS。RED（修前）：全部走 _FALLBACK_DAYS（3 年）。
    """
    money_types = [
        # 年終獎金結算（獨立轉帳，金流）
        "year_end_cycle",
        "year_end_settlement",
        "year_end_special_bonus",
        "appraisal_payout",
        # 考核結算 / 獎金率（影響獎金金額）
        "appraisal_summary",
        "appraisal_bonus_rate",
        # 月度固定費用（金流）
        "monthly_fixed_cost",
        # 員工懲處扣薪 / 才藝鐘點費（皆牽動薪資）
        "disciplinary_action",
        "art_teacher_payroll",
    ]
    for et in money_types:
        assert _retention_days_for(et) == _FINANCE_DAYS, (
            f"{et} 屬金流，應走 7 年 _FINANCE_DAYS，"
            f"實得 {_retention_days_for(et)} 天"
        )


def test_retention_days_medication_log_not_finance():
    """給藥紀錄屬醫療紀錄，非金流，不應被誤分到 7 年金流組。

    醫療類沿用 fallback（3 年）即可；明確鎖定避免日後誤把 medication_log
    塞進 _FINANCE_TYPES。
    """
    assert _retention_days_for("medication_log") == _FALLBACK_DAYS


# ── cleanup_audit_logs SQL 行為（用 SQLite in-memory）──


@pytest.fixture()
def session():
    """SQLite in-memory session（不污染 dev DB）。"""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite:///:memory:")
    AuditLog.__table__.create(engine)
    Session = sessionmaker(bind=engine)
    s = Session()
    yield s
    s.close()


def _insert_log(session, entity_type: str, days_ago: int):
    log = AuditLog(
        user_id=1,
        username="x",
        action="UPDATE",
        entity_type=entity_type,
        entity_id="1",
        summary="test",
        ip_address="1.2.3.4",
        created_at=now_taipei_naive() - timedelta(days=days_ago),
    )
    session.add(log)
    session.commit()
    return log.id


def test_finance_log_older_than_7y_deleted(session):
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1
    assert session.query(AuditLog).count() == 0


def test_finance_log_within_7y_kept(session):
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS - 30)
    deleted = cleanup_audit_logs(session)
    assert deleted == 0
    assert session.query(AuditLog).count() == 1


def test_auth_log_older_than_6m_deleted(session):
    _insert_log(session, "auth", days_ago=_AUTH_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_auth_log_within_6m_kept(session):
    _insert_log(session, "auth", days_ago=_AUTH_DAYS - 10)
    deleted = cleanup_audit_logs(session)
    assert deleted == 0


def test_student_log_older_than_3y_deleted(session):
    _insert_log(session, "student", days_ago=_STUDENT_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_unknown_entity_type_uses_fallback_3y(session):
    _insert_log(session, "custom_type_xyz", days_ago=_FALLBACK_DAYS + 1)
    deleted = cleanup_audit_logs(session)
    assert deleted == 1


def test_mixed_entity_types(session):
    """不同 entity_type 按各自 retention 刪。"""
    _insert_log(session, "salary", days_ago=_FINANCE_DAYS + 1)  # 刪
    _insert_log(session, "salary", days_ago=100)  # 保
    _insert_log(session, "auth", days_ago=_AUTH_DAYS + 1)  # 刪
    _insert_log(session, "auth", days_ago=10)  # 保
    _insert_log(session, "student", days_ago=_STUDENT_DAYS + 1)  # 刪

    deleted = cleanup_audit_logs(session)
    assert deleted == 3
    assert session.query(AuditLog).count() == 2


def test_empty_table_no_op(session):
    deleted = cleanup_audit_logs(session)
    assert deleted == 0


def test_probe_failure_raises_not_silent_when_role_missing():
    """P2：PG 上 SET LOCAL ROLE audit_archiver 失敗（prod 漏建 role）時，cleanup
    必須 raise，讓 scheduler_iteration 計入 consecutive_failures → capture_exception
    /LINE 告警；而非靜默 return 0（否則 audit_logs §11 retention 永不執行卻監控全綠）。
    """
    from unittest.mock import MagicMock

    session = MagicMock()
    session.get_bind.return_value.dialect.name = "postgresql"

    def _execute(stmt, *a, **k):
        if "SET LOCAL ROLE" in str(stmt):
            raise Exception('role "audit_archiver" does not exist')
        return MagicMock()

    session.execute.side_effect = _execute

    with pytest.raises(RuntimeError, match="audit_archiver"):
        cleanup_audit_logs(session)


# ── run_audit_log_gc_once 的回傳契約（-> int，不可回 None）──


def test_run_once_returns_int_zero_when_cleanup_raises(monkeypatch):
    """回歸：cleanup_audit_logs raise（prod 漏建 audit_archiver role）時，
    run_audit_log_gc_once 必須回 int 0 而非 None。

    根因：原本 `return deleted` 寫在 `with scheduler_iteration(...)` 區塊內，
    內層 raise 被 scheduler_iteration by-design 吞掉後，該 return 從未執行 →
    函式 fall-through 回 None（雖簽名標 -> int）。caller 端 `if deleted > 0`
    就會炸 `TypeError: '>' not supported between NoneType and int`。
    """
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    import models.base as models_base
    import utils.audit_log_gc as gc
    from utils import scheduler_observability

    scheduler_observability.reset_for_tests()

    @contextmanager
    def _fake_scope():
        yield MagicMock()

    @contextmanager
    def _fake_lock(*args, **kwargs):
        yield True  # 已搶到 advisory lock

    def _raise_role_missing(_session):
        raise RuntimeError("audit_log GC 無法執行：SET ROLE audit_archiver 失敗")

    monkeypatch.setattr(models_base, "session_scope", _fake_scope)
    monkeypatch.setattr(gc, "try_scheduler_lock", _fake_lock)
    monkeypatch.setattr(gc, "cleanup_audit_logs", _raise_role_missing)

    result = gc.run_audit_log_gc_once(session_factory=None)

    assert result == 0
    assert isinstance(result, int)
    scheduler_observability.reset_for_tests()


def test_run_once_returns_zero_when_lock_not_acquired(monkeypatch):
    """另一 worker 已持鎖時回 0（不執行刪除），確保改動後此路徑仍回 int。"""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    import models.base as models_base
    import utils.audit_log_gc as gc
    from utils import scheduler_observability

    scheduler_observability.reset_for_tests()

    @contextmanager
    def _fake_scope():
        yield MagicMock()

    @contextmanager
    def _fake_lock(*args, **kwargs):
        yield False  # 沒搶到鎖

    monkeypatch.setattr(models_base, "session_scope", _fake_scope)
    monkeypatch.setattr(gc, "try_scheduler_lock", _fake_lock)

    result = gc.run_audit_log_gc_once(session_factory=None)

    assert result == 0
    assert isinstance(result, int)
    scheduler_observability.reset_for_tests()
