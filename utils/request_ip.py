"""utils/request_ip.py — 反向代理感知的 client IP 解析。

幼稚園系統部署在 LB / nginx / cloudflare 後面時，`request.client.host`
會是 proxy 的內網 IP，不再代表真實客戶端。本 helper 統一處理：

1. 優先讀 `X-Forwarded-For`（chain 最右一筆 = LB 觀察到的客戶端）
2. 若該標頭也屬於 trusted proxy（同時部署兩層 LB），逐個剝離
3. 全部失敗則 fallback 到 `request.client.host`

Why: 整站 rate_limit、audit log、login 失敗記錄都讀 `request.client.host`。
若不修，部署在 LB 後 → 單一攻擊者 5 分鐘 20 次失敗 → 全站 HTTP 429 DoS；
audit log 全部記錄成 LB 內網 IP，事後無法追蹤。

trusted proxy 透過環境變數 `TRUSTED_PROXY_IPS` 設定（逗號分隔 IP / CIDR）。
未設定時，預設只信任 RFC1918 內網（10/8、172.16/12、192.168/16、127/8），
給單層 LB 部署的常見情境 sane default。
"""

import ipaddress
import logging
from typing import Optional

from fastapi import Request

from config import settings

logger = logging.getLogger(__name__)


_DEFAULT_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)


def _parse_trusted_proxies() -> tuple:
    """讀環境變數 TRUSTED_PROXY_IPS，回傳 ip_network tuple。失敗時 fallback RFC1918。"""
    raw = (settings.network.trusted_proxy_ips or "").strip()
    if not raw:
        return _DEFAULT_PRIVATE_NETWORKS
    nets = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            logger.warning("TRUSTED_PROXY_IPS 包含無效項目：%r，已忽略", token)
    return tuple(nets) if nets else _DEFAULT_PRIVATE_NETWORKS


def _is_trusted(ip_str: str, trusted: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def get_client_ip(request: Request) -> Optional[str]:
    """回傳 best-effort 客戶端 IP；無法判定時回傳 None（不要回傳 'unknown' 字串）。

    取值順序：
    1. `X-Forwarded-For` 由右至左剝除 trusted proxy → 第一個非信任 IP
    2. `X-Real-IP`（nginx `proxy_set_header X-Real-IP $remote_addr` 風格）
    3. `request.client.host`

    呼叫端若需要字串 fallback，自行 `or 'unknown'`。
    """
    trusted = _parse_trusted_proxies()

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        chain = [token.strip() for token in xff.split(",") if token.strip()]
        for candidate in reversed(chain):
            if not _is_trusted(candidate, trusted):
                return candidate
        if chain:
            return chain[0]

    xri = (request.headers.get("x-real-ip") or "").strip()
    if xri:
        return xri

    if request.client and request.client.host:
        return request.client.host

    return None
