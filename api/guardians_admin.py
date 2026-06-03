"""api/parent_portal/binding_admin.py — 行政端：簽發家長綁定碼

由 GUARDIANS_WRITE 權限的行政人員為特定 Guardian 簽發一次性綁定碼。
明碼僅回傳此次 API call 一次（行政再線下交給家長），DB 只存 sha256 hash。

稽核：呼叫成功後 logger.warning + 寫入 AuditLog（敏感操作）。
"""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta
from utils.taipei_time import now_taipei_naive

from fastapi import APIRouter, Depends, HTTPException, Request

from models.database import (
    AuditLog,
    Guardian,
    GuardianBindingCode,
    ParentDeviceSetupCode,
    ParentRefreshToken,
    get_session,
)
from utils.audit import _extract_impersonation_from_header
from utils.auth import require_staff_permission
from utils.permissions import Permission
from utils.portfolio_access import assert_student_access
from utils.request_ip import get_client_ip

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/guardians", tags=["parent-bind-admin"])


_CODE_TTL_HOURS = 24
# S4: 8 → 12 位英數，熵度從 ~40 bits 提高到 ~60 bits（32^12）。
# 搭 sha256 + 一次性 + IP rate limit + per-guardian active cap 後遠超暴力可行範圍。
_CODE_LENGTH = 12
_CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # 去掉 I/O/0/1 防誤讀
# S4: 單一 guardian 同時 active 上限；超過時拒簽避免「行政員批量先發、攻擊者
# 暴力試所有 active code 撞 hash」的情境（即便 sha256 已 collision-resistant，
# 仍降低成功通過 atomic UPDATE 的攻擊面）。
_MAX_ACTIVE_CODES_PER_GUARDIAN = 3


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

        # F-025 班級 scope 守衛：以 guardian 所屬學生判定，class-scoped 角色不可
        # 跨班為他班孩童簽發家長綁定碼（account-linkage 越權）。
        assert_student_access(session, current_user, guardian.student_id)

        # S4 per-guardian active cap：避免單一 guardian 累積過多 unused active code。
        now = now_taipei_naive()
        active_count = (
            session.query(GuardianBindingCode)
            .filter(
                GuardianBindingCode.guardian_id == guardian.id,
                GuardianBindingCode.used_at.is_(None),
                GuardianBindingCode.expires_at > now,
            )
            .count()
        )
        if active_count >= _MAX_ACTIVE_CODES_PER_GUARDIAN:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此監護人已有 {active_count} 個未使用的綁定碼，"
                    f"請先讓家長使用既有碼或等失效後再簽發新碼"
                ),
            )

        plain_code = _generate_plain_code()
        code_hash = _hash_code(plain_code)
        expires_at = now_taipei_naive() + timedelta(hours=_CODE_TTL_HOURS)

        binding = GuardianBindingCode(
            guardian_id=guardian.id,
            code_hash=code_hash,
            expires_at=expires_at,
            created_by=current_user["user_id"],
        )
        session.add(binding)

        _imp_by, _imp_name = _extract_impersonation_from_header(request)
        ip = get_client_ip(request)
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="CREATE",
                entity_type="guardian_binding",
                entity_id=str(guardian.id),
                summary="簽發家長綁定碼",
                ip_address=ip,
                created_at=now_taipei_naive(),
                impersonated_by=_imp_by,
                impersonated_by_name=_imp_name,
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


@router.post("/{guardian_id}/device-setup-code")
def create_device_setup_code(
    guardian_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """為指定 Guardian 簽發無 LINE 裝置登入碼（明碼僅此次回傳）。"""
    session = get_session()
    try:
        guardian = (
            session.query(Guardian)
            .filter(Guardian.id == guardian_id, Guardian.deleted_at.is_(None))
            .first()
        )
        if guardian is None:
            raise HTTPException(status_code=404, detail="找不到監護人")

        # F-025 班級 scope 守衛：class-scoped 角色不可跨班簽發裝置登入碼。
        assert_student_access(session, current_user, guardian.student_id)

        now = now_taipei_naive()
        active_count = (
            session.query(ParentDeviceSetupCode)
            .filter(
                ParentDeviceSetupCode.guardian_id == guardian.id,
                ParentDeviceSetupCode.used_at.is_(None),
                ParentDeviceSetupCode.expires_at > now,
            )
            .count()
        )
        if active_count >= _MAX_ACTIVE_CODES_PER_GUARDIAN:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"此監護人已有 {active_count} 個未使用的裝置登入碼，"
                    f"請先讓家長使用既有碼或等失效後再簽發"
                ),
            )

        plain_code = _generate_plain_code()
        code_hash = _hash_code(plain_code)
        expires_at = now_taipei_naive() + timedelta(hours=_CODE_TTL_HOURS)

        session.add(
            ParentDeviceSetupCode(
                guardian_id=guardian.id,
                code_hash=code_hash,
                expires_at=expires_at,
                created_by=current_user["user_id"],
            )
        )
        _imp_by, _imp_name = _extract_impersonation_from_header(request)
        ip = get_client_ip(request)
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="CREATE",
                entity_type="parent_device_setup",
                entity_id=str(guardian.id),
                summary="簽發無 LINE 裝置登入碼",
                ip_address=ip,
                created_at=now_taipei_naive(),
                impersonated_by=_imp_by,
                impersonated_by_name=_imp_name,
            )
        )
        session.commit()
        logger.warning(
            "[device-setup-code] guardian_id=%s created_by=%s",
            guardian.id,
            current_user["user_id"],
        )
        return {"code": plain_code, "expires_at": expires_at.isoformat()}
    finally:
        session.close()


@router.post("/{guardian_id}/revoke-devices")
def revoke_guardian_devices(
    guardian_id: int,
    request: Request,
    current_user: dict = Depends(require_staff_permission(Permission.GUARDIANS_WRITE)),
):
    """撤銷此 Guardian 對應家長 User 的所有未撤銷裝置（遺失/被盜裝置）。

    撤銷後該家長所有裝置下次 /refresh 即 401，需重新以新設定碼設定。
    （含 LINE 裝置一併撤銷——「全撤」為安全動作，over-revoke 安全。）
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

        # F-025 班級 scope 守衛：class-scoped 角色不可跨班撤銷他班家長裝置。
        assert_student_access(session, current_user, guardian.student_id)

        if guardian.user_id is None:
            return {"revoked": 0}

        n = (
            session.query(ParentRefreshToken)
            .filter(
                ParentRefreshToken.user_id == guardian.user_id,
                ParentRefreshToken.revoked_at.is_(None),
            )
            .update({"revoked_at": now_taipei_naive()}, synchronize_session=False)
        )
        ip = get_client_ip(request)
        session.add(
            AuditLog(
                user_id=current_user["user_id"],
                username=current_user.get("name") or current_user.get("username") or "",
                action="UPDATE",
                entity_type="parent_device_setup",
                entity_id=str(guardian.id),
                summary=f"撤銷家長裝置（{int(n or 0)} 個 session）",
                ip_address=ip,
                created_at=now_taipei_naive(),
            )
        )
        session.commit()
        logger.warning(
            "[revoke-devices] guardian_id=%s user_id=%s revoked=%s",
            guardian.id,
            guardian.user_id,
            n,
        )
        return {"revoked": int(n or 0)}
    finally:
        session.close()
