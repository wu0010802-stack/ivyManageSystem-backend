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

# Module-level memo：(raw_value, parsed_tuple)
# 以 raw string 為 key：env 改變時自動 miss（不永久快取），
# 相同 raw 時只解析一次 → warning 也只發一次，防 prod log 洗版。
_TRUSTED_PROXIES_CACHE: tuple[str, tuple] | None = None


def _parse_trusted_proxies() -> tuple:
    """讀環境變數 TRUSTED_PROXY_IPS，回傳 ip_network tuple。失敗時 fallback RFC1918。

    結果以 raw string 為 key 做 module 層 memo，確保同一 env 設定只解析一次、
    warning 只發一次，避免每次 request 都重算並洗版 log。
    """
    global _TRUSTED_PROXIES_CACHE

    raw = (settings.network.trusted_proxy_ips or "").strip()

    # Cache hit：raw 未變動，直接回傳上次解析結果（不重新 warn）
    if _TRUSTED_PROXIES_CACHE is not None and _TRUSTED_PROXIES_CACHE[0] == raw:
        return _TRUSTED_PROXIES_CACHE[1]

    # Cache miss：重新解析（env 首次設定或 env 變更）
    if not raw:
        result = _DEFAULT_PRIVATE_NETWORKS
        _TRUSTED_PROXIES_CACHE = (raw, result)
        return result

    nets = []
    invalid_tokens = []
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            nets.append(ipaddress.ip_network(token, strict=False))
        except ValueError:
            invalid_tokens.append(token)

    if nets:
        # 有合法 CIDR；若同時有無效項目，發一條警告（僅警告，不 fallback）
        if invalid_tokens:
            logger.warning(
                "TRUSTED_PROXY_IPS 含無效項目 %r（已忽略）；有效 CIDR：%r。"
                "prod 請確認 Zeabur edge 出口 CIDR 皆列入，否則 XFF 可能被偽造繞過 per-IP 限流。",
                invalid_tokens,
                [str(n) for n in nets],
            )
        result = tuple(nets)
    else:
        # 全部 token 無效（含字面 "*"）或 raw 空白後全部 token 為空，
        # fallback RFC1918 並發單一統一 warning（不再逐 token 警告，減少 log 洗版）
        logger.warning(
            "TRUSTED_PROXY_IPS=%r 解析後無有效 CIDR（無效項目：%r），"
            "rate-limit / audit IP fallback 成 RFC1918 預設信任。"
            "prod 請把 TRUSTED_PROXY_IPS 設為 Zeabur edge 出口 CIDR，"
            "否則 XFF 可能被偽造繞過 per-IP 限流。",
            raw,
            invalid_tokens,
        )
        result = _DEFAULT_PRIVATE_NETWORKS

    _TRUSTED_PROXIES_CACHE = (raw, result)
    return result


def _is_trusted(ip_str: str, trusted: tuple) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return any(ip in net for net in trusted)


def _has_explicit_trusted_proxies() -> bool:
    """raw env TRUSTED_PROXY_IPS 是否「明設」了至少一個合法 CIDR / IP。

    RA-HIGH-2：只有明設可信代理時，才該信任 X-Forwarded-For / X-Real-IP。
    未明設（空 / "*" / 全為無效 token）時回 False → get_client_ip 忽略轉發標頭，
    直接回直連 peer，避免攻擊者偽造標頭繞過 per-IP 限流 / 嫁禍他人。

    直接讀 raw 字串判斷（非比對 _parse_trusted_proxies 的回傳 tuple）：避免
    「明設的 CIDR 剛好等於 RFC1918 預設」被誤判為未明設。
    """
    raw = (settings.network.trusted_proxy_ips or "").strip()
    if not raw:
        return False
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            ipaddress.ip_network(token, strict=False)
            return True
        except ValueError:
            continue
    return False


def warn_if_trusted_proxies_unset() -> None:
    """啟動告警（P2-7，2026-06-23 全系統資安掃描）。

    未明設可信代理（空 / "*" / 全無效 token）時，get_client_ip 忽略轉發標頭直接回直連
    peer。部署在反向代理（如 Zeabur edge）後，所有外部請求的 peer 都是同一內網 NAT 出口
    IP → per-IP 限流 / audit IP 塌成單一共享桶（易被單點打爆成全站 429，暴力破解防護
    喪失 per-attacker 隔離）。

    _parse_trusted_proxies 的同類 fallback warning 因 get_client_ip 在未明設時短路
    return 而成死碼（永不觸發）；改由啟動時主動檢查，使「乾淨啟動 log ＝ 已設 edge CIDR」
    這條 runbook 驗證步驟真正成立。啟動僅呼叫一次，無洗版疑慮。
    """
    if _has_explicit_trusted_proxies():
        return
    raw = (settings.network.trusted_proxy_ips or "").strip()
    logger.warning(
        "TRUSTED_PROXY_IPS 未明設可信代理（目前值 %r）：反向代理（如 Zeabur edge）後，"
        "per-IP 限流 / audit IP 將以直連 peer（內網 NAT 出口）為 key，全體外部請求共用"
        "單一限流桶，易被單點打爆成全站 429，且暴力破解防護喪失 per-attacker 隔離。"
        "prod 請把 TRUSTED_PROXY_IPS 設為 edge 出口 CIDR（搭配 RATE_LIMIT_BACKEND=postgres）。",
        raw or "(空)",
    )


def get_client_ip(request: Request) -> Optional[str]:
    """回傳 best-effort 客戶端 IP；無法判定時回傳 None（不要回傳 'unknown' 字串）。

    取值順序：
    1. `X-Forwarded-For` 由右至左剝除 trusted proxy → 第一個非信任 IP
    2. `X-Real-IP`（nginx `proxy_set_header X-Real-IP $remote_addr` 風格）
    3. `request.client.host`

    RA-HIGH-2：只有「明設」可信代理（TRUSTED_PROXY_IPS 含合法 CIDR）時才信任
    X-Forwarded-For / X-Real-IP。未明設時（空 / "*" / 全無效）一律忽略轉發標頭，
    直接回直連 peer，避免攻擊者偽造標頭繞過 per-IP 限流 / 嫁禍他人。

    呼叫端若需要字串 fallback，自行 `or 'unknown'`。
    """
    # 未明設可信代理 → 不信任任何轉發標頭，直接回直連 peer。
    if not _has_explicit_trusted_proxies():
        if request.client and request.client.host:
            return request.client.host
        return None

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
    # X-Real-IP 只在直接連線方（peer）是 trusted proxy 時採信（nginx 設此 header）；
    # 否則為 client 可控、可偽造繞過 per-IP 限流 / 污染 audit IP，須忽略並 fall through。
    if xri and request.client and _is_trusted(request.client.host, trusted):
        return xri

    if request.client and request.client.host:
        return request.client.host

    return None
