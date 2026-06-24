"""崩潰防護 P1：鎖爭用/死鎖統一映射 409 helper。

才藝待審報名的後台寫入端點（match / rematch / force_accept / restore /
update_registration_basic）取鎖序為 reg row → identity advisory，與公開
public_update（advisory → reg row）相反；同一筆 pending 報名「家長自助改課 +
後台審核」併發 → PG deadlock detector 中止其一（40P01）。後台原本落入通用
except → raise_safe_500 → 500 + Sentry 噪音。

修法：utils.errors.raise_lock_contention_or_500() —— 鎖爭用（40P01/55P03）→ 乾淨
409（可重試），其餘 → raise_safe_500（500）。與 public.py 報名熱路徑的 409 行為一致。
"""

import types

import pytest
from fastapi import HTTPException

from utils.errors import raise_lock_contention_or_500


def _op_error(pgcode: str) -> Exception:
    """模擬 SQLAlchemy OperationalError：.orig.pgcode 帶 driver SQLSTATE。"""
    e = Exception(f"simulated db error pgcode={pgcode}")
    e.orig = types.SimpleNamespace(pgcode=pgcode)
    return e


@pytest.mark.parametrize("pgcode", ["40P01", "55P03"])
def test_lock_contention_maps_to_409(pgcode):
    with pytest.raises(HTTPException) as ei:
        raise_lock_contention_or_500(_op_error(pgcode))
    assert ei.value.status_code == 409


def test_statement_timeout_is_not_lock_contention_maps_to_500():
    # 57014 = statement_timeout：非鎖的長查詢異常，仍應 500 / 上報，不可誤判可重試。
    with pytest.raises(HTTPException) as ei:
        raise_lock_contention_or_500(_op_error("57014"))
    assert ei.value.status_code == 500


def test_non_db_error_maps_to_500():
    with pytest.raises(HTTPException) as ei:
        raise_lock_contention_or_500(ValueError("boom"))
    assert ei.value.status_code == 500
