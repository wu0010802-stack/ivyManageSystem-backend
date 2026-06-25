"""PII Retention GC 測試：retention 邊界、dry-run、SKIP LOCKED、idempotency。"""

from datetime import datetime, timedelta, timezone

import pytest

from models.activity import ActivityRegistration
from models.audit import AuditLog
from models.classroom import Student, LIFECYCLE_ACTIVE, LIFECYCLE_GRADUATED
from models.guardian import Guardian
from services.pii_retention_scheduler import _run_pii_retention_gc

_counter = 0


def _make_guardian_pair(
    session, *, lifecycle, days_ago, user_id=None, pii_redacted=False
):
    """建一對 student+guardian，方便重複用。"""
    global _counter
    _counter += 1
    student = Student(
        student_id=f"TEST-{_counter:04d}",
        name="畢業生",
        birthday=None,
        lifecycle_status=lifecycle,
        terminal_entered_at=(
            datetime.now(timezone.utc) - timedelta(days=days_ago)
            if lifecycle != LIFECYCLE_ACTIVE
            else None
        ),
    )
    session.add(student)
    session.flush()
    g = Guardian(
        student_id=student.id,
        user_id=user_id,
        name="王媽媽",
        phone="0912345678",
        email="mom@example.com",
        relation="母親",
        custody_note="探視週末",
        pii_redacted_at=(datetime.now(timezone.utc) if pii_redacted else None),
    )
    session.add(g)
    session.flush()
    return student, g


def test_gc_redacts_after_365_days(test_db_session, monkeypatch):
    # 直接 patch dry_run_enabled（與 test_gc_redacts_student_parent_snapshot 一致）：
    # setenv 在 test_db_session 已觸發 get_settings() 快取後才設定 → 太晚，dry_run
    # 仍為預設安全值 True → GC 不抹 → 測試在隔離/全套件下不穩定。
    monkeypatch.setattr(
        "services.pii_retention_scheduler.dry_run_enabled", lambda: False
    )
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.name == "[已離校家長]"
    assert g.phone is None
    assert g.email is None
    assert g.relation is None
    assert g.custody_note is None
    assert g.user_id is None
    assert g.pii_redacted_at is not None


def test_gc_redacts_student_parent_snapshot(test_db_session, monkeypatch):
    """GC 抹 Guardian 時，也要抹 students 表上的去正規化家長快照（parent_name/phone）。

    對稱補抹：否則同一份家長 PII 以明文續存於 students（雙寫副本），等同 GC 被繞過。
    """
    # 直接 patch dry_run_enabled：test_db_session setup 已先觸發 get_settings() 快取
    # dry_run=True（預設安全值），此時測試體內 setenv 已太晚。patch 確保關閉 dry-run。
    monkeypatch.setattr(
        "services.pii_retention_scheduler.dry_run_enabled", lambda: False
    )
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    student.parent_name = "王媽媽"
    student.parent_phone = "0912345678"
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(student)
    assert student.parent_name == "[已離校家長]"
    assert student.parent_phone is None


def test_gc_redacts_activity_registration_parent_pii(test_db_session, monkeypatch):
    """GC 抹 Guardian 時，也要抹 activity_registrations 上去正規化的家長聯絡 PII
    （公開報名表單雙寫的 parent_phone / email）。否則終態學生的家長 PII 以明文
    續存於才藝報名表，等同 GC 被繞過（個資法 §11；設計審查 2026-06-25 主題 B）。
    student_name / birthday 屬學生本人 PII，依 retention 政策保留。"""
    monkeypatch.setattr(
        "services.pii_retention_scheduler.dry_run_enabled", lambda: False
    )
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    reg = ActivityRegistration(
        student_name="小明",
        parent_phone="0912345678",
        email="mom@example.com",
        student_id=student.id,
        school_year=113,
        semester=1,
    )
    test_db_session.add(reg)
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(reg)
    assert reg.parent_phone is None
    assert reg.email is None
    # 學生本人 PII 保留（與 students 表只抹 parent_* 一致）
    assert reg.student_name == "小明"


def test_registry_covers_all_parent_pii_columns_in_models():
    """completeness 守衛（設計審查 2026-06-25 主題 B）：任何 model 上命名為
    parent_name / parent_phone / parent_email 的家長 PII 欄位都必須登記於
    PARENT_PII_DENORMALIZED_LOCATIONS，否則 GC 會漏抹該去正規化副本（個資法 §11）。
    2026-06-25 前 activity_registrations.parent_phone 即因未登記被漏抹。"""
    import models.database  # noqa: F401 觸發全 model 註冊
    from models.base import Base
    from services.pii_retention_scheduler import PARENT_PII_DENORMALIZED_LOCATIONS

    parent_pii_names = {"parent_name", "parent_phone", "parent_email"}
    registered = {
        (loc["table"], c)
        for loc in PARENT_PII_DENORMALIZED_LOCATIONS
        for c in (loc["null_columns"] + list(loc["placeholder_columns"]))
    }
    found = set()
    for mapper in Base.registry.mappers:
        table = mapper.local_table.name if mapper.local_table is not None else None
        if table is None:
            continue
        for col in mapper.columns:
            if col.name in parent_pii_names:
                found.add((table, col.name))
    missing = found - registered
    assert not missing, (
        "以下 model 家長 PII 欄位未登記於 PARENT_PII_DENORMALIZED_LOCATIONS，"
        f"GC 會漏抹（個資法 §11）: {sorted(missing)}；請補登記。"
    )


def test_registry_entries_reference_existing_columns():
    """防腐：registry 的表 / link_column / 抹除欄位都必須真實存在於 model
    （欄位改名後不致靜默失效、組出無效 UPDATE SQL）。"""
    import models.database  # noqa: F401
    from models.base import Base
    from services.pii_retention_scheduler import PARENT_PII_DENORMALIZED_LOCATIONS

    tables = {
        m.local_table.name: m.local_table
        for m in Base.registry.mappers
        if m.local_table is not None
    }
    for loc in PARENT_PII_DENORMALIZED_LOCATIONS:
        tbl = tables.get(loc["table"])
        assert tbl is not None, f"registry 表不存在: {loc['table']}"
        cols = {c.name for c in tbl.columns}
        assert (
            loc["link_column"] in cols
        ), f"{loc['table']}.{loc['link_column']} (link_column) 不存在"
        for c in loc["null_columns"] + list(loc["placeholder_columns"]):
            assert c in cols, f"{loc['table']}.{c} 不存在於 model"


def test_gc_skips_within_retention_window(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=300,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.name == "王媽媽"
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_skips_active_students(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_ACTIVE,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.phone == "0912345678"


def test_dry_run_does_not_modify(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "1")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.phone == "0912345678"
    assert g.pii_redacted_at is None


def test_gc_idempotent_skips_already_redacted(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
        pii_redacted=True,
    )
    initial_redacted_at = g.pii_redacted_at
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    # SQLite 存回時會剝 tzinfo；比較 naive 值即可（語意是「未被動過」）
    refreshed = g.pii_redacted_at
    if refreshed is not None and refreshed.tzinfo is None:
        initial_naive = initial_redacted_at.replace(tzinfo=None)
    else:
        initial_naive = initial_redacted_at
    assert refreshed == initial_naive


def test_gc_writes_audit_log_without_pii(test_db_session, monkeypatch):
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    log = (
        test_db_session.query(AuditLog)
        .filter(
            AuditLog.entity_type == "guardian",
            AuditLog.entity_id == str(g.id),
            AuditLog.username == "pii_retention_gc",
        )
        .first()
    )
    assert log is not None
    assert "0912345678" not in (log.changes or "")
    assert "mom@example.com" not in (log.changes or "")
    assert "王媽媽" not in (log.changes or "")
    assert str(student.id) in log.changes


def test_gc_redact_unlinks_user_so_parent_portal_returns_empty(
    test_db_session, monkeypatch
):
    """user_id 解綁後 _get_parent_student_ids 回空 list（家長 portal 看不到）。"""
    monkeypatch.setenv("PII_RETENTION_GC_DRY_RUN", "0")
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=99,
    )
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    from api.parent_portal._shared import _get_parent_student_ids

    test_db_session.expire_all()
    guardian_ids, student_ids = _get_parent_student_ids(test_db_session, 99)
    assert guardian_ids == []
    assert student_ids == []


def test_gc_redacts_soft_deleted_guardian(test_db_session, monkeypatch):
    """P2：軟刪除的 Guardian（deleted_at 已設）其 student 進終態 >365d 也必須被 GC

    抹 PII。原本 SELECT 帶 g.deleted_at IS NULL → 排除所有軟刪 Guardian → 離開
    系統的家長 PII（手機/Email/LINE user_id/監護說明）永久殘留（個資法 §11 破口）。
    """
    monkeypatch.setattr(
        "services.pii_retention_scheduler.dry_run_enabled", lambda: False
    )
    student, g = _make_guardian_pair(
        test_db_session,
        lifecycle=LIFECYCLE_GRADUATED,
        days_ago=400,
        user_id=7,
    )
    # 軟刪除（監護權變更/離婚/誤建修正）
    g.deleted_at = datetime.now(timezone.utc) - timedelta(days=30)
    test_db_session.commit()

    _run_pii_retention_gc(session=test_db_session)

    test_db_session.expire_all()
    test_db_session.refresh(g)
    assert g.name == "[已離校家長]"
    assert g.phone is None
    assert g.email is None
    assert g.user_id is None
    assert g.custody_note is None
    assert g.pii_redacted_at is not None
