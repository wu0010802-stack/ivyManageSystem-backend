"""tests/test_data_quality_dispatch.py — Ch2 dispatch layer."""

from models.data_quality import DataQualityReport
from services.data_quality._base import Violation
from services.data_quality.dispatch import emit


def _v(rule_code="employee_active_but_offboarded", entity_id="42"):
    return Violation(
        rule_code=rule_code,
        severity="P1",
        entity_type="employee",
        entity_id=entity_id,
        summary=f"員工 #{entity_id} ...",
    )


def test_emit_writes_new_row_when_no_existing(test_db_session):
    v = _v()
    is_new = emit(v, test_db_session, line_queue=[])

    row = (
        test_db_session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .first()
    )
    assert row is not None
    assert row.status == "open"
    assert is_new is True


def test_emit_dedups_same_open_violation(test_db_session):
    v = _v(entity_id="43")
    is_new_first = emit(v, test_db_session, line_queue=[])
    is_new_second = emit(v, test_db_session, line_queue=[])

    rows = (
        test_db_session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .all()
    )
    assert len(rows) == 1
    assert is_new_first is True
    assert is_new_second is False
    assert rows[0].last_seen_at >= rows[0].detected_at


def test_emit_skips_ignored_status(test_db_session):
    v = _v(entity_id="44")
    # 預先插入 ignored row
    pre = DataQualityReport(
        rule_code=v.rule_code,
        severity=v.severity,
        entity_type=v.entity_type,
        entity_id=v.entity_id,
        summary="prev",
        dedup_key=v.dedup_key,
        status="ignored",
    )
    test_db_session.add(pre)
    test_db_session.commit()

    is_new = emit(v, test_db_session, line_queue=[])
    rows = (
        test_db_session.query(DataQualityReport)
        .filter(DataQualityReport.dedup_key == v.dedup_key)
        .all()
    )
    assert len(rows) == 1
    assert rows[0].status == "ignored"
    assert is_new is False


def test_emit_appends_to_line_queue_on_new_open(test_db_session):
    v = _v(entity_id="45")
    queue = []
    emit(v, test_db_session, line_queue=queue)
    assert v in queue


def test_emit_does_not_append_to_line_queue_on_dedup(test_db_session):
    v = _v(entity_id="46")
    queue = []
    emit(v, test_db_session, line_queue=queue)
    assert len(queue) == 1
    emit(v, test_db_session, line_queue=queue)
    assert len(queue) == 1  # dedup → 不再加


def test_flush_line_digest_skips_empty_queue(monkeypatch):
    """空 queue 不打 LINE。"""
    from services.data_quality import dispatch as d

    called = {"push": False}

    class FakeLine:
        def _push(self, text):
            called["push"] = True
            return True

    monkeypatch.setattr(d, "_get_line_service", lambda: FakeLine())

    d.flush_line_digest([])
    assert called["push"] is False


def test_flush_line_digest_pushes_when_queue_has_violations(monkeypatch):
    """非空 queue 推一則 digest（含前 3 條）。"""
    from services.data_quality import dispatch as d

    captured = {}

    class FakeLine:
        def _push(self, text):
            captured["text"] = text
            return True

    monkeypatch.setattr(d, "_get_line_service", lambda: FakeLine())

    queue = [_v(entity_id=str(i)) for i in range(5)]
    d.flush_line_digest(queue)
    assert "資料品質告警" in captured["text"]
    # 列首 3 條 + 「另 N 條」字樣
    assert "另 2 條" in captured["text"]
