"""Finding 7（2026-06-22）：海報替換的原子性。

upload_activity_poster 原本順序為「存新檔 → 刪舊檔 → commit DB」。若 commit
失敗，DB rollback 後 poster_url 仍指向「已被刪除」的舊檔，新檔則成孤兒——
等於一次失敗的替換永久弄壞現有海報。

修補後順序為「存新檔 → commit DB → commit 成功後才刪舊檔」：commit 失敗時舊檔
完好（DB 仍指向它），新檔變孤兒（可日後清理，是較輕的代價）。
"""

import asyncio
import io
import os
import sys

import pytest
from fastapi import HTTPException
from PIL import Image
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from api.activity import settings as settings_mod
from models.database import ActivityRegistrationSettings, Base

_OLD_KEY = "0123456789abcdef0123456789abcdef.png"
_OLD_URL = f"/api/activity/public/poster/{_OLD_KEY}"


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (2, 2), (10, 20, 30)).save(buf, "PNG")
    return buf.getvalue()


class _UploadStub:
    """最小 UploadFile 替身：async read(size) + filename。"""

    def __init__(self, data: bytes, filename: str):
        self.filename = filename
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


class _FakeBackend:
    def __init__(self):
        self.ops: list[tuple[str, str]] = []

    def save(self, module, key, data, content_type):
        self.ops.append(("save", key))

    def delete(self, module, key):
        self.ops.append(("delete", key))

    def public_url(self, module, key):
        return f"/api/activity/public/poster/{key}"

    @property
    def deleted(self):
        return [k for op, k in self.ops if op == "delete"]

    @property
    def saved(self):
        return [k for op, k in self.ops if op == "save"]


class _CommitFails:
    """委派給真實 session，但 commit() 一律拋例外。"""

    def __init__(self, session):
        object.__setattr__(self, "_s", session)

    def __getattr__(self, name):
        return getattr(self._s, name)

    def commit(self):
        raise RuntimeError("simulated DB commit failure")


@pytest.fixture
def poster_env(monkeypatch):
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Session = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    seed = Session()
    seed.add(ActivityRegistrationSettings(id=1, poster_url=_OLD_URL))
    seed.commit()
    seed.close()

    fake = _FakeBackend()
    monkeypatch.setattr("utils.storage.get_backend", lambda: fake)
    yield Session, fake
    engine.dispose()


def test_old_poster_not_deleted_when_commit_fails(poster_env, monkeypatch):
    Session, fake = poster_env
    monkeypatch.setattr(settings_mod, "get_session", lambda: _CommitFails(Session()))

    with pytest.raises(HTTPException) as ei:
        asyncio.run(
            settings_mod.upload_activity_poster(
                file=_UploadStub(_png_bytes(), "new.png"),
                current_user={"username": "admin"},
            )
        )
    assert ei.value.status_code == 500
    # commit 失敗 → 舊檔不可被刪（否則 DB rollback 後指向已刪檔）
    assert _OLD_KEY not in fake.deleted
    # 新檔有寫入（成為孤兒，可日後清理）
    assert fake.saved, "新檔仍應已寫入儲存後端"


def test_old_poster_deleted_after_successful_commit(poster_env, monkeypatch):
    Session, fake = poster_env
    monkeypatch.setattr(settings_mod, "get_session", lambda: Session())

    result = asyncio.run(
        settings_mod.upload_activity_poster(
            file=_UploadStub(_png_bytes(), "new.png"),
            current_user={"username": "admin"},
        )
    )
    assert result["poster_url"].endswith(".png")
    # 成功路徑：舊檔被刪、且刪除發生在存新檔之後
    assert _OLD_KEY in fake.deleted
    assert fake.ops[0][0] == "save"
    assert fake.ops.index(("delete", _OLD_KEY)) > 0
    # DB 已更新為新 URL
    s = Session()
    try:
        cfg = s.query(ActivityRegistrationSettings).first()
        assert cfg.poster_url == result["poster_url"]
        assert cfg.poster_url != _OLD_URL
    finally:
        s.close()
