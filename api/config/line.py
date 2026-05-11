"""api/config/line.py — LINE 通知設定 (get/put/test)。

3 個 endpoint + 2 個 Pydantic schema。_line_service singleton 經
init_config_services 注入後在 __init__ module-level；本檔以 lazy
back-import (``from . import _line_service``) 取得當前值，符合
api/salary 套件相同 pattern。
"""

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from models.database import get_session, LineConfig
from utils.auth import require_staff_permission
from utils.errors import raise_safe_500
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter()


class LineConfigRead(BaseModel):
    is_enabled: bool
    target_id: Optional[str]
    has_token: bool  # 是否已設定 token（不返回原值）
    has_secret: bool  # 是否已設定 channel_secret


class LineConfigUpdate(BaseModel):
    is_enabled: Optional[bool] = None
    target_id: Optional[str] = None
    channel_access_token: Optional[str] = None  # 空字串 = 不更新
    channel_secret: Optional[str] = None  # 空字串 = 不更新


@router.get("/line", response_model=LineConfigRead)
def get_line_config(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """取得 LINE 通知設定（token 以 has_token 表示，不回傳原值）"""
    session = get_session()
    try:
        cfg = session.query(LineConfig).first()
        if not cfg:
            return LineConfigRead(
                is_enabled=False, target_id=None, has_token=False, has_secret=False
            )
        return LineConfigRead(
            is_enabled=cfg.is_enabled,
            target_id=cfg.target_id,
            has_token=bool(cfg.channel_access_token),
            has_secret=bool(getattr(cfg, "channel_secret", None)),
        )
    finally:
        session.close()


@router.put("/line")
def update_line_config(
    data: LineConfigUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """更新 LINE 通知設定，空字串 token 視為不更新"""
    from . import _line_service  # lazy back-import

    session = get_session()
    try:
        cfg = session.query(LineConfig).first()
        if not cfg:
            cfg = LineConfig()
            session.add(cfg)

        if data.is_enabled is not None:
            cfg.is_enabled = data.is_enabled
        if data.target_id is not None:
            cfg.target_id = data.target_id
        if data.channel_access_token:  # 空字串不更新
            cfg.channel_access_token = data.channel_access_token
        if data.channel_secret:  # 空字串不更新
            cfg.channel_secret = data.channel_secret

        session.commit()

        # 熱更新 LineService（若已注入）
        if _line_service is not None:
            channel_secret = getattr(cfg, "channel_secret", None)
            if cfg.is_enabled and cfg.channel_access_token and cfg.target_id:
                _line_service.configure(
                    cfg.channel_access_token, cfg.target_id, True, channel_secret
                )
            else:
                _line_service.configure("", "", False, channel_secret)

        logger.warning(
            "LINE 通知設定已更新，操作人：%s", current_user.get("username", "")
        )
        return {"message": "LINE 通知設定已更新"}
    except Exception as e:
        session.rollback()
        raise_safe_500(e)
    finally:
        session.close()


@router.post("/line/test")
def test_line_notify(
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """發送測試訊息，驗證 LINE 通知是否正常"""
    from . import _line_service  # lazy back-import

    if _line_service is None:
        raise HTTPException(status_code=503, detail="LINE 通知服務未初始化")
    ok = _line_service._push("【測試】LINE 通知連線正常")
    if not ok:
        raise HTTPException(
            status_code=422,
            detail="LINE 通知發送失敗，請確認 token 與 target_id 是否正確，且通知功能已啟用",
        )
    return {"message": "測試訊息已發送"}
