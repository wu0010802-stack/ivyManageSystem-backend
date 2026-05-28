"""HIBP (Have I Been Pwned) k-anonymity password check.

api.pwnedpasswords.com 提供 k-anonymity SHA-1 query：
- 將密碼 SHA-1 hash 取前 5 碼當 prefix
- GET https://api.pwnedpasswords.com/range/{prefix}
- 回傳：所有 SHA-1 開頭符合 prefix 的 hash 後 35 碼 + 出現次數
- 我們本機比對 hash 後 35 碼是否在 response 內

優點：HIBP 永不知道完整密碼或 hash；只看到 5-char prefix。

Fail-open: timeout / network error 視為「不在 HIBP DB」，讓 user 完成
密碼設定，避免因 HIBP API down 整站無法改密碼。
"""

from __future__ import annotations

import hashlib
import logging

import requests

logger = logging.getLogger(__name__)

HIBP_API_URL = "https://api.pwnedpasswords.com/range/{prefix}"
HIBP_TIMEOUT_SECONDS = 3.0


class PasswordPwnedError(Exception):
    """密碼在 HIBP DB 中（已洩漏）。"""

    def __init__(self, occurrences: int):
        self.occurrences = occurrences
        super().__init__(f"Password found in HIBP DB ({occurrences} occurrences)")


def assert_not_pwned(password: str) -> None:
    """檢查密碼是否在 HIBP DB；命中則 raise PasswordPwnedError。

    Fail-open：network/timeout error 視為 not-pwned 放行（log warning）。
    """
    sha1 = hashlib.sha1(password.encode("utf-8")).hexdigest().upper()
    prefix, suffix = sha1[:5], sha1[5:]

    try:
        resp = requests.get(
            HIBP_API_URL.format(prefix=prefix),
            timeout=HIBP_TIMEOUT_SECONDS,
            headers={"Add-Padding": "true"},
        )
        resp.raise_for_status()
    except (requests.RequestException, requests.Timeout) as e:
        logger.warning("HIBP unreachable, fail-open password set: %s", e)
        return

    for line in resp.text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 2:
            continue
        if parts[0].upper() == suffix:
            try:
                count = int(parts[1])
            except ValueError:
                count = 1
            raise PasswordPwnedError(occurrences=count)
