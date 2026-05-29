"""P0d-3a backfill script idempotency 測試。

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §4.5
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.classroom import LIFECYCLE_ACTIVE
from models.database import Base, Student


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "backfill.sqlite"
    engine = create_engine(
        f"sqlite:///{db_path}", connect_args={"check_same_thread": False}
    )
    sf = sessionmaker(bind=engine)
    old_engine = base_module._engine
    old_sf = base_module._SessionFactory
    base_module._engine = engine
    base_module._SessionFactory = sf
    Base.metadata.create_all(engine)

    yield sf

    base_module._engine = old_engine
    base_module._SessionFactory = old_sf
    engine.dispose()


def _insert_plaintext_row(sf, *, allergy: str, medication: str):
    """繞過 ORM 加密路徑直接 raw INSERT plaintext。"""
    with sf() as s:
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, "
                "allergy, medication, is_active) VALUES "
                "(:sid, :name, :ls, :allergy, :medication, 1)"
            ),
            {
                "sid": f"S_{allergy[:5]}",
                "name": f"學生_{allergy[:5]}",
                "ls": LIFECYCLE_ACTIVE,
                "allergy": allergy,
                "medication": medication,
            },
        )
        s.commit()


def test_count_plaintext_rows_returns_correct_counts(db_session):
    from scripts.encrypt_medical_fields import _count_plaintext_rows

    sf = db_session
    _insert_plaintext_row(sf, allergy="花粉過敏", medication="氣管擴張劑")
    _insert_plaintext_row(sf, allergy="塵蟎", medication="抗組織胺")

    with sf() as session:
        counts = _count_plaintext_rows(session)
    assert counts["allergy"] == 2
    assert counts["medication"] == 2
    assert counts["special_needs"] == 0


def test_dry_run_does_not_modify_db(db_session):
    from scripts.encrypt_medical_fields import _backfill_field
    from utils.medical_encryption import is_encrypted

    sf = db_session
    _insert_plaintext_row(sf, allergy="花粉過敏", medication="氣管擴張劑")

    with sf() as session:
        stats = _backfill_field(session, "allergy", dry_run=True)

    assert stats["checked"] == 1
    assert stats["encrypted_now"] == 1

    # DB 應仍是 plaintext
    with sf() as session:
        row = session.execute(text("SELECT allergy FROM students LIMIT 1")).scalar()
        assert row == "花粉過敏"
        assert not is_encrypted(row)


def test_execute_encrypts_plaintext_rows(db_session):
    from scripts.encrypt_medical_fields import _backfill_field
    from utils.medical_encryption import decrypt_medical, is_encrypted

    sf = db_session
    _insert_plaintext_row(sf, allergy="花粉過敏", medication="氣管擴張劑")

    with sf() as session:
        stats = _backfill_field(session, "allergy", dry_run=False)

    assert stats["encrypted_now"] == 1

    # DB 應變密文，ORM 可解回
    with sf() as session:
        raw = session.execute(text("SELECT allergy FROM students")).scalar()
        assert raw != "花粉過敏"
        assert is_encrypted(raw)
        # ORM 透明解密
        student = session.query(Student).first()
        assert student.allergy == "花粉過敏"


def test_idempotent_rerun_skips_encrypted(db_session):
    """重跑 backfill 不重複加密既已加密的 row（idempotent）。"""
    from scripts.encrypt_medical_fields import _backfill_field

    sf = db_session
    _insert_plaintext_row(sf, allergy="花粉過敏", medication="氣管擴張劑")

    # 第一輪：plaintext → encrypted
    with sf() as session:
        stats1 = _backfill_field(session, "allergy", dry_run=False)
    assert stats1["encrypted_now"] == 1

    # 第二輪：已加密 → skip
    with sf() as session:
        stats2 = _backfill_field(session, "allergy", dry_run=False)
    assert stats2["encrypted_now"] == 0
    assert stats2["encrypted_skipped"] == 1


def test_handles_null_and_empty_string(db_session):
    """NULL / empty string 不處理（filter 排除）。"""
    from scripts.encrypt_medical_fields import _backfill_field

    sf = db_session
    with sf() as s:
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, "
                "is_active) VALUES ('S_null', 'NullStudent', 'active', 1)"
            )
        )
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, "
                "allergy, is_active) VALUES ('S_empty', 'EmptyStudent', "
                "'active', '', 1)"
            )
        )
        s.commit()

    with sf() as session:
        stats = _backfill_field(session, "allergy", dry_run=False)

    # 兩列都不在 plaintext 範圍（NULL filter + empty filter）
    assert stats["checked"] == 0


def test_mixed_plaintext_and_encrypted_rows(db_session):
    """既有混合 plaintext + 已加密 rows，backfill 只加密 plaintext。"""
    from scripts.encrypt_medical_fields import _backfill_field
    from utils.medical_encryption import encrypt_medical, is_encrypted

    sf = db_session
    _insert_plaintext_row(sf, allergy="花粉過敏", medication="氣管擴張劑")
    # 第二列直接寫密文（模擬之前 ORM 已加密的 row）
    with sf() as s:
        ct = encrypt_medical("已加密塵蟎")
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, "
                "allergy, is_active) VALUES "
                "(:sid, :name, :ls, :allergy, 1)"
            ),
            {
                "sid": "S_already",
                "name": "已加密學生",
                "ls": LIFECYCLE_ACTIVE,
                "allergy": ct,
            },
        )
        s.commit()

    with sf() as session:
        stats = _backfill_field(session, "allergy", dry_run=False)

    assert stats["checked"] == 2
    assert stats["encrypted_now"] == 1  # 只加密 plaintext 那列
    assert stats["encrypted_skipped"] == 1  # 已加密列被 skip

    # 兩列都應可正確 ORM 解密
    with sf() as session:
        students = session.query(Student).order_by(Student.student_id).all()
        allergies = {s.allergy for s in students}
        assert "花粉過敏" in allergies
        assert "已加密塵蟎" in allergies
