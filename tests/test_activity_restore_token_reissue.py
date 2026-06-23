"""tests/test_activity_restore_token_reissue.py

Restore 後公開修改安全性降回 PII 驗證（code review #1，High）。

問題：reject_registration 會清掉 query_token_hash / query_token_issued_at；
restore_registration 把報名翻回 is_active=True 卻從不重發 token。於是
_parent_mutation_identity_ok 把 query_token_hash IS NULL 當「無 token 舊報名」，
退回姓名+生日+電話三欄驗證——一個本來是 token 時代（強驗證）的報名，被拒→復原
後永久降級成弱三欄驗證，重新打開資安 #5 已封的洞。

修正口徑（業主裁定）：restore 時重新產生 query_token_hash（不回明文）。公開破壞性
mutation 的三欄路徑即失效；家長改走登入家長端或由後台管理。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.database import ActivityCourse, ActivityRegistration
from utils.academic import resolve_current_academic_term

# 重用 capacity 測試的 fixture 與 helpers（同一報名審核工作流）
from tests.test_activity_restore_capacity import (  # noqa: F401
    restore_client,
    _add_admin,
    _login,
    _register,
)


def _seed_course(session, *, capacity=30):
    sy, sem = resolve_current_academic_term()
    _add_admin(session)
    course = ActivityCourse(
        name="圍棋",
        price=1200,
        capacity=capacity,
        allow_waitlist=True,
        school_year=sy,
        semester=sem,
        is_active=True,
    )
    session.add(course)
    session.commit()
    return course.id


def _register_reject_restore(client, sf):
    """public register → reject → restore，回 reg_id。"""
    with sf() as s:
        _seed_course(s)
    r = _register(client, name="王小明", birthday="2020-05-10", phone="0912345678")
    assert r.status_code == 201, r.text
    reg_id = r.json()["id"]

    _login(client)
    rj = client.post(
        f"/api/activity/registrations/{reg_id}/reject",
        json={"reason": "測試用拒絕原因"},
    )
    assert rj.status_code == 200, rj.text
    # reject 後 token 已清空（前置確認）
    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        assert reg.query_token_hash is None
        assert reg.query_token_issued_at is None

    res = client.post(f"/api/activity/registrations/{reg_id}/restore")
    assert res.status_code == 200, res.text
    return reg_id


def test_restore_reissues_query_token(restore_client):
    """restore 後 query_token_hash / issued_at 須重新產生（非 None），
    讓報名回到 token 時代強驗證、不落入三欄 legacy 路徑。"""
    client, sf = restore_client
    reg_id = _register_reject_restore(client, sf)

    with sf() as s:
        reg = s.query(ActivityRegistration).filter_by(id=reg_id).one()
        assert (
            reg.query_token_hash is not None
        ), "restore 後應重發 query_token_hash（否則公開 mutation 降回三欄弱驗證）"
        assert (
            reg.query_token_issued_at is not None
        ), "restore 後應一併寫入 query_token_issued_at"


def test_public_update_after_restore_rejects_legacy_three_field(restore_client):
    """攻擊路徑：restore 後僅憑姓名+生日+電話（無 token）打 /public/update 應被
    拒（403）——三欄不再足以授權破壞性修改。"""
    client, sf = restore_client
    reg_id = _register_reject_restore(client, sf)

    res = client.post(
        "/api/activity/public/update",
        json={
            "id": reg_id,
            "name": "王小明",
            "birthday": "2020-05-10",
            "class": "大象班",
            "parent_phone": "0912345678",
            "courses": [{"name": "圍棋", "price": "1"}],
            "supplies": [],
            # 刻意不帶 query_token：模擬只知三欄 PII 的陌生人
        },
    )
    assert res.status_code == 403, (
        "restore 後僅三欄（無 token）不應能修改報名；"
        f"實得 {res.status_code}：{res.text}"
    )
