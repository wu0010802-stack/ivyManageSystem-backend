"""utils/medical_field_type.py — SQLAlchemy TypeDecorator for transparent medical encryption.

P0d Phase 1 提供基礎 TypeDecorator class；Phase 2 PR 才會實際套到 Student model column。

使用範例（Phase 2 PR 才會這樣寫）:
    from utils.medical_field_type import EncryptedText

    class Student(Base):
        allergy = Column(EncryptedText, nullable=True)
        medication = Column(EncryptedText, nullable=True)

Refs: docs/superpowers/specs/2026-05-28-medical-fields-encryption-design.md §3.2
"""

from __future__ import annotations

from sqlalchemy.types import Text, TypeDecorator

from utils.medical_encryption import decrypt_medical, encrypt_medical


class EncryptedText(TypeDecorator):
    """Transparent encryption for medical text fields.

    DB 層仍是 Text；ORM 層 process_bind_param/process_result_value 自動加解密。
    對既有 raw SQL 查詢無侵入，但會看到密文。
    """

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt_medical(value)

    def process_result_value(self, value, dialect):
        return decrypt_medical(value)
