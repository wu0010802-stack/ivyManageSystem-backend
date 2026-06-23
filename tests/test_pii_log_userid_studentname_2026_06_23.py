"""資安回歸：log 落明文 PII（LINE userId / 學生姓名）。

PIIRedactionFilter 只遮 key=value 與 'key': 'value' 形式；下列兩處為「裸值」或
key 不在 denylist，明文落 log：
- services/notification/dispatch.py：`user=%s` 直接記原始 LINE userId（key `user`
  不在 denylist）。LINE userId 為可關聯個人的識別碼。
- api/dismissal_calls.py：`學生 %s` 記學生姓名（裸值，無 key）。child PII。

修補：LINE userId 截短為前 8 碼（沿用 services/line_service.py 既有 pattern）；
學生姓名改記 student_id。
"""

import logging
import os
import sys

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base

# ───────────────────────── dispatch.py：LINE userId ─────────────────────────


def test_send_to_line_user_sync_log_truncates_userid(caplog, monkeypatch):
    """推送失敗的 warning log 只保留 userId 前 8 碼，不落完整 LINE userId。"""
    from services.notification import dispatch as dispatch_mod

    FULL_ID = "U" + "f" * 32  # 33 碼，前 8 碼 ≠ 完整

    class _BoomAdapter:
        def send(self, *a, **k):
            raise RuntimeError("line down")

    monkeypatch.setattr("services.notification.renderers.render", lambda *a, **k: "msg")
    monkeypatch.setattr(dispatch_mod, "_get_line_adapter", lambda: _BoomAdapter())

    with caplog.at_level(logging.WARNING, logger="services.notification.dispatch"):
        ok = dispatch_mod.send_to_line_user_sync(FULL_ID, "__unknown_event__", {})

    assert ok is False
    assert FULL_ID not in caplog.text, f"log 含完整 LINE userId：{caplog.text!r}"
    assert FULL_ID[:8] in caplog.text, "應保留前 8 碼供 debug 關聯"


# ───────────────────────── dismissal_calls.py：學生姓名 ─────────────────────────


@pytest.fixture
def dismissal_db():
    engine = create_engine(
        "sqlite:///:memory:", connect_args={"check_same_thread": False}
    )
    session_factory = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = session_factory
    Base.metadata.create_all(engine)
    yield session_factory
    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def test_db_create_dismissal_call_log_omits_student_name(
    caplog, monkeypatch, dismissal_db
):
    """建立接送通知的 info log 不含學生姓名，改記 student_id。"""
    from models.database import Student, Classroom
    from api.dismissal_calls import _db_create_dismissal_call, DismissalCallCreate

    SECRET_NAME = "秘密學生姓名XQZ"
    sf = dismissal_db
    with sf() as s:
        cls = Classroom(name="海豚班", is_active=True, school_year=114, semester=1)
        s.add(cls)
        s.flush()
        stu = Student(
            student_id="S999",
            name=SECRET_NAME,
            classroom_id=cls.id,
            is_active=True,
        )
        s.add(stu)
        s.commit()
        sid, cid = stu.id, cls.id

    # 隔離 dispatch 副作用（context 內含 student_name，非本測試標的）
    monkeypatch.setattr("services.notification.dispatch.enqueue", lambda **k: None)

    body = DismissalCallCreate(student_id=sid, classroom_id=cid, note=None)
    with caplog.at_level(logging.INFO, logger="api.dismissal_calls"):
        out, _ = _db_create_dismissal_call(body, user_id=1)

    assert out["student_name"] == SECRET_NAME  # 回傳值仍含（給前端顯示）
    assert SECRET_NAME not in caplog.text, f"log 含學生姓名 PII：{caplog.text!r}"
    assert str(sid) in caplog.text, "log 應改記 student_id"
