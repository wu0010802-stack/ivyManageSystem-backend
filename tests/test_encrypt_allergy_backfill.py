"""RA-MED-10 StudentAllergy 加密 backfill script idempotency 測試。

對齊 tests/test_encrypt_medical_backfill.py 結構。
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
from models.database import Base
from models.portfolio import StudentAllergy


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "allergy-backfill.sqlite"
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


def _insert_plaintext_allergy(sf, *, allergen: str, symptom: str, note: str):
    """繞過 ORM 加密路徑直接 raw INSERT plaintext。"""
    with sf() as s:
        # 先建 student（FK）
        s.execute(
            text(
                "INSERT INTO students (student_id, name, lifecycle_status, is_active) "
                "VALUES (:sid, :name, :ls, 1)"
            ),
            {"sid": f"S_{allergen[:4]}", "name": "生", "ls": LIFECYCLE_ACTIVE},
        )
        stu_id = s.execute(
            text("SELECT id FROM students ORDER BY id DESC LIMIT 1")
        ).scalar()
        s.execute(
            text(
                "INSERT INTO student_allergies "
                "(student_id, allergen, severity, reaction_symptom, first_aid_note, "
                "active, created_at, updated_at) VALUES "
                "(:sid, :al, 'severe', :sy, :no, 1, '2026-01-01', '2026-01-01')"
            ),
            {"sid": stu_id, "al": allergen, "sy": symptom, "no": note},
        )
        s.commit()


def test_count_plaintext_rows(db_session):
    from scripts.encrypt_student_allergies import _count_plaintext_rows

    sf = db_session
    _insert_plaintext_allergy(sf, allergen="花生", symptom="紅疹", note="抗組織胺")
    _insert_plaintext_allergy(sf, allergen="塵蟎", symptom="鼻塞", note="鼻噴劑")

    with sf() as session:
        counts = _count_plaintext_rows(session)
    assert counts["allergen"] == 2
    assert counts["reaction_symptom"] == 2
    assert counts["first_aid_note"] == 2


def test_execute_encrypts_and_orm_decrypts(db_session):
    from scripts.encrypt_student_allergies import _backfill_field
    from utils.medical_encryption import is_encrypted

    sf = db_session
    _insert_plaintext_allergy(sf, allergen="花生", symptom="紅疹", note="抗組織胺")

    with sf() as session:
        stats = _backfill_field(session, "allergen", dry_run=False)
    assert stats["encrypted_now"] == 1

    with sf() as session:
        raw = session.execute(text("SELECT allergen FROM student_allergies")).scalar()
        assert raw != "花生"
        assert is_encrypted(raw)
        # ORM 透明解密
        a = session.query(StudentAllergy).first()
        assert a.allergen == "花生"


def test_idempotent_rerun_skips_encrypted(db_session):
    from scripts.encrypt_student_allergies import _backfill_field

    sf = db_session
    _insert_plaintext_allergy(sf, allergen="花生", symptom="紅疹", note="抗組織胺")

    with sf() as session:
        stats1 = _backfill_field(session, "allergen", dry_run=False)
    assert stats1["encrypted_now"] == 1

    with sf() as session:
        stats2 = _backfill_field(session, "allergen", dry_run=False)
    assert stats2["encrypted_now"] == 0
    assert stats2["encrypted_skipped"] == 1


def test_dry_run_does_not_modify(db_session):
    from scripts.encrypt_student_allergies import _backfill_field
    from utils.medical_encryption import is_encrypted

    sf = db_session
    _insert_plaintext_allergy(sf, allergen="花生", symptom="紅疹", note="抗組織胺")

    with sf() as session:
        stats = _backfill_field(session, "allergen", dry_run=True)
    assert stats["encrypted_now"] == 1

    with sf() as session:
        raw = session.execute(text("SELECT allergen FROM student_allergies")).scalar()
        assert raw == "花生"  # 未改
        assert not is_encrypted(raw)
