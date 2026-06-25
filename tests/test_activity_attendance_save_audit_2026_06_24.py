"""tests/test_activity_attendance_save_audit_2026_06_24.py

才藝點名「儲存出席紀錄」顯式稽核覆蓋（2026-06-24 code review finding）。

問題：AuditMiddleware 的 ENTITY_PATTERNS 用 re.match 比對，pattern 為
`/api/activity/sessions`，對不上實際路由 `/api/activity/attendance/sessions/
{id}/records`（admin）與 `/api/portal/activity/attendance/sessions/{id}/records`
（教師 portal）→ _parse_entity_type 回 None → dispatch 短路，整批點名寫入
**不留任何 AuditLog**。同檔其他端點（建立/刪除/匯出）皆已顯式 write_explicit_audit，
唯獨真正改變出席狀態（直接影響退費比例 T_served 與出席統計）的 PUT records 漏掉。

影響：誰把學生標成出席/缺席事後無法從 audit_logs 追溯。

修正後：admin 與 portal 兩個 batch attendance 端點均落 AuditLog
（action=UPDATE, entity_type=activity_session, entity_id=session_id），
與 delete/export 對齊。

沿用 test_activity_attendance_batch_race.py 的整合測試慣例（TestClient +
monkeypatch base_module._SessionFactory；client fixture 同時掛 admin 與 portal
router，_setup_scene 建好課程/有效報名/場次並 link employee 供 portal 授權）。
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from models.audit import AuditLog
from tests.test_activity_attendance_batch_race import (  # noqa: F401  (client 為 fixture)
    _setup_scene,
    client,
)
from tests.test_activity_pos import _login


def _session_audits(sf, *, action=None):
    with sf() as s:
        q = s.query(AuditLog).filter(AuditLog.entity_type == "activity_session")
        if action is not None:
            q = q.filter(AuditLog.action == action)
        return q.all()


def test_admin_batch_update_attendance_writes_audit(client):
    """管理端 PUT /api/activity/attendance/sessions/{id}/records 須留稽核。"""
    c, sf = client
    _, session_id, reg_id = _setup_scene(sf)

    _login(c)
    resp = c.put(
        f"/api/activity/attendance/sessions/{session_id}/records",
        json={
            "records": [{"registration_id": reg_id, "is_present": True, "notes": "到"}]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1, resp.json()

    audits = _session_audits(sf, action="UPDATE")
    assert any(a.entity_id == str(session_id) for a in audits), [
        (a.action, a.entity_id, a.summary) for a in _session_audits(sf)
    ]


def test_portal_batch_update_attendance_writes_audit(client):
    """教師 portal PUT /api/portal/activity/attendance/sessions/{id}/records 須留稽核。"""
    c, sf = client
    _, session_id, reg_id = _setup_scene(sf)

    _login(c)
    resp = c.put(
        f"/api/portal/activity/attendance/sessions/{session_id}/records",
        json={
            "records": [{"registration_id": reg_id, "is_present": False, "notes": "缺"}]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["updated"] == 1, resp.json()

    audits = _session_audits(sf, action="UPDATE")
    assert any(a.entity_id == str(session_id) for a in audits), [
        (a.action, a.entity_id, a.summary) for a in _session_audits(sf)
    ]
