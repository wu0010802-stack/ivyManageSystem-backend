"""dev DB 安全護欄。

`assert_dev_db` 確保 seedgen(尤其 --wipe)只對本機 dev 資料庫執行,
防止誤刪 prod/staging。任一條件不符即 raise `GuardError`。
"""

from __future__ import annotations

from urllib.parse import urlsplit


class GuardError(RuntimeError):
    """資料庫護欄不通過時拋出。"""


# 允許的本機 host 白名單。
_LOCAL_HOSTS = {"localhost", "127.0.0.1"}
# dev DB 名稱(path 去掉前導斜線後須完全相符)。
_DEV_DB_NAME = "ivymanagement"


def assert_dev_db(url: str, env: str, allow_non_dev: bool) -> None:
    """斷言 `url` 指向本機 dev DB,否則 raise `GuardError`。

    Args:
        url: SQLAlchemy/psycopg 連線字串。
        env: 應用環境(`settings.core.env`),production 一律拒絕。
        allow_non_dev: True 直接放行(對應 CLI `--i-know-not-dev`)。

    放行條件(allow_non_dev 為 False 時全部須成立):
        - hostname 屬於本機白名單(localhost / 127.0.0.1)
        - 資料庫名稱為 ivymanagement
        - 連線字串不含 sslmode query(遠端託管 DB 才需要)
        - env 不是 production
    """
    if allow_non_dev:
        return

    parts = urlsplit(url)
    hostname = parts.hostname
    db_name = parts.path.lstrip("/")

    if hostname not in _LOCAL_HOSTS:
        raise GuardError(f"拒絕:host {hostname!r} 非本機 dev DB(允許 {_LOCAL_HOSTS})。")
    if db_name != _DEV_DB_NAME:
        raise GuardError(f"拒絕:資料庫 {db_name!r} 非 {_DEV_DB_NAME!r}。")
    if "sslmode" in (parts.query or ""):
        raise GuardError("拒絕:連線字串含 sslmode,疑似遠端託管 DB。")
    if env == "production":
        raise GuardError("拒絕:env=production,seedgen 僅供 dev 使用。")
