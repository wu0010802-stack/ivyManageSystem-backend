"""api/parent_portal/binding_admin.py — 行政端：簽發家長綁定碼

由 GUARDIANS_WRITE 權限的行政人員為特定 Guardian 簽發一次性綁定碼。
明碼僅回傳此次 API call 一次（行政再線下交給家長），DB 只存 sha256 hash。

稽核：呼叫成功後 logger.warning + 寫入 AuditLog（敏感操作）。
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request

from models.database import AuditLog, Guardian, GuardianBindingCode, get_session
from utils.auth import require_staff_permission
from utils.permissions import Permission

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guardians", tags=["parent-bind-admin"])


_CODE_TTL_HOURS = 24
_CODE_LENGTH = 8  # 8 位英數，~47 bits entropy；搭 sha256 + 一次性 + rate limit 已夠
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I/O/0/1 防誤讀


def _generate_plain_code() -> str:
    return "".join(secrets.choice(_CODE_ALPHABET) for _ in range(_CODE_LENGTH))


def _hash_code(plain: str) -> str:
    return hashlib.sha256(plain.encode("utf-8")).hexdigest()


@router.post("/{guardian_id}/binding-code")
def create_binding_code(
    guardian_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """為指定 Guardian 簽發一次性綁定碼。

    Returns:
        {
            "code": "<8 位明碼，僅此次回傳>",
            "expires_at": "<24h 後的 ISO 時間>"
        }
    """
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="找不到監護人")

        plain_code = _generate_plain_code()
        code_hash = _hash_code(plain_code)
        expires_at = datetime.now() + timedelta(hours=_CODE_TTL_HOURS)

        binding = GuardianBindingCode(
            guardian_id=guardian.id,
            code_hash=code_hash,
            expires_at=expires_at,
            created_by=current_user["user_id"],
        )
        session.add(binding)

        ip = request.client.host if request.client else None
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="CREATE",
                entity_type="guardian_binding",
                entity_id=str(guardian.id),
                summary="簽發家長綁定碼",
                ip_address=ip,
                created_at=datetime.now(),
            )
        )
        session.commit()

        logger.warning(
            "[binding-code] guardian_id=%s created_by=%s",
            guardian.id,
            current_user["user_id"],
        )
        return {
            "code": plain_code,
            "expires_at": expires_at.isoformat(),
        }
    finally:
        session.close()
