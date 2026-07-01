"""園內 kiosk 端點的 IP 白名單守衛（fail-closed）。"""

import ipaddress
import logging

from fastapi import HTTPException, Request

from config import settings
from utils.request_ip import get_client_ip

logger = logging.getLogger(__name__)


def _ip_in_any(ip_str: str, cidrs: list[str]) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if ip in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def assert_kiosk_ip_allowed(request: Request) -> None:
    """打卡端點守衛：client IP 不在 ATTENDANCE_KIOSK_ALLOWED_IPS → 403。

    fail-closed：白名單未設定或空 → 一律 403（kiosk 功能停用）。
    """
    allowed = settings.network.attendance_kiosk_allowed_ips or []
    if not allowed:
        logger.warning(
            "kiosk 端點被拒：ATTENDANCE_KIOSK_ALLOWED_IPS 未設定（fail-closed）"
        )
        raise HTTPException(status_code=403, detail="打卡裝置未授權")
    client_ip = get_client_ip(request)
    if not client_ip or not _ip_in_any(client_ip, allowed):
        logger.warning("kiosk 端點被拒：client_ip=%s 不在白名單", client_ip)
        raise HTTPException(status_code=403, detail="此裝置不在允許的打卡網路範圍")
