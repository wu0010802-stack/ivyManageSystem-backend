"""[C41] LineConfig 憑證 EncryptedText 加密（at-rest）。

channel_access_token / channel_secret 原為明文 String 欄位，DB 外洩即直接暴露
LINE Messaging API 憑證（可冒名推播 / 偽造 webhook 簽名）。改用 EncryptedText
（ORM 透明 Fernet 加密；DB 底層仍 Text；legacy 明文 passthrough）。

對齊 models/portfolio.py StudentAllergy 的醫療欄位加密做法與
tests/test_student_allergy_encryption.py 的測試樣式。
"""

from __future__ import annotations

import os
import sys

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import models.base as base_module
from models.base import Base
from models.line_config import LineConfig


@pytest.fixture
def db_session(tmp_path):
    db_path = tmp_path / "line-config-enc.sqlite"
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


def test_line_creds_encrypted_at_rest_plaintext_via_orm(db_session):
    """寫 LineConfig → DB 原始 channel_access_token/channel_secret 為 Fernet 密文；
    ORM 讀回明文。target_id 維持明文。"""
    sf = db_session
    token = "A" * 180  # 模擬 LINE long-lived channel access token
    secret = "b" * 32  # 模擬 channel secret（hex）
    with sf() as s:
        cfg = LineConfig(
            channel_access_token=token,
            channel_secret=secret,
            target_id="Uabc123",
            is_enabled=True,
        )
        s.add(cfg)
        s.commit()
        cfg_id = cfg.id

    # DB 原始值（繞過 ORM）：兩個憑證欄應為 gAAAAA 開頭密文，非明文
    with sf() as s:
        row = s.execute(
            text(
                "SELECT channel_access_token, channel_secret, target_id "
                "FROM line_configs WHERE id = :id"
            ),
            {"id": cfg_id},
        ).one()
        raw_token, raw_secret, raw_target = row
        assert raw_token.startswith("gAAAAA"), raw_token
        assert raw_secret.startswith("gAAAAA"), raw_secret
        assert raw_token != token
        assert raw_secret != secret
        # target_id 非機密憑證，維持明文
        assert raw_target == "Uabc123"

    # ORM 透明解密
    with sf() as s:
        cfg = s.query(LineConfig).filter(LineConfig.id == cfg_id).first()
        assert cfg.channel_access_token == token
        assert cfg.channel_secret == secret
        assert cfg.target_id == "Uabc123"


def test_legacy_plaintext_creds_passthrough(db_session):
    """遷移窗口：既有明文憑證（未 backfill）ORM 讀取應原樣回（decrypt passthrough）。"""
    sf = db_session
    with sf() as s:
        # 繞過 ORM 直接寫明文（模擬加密前既有資料）
        s.execute(
            text(
                "INSERT INTO line_configs "
                "(channel_access_token, channel_secret, target_id, is_enabled) "
                "VALUES ('legacy-plain-token', 'legacy-secret', 'Uxyz', 1)"
            )
        )
        s.commit()

    with sf() as s:
        cfg = s.query(LineConfig).first()
        assert cfg.channel_access_token == "legacy-plain-token"
        assert cfg.channel_secret == "legacy-secret"
