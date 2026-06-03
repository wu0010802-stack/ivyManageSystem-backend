"""回歸：DsrRequestAdminOut 必須容忍 user_id=None（RA-MED-9 SET NULL 後）。

dsr_requests.user_id 改 ON DELETE SET NULL 後，申請人被硬刪會留下 user_id=NULL 的列；
admin DSR queue 列表（api/dsr_admin.py 序列化 DsrRequestAdminOut）若 user_id 非 Optional，
碰到該列會 500——正好發生在 admin 稽核 DSR 時。純 Pydantic 驗證，不需 DB / FK 強制。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../.."))

from schemas.dsr import DsrRequestAdminOut


def test_dsr_admin_out_allows_null_user_id():
    out = DsrRequestAdminOut(
        id=1,
        user_id=None,
        request_type="delete",
        status="pending",
        submitted_at="2026-06-03T00:00:00",
    )
    assert out.user_id is None
