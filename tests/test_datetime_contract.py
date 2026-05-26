"""utils.taipei_time helper 的 TZ-agnostic 行為斷言。

Phase 0 已將 prod container 設 TZ=Asia/Taipei，但本檔測試 helper 本身
不依賴 container TZ — TZ=UTC matrix run 也必須全綠。
"""

import tomllib
from datetime import datetime

from utils.taipei_time import (
    TAIPEI_TZ,
    now_taipei_aware,
    now_taipei_naive,
)


def test_now_taipei_naive_no_tzinfo():
    result = now_taipei_naive()
    assert result.tzinfo is None, "now_taipei_naive() 必須回 naive datetime"


def test_now_taipei_naive_matches_taipei_wall_clock():
    expected = datetime.now(TAIPEI_TZ).replace(tzinfo=None)
    result = now_taipei_naive()
    delta = abs((result - expected).total_seconds())
    assert delta < 1.0, f"差距 {delta}s 太大；helper 應與 datetime.now(TAIPEI_TZ) 一致"


def test_now_taipei_aware_has_tzinfo():
    result = now_taipei_aware()
    assert result.tzinfo is not None, "now_taipei_aware() 必須帶 tzinfo"
    assert result.tzinfo == TAIPEI_TZ, f"tzinfo 應為 TAIPEI_TZ，實際 {result.tzinfo}"


def test_ruff_dtz_config_loaded():
    """斷言 pyproject.toml 的 ruff config 啟用了 DTZ rule + 3 個 per-file-ignores。"""
    with open("pyproject.toml", "rb") as f:
        cfg = tomllib.load(f)
    select = cfg["tool"]["ruff"]["lint"]["select"]
    assert "DTZ" in select, f"ruff lint.select 必須含 'DTZ'，實際 {select}"
    ignores = cfg["tool"]["ruff"]["lint"]["per-file-ignores"]
    for path in ["tests/**/*.py", "alembic/versions/**/*.py", "utils/taipei_time.py"]:
        assert path in ignores, f"per-file-ignores 缺 {path}"
        assert "DTZ" in ignores[path], f"per-file-ignores[{path}] 必須含 DTZ"
