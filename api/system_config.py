"""SystemConfig 通用 CRUD API。

主要 keys（本系統實際使用）：
- bank.payer_name      銀行轉帳名冊上的「公司戶名」
- bank.payer_account   公司付款銀行帳號（轉帳名冊頂端顯示）

未來可擴張其他系統級設定（不適合放 BonusConfig / AttendancePolicy 的雜項）。
"""

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from models.base import session_scope
from models.database import SystemConfig
from utils.auth import require_staff_permission
from utils.permissions import Permission

router = APIRouter(prefix="/api", tags=["system-config"])


# 已知 keys 預設值（GET 時若 key 不存在，回傳 default 供前端 prefill）
KNOWN_DEFAULTS: dict[str, dict] = {
    "bank.payer_name": {
        "value": "高雄市私立常春藤幼兒園",
        "type": "bank",
        "description": "公司戶名（銀行轉帳名冊頂端顯示）",
    },
    "bank.payer_account": {
        "value": "0727-940-008106",
        "type": "bank",
        "description": "公司付款銀行帳號",
    },
}


class SystemConfigOut(BaseModel):
    config_key: str
    config_value: str
    config_type: str = "general"
    description: Optional[str] = None
    is_default: bool = Field(False, description="True=DB 無此 key，目前顯示的是預設值")
    updated_at: Optional[datetime] = None


class SystemConfigUpdate(BaseModel):
    config_value: str = Field(..., min_length=1, max_length=1000)
    description: Optional[str] = Field(None, max_length=200)


def _to_out(obj: SystemConfig | None, key: str) -> SystemConfigOut:
    if obj is None:
        default = KNOWN_DEFAULTS.get(key, {})
        return SystemConfigOut(
            config_key=key,
            config_value=default.get("value", ""),
            config_type=default.get("type", "general"),
            description=default.get("description"),
            is_default=True,
        )
    return SystemConfigOut(
        config_key=obj.config_key,
        config_value=obj.config_value,
        config_type=obj.config_type or "general",
        description=obj.description,
        is_default=False,
        updated_at=obj.updated_at,
    )


@router.get("/system-configs")
def list_configs(
    prefix: Optional[str] = Query(None, description="config_key 前綴篩選"),
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """列出所有 SystemConfig。若指定 prefix（例：bank），同時補上 KNOWN_DEFAULTS 中該前綴的未設定值。"""
    with session_scope() as session:
        q = session.query(SystemConfig)
        if prefix:
            q = q.filter(SystemConfig.config_key.like(f"{prefix}%"))
        existing = {c.config_key: c for c in q.all()}

        # 補上 KNOWN_DEFAULTS 中沒有 DB 記錄的（讓前端可看到所有可調項目）
        result_keys = set(existing.keys())
        for key in KNOWN_DEFAULTS:
            if prefix and not key.startswith(prefix):
                continue
            result_keys.add(key)

        items = []
        for key in sorted(result_keys):
            items.append(_to_out(existing.get(key), key).model_dump())
        return {"items": items}


@router.get("/system-configs/{config_key}")
def get_config(
    config_key: str,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_READ)),
):
    """讀取單筆設定；若 DB 無此 key 且為 KNOWN_DEFAULTS，回傳預設值（is_default=True）。"""
    with session_scope() as session:
        obj = (
            session.query(SystemConfig)
            .filter(SystemConfig.config_key == config_key)
            .first()
        )
        if obj is None and config_key not in KNOWN_DEFAULTS:
            raise HTTPException(status_code=404, detail="設定不存在")
        return _to_out(obj, config_key).model_dump()


@router.put("/system-configs/{config_key}")
def upsert_config(
    config_key: str,
    payload: SystemConfigUpdate,
    current_user: dict = Depends(require_staff_permission(Permission.SETTINGS_WRITE)),
):
    """新增或更新單筆設定（upsert）。"""
    with session_scope() as session:
        obj = (
            session.query(SystemConfig)
            .filter(SystemConfig.config_key == config_key)
            .first()
        )
        if obj is None:
            default = KNOWN_DEFAULTS.get(config_key, {})
            obj = SystemConfig(
                config_key=config_key,
                config_value=payload.config_value,
                config_type=default.get("type", "general"),
                description=payload.description or default.get("description"),
            )
            session.add(obj)
        else:
            obj.config_value = payload.config_value
            if payload.description is not None:
                obj.description = payload.description
            obj.updated_at = datetime.now()  # noqa: DTZ005
        session.flush()
        result = _to_out(obj, config_key).model_dump()
        session.commit()
        return result
