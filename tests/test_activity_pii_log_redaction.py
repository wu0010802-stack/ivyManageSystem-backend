"""tests/test_activity_pii_log_redaction.py — 驗證 public.py log 不含幼兒姓名與家長電話 PII。

涵蓋：
- 成功報名（新報名 info log）不含學生姓名與家長電話
- silent-reject honeypot 路徑 warning log 不含 name/phone PII
- 前台更新報名 info log 不含學生姓名
- /public/inquiries silent-reject warning log 不含 name/phone PII
"""

import logging
import os
import sys
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from api.activity import router as activity_router
from api.activity.public import (
    _public_inquiry_limiter_instance,
    _public_register_limiter_instance,
)
from models.database import (
    ActivityCourse,
    ActivityRegistration,
    Base,
    Classroom,
)
from utils.academic import resolve_current_academic_term

# ─── fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "pii-redact.sqlite"
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

    # 清空 limiter 計數，避免跨測試污染
    _public_register_limiter_instance._timestamps.clear()
    _public_inquiry_limiter_instance._timestamps.clear()

    app = FastAPI()
    app.include_router(activity_router)

    with TestClient(app) as c:
        yield c, session_factory

    base_module._engine = old_engine
    base_module._SessionFactory = old_session_factory
    engine.dispose()


def _seed_term():
    return resolve_current_academic_term()


def _seed_basic(session, sy, sem):
    classroom = Classroom(name="大象班", is_active=True, school_year=sy, semester=sem)
    session.add(classroom)
    session.flush()
    session.add(
        ActivityCourse(
            name="圍棋",
            price=1200,
            school_year=sy,
            semester=sem,
            is_active=True,
        )
    )
    session.commit()
    return classroom


def _public_register_payload(
    *,
    name="王小明唯一X",
    birthday="2020-05-10",
    phone="0912345678",
    class_="大象班",
    course_name="圍棋",
    hp="",
    ts=None,
):
    payload = {
        "name": name,
        "birthday": birthday,
        "parent_phone": phone,
        "class": class_,
        "courses": [{"name": course_name, "price": "1"}],
        "supplies": [],
        "_hp": hp,
    }
    if ts is not None:
        payload["_ts"] = ts
    return payload


# ─── 成功報名 log 不含 PII ───────────────────────────────────────────────────


class TestRegisterSuccessLogNoPII:
    def test_new_register_info_log_no_student_name(self, client, caplog):
        """成功報名時 logger.info 不應包含學生姓名或家長電話。"""
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)

        PII_NAME = "王小明唯一X"
        PII_PHONE = "0912345678"

        with caplog.at_level(logging.INFO, logger="api.activity.public"):
            res = c.post(
                "/api/activity/public/register",
                json=_public_register_payload(name=PII_NAME, phone=PII_PHONE),
            )

        assert res.status_code == 201, res.text
        full_log = caplog.text
        assert (
            PII_NAME not in full_log
        ), f"log 含學生姓名 PII（{PII_NAME!r}）：{full_log!r}"
        assert (
            PII_PHONE not in full_log
        ), f"log 含家長電話 PII（{PII_PHONE!r}）：{full_log!r}"


# ─── silent-reject honeypot log 不含 PII ────────────────────────────────────


class TestSilentRejectLogNoPII:
    def test_honeypot_register_warning_no_name_phone(self, client, caplog):
        """honeypot 命中時的 warning log 不應含 name / phone 明文。"""
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            _seed_basic(s, sy, sem)

        PII_NAME = "王小明唯一X"
        PII_PHONE = "0912345678"

        # hp 非空 → should_silent_reject_bot() 回 True
        honeypot_payload = _public_register_payload(
            name=PII_NAME,
            phone=PII_PHONE,
            hp="bot填入的隱藏欄位",
        )

        with caplog.at_level(logging.WARNING, logger="api.activity.public"):
            res = c.post(
                "/api/activity/public/register",
                json=honeypot_payload,
            )

        # silent-reject 回偽裝成功，狀態仍 201
        assert res.status_code == 201, res.text

        full_log = caplog.text
        assert (
            PII_NAME not in full_log
        ), f"silent-reject log 含學生姓名 PII（{PII_NAME!r}）：{full_log!r}"
        assert (
            PII_PHONE not in full_log
        ), f"silent-reject log 含家長電話 PII（{PII_PHONE!r}）：{full_log!r}"

    def test_honeypot_inquiry_warning_no_name_phone(self, client, caplog):
        """inquiry honeypot 命中時的 warning log 不應含 name / phone 明文。"""
        c, _ = client

        PII_NAME = "王小明唯一X"
        PII_PHONE = "0912345678"

        honeypot_payload = {
            "name": PII_NAME,
            "phone": PII_PHONE,
            "question": "請問有什麼課程？",
            "_hp": "bot填入的隱藏欄位",
        }

        with caplog.at_level(logging.WARNING, logger="api.activity.public"):
            res = c.post(
                "/api/activity/public/inquiries",
                json=honeypot_payload,
            )

        # silent-reject 回偽裝成功，狀態仍 201
        assert res.status_code == 201, res.text

        full_log = caplog.text
        assert (
            PII_NAME not in full_log
        ), f"inquiry silent-reject log 含姓名 PII（{PII_NAME!r}）：{full_log!r}"
        assert (
            PII_PHONE not in full_log
        ), f"inquiry silent-reject log 含電話 PII（{PII_PHONE!r}）：{full_log!r}"


# ─── 前台更新報名 log 不含 PII ───────────────────────────────────────────────


class TestPublicUpdateLogNoPII:
    def test_update_info_log_no_student_name(self, client, caplog):
        """前台更新報名時 logger.info 不應包含學生姓名。

        直接在 DB 植入 reg，繞過 silent-success path；
        再以 /public/update 三欄驗証更新，斷言 log 不含姓名 PII。
        """
        c, sf = client
        sy, sem = _seed_term()
        with sf() as s:
            classroom = _seed_basic(s, sy, sem)
            course = (
                s.query(ActivityCourse)
                .filter_by(name="圍棋", school_year=sy, semester=sem)
                .first()
            )

        PII_NAME = "王小明唯一X"
        PII_PHONE = "0912345678"

        # 直接植入 reg（不走 register endpoint，避免 silent-success path）
        with sf() as s:
            reg = ActivityRegistration(
                student_name=PII_NAME,
                birthday="2020-05-10",
                class_name="大象班",
                parent_phone=PII_PHONE,
                school_year=sy,
                semester=sem,
                is_active=True,
                paid_amount=0,
                is_paid=False,
            )
            s.add(reg)
            s.flush()
            from models.database import RegistrationCourse

            s.add(
                RegistrationCourse(
                    registration_id=reg.id,
                    course_id=course.id,
                    status="enrolled",
                    price_snapshot=1200,
                )
            )
            s.commit()
            reg_id = reg.id

        update_payload = {
            "id": reg_id,
            "name": PII_NAME,
            "birthday": "2020-05-10",
            "class": "大象班",
            "parent_phone": PII_PHONE,
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
        }

        with caplog.at_level(logging.INFO, logger="api.activity.public"):
            r = c.post(
                "/api/activity/public/update",
                json=update_payload,
            )

        assert r.status_code == 200, r.text

        full_log = caplog.text
        assert (
            PII_NAME not in full_log
        ), f"update log 含學生姓名 PII（{PII_NAME!r}）：{full_log!r}"
