"""api/parent_portal/_consent_gate.py — service_essential consent gate。

個資法 §8 告知義務守衛：家長每次存取資料端點前，確認其已對當期生效政策
簽署 service_essential 同意。

公開介面：
  require_current_consent(write: bool = False)
      FastAPI dependency factory，回傳 async check dependency。

      - flag off  → no-op，直接回 current_user（dark-launch 模式）
      - flag on   → 查 has_signed_current_policy
          - DB error + write=True  → 503（fail-closed：寫路徑不可 degrade）
          - DB error + write=False → 回 current_user（degraded fail-open，記 WARNING）
          - 未簽當期政策         → 403 + X-Consent-Required: service_essential header
          - 已簽當期政策         → 回 current_user

掛法（在端點 Depends 中取代 require_parent_role()）：
  current_user: dict = Depends(require_current_consent(write=True))   # POST/PUT/PATCH/DELETE
  current_user: dict = Depends(require_current_consent(write=False))  # GET

注意：gate 內部已依賴 require_parent_role()，呼叫端不需重複掛 require_parent_role。
"""

from __future__ import annotations

import logging

from fastapi import Depends, HTTPException

from config import get_settings
from models.base import session_scope
from services.consent.checker import has_signed_current_policy
from utils.auth import require_parent_role

logger = logging.getLogger(__name__)


def require_current_consent(write: bool = False):
    """依 write 旗標決定 DB error 的 fail 模式，回傳 async dependency。

    Args:
        write: True = POST/PUT/PATCH/DELETE（fail-closed on DB error）；
               False = GET（degraded fail-open on DB error）。
    """

    async def check(current_user: dict = Depends(require_parent_role())):
        if not get_settings().consent.enforcement_enabled:
            return current_user

        try:
            with session_scope() as session:
                ok = has_signed_current_policy(session, current_user["user_id"])
        except Exception as exc:
            if write:
                logger.error(
                    "consent gate DB error（fail-closed）: user_id=%s exc=%s",
                    current_user.get("user_id"),
                    exc,
                )
                raise HTTPException(status_code=503, detail="同意狀態檢查暫時不可用")
            logger.warning(
                "consent gate DB error（degraded fail-open）: user_id=%s exc=%s",
                current_user.get("user_id"),
                exc,
            )
            return current_user

        if not ok:
            raise HTTPException(
                status_code=403,
                detail="請先重新簽署當期隱私權政策",
                headers={"X-Consent-Required": "service_essential"},
            )
        return current_user

    return check
