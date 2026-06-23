"""tests/test_parent_activity_cache_after_commit_2026_06_23.py

Code review P2（2026-06-23）：家長端報名/候補確認在交易 commit 前先清 dashboard 快取。

家長端 handler 只 flush()，真正 commit 由 parent DB dependency 的 `with session.begin():`
（models/parent_db.build_parent_session_for_user）在 handler 結束時負責——handler 不可
自行 commit（RLS：SET LOCAL app.current_user_id 會在 commit 時失效）。但原碼在 flush()
後就 inline 呼叫 `invalidate_dashboard_caches`（report_cache_service 用獨立 session 立即
commit 刪快取）。於是在「快取已刪、parent 交易尚未 commit」的窗口內，並發 dashboard
讀取會以 pre-commit stale 資料重建快取並續存 TTL(1800s) → 最長 30 分鐘陳舊。

公開/後台端（api/activity/public.py 等）都是 `session.commit()` 之後才 invalidate；
家長端順序顛倒。修法：把家長端 cache invalidation 延後到 parent 交易 commit 之後
（post-commit callback queue）。

驗證策略（並發讀者視角）：spy `invalidate_dashboard_caches`，在被呼叫的當下另開一條
session（= 並發讀者，只看得到已 commit 資料）觀察本次寫入是否已可見。
- 修前：invalidate 在 handler 內 pre-commit 觸發 → 並發讀者看不到本次寫入（stale）。
- 修後：invalidate 在 commit 後觸發 → 並發讀者看得到本次寫入。

DB 隔離：SQLite + activity_client（monkeypatch base_module）。
"""

import os
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import (
    ActivityRegistration,
    RegistrationCourse,
)
from services.activity_service import activity_service

# 複用家長端 app fixture（SQLite + parent RLS override）與 helper。
from tests.test_parent_activity import (  # noqa: F401
    activity_client,
    _setup_family,
    _create_course,
    _parent_token,
)

# ── 機制單元測試：parent_db post-commit callback queue ────────────────────────


def test_post_commit_callbacks_run_after_commit_and_swallow_errors():
    """register_parent_post_commit 把 callback 延後到 run 才執行；最佳努力（吞錯）。"""
    from models.parent_db import (
        register_parent_post_commit,
        run_parent_post_commit_callbacks,
    )

    class _FakeSession:
        def __init__(self):
            self.info = {}

    s = _FakeSession()
    calls = []

    def _boom():
        raise RuntimeError("callback backend down")

    register_parent_post_commit(s, lambda: calls.append("a"))
    register_parent_post_commit(s, _boom)  # 中間一個拋錯，不可阻擋其餘
    register_parent_post_commit(s, lambda: calls.append("c"))

    # 註冊後尚未執行（延後到 commit 後）
    assert calls == []

    run_parent_post_commit_callbacks(s)
    # 非失敗的 callback 都跑了；錯誤被吞（best-effort，不可炸 request）
    assert calls == ["a", "c"]

    # queue 已清空：再跑一次不重複執行
    run_parent_post_commit_callbacks(s)
    assert calls == ["a", "c"]


# ── 共用 spy ──────────────────────────────────────────────────────────────────


def _spy_invalidate(monkeypatch, observer):
    """攔截 invalidate_dashboard_caches：呼叫當下跑 observer()（並發讀者視角）記錄
    本次寫入是否已可見。刻意「不」委派真實刪除——只驗證呼叫時機（commit 前/後），
    避免在 SQLite 下與 handler 仍持有的寫鎖衝突成 `database is locked`（該鎖衝突本身
    亦是本 bug 在 SQLite 的副作用之一）。實際刪除行為另有
    test_activity_dashboard_invalidate_on_enrollment 覆蓋。"""
    captured = {"called": False, "observed": None}

    def _spy(*args, **kwargs):
        captured["called"] = True
        captured["observed"] = observer()
        return 0

    monkeypatch.setattr(activity_service, "invalidate_dashboard_caches", _spy)
    return captured


# ── 路徑 1：家長 register ────────────────────────────────────────────────────


def test_parent_register_invalidates_cache_after_commit(activity_client, monkeypatch):
    """家長報名：清快取時本次報名必須已 committed（並發讀者看得到）。"""
    client, sf = activity_client
    with sf() as s:
        user, _, student, _ = _setup_family(s)
        course = _create_course(s, name="繪畫", capacity=5)
        s.commit()
        token = _parent_token(user)
        sid = student.id
        cid = course.id

    def _observer():
        # 並發讀者：另開 session 看本次報名是否已可見
        with sf() as fresh:
            return (
                fresh.query(ActivityRegistration)
                .filter(
                    ActivityRegistration.student_id == sid,
                    ActivityRegistration.is_active.is_(True),
                )
                .count()
            )

    captured = _spy_invalidate(monkeypatch, _observer)

    res = client.post(
        "/api/parent/activity/register",
        json={
            "student_id": sid,
            "school_year": 115,
            "semester": 1,
            "course_ids": [cid],
            "supply_ids": [],
        },
        cookies={"access_token": token},
    )
    assert res.status_code == 201, res.text
    assert captured["called"], "報名後應觸發 dashboard 快取失效"
    assert captured["observed"] == 1, (
        "清快取時本次報名尚未 committed（並發讀者看不到）→ 會以 stale 資料重建快取並續存 "
        f"30 分鐘。observed={captured['observed']}（期望 1）"
    )


# ── 路徑 2：家長 confirm-promotion ───────────────────────────────────────────


def test_parent_confirm_promotion_invalidates_cache_after_commit(
    activity_client, monkeypatch
):
    """家長候補轉正確認：清快取時轉正（enrolled）必須已 committed。"""
    client, sf = activity_client
    with sf() as s:
        user, _, student, _ = _setup_family(s)
        course = _create_course(s, name="繪畫", capacity=5)
        reg = ActivityRegistration(
            student_name=student.name,
            is_active=True,
            school_year=115,
            semester=1,
            student_id=student.id,
            parent_phone="0911",
            pending_review=False,
            match_status="manual",
        )
        s.add(reg)
        s.flush()
        rc = RegistrationCourse(
            registration_id=reg.id,
            course_id=course.id,
            status="promoted_pending",
            price_snapshot=course.price,
            promoted_at=datetime.now(),
            confirm_deadline=datetime.now() + timedelta(hours=24),
        )
        s.add(rc)
        s.commit()
        token = _parent_token(user)
        reg_id = reg.id
        course_id = course.id
        rc_id = rc.id

    def _observer():
        with sf() as fresh:
            return (
                fresh.query(RegistrationCourse.status)
                .filter(RegistrationCourse.id == rc_id)
                .scalar()
            )

    captured = _spy_invalidate(monkeypatch, _observer)

    res = client.post(
        f"/api/parent/activity/registrations/{reg_id}/confirm-promotion",
        json={"course_id": course_id},
        cookies={"access_token": token},
    )
    assert res.status_code == 200, res.text
    assert captured["called"], "候補轉正後應觸發 dashboard 快取失效"
    assert captured["observed"] == "enrolled", (
        "清快取時轉正尚未 committed（並發讀者看到舊 promoted_pending）→ 會以 stale 資料"
        f"重建快取。observed={captured['observed']!r}（期望 'enrolled'）"
    )
