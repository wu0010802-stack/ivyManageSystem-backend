"""
回歸測試：AuditMiddleware 背景寫入。

原設計在 request 週期內同步 commit AuditLog，拖慢所有寫操作。
改為 asyncio.create_task + to_thread 推入 threadpool 後，
必須驗證 row 真的寫進 DB,而不只是 middleware 不 crash。
"""

import asyncio
import os
import sys
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.database import AuditLog, Base
from utils.audit import (
    ENTITY_LABELS,
    _background_tasks,
    _parse_entity_type,
    _schedule_audit_write,
    _write_audit_sync,
    write_explicit_audit,
)


@pytest.fixture
def sqlite_engine(tmp_path):
    """建立隔離的 sqlite,並覆寫 base_module 的 engine/session factory。"""
    db_path = tmp_path / "audit.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    session_factory = sessionmaker(bind=engine)

    old_engine = base_module._engine
    old_session_factory = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)

    yield engine, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _sample_payload(**overrides):
    payload = dict(
        user_id=1,
        username="tester",
        action="CREATE",
        entity_type="employee",
        entity_id="42",
        summary="新增員工",
        ip_address="127.0.0.1",
        created_at=datetime.now(),
    )
    payload.update(overrides)
    return payload


class TestWriteAuditSync:
    def test_writes_audit_row(self, sqlite_engine):
        """同步寫入路徑能落地 AuditLog。"""
        _, session_factory = sqlite_engine

        _write_audit_sync(_sample_payload(summary="測試同步寫入"))

        with session_factory() as s:
            logs = s.query(AuditLog).all()

        assert len(logs) == 1
        assert logs[0].summary == "測試同步寫入"
        assert logs[0].action == "CREATE"
        assert logs[0].entity_type == "employee"

    def test_swallows_exceptions_silently(self, sqlite_engine, caplog, monkeypatch):
        """寫入失敗時僅記警告,不應拋錯。"""
        import utils.audit as audit_module

        caplog.set_level("WARNING", logger="utils.audit")

        def _raise():
            raise RuntimeError("boom")

        monkeypatch.setattr(audit_module, "get_session", _raise)

        _write_audit_sync(_sample_payload())  # 不應拋錯

        assert any("Audit log write failed" in r.message for r in caplog.records)


class TestScheduleAuditWrite:
    def test_scheduled_task_actually_writes_row(self, sqlite_engine):
        """在 event loop 中排程後,背景任務應寫入 AuditLog。"""
        _, session_factory = sqlite_engine

        async def run():
            _schedule_audit_write(_sample_payload(summary="測試背景寫入"))
            # 等背景 task 排空
            pending = list(_background_tasks)
            if pending:
                await asyncio.gather(*pending)

        asyncio.run(run())

        with session_factory() as s:
            logs = s.query(AuditLog).all()

        assert len(logs) == 1
        assert logs[0].summary == "測試背景寫入"

    def test_task_is_tracked_during_execution(self, sqlite_engine):
        """排程中的 task 應存在 _background_tasks,完成後自動移除。"""

        async def run():
            _schedule_audit_write(_sample_payload())
            assert len(_background_tasks) >= 1, "背景 task 未被追蹤"
            pending = list(_background_tasks)
            if pending:
                await asyncio.gather(*pending)
            assert all(t.done() for t in pending)

        asyncio.run(run())
        # 完成的 task 應被 done_callback 移除
        assert not any(not t.done() for t in _background_tasks)

    def test_no_event_loop_falls_back_to_sync(self, sqlite_engine):
        """沒有 event loop 時(如直接在測試函式呼叫),應同步寫入。"""
        _, session_factory = sqlite_engine

        _schedule_audit_write(_sample_payload(summary="無 loop 回退"))

        with session_factory() as s:
            logs = s.query(AuditLog).all()

        assert len(logs) == 1
        assert logs[0].summary == "無 loop 回退"


class TestExplicitAudit:
    """write_explicit_audit:給 GET 匯出端點顯式留稽核痕跡用。

    AuditMiddleware 只審計 POST/PUT/PATCH/DELETE,GET 匯出無法被自動覆蓋,
    這條路徑必須能獨立把 user/筆數/敏感旗標寫進 AuditLog。"""

    def _make_request(self):
        from starlette.requests import Request

        scope = {
            "type": "http",
            "method": "GET",
            "path": "/api/exports/employees",
            "headers": [],
            "query_string": b"",
            "client": ("10.0.0.1", 12345),
        }
        return Request(scope)

    def test_writes_row_with_changes(self, sqlite_engine):
        """提供 changes dict 應序列化成 JSON 寫入 changes 欄位。"""
        _, session_factory = sqlite_engine
        request = self._make_request()

        write_explicit_audit(
            request,
            action="EXPORT",
            entity_type="employee",
            summary="匯出員工名冊(3 筆,含完整銀行帳號)",
            changes={"count": 3, "is_full_bank_account": True},
        )

        with session_factory() as s:
            logs = s.query(AuditLog).all()

        assert len(logs) == 1
        log = logs[0]
        assert log.action == "EXPORT"
        assert log.entity_type == "employee"
        assert "含完整銀行帳號" in log.summary
        assert log.ip_address == "10.0.0.1"
        # changes JSON 應保留 is_full_bank_account 旗標,讓稽核可篩選
        assert log.changes is not None
        assert "is_full_bank_account" in log.changes
        assert "true" in log.changes.lower()

    def test_anonymous_when_no_token(self, sqlite_engine):
        """無 JWT 也應留下 anonymous 痕跡(避免拒絕 audit 寫入導致沉默)。"""
        _, session_factory = sqlite_engine
        request = self._make_request()

        write_explicit_audit(
            request,
            action="EXPORT",
            entity_type="employee",
            summary="匿名匯出測試",
        )

        with session_factory() as s:
            logs = s.query(AuditLog).all()

        assert len(logs) == 1
        assert logs[0].username == "anonymous"

    def test_swallows_exceptions(self, sqlite_engine, caplog, monkeypatch):
        """內部失敗只記警告,不可影響原 GET 請求回應。"""
        import utils.audit as audit_module

        caplog.set_level("WARNING", logger="utils.audit")

        def _raise(*_a, **_kw):
            raise RuntimeError("forced")

        monkeypatch.setattr(audit_module, "_schedule_audit_write", _raise)

        # 不應拋錯
        write_explicit_audit(
            self._make_request(),
            action="EXPORT",
            entity_type="employee",
            summary="例外測試",
        )

        assert any("Explicit audit write failed" in r.message for r in caplog.records)


class TestActivityEntityTypeMapping:
    """確保才藝系統各路徑都被 AuditMiddleware 覆蓋，且 POS 日結優先於 POS。"""

    @pytest.mark.parametrize(
        "path,expected_entity",
        [
            # 才藝報名（含 waitlist 合併進同一類）
            ("/api/activity/registrations", "activity_registration"),
            ("/api/activity/registrations/42", "activity_registration"),
            ("/api/activity/registrations/42/payment", "activity_registration"),
            ("/api/activity/registrations/42/payments/7", "activity_registration"),
            ("/api/activity/registrations/42/courses/3", "activity_registration"),
            ("/api/activity/waitlist/sweep-expired", "activity_registration"),
            # 其他 activity 子模組
            ("/api/activity/courses/1", "activity_course"),
            ("/api/activity/supplies/9", "activity_supply"),
            ("/api/activity/inquiries/5/reply", "activity_inquiry"),
            ("/api/activity/sessions/1/records", "activity_session"),
            ("/api/activity/settings/registration-time", "activity_settings"),
            # POS：daily-close 必須先於 pos 被匹配到（first match wins）
            ("/api/activity/pos/daily-close/2026-04-21", "activity_daily_close"),
            ("/api/activity/pos/checkout", "activity_pos"),
            # public 路徑目前刻意不進 audit
            ("/api/activity/public/register", None),
            ("/api/activity/public/update", None),
        ],
    )
    def test_entity_type_for_path(self, path, expected_entity):
        assert _parse_entity_type(path) == expected_entity

    def test_all_activity_entities_have_chinese_label(self):
        """新增的 entity_type 必須在 ENTITY_LABELS 有對應中文，否則前端下拉會顯示英文 key。"""
        required = {
            "activity_registration",
            "activity_course",
            "activity_supply",
            "activity_inquiry",
            "activity_session",
            "activity_pos",
            "activity_daily_close",
            "activity_settings",
        }
        missing = required - ENTITY_LABELS.keys()
        assert not missing, f"缺少中文 label 的 entity_type：{missing}"
