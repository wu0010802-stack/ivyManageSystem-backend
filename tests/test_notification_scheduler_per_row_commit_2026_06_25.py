"""崩潰防護（bug-hunt 2026-06-25，P2）：LINE retry / pending_uploads 排程應逐筆 commit。

問題：tick_line_retry / tick_pending_uploads 原本以單一交易撈 ≤N 列（FOR UPDATE），
在 for 迴圈內逐列做外部送出（LINE HTTP / Supabase upload），成功只在記憶體標記，
整批跑完才「單次 commit」。進程在迴圈中途崩潰 / 被部署重啟（Zeabur push 即重啟）時，
已成功送出 N 筆的標記隨 rollback 丟失 → 下個 tick 全數重送（家長收重複 LINE 通知 /
Supabase 重複上傳）。skip_locked 只防多 worker 雙發，不防此 crash-window。

修法：逐筆短交易（re-lock + 處理 + commit），每筆成功狀態即時落地；鎖後 re-check
合格性確保多 worker 不雙發、不處理他 worker 已推進的 row。

本測試模擬「處理完第 1 列、第 2 列送出時進程崩潰（BaseException 逸出，非 Exception）」，
斷言第 1 列的成功標記已落地（不會下個 tick 重送）。修前單次 commit → 第 1 列標記隨
rollback 丟失，斷言失敗（RED）。
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import pytest


def _make_log(user_id, **over):
    from models.database import NotificationLog

    base = dict(
        recipient_user_id=user_id,
        event_type="parent.fee_due",
        title="t",
        body="b",
        payload_json={
            "student_name": "X",
            "item_name": "I",
            "amount": 100,
            "due_date": "2026-06-01",
        },
        channels_attempted=["line"],
        channels_succeeded=[],
        channels_failed=[{"channel": "line", "error": "X"}],
        line_next_retry_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        line_retry_count=0,
        is_inbox_visible=False,
    )
    base.update(over)
    return NotificationLog(**base)


def test_line_retry_crash_midloop_persists_already_sent_row(
    test_db_session, monkeypatch
):
    from models.database import NotificationLog, User
    from services.notification.retry_scheduler import tick_line_retry

    user = User(
        username="pcrash",
        password_hash="x",
        line_user_id="U1",
        is_active=True,
        line_follow_confirmed_at=datetime.now(),
    )
    test_db_session.add(user)
    test_db_session.commit()

    row1 = _make_log(user.id)
    row2 = _make_log(user.id)
    test_db_session.add_all([row1, row2])
    test_db_session.commit()
    id1, id2 = row1.id, row2.id
    assert id1 < id2  # row1 先處理

    calls = {"n": 0}

    def _send(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            return None  # row1 送出成功
        raise KeyboardInterrupt("模擬 tick 中途進程崩潰")  # row2 送出時崩潰

    monkeypatch.setattr(
        "services.notification.retry_scheduler._get_line_adapter",
        lambda: MagicMock(send=_send),
    )

    with pytest.raises(KeyboardInterrupt):
        tick_line_retry()

    test_db_session.expire_all()
    r1 = test_db_session.query(NotificationLog).filter_by(id=id1).first()
    r2 = test_db_session.query(NotificationLog).filter_by(id=id2).first()
    # 修後：row1 成功標記已逐筆 commit 落地 → 下個 tick 不重送。
    assert r1.line_next_retry_at is None, "row1 成功標記應已落地（避免重送）"
    assert "line(retry)" in r1.channels_succeeded
    # row2 尚未處理完，仍保留待下個 tick retry。
    assert r2.line_next_retry_at is not None


def test_line_retry_normal_batch_all_succeed(test_db_session, monkeypatch):
    """非崩潰路徑：多列全部成功，逐筆 commit 後皆落地（不回歸既有行為）。"""
    from models.database import NotificationLog, User
    from services.notification.retry_scheduler import tick_line_retry

    user = User(
        username="pbatch",
        password_hash="x",
        line_user_id="U2",
        is_active=True,
        line_follow_confirmed_at=datetime.now(),
    )
    test_db_session.add(user)
    test_db_session.commit()
    rows = [_make_log(user.id) for _ in range(3)]
    test_db_session.add_all(rows)
    test_db_session.commit()
    ids = [r.id for r in rows]

    monkeypatch.setattr(
        "services.notification.retry_scheduler._get_line_adapter",
        lambda: MagicMock(send=MagicMock(return_value=None)),
    )

    result = tick_line_retry()
    assert result["attempted"] == 3
    assert result["succeeded"] == 3
    test_db_session.expire_all()
    for rid in ids:
        r = test_db_session.query(NotificationLog).filter_by(id=rid).first()
        assert r.line_next_retry_at is None
        assert "line(retry)" in r.channels_succeeded


def test_pending_uploads_crash_midloop_persists_already_uploaded_row(
    test_db_session, tmp_path, monkeypatch
):
    from models.pending_uploads import PendingUpload
    from services.notification.pending_uploads_scheduler import tick_pending_uploads

    now = datetime.now(timezone.utc)
    f1 = tmp_path / "a.bin"
    f1.write_bytes(b"AAA")
    f2 = tmp_path / "b.bin"
    f2.write_bytes(b"BBB")
    row1 = PendingUpload(
        module="activity_posters",
        key="x/a.png",
        content_type="image/png",
        local_path=str(f1),
        attempts=0,
        next_retry_at=now - timedelta(seconds=10),
    )
    row2 = PendingUpload(
        module="activity_posters",
        key="x/b.png",
        content_type="image/png",
        local_path=str(f2),
        attempts=0,
        next_retry_at=now - timedelta(seconds=10),
    )
    test_db_session.add_all([row1, row2])
    test_db_session.commit()
    id1, id2 = row1.id, row2.id
    assert id1 < id2

    up = {"n": 0}

    def _upload(*a, **k):
        up["n"] += 1
        if up["n"] == 1:
            return None  # row1 上傳成功
        raise KeyboardInterrupt("模擬 tick 中途進程崩潰")  # row2 上傳時崩潰

    fake_bucket = MagicMock()
    fake_bucket.upload.side_effect = _upload
    fake_client = MagicMock()
    fake_client.storage.from_.return_value = fake_bucket
    fake_backend = MagicMock()
    fake_backend.__class__.__name__ = "SupabaseStorage"
    fake_backend._client = fake_client
    monkeypatch.setattr("utils.storage.get_backend", lambda: fake_backend)

    with pytest.raises(KeyboardInterrupt):
        tick_pending_uploads(now_provider=lambda: now)

    test_db_session.expire_all()
    r1 = test_db_session.query(PendingUpload).filter_by(id=id1).first()
    r2 = test_db_session.query(PendingUpload).filter_by(id=id2).first()
    # 修後：row1 成功標記已逐筆 commit 落地 → 下個 tick 不重複上傳。
    assert r1.succeeded_at is not None, "row1 succeeded_at 應已落地（避免重複上傳）"
    # row2 尚未處理完，仍待重試。
    assert r2.succeeded_at is None
